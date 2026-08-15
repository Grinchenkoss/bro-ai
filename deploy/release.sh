#!/usr/bin/env bash
# Выпуск релиза (для МЕЙНТЕЙНЕРА продукта). Переносит [Unreleased] в новую версию
# в CHANGELOG, ставит тег vX.Y.Z и пушит. Партнёр увидит релиз через deploy/update.sh.
#
# Использование:  deploy/release.sh 0.2.0
# Перед выпуском: впиши изменения в раздел «## [Unreleased]» в CHANGELOG.md.
set -euo pipefail
ROOT=/opt/friend-claude
cd "$ROOT"

NEW="${1:-}"
[ -z "$NEW" ] && { echo "Использование: deploy/release.sh X.Y.Z"; exit 1; }
echo "$NEW" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo "Версия должна быть вида X.Y.Z"; exit 1; }
git tag -l "v$NEW" | grep -q . && { echo "Тег v$NEW уже существует"; exit 1; }

# перенести [Unreleased] -> [NEW] - дата, добавить свежий пустой [Unreleased]
TODAY=$(date +%Y-%m-%d)
python3 - "$NEW" "$TODAY" <<'PY'
import sys
new, today = sys.argv[1], sys.argv[2]
p = "CHANGELOG.md"
s = open(p, encoding="utf-8").read()
if f"## [{new}]" in s:
    print("раздел уже есть — пропускаю правку CHANGELOG"); raise SystemExit(0)
empty = "## [Unreleased]\n### Added\n### Changed\n### Fixed\n\n"
if "## [Unreleased]" not in s:
    print("нет раздела [Unreleased] в CHANGELOG"); raise SystemExit(1)
s = s.replace("## [Unreleased]", empty + f"## [{new}] — {today}", 1)
open(p, "w", encoding="utf-8").write(s)
print(f"CHANGELOG: [Unreleased] → [{new}] — {today}")
PY

echo "$NEW" > VERSION
git add CHANGELOG.md VERSION
git commit -q -m "release: v$NEW"
git tag -a "v$NEW" -m "v$NEW"
git push origin HEAD --follow-tags
echo "✓ Релиз v$NEW выпущен и запушен. Партнёр получит его командой: deploy/update.sh"
