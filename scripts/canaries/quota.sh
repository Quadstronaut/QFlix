#!/usr/bin/env bash
# Quota canary: track Ultra.cc per-user disk quota across three action levels.
#
# Why three levels: Ultra.cc enforces a HARD per-user quota. At 100% the
# kernel silently denies writes and SQLite-backed apps (Sonarr, Radarr,
# Prowlarr) crash with "disk I/O error" — invisible until the
# next operator login. We MUST reclaim space BEFORE the wall, not after.
#
# Thresholds (override via env QFLIX_CANARY_QUOTA_*_PCT):
#   80% WARN     → annotate Kuma msg; stay UP. Operator can plan a cleanup.
#   90% CRITICAL → autonomously fire qflix-reaper --execute (honoring its
#                  built-in max-items/max-pct caps + run-lock) to reclaim.
#                  Push DOWN so operator sees something happened.
#   98% FAIL     → autonomous reclaim is too late; force operator attention.
#                  DOWN + msg flagging intervention needed.
#
# Stage labels (printed to stderr on failure → Kuma `msg=`):
#   STAGE=quota-warn        — 80%+, still UP, no autonomous action
#   STAGE=quota-critical    — 90%+, fired qflix-reaper reclaim, DOWN
#   STAGE=quota-fail        — 98%+, intervention needed, DOWN
#   STAGE=quota-parse-fail  — could not parse `quota` output
#   STAGE=quota-reclaim-fail — qflix-reaper returned errors during reclaim
#
# Exit:
#   0 — under critical (80% warn still exits 0; Kuma sees msg=PASS-WARN)
#   1 — critical/fail/parse — Kuma sees status=down + STAGE label
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

WARN_PCT=${QFLIX_CANARY_QUOTA_WARN_PCT:-80}
CRIT_PCT=${QFLIX_CANARY_QUOTA_CRIT_PCT:-90}
FAIL_PCT=${QFLIX_CANARY_QUOTA_FAIL_PCT:-98}

RES=$(sshm "
set -uo pipefail
WARN_PCT=${WARN_PCT}; CRIT_PCT=${CRIT_PCT}; FAIL_PCT=${FAIL_PCT}
# Parse quota -p output. Sample:
#   /dev/sdac1 1595865084  2929721344 2929721344       0  151694 ...
# Cols: filesystem blocks(used) quota(soft) limit(hard) grace ...
# blocks/quota are 1KiB units. May have trailing '*' on blocks when grace started.
LINE=\$(quota -p 2>/dev/null | awk '\$1 ~ /^\/dev\//')
if [ -z \"\$LINE\" ]; then
  echo 'STAGE=quota-parse-fail msg=no-dev-line-in-quota-output' >&2
  exit 1
fi
USED=\$(printf '%s' \"\$LINE\" | awk '{print \$2}' | tr -d '*')
LIMIT=\$(printf '%s' \"\$LINE\" | awk '{print \$3}')
if [ -z \"\$USED\" ] || [ -z \"\$LIMIT\" ] || [ \"\$LIMIT\" -le 0 ]; then
  echo \"STAGE=quota-parse-fail msg=used=\$USED-limit=\$LIMIT\" >&2
  exit 1
fi
PCT=\$(awk -v u=\"\$USED\" -v l=\"\$LIMIT\" 'BEGIN{printf \"%.1f\", (u/l)*100}')
USED_G=\$(awk -v u=\"\$USED\" 'BEGIN{printf \"%.0f\", u/1048576}')
LIMIT_G=\$(awk -v l=\"\$LIMIT\" 'BEGIN{printf \"%.0f\", l/1048576}')
PCT_INT=\${PCT%.*}

# 98% FAIL — operator intervention; do NOT fire autonomous reclaim again
# (it would race with whatever the operator is mid-doing).
if [ \"\$PCT_INT\" -ge \"\$FAIL_PCT\" ]; then
  echo \"STAGE=quota-fail msg=quota-\${PCT}%-of-\${LIMIT_G}G-FAIL-\${FAIL_PCT}%-OPERATOR-INTERVENTION-NEEDED\" >&2
  exit 1
fi

# 90% CRITICAL — autonomously fire qflix-reaper --execute, push DOWN. The
# reaper's own default caps (max-items/max-pct, abort-before-mutation) +
# run-lock ARE the safety envelope; the caps are NOT overridden. Fold the
# reaper's stdout+stderr into REAPER_OUT so they never reach the canary's
# PASS/PASS-WARN stdout contract, and capture \$? immediately.
if [ \"\$PCT_INT\" -ge \"\$CRIT_PCT\" ]; then
  REAPER_OUT=\$(python3 ~/scripts/maint/qflix-reaper.py --execute --json 2>&1)
  REAPER_RC=\$?
  if [ \"\$REAPER_RC\" -eq 0 ]; then
    echo \"STAGE=quota-critical msg=quota-\${PCT}%-of-\${LIMIT_G}G-FIRED-reaper-rc=0\" >&2
  else
    echo \"STAGE=quota-reclaim-fail msg=quota-\${PCT}%-of-\${LIMIT_G}G-reaper-rc=\${REAPER_RC}\" >&2
  fi
  exit 1
fi

# 80% WARN — annotate but exit 0 (Kuma stays UP, msg communicates the warn)
if [ \"\$PCT_INT\" -ge \"\$WARN_PCT\" ]; then
  echo \"PASS-WARN: quota=\${PCT}%-used=\${USED_G}G/\${LIMIT_G}G-warn=\${WARN_PCT}%-crit=\${CRIT_PCT}%-fail=\${FAIL_PCT}%\"
  exit 0
fi
echo \"PASS: quota=\${PCT}%-used=\${USED_G}G/\${LIMIT_G}G-warn=\${WARN_PCT}%-crit=\${CRIT_PCT}%-fail=\${FAIL_PCT}%\"
exit 0
") || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC
