#!/usr/bin/env bash
# Quota canary: track Ultra.cc per-user disk quota across three action levels.
#
# Why three levels: Ultra.cc enforces a HARD per-user quota. At 100% the
# kernel silently denies writes and SQLite-backed apps (Sonarr, Radarr,
# Prowlarr, Maintainerr) crash with "disk I/O error" — invisible until the
# next operator login. We MUST reclaim space BEFORE the wall, not after.
#
# Thresholds (override via env QFLIX_CANARY_QUOTA_*_PCT):
#   80% WARN     → annotate Kuma msg; stay UP. Operator can plan a cleanup.
#   90% CRITICAL → autonomously fire Maintainerr execute + collections/handle
#                  to reclaim. Push DOWN so operator sees something happened.
#   98% FAIL     → autonomous reclaim is too late; force operator attention.
#                  DOWN + msg flagging intervention needed.
#
# Stage labels (printed to stderr on failure → Kuma `msg=`):
#   STAGE=quota-warn        — 80%+, still UP, no autonomous action
#   STAGE=quota-critical    — 90%+, fired Maintainerr reclaim, DOWN
#   STAGE=quota-fail        — 98%+, intervention needed, DOWN
#   STAGE=quota-parse-fail  — could not parse `quota` output
#   STAGE=quota-reclaim-fail — Maintainerr returned errors during reclaim
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

# 90% CRITICAL — autonomously fire Maintainerr reclaim, push DOWN
if [ \"\$PCT_INT\" -ge \"\$CRIT_PCT\" ]; then
  HTPW=\$(cat ~/secrets/htpasswd.password)
  MTKEY=\$(cat ~/secrets/maintainerr.key)
  HOST=\$(cat ~/secrets/seedbox.host)
  USERPART=\${HOST%%.*}
  DOMAIN=\${HOST#*.}
  BASE=\"https://maintainerr-\${USERPART}.\${DOMAIN}\"
  BASIC=\$(printf 'quadstronaut:%s' \"\$HTPW\" | base64 -w0)
  IDS=\$(curl -sk -m 10 -H \"X-Api-Key: \$MTKEY\" -H \"Authorization: Basic \$BASIC\" \"\$BASE/api/rules\" \\
    | python3 -c 'import sys,json
try:
    r=json.load(sys.stdin)
    print(\" \".join(str(g[\"id\"]) for g in r if g.get(\"isActive\")))
except Exception: pass')
  RCSTR=\"rules-empty\"
  EX=0
  if [ -n \"\$IDS\" ]; then
    NRULES=\$(echo \"\$IDS\" | wc -w)
    for ID in \$IDS; do
      C=\$(curl -sk -m 30 -o /dev/null -w '%{http_code}' \\
        -H \"X-Api-Key: \$MTKEY\" -H \"Authorization: Basic \$BASIC\" \\
        -H 'Content-Type: application/json' -X POST \\
        --data '{\"id\":'\"\$ID\"'}' \"\$BASE/api/rules/execute\")
      [ \"\$C\" = '200' ] || [ \"\$C\" = '201' ] || EX=\$((EX+1))
    done
    CH=\$(curl -sk -m 30 -o /dev/null -w '%{http_code}' \\
      -H \"X-Api-Key: \$MTKEY\" -H \"Authorization: Basic \$BASIC\" \\
      -X POST \"\$BASE/api/collections/handle\")
    RCSTR=\"rules=\${NRULES}-handle=\${CH}-exec-fail=\${EX}\"
  fi
  echo \"STAGE=quota-critical msg=quota-\${PCT}%-of-\${LIMIT_G}G-FIRED-maintainerr-\${RCSTR}\" >&2
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
