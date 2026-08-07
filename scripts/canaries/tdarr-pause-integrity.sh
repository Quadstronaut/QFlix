#!/usr/bin/env bash
# tdarr-pause-integrity canary: is the fair-use quiet-hours PAUSE actually held?
#
# WHY THIS EXISTS
# ---------------
# tdarr-node is deliberately stopped 18:00-23:00 UTC by tdarr-node-pause.timer
# so transcoding does not compete with the streaming peak. Every surface that
# could notice the pause FAILING is deliberately blinded during exactly that
# window, by design and for good reasons:
#
#   - lib/pusher.py, on suppression.in_pause_window(), pushes "Tdarr Node" UP
#     with "[paused: fair-use quiet hours]" and SKIPS probe + recovery outright.
#     Without that it auto-healed the node ~2min into the pause every night
#     (the false-recovery bug, 2026-06-12).
#   - canaries/tdarr-healthcheck.sh holds its stall threshold ABOVE the 5h pause
#     so a legitimately idle node does not read as wedged.
#
# The combined effect: if tdarr-node-pause.timer is disabled, masked, or dropped
# by a slot rebuild, the node transcodes straight through the streaming peak and
# EVERY surface stays green. The pause half also has no dead-man of its own -
# a stuck-PAUSED node eventually goes red when the window closes and the pusher
# stops suppressing, but a stuck-RUNNING one is invisible forever.
#
# This canary is the missing direction: during the window, assert the node is
# INACTIVE. It is DETECT-ONLY. It deliberately does NOT stop the node itself -
# an auto-heal here would fight the pusher's suppression and the recovery
# breaker, and the operator design law keeps one concern per module.
#
# WHY NOT INVERT THE PREDICATE INSIDE pusher.py
# ---------------------------------------------
# That was the obvious fix and it is the wrong place. pusher.py is the alerting
# HOT PATH for all 35 app monitors; a bug in a new branch there pages falsely on
# every app, every cycle. This concern is separable, so it gets its own module,
# own timer and own Kuma monitor - independently swappable, and a bug here can
# only ever mis-report tdarr-node.
#
# THE WINDOW IS READ, NOT HARDCODED. manifest/apps.yaml's pause_window is the
# single source of truth, shared with lib/suppression.in_pause_window and with
# 50c-tdarr-quiet-hours.sh's OnCalendar. Restating 18/23 here would make a
# fourth policy surface out of a number that has already drifted across three.
# Semantics match manifest.PauseWindow.contains: [start, end), UTC hours,
# wrap-around supported.
#
# FIRST-HOUR GRACE. The pause timer fires at start_hour:00 and the node takes a
# moment to stop; asserting immediately would false-fire at the top of every
# window. The whole first hour is exempt, reported as a named skip rather than
# passed over silently. That costs one hour of coverage out of five and buys a
# canary that never cries wolf on a healthy pause.
#
# EXIT CODES
#   0 - node correctly inactive during the window, or outside the window/grace
#   1 - tdarr-pause-violated: node ACTIVE during the enforced part of the window
#   2 - could not assert: manifest unreadable, no pause_window declared, or the
#       unit state could not be read. Never a silent clean pass - a canary that
#       cannot see the node must not report the pause as held.
#
# Lives on the seedbox at ~/scripts/canaries/tdarr-pause-integrity.sh (deployed
# by 240-maintenance-install.sh). Invoked by
# manitoba-maint-canary-tdarr-pause-integrity, which pushes status=up/down to
# Kuma monitor "Canary Tdarr Pause Integrity".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail

UNIT=${QFLIX_CANARY_TDARR_UNIT:-tdarr-node.service}
APP_KEY=${QFLIX_CANARY_TDARR_APP:-tdarr-node}

# Same candidate list timer-liveness.sh uses, for the same reason: the repo
# checkout is not guaranteed to exist on the box.
MANIFEST=""
for _cand in "$HOME/.opt/maint/apps.yaml" "$HOME/manifest/apps.yaml" "$HOME/scripts/maint/apps.yaml"; do
  [ -f "$_cand" ] && { MANIFEST="$_cand"; break; }
done
if [ -z "$MANIFEST" ]; then
  printf "STAGE=tdarr-pause-manifest-missing msg=no-apps.yaml-on-box\n" >&2
  exit 2
fi

WINDOW=$(python3 - "$MANIFEST" "$APP_KEY" <<PY
import sys, yaml
try:
    d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
    app = (d.get("apps") or {}).get(sys.argv[2]) or {}
    pw = app.get("pause_window")
    if not pw:
        sys.exit(3)
    print(int(pw["start_hour_utc"]), int(pw["end_hour_utc"]))
except SystemExit:
    raise
except Exception:
    sys.exit(4)
PY
)
RC=$?
if [ "$RC" -eq 3 ]; then
  printf "STAGE=tdarr-pause-not-declared msg=no-pause_window-for-%s-in-manifest\n" "$APP_KEY" >&2
  exit 2
fi
if [ "$RC" -ne 0 ] || [ -z "$WINDOW" ]; then
  printf "STAGE=tdarr-pause-manifest-unreadable msg=could-not-parse-pause_window-rc-%s\n" "$RC" >&2
  exit 2
fi
START=$(printf %s "$WINDOW" | cut -d" " -f1)
END=$(printf %s "$WINDOW" | cut -d" " -f2)
# QFLIX_CANARY_TDARR_HOUR injects the UTC hour so the enforced branch can be
# exercised on demand. Without it this canary is only testable for 4 hours a
# day, which in practice means never tested - and an untested alarm is an
# assumption, not a guard.
HOUR=${QFLIX_CANARY_TDARR_HOUR:-$(date -u +%-H)}

# manifest.PauseWindow.contains, transcribed: [start, end), wrap-around aware.
in_window() {
  local h=$1
  [ "$START" -eq "$END" ] && return 1
  if [ "$START" -lt "$END" ]; then
    [ "$h" -ge "$START" ] && [ "$h" -lt "$END" ]
  else
    [ "$h" -ge "$START" ] || [ "$h" -lt "$END" ]
  fi
}

if ! in_window "$HOUR"; then
  printf "PASS: tdarr-pause-integrity - SKIP: %02dh UTC is outside the pause window [%02d,%02d) - a stuck-PAUSED node is already covered by the Tdarr Node monitor once the window closes\n" \
    "$HOUR" "$START" "$END"
  exit 0
fi

if [ "$HOUR" -eq "$START" ]; then
  printf "PASS: tdarr-pause-integrity - SKIP: %02dh UTC is the first hour of the window [%02d,%02d), grace while the pause timer stops the node\n" \
    "$HOUR" "$START" "$END"
  exit 0
fi

STATE=$(systemctl --user is-active "$UNIT" 2>/dev/null)
if [ -z "$STATE" ]; then
  printf "STAGE=tdarr-pause-state-unreadable msg=is-active-returned-nothing-for-%s\n" "$UNIT" >&2
  exit 2
fi

if [ "$STATE" = "active" ]; then
  printf "STAGE=tdarr-pause-violated msg=%s-is-ACTIVE-at-%02dh-UTC-inside-quiet-hours-[%02d,%02d)-pause-timer-not-holding-node-transcoding-through-streaming-peak\n" \
    "$UNIT" "$HOUR" "$START" "$END" >&2
  exit 1
fi

printf "PASS: tdarr-pause-integrity - %s is %s at %02dh UTC, pause window [%02d,%02d) is being held\n" \
  "$UNIT" "$STATE" "$HOUR" "$START" "$END"
exit 0
')
RC=$?
echo "$RES"
exit $RC
