#!/usr/bin/env bash
# stream-cap-liveness canary: are the per-member stream-cap crons still running?
#
# WHY THIS EXISTS
# ---------------
# Two crontab entries enforce and observe the fair-use concurrent-stream cap:
#
#   * * * * *  ~/scripts/plex/kill_stream.sh --max 4
#   * * * * *  ~/scripts/plex/stream_stats.sh
#
# kill_stream is MEMBER-FACING ENFORCEMENT - it is what actually stops one
# account from opening unlimited simultaneous streams. Before this canary it had
# NO dead-man of any kind: not among the Kuma monitors, not among the push
# tokens, no self-push in either script, and structurally unreachable by the
# C-01 timer ledger, whose boundary is systemd timers and (since 2026-08-06)
# declared installer units - crontab is a THIRD scheduling plane that neither
# leg enumerates. The only freshness check in the repo was an assertion inside
# smoke-test.sh, which no timer runs.
#
# The failure is silent and open-ended: if the crontab is dropped by a slot
# rebuild (59a-plex-stream-crons-install.sh records that this already happened
# once), or the plexapi venv breaks, or plex.token expires, the cap simply stops
# being enforced. Nothing goes red. The first signal is a member noticing they
# can stream without limit, or nobody noticing at all.
#
# DETECT-ONLY, AND DELIBERATELY NOT A PATCH TO EITHER SCRIPT. The obvious
# alternative was to add a Kuma push at the end of kill_stream.sh. That edits
# the enforcement path itself: a bug there does not merely mis-report, it can
# stop the cap working or wedge the every-minute cron. A separate observer can
# only ever mis-report, which is the failure mode you want on the safety-
# critical path. One concern, one module (operator design law).
#
# PREDICATES - both read the artefacts the crons already write every run:
#
#   1. stream_stats  -> ~/.apps/stream-stats/state.json, whose `ts` field is
#      stamped by the writer each invocation.
#   2. kill_stream   -> ~/.apps/stream-stats/kill-history.json, to which
#      kill_stream.py:76-77 APPENDS a {ts, decisions} record on EVERY run (not
#      only when it kills something - verified by reading that code, and by
#      sampling both files 75s apart and watching each advance by exactly 60s).
#
# THE CLOCK IS THE EMBEDDED `ts`, NOT THE FILE MTIME. mtime-freshness is a named
# defect class in this repo (manifest/defect-classes.yaml C-04, discovered
# 2026-07-25): a file that is rewritten or merely touched looks fresh while its
# newest CONTENT is days old. Both artefacts carry a writer-stamped epoch, so
# the content clock is available and mtime is not consulted at all.
#
# PRIVACY - LOAD-BEARING, NOT INCIDENTAL. state.json contains member Plex
# usernames, what they are watching, and per-user stream counts. The operator
# principle is explicit: content presence may be surfaced in admin tooling,
# member CONSUMPTION may not. This canary therefore reads ONLY the numeric `ts`
# fields and emits ONLY ages in seconds. It never prints a username, a title, a
# per-user count, or even the total active-stream count - a Kuma msg= is a
# durable, forwarded, operator-visible string, and a stream count is
# consumption. Any future edit that widens what is parsed must keep that line.
#
# THRESHOLD. Both crons fire every minute, so anything under a few minutes is
# normal. Both scripts hold a flock and exit 0 rather than queueing when a prior
# run is still in flight, so a slow Plex legitimately skips several minutes in a
# row on this shared box. 900s (15 min) tolerates ~14 consecutive skips and
# still catches a genuinely dead cron inside a quarter hour.
#
# EXIT CODES
#   0 - both crons have written inside the window
#   1 - stream-cap-stats-stale / stream-cap-kill-stale: a cron has stopped
#   2 - could not assert: artefact unreadable or its `ts` unparseable. Never a
#       silent pass - a canary that cannot read the clock must not report the
#       cap as enforced.
#
# Lives on the seedbox at ~/scripts/canaries/stream-cap-liveness.sh (deployed by
# 240-maintenance-install.sh). Invoked by
# manitoba-maint-canary-stream-cap-liveness, which pushes status=up/down to Kuma
# monitor "Canary Stream Cap Liveness".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail

MAX_AGE=${QFLIX_CANARY_STREAMCAP_MAX_AGE_S:-900}
DIR=${QFLIX_CANARY_STREAMCAP_DIR:-$HOME/.apps/stream-stats}
STATE="$DIR/state.json"
HIST="$DIR/kill-history.json"

# Extract the writer-stamped epoch. For state.json that is the top-level `ts`;
# for kill-history.json it is the `ts` of the LAST appended record. Prints the
# integer or nothing. Reads ONLY ts - see the privacy note in the header.
read_ts() {
  python3 - "$1" "$2" <<PY 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(2)
try:
    if sys.argv[2] == "last":
        if not isinstance(d, list) or not d:
            sys.exit(3)
        print(int(d[-1]["ts"]))
    else:
        print(int(d["ts"]))
except Exception:
    sys.exit(4)
PY
}

NOW=$(date -u +%s)
FAILED=""
AGES=""

check() {
  local label="$1" path="$2" mode="$3" stage="$4"
  if [ ! -f "$path" ]; then
    printf "STAGE=%s msg=%s-artefact-missing-at-%s-cron-has-never-run-or-was-removed\n" \
      "$stage" "$label" "$path" >&2
    FAILED="yes"
    return
  fi
  local ts age
  ts=$(read_ts "$path" "$mode")
  if [ -z "$ts" ]; then
    printf "STAGE=stream-cap-unreadable msg=%s-ts-unparseable-in-%s\n" "$label" "$path" >&2
    BROKEN="yes"
    return
  fi
  age=$((NOW - ts))
  AGES="$AGES ${label}=${age}s"
  if [ "$age" -gt "$MAX_AGE" ]; then
    printf "STAGE=%s msg=%s-last-wrote-%ss-ago-limit-%ss-per-minute-cron-has-stopped\n" \
      "$stage" "$label" "$age" "$MAX_AGE" >&2
    FAILED="yes"
  fi
}

BROKEN=""
check "stream_stats" "$STATE" "top"  "stream-cap-stats-stale"
check "kill_stream"  "$HIST"  "last" "stream-cap-kill-stale"

# A PROVEN failure outranks an unreadable one: if either cron is demonstrably
# dead we report exit 1, even when the other artefact could not be parsed.
[ -n "$FAILED" ] && exit 1
if [ -n "$BROKEN" ]; then
  printf "STAGE=stream-cap-inconclusive msg=could-not-read-one-or-both-artefact-clocks\n" >&2
  exit 2
fi

printf "PASS: stream-cap-liveness - both per-minute crons writing (limit %ss):%s\n" "$MAX_AGE" "$AGES"
exit 0
')
RC=$?
echo "$RES"
exit $RC
