#!/usr/bin/env python3
"""FRIEND_CLAUDE — мастер развёртывания под свои аккаунты (двухфазный).

Этап 1 (до старта): спрашивает КРИТИЧНОЕ (домен, токен Claude, пароль консоли,
Telegram-бот, каталог проектов) → поднимает веб-консоль + бота.
Этап 2 (после старта): необязательное — Gemini, помощница (Telegram), голос,
поддомен превью.

Запуск:
    sudo python3 deploy/wizard.py            # этап 1 (потом предложит этап 2)
    sudo python3 deploy/wizard.py --phase2   # только этап 2
"""
import os, sys, json, time, secrets, subprocess, getpass
from pathlib import Path
from datetime import date

ROOT = Path("/opt/friend-claude")
BOT_DIR = ROOT / "bot"
WEB_DIR = ROOT / "web"
SCRIPTS = WEB_DIR / "scripts"
VENV = BOT_DIR / "venv"
PY = VENV / "bin" / "python"
PIP = VENV / "bin" / "pip"
ENV = BOT_DIR / ".env"
HOME = Path.home()
CFG = HOME / ".config" / "friend-claude"
MODELS_ENV = CFG / "models.env"
CLAUDE_ENV = CFG / "claude.env"
VOICE = CFG / "voice.json"
ANZ_TG = CFG / "anzhela_tg.json"
ASSIST = HOME / "friend-claude-assistant"
INBOX = HOME / "friend-claude-inbox"
PREVIEWS = Path("/srv/previews")
CADDY = Path("/etc/caddy/Caddyfile")
UNITS = Path("/etc/systemd/system")
TPL = Path(__file__).resolve().parent / "templates"
MARK = CFG / ".phase1-done"
CLAUDE_BIN = HOME / ".local" / "bin" / "claude"

G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; B = "\033[1m"; D = "\033[2m"; X = "\033[0m"


def ok(m): print(f"{G}✓{X} {m}")
def warn(m): print(f"{Y}!{X} {m}")
def err(m): print(f"{R}✗{X} {m}")
def head(m): print(f"\n{B}══ {m} ══{X}")


def ask(q, default=None, secret=False, required=True, digits=False):
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        v = (getpass.getpass if secret else input)(f"  {q}{suffix}: ").strip()
        if not v and default is not None:
            v = default
        if not v and not required:
            return ""
        if not v:
            warn("нужно значение"); continue
        if digits and not str(v).isdigit():
            warn("только цифры"); continue
        return v


def yn(q, default=True):
    v = input(f"  {q} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not v else v in ("y", "yes", "д", "да")


def merge_env(path, updates):
    """Обновить/добавить ключи в env-файле, сохранив остальное."""
    lines, seen = [], set()
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            k = ln.split("=", 1)[0].strip() if ("=" in ln and not ln.lstrip().startswith("#")) else None
            if k in updates:
                lines.append(f"{k}={updates[k]}"); seen.add(k)
            else:
                lines.append(ln)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8"); path.chmod(0o600)


def read_env_val(key):
    if ENV.exists():
        for ln in ENV.read_text(encoding="utf-8").splitlines():
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1].strip()
    return None


def write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8"); path.chmod(mode)


def sctl(*args):
    subprocess.run(["systemctl", *args], check=False)


def need_root():
    if os.geteuid() != 0:
        err("Запусти под root (sudo)."); sys.exit(1)


# ---------- шаблоны systemd ----------
UNIT_WEB = """[Unit]
Description=FRIEND_CLAUDE Web console
After=network-online.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=/opt/friend-claude/web
EnvironmentFile=/opt/friend-claude/bot/.env
ExecStart=/opt/friend-claude/bot/venv/bin/uvicorn server:app --host 127.0.0.1 --port 8787
Restart=always
RestartSec=3
KillMode=process

[Install]
WantedBy=multi-user.target
"""

UNIT_BOT = """[Unit]
Description=FRIEND_CLAUDE Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=/opt/friend-claude/bot
EnvironmentFile=/opt/friend-claude/bot/.env
ExecStart=/opt/friend-claude/bot/venv/bin/python /opt/friend-claude/bot/bot.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

UNIT_ANZ = """[Unit]
Description=Assistant Telegram listener (personal assistant inbound)
After=network-online.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=/opt/friend-claude/web/scripts
EnvironmentFile=/opt/friend-claude/bot/.env
ExecStart=/opt/friend-claude/bot/venv/bin/python /opt/friend-claude/web/scripts/anzhela_listener.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

# ---------- шаблон Caddy (плейсхолдеры, без f-string) ----------
_CADDY_CONSOLE = "__DOMAIN__ {\n    reverse_proxy 127.0.0.1:8787\n}\n"
_CADDY_AUTH = (
    "\n    @private path /_private/*\n"
    "    basic_auth @private {\n"
    "        __USER__ __HASH__\n"
    "    }\n"
)
_CADDY_PROJECTS = (
    "__DOMAIN__ {\n"
    "    root * /srv/previews\n"
    "    encode gzip\n"
    "__AUTH__"
    "    file_server\n\n"
    "    handle_errors {\n"
    "        @404 expression {err.status_code} == 404\n"
    "        handle @404 {\n"
    "            rewrite * /404.html\n"
    "            file_server {\n"
    "                status 404\n"
    "            }\n"
    "        }\n"
    "    }\n"
    "}\n"
)


def caddyfile(console_domain, projects_domain=None, user=None, hashv=None):
    out = _CADDY_CONSOLE.replace("__DOMAIN__", console_domain)
    if projects_domain:
        auth = _CADDY_AUTH.replace("__USER__", user).replace("__HASH__", hashv) if (user and hashv) else ""
        out += "\n" + _CADDY_PROJECTS.replace("__DOMAIN__", projects_domain).replace("__AUTH__", auth)
    return out


def caddy_console_domain():
    """Достать домен консоли из первой строки Caddyfile (для доп-рендера на этапе 2)."""
    try:
        return CADDY.read_text(encoding="utf-8").splitlines()[0].split()[0]
    except Exception:
        return None


# ============================ ЭТАП 1 ============================
def phase1():
    need_root()
    head("Этап 1 — критичное для старта")
    for d in (CFG, ASSIST, INBOX):
        d.mkdir(parents=True, exist_ok=True)
    if not PY.exists():
        err(f"venv не найден ({PY}). Сначала запусти deploy/install.sh."); sys.exit(1)

    console_domain = ask("Домен веб-консоли (напр. console.example.com)")

    print(f"\n  {D}Токен Claude Code (OAuth) — без него агент не запустится.{X}")
    print(f"  {D}Получить: на этом сервере `claude setup-token` (нужен вход в аккаунт Claude).{X}")
    if CLAUDE_BIN.exists() and yn("Запустить `claude setup-token` сейчас?", default=False):
        subprocess.run([str(CLAUDE_BIN), "setup-token"])
    claude_token = ask("Вставь токен Claude (sk-ant-oat01-...)", secret=True)

    web_pw = ask("Пароль веб-консоли", default=secrets.token_urlsafe(12))
    bot_token = ask("Telegram BOT_TOKEN (от @BotFather)")
    owner_id = ask("Твой Telegram OWNER_ID (числовой; узнать у @userinfobot)", digits=True)
    projects_dir = ask("Каталог проектов", default="/root/projects")
    Path(projects_dir).mkdir(parents=True, exist_ok=True)

    merge_env(ENV, {
        "BOT_TOKEN": bot_token, "OWNER_ID": owner_id, "PROJECTS_DIR": projects_dir,
        "CLAUDE_CODE_OAUTH_TOKEN": claude_token, "WEB_PASSWORD": web_pw, "BOT_DIR": str(BOT_DIR),
    })
    merge_env(CLAUDE_ENV, {"CLAUDE_CODE_OAUTH_TOKEN": claude_token})
    if not VOICE.exists():
        write(VOICE, json.dumps({"speaker": "xenia"}))
    # чистый список проектов для нового экземпляра (чтобы не унаследовать чужой из репо)
    write(BOT_DIR / "projects.json", "{}\n")
    ok("Конфиги записаны (.env, claude.env)")

    write(CADDY, caddyfile(console_domain))
    sctl("enable", "--now", "caddy"); sctl("reload", "caddy")
    ok("Caddy настроен (TLS выпустится сам, когда DNS домена укажет на этот сервер)")

    write(UNITS / "friend-claude-web.service", UNIT_WEB)
    write(UNITS / "friend-claude-bot.service", UNIT_BOT)
    sctl("daemon-reload")
    for name in ("friend-claude-web", "friend-claude-bot"):
        sctl("enable", "--now", name)
    time.sleep(3)

    MARK.write_text(str(int(time.time())))
    head("Готово — этап 1")
    ok(f"Консоль:  https://{console_domain}")
    ok(f"Пароль:   {web_pw}")
    print(f"  {D}Проверь: A-запись {console_domain} → IP этого сервера. Затем открой консоль.{X}")
    print(f"  {D}Статус:  systemctl status friend-claude-web friend-claude-bot{X}")
    if yn("\nПерейти к этапу 2 (Gemini, помощница, голос, превью)?", default=True):
        phase2()


# ============================ ЭТАП 2 ============================
def phase2():
    need_root()
    head("Этап 2 — необязательное (любой пункт можно пропустить)")

    # A) Gemini — генерация image/html/video
    if yn("Настроить генерацию контента (Gemini API key)?", default=True):
        key = ask("GEMINI_API_KEY (aistudio.google.com/apikey)", secret=True)
        merge_env(MODELS_ENV, {"GEMINI_API_KEY": key})
        ok("Gemini-ключ сохранён — проектам доступен shared/gen.py")

    # B) Превью-домен
    if yn("Поднять поддомен превью проектов (ссылки заказчикам)?", default=False):
        _setup_previews()

    # C) Помощница
    if yn("Поднять помощницу (отдельный Telegram-аккаунт со своим номером)?", default=False):
        _setup_assistant()

    # D) Голос
    if yn("Задать тембр голоса помощницы?", default=False):
        sp = ask("Тембр (xenia/baya/kseniya/eugene)", default="xenia")
        write(VOICE, json.dumps({"speaker": sp}))
        ok("Голос сохранён")

    head("Этап 2 завершён")
    print(f"  {D}Остальное (Syncthing-передача, netmon, graphify) — см. deploy/README.md.{X}")


def _setup_previews():
    pd = ask("Домен превью (напр. projects.example.com)")
    brand = ask("Бренд для страниц-заглушек", default=pd.split(".")[0])
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    idx = (TPL / "preview-index.html").read_text(encoding="utf-8")
    e404 = (TPL / "preview-404.html").read_text(encoding="utf-8")
    write(PREVIEWS / "index.html", idx.replace("{{BRAND}}", brand).replace("{{PROJECTS_DOMAIN}}", pd))
    write(PREVIEWS / "404.html", e404.replace("{{BRAND}}", brand))
    user = hashv = None
    if yn("Закрыть приватную зону /_private паролем?", default=True):
        user = ask("Логин", default="admin")
        pw = ask("Пароль", default=secrets.token_urlsafe(9))
        r = subprocess.run(["caddy", "hash-password", "--plaintext", pw], capture_output=True, text=True)
        hashv = r.stdout.strip()
        print(f"  {D}доступ к /_private: {user} / {pw}{X}")
    console = caddy_console_domain() or ask("Домен консоли (повтори)")
    write(CADDY, caddyfile(console, pd, user, hashv))
    sctl("reload", "caddy")
    ok(f"Превью: https://{pd}/  (клади файлы в /srv/previews/<проект>/)")


def _setup_assistant():
    head("Помощница — персона + вход в Telegram")
    name = ask("Имя помощницы (напр. Мария)")
    username = ask("Её @username (если задан, иначе Enter)", required=False)
    owner_name = ask("Как тебя звать (для её досье)")
    owner_id = read_env_val("OWNER_ID") or ask("Твой Telegram OWNER_ID", digits=True)

    # каркас памяти
    ASSIST.mkdir(parents=True, exist_ok=True)
    (ASSIST / "state").mkdir(exist_ok=True)
    persona = (TPL / "assistant-CLAUDE.md").read_text(encoding="utf-8")
    persona = persona.replace("{{ASSISTANT_NAME}}", name).replace("{{ASSISTANT_USERNAME}}", username or "(без username)")
    write(ASSIST / "CLAUDE.md", persona)
    if not (ASSIST / "owner.md").exists():
        seed = (TPL / "owner.md").read_text(encoding="utf-8")
        seed = seed.replace("{{OWNER_NAME}}", owner_name).replace("{{OWNER_ID}}", str(owner_id)).replace("{{DATE}}", str(date.today()))
        write(ASSIST / "owner.md", seed)
    for f, empty in (("board.json", '{"cards":[]}'), ("known.json", '{"known":[]}')):
        if not (ASSIST / f).exists():
            write(ASSIST / f, empty)
    if not (ASSIST / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=str(ASSIST), check=False)
    ok(f"Каркас памяти помощницы готов: {ASSIST}")

    # голосовые/озвучка нужны голосовые зависимости — доставим при желании
    if yn("Установить голосовые зависимости (STT/TTS: torch, faster-whisper, piper)? Тяжёлые ~1-2ГБ", default=False):
        subprocess.run([str(PIP), "install", "-q", "faster-whisper", "piper-tts", "torch", "omegaconf", "soundfile"], check=False)

    # вход в Telegram
    print(f"\n  {D}Нужны api_id/api_hash с https://my.telegram.org → API development tools,{X}")
    print(f"  {D}и номер телефона аккаунта помощницы (отдельный от твоего).{X}")
    api_id = ask("api_id", digits=True)
    api_hash = ask("api_hash")
    phone = ask("Телефон помощницы (+7...)")
    subprocess.run([str(PY), str(SCRIPTS / "tg_login_step1.py"), api_id, api_hash, phone], check=False)
    code = ask("Код входа (придёт в Telegram этого аккаунта)")
    twofa = ask("Пароль 2FA (если включён, иначе Enter)", required=False)
    args = [str(PY), str(SCRIPTS / "tg_login_step2.py"), code] + ([twofa] if twofa else [])
    subprocess.run(args, check=False)
    if not ANZ_TG.exists():
        err("Вход не завершён (нет anzhela_tg.json). Проверь код/2FA и повтори этап 2."); return

    # дописать owner в creds помощницы
    creds = json.loads(ANZ_TG.read_text(encoding="utf-8"))
    creds["owner_id"] = int(owner_id)
    creds["owner_name"] = owner_name
    ANZ_TG.write_text(json.dumps(creds, ensure_ascii=False), encoding="utf-8"); ANZ_TG.chmod(0o600)

    if yn("Задать имя/аватар помощнице в её Telegram-профиле?", default=False):
        subprocess.run([str(PY), str(SCRIPTS / "tg_set_profile.py"), name], check=False)

    write(UNITS / "anzhela-listener.service", UNIT_ANZ)
    sctl("daemon-reload")
    sctl("enable", "--now", "anzhela-listener")
    time.sleep(2)
    ok(f"Помощница «{name}» поднята и слушает Telegram (сервис anzhela-listener)")


def main():
    need_root()
    print(f"{B}FRIEND_CLAUDE — мастер развёртывания{X}")
    only2 = "--phase2" in sys.argv
    if only2:
        phase2()
    elif MARK.exists():
        warn("Этап 1 уже пройден.")
        if yn("Запустить этап 2?", default=True):
            phase2()
        elif yn("Переиграть этап 1 заново?", default=False):
            phase1()
    else:
        phase1()


if __name__ == "__main__":
    main()
