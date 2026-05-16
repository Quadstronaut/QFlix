#!/usr/bin/env bash
# Deletion canary: Maintainerr 60-day rules exist for all 4 Plex libraries
# under the canonical 'QFlix <short>-60d' name scheme and are active.
# (Plex libraries were renamed to a 'QFlix - ' prefix in 2026-05; rules
# follow the convention in scripts/configure/27b-maintainerr-rules.py
# which strips that prefix and writes the rule as `QFlix <short>-60d`.)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
HTPW=$(cat ~/secrets/htpasswd.password)
MTKEY=$(cat ~/secrets/maintainerr.key)
PUBLIC_HOST=$(cat ~/secrets/seedbox.host 2>/dev/null || echo "quadstronaut.seedbox.example.com")
USERPART=${PUBLIC_HOST%%.*}
DOMAIN=${PUBLIC_HOST#*.}
MT_HOST="maintainerr-${USERPART}.${DOMAIN}"
BASIC=$(printf "quadstronaut:%s" "$HTPW" | base64 -w0)
RULES=$(curl -sk -m 5 -H "X-Api-Key: $MTKEY" -H "Authorization: Basic $BASIC" "https://${MT_HOST}/api/rules")
echo "$RULES" | python3 -c "
import sys, json
d = json.load(sys.stdin)
active = [g for g in d if g.get(\"isActive\")]
names = [g.get(\"name\") for g in active]
print(\"COUNT=\" + str(len(active)))
print(\"NAMES=\" + \",\".join(names))
"
')
echo "$RES"
COUNT=$(echo "$RES" | grep -oE 'COUNT=[0-9]+' | cut -d= -f2)
NAMES=$(echo "$RES" | grep -E 'NAMES=' | cut -d= -f2-)

[ "${COUNT:-0}" -ge 4 ] || { echo "FAIL: <4 active Maintainerr rules (got $COUNT)" >&2; exit 1; }
for expect in "QFlix Movies-60d" "QFlix TV-60d" "QFlix Anime-60d" "QFlix Anime Movies-60d"; do
  echo "$NAMES" | grep -qF "$expect" || { echo "FAIL: missing rule '$expect'" >&2; exit 1; }
done
echo "PASS: deletion canary — 4 active 60-day rules"
