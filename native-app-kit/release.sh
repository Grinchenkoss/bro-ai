#!/usr/bin/env bash
# Симметричный релиз НАТИВНОГО ПРИЛОЖЕНИЯ (мобильная часть продукта).
# Зеркало серверного deploy/release.sh, но для iOS:
#   • переносит [Unreleased] в CHANGELOG в новую версию,
#   • выставляет marketing version = X.Y.Z и увеличивает build number (agvtool, если есть),
#   • коммитит, ставит тег vX.Y.Z, пушит.
#
# Использование:  ./release.sh 1.2.0
# Перед релизом:  впиши изменения в раздел «## [Unreleased]» в CHANGELOG.md.
set -euo pipefail

NEW="${1:-}"
[ -z "$NEW" ] && { echo "Использование: ./release.sh X.Y.Z"; exit 1; }
echo "$NEW" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo "Версия должна быть вида X.Y.Z"; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "Запусти внутри git-репозитория приложения"; exit 1; }
git tag -l "v$NEW" | grep -q . && { echo "Тег v$NEW уже существует"; exit 1; }
[ -n "$(git status --porcelain)" ] && { echo "Есть незакоммиченные правки — закоммить/спрячь перед релизом"; exit 1; }

# 1) CHANGELOG: [Unreleased] -> [NEW] - дата
TODAY=$(date +%Y-%m-%d)
if [ -f CHANGELOG.md ]; then
  python3 - "$NEW" "$TODAY" <<'PY'
import sys
new, today = sys.argv[1], sys.argv[2]
s = open("CHANGELOG.md", encoding="utf-8").read()
if f"## [{new}]" in s:
    print("раздел уже есть — пропускаю"); raise SystemExit(0)
empty = "## [Unreleased]\n### Added\n### Changed\n### Fixed\n\n"
if "## [Unreleased]" in s:
    s = s.replace("## [Unreleased]", empty + f"## [{new}] — {today}", 1)
else:
    s = s.rstrip() + f"\n\n## [{new}] — {today}\n"
open("CHANGELOG.md", "w", encoding="utf-8").write(s)
print(f"CHANGELOG: → [{new}] — {today}")
PY
else
  echo "(!) нет CHANGELOG.md — создай из шаблона native-app-kit/CHANGELOG.md"
fi

echo "$NEW" > VERSION

# 2) версии в Xcode-проекте
if command -v agvtool >/dev/null 2>&1 && ls *.xcodeproj >/dev/null 2>&1; then
  agvtool new-marketing-version "$NEW" >/dev/null && echo "marketing version → $NEW"
  agvtool next-version -all >/dev/null && echo "build number ++ → $(agvtool what-version -terse 2>/dev/null)"
else
  echo "(!) agvtool/.xcodeproj не найдены — выстави вручную:"
  echo "    CFBundleShortVersionString = $NEW,  CFBundleVersion ++ (Xcode → Target → General)."
fi

# 3) коммит + тег + пуш
git add -A
git commit -q -m "release: v$NEW"
git tag -a "v$NEW" -m "v$NEW"
git push origin HEAD --follow-tags
echo "✓ Релиз приложения v$NEW выпущен и запушен."
echo "  Дальше — сборка и загрузка в TestFlight (Xcode Organizer или fastlane)."
