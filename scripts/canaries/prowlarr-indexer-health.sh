#!/usr/bin/env bash
# prowlarr-indexer-health canary: detect two patterns that the 2026-05-09
# and 2026-05-16 audits found recurring across the *arr stack but which
# nothing alerts on today:
#
#   STAGE=prowlarr-429-cascade   Many simultaneous HTTP 429s from *arr →
#                                Prowlarr in the last $WINDOW. The root
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
# ===========================================================================
# WHY THIS PROBE WAS BLIND, AND WHY THE FIRST FIX WAS NOT ENOUGH (2026-08-03)
# ===========================================================================
# The canary stayed exit 0 through a real, sustained cascade. Measured from
# Kuma heartbeat history and the same LogsQL index, on 2026-08-02 (UTC):
#     5m bucket 02:00 -> 559 lines   5m bucket 02:15 -> 334 lines
#     heartbeat 02:01:08  429_count=0    heartbeat 02:15:49  429_count=0
#     heartbeat 02:30:16  429_count=0    heartbeat 02:45:14  429_count=0
# TWO independent terms produce that, and a fix that addresses only one leaves
# the probe blind:
#
#   TERM 1 — RUN SPACING. OnCalendar=*:0/15 with RandomizedDelaySec=60 puts
#   consecutive runs up to 15m+2x60s = 17m apart. Any lookback shorter than
#   that leaves a hole in every cycle. The 02:30:16 run looked at
#   [02:20, 02:30]; the 334-line burst sat at [02:15, 02:20] and was never in
#   ANY window.
#
#   TERM 2 — INGEST LAG. vlogs is not a live tail. qflix-vlogs-ingest.timer is
#   OnUnitActiveSec=5min + RandomizedDelaySec=30, so an event at time T is not
#   QUERYABLE until roughly T+6m. The probe filters on _time, so a burst is
#   only visible to a tick landing in [T+lag, T+WINDOW]. With WINDOW=10m and a
#   6m lag that is a 4-minute slot against 15-minute ticks — it misses most of
#   the time, and if lag ever exceeded the window it would return 0 FOREVER
#   while reporting "flowing".
#
# So the window must clear BOTH: WINDOW >= run-spacing + ingest-lag-budget.
#     17m (spacing) + 25m (lag budget) = 42m   ->   WINDOW default 45m
# and the lag budget itself is now ASSERTED rather than assumed (see the lag
# probe below), because an unmeasured assumption inside a watchdog is the thing
# the watchdog was supposed to remove. Measured 2026-08-03: the union of the
# four *arr apps was non-empty in 96 of 96 consecutive 15-minute buckets over
# 24h, so 25m of total silence from all four at once is a broken pipeline, not
# an idle night.
#
# COST OF THE WIDER WINDOW, stated plainly: a single burst is now re-counted on
# ~3 consecutive ticks. Kuma collapses a repeated DOWN into one incident, and
# the alternative is not reporting it at all. Measured over 48h there were 12
# non-empty 45m buckets, every one of them >= 34 lines, so on real data the
# threshold choice anywhere between ~5 and ~34 is outcome-identical.
#
# THIS PROBE WILL FIRE UNTIL THE KNABEN / TOKYO TOSHOKAN REMEDIATION LANDS.
# That is correct: the grab-failure cascade documented in
# docs/prowlarr-indexer-remediation-2026-08-03.md is live right now.
#
# Tunables (override via systemd Environment= or env vars):
#   PROWLARR_CASCADE_429_THRESHOLD   default 25 (log LINES / window, see above)
#   PROWLARR_CASCADE_WINDOW          default 45m
#   PROWLARR_INGEST_LAG_BUDGET_MIN   default 25 (minutes; blindness assertion)
#
# Exits — empty-because-clean must differ from empty-because-broken:
#   0 — healthy: both probes ANSWERED, no cascade, no chronic-stale indexer
#   1 — at least one STAGE detected (see stderr for label + msg=…)
#   2 — the canary could not assert anything: secrets unreadable, vlogs or
#       Prowlarr unreachable, the LogsQL query never answered, or ingest lag
#       exceeded the budget so a zero count means nothing. Previously EVERY one
#       of these degraded into a zero and printed "prowlarr-indexer-flowing" —
#       a canary that asserts the stack is healthy when it cannot see the stack
#       at all. That is the C-09 silent-exit class and it is fixed here.
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
# 45m = worst-case timer run spacing (17m) + ingest lag budget (25m) + margin.
# See the header: a window sized against the TIMER ALONE is still blind,
# because vlogs is a 5-minute batch ingest, not a live tail.
WINDOW=${PROWLARR_CASCADE_WINDOW:-45m}
# Minutes of tolerated ingest lag. Past this a zero count carries no
# information, so the canary says so (exit 2) instead of printing "flowing".
LAG_BUDGET_MIN=${PROWLARR_INGEST_LAG_BUDGET_MIN:-25}
# Require >=2 chronically-stale indexers before firing — a single public indexer
# (Tokyo Toshokan, Knaben, ...) going briefly unavailable is routine noise, not a
# real outage. Tuned 2026-06-27 (set to 1 for strict single-indexer detection).
STALE_THRESHOLD=${PROWLARR_INDEXER_STALE_THRESHOLD:-2}

APPS="(app:sonarr OR app:sonarr2 OR app:radarr OR app:radarr2)"

VL_PORT=$(cat "$SECRETS/vlogs.port" 2>/dev/null)
PROW_PORT=$(cat "$SECRETS/prowlarr.port" 2>/dev/null)
PROW_URLBASE=$(cat "$SECRETS/prowlarr.urlbase" 2>/dev/null || echo prowlarr)
PROW_KEY=$(cat "$SECRETS/prowlarr.key" 2>/dev/null)

# Config-missing is exit 2, not 1: a canary that cannot read its own secret has
# asserted NOTHING, and telling that apart from a real cascade is the whole
# point of the 1-vs-2 split.
if [ -z "$VL_PORT" ] || [ -z "$PROW_PORT" ] || [ -z "$PROW_KEY" ]; then
  printf "STAGE=prowlarr-canary-config-missing msg=vlogs.port=%s-prowlarr.port=%s-key=%s\n" \
    "${VL_PORT:-EMPTY}" "${PROW_PORT:-EMPTY}" "${PROW_KEY:+SET}" >&2
  exit 2
fi
VL="http://127.0.0.1:$VL_PORT"
PROW="http://127.0.0.1:$PROW_PORT/$PROW_URLBASE"

# vlq QUERY START -> prints raw body on stdout, returns curl status.
# Retries transport faults twice: this is a shared Ultra.cc slot and neighbour
# I/O storms routinely block a <2ms LogsQL query for seconds (same rationale as
# scripts/canaries/vlogs-stall.sh). A non-zero return is NEVER folded into a
# count by the callers below.
vlq() {
  local try rc body
  for try in 1 2 3; do
    body=$(curl -sf -m 12 --get \
      --data-urlencode "query=$1" \
      --data-urlencode "start=$2" \
      "$VL/select/logsql/query" 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 0 ]; then printf "%s" "$body"; return 0; fi
    sleep 2
  done
  return 1
}

# --- Probe 0: INGEST LAG (the blindness assertion) -------------------------
# Newest ingested _time across the four *arr apps. This is a lag measurement,
# not an idleness measurement: any ONE of the four answering recently is
# enough, and the union was measured non-empty in 96/96 consecutive 15-minute
# buckets over 24h. `sort by (_time desc) | limit 1` is used rather than
# max(_time) because it is the shape verified live against the deployed
# VictoriaLogs.
LAG_Q="$APPS | sort by (_time desc) | limit 1 | fields _time"
if ! LAG_RAW=$(vlq "$LAG_Q" "24h"); then
  printf "STAGE=prowlarr-vlogs-unreachable msg=lag-probe-no-200-after-3-tries-cannot-assert\n" >&2
  exit 2
fi
NEWEST=$(printf "%s" "$LAG_RAW" | python3 -c "
import sys, json, calendar, time
stamp = None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    v = d.get(\"_time\")
    if v:
        stamp = v
        break
if not stamp:
    print(\"NONE\")
else:
    try:
        print(int(calendar.timegm(time.strptime(stamp[:19], \"%Y-%m-%dT%H:%M:%S\"))))
    except Exception:
        print(\"NONE\")
" 2>/dev/null) || NEWEST="NONE"
# The x-prefix idiom, not a case pattern starting with the empty string: this
# whole block is inside a SINGLE-QUOTED argument to sshm, and two adjacent
# single quotes there cancel each other out, so a literal empty-string pattern
# would arrive on the box as a bare leading pipe and fail to parse. Same class
# as the unbalanced quote fixed in 320b8cf; locked by
# tests/unit/test_canary_sshm_quoting.py.
case "x${NEWEST:-}" in
  x|x*[!0-9]*)
    # Zero *arr lines in 24 hours means the query answered but the index has
    # nothing to answer WITH. A count of 0 from Probe 1 would then be
    # meaningless, so this is BROKEN, not clean.
    printf "STAGE=prowlarr-vlogs-no-data msg=no-arr-log-lines-in-24h-a-zero-429-count-would-be-meaningless\n" >&2
    exit 2 ;;
esac
LAG_MIN=$(( ( $(date -u +%s) - NEWEST ) / 60 ))
[ "$LAG_MIN" -lt 0 ] && LAG_MIN=0
if [ "$LAG_MIN" -gt "$LAG_BUDGET_MIN" ]; then
  printf "STAGE=prowlarr-vlogs-lagging msg=newest-arr-log-line-is-%smin-old-budget=%smin-window=%s-cannot-assert\n" \
    "$LAG_MIN" "$LAG_BUDGET_MIN" "$WINDOW" >&2
  exit 2
fi

# --- Probe 1: 429 cascade across *arr stack (vlogs) -----------------------
# LogsQL count of TooManyRequests / "429" entries from sonarr/radarr/etc in
# the last $WINDOW. Single round-trip via stats count().
QUERY="$APPS _msg:\"TooManyRequests\" | stats count() as n"
if ! RAW=$(vlq "$QUERY" "$WINDOW"); then
  printf "STAGE=prowlarr-vlogs-unreachable msg=cascade-query-no-200-after-3-tries-cannot-assert\n" >&2
  exit 2
fi

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
" 2>/dev/null) || N429=""
case "x${N429:-}" in
  x|x*[!0-9]*)
    # The query returned 200 but the body did not parse. That is a changed
    # response shape, not a quiet stack — never silently a zero.
    printf "STAGE=prowlarr-vlogs-unparseable msg=cascade-query-body-had-no-integer-n\n" >&2
    exit 2 ;;
esac

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
#
# An UNREACHABLE Prowlarr used to default to "[]" here, i.e. to zero stale
# indexers, i.e. to a green push. It is exit 2 now: a health endpoint we could
# not read is not a health endpoint that said everything is fine.
HEALTH=$(curl -sf -m 8 -H "X-Api-Key: $PROW_KEY" "$PROW/api/v1/health" 2>/dev/null) || {
  printf "STAGE=prowlarr-health-unreachable msg=api-v1-health-no-200-cannot-assert\n" >&2
  exit 2
}
STALE=$(printf "%s" "$HEALTH" | python3 -c "
import sys, json
try:
    items = json.loads(sys.stdin.read() or \"[]\")
except Exception:
    sys.exit(3)
if not isinstance(items, list):
    sys.exit(3)
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
") || {
  printf "STAGE=prowlarr-health-unparseable msg=api-v1-health-body-was-not-a-json-list\n" >&2
  exit 2
}
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
  printf "STAGE=prowlarr-429-cascade msg=count=%s-window=%s-threshold=%s-lag=%smin-stale_indexers=[%s]\n" \
    "$N429" "$WINDOW" "$THRESHOLD" "$LAG_MIN" "$STALE_HINT" >&2
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
  # lag= is printed on the CLEAN line too: it is the evidence that the zero
  # above is a measured zero rather than an unmeasured one.
  printf "prowlarr-indexer-flowing 429_count=%s stale_indexers=%s window=%s lag=%smin\n" \
    "$N429" "$STALE_COUNT" "$WINDOW" "$LAG_MIN"
  exit 0
fi
exit 1
')
RC=$?
echo "$RES"
exit $RC
