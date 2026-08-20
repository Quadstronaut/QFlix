#!/usr/bin/env bash
# tdarr-throttle-integrity canary: is the fair-use WORKER CAP actually held?
#
# WHY THIS EXISTS
# ---------------
# Until 2026-08-20 this file was tdarr-pause-integrity.sh and asserted the
# opposite of a liveness check: during the 18:00-23:00 UTC fair-use pause,
# tdarr-node must be INACTIVE. That pause is retired — the node runs 24/7 and
# fair-use moved from the clock to the worker cap.
#
# The predicate changed; the CONCERN did not. Fair-use on a SHARED seedbox slot
# is now one number, and that number has exactly the shape of blind spot the
# pause had: nothing else on the box can see it drift.
#
#   * Tdarr stores worker limits in TWO layers. SettingsGlobalJSONDB is the
#     server-wide default; NodeJSONDB.workerLimits is the per-node override,
#     and only the second one gates work. The global seeds the node record ONCE
#     at first registration and is never re-read.
#   * Proven drift, 2026-08-07: the global was set to 1/1 and the node went on
#     running FOUR workers at ~94% CPU. The global edit persisted cleanly, so
#     every check short of counting live workers agreed the change had taken.
#   * The server REWRITES the node record when the node reconnects, so a cap
#     applied to a running server is silently clobbered on the next connect —
#     which is to say the drift direction is toward MORE workers, not fewer.
#
# A throttle nobody audits is a throttle that quietly stops being one. On a
# shared slot that is the failure that gets the account noticed.
#
# WHAT THIS DOES **NOT** DO
# -------------------------
# It does not assert the node is alive. With pause_window gone, lib/pusher.py
# probes and auto-heals tdarr-node every cycle, so "Tdarr Node" already owns
# 24/7 liveness — and that monitor is the one thing the pause's retirement gave
# us for free. Duplicating it here would be a second surface for one fact.
#
# It is also DETECT-ONLY. Rewriting the limits here would fight
# 50b-tdarr-config.py (which must stop the server to write the node record
# safely), and the operator design law keeps one concern per module.
#
# THE CAP IS READ, NOT HARDCODED. manifest/apps.yaml's tdarr-node.throttle is
# the single source of truth, shared with 50b's NODE_WORKER_LIMITS. Restating
# "2" here would make a third policy surface out of the number this canary
# exists to protect — the identical mistake the pause window's hours made
# across four files before they were centralised.
#
# EXIT CODES
#   0 - live per-node worker limits match the manifest cap
#   1 - tdarr-throttle-exceeded: node is running MORE workers than declared
#   1 - tdarr-throttle-mismatch: limits differ from the manifest in any other
#       direction (fewer than declared is also drift — it means 50b did not
#       take, and the backlog silently stops converging)
#   2 - could not assert: manifest unreadable, no throttle declared, server
#       unreachable, or the node is not registered. Never a silent clean pass.
#
# A node that is DOWN exits 2, not 0 and not 1: this canary cannot see a cap on
# a node that is not there, and "Tdarr Node" is the monitor that owns that fact.
#
# Lives on the seedbox at ~/scripts/canaries/tdarr-throttle-integrity.sh
# (deployed by 240-maintenance-install.sh). Invoked by
# manitoba-maint-canary-tdarr-throttle-integrity, which pushes status=up/down
# to Kuma monitor "Canary Tdarr Throttle Integrity".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail

APP_KEY=${QFLIX_CANARY_TDARR_APP:-tdarr-node}

# Same candidate list timer-liveness.sh uses, for the same reason: the repo
# checkout is not guaranteed to exist on the box.
MANIFEST=""
for _cand in "$HOME/.opt/maint/apps.yaml" "$HOME/manifest/apps.yaml" "$HOME/scripts/maint/apps.yaml"; do
  [ -f "$_cand" ] && { MANIFEST="$_cand"; break; }
done
if [ -z "$MANIFEST" ]; then
  printf "STAGE=tdarr-throttle-manifest-missing msg=no-apps.yaml-on-box\n" >&2
  exit 2
fi

CAP=$(python3 - "$MANIFEST" "$APP_KEY" <<PY
import sys, yaml
try:
    d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
    app = (d.get("apps") or {}).get(sys.argv[2]) or {}
    t = app.get("throttle")
    if not t:
        sys.exit(3)
    print(int(t["transcode_workers"]), int(t["health_check_workers"]))
except SystemExit:
    raise
except Exception:
    sys.exit(4)
PY
)
RC=$?
if [ "$RC" -eq 3 ]; then
  printf "STAGE=tdarr-throttle-not-declared msg=no-throttle-for-%s-in-manifest\n" "$APP_KEY" >&2
  exit 2
fi
if [ "$RC" -ne 0 ] || [ -z "$CAP" ]; then
  printf "STAGE=tdarr-throttle-manifest-unreadable msg=could-not-parse-throttle-rc-%s\n" "$RC" >&2
  exit 2
fi
WANT_T=$(printf %s "$CAP" | cut -d" " -f1)
WANT_H=$(printf %s "$CAP" | cut -d" " -f2)

CONF="$HOME/.apps/tdarr/configs/Tdarr_Server_Config.json"
PORT=$(grep -oP "\"serverPort\":\s*\"?\K[0-9]+" "$CONF" 2>/dev/null | head -1)
PORT=${PORT:-42018}

NODES=$(curl -sfm 10 "http://127.0.0.1:${PORT}/api/v2/get-nodes" 2>/dev/null)
if [ -z "$NODES" ]; then
  printf "STAGE=tdarr-throttle-server-unreachable msg=get-nodes-empty-on-port-%s-cannot-assert-cap\n" "$PORT" >&2
  exit 2
fi

# Emit one "name transcodecpu healthcheckcpu transcodegpu healthcheckgpu" line
# per registered node. Absent keys print as -1 so they can never silently read
# as a compliant 0.
LIVE=$(printf %s "$NODES" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if not isinstance(d, dict) or not d:
    sys.exit(5)
for k, v in d.items():
    wl = (v or {}).get(\"workerLimits\") or {}
    print(v.get(\"nodeName\", k),
          wl.get(\"transcodecpu\", -1), wl.get(\"healthcheckcpu\", -1),
          wl.get(\"transcodegpu\", -1), wl.get(\"healthcheckgpu\", -1))
" 2>/dev/null)
RC=$?
if [ "$RC" -ne 0 ] || [ -z "$LIVE" ]; then
  printf "STAGE=tdarr-throttle-no-nodes msg=server-up-but-zero-registered-nodes-cannot-assert-cap\n" >&2
  exit 2
fi

VIOLATION=""
EXCEEDED=0
REPORT=""
while read -r NAME TCPU HCPU TGPU HGPU; do
  [ -n "$NAME" ] || continue
  REPORT="$REPORT $NAME=${TCPU}t/${HCPU}h"
  if [ "$TCPU" -gt "$WANT_T" ] || [ "$HCPU" -gt "$WANT_H" ]; then
    EXCEEDED=1
    VIOLATION="$VIOLATION $NAME(${TCPU}t/${HCPU}h)"
  elif [ "$TCPU" -ne "$WANT_T" ] || [ "$HCPU" -ne "$WANT_H" ]; then
    VIOLATION="$VIOLATION $NAME(${TCPU}t/${HCPU}h)"
  fi
  # GPU workers are capped at 0 on this slot; anything else is drift too.
  if [ "$TGPU" -gt 0 ] || [ "$HGPU" -gt 0 ]; then
    EXCEEDED=1
    VIOLATION="$VIOLATION $NAME(gpu ${TGPU}t/${HGPU}h)"
  fi
done <<EOF
$LIVE
EOF

if [ -n "$VIOLATION" ]; then
  if [ "$EXCEEDED" -eq 1 ]; then
    printf "STAGE=tdarr-throttle-exceeded msg=node-running-MORE-workers-than-manifest-cap-%st/%sh-live:%s-shared-slot-fair-use-breached\n" \
      "$WANT_T" "$WANT_H" "$(printf %s "$VIOLATION" | tr " " ",")" >&2
  else
    printf "STAGE=tdarr-throttle-mismatch msg=node-worker-limits-differ-from-manifest-cap-%st/%sh-live:%s-50b-did-not-take-backlog-may-stall\n" \
      "$WANT_T" "$WANT_H" "$(printf %s "$VIOLATION" | tr " " ",")" >&2
  fi
  exit 1
fi

printf "PASS: tdarr-throttle-integrity - worker cap %st/%sh held on all registered nodes:%s\n" \
  "$WANT_T" "$WANT_H" "$REPORT"
exit 0
')
RC=$?
echo "$RES"
exit $RC
