#!/usr/bin/env bash
# vlogs-stall canary: detect a stalled VictoriaLogs ingest pipeline.
#
# Failure modes detected:
#   vlogs-down         — VictoriaLogs HTTP unreachable (server crashed/wedged)
#   vlogs-query-fail   — count query never returned 200 across 3 tries
#                        (query engine wedged, not a transient blip)
#   vlogs-no-ingest    — server reachable but no log entries in last 15 min
#                        across the entire index (ingest timer broken)
#   vlogs-stale-app    — at least one tracked app returned a DEFINITIVE
#                        zero-count in 30 min (logs.py routing broken)
#
# Transient-fault handling (added 2026-05-25):
#   This box is an Ultra.cc *shared* seedbox. Neighbour disk-I/O storms
#   periodically block a normally <2ms LogsQL query for multiple seconds
#   (the same contention that stalls the Kuma pusher — see PR #55 and the
#   Kuma-retention task). The previous version used tight curl timeouts
#   (5s/10s) with no retry, and stage 3 collapsed a curl failure into
#   "app logged zero" (AN=0) — so a transient query timeout masqueraded
#   as an ingest stall and fired a FALSE canary failure during quiet
#   early-morning hours. Now every query retries up to 3× with backoff,
#   and a query that never returns 200 is treated as INCONCLUSIVE for the
#   per-app freshness check (skipped, not counted stale). Only a real
#   200-response showing zero counts as a stall.
#
# Every non-pass also appends its reason to
#   ~/.opt/maint/canary-vlogs-stall.log
# because the failure msg otherwise only lands in Kuma's heartbeat table,
# which is owned by the Kuma Docker container and unreadable by the SSH
# user — that gap made the 2026-05-25 false-positive hard to diagnose.
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

LOG="$HOME/.opt/maint/canary-vlogs-stall.log"
logfail() {
  # Append timestamped reason locally; keep the file from growing unbounded.
  mkdir -p "$(dirname "$LOG")" 2>/dev/null
  printf "%s %s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG" 2>/dev/null
  if [ -f "$LOG" ] && [ "$(wc -l < "$LOG" 2>/dev/null)" -gt 300 ]; then
    tail -n 200 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null
  fi
}

# vlq QUERY WINDOW
#   Runs a LogsQL count() query, retrying transient curl failures (timeout,
#   connection reset, 5xx) up to 3× with backoff. 8s per-try timeout rides
#   over the multi-second I/O stalls without the old hard 5s cutoff.
#   On a 200 response: prints the integer count and returns 0 (count may be 0).
#   If all tries fail at transport level: prints nothing and returns 1.
vlq() {
  local q="$1" win="$2" body rc n try
  for try in 1 2 3; do
    body=$(curl -sf -m 8 --get \
      --data-urlencode "query=$q" \
      --data-urlencode "start=$win" \
      "$VL/select/logsql/query" 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 0 ]; then
      n=$(printf "%s" "$body" | python3 -c "import sys, json
c = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
        c = int(d.get(\"n\", 0))
        break
    except Exception:
        pass
print(c)")
      printf "%s" "$n"
      return 0
    fi
    sleep 2
  done
  return 1
}

# Stage 1: server reachable? (retry — health can blip under I/O load too)
H=000
for try in 1 2 3; do
  H=$(curl -s -o /dev/null -w "%{http_code}" -m 6 "$VL/health" 2>/dev/null || echo "000")
  [ "$H" = "200" ] && break
  sleep 2
done
if [ "$H" != "200" ]; then
  printf "STAGE=vlogs-down msg=health-http-%s\n" "$H" >&2
  logfail "vlogs-down health-http-$H"
  exit 1
fi

# Stage 2: any ingest in last 15 min? A transient query timeout retries
# inside vlq; only a query that never returns 200 across 3 tries fails here.
if ! N=$(vlq "* | stats count() as n" "15m"); then
  printf "STAGE=vlogs-query-fail msg=count-query-no-200-after-retries\n" >&2
  logfail "vlogs-query-fail count-query-no-200-after-retries"
  exit 1
fi
if [ "${N:-0}" -lt 1 ]; then
  printf "STAGE=vlogs-no-ingest msg=zero-lines-last-15min\n" >&2
  logfail "vlogs-no-ingest zero-lines-last-15min"
  exit 1
fi

# Stage 3: per-app freshness. sonarr/radarr/prowlarr poll continuously and
# qbittorrent logs torrent events; all four reliably log within 30 min when
# routing is healthy. A query that fails at transport level is INCONCLUSIVE
# (skipped) — NOT counted as stale — so a transient stall no longer fires a
# false vlogs-stale-app. Only a definitive 200-with-zero marks an app stale.
STALE=""
SKIPPED=""
for app in sonarr radarr prowlarr qbittorrent; do
  if AN=$(vlq "app:$app | stats count() as n" "30m"); then
    if [ "${AN:-0}" -lt 1 ]; then
      STALE="$STALE $app"
    fi
  else
    SKIPPED="$SKIPPED $app"
  fi
done

# A single quiet *arr is NORMAL — radarr/qbittorrent legitimately go >30m with
# no log lines when idle (no grabs/imports/searches). Require >=2 stale apps
# before failing: a real logs.py/ingest routing break shows up as multiple apps
# stale at once, whereas one stale app is just idleness. Tuned 2026-06-27 after
# 153 false "stale-30m-1 radarr" fires. 0-1 stale is tolerated and noted.
STALE_COUNT=$(printf "%s" "$STALE" | wc -w)
if [ "${STALE_COUNT:-0}" -ge 2 ]; then
  printf "STAGE=vlogs-stale-app msg=apps-stale-30m-%s%s\n" "$STALE_COUNT" "$STALE" >&2
  logfail "vlogs-stale-app apps-stale-30m-$STALE_COUNT$STALE"
  exit 1
fi

printf "vlogs-flowing total_15m=%s stale-tolerated=%s%s%s\n" "$N" "${STALE_COUNT:-0}" \
  "$( [ -n "$STALE" ] && printf " idle:%s" "$STALE" )" \
  "$( [ -n "$SKIPPED" ] && printf " inconclusive:%s" "$SKIPPED" )"
exit 0
')
RC=$?
echo "$RES"
exit $RC
