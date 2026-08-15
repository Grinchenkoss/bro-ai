#!/usr/bin/env bash
# FRIEND_CLAUDE — установка на чистый сервер (Ubuntu/Debian, под root).
# Код продукта должен уже лежать в /opt/friend-claude (склонируй свой репозиторий туда).
set -euo pipefail
ROOT=/opt/friend-claude

echo "══ Проверки ══"
[ "$(id -u)" = 0 ] || { echo "Запусти под root (sudo bash deploy/install.sh)"; exit 1; }
[ -f "$ROOT/web/server.py" ] || { echo "Код не найден в $ROOT — сначала склонируй репозиторий в $ROOT."; exit 1; }

echo "══ Системные пакеты ══"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl ca-certificates gnupg ffmpeg

# --- Caddy (reverse-proxy + авто-TLS) ---
if ! command -v caddy >/dev/null; then
  echo "  ставлю Caddy…"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -y && apt-get install -y caddy
fi

# --- Node + Claude Code CLI (агент) ---
if ! command -v node >/dev/null; then
  echo "  ставлю Node.js…"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs
fi
if ! command -v claude >/dev/null; then
  echo "  ставлю Claude Code CLI…"
  npm i -g @anthropic-ai/claude-code || echo "  (!) не удалось через npm — поставь Claude Code вручную"
fi

# --- GitHub CLI (для create/clone проектов) — по возможности ---
if ! command -v gh >/dev/null; then
  (curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update -y && apt-get install -y gh) || echo "  (gh не установлен — необязательно)"
fi

echo "══ Python-окружение (venv) ══"
python3 -m venv "$ROOT/bot/venv"
"$ROOT/bot/venv/bin/pip" install -q --upgrade pip
# ядро для этапа 1 (консоль + бот). Голосовые зависимости (torch и пр.) ставит мастер на этапе 2 по желанию.
"$ROOT/bot/venv/bin/pip" install -q "fastapi" "uvicorn[standard]" "python-multipart" "telethon" "httpx"

echo "══ Мастер настройки (этап 1) ══"
echo "  Claude Code: если ещё не входил в аккаунт — выполни 'claude setup-token' (мастер предложит)."
python3 "$ROOT/deploy/wizard.py"

echo
echo "Готово. Дальнейшая тонкая настройка: sudo python3 $ROOT/deploy/wizard.py --phase2"
