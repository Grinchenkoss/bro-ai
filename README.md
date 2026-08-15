# BRO AI

**Персональная AI-консоль для управления разработкой множества проектов из одного окна.**

BRO AI — это связка из браузерной веб-консоли и Telegram-бота-пульта, которые оркеструют агентов [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Разворачиваешь своего помощника на своём сервере — и ведёшь десятки проектов, агенты работают параллельно, а ты управляешь ими хоть с телефона.

> Проект развивался как личный инструмент (внутреннее имя — FRIEND_CLAUDE). Эта публичная версия очищена от приватной инфраструктуры и готова к самостоятельной установке.

---

## Что умеет

**Веб-консоль** (`web/`)
- Блоки проектов и окна агентов, живой стрим выполнения (как в терминале)
- Управление чатами: rename / delete / resume, PR и diff прямо из UI
- Режимы выполнения (авто / только-чтение), стоп, таймер, thinking, промпты, память
- Мультимодель: Claude по подписке; Gemini / GPT — по своим API-ключам
- Скользящее окно учёта расхода (сессия Max), поиск по всему, расписания, уведомления
- PWA: ставится на телефон, мобильная вёрстка

**Telegram-бот** (`bot/`)
- Пульт к Claude Code с телефона: «проект X, поправь Y» — и агент работает
- Мультипроект, лок на владельца, режимы инструментов, окно расхода токенов

## Стек
- **Backend:** Python 3.12 — FastAPI (веб-консоль), python-telegram-bot (бот)
- **Frontend:** ванильный HTML/CSS/JS (без сборки), PWA
- **Движок:** Claude Code CLI (подписка Claude или API-ключ Anthropic)

---

## Быстрый старт

Нужен Linux-сервер (проверено на Ubuntu 24.04), Python 3.12, установленный [Claude Code](https://docs.anthropic.com/en/docs/claude-code) и подписка Claude (или `ANTHROPIC_API_KEY`).

```bash
git clone https://github.com/Grinchenkoss/bro-ai.git
cd bro-ai

# 1) конфигурация бота
cp bot/.env.example bot/.env
# отредактируй bot/.env: BOT_TOKEN (от @BotFather), OWNER_ID (свой Telegram ID),
# CLAUDE_CODE_OAUTH_TOKEN (из `claude setup-token`)

# 2) мастер установки (создаёт venv, ставит зависимости, настраивает сервисы)
python3 deploy/wizard.py
```

Подробнее об установке и деплое — в [`deploy/README.md`](deploy/README.md).

### Запуск вручную (без мастера)

```bash
# веб-консоль
cd web && uvicorn server:app --host 127.0.0.1 --port 8787
# бот
cd bot && python bot.py
```

Systemd-юниты для прода — `bot/friend-claude-bot.service` и `web/friend-claude-web.service` (отредактируй пути под себя).

---

## Структура

```
bot/     — Telegram-бот (пульт к Claude Code)
web/     — веб-консоль (FastAPI backend + статика)
deploy/  — мастер установки, скрипты деплоя, шаблоны
docs/    — руководство и заметки по коллаборации
shared/  — общие ресурсы
```

## Безопасность
- Секреты только в `.env` (в git не попадает — см. `.gitignore`). В репозитории — лишь `.env.example` с плейсхолдерами.
- Бот залочен на `OWNER_ID` — отвечает только владельцу.
- Веб-консоль защищена паролем (`WEB_PASSWORD`); поднимай её за reverse-proxy с TLS.

## Лицензия
MIT — см. [LICENSE](LICENSE).

---

*Собрано с помощью Claude Code.*
