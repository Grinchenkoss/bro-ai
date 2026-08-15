#!/usr/bin/env python3
"""FRIEND_CLAUDE — веб-бэкенд (Ф6, расширенный).

«Мозг на сервере» + браузерный UI: блоки проектов, окна агентов, живой стрим,
управление чатами (rename/delete), PR/diff, создание проектов из UI,
мультимодель (Claude=подписка; Gemini/GPT=по API-ключам), Gemini-дизайн.

Запуск: uvicorn server:app --host 127.0.0.1 --port 8787
"""
import asyncio
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Query, Depends, UploadFile, File
from fastapi.responses import FileResponse

# ---------- конфиг ----------
APP_DIR = Path(__file__).resolve().parent
STATIC = APP_DIR / "static"
BOT_DIR = Path(os.environ.get("BOT_DIR", "/opt/friend-claude/bot"))
PROJECTS_FILE = BOT_DIR / "projects.json"
STATE_FILE = BOT_DIR / "state.json"
META_FILE = BOT_DIR / "meta.json"
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/root/projects"))
KEYS_FILE = Path.home() / ".config" / "friend-claude" / "models.env"
BAL_FILE = Path.home() / ".config" / "friend-claude" / "gemini_balance.json"
GEN_DIR = Path.home() / "friend-claude-gen"          # хранилище генераций Gemini
GEN_META = GEN_DIR / "index.json"
INBOX_DIR = Path.home() / "friend-claude-inbox"      # передаточная папка (файлы от юзера агенту)
INBOX_DIR.mkdir(parents=True, exist_ok=True)
try:
    INBOX_DIR.chmod(0o700)
except Exception:
    pass

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
ALLOWED_TOOLS = os.environ.get(
    "ALLOWED_TOOLS", "Read Write Edit Bash Grep Glob WebFetch WebSearch TodoWrite")
READONLY_TOOLS = os.environ.get("READONLY_TOOLS", "Read Grep Glob WebFetch WebSearch")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "40"))
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")  # обязателен в проде

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")           # текст/факты
GEMINI_DESIGN_MODEL = os.environ.get("GEMINI_DESIGN_MODEL", "gemini-pro-latest")  # сайты/HTML (сильнее)
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")  # картинки (Nano Banana)
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

app = FastAPI(title="FRIEND_CLAUDE Web")


# ---------- хранилище ----------
def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_projects():
    return _load(PROJECTS_FILE, {})


def load_state():
    return _load(STATE_FILE, {"active": None, "sessions": {}})


def load_meta():
    return _load(META_FILE, {})


def proj_meta(name):
    m = load_meta().get(name, {})
    return {"protected": bool(m.get("protected", False)), "mode": m.get("mode", "safe")}


def sessions_dir(cwd: str) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    return Path.home() / ".claude" / "projects" / slug


def normalize_session(f: Path):
    """Унификация «есть в вебе = есть в VS Code». Расширение VS Code показывает только свои
    сессии (entrypoint=claude-vscode) и прячет созданные веб-консолью (claude -p → sdk-cli).
    Каждый прогон консоли ДОПИСЫВАЕТ новые sdk-cli записи, поэтому заменяем ВСЕ вхождения
    (не только голову — иначе свежие записи в конце остаются sdk-cli и чат снова прячется).
    Идемпотентно, --resume не ломается. Огромные архивные сессии из консоли не запускаются."""
    try:
        if not f.is_file() or f.stat().st_size > 120 * 1024 * 1024:
            return
        raw = f.read_text(errors="ignore")
        if '"entrypoint":"sdk-cli"' not in raw:
            return
        f.write_text(raw.replace('"entrypoint":"sdk-cli"', '"entrypoint":"claude-vscode"'))
    except Exception:
        pass


# ---------- личный помощник (спец-проект, скрыт из списка проектов) ----------
ASSIST_NAME = "__assistant__"
ASSIST_DIR = Path.home() / "friend-claude-assistant"


def _ensure_assistant():
    ASSIST_DIR.mkdir(parents=True, exist_ok=True)
    board = ASSIST_DIR / "board.json"
    if not board.exists():
        board.write_text('{"cards": []}', encoding="utf-8")
    projects = load_projects()
    if projects.get(ASSIST_NAME) != str(ASSIST_DIR):
        projects[ASSIST_NAME] = str(ASSIST_DIR)
        _save(PROJECTS_FILE, projects)


_ensure_assistant()


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                return b["text"]
    return ""


def session_meta(path: Path):
    title = first_user = last_user = last_asst = None
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") == "custom-title" and r.get("customTitle"):
                    title = r["customTitle"]
                    continue
                msg = r.get("message") if isinstance(r.get("message"), dict) else None
                role = (msg or {}).get("role") or r.get("type")
                raw = _extract_text((msg or r).get("content")).strip()   # с переносами строк
                txt = " ".join(raw.split())                             # схлопнутый — для фильтра/заголовка
                if not txt or txt.startswith("<") or txt.startswith("Caveat"):
                    continue
                if role == "user":
                    last_user = raw
                    if first_user is None:
                        first_user = txt
                elif role == "assistant":
                    last_asst = raw
    except Exception:
        pass
    return {
        "title": title or (first_user[:60] if first_user else "(без названия)"),
        "last_user": last_user,
        "last_asst": last_asst,
    }


def list_project_sessions(cwd: str, limit: int = 40):
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
    out = []
    for it in items[:limit]:
        m = session_meta(it["path"])
        out.append({"id": it["id"], "mtime": it["mtime"], **m})
    return out


def load_model_keys():
    keys = {}
    try:
        for line in KEYS_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip()
    except Exception:
        pass
    return keys


# ---------- auth ----------
def check_auth(token: str):
    if not WEB_PASSWORD:
        raise HTTPException(500, "WEB_PASSWORD не задан на сервере")
    if not token or not hmac.compare_digest(token, WEB_PASSWORD):
        raise HTTPException(401, "не авторизован")
    return True


def auth_header(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "").strip()
    check_auth(token)
    return True


def _run_env():
    env = os.environ.copy()
    env["PATH"] = f"{Path.home() / '.local/bin'}:{env.get('PATH', '')}"
    return env


def _mcp_flags(cwd, tools):
    """Если у проекта есть свой .mcp.json (напр. пиновый graphify) — подключаем
    его в headless-режиме и разрешаем инструменты сервера. Иначе — без изменений.
    --strict-mcp-config: грузим ТОЛЬКО этот конфиг (без авто-доверия/подсказок)."""
    try:
        cfg = Path(cwd) / ".mcp.json"
        if cfg.is_file():
            return ["--mcp-config", str(cfg), "--strict-mcp-config"], tools + " mcp__graphify"
    except Exception:
        pass
    return [], tools


async def _sh(args, cwd):
    p = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=_run_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    return p.returncode, out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace").strip()


# ---------- API: чтение ----------
@app.post("/api/login")
async def login(body: dict):
    check_auth((body or {}).get("password", ""))
    return {"ok": True, "token": WEB_PASSWORD}


@app.get("/api/projects")
async def api_projects(_: bool = Depends(auth_header)):
    projects = load_projects()
    active = load_state().get("active")
    out = []
    for name, path in projects.items():
        if name == ASSIST_NAME:
            continue
        m = proj_meta(name)
        n = len(list(sessions_dir(path).glob("*.jsonl"))) if sessions_dir(path).is_dir() else 0
        out.append({"name": name, "path": path, "active": name == active,
                    "protected": m["protected"], "mode": m["mode"], "chats": n})
    return {"projects": out}


@app.get("/api/chats")
async def api_chats(project: str = Query(...), _: bool = Depends(auth_header)):
    projects = load_projects()
    if project not in projects:
        raise HTTPException(404, "нет проекта")
    return {"chats": list_project_sessions(projects[project])}


# ---------- API: управление чатами ----------
def _find_session_file(project: str, chat_id: str) -> Path:
    projects = load_projects()
    if project not in projects:
        raise HTTPException(404, "нет проекта")
    d = sessions_dir(projects[project])
    f = d / f"{chat_id}.jsonl"
    if not f.is_file():
        raise HTTPException(404, "нет чата")
    return f


@app.post("/api/chat/rename")
async def api_chat_rename(body: dict, _: bool = Depends(auth_header)):
    project = (body or {}).get("project")
    chat_id = (body or {}).get("chat")
    title = (str((body or {}).get("title") or "")).strip()[:80]
    if not title:
        raise HTTPException(400, "пустое имя")
    f = _find_session_file(project, chat_id)
    with f.open("a") as fh:
        fh.write(json.dumps({"type": "custom-title", "customTitle": title}, ensure_ascii=False) + "\n")
    return {"ok": True, "title": title}


@app.post("/api/chat/delete")
async def api_chat_delete(body: dict, _: bool = Depends(auth_header)):
    project = (body or {}).get("project")
    chat_id = (body or {}).get("chat")
    f = _find_session_file(project, chat_id)
    f.unlink()
    # почистить выбор в state.json, если указывал на этот чат
    try:
        st = load_state()
        sel = st.get("session_sel", {})
        if sel.get(project) == chat_id:
            sel.pop(project, None)
            st["session_sel"] = sel
            _save(STATE_FILE, st)
    except Exception:
        pass
    return {"ok": True}


# ---------- API: создание проекта ----------
@app.post("/api/project/create")
async def api_project_create(body: dict, _: bool = Depends(auth_header)):
    body = body or {}
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("name", "")).strip())
    if not name:
        raise HTTPException(400, "нужно имя проекта (буквы/цифры/-/_)")
    projects = load_projects()
    if name in projects:
        raise HTTPException(400, "проект с таким именем уже есть")
    target = PROJECTS_DIR / name
    if target.exists():
        raise HTTPException(400, f"папка {target} уже существует")
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    repo = str(body.get("repo", "")).strip()          # owner/name или URL — клонировать существующий
    create_remote = bool(body.get("create_remote"))    # создать новый репо на GitHub
    private = body.get("private", True)

    if repo:
        if "://" in repo or repo.endswith(".git"):
            rc, out, err = await _sh(["git", "clone", repo, str(target)], PROJECTS_DIR)
        else:
            rc, out, err = await _sh(["gh", "repo", "clone", repo, str(target)], PROJECTS_DIR)
        if rc != 0:
            return {"ok": False, "error": ("clone: " + (err or out))[:800]}
    elif create_remote:
        vis = "--private" if private else "--public"
        rc, out, err = await _sh(["gh", "repo", "create", name, vis, "--clone"], PROJECTS_DIR)
        if rc != 0:
            return {"ok": False, "error": ("gh create: " + (err or out))[:800]}
    else:
        target.mkdir(parents=True)
        await _sh(["git", "init"], target)
        (target / "README.md").write_text(f"# {name}\n")

    projects[name] = str(target)
    _save(PROJECTS_FILE, projects)
    meta = load_meta()
    meta[name] = {"protected": False, "mode": "safe"}
    _save(META_FILE, meta)
    return {"ok": True, "name": name, "path": str(target)}


@app.post("/api/project/protect")
async def api_project_protect(body: dict, _: bool = Depends(auth_header)):
    project = (body or {}).get("project")
    val = bool((body or {}).get("protected"))
    projects = load_projects()
    if project not in projects:
        raise HTTPException(404, "нет проекта")
    meta = load_meta()
    m = meta.get(project, {"protected": False, "mode": "safe"})
    m["protected"] = val
    meta[project] = m
    _save(META_FILE, meta)
    return {"ok": True, "protected": val}


# ---------- API: ключи моделей (значения в браузер не отдаём) ----------
@app.get("/api/keys")
async def api_keys_status(_: bool = Depends(auth_header)):
    k = load_model_keys()
    return {"gemini": bool(k.get("GEMINI_API_KEY")), "openai": bool(k.get("OPENAI_API_KEY"))}


@app.post("/api/keys")
async def api_keys_save(body: dict, _: bool = Depends(auth_header)):
    body = body or {}
    keys = load_model_keys()
    if body.get("gemini") is not None:
        v = str(body["gemini"]).strip()
        if v:
            keys["GEMINI_API_KEY"] = v
        else:
            keys.pop("GEMINI_API_KEY", None)
    if body.get("openai") is not None:
        v = str(body["openai"]).strip()
        if v:
            keys["OPENAI_API_KEY"] = v
        else:
            keys.pop("OPENAI_API_KEY", None)
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEYS_FILE.write_text("".join(f"{k}={v}\n" for k, v in keys.items()))
    os.chmod(KEYS_FILE, 0o600)
    return {"ok": True, "gemini": bool(keys.get("GEMINI_API_KEY")), "openai": bool(keys.get("OPENAI_API_KEY"))}


# ---------- API: мультимодель (Gemini / GPT по API-ключам) ----------
def _extract_html(text: str) -> str:
    m = re.search(r"```(?:html)?\s*(.*?)```", text, re.S)
    body = m.group(1).strip() if m else text.strip()
    return body if "<" in body else ""


@app.post("/api/model/ask")
async def api_model_ask(body: dict, _: bool = Depends(auth_header)):
    body = body or {}
    model = (body.get("model") or "gemini").lower()
    prompt = str(body.get("prompt", "")).strip()
    design = bool(body.get("design"))
    search = bool(body.get("search"))     # Google-grounding: факты с источниками
    if not prompt:
        raise HTTPException(400, "пустой запрос")
    keys = load_model_keys()
    sys_design = ("Ты дизайнер интерфейсов. Верни ОДИН самодостаточный HTML-документ "
                  "(со встроенным CSS, без внешних ссылок) — готовую страницу. Только код в блоке ```html.")
    full = (sys_design + "\n\nЗадача: " + prompt) if design else prompt

    try:
        async with httpx.AsyncClient(timeout=120) as cli:
            if model == "gemini":
                key = keys.get("GEMINI_API_KEY")
                if not key:
                    return {"ok": False, "error": "Gemini-ключ не задан (⚙ Настройки → ключи)"}
                mdl = GEMINI_DESIGN_MODEL if design else GEMINI_MODEL
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={key}"
                payload = {"contents": [{"parts": [{"text": full}]}]}
                if search:
                    payload["tools"] = [{"google_search": {}}]
                r = await cli.post(url, json=payload)
                if r.status_code != 200:
                    return {"ok": False, "error": f"Gemini {r.status_code}: {r.text[:300]}"}
                data = r.json()
                um = data.get("usageMetadata") or {}
                pin, pout = GEMINI_PRICING.get(mdl, (0.30, 2.50))
                cost = um.get("promptTokenCount", 0) / 1e6 * pin + um.get("candidatesTokenCount", 0) / 1e6 * pout
                _charge(cost)
                cand = (data.get("candidates") or [{}])[0]
                text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
                out = {"ok": True, "text": text, "cost": round(cost, 5)}
                gm = cand.get("groundingMetadata") or {}
                srcs = []
                for c in (gm.get("groundingChunks") or []):
                    w = c.get("web") or {}
                    if w.get("uri"):
                        srcs.append({"title": w.get("title") or w.get("uri"), "uri": w.get("uri")})
                if srcs:
                    out["sources"] = srcs[:8]
                if design:
                    out["html"] = _extract_html(text)
                return out
            elif model in ("gpt", "openai"):
                key = keys.get("OPENAI_API_KEY")
                if not key:
                    return {"ok": False, "error": "OpenAI-ключ не задан (⚙ Настройки → ключи)"}
                r = await cli.post("https://api.openai.com/v1/chat/completions",
                                   headers={"Authorization": f"Bearer {key}"},
                                   json={"model": OPENAI_MODEL, "messages": [{"role": "user", "content": full}]})
                if r.status_code != 200:
                    return {"ok": False, "error": f"OpenAI {r.status_code}: {r.text[:300]}"}
                out = {"ok": True, "text": r.json()["choices"][0]["message"]["content"]}
                if design:
                    out["html"] = _extract_html(out["text"])
                return out
            return {"ok": False, "error": "неизвестная модель"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.post("/api/gemini/image")
async def api_gemini_image(body: dict, _: bool = Depends(auth_header)):
    prompt = str((body or {}).get("prompt", "")).strip()
    image = str((body or {}).get("image", "")).strip()       # data:...;base64,... — для редактирования
    if not prompt:
        raise HTTPException(400, "пустой запрос")
    key = load_model_keys().get("GEMINI_API_KEY")
    if not key:
        return {"ok": False, "error": "Gemini-ключ не задан (⚙ Настройки → ключи)"}
    parts = [{"text": prompt}]
    if image and "base64," in image:
        try:
            mime = image.split(";")[0].split(":", 1)[1]
            parts.insert(0, {"inlineData": {"mimeType": mime, "data": image.split("base64,", 1)[1]}})
        except Exception:
            pass
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_IMAGE_MODEL}:generateContent?key={key}"
    try:
        async with httpx.AsyncClient(timeout=150) as cli:
            r = await cli.post(url, json={"contents": [{"parts": parts}]})
        if r.status_code != 200:
            return {"ok": False, "error": f"Gemini {r.status_code}: {r.text[:300]}"}
        parts = ((r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", []))
        img = None
        text = ""
        for p in parts:
            inl = p.get("inlineData") or p.get("inline_data")
            if inl and inl.get("data"):
                mime = inl.get("mimeType") or inl.get("mime_type") or "image/png"
                img = f"data:{mime};base64,{inl['data']}"
            elif p.get("text"):
                text += p["text"]
        if not img:
            return {"ok": False, "error": "картинка не вернулась" + (": " + text[:180] if text else "")}
        _charge(IMAGE_PRICE)
        return {"ok": True, "image": img, "text": text, "cost": IMAGE_PRICE}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ---------- Gemini: оценка стоимости + видео (Veo) ----------
GEMINI_PRICING = {  # $/1M токенов (вход, выход)
    "gemini-flash-latest": (0.30, 2.50),
    "gemini-pro-latest": (1.25, 10.00),
}
IMAGE_PRICE = 0.039
VEO_PRICE_SEC = {"veo-3.1-generate-preview": 0.40, "veo-3.1-fast-generate-preview": 0.10, "veo-3.1-lite-generate-preview": 0.05}


async def _count_tokens(mdl, text, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:countTokens?key={key}"
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(url, json={"contents": [{"parts": [{"text": text}]}]})
    return r.json().get("totalTokens", len(text) // 4) if r.status_code == 200 else len(text) // 4


@app.post("/api/gemini/estimate")
async def api_gemini_estimate(body: dict, _: bool = Depends(auth_header)):
    body = body or {}
    mode = body.get("mode", "site")
    key = load_model_keys().get("GEMINI_API_KEY")
    if not key:
        return {"ok": False}
    if mode == "image":
        return {"ok": True, "cost": IMAGE_PRICE, "note": "1 картинка"}
    if mode == "video":
        mdl = body.get("vmodel", "veo-3.1-fast-generate-preview")
        dur = max(1, min(int(body.get("duration") or 8), 20))
        return {"ok": True, "cost": round(VEO_PRICE_SEC.get(mdl, 0.10) * dur, 2), "note": f"{dur}с видео"}
    mdl = "gemini-pro-latest" if mode == "site" else "gemini-flash-latest"
    pin, pout = GEMINI_PRICING.get(mdl, (0.30, 2.50))
    try:
        intok = await _count_tokens(mdl, str(body.get("text", "")), key)
    except Exception:
        intok = len(str(body.get("text", ""))) // 4
    out_assumed = {"site": 3500, "facts": 900, "doc": 1400}.get(mode, 900)
    cost = intok / 1e6 * pin + out_assumed / 1e6 * pout
    return {"ok": True, "cost": round(cost, 4), "in_tokens": intok, "out_assumed": out_assumed}


@app.post("/api/gemini/video")
async def api_gemini_video(body: dict, _: bool = Depends(auth_header)):
    prompt = str((body or {}).get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(400, "пустой запрос")
    key = load_model_keys().get("GEMINI_API_KEY")
    if not key:
        return {"ok": False, "error": "Gemini-ключ не задан"}
    mdl = (body or {}).get("vmodel") or "veo-3.1-fast-generate-preview"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:predictLongRunning?key={key}"
    try:
        async with httpx.AsyncClient(timeout=60) as cli:
            r = await cli.post(url, json={"instances": [{"prompt": prompt}], "parameters": {"aspectRatio": "16:9"}})
        if r.status_code != 200:
            return {"ok": False, "error": f"Veo {r.status_code}: {r.text[:300]}"}
        _charge(VEO_PRICE_SEC.get(mdl, 0.10) * 8)   # ~8с ролик
        return {"ok": True, "op": r.json().get("name")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.get("/api/gemini/video/status")
async def api_gemini_video_status(op: str = Query(...), _: bool = Depends(auth_header)):
    key = load_model_keys().get("GEMINI_API_KEY")
    if not key:
        return {"ok": False, "error": "нет ключа"}
    try:
        async with httpx.AsyncClient(timeout=30) as cli:
            r = await cli.get(f"https://generativelanguage.googleapis.com/v1beta/{op}?key={key}")
        d = r.json()
        if not d.get("done"):
            return {"ok": True, "done": False}
        m = re.search(r'"(https://[^"]+?(?:files/[^"]+|\.mp4[^"]*))"', json.dumps(d))
        if not m:
            return {"ok": True, "done": True, "error": "URI видео не найден", "raw": json.dumps(d)[:300]}
        uri = m.group(1)
        async with httpx.AsyncClient(timeout=180) as cli:
            vr = await cli.get(uri + (("&" if "?" in uri else "?") + "key=" + key), follow_redirects=True)
        import base64
        return {"ok": True, "done": True, "video": "data:video/mp4;base64," + base64.b64encode(vr.content).decode()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ---------- баланс Gemini (ручной) + списание по факту ----------
def _bal_load():
    return _load(BAL_FILE, {"balance": None, "spent": 0.0})


def _charge(cost):
    try:
        if not cost or cost <= 0:
            return
        d = _bal_load()
        d["spent"] = round(float(d.get("spent") or 0) + cost, 6)
        if d.get("balance") is not None:
            d["balance"] = round(float(d["balance"]) - cost, 6)
        _save(BAL_FILE, d)
    except Exception:
        pass


@app.get("/api/gemini/balance")
async def api_bal_get(_: bool = Depends(auth_header)):
    d = _bal_load()
    return {"ok": True, "balance": d.get("balance"), "spent": round(float(d.get("spent") or 0), 4)}


@app.post("/api/gemini/balance")
async def api_bal_set(body: dict, _: bool = Depends(auth_header)):
    d = _bal_load()
    body = body or {}
    if body.get("balance") is not None:
        try:
            d["balance"] = round(float(body["balance"]), 4)
        except Exception:
            raise HTTPException(400, "плохое число")
    if body.get("reset_spent"):
        d["spent"] = 0.0
    _save(BAL_FILE, d)
    return {"ok": True, "balance": d.get("balance"), "spent": round(float(d.get("spent") or 0), 4)}


# ---------- галерея генераций Gemini ----------
def _gen_list():
    return _load(GEN_META, [])


@app.post("/api/gen/save")
async def api_gen_save(body: dict, _: bool = Depends(auth_header)):
    body = body or {}
    typ = body.get("type")
    data = body.get("data") or ""
    prompt = str(body.get("prompt", ""))[:200]
    if typ not in ("site", "image", "video") or not data:
        raise HTTPException(400, "нет данных")
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    gid = "g" + str(int(time.time() * 1000))
    fn = GEN_DIR / f"{gid}.{ {'site':'html','image':'png','video':'mp4'}[typ] }"
    try:
        if typ == "site":
            fn.write_text(data)
        else:
            import base64
            b64 = data.split("base64,", 1)[1] if "base64," in data else data
            fn.write_bytes(base64.b64decode(b64))
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    items = _gen_list()
    items.insert(0, {"id": gid, "type": typ, "prompt": prompt, "file": fn.name,
                     "ts": datetime.now().isoformat(timespec="seconds")})
    _save(GEN_META, items[:300])
    return {"ok": True, "id": gid}


@app.get("/api/gen/list")
async def api_gen_list(_: bool = Depends(auth_header)):
    return {"items": _gen_list()}


@app.post("/api/gen/delete")
async def api_gen_delete(body: dict, _: bool = Depends(auth_header)):
    gid = (body or {}).get("id")
    items = _gen_list()
    for it in items:
        if it["id"] == gid:
            try:
                (GEN_DIR / it["file"]).unlink()
            except Exception:
                pass
    _save(GEN_META, [x for x in items if x["id"] != gid])
    return {"ok": True}


@app.get("/api/gen/file")
async def api_gen_file(id: str = Query(...), token: str = Query(""), authorization: str = Header(default="")):
    tok = token or authorization.replace("Bearer ", "").strip()
    if not WEB_PASSWORD or not hmac.compare_digest(tok, WEB_PASSWORD):
        raise HTTPException(401, "не авторизован")
    for it in _gen_list():
        if it["id"] == id:
            f = GEN_DIR / it["file"]
            if f.is_file():
                mt = {"html": "text/html", "png": "image/png", "mp4": "video/mp4"}.get(it["file"].rsplit(".", 1)[-1], "application/octet-stream")
                return FileResponse(str(f), media_type=mt)
    raise HTTPException(404, "нет файла")


# ---------- передаточная папка (inbox): файлы от юзера напрямую агенту ----------
def _inbox_safe(name: str) -> str:
    name = os.path.basename(name or "").strip()
    name = re.sub(r"[^\w.\- ()@+]+", "_", name, flags=re.UNICODE)
    return (name[:120] or "file")


def _inbox_unique(name: str) -> Path:
    dest = INBOX_DIR / name
    if not dest.exists():
        return dest
    stem, dot, ext = name.partition(".")
    i = 2
    while True:
        cand = INBOX_DIR / (f"{stem}-{i}{dot}{ext}")
        if not cand.exists():
            return cand
        i += 1


@app.post("/api/inbox/upload")
async def api_inbox_upload(file: UploadFile = File(...), _: bool = Depends(auth_header)):
    name = _inbox_safe(file.filename)
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "файл больше 200 МБ")
    dest = _inbox_unique(name)
    dest.write_bytes(data)
    try:
        dest.chmod(0o600)
    except Exception:
        pass
    return {"ok": True, "name": dest.name, "size": len(data), "path": str(dest)}


@app.get("/api/inbox/list")
async def api_inbox_list(_: bool = Depends(auth_header)):
    items = []
    for f in INBOX_DIR.iterdir():
        if f.is_file() and not f.name.startswith("."):
            st = f.stat()
            items.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime, "path": str(f)})
    items.sort(key=lambda x: -x["mtime"])
    return {"files": items, "dir": str(INBOX_DIR)}


@app.post("/api/inbox/delete")
async def api_inbox_delete(body: dict, _: bool = Depends(auth_header)):
    f = INBOX_DIR / _inbox_safe((body or {}).get("name", ""))
    if f.is_file():
        f.unlink()
    return {"ok": True}


@app.get("/api/inbox/file")
async def api_inbox_file(name: str = Query(...), token: str = Query(""), authorization: str = Header(default="")):
    tok = token or authorization.replace("Bearer ", "").strip()
    if not WEB_PASSWORD or not hmac.compare_digest(tok, WEB_PASSWORD):
        raise HTTPException(401, "не авторизован")
    f = INBOX_DIR / _inbox_safe(name)
    if not f.is_file():
        raise HTTPException(404, "нет файла")
    return FileResponse(str(f), filename=f.name, media_type="application/octet-stream")


# ---------- личный помощник: доска и память ----------
@app.get("/api/assistant/board")
async def api_assistant_board(_: bool = Depends(auth_header)):
    f = ASSIST_DIR / "board.json"
    try:
        data = json.loads(f.read_text())
        cards = data.get("cards", []) if isinstance(data, dict) else []
    except Exception:
        cards = []
    return {"cards": cards if isinstance(cards, list) else []}


@app.post("/api/assistant/board")
async def api_assistant_board_save(body: dict, _: bool = Depends(auth_header)):
    cards = (body or {}).get("cards", [])
    if not isinstance(cards, list):
        raise HTTPException(400, "cards должен быть списком")
    (ASSIST_DIR / "board.json").write_text(
        json.dumps({"cards": cards}, ensure_ascii=False, indent=2), encoding="utf-8")
    _assist_snapshot("правка доски (board.json)")
    return {"ok": True, "count": len(cards)}


@app.get("/api/assistant/memory")
async def api_assistant_memory(_: bool = Depends(auth_header)):
    f = ASSIST_DIR / "owner.md"
    return {"text": f.read_text(encoding="utf-8") if f.is_file() else ""}


def _assist_snapshot(msg: str = "ручная правка из консоли"):
    """Гит-снимок памяти Анжелы (чтобы ручные правки из консоли тоже откатывались)."""
    try:
        import subprocess
        for args in (["add", "-A"], ["commit", "-q", "-m", f"console: {msg[:80]}"]):
            subprocess.run(["git", "-C", str(ASSIST_DIR), *args],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass


@app.post("/api/assistant/memory")
async def api_assistant_memory_save(body: dict, _: bool = Depends(auth_header)):
    (ASSIST_DIR / "owner.md").write_text(str((body or {}).get("text", "")), encoding="utf-8")
    _assist_snapshot("правка памяти (owner.md)")
    return {"ok": True}


@app.get("/api/assistant/rules")
async def api_assistant_rules(_: bool = Depends(auth_header)):
    f = ASSIST_DIR / "CLAUDE.md"
    return {"text": f.read_text(encoding="utf-8") if f.is_file() else ""}


@app.post("/api/assistant/rules")
async def api_assistant_rules_save(body: dict, _: bool = Depends(auth_header)):
    (ASSIST_DIR / "CLAUDE.md").write_text(str((body or {}).get("text", "")), encoding="utf-8")
    _assist_snapshot("правка правил (CLAUDE.md)")
    return {"ok": True}


@app.get("/api/assistant/known")
async def api_assistant_known(_: bool = Depends(auth_header)):
    f = ASSIST_DIR / "known.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        known = data.get("known", []) if isinstance(data, dict) else []
    except Exception:
        known = []
    return {"known": known if isinstance(known, list) else []}


@app.post("/api/assistant/known")
async def api_assistant_known_save(body: dict, _: bool = Depends(auth_header)):
    known = (body or {}).get("known", [])
    if not isinstance(known, list):
        raise HTTPException(400, "known должен быть списком")
    (ASSIST_DIR / "known.json").write_text(
        json.dumps({"known": known}, ensure_ascii=False, indent=2), encoding="utf-8")
    _assist_snapshot("правка контактов (known.json)")
    return {"ok": True, "count": len(known)}


# ---------- история сообщений чата (для восстановления окна) ----------
@app.get("/api/chat/messages")
async def api_chat_messages(project: str = Query(...), chat: str = Query(...),
                            limit: int = Query(40), _: bool = Depends(auth_header)):
    f = _find_session_file(project, chat)
    msgs = []
    for line in f.open(errors="ignore"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        m = r.get("message") if isinstance(r.get("message"), dict) else None
        role = (m or {}).get("role")
        content = (m or {}).get("content")
        if role == "user":
            txt = content if isinstance(content, str) else _extract_text(content)
            if txt and not txt.startswith("<") and not txt.startswith("Caveat"):
                msgs.append({"role": "user", "text": txt})
        elif role == "assistant":
            if isinstance(content, list):
                for b in content:
                    if b.get("type") == "text" and b.get("text"):
                        msgs.append({"role": "assistant", "text": b["text"]})
                    elif b.get("type") == "tool_use":
                        msgs.append({"role": "tool", "name": b.get("name", "tool"), "input": b.get("input") or {}})
            elif isinstance(content, str) and content:
                msgs.append({"role": "assistant", "text": content})
    return {"messages": msgs[-int(limit):]}


# ---------- приём картинки к задаче агента (агент читает файл через Read) ----------
@app.post("/api/attach")
async def api_attach(body: dict, _: bool = Depends(auth_header)):
    body = body or {}
    project = body.get("project")
    image = body.get("image", "")
    if project not in load_projects():
        raise HTTPException(404, "нет проекта")
    if "base64," not in image:
        raise HTTPException(400, "нет картинки")
    import base64
    d = Path("/tmp/fc-attach")
    d.mkdir(exist_ok=True)
    ext = (image.split(";")[0].split("/")[-1] or "png")[:5]
    fn = d / f"a{int(time.time() * 1000)}.{ext}"
    try:
        fn.write_bytes(base64.b64decode(image.split("base64,", 1)[1]))
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    return {"ok": True, "path": str(fn)}


# ---------- Фоновые прогоны агента: живут независимо от WS (клиент можно закрыть) ----------
class _Run:
    __slots__ = ("proc", "events", "done", "code", "stderr", "subs", "notify",
                 "project", "mode", "cwd", "started", "finished", "task", "stop", "sid")

    def __init__(self, proc, project, mode, cwd):
        self.proc = proc
        self.events = []            # буфер stream-json событий (реплей при до-цепе)
        self.done = False
        self.code = None
        self.stderr = ""
        self.subs = 0              # число живых зрителей (для справки)
        self.notify = asyncio.Event()
        self.project = project
        self.mode = mode
        self.cwd = cwd
        self.started = time.time()
        self.finished = None
        self.task = None
        self.stop = False
        self.sid = None


RUNS: dict = {}            # ключ (chat_id или session_id) -> _Run
RUN_TTL_DONE = 1800        # держим завершённый прогон 30 мин для до-цепа/просмотра результата
MAX_BUFFER = 6000          # потолок буфера событий (защита памяти)


def _bump(run):
    n = run.notify
    run.notify = asyncio.Event()
    n.set()


def _gc_runs():
    now = time.time()
    for k, r in list(RUNS.items()):
        if r.done and r.finished and now - r.finished > RUN_TTL_DONE:
            RUNS.pop(k, None)


async def _drain_run(run, primary_key):
    """Фон: тянем stdout прогона до конца, копим в буфер, будим зрителей.
    Живёт независимо от того, подключён ли сейчас WS-клиент."""
    try:
        async for raw in run.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            run.events.append(ev)
            if len(run.events) > MAX_BUFFER:
                del run.events[:len(run.events) - MAX_BUFFER]
            if isinstance(ev, dict) and ev.get("session_id") and not run.sid:
                run.sid = ev["session_id"]
                if run.sid not in RUNS:
                    RUNS[run.sid] = run     # новый чат теперь можно до-цепить по session_id
            _bump(run)
        await run.proc.wait()
        run.code = run.proc.returncode
        run.stderr = (await run.proc.stderr.read()).decode("utf-8", "replace") if run.proc.stderr else ""
    except Exception as e:
        run.stderr = str(e)[:500]
    finally:
        run.done = True
        run.finished = time.time()
        _bump(run)
        try:
            sid = run.sid or (primary_key if isinstance(primary_key, str) else None)
            if sid and run.cwd:
                normalize_session(sessions_dir(run.cwd) / f"{sid}.jsonl")
        except Exception:
            pass
    _gc_runs()


@app.websocket("/ws")
async def ws_run(ws: WebSocket):
    await ws.accept()
    run = None
    try:
        token = ws.query_params.get("token", "")
        if not WEB_PASSWORD or not hmac.compare_digest(token, WEB_PASSWORD):
            await ws.send_json({"type": "error", "error": "unauthorized"})
            await ws.close()
            return
        init = await ws.receive_json()
        project = init.get("project")
        prompt = init.get("prompt", "")
        chat_id = init.get("chat")
        attach = bool(init.get("attach"))
        try:
            frm = int(init.get("from") or 0)
        except Exception:
            frm = 0
        projects = load_projects()
        if project not in projects:
            await ws.send_json({"type": "error", "error": "нет проекта"})
            return
        cwd = projects[project]
        existing = RUNS.get(chat_id) if chat_id else None

        if attach or (existing is not None and not prompt):
            # ДО-ЦЕП к живому/недавнему прогону
            run = existing
            if run is None:
                await ws.send_json({"type": "gone"})
                return
            await ws.send_json({"type": "attach", "project": run.project, "mode": run.mode, "done": run.done})
        elif existing is not None and not existing.done and existing.proc.returncode is None:
            # уже есть живой прогон этой сессии — не плодим второй, просто до-цепляемся
            run = existing
            await ws.send_json({"type": "attach", "project": run.project, "mode": run.mode, "done": run.done})
        else:
            # СТАРТ нового фонового прогона
            wmode = (init.get("mode") or "").lower()
            if wmode == "readonly":
                mode, tools = "readonly", READONLY_TOOLS
            elif wmode == "auto":
                mode, tools = "auto", ALLOWED_TOOLS
            else:
                mode = proj_meta(project)["mode"]
                tools = READONLY_TOOLS if mode == "readonly" else ALLOWED_TOOLS
            pmode = "plan" if mode == "readonly" else "acceptEdits"
            mcp_extra, tools = _mcp_flags(cwd, tools)
            cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json", "--verbose",
                   "--allowedTools", tools, "--permission-mode", pmode, "--max-turns", str(MAX_TURNS)] + mcp_extra
            cmodel = (init.get("cmodel") or "").lower()
            if cmodel in ("opus", "sonnet", "haiku", "fable"):
                cmd += ["--model", cmodel]
            if chat_id:
                cmd += ["--resume", chat_id]
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, env=_run_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,        # отвязываем прогон от процесса-обработчика/WS
                limit=32 * 1024 * 1024)
            run = _Run(proc, project, mode, cwd)
            if chat_id:
                RUNS[chat_id] = run
            run.task = asyncio.create_task(_drain_run(run, chat_id))
            await ws.send_json({"type": "start", "project": project, "mode": mode})

        # ---- зритель: реплей с курсора + живой хвост ----
        cursor = max(0, min(frm, len(run.events))) if attach else 0
        sid_sent = False

        async def watch_stop():
            try:
                while True:
                    msg = await ws.receive_json()
                    if isinstance(msg, dict) and msg.get("action") == "stop":
                        run.stop = True
                        try:
                            run.proc.terminate()
                        except Exception:
                            pass
                        return
            except Exception:
                return

        watcher = asyncio.create_task(watch_stop())
        run.subs += 1
        try:
            while True:
                w = run.notify
                if run.sid and not sid_sent:
                    await asyncio.wait_for(ws.send_json({"type": "session", "id": run.sid}), timeout=8)
                    sid_sent = True
                while cursor < len(run.events):
                    await asyncio.wait_for(ws.send_json({"type": "event", "event": run.events[cursor]}), timeout=8)
                    cursor += 1
                if run.done:
                    await asyncio.wait_for(
                        ws.send_json({"type": "done", "code": run.code, "stderr": (run.stderr or "")[-500:]}), timeout=8)
                    break
                try:
                    await asyncio.wait_for(w.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        finally:
            run.subs = max(0, run.subs - 1)
            watcher.cancel()
    except WebSocketDisconnect:
        pass                                    # разрыв ≠ стоп: прогон живёт в фоне
    except Exception as e:  # noqa
        try:
            await ws.send_json({"type": "error", "error": str(e)[:300]})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
    _gc_runs()


# ---------- обновления продукта (проверка upstream + применение) ----------
REPO_DIR = Path(__file__).resolve().parents[1]     # /opt/friend-claude
_upd_cache = {"t": 0, "data": None}


async def _git(*args, timeout=25):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"               # никогда не спрашивать пароль (не виснем)
    p = await asyncio.create_subprocess_exec(
        "git", "-C", str(REPO_DIR), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except Exception:
            pass
        return 1, "", "timeout"
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _update_status(force=False):
    now = time.time()
    if not force and _upd_cache["data"] is not None and now - _upd_cache["t"] < 300:
        return _upd_cache["data"]
    data = {"available": False, "current": None, "latest": None, "behind": 0, "changelog": "", "checked": int(now)}
    try:
        vf = REPO_DIR / "VERSION"
        data["current"] = vf.read_text().strip() if vf.exists() else None
        _, remotes, _ = await _git("remote")
        up = "upstream" if "upstream" in remotes.split() else "origin"
        await _git("fetch", "-q", "--tags", up, timeout=25)
        _, tags, _ = await _git("tag", "-l", "v*", "--sort=-v:refname")
        latest_tag = tags.splitlines()[0].strip() if tags.strip() else ""
        target = latest_tag or f"{up}/main"
        data["latest"] = latest_tag.lstrip("v") if latest_tag else f"{up}/main"
        _, behind, _ = await _git("rev-list", "--count", f"HEAD..{target}")
        data["behind"] = int((behind.strip() or "0"))
        data["available"] = data["behind"] > 0
        if data["available"]:
            _, cl, _ = await _git("show", f"{target}:CHANGELOG.md")
            excerpt, started = [], False
            for line in cl.splitlines():
                if line.startswith("## ["):
                    if started:
                        break
                    if "Unreleased" in line:       # пропускаем пустой Unreleased
                        continue
                    started = True
                if started:
                    excerpt.append(line)
                if len(excerpt) > 50:
                    break
            data["changelog"] = "\n".join(excerpt).strip()
    except Exception as e:
        data["error"] = str(e)[:200]
    _upd_cache.update(t=now, data=data)
    return data


@app.get("/api/update/check")
async def api_update_check(force: bool = Query(False), _: bool = Depends(auth_header)):
    return await _update_status(force=force)


@app.post("/api/update/apply")
async def api_update_apply(_: bool = Depends(auth_header)):
    script = REPO_DIR / "deploy" / "update.sh"
    if not script.exists():
        return {"ok": False, "error": "deploy/update.sh не найден"}
    log = open("/tmp/fc-update.log", "wb")
    # ОТВЯЗАННО (start_new_session): переживёт рестарт friend-claude-web (KillMode=process)
    await asyncio.create_subprocess_exec(
        "bash", str(script), "--yes",
        cwd=str(REPO_DIR), env=_run_env(),
        stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True)
    _upd_cache["data"] = None
    return {"ok": True}


# ---------- git / PR ----------
@app.get("/api/diff")
async def api_diff(project: str = Query(...), _: bool = Depends(auth_header)):
    projects = load_projects()
    if project not in projects:
        raise HTTPException(404, "нет проекта")
    cwd = projects[project]
    rc, status, _ = await _sh(["git", "status", "--short"], cwd)
    if rc != 0:
        return {"git": False, "status": ""}
    _, stat, _ = await _sh(["git", "diff", "--stat"], cwd)
    return {"git": True, "status": status, "stat": stat}


@app.post("/api/pr")
async def api_pr(body: dict, _: bool = Depends(auth_header)):
    project = (body or {}).get("project")
    title = (body or {}).get("title") or f"Правки от Claude ({time.strftime('%Y-%m-%d %H:%M')})"
    projects = load_projects()
    if project not in projects:
        raise HTTPException(404, "нет проекта")
    cwd = projects[project]
    rc, _, _ = await _sh(["git", "rev-parse", "--is-inside-work-tree"], cwd)
    if rc != 0:
        return {"ok": False, "error": "не git-репозиторий"}
    rc, st, _ = await _sh(["git", "status", "--porcelain"], cwd)
    if not st.strip():
        return {"ok": False, "error": "нет изменений"}
    _, base, _ = await _sh(["git", "branch", "--show-current"], cwd)
    base = base or "main"
    branch = f"claude/patch-{time.strftime('%Y%m%d-%H%M%S')}"
    for args in (["git", "switch", "-c", branch], ["git", "add", "-A"],
                 ["git", "commit", "-m", title], ["git", "push", "-u", "origin", branch]):
        rc, out, err = await _sh(args, cwd)
        if rc != 0:
            await _sh(["git", "switch", base], cwd)
            return {"ok": False, "error": (err or out)[:800]}
    _, stat, _ = await _sh(["git", "diff", "--stat", f"{base}...{branch}"], cwd)
    rc, out, err = await _sh(["gh", "pr", "create", "--base", base, "--head", branch,
                              "--title", title, "--body", f"Авто-PR от FRIEND_CLAUDE.\n\n```\n{stat[:1500]}\n```"], cwd)
    await _sh(["git", "switch", base], cwd)
    url = next((l for l in out.splitlines() if l.startswith("http")), out)
    return {"ok": rc == 0, "url": url, "branch": branch, "error": None if rc == 0 else (err or out)[:800]}


# ---------- сессия Max: скользящее 5-часовое окно ----------
_sess_cache = {"t": 0, "data": None}
CALIB_FILE = BOT_DIR / "session_calib.json"

# ---------- реальные лимиты Claude: заголовки anthropic-ratelimit-unified-* ----------
# Те же данные, что показывает `/usage` в CLI. Достаются коротким probe-запросом
# к /v1/messages (haiku, max_tokens:1 — доли копейки), заголовки несут реальную
# загрузку 5-часового и недельного окна. Кэшируем, чтобы не частить и не тратить лишнего.
_usage_cache = {"t": 0, "data": None}
OAUTH_TOKEN = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()


async def _fetch_real_usage():
    if not OAUTH_TOKEN:
        return None
    headers = {
        "Authorization": f"Bearer {OAUTH_TOKEN}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json",
    }
    body = {"model": "claude-haiku-4-5", "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}]}
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post("https://api.anthropic.com/v1/messages",
                               headers=headers, json=body)
        h = r.headers

        def fnum(k):
            try:
                return float(h[k])
            except Exception:
                return None

        def fint(k):
            try:
                return int(float(h[k]))
            except Exception:
                return None

        five = fnum("anthropic-ratelimit-unified-5h-utilization")
        week = fnum("anthropic-ratelimit-unified-7d-utilization")
        if five is None and week is None:
            return None
        return {
            "five_hour": {"utilization": five, "reset": fint("anthropic-ratelimit-unified-5h-reset"),
                          "status": h.get("anthropic-ratelimit-unified-5h-status")},
            "seven_day": {"utilization": week, "reset": fint("anthropic-ratelimit-unified-7d-reset"),
                          "status": h.get("anthropic-ratelimit-unified-7d-status")},
            "representative": h.get("anthropic-ratelimit-unified-representative-claim"),
            "overall_status": h.get("anthropic-ratelimit-unified-status"),
            "fetched_at": int(time.time()),
        }
    except Exception:
        return None


async def get_real_usage(ttl: int = 60):
    now = time.time()
    if _usage_cache["data"] is not None and now - _usage_cache["t"] < ttl:
        return _usage_cache["data"]
    data = await _fetch_real_usage()
    if data is not None:
        _usage_cache.update(t=now, data=data)
        return data
    return _usage_cache["data"]   # отдаём последнее известное, если свежий запрос не удался


def _session_from_real(real):
    now = time.time()
    fh = real.get("five_hour") or {}
    wk = real.get("seven_day") or {}
    out = {"source": "real", "active": True,
           "representative": real.get("representative"),
           "overall_status": real.get("overall_status"),
           "fetched_at": real.get("fetched_at")}
    fu, fr = fh.get("utilization"), fh.get("reset")
    out["five_util"] = fu
    out["five_pct"] = round(fu * 100, 1) if fu is not None else None
    out["five_status"] = fh.get("status")
    if fr:
        out["end"] = datetime.fromtimestamp(fr, timezone.utc).isoformat()
        out["remaining_sec"] = max(0, int(fr - now))
    wu, wr = wk.get("utilization"), wk.get("reset")
    out["week_util"] = wu
    out["week_pct"] = round(wu * 100, 1) if wu is not None else None
    out["week_status"] = wk.get("status")
    if wr:
        out["week_end"] = datetime.fromtimestamp(wr, timezone.utc).isoformat()
        out["week_remaining_sec"] = max(0, int(wr - now))
    return out


def _compute_session():
    base = Path.home() / ".claude" / "projects"
    now = datetime.now(timezone.utc)
    # запас в ~2 блока: чтобы отличить настоящее начало текущего блока от
    # "хвоста" уже истёкшего предыдущего блока, который иначе попадает в
    # окно выборки и принимается за начало текущей сессии
    lookback = now - timedelta(hours=11)
    events = []
    for f in base.glob("*/*.jsonl"):
        try:
            size = f.stat().st_size
            with f.open("rb") as fh:
                if size > 6_000_000:            # хвост крупных файлов (последние ~6МБ)
                    fh.seek(size - 6_000_000)
                    fh.readline()
                for raw in fh:
                    if b'"usage"' not in raw:
                        continue
                    try:
                        r = json.loads(raw.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    u = ((r.get("message") or {}).get("usage")) or {}
                    ts = r.get("timestamp")
                    if not u or not ts:
                        continue
                    try:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if t < lookback:
                        continue
                    events.append((t, u.get("output_tokens", 0),
                                   u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)))
        except Exception:
            continue
    if not events:
        return {"active": False, "source": "local"}
    events.sort(key=lambda x: x[0])
    # непересекающиеся 5-часовые блоки: новый блок стартует только когда
    # предыдущий уже истёк (событие >= начало_блока + 5ч)
    block_start = events[0][0]
    for t, *_ in events:
        if t >= block_start + timedelta(hours=5):
            block_start = t
    block_end = block_start + timedelta(hours=5)
    if now >= block_end:
        return {"active": False, "source": "local"}
    cur = [e for e in events if e[0] >= block_start]
    remaining = max(0, int((block_end - now).total_seconds()))
    return {"active": True, "start": block_start.isoformat(), "end": block_end.isoformat(),
            "remaining_sec": remaining, "out_tokens": sum(e[1] for e in cur),
            "cache_tokens": sum(e[2] for e in cur), "msgs": len(cur), "source": "local"}


@app.get("/api/session")
async def api_session(_: bool = Depends(auth_header)):
    # 1) РЕАЛЬНЫЕ данные из лимитов Claude (как /usage) — приоритет
    real = await get_real_usage()
    if real:
        return _session_from_real(real)
    # 2) запасной путь: локальная прикидка из логов + ручная калибровка
    now = time.time()
    if now - _sess_cache["t"] >= 30 or _sess_cache["data"] is None:
        data = await asyncio.get_event_loop().run_in_executor(None, _compute_session)
        _sess_cache.update(t=now, data=data)
    data = dict(_sess_cache["data"])
    calib = _load(CALIB_FILE, None)
    if calib and calib.get("real_end"):
        try:
            real_end = datetime.fromisoformat(calib["real_end"])
        except Exception:
            real_end = None
        n = datetime.now(timezone.utc)
        if real_end and n < real_end:
            # ручная калибровка по данным claude.ai перекрывает локальную
            # прикидку, пока не истечёт сама — локальные логи не видят
            # активность с других устройств/сессий на этом же аккаунте
            data.update(active=True, end=real_end.isoformat(),
                        remaining_sec=max(0, int((real_end - n).total_seconds())),
                        source="calibrated")
    return data


@app.post("/api/session/calibrate")
async def api_session_calibrate(body: dict, _: bool = Depends(auth_header)):
    remaining_sec = int(body.get("remaining_sec") or 0)
    if remaining_sec <= 0:
        _save(CALIB_FILE, {})
        return {"ok": True, "cleared": True}
    real_end = datetime.now(timezone.utc) + timedelta(seconds=remaining_sec)
    _save(CALIB_FILE, {"real_end": real_end.isoformat(),
                        "saved_at": datetime.now(timezone.utc).isoformat()})
    return {"ok": True, "real_end": real_end.isoformat()}


# ---------- аналитика: расход токенов по дням и проектам ----------
_an_cache = {"t": 0, "data": None}


def _compute_analytics():
    base = Path.home() / ".claude" / "projects"
    now = datetime.now(timezone.utc)
    projects = load_projects()
    slugmap = {re.sub(r"[^a-zA-Z0-9]", "-", p): n for n, p in projects.items()}
    day7 = now - timedelta(days=7)
    d24 = now - timedelta(hours=24)
    days, proj24 = {}, {}
    for f in base.glob("*/*.jsonl"):
        pname = slugmap.get(f.parent.name)
        try:
            for raw in f.open("rb"):
                if b'"usage"' not in raw:
                    continue
                try:
                    r = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                u = ((r.get("message") or {}).get("usage")) or {}
                ts = r.get("timestamp")
                if not u or not ts:
                    continue
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    continue
                if t < day7:
                    continue
                o = u.get("output_tokens", 0)
                i = u.get("input_tokens", 0)
                c = u.get("cache_read_input_tokens", 0)
                dd = t.strftime("%m-%d")
                a = days.setdefault(dd, [0, 0, 0])
                a[0] += o
                a[1] += c
                a[2] += 1
                if t >= d24 and pname:
                    p = proj24.setdefault(pname, [0, 0])
                    p[0] += 1
                    p[1] += o + i
        except Exception:
            continue
    days_list = [{"date": k, "out": v[0], "cache": v[1], "msgs": v[2]} for k, v in sorted(days.items())][-7:]
    proj_list = sorted([{"project": k, "msgs": v[0], "tokens": v[1]} for k, v in proj24.items()],
                       key=lambda x: -x["tokens"])
    return {"days": days_list, "projects": proj_list, "window": _compute_session()}


@app.get("/api/analytics")
async def api_analytics(_: bool = Depends(auth_header)):
    now = time.time()
    if now - _an_cache["t"] < 60 and _an_cache["data"] is not None:
        data = _an_cache["data"]
    else:
        data = await asyncio.get_event_loop().run_in_executor(None, _compute_analytics)
        _an_cache.update(t=now, data=data)
    # поверх локальной прикидки окна кладём РЕАЛЬНЫЕ лимиты (5ч + неделя), если доступны
    real = await get_real_usage()
    if real:
        data = dict(data)
        data["window"] = _session_from_real(real)
    return data


# ---------- поиск по всему (проекты + чаты + содержимое) ----------
def _search_all(q: str):
    q = q.lower()
    projects = load_projects()
    out = []
    for name, path in projects.items():
        d = sessions_dir(path)
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            title = None
            hit = None
            try:
                for line in f.open(errors="ignore"):
                    low = line.lower()
                    if '"custom-title"' in line:
                        try:
                            r = json.loads(line)
                            if r.get("customTitle"):
                                title = r["customTitle"]
                        except Exception:
                            pass
                    if hit is None and q in low:
                        try:
                            r = json.loads(line)
                            txt = " ".join(_extract_text((r.get("message") or r).get("content")).split())
                        except Exception:
                            txt = ""
                        if txt and q in txt.lower():
                            i = txt.lower().find(q)
                            hit = txt[max(0, i - 40):i + 90]
            except Exception:
                continue
            if hit is not None:
                if not title:
                    title = session_meta(f)["title"]
                out.append({"project": name, "chat": f.stem, "title": title, "snippet": hit.strip()})
            if len(out) >= 40:
                return out
    return out


@app.get("/api/search")
async def api_search(q: str = Query(...), _: bool = Depends(auth_header)):
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": []}
    res = await asyncio.get_event_loop().run_in_executor(None, _search_all, q)
    return {"results": res}


# ---------- активность: последние чаты по всем проектам ----------
@app.get("/api/activity")
async def api_activity(_: bool = Depends(auth_header)):
    projects = load_projects()
    items = []
    for name, path in projects.items():
        for s in list_project_sessions(path, limit=8):
            items.append({"project": name, **s})
    items.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return {"items": items[:30]}


# ---------- расписания (фоновый запуск агентов) ----------
SCHED_FILE = BOT_DIR / "schedules.json"


def load_sched():
    return _load(SCHED_FILE, [])


def save_sched(s):
    _save(SCHED_FILE, s)


@app.get("/api/schedules")
async def api_sched_list(_: bool = Depends(auth_header)):
    return {"schedules": load_sched()}


@app.post("/api/schedules")
async def api_sched_add(body: dict, _: bool = Depends(auth_header)):
    body = body or {}
    project = body.get("project")
    prompt = (body.get("prompt") or "").strip()
    kind = body.get("kind", "daily")
    if project not in load_projects():
        raise HTTPException(404, "нет проекта")
    if not prompt:
        raise HTTPException(400, "пустая задача")
    it = {"id": "s" + str(int(time.time() * 1000)), "project": project, "prompt": prompt,
          "kind": kind, "time": body.get("time"), "minutes": body.get("minutes"),
          "chat": body.get("chat"), "enabled": True,
          "last_run": None, "last_ok": None, "last_summary": None}
    s = load_sched()
    s.append(it)
    save_sched(s)
    return {"ok": True, "schedule": it}


@app.post("/api/schedules/update")
async def api_sched_update(body: dict, _: bool = Depends(auth_header)):
    sid = (body or {}).get("id")
    s = load_sched()
    for it in s:
        if it["id"] == sid:
            if "enabled" in body:
                it["enabled"] = bool(body["enabled"])
            if body.get("delete"):
                s = [x for x in s if x["id"] != sid]
            break
    save_sched(s)
    return {"ok": True, "schedules": s}


def _sched_due(it, now):
    lr = it.get("last_run")
    last = None
    if lr:
        try:
            last = datetime.fromisoformat(lr)
        except Exception:
            last = None
    if it["kind"] == "interval":
        m = int(it.get("minutes") or 60)
        return last is None or (now - last).total_seconds() >= m * 60
    if it["kind"] == "daily":
        hhmm = it.get("time") or "09:00"
        try:
            hh, mm = [int(x) for x in hhmm.split(":")]
        except Exception:
            return False
        if now.hour == hh and now.minute == mm:
            return last is None or last.strftime("%Y-%m-%d") != now.strftime("%Y-%m-%d")
    return False


async def _run_scheduled(it):
    projects = load_projects()
    cwd = projects.get(it["project"])
    if not cwd:
        return
    mode = proj_meta(it["project"])["mode"]
    tools = READONLY_TOOLS if mode == "readonly" else ALLOWED_TOOLS
    mcp_extra, tools = _mcp_flags(cwd, tools)
    cmd = [CLAUDE_BIN, "-p", it["prompt"], "--output-format", "json",
           "--allowedTools", tools, "--max-turns", str(MAX_TURNS)] + mcp_extra
    if it.get("chat"):
        cmd += ["--resume", it["chat"]]
    ok = False
    summary = ""
    sess = it.get("chat")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=_run_env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        ok = proc.returncode == 0
        try:
            j = json.loads(out.decode("utf-8", "replace"))
            summary = (j.get("result") or "")[:280]
            sess = j.get("session_id") or sess
        except Exception:
            summary = (err.decode("utf-8", "replace") or "нет вывода")[:200]
    except Exception as e:
        summary = str(e)[:200]
    s = load_sched()
    for x in s:
        if x["id"] == it["id"]:
            x["last_run"] = datetime.now().isoformat(timespec="seconds")
            x["last_ok"] = ok
            x["last_summary"] = summary
            if sess:
                x["chat"] = sess
    save_sched(s)


async def _scheduler_loop():
    while True:
        try:
            await asyncio.sleep(30)
            now = datetime.now()
            s = load_sched()
            changed = False
            for it in s:
                if it.get("enabled") and _sched_due(it, now):
                    it["last_run"] = now.isoformat(timespec="seconds")  # застолбить, чтобы не задвоить
                    changed = True
                    asyncio.create_task(_run_scheduled(dict(it)))
            if changed:
                save_sched(s)
        except asyncio.CancelledError:
            break
        except Exception:
            pass


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_scheduler_loop())


# ---------- память проекта (CLAUDE.md — агент её читает автоматически) ----------
@app.get("/api/memory")
async def api_memory_get(project: str = Query(...), _: bool = Depends(auth_header)):
    projects = load_projects()
    if project not in projects:
        raise HTTPException(404, "нет проекта")
    f = Path(projects[project]) / "CLAUDE.md"
    return {"exists": f.is_file(), "content": f.read_text() if f.is_file() else ""}


@app.post("/api/memory")
async def api_memory_set(body: dict, _: bool = Depends(auth_header)):
    project = (body or {}).get("project")
    content = (body or {}).get("content", "")
    projects = load_projects()
    if project not in projects:
        raise HTTPException(404, "нет проекта")
    f = Path(projects[project]) / "CLAUDE.md"
    f.write_text(content)
    return {"ok": True, "bytes": len(content)}


# ---------- авто-верификация: прогнать команду проверки (тесты/линт) ----------
@app.post("/api/verify")
async def api_verify(body: dict, _: bool = Depends(auth_header)):
    project = (body or {}).get("project")
    cmd = (str((body or {}).get("cmd") or "")).strip()
    projects = load_projects()
    if project not in projects:
        raise HTTPException(404, "нет проекта")
    if not cmd:
        raise HTTPException(400, "пустая команда")
    cwd = projects[project]
    try:
        p = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, env=_run_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=180)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "output": out.decode("utf-8", "replace")[-4000:]}
    except asyncio.TimeoutError:
        try:
            p.kill()
        except Exception:
            pass
        return {"ok": False, "code": -1, "output": "⏱ таймаут 180с"}
    except Exception as e:
        return {"ok": False, "code": -1, "output": str(e)[:400]}


# ---------- смена пароля консоли ----------
def _env_set(key, val):
    path = BOT_DIR / ".env"
    lines, seen = [], False
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            if ln.startswith(key + "="):
                lines.append(f"{key}={val}"); seen = True
            else:
                lines.append(ln)
    if not seen:
        lines.append(f"{key}={val}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass


@app.post("/api/password")
async def api_password(body: dict, _: bool = Depends(auth_header)):
    global WEB_PASSWORD
    cur = (body or {}).get("current") or ""
    new = (body or {}).get("new") or ""
    if not WEB_PASSWORD or not hmac.compare_digest(cur, WEB_PASSWORD):
        return {"ok": False, "error": "Текущий пароль неверный"}
    if len(new) < 6:
        return {"ok": False, "error": "Новый пароль коротковат (мин. 6 символов)"}
    _env_set("WEB_PASSWORD", new)
    WEB_PASSWORD = new                       # применяем вживую, без перезапуска
    return {"ok": True, "token": new}


# ---------- веб-страницы на домене превью (список / удаление / доступ) ----------
PREVIEWS_DIR = Path("/srv/previews")
PREVIEW_AUTH = KEYS_FILE.parent / "preview_auth.json"     # {"user","password"} — для показа в UI


def _projects_domain():
    dom = None
    try:
        for ln in Path("/etc/caddy/Caddyfile").read_text(encoding="utf-8").splitlines():
            st = ln.strip()
            if st.endswith("{") and not st.startswith(("@", "handle", "file_server", "basic_auth", "root", "encode")):
                dom = st[:-1].strip()
            if "/srv/previews" in st and dom:
                return dom
    except Exception:
        pass
    return dom


def _html_meta(f):
    try:
        head = f.read_text("utf-8", "replace")[:6000]
    except Exception:
        return None, ""
    t = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    d = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', head, re.I | re.S)
    title = re.sub(r"\s+", " ", t.group(1)).strip() if t else None
    desc = re.sub(r"\s+", " ", d.group(1)).strip() if d else ""
    return title, desc


@app.get("/api/pages")
async def api_pages(_: bool = Depends(auth_header)):
    # Показываем осмысленные точки входа, а не каждый файл: корневая, верхнеуровневые
    # страницы проекта и вложенные сайты (каталог с index.html) — одним пунктом.
    # Так зеркала/архивы (сотни страниц) не засоряют список.
    dom = _projects_domain()
    base = f"https://{dom}" if dom else ""
    root = PREVIEWS_DIR
    out = []

    def entry(html_file, urlpath, rel_del, is_dir, title=None):
        t, desc = _html_meta(html_file)
        out.append({
            "rel": rel_del, "is_dir": is_dir, "url": base + urlpath, "path": urlpath,
            "title": title or t or (rel_del or "страница"), "desc": desc,
            "private": rel_del.startswith("_private"),
            "mtime": int(html_file.stat().st_mtime),
        })

    if root.is_dir():
        if (root / "index.html").exists():
            entry(root / "index.html", "/", "index.html", False, title="Главная домена")
        for proj in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda x: x.name):
            pr = proj.name
            for f in sorted(proj.glob("*.html"), key=lambda x: x.name):
                if f.name == "404.html":
                    continue
                if f.name == "index.html":
                    entry(f, f"/{pr}/", pr, True)                 # точка входа проекта
                else:
                    rel = f"{pr}/{f.name}"
                    entry(f, "/" + rel, rel, False)               # отдельная страница
            for sub in sorted((d for d in proj.iterdir() if d.is_dir()), key=lambda x: x.name):
                idx = sub / "index.html"
                if idx.exists():
                    rel = f"{pr}/{sub.name}"
                    entry(idx, "/" + rel + "/", rel, True)        # вложенный сайт — одним пунктом
    out.sort(key=lambda x: -x["mtime"])
    return {"pages": out, "domain": dom, "has_auth": PREVIEW_AUTH.exists()}


@app.post("/api/pages/delete")
async def api_pages_delete(body: dict, _: bool = Depends(auth_header)):
    rel = (body or {}).get("rel") or ""
    root = PREVIEWS_DIR.resolve()
    target = (PREVIEWS_DIR / rel).resolve()
    if not str(target).startswith(str(root) + "/"):
        return {"ok": False, "error": "недопустимый путь"}
    if target.name in ("index.html", "404.html") and target.parent == root:
        return {"ok": False, "error": "это корневая страница домена — не удаляю"}
    try:
        if target.is_dir():
            import shutil
            shutil.rmtree(target)             # папка-сайт (напр. вложенное зеркало) целиком
        elif target.is_file():
            target.unlink()
            p = target.parent
            if p != root and p.is_dir() and not any(p.iterdir()):
                p.rmdir()                     # чистим опустевший каталог
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/defaults")
async def api_defaults(_: bool = Depends(auth_header)):
    """Реальные дефолты Claude Code: модель подписки + effort (из ~/.claude/settings.json)."""
    m, eff = "opus", None
    try:
        d = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
        m = d.get("model") or m
        eff = d.get("effortLevel")
    except Exception:
        pass
    labels = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "fable": "Fable 5",
              "claude-opus-5": "Opus 5", "claude-opus-4-8": "Opus 4.8", "claude-opus-4-7": "Opus 4.7",
              "claude-sonnet-5": "Sonnet 5", "claude-fable-5": "Fable 5", "claude-haiku-4-5": "Haiku 4.5"}
    return {"model": m, "model_label": labels.get(m, m), "effort": eff}


@app.get("/api/pages/auth")
async def api_pages_auth(_: bool = Depends(auth_header)):
    if PREVIEW_AUTH.exists():
        try:
            d = json.loads(PREVIEW_AUTH.read_text(encoding="utf-8"))
            return {"ok": True, "user": d.get("user"), "password": d.get("password")}
        except Exception:
            pass
    return {"ok": False}


# ---------- PWA / статика ----------
@app.get("/")
async def index():
    # no-cache: WebKit/браузер всегда перепроверяет HTML → PWA на айфоне видит свежий UI
    return FileResponse(str(STATIC / "index.html"),
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/sw.js")
async def sw():
    return FileResponse(str(STATIC / "sw.js"), media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(str(STATIC / "manifest.webmanifest"), media_type="application/manifest+json")


from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
