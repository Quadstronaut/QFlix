#!/usr/bin/env bash
# Movie canary: verify Seerr → Radarr → import propagation by
# inspecting current request/movie state. Read-only — does NOT request
# new content. The smoke gate's job is to confirm the wiring is alive,
# not to fill the disk.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

# Seerr → Radarr connection live + at least one movie in Radarr
RES=$(sshm '
JS_PORT=$(cat ~/secrets/seerr.port)
JS_KEY=$(cat ~/secrets/seerr.key)
RADARR_PORT=$(cat ~/secrets/radarr.port)
RADARR_KEY=$(cat ~/secrets/radarr.key)
RADARR_BASE=$(cat ~/secrets/radarr.urlbase 2>/dev/null || echo radarr)
# 1. Seerr → Radarr server registered + reachable
JS_RADARR=$(curl -sf -m 5 -H "X-Api-Key: $JS_KEY" "http://127.0.0.1:$JS_PORT/api/v1/settings/radarr" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d))")
# 2. Radarr health check
RADARR_HEALTH=$(curl -sf -m 5 -H "X-Api-Key: $RADARR_KEY" "http://127.0.0.1:$RADARR_PORT/$RADARR_BASE/api/v3/system/status" -o /dev/null -w "%{http_code}")
# 3. Radarr has at least one movie tracked
RADARR_MOVIES=$(curl -sf -m 5 -H "X-Api-Key: $RADARR_KEY" "http://127.0.0.1:$RADARR_PORT/$RADARR_BASE/api/v3/movie" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
echo "JS_RADARR_SERVERS=$JS_RADARR"
echo "RADARR_HEALTH=$RADARR_HEALTH"
echo "RADARR_MOVIES=$RADARR_MOVIES"
')
echo "$RES"
JS_RADARR=$(echo "$RES" | grep -oE 'JS_RADARR_SERVERS=[0-9]+' | cut -d= -f2)
RADARR_HEALTH=$(echo "$RES" | grep -oE 'RADARR_HEALTH=[0-9]+' | cut -d= -f2)
RADARR_MOVIES=$(echo "$RES" | grep -oE 'RADARR_MOVIES=[0-9]+' | cut -d= -f2)

[ "${JS_RADARR:-0}" -ge 1 ] || { echo "FAIL: Seerr has 0 Radarr servers" >&2; exit 1; }
[ "${RADARR_HEALTH:-0}" = 200 ] || { echo "FAIL: Radarr unhealthy ($RADARR_HEALTH)" >&2; exit 1; }
[ "${RADARR_MOVIES:-0}" -ge 1 ] || { echo "FAIL: Radarr has 0 movies" >&2; exit 1; }
echo "PASS: movie canary"
