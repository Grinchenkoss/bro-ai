#!/usr/bin/env bash
# Обновление продукта из upstream: показать «что нового» → применить → перезапустить.
# Для того, кто ДЕРЖИТ экземпляр (партнёр). Безопасно: локальные правки уходят в git stash.
set -euo pipefail
ROOT=/opt/friend-claude
cd "$ROOT"

YES=0; [[ "${1:-}" == "-y" || "${1:-}" == "--yes" ]] && YES=1   # --yes: без вопроса (запуск из консоли)

# upstream = канонический репозиторий продукта (если есть remote 'upstream', иначе 'origin')
UP=$(git remote | grep -qx upstream && echo upstream || echo origin)
INSTALLED=$(cat VERSION 2>/dev/null || echo "0.0.0")

echo "══ Обновление продукта ══"
echo "  установлено: v$INSTALLED   ·   источник: $UP"
git fetch -q --tags "$UP"

TARGET=$(git tag -l 'v*' --sort=-v:refname | head -1)
[ -z "$TARGET" ] && TARGET="$UP/main"
echo "  доступно:    $TARGET"

echo
echo "──────── Что нового (CHANGELOG) ────────"
git show "$TARGET:CHANGELOG.md" 2>/dev/null | sed -n '/^## /{:a;p;n;/^## \[/q;ba}' | sed -n '1,60p' \
  || echo "(в целевой версии нет CHANGELOG)"

echo
echo "──────── Коммиты с v$INSTALLED ────────"
git --no-pager log --oneline "v$INSTALLED..$TARGET" 2>/dev/null | head -40 \
  || git --no-pager log --oneline -20 "$TARGET"

echo
if [ "$YES" != 1 ]; then
  read -rp "Применить обновление? (локальные правки → git stash) [y/N]: " a
  [[ "$a" =~ ^[yYдД] ]] || { echo "отменено"; exit 0; }
fi

ts=$(date +%Y%m%d-%H%M%S)
if ! git diff --quiet || ! git diff --cached --quiet; then
  git stash push -u -m "pre-update-$ts" >/dev/null 2>&1 && echo "  локальные правки → stash: pre-update-$ts"
fi

REQ_BEFORE=$(git hash-object web/requirements.txt 2>/dev/null || true)
if ! git merge --ff-only "$TARGET" 2>/dev/null; then
  echo "!! Быстрое обновление не прошло — у тебя есть свои коммиты, разошедшиеся с upstream."
  echo "   Влей вручную:  git rebase $TARGET   (или создай PR со своими правками — см. docs/COLLABORATION.md)"
  exit 1
fi
REQ_AFTER=$(git hash-object web/requirements.txt 2>/dev/null || true)
if [ "$REQ_BEFORE" != "$REQ_AFTER" ]; then
  echo "  requirements изменились — доставляю зависимости…"
  bot/venv/bin/pip install -q -r web/requirements.txt || true
fi

# зафиксировать установленную версию
if git describe --tags --exact-match 2>/dev/null | sed 's/^v//' > VERSION.tmp && [ -s VERSION.tmp ]; then
  mv VERSION.tmp VERSION
else
  rm -f VERSION.tmp; echo "$TARGET" > VERSION
fi

for s in friend-claude-web friend-claude-bot anzhela-listener; do
  systemctl restart "$s" 2>/dev/null || true
done
echo "✓ Обновлено до v$(cat VERSION). Сервисы перезапущены."
echo "  Локальные правки (если были): git stash list · git stash pop"
