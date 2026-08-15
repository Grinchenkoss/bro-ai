#!/usr/bin/env python3
"""FRIEND_CLAUDE — Telegram-пульт к Claude Code на сервере.

Лок на владельца по OWNER_ID. Приём текста -> запуск агента в папке активного
проекта (headless, --allowedTools) -> ответ + стоимость. Мультипроект: /project.
"""
import asyncio
import html
import json
import os
import re
import shlex
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.enums import ChatAction
from aiogram.client.default import DefaultBotProperties

# ---------- конфиг ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/root/projects"))
APP_DIR = Path(__file__).resolve().parent
STATE_FILE = APP_DIR / "state.json"
PROJECTS_FILE = APP_DIR / "projects.json"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
ALLOWED_TOOLS = os.environ.get(
    "ALLOWED_TOOLS", "Read Write Edit Bash Grep Glob WebFetch WebSearch TodoWrite"
)
READONLY_TOOLS = os.environ.get("READONLY_TOOLS", "Read Grep Glob WebFetch WebSearch")
RUN_TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "1800"))
MAX_TURNS = int(os.environ.get("MAX_TURNS", "40"))  # потолок шагов агента на задачу
TG_LIMIT = 4000
WINDOW_SEC = int(os.environ.get("WINDOW_SEC", str(5 * 3600)))  # окно лимита подписки ~5ч
META_FILE = APP_DIR / "meta.json"  # per-project: protected, mode

run_lock = asyncio.Lock()

# ожидающие подтверждения задачи (для защищённых проектов): id -> (prompt, project)
_pending: dict[str, tuple[str, str]] = {}
_pending_seq = 0


def load_meta():
    return _load(META_FILE, {})


def proj_meta(name: str):
    m = load_meta().get(name, {})
    return {"protected": bool(m.get("protected", False)), "mode": m.get("mode", "safe")}


def set_proj_meta(name: str, **kw):
    m = load_meta()
    cur = m.get(name, {})
    cur.update(kw)
    m[name] = cur
    _save(META_FILE, m)


def fmt_tok(n: int) -> str:
    n = int(n or 0)
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}ч{m:02d}м"
    return f"{m}м"


# ---------- маленькое хранилище на json ----------
def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_state():
    return _load(STATE_FILE, {"active": None, "sessions": {}})


def save_state(s):
    _save(STATE_FILE, s)


def load_projects():
    return _load(PROJECTS_FILE, {})


def save_projects(p):
    _save(PROJECTS_FILE, p)


# ---------- лок на владельца ----------
class OwnerOnly(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id != OWNER_ID:
            return  # молча игнорируем всех, кроме владельца
        return await handler(event, data)


dp = Dispatcher()
dp.message.middleware(OwnerOnly())
dp.callback_query.middleware(OwnerOnly())

# постоянная клавиатура-меню (всегда под рукой у владельца)
BTN = {
    "status": "📊 Где я", "chats": "💬 Чаты", "new": "🔄 Новый чат",
    "projects": "📁 Проекты", "diff": "📋 Изменения", "pr": "🔀 Создать PR",
    "discard": "↩️ Откатить", "protect": "🔒 Защита", "mode": "👁 Режим",
    "usage": "📈 Расход", "help": "❓ Помощь",
}
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN["status"]), KeyboardButton(text=BTN["chats"]), KeyboardButton(text=BTN["new"])],
        [KeyboardButton(text=BTN["projects"]), KeyboardButton(text=BTN["diff"]), KeyboardButton(text=BTN["pr"])],
        [KeyboardButton(text=BTN["discard"]), KeyboardButton(text=BTN["protect"]), KeyboardButton(text=BTN["mode"])],
        [KeyboardButton(text=BTN["usage"]), KeyboardButton(text=BTN["help"])],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Напиши задачу или выбери кнопку…",
)


# ---------- вызов claude ----------
def _subenv():
    env = os.environ.copy()
    home = str(Path.home())
    env["PATH"] = f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    env.setdefault("HOME", home)
    return env


async def run_shell(args, cwd):
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=_subenv(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace").strip()


async def has_changes(cwd) -> bool:
    rc, out, _ = await run_shell(["git", "status", "--porcelain"], cwd)
    return rc == 0 and bool(out.strip())


def sessions_dir(cwd: str) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    return Path.home() / ".claude" / "projects" / slug


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                return b["text"]
    return ""


def session_label(path: Path) -> str:
    """Название чата: кастомное имя (type=custom-title, как в родном Claude Code),
    иначе первое сообщение пользователя."""
    title = None
    first_user = None
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") == "custom-title" and r.get("customTitle"):
                    title = r["customTitle"]  # последнее переименование — актуальное
                    continue
                if first_user is None:
                    msg = r.get("message") if isinstance(r.get("message"), dict) else None
                    is_user = r.get("type") == "user" or (msg and msg.get("role") == "user")
                    if is_user:
                        txt = " ".join(_extract_text((msg or r).get("content")).split())
                        if txt and not txt.startswith("<"):
                            first_user = txt[:38]
    except Exception:
        pass
    return title or first_user or "(без названия)"


def list_project_sessions(cwd: str, limit: int = 8):
    """Список чатов проекта: [{id, label, mtime}], свежие сверху."""
    d = sessions_dir(cwd)
    if not d.is_dir():
        return []
    items = []
    for f in d.glob("*.jsonl"):
        try:
            items.append({"id": f.stem, "mtime": f.stat().st_mtime, "path": f})
        except Exception:
            continue
    items.sort(key=lambda x: x["mtime"], reverse=True)
    items = items[:limit]
    for it in items:
        it["label"] = session_label(it["path"])
    return items


def session_summary(path: Path):
    """Последняя задача (запрос пользователя) и последний ответ ассистента из чата."""
    last_user = None
    last_asst = None
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                msg = r.get("message") if isinstance(r.get("message"), dict) else None
                role = (msg or {}).get("role") or r.get("type")
                txt = " ".join(_extract_text((msg or r).get("content")).split())
                if not txt or txt.startswith("<") or txt.startswith("Caveat"):
                    continue
                if role == "user":
                    last_user = txt
                elif role == "assistant":
                    last_asst = txt
    except Exception:
        pass
    return last_user, last_asst


# ожидание нового имени чата (переименование): {"id":.., "project":..}
_rename_target = {"id": None, "project": None}


def set_session_title(cwd: str, sid: str, name: str) -> bool:
    """Переименовать чат: дописать запись custom-title (как делает родной Claude Code)."""
    path = sessions_dir(cwd) / f"{sid}.jsonl"
    if not path.is_file():
        return False
    rec = {"type": "custom-title", "customTitle": name[:60], "sessionId": sid}
    try:
        with path.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


async def run_claude(prompt: str, cwd: str, resume_id, mode: str = "safe"):
    env = os.environ.copy()
    env["PATH"] = f"{Path.home() / '.local/bin'}:{env.get('PATH', '')}"
    tools = READONLY_TOOLS if mode == "readonly" else ALLOWED_TOOLS
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json",
           "--allowedTools", tools, "--max-turns", str(MAX_TURNS)]
    if resume_id:
        cmd += ["--resume", resume_id]  # продолжить выбранный чат (общий с Mac)
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "text": f"⏱ Превышен таймаут {RUN_TIMEOUT}s, процесс убит."}

    stdout = out.decode("utf-8", "replace").strip()
    stderr = err.decode("utf-8", "replace").strip()
    if not stdout:
        tail = stderr[-1500:] if stderr else f"код возврата {proc.returncode}"
        return {"ok": False, "text": f"❌ Пустой ответ. stderr:\n{tail}"}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": False, "text": f"❌ Не разобрал JSON:\n{stdout[:1500]}"}

    usage = data.get("usage", {}) or {}
    ctx_window = 0
    for mu in (data.get("modelUsage") or {}).values():
        ctx_window = max(ctx_window, int(mu.get("contextWindow", 0) or 0))
    return {
        "ok": not data.get("is_error", False),
        "text": data.get("result", "(пусто)"),
        "session_id": data.get("session_id"),
        "cost": data.get("total_cost_usd") or 0.0,
        "turns": data.get("num_turns"),
        "dur": data.get("duration_ms"),
        "usage": usage,
        "ctx_window": ctx_window,
    }


def usage_numbers(res):
    """Из usage считаем: новые токены запроса и заполненность контекста."""
    u = res.get("usage", {}) or {}
    inp = int(u.get("input_tokens", 0) or 0)
    out = int(u.get("output_tokens", 0) or 0)
    cc = int(u.get("cache_creation_input_tokens", 0) or 0)
    cr = int(u.get("cache_read_input_tokens", 0) or 0)
    req_new = inp + out + cc           # реально обработанные (новые) токены запроса
    ctx_used = inp + cc + cr           # сколько контекста ушло в модель
    return req_new, ctx_used


def window_add(res):
    """Копим токены/оценку$ за скользящее окно WINDOW_SEC. Возвращаем (потрачено_ток, оценка$, сброс_через_сек)."""
    st = load_state()
    w = st.get("window") or {}
    now = time.time()
    start = w.get("start", 0)
    if not start or now - start >= WINDOW_SEC:
        start, tokens, cost = now, 0, 0.0
    else:
        tokens, cost = int(w.get("tokens", 0)), float(w.get("cost", 0.0))  # noqa
    req_new, _ = usage_numbers(res)
    tokens += req_new
    cost += float(res.get("cost") or 0.0)
    st["window"] = {"start": start, "tokens": tokens, "cost": cost}
    save_state(st)
    reset_in = int(WINDOW_SEC - (now - start))
    return tokens, cost, reset_in


def fmt_footer(res, project):
    req_new, ctx_used = usage_numbers(res)
    ctx_win = res.get("ctx_window") or 0
    wtok, wcost, reset_in = window_add(res)

    l1 = [f"📁 {project}"]
    if res.get("turns"):
        l1.append(f"{res['turns']} шаг")
    if res.get("dur"):
        l1.append(f"{res['dur'] / 1000:.1f}s")

    l2 = [f"🧮 запрос ~{fmt_tok(req_new)} ток"]
    if ctx_win:
        l2.append(f"контекст {fmt_tok(ctx_used)}/{fmt_tok(ctx_win)}")
    if res.get("cost"):
        l2.append(f"≈${res['cost']:.4f}")

    l3 = f"📊 окно 5ч: ~{fmt_tok(wtok)} ток (≈${wcost:.3f}) · сброс через {fmt_dur(reset_in)}"
    return f"{' · '.join(l1)}\n{' · '.join(l2)}\n{l3}"


# ---------- команды ----------
HELP = (
    "🤖 <b>FRIEND_CLAUDE</b> — пульт к Claude на сервере.\n\n"
    "Просто пиши задачу текстом — выполню в активном проекте.\n\n"
    "<b>Команды:</b>\n"
    "/projects — список проектов и активный\n"
    "/project <i>name</i> — переключить активный проект\n"
    "/addproject <i>name</i> [<i>path</i>] — зарегистрировать проект\n"
    "/ls — папки в каталоге проектов\n"
    "💬 Чаты — список чатов проекта, продолжить любой\n"
    "/new — начать новый чат (прежние сохраняются)\n"
    "/status — что активно сейчас\n"
    "/usage — расход токенов за 5ч-окно (моя оценка)\n\n"
    "<b>Git / ревью:</b>\n"
    "/diff — показать незакоммиченные правки\n"
    "/pr <i>[заголовок]</i> — ветка + коммит + push + Pull Request\n"
    "/discard — откатить незакоммиченные правки\n\n"
    "<b>Безопасность:</b>\n"
    "/protect — вкл/выкл подтверждение задач для проекта\n"
    "/mode <i>safe|readonly</i> — правки+bash или только чтение"
)


@dp.message(Command("usage"))
async def cmd_usage(m: Message):
    st = load_state()
    w = st.get("window") or {}
    start = w.get("start", 0)
    now = time.time()
    if not start or now - start >= WINDOW_SEC:
        await m.answer(
            "📊 Окно пустое — за последние 5ч запросов не было.\n\n"
            "<i>Это моя счётная за скользящее окно, а не квота подписки. "
            "Реальный остаток лимита — в интерактивном claude: команда /usage.</i>"
        )
        return
    tokens = int(w.get("tokens", 0))
    cost = float(w.get("cost", 0.0))
    reset_in = int(WINDOW_SEC - (now - start))
    await m.answer(
        f"📊 <b>За текущее 5ч-окно</b>\n"
        f"Токенов: ~{fmt_tok(tokens)}\n"
        f"Оценка по API-тарифу: ≈${cost:.3f} <i>(не списание — у тебя подписка)</i>\n"
        f"Сброс окна через: {fmt_dur(reset_in)}\n\n"
        f"<i>Это мой подсчёт, не квота подписки (её размер CLI не отдаёт). "
        f"Реальный остаток лимита: <code>ssh -t friend-claude claude</code> → /usage.</i>"
    )


def header():
    """Строка «где я»: проект · защита · режим."""
    active, _ = active_project()
    if not active:
        return "📍 Проект не выбран — нажми «📁 Проекты»"
    meta = proj_meta(active)
    lock = "🔒" if meta["protected"] else "🔓"
    mode = "👁readonly" if meta["mode"] == "readonly" else "✏️safe"
    return f"📍 <b>{active}</b> · {lock} · {mode}"


@dp.message(Command("start", "help"))
async def cmd_help(m: Message):
    await m.answer(f"{header()}\n\n{HELP}", reply_markup=MAIN_KB)


@dp.message(Command("ls"))
async def cmd_ls(m: Message):
    if not PROJECTS_DIR.exists():
        await m.answer(f"Каталог {PROJECTS_DIR} не существует.")
        return
    dirs = sorted(p.name for p in PROJECTS_DIR.iterdir() if p.is_dir())
    if not dirs:
        await m.answer(f"В {PROJECTS_DIR} пусто. Склонируй репозиторий туда и /addproject.")
        return
    lines = []
    for d in dirs:
        git = "◆" if (PROJECTS_DIR / d / ".git").exists() else "▫️"
        lines.append(f"{git} {d}")
    await m.answer(f"<b>{PROJECTS_DIR}</b>:\n" + "\n".join(lines))


@dp.message(Command("projects"))
async def cmd_projects(m: Message):
    projects = load_projects()
    active = load_state().get("active")
    if not projects:
        await m.answer("Проектов нет. Добавь: <code>/addproject имя /путь</code>")
        return
    lines = []
    for name, path in projects.items():
        mark = "✅" if name == active else "▫️"
        lines.append(f"{mark} <b>{name}</b> — <code>{path}</code>")
    await m.answer("Проекты:\n" + "\n".join(lines))


@dp.message(Command("addproject"))
async def cmd_addproject(m: Message, command: CommandObject):
    args = shlex.split(command.args or "")
    if not args:
        await m.answer("Формат: <code>/addproject имя [/путь]</code>")
        return
    name = args[0]
    path = Path(args[1]) if len(args) > 1 else PROJECTS_DIR / name
    if not path.is_dir():
        await m.answer(f"Папка не найдена: <code>{path}</code>\nСклонируй проект туда сначала.")
        return
    projects = load_projects()
    projects[name] = str(path.resolve())
    save_projects(projects)
    st = load_state()
    if st.get("active") is None:
        st["active"] = name
        save_state(st)
    await m.answer(f"✅ Проект <b>{name}</b> → <code>{path}</code>\nАктивный: {load_state()['active']}")


@dp.message(Command("project"))
async def cmd_project(m: Message, command: CommandObject):
    name = (command.args or "").strip()
    projects = load_projects()
    if not name:
        await m.answer("Формат: <code>/project имя</code>. Список: /projects")
        return
    if name not in projects:
        await m.answer(f"Нет такого проекта: {name}. /projects — список, /addproject — добавить.")
        return
    st = load_state()
    st["active"] = name
    save_state(st)
    await m.answer(f"✅ Активный проект: <b>{name}</b>\n<code>{projects[name]}</code>")


@dp.message(Command("new"))
async def cmd_new(m: Message):
    st = load_state()
    active = st.get("active")
    if not active:
        await m.answer("Активного проекта нет.")
        return
    st["sessions"].pop(active, None)
    st.setdefault("session_sel", {}).pop(active, None)
    st.setdefault("fresh", {})[active] = True
    save_state(st)
    await m.answer(
        f"🔄 <b>{active}</b>: следующее сообщение начнёт НОВЫЙ чат.\n"
        f"<i>Прежние чаты не удаляются — они в «💬 Чаты».</i>",
        reply_markup=MAIN_KB,
    )


@dp.message(Command("status"))
async def cmd_status(m: Message):
    st = load_state()
    active = st.get("active")
    if not active:
        await m.answer("Активного проекта нет. /projects")
        return
    projects = load_projects()
    cwd = projects.get(active, "")
    meta = proj_meta(active)
    prot = "🔒 защищён (подтверждение)" if meta["protected"] else "🔓 обычный"
    mode = "👁 readonly" if meta["mode"] == "readonly" else "✏️ safe (правки+bash)"
    fresh = st.get("fresh", {}).get(active, False)
    sel = st.get("session_sel", {}).get(active)
    sd = sessions_dir(cwd)
    n_chats = len(list(sd.glob("*.jsonl"))) if sd.is_dir() else 0
    if fresh or not sel:
        chat = f"новый при следующем сообщении (всего чатов: {n_chats})"
    else:
        chat = f"выбран {sel[:8]}… (всего чатов: {n_chats})"
    await m.answer(
        f"📁 <b>{active}</b>\n<code>{cwd}</code>\n"
        f"💬 Чат: {chat}\n🛡 Защита: {prot}\n👓 Режим: {mode}\n⚙️ Потолок шагов: {MAX_TURNS}",
        reply_markup=MAIN_KB,
    )


@dp.message(Command("protect"))
async def cmd_protect(m: Message):
    active, _ = active_project()
    if not active:
        await m.answer("Активного проекта нет. /projects")
        return
    new_val = not proj_meta(active)["protected"]
    set_proj_meta(active, protected=new_val)
    if new_val:
        await m.answer(f"🔒 Проект <b>{active}</b> защищён — каждая задача теперь требует подтверждения кнопкой.")
    else:
        await m.answer(f"🔓 С проекта <b>{active}</b> снята защита — задачи выполняются сразу.")


@dp.message(Command("mode"))
async def cmd_mode(m: Message, command: CommandObject):
    active, _ = active_project()
    if not active:
        await m.answer("Активного проекта нет. /projects")
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("safe", "readonly"):
        cur = proj_meta(active)["mode"]
        await m.answer(f"Режим <b>{active}</b>: {cur}.\n"
                       f"<code>/mode safe</code> — правки+bash · <code>/mode readonly</code> — только чтение.")
        return
    set_proj_meta(active, mode=arg)
    await m.answer(f"✅ Режим <b>{active}</b>: {arg}"
                   + (" (агент только смотрит, без правок и bash)" if arg == "readonly" else ""))


def active_project():
    st = load_state()
    active = st.get("active")
    projects = load_projects()
    if not active or active not in projects:
        return None, None
    return active, projects[active]


@dp.message(Command("diff"))
async def cmd_diff(m: Message):
    active, cwd = active_project()
    if not active:
        await m.answer("Активного проекта нет. /projects")
        return
    rc, status, _ = await run_shell(["git", "status", "--short"], cwd)
    if rc != 0:
        await m.answer("Это не git-репозиторий.")
        return
    if not status:
        await m.answer(f"📁 <b>{active}</b>: рабочее дерево чистое, менять нечего.")
        return
    _, stat, _ = await run_shell(["git", "diff", "--stat"], cwd)
    body = f"📁 <b>{active}</b> — незакоммиченное:\n<pre>{html.escape(status)}</pre>"
    if stat:
        body += f"\n<pre>{html.escape(stat[:1500])}</pre>"
    await m.answer(body[:TG_LIMIT])


@dp.message(Command("discard"))
async def cmd_discard(m: Message):
    active, cwd = active_project()
    if not active:
        await m.answer("Активного проекта нет. /projects")
        return
    if not await has_changes(cwd):
        await m.answer(f"📁 <b>{active}</b>: и так чисто.")
        return
    await run_shell(["git", "reset", "--hard"], cwd)
    await run_shell(["git", "clean", "-fd"], cwd)
    await m.answer(f"🗑 <b>{active}</b>: незакоммиченные изменения откачены.")


@dp.message(Command("pr"))
async def cmd_pr(m: Message, command: CommandObject):
    title = (command.args or "").strip()
    await do_pr(m, title)


async def do_pr(m: Message, title: str):
    active, cwd = active_project()
    if not active:
        await m.answer("Активного проекта нет. /projects")
        return
    title = title.strip() or f"Правки от Claude ({time.strftime('%Y-%m-%d %H:%M')})"

    # git-репозиторий?
    rc, _, _ = await run_shell(["git", "rev-parse", "--is-inside-work-tree"], cwd)
    if rc != 0:
        await m.answer(f"📁 <b>{active}</b> — это не git-репозиторий, PR не сделать.")
        return
    # есть remote origin?
    rc, origin, _ = await run_shell(["git", "remote", "get-url", "origin"], cwd)
    if rc != 0 or not origin:
        await m.answer(f"📁 <b>{active}</b> — нет remote <code>origin</code>. Нужен GitHub-репозиторий.")
        return
    # есть что коммитить?
    if not await has_changes(cwd):
        await m.answer(f"📁 <b>{active}</b> — нет изменений для PR. Сначала дай агенту задачу.")
        return

    async with run_lock:
        status = await m.answer("🔀 Готовлю PR…")
        # базовая ветка = текущая
        _, base, _ = await run_shell(["git", "branch", "--show-current"], cwd)
        base = base or "main"
        branch = f"claude/patch-{time.strftime('%Y%m%d-%H%M%S')}"

        steps = [
            (["git", "switch", "-c", branch], "создаю ветку"),
            (["git", "add", "-A"], "добавляю файлы"),
            (["git", "commit", "-m", title], "коммит"),
            (["git", "push", "-u", "origin", branch], "пуш"),
        ]
        for args, label in steps:
            rc, out, err = await run_shell(args, cwd)
            if rc != 0:
                await run_shell(["git", "switch", base], cwd)  # вернуться назад
                await status.edit_text(f"❌ Шаг «{label}» упал:\n<pre>{html.escape((err or out)[:1200])}</pre>")
                return

        # тело PR: краткая статистика
        _, stat, _ = await run_shell(["git", "diff", "--stat", f"{base}...{branch}"], cwd)
        pr_body = f"Автоматический PR от FRIEND_CLAUDE.\n\n```\n{stat[:2000]}\n```"
        rc, out, err = await run_shell(
            ["gh", "pr", "create", "--base", base, "--head", branch,
             "--title", title, "--body", pr_body], cwd)
        await run_shell(["git", "switch", base], cwd)  # рабочее дерево обратно на базу

        if rc != 0:
            await status.edit_text(
                f"⚠️ Ветка запушена (<code>{branch}</code>), но PR не создался:\n<pre>{html.escape((err or out)[:1000])}</pre>")
            return
        url = next((ln for ln in out.splitlines() if ln.startswith("http")), out)
        await status.edit_text(
            f"✅ <b>PR готов</b>\n📁 {active} · ветка <code>{branch}</code>\n"
            f"«{title}»\n\n🔗 {url}")


async def execute_task(m: Message, prompt: str, active: str, cwd: str, mode: str):
    if run_lock.locked():
        await m.answer("⏳ Уже выполняю предыдущую задачу — дождись ответа.")
        return
    st = load_state()
    fresh = st.get("fresh", {}).get(active, False)
    sel = st.get("session_sel", {}).get(active)
    if not fresh and not sel:
        # выбора нет — продолжаем ПОСЛЕДНИЙ чат, а не плодим новые
        latest = list_project_sessions(cwd, limit=1)
        if latest:
            sel = latest[0]["id"]
    resume_id = None if fresh else sel
    async with run_lock:
        tag = " 👁readonly" if mode == "readonly" else ""
        cont_tag = " • новый чат" if not resume_id else ""
        status = await m.answer(f"⏳ Работаю…{tag}{cont_tag}")
        await m.bot.send_chat_action(m.chat.id, ChatAction.TYPING)
        res = await run_claude(prompt, cwd, resume_id, mode)

        st = load_state()
        if fresh:
            st.setdefault("fresh", {})[active] = False
        if res.get("session_id"):
            st.setdefault("session_sel", {})[active] = res["session_id"]
            st.setdefault("sessions", {})[active] = res["session_id"]
        save_state(st)

        text = res["text"]
        footer = fmt_footer(res, active) if res.get("ok") else f"📁 {active}"
        body = text if len(text) <= TG_LIMIT else text[:TG_LIMIT] + "\n\n…(обрезано)"
        msg = f"{html.escape(body)}\n\n<code>{html.escape(footer)}</code>"
        try:
            await status.edit_text(msg)
        except Exception:
            # последний рубеж — plain text, чтобы ответ точно долетел
            try:
                await status.edit_text(body[:TG_LIMIT] or "(пусто)", parse_mode=None)
            except Exception:
                await m.answer(body[:TG_LIMIT] or "(пусто)", parse_mode=None)


async def show_projects_menu(m: Message):
    projects = load_projects()
    active = load_state().get("active")
    if not projects:
        await m.answer("Проектов нет. Добавь: <code>/addproject имя /путь</code>", reply_markup=MAIN_KB)
        return
    rows = [[InlineKeyboardButton(
        text=("✅ " if n == active else "▫️ ") + n, callback_data=f"proj:{n}")] for n in projects]
    await m.answer("Выбери активный проект:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def show_mode_menu(m: Message):
    active, _ = active_project()
    if not active:
        await m.answer("Сначала выбери проект (📁 Проекты).", reply_markup=MAIN_KB)
        return
    cur = proj_meta(active)["mode"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("• " if cur == "safe" else "") + "✏️ safe — правки + bash", callback_data="mode:safe")],
        [InlineKeyboardButton(text=("• " if cur == "readonly" else "") + "👁 readonly — только чтение", callback_data="mode:readonly")],
    ])
    await m.answer(f"Режим для <b>{active}</b> (сейчас: {cur}):", reply_markup=kb)


async def show_sessions_menu(m: Message):
    active, cwd = active_project()
    if not active:
        await m.answer("Сначала выбери проект (📁 Проекты).", reply_markup=MAIN_KB)
        return
    sessions = list_project_sessions(cwd)
    if not sessions:
        await m.answer(
            f"В проекте <b>{active}</b> пока нет чатов.\nНапиши задачу — начнётся первый.",
            reply_markup=MAIN_KB)
        return
    sel = load_state().get("session_sel", {}).get(active)
    rows = [[InlineKeyboardButton(text="➕ Новый чат", callback_data="newchat")]]
    for s in sessions:
        mark = "✅ " if s["id"] == sel else "💬 "
        rows.append([
            InlineKeyboardButton(text=f"{mark}{s['label']}", callback_data=f"sess:{s['id']}"),
            InlineKeyboardButton(text="✏️", callback_data=f"ren:{s['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"del:{s['id']}"),
        ])
    await m.answer(
        f"Чаты проекта <b>{active}</b>:\n💬 продолжить · ✏️ переименовать · 🗑 удалить",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query()
async def on_callback(cq: CallbackQuery):
    data = cq.data or ""
    if data == "newchat":
        active, _ = active_project()
        if active:
            st = load_state()
            st.setdefault("session_sel", {}).pop(active, None)
            st.setdefault("fresh", {})[active] = True
            save_state(st)
        await cq.answer("Новый чат")
        await cq.message.edit_text(f"🔄 Новый чат начнётся со следующего сообщения.\n{header()}")
        return
    if data.startswith("sess:"):
        sid = data[5:]
        active, _ = active_project()
        if not active:
            await cq.answer("Нет проекта", show_alert=True)
            return
        st = load_state()
        st.setdefault("session_sel", {})[active] = sid
        st.setdefault("fresh", {})[active] = False
        save_state(st)
        await cq.answer("Чат выбран")
        _, cwd = active_project()
        last_user, last_asst = session_summary(sessions_dir(cwd) / f"{sid}.jsonl")
        parts = [f"✅ Выбран чат.\n{header()}"]
        if last_user:
            parts.append(f"\n📝 <b>Последняя задача:</b>\n{html.escape(last_user[:350])}")
        if last_asst:
            parts.append(f"\n💬 <b>Итог:</b>\n{html.escape(last_asst[:500])}")
        parts.append("\n<i>Пиши сообщение — продолжу в этом контексте.</i>")
        body = "\n".join(parts)
        try:
            await cq.message.edit_text(body[:TG_LIMIT])
        except Exception:
            await cq.message.edit_text(f"✅ Выбран чат.\n{header()}", parse_mode=None)
        return
    if data.startswith("ren:"):
        sid = data[4:]
        active, _ = active_project()
        _rename_target["id"] = sid
        _rename_target["project"] = active
        await cq.answer()
        await cq.message.edit_text("✏️ Пришли новое название чата одним сообщением.")
        return
    if data.startswith("del:"):
        sid = data[4:]
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delok:{sid}"),
            InlineKeyboardButton(text="✖️ Нет", callback_data="delno"),
        ]])
        await cq.answer()
        await cq.message.edit_text("🗑 Удалить этот чат безвозвратно?", reply_markup=kb)
        return
    if data == "delno":
        await cq.answer("Отменено")
        await cq.message.edit_text("Удаление отменено.")
        return
    if data.startswith("delok:"):
        sid = data[6:]
        active, cwd = active_project()
        if active:
            try:
                (sessions_dir(cwd) / f"{sid}.jsonl").unlink()
            except Exception:
                pass
            st = load_state()
            if st.get("session_sel", {}).get(active) == sid:
                st["session_sel"].pop(active, None)
                save_state(st)
        await cq.answer("Удалено")
        await cq.message.edit_text(f"🗑 Чат удалён.\n{header()}")
        return
    if data.startswith("no:"):
        _pending.pop(data[3:], None)
        await cq.message.edit_text("✖️ Отменено.")
        await cq.answer()
        return
    if data.startswith("run:"):
        item = _pending.pop(data[4:], None)
        if not item:
            await cq.answer("Запрос устарел — отправь задачу заново.", show_alert=True)
            return
        prompt, project = item
        projects = load_projects()
        if project not in projects:
            await cq.answer("Проект пропал.", show_alert=True)
            return
        await cq.answer()
        await cq.message.edit_text(f"🔒→▶️ Подтверждено. Выполняю в <b>{project}</b>…")
        await execute_task(cq.message, prompt, project, projects[project], proj_meta(project)["mode"])
        return
    if data.startswith("proj:"):
        name = data[5:]
        if name not in load_projects():
            await cq.answer("Нет такого проекта", show_alert=True)
            return
        st = load_state()
        st["active"] = name
        save_state(st)
        await cq.answer(f"Активен: {name}")
        await cq.message.edit_text(f"✅ Переключено.\n{header()}")
        return
    if data.startswith("mode:"):
        val = data[5:]
        active, _ = active_project()
        if not active:
            await cq.answer("Нет проекта", show_alert=True)
            return
        set_proj_meta(active, mode=val)
        await cq.answer(f"Режим: {val}")
        await cq.message.edit_text(f"✅ Режим <b>{active}</b>: {val}\n{header()}")
        return


BUTTON_ROUTES = {
    BTN["status"]: cmd_status,
    BTN["chats"]: show_sessions_menu,
    BTN["projects"]: show_projects_menu,
    BTN["new"]: cmd_new,
    BTN["diff"]: cmd_diff,
    BTN["pr"]: lambda m: do_pr(m, ""),
    BTN["discard"]: cmd_discard,
    BTN["protect"]: cmd_protect,
    BTN["mode"]: show_mode_menu,
    BTN["usage"]: cmd_usage,
    BTN["help"]: cmd_help,
}


@dp.message()
async def on_text(m: Message):
    if not m.text:
        await m.answer("Пока принимаю только текст.")
        return
    # перехват нового имени чата (после нажатия ✏️)
    if _rename_target["id"]:
        sid = _rename_target["id"]
        proj = _rename_target["project"]
        _rename_target["id"] = None
        _rename_target["project"] = None
        projects = load_projects()
        if proj in projects and set_session_title(projects[proj], sid, m.text):
            await m.answer(f"✅ Чат переименован в «{html.escape(m.text.strip()[:60])}».", reply_markup=MAIN_KB)
        else:
            await m.answer("Не удалось переименовать (чат не найден).", reply_markup=MAIN_KB)
        return
    route = BUTTON_ROUTES.get(m.text)
    if route:
        await route(m)
        return
    active, cwd = active_project()
    if not active:
        await m.answer("Сначала выбери проект: нажми «📁 Проекты».", reply_markup=MAIN_KB)
        return
    meta = proj_meta(active)
    if meta["protected"]:
        global _pending_seq
        _pending_seq += 1
        pid = str(_pending_seq)
        _pending[pid] = (m.text, active)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="▶️ Выполнить", callback_data=f"run:{pid}"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data=f"no:{pid}"),
        ]])
        await m.answer(
            f"🔒 Проект <b>{active}</b> защищён. Выполнить задачу?\n<i>«{html.escape(m.text[:200])}»</i>",
            reply_markup=kb,
        )
        return
    await execute_task(m, m.text, active, cwd, meta["mode"])


async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    print(f"FRIEND_CLAUDE bot up. owner={OWNER_ID} projects_dir={PROJECTS_DIR}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
