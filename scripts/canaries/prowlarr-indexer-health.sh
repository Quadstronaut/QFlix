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
# WHAT PROBE 1 ACTUALLY COUNTS — LOG LINES, NOT EVENTS (2026-08-03).
# One failed grab emits roughly TEN matching lines, because vlogs indexes each
# stack-trace frame of the TooManyRequestsException as its own record. So a
# threshold of 25 is ~2-3 real grab failures, not 25. Do not "raise it to
# something sensible" without re-measuring that ratio; the number reads ten
# times stricter than it is.
#
# WINDOW vs TIMER — the blind gap this canary shipped with (fixed 2026-08-03).
# manitoba-maint-canary-prowlarr-indexer-health.timer is OnCalendar=*:0/15 with
# RandomizedDelaySec=60, so consecutive runs land up to 15m+60s apart. A 10m
# lookback therefore left AT LEAST 5 minutes of every cycle unobserved, and the
# 429 bursts are minutes long: on 2026-08-02 the Knaben 403->500->backoff->429
# chain produced 10-minute buckets of 234/177/126/108/86/79 lines and this
# canary stayed exit 0 through all of them. The window must be >= the worst-case
# run spacing (15m + 2x60s jitter = 17m) or bursts fall between ticks; 20m gives
# ~3 minutes of deliberate overlap. Overlap can re-report one burst on two
# consecutive ticks — that is the correct trade, since the alternative is not
# reporting it at all, and Kuma collapses a repeated DOWN into one incident.
# tests/unit/test_prowlarr_indexer_health_tuning.py pins this relationship
# against the real timer file so it cannot silently drift back.
#
# Tunables (override via systemd Environment= or env vars):
#   PROWLARR_CASCADE_429_THRESHOLD   default 25 (log LINES / window, see above)
#   PROWLARR_CASCADE_WINDOW          default 20m
#
# Exits:
#   0 — healthy (no cascade, no chronic-stale indexer)
#   1 — at least one STAGE detected (see stderr for label + msg=…)
#
# NOT IN SCOPE, deliberately: "Prowlarr thinks it syncs indexer X to Radarr but
# Radarr does not have it" and "an indexer's download path 403s because
# preferMagnetUrl is off". Both are CONFIG state, permanent until an operator
# acts, and invisible to both probes below (Prowlarr /api/v1/health is [] for
# them). They live in scripts/canaries/prowlarr-app-sync.sh — own module, own
# timer, own Kuma check.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
SECRETS=~/secrets
# 25, reconciled 2026-08-03. The code said 40 while BOTH doc surfaces (this
# script header and manifest/apps.yaml) said 25 — three surfaces, two values,
# the "prompt and rules are two policy surfaces" failure again. Reconciled DOWN
# to the documented 25 because it is the more sensitive of the two and this
# probe has never once fired. Counts LOG LINES (~10 per grab failure).
THRESHOLD=${PROWLARR_CASCADE_429_THRESHOLD:-25}
# 20m, widened from 10m 2026-08-03: must exceed the worst-case timer run
# spacing (OnCalendar=*:0/15 + 2x RandomizedDelaySec=60 = 17m) or bursts fall
# into the gap between ticks. See the header.
WINDOW=${PROWLARR_CASCADE_WINDOW:-20m}
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
