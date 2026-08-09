#!/usr/bin/env bash
# Thread-ceiling canary: track the user's task count (processes + threads)
# against the RLIMIT_NPROC ceiling (ulimit -u).
#
# WHY: Ultra.cc's shared box has 128 cores but ulimit -u = 2000 per user. A Go
# app that defaults GOMAXPROCS to the host's 128 cores spawns hundreds of
# threads, and near the 2000 ceiling pthread_create() fails with EAGAIN — the
# crash-loop class from the 2026-06-26 VictoriaLogs incident (memory
# seedbox-thread-cap-gomaxprocs). RLIMIT_NPROC counts EVERY task (process or
# thread) the user owns, so the live thread count is the number that matters.
# It's invisible until an app dies, so we watch the headroom. Baseline at the
# 2026-07-27 audit was ~1026/2000 (51%).
#
# Thresholds (override via env QFLIX_CANARY_THREAD_*_PCT):
#   70% WARN → annotate Kuma msg; stay UP. Time to find the thread producer.
#   85% FAIL → DOWN. A runaway producer needs a GOMAXPROCS cap (in its unit) or
#              a restart — an autonomous kill is unsafe here (we can't know which
#              app to sacrifice without risking a customer-facing outage).
#
# Stage labels (printed to stderr on failure → Kuma `msg=`):
#   STAGE=thread-warn        — 70%+, still UP, msg communicates the warn
#   STAGE=thread-fail        — 85%+, DOWN, operator attention
#   STAGE=thread-parse-fail  — could not read the count or the ulimit
#
# Exit:
#   0 — under FAIL (70% warn still exits 0; Kuma sees msg=PASS-WARN)
#   1 — fail/parse — Kuma sees status=down + STAGE label
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

WARN_PCT=${QFLIX_CANARY_THREAD_WARN_PCT:-70}
FAIL_PCT=${QFLIX_CANARY_THREAD_FAIL_PCT:-85}

RES=$(sshm "
set -uo pipefail
WARN_PCT=${WARN_PCT}; FAIL_PCT=${FAIL_PCT}
LIMIT=\$(ulimit -u 2>/dev/null)
UID_N=\$(id -u)
# Threads == tasks (light-weight processes). RLIMIT_NPROC is enforced against
# the count of ALL tasks the user owns, so -L (one line per thread) is the metric.
THREADS=\$(ps -u \"\$UID_N\" -L --no-headers 2>/dev/null | wc -l)
PROCS=\$(ps -u \"\$UID_N\" --no-headers 2>/dev/null | wc -l)
if [ -z \"\$LIMIT\" ] || [ \"\$LIMIT\" = 'unlimited' ] || [ \"\$THREADS\" -le 0 ]; then
  echo \"STAGE=thread-parse-fail msg=limit=\${LIMIT}-threads=\${THREADS}\" >&2
  exit 1
fi
PCT=\$(awk -v t=\"\$THREADS\" -v l=\"\$LIMIT\" 'BEGIN{printf \"%.1f\", (t/l)*100}')
PCT_INT=\${PCT%.*}

# 85% FAIL — a runaway thread producer; operator must cap GOMAXPROCS or restart.
if [ \"\$PCT_INT\" -ge \"\$FAIL_PCT\" ]; then
  echo \"STAGE=thread-fail msg=threads-\${THREADS}/\${LIMIT}-\${PCT}%-procs-\${PROCS}-FAIL-\${FAIL_PCT}%-runaway-needs-GOMAXPROCS-cap-or-restart\" >&2
  exit 1
fi

# 70% WARN — annotate but stay UP (Kuma green, msg communicates the warn).
if [ \"\$PCT_INT\" -ge \"\$WARN_PCT\" ]; then
  echo \"PASS-WARN: threads=\${THREADS}/\${LIMIT}-\${PCT}%-procs=\${PROCS}-warn=\${WARN_PCT}%-fail=\${FAIL_PCT}%\"
  exit 0
fi
echo \"PASS: threads=\${THREADS}/\${LIMIT}-\${PCT}%-procs=\${PROCS}-warn=\${WARN_PCT}%-fail=\${FAIL_PCT}%\"
exit 0
") || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC
