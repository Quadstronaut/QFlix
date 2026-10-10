#!/usr/bin/env bash
# 55-rollback.sh -- abort the cutover and return to blue in under a minute
# (spec section 8 row 55: "Under 1 min. Mute green before anything is
# re-enabled on blue"). API calls and unit/secret toggles only, no data sync.
#
# ORDER IS THE POINT (each step idempotent; safe whatever 50 reached):
#   1 mute green        Kuma human channels detached, Discord webhook parked,
#                       comms jobs held. FIRST, so there is never a moment with
#                       two pagers/senders (I-1).
#   2 green disarmed    remove green's --execute drop-in and READ BACK that it is
#                       gone. Blue's gate is re-armed only after this (I-5).
#   3 blue loud         Kuma human channels re-attached, webhook un-parked.
#   4 unfreeze blue     resume EXACTLY the torrents the freeze snapshot recorded
#                       as active, and SAB only if it was running then. No
#                       snapshot = refusal, never `hashes=all` (that would also
#                       resume torrents the operator had paused on purpose).
#   5 blue comms        release blue's newsletter timer + listmonk-sync cron.
#   6 re-arm blue gate  ONLY if 50 recorded blue as armed (the recorded drop-in
#                       is restored byte-for-byte); recorded-disarmed stays off.
#   The front door is not automated: if 50 step 7 flipped it, flip it back.
#
# I-2: every blue write here is the mirror of a 50 write. I-3: plan only
# without --execute. Stops on the first failure, printing COMPLETED steps.
#
# USAGE: 55-rollback.sh NEW_HOST [--old-host HOST] [--execute] [--yes]
# EXIT:  0 blue live again | 1 a step failed | 2 refused / could-not-assert
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

usage() { echo "usage: $0 NEW_HOST [--old-host HOST] [--execute] [--yes]" >&2; }
mig_args "" "$@"
need_new_host
SNAP="$MSTATE/freeze-snapshot.json"
GATE_REC="$MSTATE/blue-gate-dropin.conf"
GATE_WAS_OFF="$MSTATE/blue-gate-was-disarmed"

if [ "$EXECUTE" -ne 1 ]; then
  cat <<EOF
rollback plan, green=$NEW_HOST -> blue (dry-run: nothing below runs without --execute)

  1. mute green       : Kuma human channels off, Discord webhook parked, comms held (FIRST, I-1)
  2. green disarmed   : remove green's gate drop-in, read back absent (I-5)
  3. blue loud        : Kuma human channels on, Discord webhook un-parked
  4. unfreeze blue    : resume exactly the hashes in $SNAP (refuses without it)
  5. blue comms       : release ${COMMS_JOBS[*]} on blue
  6. re-arm blue gate : only if 50 recorded blue armed ($GATE_REC)
  front door          : manual -- flip it back if 50 step 7 flipped it
EOF
  exit 0
fi

resolve_old_host
sshb true >/dev/null 2>&1 || stage 2 blue-unreachable "ssh-to-OLD_HOST-failed"
window_guard
log_warn "EXECUTING rollback green -> blue"

confirm "STEP 1/6 mute green (Kuma, webhook, comms)"
GREEN_UP=1; sshg true >/dev/null 2>&1 || GREEN_UP=0
if [ "$GREEN_UP" -eq 1 ]; then
  on_box g kuma_channels.py mute >/dev/null 2>&1; KRC=$?
  [ "$KRC" -eq 0 ] || [ "$KRC" -eq 2 ] || step_fail 1 mute-green "green-kuma-mute-failed"
  sshg "$(pager_cmd mute)" >/dev/null 2>&1 || step_fail 1 mute-green "green-webhook-park-failed"
  sshg "$(comms_cmd hold)" >/dev/null 2>&1 || step_fail 1 mute-green "green-comms-hold-failed"
  step_done 1-green-muted
else
  # Unreachable green cannot page through ITS webhook either way, but we cannot
  # prove its gate is disarmed, so step 6 will refuse to re-arm blue.
  log_warn "green unreachable: cannot mute it; continuing to restore blue (gate stays as-is)"
  step_done 1-green-unreachable-skipped
fi

confirm "STEP 2/6 confirm green's gate is disarmed"
GREEN_DISARMED=0
if [ "$GREEN_UP" -eq 1 ]; then
  sshg "$(gate_disarm_cmd)" >/dev/null 2>&1 || step_fail 1 green-disarm "GATE-STATE-UNKNOWN-on-green"
  [ "$(sshg "$(gate_state_cmd)" 2>/dev/null | tr -d '\r')" = disarmed ] \
    || step_fail 1 green-disarm "green-still-armed-after-rm;-refusing-to-continue"
  GREEN_DISARMED=1
  step_done 2-green-confirmed-disarmed
else
  step_done 2-green-gate-unverifiable
fi

confirm "STEP 3/6 make blue loud again"
on_box b kuma_channels.py loud >/dev/null 2>&1 || step_fail 1 blue-loud "blue-kuma-attach-failed"
sshb "$(pager_cmd loud)" >/dev/null 2>&1 || step_fail 1 blue-loud "blue-webhook-unpark-failed"
step_done 3-blue-loud

confirm "STEP 4/6 unfreeze blue (snapshot hashes only)"
if [ -s "$SNAP" ]; then
  on_box b freeze.py resume "$(cat "$SNAP")" >/dev/null 2>&1 || step_fail 1 unfreeze-blue "resume-not-verified"
  mv -f "$SNAP" "$SNAP.used-$(date -u +%Y%m%dT%H%M%SZ)"
  step_done 4-blue-unfrozen
else
  step_fail 2 freeze-snapshot-missing "no-$SNAP;-refusing-hashes=all-(resume-by-hand-in-the-qBit-UI)"
fi

confirm "STEP 5/6 release blue's comms jobs"
sshb "$(comms_cmd release)" >/dev/null 2>&1 || step_fail 1 blue-comms "release-failed-on-blue"
step_done 5-blue-comms-released

confirm "STEP 6/6 re-arm blue's gate (only if it was armed at cutover)"
if [ -s "$GATE_REC" ]; then
  [ "$GREEN_DISARMED" -eq 1 ] || step_fail 1 gate-order "green-not-confirmed-disarmed;-I-5-refuses-to-re-arm-blue"
  sshb "mkdir -p ~/$(dirname "$GATE_DROPIN_REL") && cat > ~/$GATE_DROPIN_REL && systemctl --user daemon-reload" < "$GATE_REC" \
    || step_fail 1 blue-rearm "drop-in-restore-failed;-NEITHER-side-is-armed"
  mv -f "$GATE_REC" "$GATE_REC.used-$(date -u +%Y%m%dT%H%M%SZ)"
  step_done 6-blue-gate-rearmed
elif [ -e "$GATE_WAS_OFF" ]; then
  rm -f "$GATE_WAS_OFF"
  step_done 6-blue-gate-was-disarmed-left-off
else
  log_warn "no record of blue's gate state from 50 step 6: leaving blue's gate as it is now"
  step_done 6-blue-gate-unchanged
fi
log_info "ROLLBACK COMPLETE: ${COMPLETED[*]}"
log_info "If 50 step 7 flipped the front door, flip it back to blue now."
exit 0
