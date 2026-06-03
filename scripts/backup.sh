#!/usr/bin/env bash
# גיבוי car-agent ל-GitHub לפי דרישה.
# שימוש:  ./scripts/backup.sh ["תיאור השינוי"]
set -euo pipefail
cd /opt/car-agent
msg="${1:-backup $(date '+%Y-%m-%d %H:%M')}"
git add -A
if git diff --cached --quiet; then
  echo "אין שינויים לגיבוי."
  exit 0
fi
git commit -m "$msg"
git push origin main
echo "✓ גובה ל-GitHub."
