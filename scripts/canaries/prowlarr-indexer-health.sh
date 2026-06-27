#!/usr/bin/env bash
# prowlarr-indexer-health canary: detect two patterns that the 2026-05-09
# and 2026-05-16 audits found recurring across the *arr stack but which
# nothing alerts on today:
#
#   STAGE=prowlarr-429-cascade   Many simultaneous HTTP 429s from *arr →
#                                Prowlarr in the last 10 min. The root
#                                cause is usually a single indexer that
#                                Prowlarr has disabled (FlareSolverr 500s,
#                                Cloudflare CAPTCHA timeouts, etc.) and
#                                every *arr is now re-trying it. WARN.md
#                                2026-05-16 captured ~70 cascading 429
#                                lines (sonarr 50, radarr 6, radarr2 9)
#                                from one such upstream event.
#
#   STAGE=indexer-rss-stale      Prowlarr's /api/v1/health reports one or
#                                more indexers with chronic RSS-coverage
#                                failures. The 2026-05-16 audit caught
#                                nekoBT in this state since 2026-05-12 (12h+
#                                gap windows) — exactly the failure mode
#                                this signal exists to surface.
#
# This canary is a *detect-and-notify* tool — it does NOT mutate Prowlarr
# state. Disabling an indexer or restarting FlareSolverr is the operator's
# call (the FlareSolverr canary already auto-restarts on listener failure;
# see scripts/maint/flaresolverr-canary.py).
#
# Tunables (override via systemd Environment= or env vars):
#   PROWLARR_CASCADE_429_THRESHOLD   default 25 (events / 10 min window)
#   PROWLARR_CASCADE_WINDOW           default 10m
#
# Exits:
#   0 — healthy (no cascade, no chronic-stale indexer)
#   1 — at least one STAGE detected (see stderr for label + msg=…)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
SECRETS=~/secrets
THRESHOLD=${PROWLARR_CASCADE_429_THRESHOLD:-40}
WINDOW=${PROWLARR_CASCADE_WINDOW:-10m}
# Require >=2 chronically-stale indexers before firing — a single public indexer
# (Tokyo Toshokan, Knaben, ...) going briefly unavailable is routine noise, not a
# real outage. Tuned 2026-06-27 (set to 1 for strict single-indexer detection).
STALE_THRESHOLD=${PROWLARR_INDEXER_STALE_THRESHOLD:-2}

VL_PORT=$(cat "$SECRETS/vlogs.port" 2>/dev/null)
PROW_PORT=$(cat "$SECRETS/prowlarr.port" 2>/dev/null)
PROW_URLBASE=$(cat "$SECRETS/prowlarr.urlbase" 2>/dev/null || echo prowlarr)
PROW_KEY=$(cat "$SECRETS/prowlarr.key" 2>/dev/null)

if [ -z "$VL_PORT" ] || [ -z "$PROW_PORT" ] || [ -z "$PROW_KEY" ]; then
  printf "STAGE=prowlarr-canary-config-missing msg=vlogs.port=%s-prowlarr.port=%s-key=%s\n" \
    "${VL_PORT:-EMPTY}" "${PROW_PORT:-EMPTY}" "${PROW_KEY:+SET}" >&2
  exit 1
fi
VL="http://127.0.0.1:$VL_PORT"
PROW="http://127.0.0.1:$PROW_PORT/$PROW_URLBASE"

# --- Probe 1: 429 cascade across *arr stack (vlogs) -----------------------
# LogsQL count of TooManyRequests / "429" entries from sonarr/radarr/etc in
# the last $WINDOW. Single round-trip via stats count().
QUERY="(app:sonarr OR app:sonarr2 OR app:radarr OR app:radarr2) _msg:\"TooManyRequests\" | stats count() as n"
RAW=$(curl -sf -m 10 --get \
  --data-urlencode "query=$QUERY" \
  --data-urlencode "start=$WINDOW" \
  "$VL/select/logsql/query" 2>/dev/null) || RAW=""

N429=$(printf "%s" "$RAW" | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line); print(d.get(\"n\", 0)); break
    except Exception: pass
else:
    print(0)
" 2>/dev/null) || N429=0
N429=${N429:-0}

CASCADE=0
if [ "$N429" -ge "$THRESHOLD" ]; then
  CASCADE=1
fi

# --- Probe 2: Prowlarr /api/v1/health for chronic indexer-down items -----
# Prowlarr surfaces long-term indexer outages as warning-level health items
# with messages like "Indexers unavailable due to failures for more than 6
# hours: <name>" — exact wording varies by Prowlarr version. We treat any
# warning-or-error level item whose message mentions "Indexer" as a stale
# signal worth surfacing.
HEALTH=$(curl -sf -m 8 -H "X-Api-Key: $PROW_KEY" "$PROW/api/v1/health" 2>/dev/null) || HEALTH="[]"
STALE=$(printf "%s" "$HEALTH" | python3 -c "
import sys, json
try:
    items = json.loads(sys.stdin.read() or \"[]\")
except Exception:
    items = []
flagged = []
for h in items:
    t = (h.get(\"type\") or \"\").lower()
    if t not in (\"warning\", \"error\"):
        continue
    msg = h.get(\"message\") or \"\"
    src = h.get(\"source\") or \"\"
    if \"indexer\" in msg.lower() or \"indexer\" in src.lower():
        flagged.append(msg[:120])
print(json.dumps(flagged))
" 2>/dev/null)
STALE=${STALE:-"[]"}
STALE_COUNT=$(printf "%s" "$STALE" | python3 -c "
import sys, json
try:
    print(len(json.loads(sys.stdin.read() or \"[]\")))
except Exception:
    print(0)
" 2>/dev/null)
STALE_COUNT=${STALE_COUNT:-0}

# --- Decision --------------------------------------------------------------
FAIL=0
if [ "$CASCADE" = "1" ]; then
  # First 120 chars of the stale-indexer list (if any) help triage the
  # cascade alert — the offending indexer is almost always one of them.
  STALE_HINT=$(printf "%s" "$STALE" | python3 -c "
import sys, json
try:
    items = json.loads(sys.stdin.read() or \"[]\")
except Exception:
    items = []
print(\";\".join(items)[:120])
" 2>/dev/null)
  printf "STAGE=prowlarr-429-cascade msg=count=%s-window=%s-threshold=%s-stale_indexers=[%s]\n" \
    "$N429" "$WINDOW" "$THRESHOLD" "$STALE_HINT" >&2
  FAIL=1
fi

if [ "$STALE_COUNT" -ge "$STALE_THRESHOLD" ] && [ "$CASCADE" = "0" ]; then
  # RSS-stale fires independently of cascade so we get the slow-decay
  # signal (chronic disabled indexer with no current 429 storm) too.
  STALE_HINT=$(printf "%s" "$STALE" | python3 -c "
import sys, json
try:
    items = json.loads(sys.stdin.read() or \"[]\")
except Exception:
    items = []
print(\";\".join(items)[:120])
" 2>/dev/null)
  printf "STAGE=indexer-rss-stale msg=count=%s-items=[%s]\n" "$STALE_COUNT" "$STALE_HINT" >&2
  FAIL=1
fi

if [ "$FAIL" = "0" ]; then
  printf "prowlarr-indexer-flowing 429_count=%s stale_indexers=%s window=%s\n" \
    "$N429" "$STALE_COUNT" "$WINDOW"
  exit 0
fi
exit 1
')
RC=$?
echo "$RES"
exit $RC
