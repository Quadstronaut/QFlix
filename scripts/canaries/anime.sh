#!/usr/bin/env bash
# Anime canary: verify Jellyseerr → Sonarr2 routing for the anime branch.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
JS_PORT=$(cat ~/secrets/jellyseerr.port)
JS_KEY=$(cat ~/secrets/jellyseerr.key)
S2_PORT=$(cat ~/secrets/sonarr2.port)
S2_KEY=$(cat ~/secrets/sonarr2.key)
S2_BASE=$(cat ~/secrets/sonarr2.urlbase 2>/dev/null || echo sonarr2)
# Jellyseerr Sonarr server count (need >=2: regular + anime)
JS_SONARR=$(curl -sf -m 5 -H "X-Api-Key: $JS_KEY" "http://127.0.0.1:$JS_PORT/api/v1/settings/sonarr" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d))")
# Sonarr2 health
S2_HEALTH=$(curl -sf -m 5 -H "X-Api-Key: $S2_KEY" "http://127.0.0.1:$S2_PORT/$S2_BASE/api/v3/system/status" -o /dev/null -w "%{http_code}")
# Sonarr2 root folder includes /Anime
S2_ROOTS=$(curl -sf -m 5 -H "X-Api-Key: $S2_KEY" "http://127.0.0.1:$S2_PORT/$S2_BASE/api/v3/rootfolder" | python3 -c "import sys,json; d=json.load(sys.stdin); print(\"|\".join(p[\"path\"] for p in d))")
echo "JS_SONARR=$JS_SONARR"
echo "S2_HEALTH=$S2_HEALTH"
echo "S2_ROOTS=$S2_ROOTS"
')
echo "$RES"
JS_SONARR=$(echo "$RES" | grep -oE 'JS_SONARR=[0-9]+' | cut -d= -f2)
S2_HEALTH=$(echo "$RES" | grep -oE 'S2_HEALTH=[0-9]+' | cut -d= -f2)
S2_ROOTS=$(echo "$RES" | grep -E 'S2_ROOTS=' | cut -d= -f2-)

[ "${JS_SONARR:-0}" -ge 2 ] || { echo "FAIL: Jellyseerr has <2 Sonarr servers (need anime + default)" >&2; exit 1; }
[ "${S2_HEALTH:-0}" = 200 ] || { echo "FAIL: Sonarr2 unhealthy ($S2_HEALTH)" >&2; exit 1; }
echo "$S2_ROOTS" | grep -qi anime || { echo "FAIL: Sonarr2 root folder does not include 'Anime' (got: $S2_ROOTS)" >&2; exit 1; }
echo "PASS: anime canary"
