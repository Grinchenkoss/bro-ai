# Устойчивость DNS (защита от блипов резолвера)

Причина инцидента 2026-07-23 21:17: кратковременный сбой исходящей сети/DNS
(«Temporary failure in name resolution») — отвалились вызовы к Anthropic API и Telegram.

## Решение — локальный кэширующий резолвер unbound с serve-expired
- Пакет `unbound`, слушает 127.0.0.1:53, автозапуск (systemctl enable unbound).
- Конфиг: `/etc/unbound/unbound.conf.d/friend-claude.conf`
  - апстримы: 1.1.1.1, 1.0.0.1, 8.8.8.8, 8.8.4.4
  - `prefetch: yes`, `serve-expired: yes` (ttl 86400) — отдаёт УСТАРЕВШИЙ кэш, если апстрим недоступен.
- `/etc/resolv.conf`: `nameserver 127.0.0.1` + фолбэк 1.1.1.1/8.8.8.8; **immutable (chattr +i)** — переживает reboot/cloud-init.
- netplan `99-virt-eth0.yaml` nameservers: `[127.0.0.1, 1.1.1.1, 8.8.8.8]` (консистентность).
- Бэкап прежнего resolv.conf: `/etc/resolv.conf.bak-YYYYMMDD`.

## Проверено
Блокировка апстрим-DNS (iptables) → закэшированные имена (api.anthropic.com, api.telegram.org)
продолжают резолвиться из кэша; новые — нет. Значит DNS-блипы больше не рвут связь.

## Ограничение
Спасает от DNS-блипов. От ПОЛНОГО обрыва аплинка (нет TCP наружу вообще) локально не спастись —
это к дата-центру. Если такие обрывы участятся — писать в их поддержку.

## Если надо поменять DNS
`chattr -i /etc/resolv.conf` → править → (по желанию `chattr +i` обратно).
