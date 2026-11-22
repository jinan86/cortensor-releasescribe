#!/usr/bin/env bash
# Build the deterministic fixture repo used by tests, the demo, and the
# end-to-end run. Safe to re-run: it wipes and recreates ONLY the repo
# subdirectory (never this script's own directory).
#
# Usage: fixtures/sample-repo/build.sh [target-dir]
# Default target: <this-dir>/repo
#
# The fixture has 6 commits. v0.1.0 tags the first commit, so the range
# v0.1.0..HEAD contains exactly 5 commits:
#   feat: add dark mode toggle
#   fix: correct total in checkout summary
#   feat: export report as CSV
#   fix: handle empty cart on checkout
#   feat!: remove legacy /v1 endpoint        (breaking)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-$SCRIPT_DIR/repo}"
rm -rf "$TARGET"
mkdir -p "$TARGET"
cd "$TARGET"

git init -q
git config user.name "Fixture Author"
git config user.email "fixture@example.invalid"
git config commit.gpgsign false

commit() {
  local msg="$1" date="$2" file="$3" content="$4"
  export GIT_AUTHOR_DATE="$date" GIT_COMMITTER_DATE="$date"
  printf '%s\n' "$content" >> "$file"
  git add "$file"
  git commit -qm "$msg"
}

commit "feat: add user login" \
  "2026-01-01T10:00:00Z" "app.py" "# sample app"
git tag v0.1.0

commit "feat: add dark mode toggle" \
  "2026-01-02T10:00:00Z" "theme.py" "DARK_MODE = True"
commit "fix: correct total in checkout summary" \
  "2026-01-03T10:00:00Z" "cart.py" "total = sum(items)"
commit "feat: export report as CSV" \
  "2026-01-04T10:00:00Z" "report.py" "def export_csv(): pass"
commit "fix: handle empty cart on checkout" \
  "2026-01-05T10:00:00Z" "cart.py" "if not items: raise EmptyCart"
commit "feat!: remove legacy /v1 endpoint" \
  "2026-01-06T10:00:00Z" "api.py" "V1_REMOVED = True"

echo "fixture repo built at $TARGET"
git log --oneline v0.1.0..HEAD
