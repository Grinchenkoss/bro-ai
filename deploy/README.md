# FRIEND_CLAUDE — развёртывание под свои аккаунты

Поднять свой экземпляр сервиса (веб-консоль + Telegram-бот + помощница) на своём
сервере со своими аккаунтами. Мастер спрашивает данные в **два этапа**: сначала
критичное для старта, потом — остальное.

## Что получится
- **Веб-консоль** `https://console.<твой-домен>` — окна проектов, живой запуск агента,
  фоновые прогоны (переживают сон устройства), аналитика расхода, реальные лимиты Claude.
- **Telegram-бот** — пульт к агенту с телефона (только владелец).
- **Помощница** (по желанию) — отдельный Telegram-аккаунт: принимает входящие, отвечает,
  эскалирует владельцу, заводит задачи агентам проектов. Со своей памятью/досье.
- **Генерация** (по желанию) — image/html/video через Gemini для проектов.
- **Превью-домен** (по желанию) — `https://projects.<домен>/<проект>/` для показа заказчикам.

## Требования
- Сервер Ubuntu/Debian, root.
- Свой домен: A-записи `console.<домен>` (обязательно) и `projects.<домен>` (если нужны превью)
  → на IP сервера. TLS Caddy выпустит сам.
- Свой аккаунт **Claude** (Max-подписка или API), **Telegram-бот** (@BotFather),
  для помощницы — отдельный **номер телефона** и `api_id/api_hash` с https://my.telegram.org.

## Установка
```bash
# 1) склонировать код продукта в /opt/friend-claude (свой форк/репозиторий)
sudo git clone <твой-репозиторий> /opt/friend-claude

# 2) установщик: системные пакеты, Node+Claude Code, venv, затем мастер (этап 1)
sudo bash /opt/friend-claude/deploy/install.sh
```

### Этап 1 — спросит критичное (для старта)
- домен веб-консоли;
- токен Claude (`claude setup-token` — мастер предложит запустить);
- пароль веб-консоли (можно сгенерировать);
- `BOT_TOKEN` бота + твой `OWNER_ID`;
- каталог проектов.

→ поднимет `friend-claude-web` и `friend-claude-bot`. Консоль доступна.

### Этап 2 — остальное (после старта)
```bash
sudo python3 /opt/friend-claude/deploy/wizard.py --phase2
```
- **Gemini** ключ (генерация);
- **Превью-домен** (заглушка + 404 + приватная зона под паролем);
- **Помощница**: имя, вход её Telegram-аккаунта (api_id/api_hash/телефон → код → 2FA),
  каркас памяти (`~/friend-claude-assistant`), запуск `anzhela-listener`;
- **Голос** (тембр).

## Где что лежит
| Что | Путь |
|---|---|
| Секреты/конфиг бота | `/opt/friend-claude/bot/.env` (chmod 600) |
| Ключи моделей (Gemini) | `~/.config/friend-claude/models.env` |
| Токен Claude (зеркало) | `~/.config/friend-claude/claude.env` |
| Сессия/creds помощницы | `~/.config/friend-claude/anzhela.session`, `anzhela_tg.json` |
| Память помощницы | `~/friend-claude-assistant/` (CLAUDE.md, owner.md, board.json, known.json) |
| Передаточная папка | `~/friend-claude-inbox/` |
| Публичные превью | `/srv/previews/` |
| Reverse-proxy/TLS | `/etc/caddy/Caddyfile` |

## Сервисы
```bash
systemctl status friend-claude-web friend-claude-bot anzhela-listener
journalctl -u friend-claude-web -f      # логи консоли
```

## Необязательная инфраструктура (вручную)
- **Передаточная папка через Syncthing** (файлы с Mac напрямую агенту): `web/scripts/syncthing_pair.py`.
- **Мониторинг сети** между серверами: `web/scripts/netmon.py` (+ таймер сбора).
- **graphify** (граф знаний по коду, пиново): `vendor/graphify` + `.mcp.json` в проекте
  (см. соответствующие разделы в CLAUDE.md проектов).

## Безопасность
- Все секреты — только в файлах `chmod 600`, в git не коммитятся.
- Веб-консоль под паролем; приватная зона превью — под basic-auth.
- Токен Claude/бота/номер помощницы у каждого свои — экземпляры полностью изолированы.
