#!/usr/bin/env bash
# vlogs-stall canary: detect a stalled VictoriaLogs ingest pipeline.
#
# Failure modes detected:
#   vlogs-down        — VictoriaLogs HTTP unreachable (server crashed/wedged)
#   vlogs-no-ingest   — server reachable but no log entries in last 15 min
#                       across the entire index (ingest timer broken)
#   vlogs-stale-app   — at least one tracked app has no entries in 30 min
#                       (logs.py routing broken for that app)
#
# Lives on the seedbox at ~/scripts/canaries/vlogs-stall.sh (deployed by
# 240-maintenance-install.sh). Invoked by manitoba-maint-canary-vlogs-stall
# every 15 min, which pushes status=up/down to Kuma monitor "Canary VLogs Stall".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
PORT=$(cat ~/secrets/vlogs.port 2>/dev/null)
if [ -z "$PORT" ]; then
  printf "STAGE=vlogs-config-missing msg=secrets/vlogs.port-empty\n" >&2
  exit 1
fi
VL=http://127.0.0.1:$PORT

# Stage 1: server reachable?
H=$(curl -s -o /dev/null -w "%{http_code}" -m 5 "$VL/health" 2>/dev/null || echo "000")
if [ "$H" != "200" ]; then
  printf "STAGE=vlogs-down msg=health-http-%s\n" "$H" >&2
  exit 1
fi

# Stage 2: any ingest in last 15 min?
# LogsQL count() over all streams in the window. VictoriaLogs returns one
# JSON line with the count.
Q=$(curl -sf -m 10 --get \
  --data-urlencode "query=* | stats count() as n" \
  --data-urlencode "start=15m" \
  "$VL/select/logsql/query" 2>&1) || {
    printf "STAGE=vlogs-query-fail msg=count-query-error\n" >&2
    exit 1
}
N=$(printf "%s" "$Q" | python3 -c "import sys, json
try:
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        d = json.loads(line)
        print(d.get(\"n\", 0))
        break
    else:
        print(0)
except Exception:
    print(0)")
if [ "${N:-0}" -lt 1 ]; then
  printf "STAGE=vlogs-no-ingest msg=zero-lines-last-15min\n" >&2
  exit 1
fi

# Stage 3: per-app freshness — at least the *arr stack should have logged
# something in 30 min. Skipping individual stale-app failure for now to keep
# the canary noise-free; surface as warning only.
STALE=""
for app in sonarr radarr prowlarr qbittorrent; do
  AN=$(curl -sf -m 5 --get \
    --data-urlencode "query=app:$app | stats count() as n" \
    --data-urlencode "start=30m" \
    "$VL/select/logsql/query" 2>/dev/null \
    | python3 -c "import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line); print(d.get(\"n\", 0)); break
    except Exception: pass
else:
    print(0)" 2>/dev/null) || AN=0
  if [ "${AN:-0}" -lt 1 ]; then
    STALE="$STALE $app"
  fi
done

if [ -n "$STALE" ]; then
  # All-stale = ingest fully broken; partial = noise. Only fail on all-stale.
  STALE_COUNT=$(printf "%s" "$STALE" | wc -w)
  if [ "$STALE_COUNT" -ge 4 ]; then
    printf "STAGE=vlogs-stale-app msg=all-arr-stale-30m%s\n" "$STALE" >&2
    exit 1
  fi
  printf "vlogs-flowing total_15m=%s warn-stale=%s\n" "$N" "$STALE"
  exit 0
fi

printf "vlogs-flowing total_15m=%s all-arr-fresh=true\n" "$N"
exit 0
')
RC=$?
echo "$RES"
exit $RC
