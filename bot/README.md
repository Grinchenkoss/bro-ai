# bot — Telegram-пульт к Claude Code

Один файл `bot.py` (aiogram 3). Запускается на сервере как systemd-сервис, лочится на владельца
по `OWNER_ID`, гоняет `claude -p` в папке активного проекта и присылает ответ + расход токенов.

## Установка на сервере (Ubuntu 24.04)

```bash
# окружение
sudo apt install -y python3.12-venv git gh
curl -fsSL https://claude.ai/install.sh | bash     # Claude Code CLI
claude setup-token                                  # OAuth-токен (подписка)
gh auth login                                       # GitHub (для /pr)

# бот
sudo mkdir -p /opt/friend-claude-bot && cd /opt/friend-claude-bot
python3 -m venv venv && venv/bin/pip install aiogram
cp /путь/bot.py .            # этот файл
cp .env.example .env         # заполнить: BOT_TOKEN, OWNER_ID, CLAUDE_CODE_OAUTH_TOKEN
chmod 600 .env

# сервис
sudo cp friend-claude-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now friend-claude-bot
journalctl -u friend-claude-bot -f
```

## Команды бота

| Команда | Что делает |
|---|---|
| текст | задача агенту в активном проекте |
| `/projects` `/project <name>` `/addproject <name> [path]` `/ls` | проекты |
| `/new` `/status` `/usage` | сессия, статус, расход токенов |
| `/diff` `/pr [заголовок]` `/discard` | ревью через PR |
| `/protect` `/mode safe\|readonly` | guardrails: подтверждение задач, режим доступа |

## Файлы состояния (в git не коммитятся)

- `.env` — секреты.
- `projects.json` — реестр проектов (имя → путь).
- `state.json` — активный проект, `session_id` на проект, окно учёта расхода.
- `meta.json` — per-project: `protected`, `mode`.
