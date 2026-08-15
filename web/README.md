# web — браузерная консоль FRIEND_CLAUDE (Ф6)

Тот же «мозг на сервере», но с UI: блоки проектов, несколько окон агентов, живой
стрим ответа. Переиспользует хранилище бота (`projects.json`, `state.json`,
`meta.json`, сессии Claude в `~/.claude/projects`). Лок на владельца по паролю.

## Из чего состоит
- `server.py` — FastAPI-бэкенд: `/api/projects`, `/api/chats`, WebSocket `/ws`
  (стрим `claude --output-format stream-json`), `/api/diff`, `/api/pr`.
- `static/index.html` — фронтенд (vanilla JS, без сборки, без CDN). Демо-режим:
  на экране входа ссылка «посмотреть демо» — UI работает без бэкенда.

## Установка на сервере
```bash
cd /opt/friend-claude-bot
venv/bin/pip install -r web/requirements.txt
# в .env добавить строку:  WEB_PASSWORD=<длинный-пароль>
sudo cp web/friend-claude-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now friend-claude-web
```
Сервис слушает **только 127.0.0.1:8787** — наружу отдаём через HTTPS-прокси.

## Наружу (выбрать одно) — обязательно HTTPS
**Cloudflare Tunnel (рекомендуется — без открытых портов):**
```bash
cloudflared tunnel --url http://127.0.0.1:8787   # выдаст https-адрес
# или именованный туннель на свой домен
```
**Caddy (свой домен, авто-TLS):**
```
console.example.com {
    reverse_proxy 127.0.0.1:8787
}
```

## Безопасность
- `WEB_PASSWORD` обязателен, сравнение constant-time. Токен = пароль, шлётся в
  `Authorization: Bearer` (по HTTPS безопасно для одного пользователя).
- Наружу только по HTTPS. Порт 8787 не открывать в firewall.
- Дальше можно усилить: rate-limit на /api/login, cookie-сессии, 2FA.

## Что дальше (бэклог Ф6)
- Переименование/удаление чатов из UI (как в боте).
- Кнопки PR/diff в шапке окна агента (эндпоинты уже есть).
- Подтверждение задачи для protected-проектов (инлайн, как в боте).
- Тёмная/светлая — есть (кнопка «Тема»); адаптив под телефон — базовый.
