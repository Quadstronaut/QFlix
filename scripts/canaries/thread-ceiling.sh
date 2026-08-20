#!/usr/bin/env bash
# Thread-ceiling canary: track the user's task count (processes + threads)
# against the RLIMIT_NPROC ceiling (ulimit -u).
#
# WHY: Ultra.cc's shared box has 128 cores but ulimit -u = 2000 per user. A Go
# app that defaults GOMAXPROCS to the host's 128 cores spawns hundreds of
# threads, and near the 2000 ceiling pthread_create() fails with EAGAIN -- the
# crash-loop class from the 2026-06-26 VictoriaLogs incident (memory
# seedbox-thread-cap-gomaxprocs). RLIMIT_NPROC counts EVERY task (process or
# thread) the user owns, so the live thread count is the number that matters.
# It's invisible until an app dies, so we watch the headroom. Baseline at the
# 2026-07-27 audit was ~1026/2000 (51%).
#
# Thresholds (override via env QFLIX_CANARY_THREAD_*_PCT):
#   65% WARN -> annotate Kuma msg; stay UP. Time to find the thread producer.
#   85% FAIL, and only when SUSTAINED across 2 consecutive samples -> DOWN.
#              A runaway producer needs a GOMAXPROCS cap (in its unit) or a
#              restart -- an autonomous kill is unsafe here (we can't know
#              which app to sacrifice without risking a customer-facing
#              outage).
#
# WHY 65 WARN / 85 FAIL + A 2-SAMPLE STREAK (set 2026-08-20 from measurement)
# --------------------------------------------------------------------------
# A ceiling canary has two ways to be worthless: it can red too late to be a
# warning, or it can red on healthy load until the operator stops reading it.
# This repo has burned weeks on the second failure mode, so the threshold is
# derived from measured load, not chosen for feel. Everything below was
# sampled on the box on 2026-08-20.
#
#   MEASURED INPUTS
#   * ulimit -u = 2000.
#   * Idle baseline, no Tdarr ffmpeg running: 993-1022 tasks across three
#     20s-spaced samples (consistent with the 2026-07-27 audit's 1026).
#   * Tdarr worker cost, sampled directly off /proc/<pid>/task:
#       - one transcode worker (ffmpeg)   = 273 tasks
#       - one healthcheck worker (ffmpeg) = 129 tasks
#   * Tdarr node worker limits (GET /api/v2/get-nodes): the live runtime grant
#     is transcodecpu=1 + healthcheckcpu=1 = 2 concurrent ffmpeg workers, but
#     the node's 24-hour SCHEDULE grants transcodecpu=2 + healthcheckcpu=2 = 4
#     concurrent workers in EVERY hour block. Two concurrent workers is
#     therefore routine, not exceptional.
#   * Routine two-worker load observed today: 1543/2000 = 77.2%. Two
#     FULL-SIZE transcode workers project to ~1687 = 84.4%.
#
#   WHY NOT 78% FAIL (the value shipped earlier today and reverted here)
#   78% of 2000 trips at 1560. The box measured 1543 with two ordinary workers
#   running -- 17 tasks under the trip -- and two full-size transcodes put it
#   at ~1687, well over. A threshold that sits BELOW normal transcoding load
#   is a false-positive generator, and a canary nobody trusts protects
#   nothing.
#
#   WHY 85% FAIL
#   85% trips at 1700. That is above the 1687 two-full-transcode projection,
#   so routine Tdarr work does not page, and it leaves 300 tasks of headroom =
#   1.10x a single 273-task transcode spawn. So the worst instantaneous case
#   -- a PASS reported at 1699 followed immediately by one worker allocating
#   its whole pool -- lands at 1972, still under 2000. One burst from the trip
#   point cannot reach EAGAIN.
#
#   WHY THE 2-SAMPLE STREAK
#   1.10x headroom is thin, and the old "red must precede the ceiling by more
#   than one burst" goal cannot be met by raising the trip further -- the
#   schedule's 4-worker grant means healthy load can legitimately reach
#   ~1010 + 2*273 + 2*129 = ~1814 (91%). So the extra safety comes from time
#   instead of headroom: FAIL requires the count to sit at or above the trip
#   on TWO consecutive runs. The timer is OnCalendar=*:0/15
#   (manitoba-maint-canary-thread-ceiling.timer), so a page means 30 minutes
#   of sustained pressure. This splits the two populations cleanly:
#     - a worker starting while another finishes crosses 1700 for one sample
#       and is gone by the next -> annotated, not paged;
#     - a GOMAXPROCS runaway is monotonic and never falls back under the trip
#       -> paged on the second sample, still with 300 tasks of room.
#   The streak counter lives in ~/.opt/maint/thread-ceiling-over.state (same
#   convention as qbit-stall-since.epoch) and is discarded if older than
#   2700s, so a disabled-then-re-enabled timer cannot page off a stale 1.
#
#   WHY WARN AT 65%
#   WARN must sit at least one burst below FAIL or it is unobservable -- a
#   single spawn would carry the count through the whole warn band between two
#   runs. 65% (1300) is 400 tasks below the 1700 trip = 1.47x a transcode
#   spawn, and sits just above the ~1010 idle baseline. Consequence, accepted
#   deliberately: WARN annotates whenever two Tdarr workers run concurrently.
#   That is correct -- it is real elevation -- and WARN exits 0, so it colours
#   the Kuma message without paging anyone.
#
# Stage labels (printed to stderr on failure -> Kuma `msg=`):
#   STAGE=thread-fail        -- 85%+ on 2 consecutive samples, DOWN
#   STAGE=thread-parse-fail  -- could not read the count or the ulimit
#   (the 65% warn and the first over-85% sample are NOT stage labels: they
#    exit 0 and ride out on the PASS-WARN stdout line)
#
# Exit:
#   0 -- under the fail trip, or over it for the first time only
#        (Kuma sees msg=PASS-WARN)
#   1 -- sustained fail / parse fail -- Kuma sees status=down + STAGE label
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

WARN_PCT=${QFLIX_CANARY_THREAD_WARN_PCT:-65}
FAIL_PCT=${QFLIX_CANARY_THREAD_FAIL_PCT:-85}
# Consecutive over-trip samples required before the canary pages. Set to 1 to
# disable the streak and restore instantaneous FAIL.
STREAK_REQ=${QFLIX_CANARY_THREAD_STREAK:-2}

RES=$(sshm "
set -uo pipefail
WARN_PCT=${WARN_PCT}; FAIL_PCT=${FAIL_PCT}; STREAK_REQ=${STREAK_REQ}
STATE_DIR=~/.opt/maint
STATE_FILE=\$STATE_DIR/thread-ceiling-over.state
# Discard a streak older than 3 timer intervals (15m * 3). Without this, a
# timer stopped mid-streak would page on its very first sample after being
# re-enabled, off a counter left over from an unrelated day.
MAX_STATE_AGE=2700

LIMIT=\$(ulimit -u 2>/dev/null)
UID_N=\$(id -u)
# Threads == tasks (light-weight processes). RLIMIT_NPROC is enforced against
# the count of ALL tasks the user owns, so -L (one line per thread) is the metric.
THREADS=\$(ps -u \"\$UID_N\" -L --no-headers 2>/dev/null | wc -l)
PROCS=\$(ps -u \"\$UID_N\" --no-headers 2>/dev/null | wc -l)

# VALIDATE THE DENOMINATOR BEFORE USING IT. The previous guard only rejected
# empty and 'unlimited'; a LIMIT of 0 or a non-numeric value sailed through,
# the percentage arithmetic aborted on divide-by-zero, PCT came out empty, and
# every downstream [ \"\" -ge N ] errored and fell through to exit 0 -- i.e.
# the canary reported GREEN precisely when it could not measure anything. Both
# operands are shape-checked first, and the -le tests are short-circuited
# behind those shape tests so a non-numeric value never reaches an arithmetic
# comparison.
case \"\${LIMIT:-}\" in ''|*[!0-9]*) LIMIT_OK=0 ;; *) LIMIT_OK=1 ;; esac
case \"\${THREADS:-}\" in ''|*[!0-9]*) THREADS_OK=0 ;; *) THREADS_OK=1 ;; esac
if [ \"\$LIMIT_OK\" -eq 0 ] || [ \"\$THREADS_OK\" -eq 0 ] || [ \"\$LIMIT\" -le 0 ] || [ \"\$THREADS\" -le 0 ]; then
  echo \"STAGE=thread-parse-fail msg=limit=\${LIMIT:-empty}-threads=\${THREADS:-empty}\" >&2
  exit 1
fi

# INTEGER TRIP POINTS, NO PERCENTAGE ROUND-TRIP. The old code formatted a
# %.1f percentage with awk, truncated it back to an int, and compared THAT to
# the threshold: at threads=1559/2000 awk rounded 77.95 up to '78.0' so the
# canary FAILed while an audit comparing the raw float said it would not. The
# threshold is now converted into a task count once and compared against the
# raw task count, so shell and python agree by construction.
TRIP_FAIL=\$(( LIMIT * FAIL_PCT / 100 ))
TRIP_WARN=\$(( LIMIT * WARN_PCT / 100 ))
# Display-only, integer tenths. Never used for a decision, and it can neither
# come out empty nor divide by zero because LIMIT is validated above.
PCT_T=\$(( THREADS * 1000 / LIMIT ))
PCT=\$(( PCT_T / 10 )).\$(( PCT_T % 10 ))

NOW=\$(date +%s)
PREV_N=0; PREV_T=0
if [ -r \"\$STATE_FILE\" ]; then
  read -r PREV_N PREV_T < \"\$STATE_FILE\" || { PREV_N=0; PREV_T=0; }
  case \"\${PREV_N:-}\" in ''|*[!0-9]*) PREV_N=0 ;; esac
  case \"\${PREV_T:-}\" in ''|*[!0-9]*) PREV_T=0 ;; esac
  [ \$(( NOW - PREV_T )) -le \"\$MAX_STATE_AGE\" ] || PREV_N=0
fi
mkdir -p \"\$STATE_DIR\" 2>/dev/null

# 85% FAIL, sustained -- a runaway thread producer; operator must cap
# GOMAXPROCS or restart. A single over-trip sample only arms the streak.
if [ \"\$THREADS\" -ge \"\$TRIP_FAIL\" ]; then
  STREAK=\$(( PREV_N + 1 ))
  printf '%s %s\n' \"\$STREAK\" \"\$NOW\" > \"\$STATE_FILE\" 2>/dev/null || true
  if [ \"\$STREAK\" -ge \"\$STREAK_REQ\" ]; then
    echo \"STAGE=thread-fail msg=threads-\${THREADS}/\${LIMIT}-\${PCT}pct-procs-\${PROCS}-at-or-over-trip-\${TRIP_FAIL}-for-\${STREAK}-consecutive-samples-runaway-needs-GOMAXPROCS-cap-or-restart\" >&2
    exit 1
  fi
  echo \"PASS-WARN: threads=\${THREADS}/\${LIMIT}-\${PCT}pct-procs=\${PROCS}-over-fail-trip=\${TRIP_FAIL}-sample=\${STREAK}/\${STREAK_REQ}-not-yet-sustained\"
  exit 0
fi

# Under the fail trip: the streak is broken, so clear it. Written on every
# under-trip run rather than only on transitions, so a hand-edited or
# half-written state file self-heals within one interval.
printf '0 %s\n' \"\$NOW\" > \"\$STATE_FILE\" 2>/dev/null || true

# 65% WARN -- annotate but stay UP (Kuma green, msg communicates the warn).
if [ \"\$THREADS\" -ge \"\$TRIP_WARN\" ]; then
  echo \"PASS-WARN: threads=\${THREADS}/\${LIMIT}-\${PCT}pct-procs=\${PROCS}-warn-trip=\${TRIP_WARN}-fail-trip=\${TRIP_FAIL}\"
  exit 0
fi
echo \"PASS: threads=\${THREADS}/\${LIMIT}-\${PCT}pct-procs=\${PROCS}-warn-trip=\${TRIP_WARN}-fail-trip=\${TRIP_FAIL}\"
exit 0
") || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC
