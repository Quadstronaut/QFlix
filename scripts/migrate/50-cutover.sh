#!/usr/bin/env bash
# 50-cutover.sh -- the "migrate me" hot swap, blue -> green (spec section 8).
#
# Eight steps, in order, each confirmed (y/N, or --yes). Stops on the FIRST
# failure and prints `COMPLETED: <steps>` so the operator knows exactly how far
# it got before choosing re-run (I-4) or 55-rollback.sh.
#
#   1 freeze blue     snapshot the ACTIVE qBit hashes + SAB paused flag to
#                     secrets/migrate/freeze-snapshot.json (once; a re-run keeps
#                     the first snapshot), then pause exactly those (freeze.py)
#   2 media delta     30-sync-media.sh --delta --execute
#   3 appdata         35-sync-appdata.sh --execute (green gate disarmed first)
#   4 validate green  40-validate-green.sh must exit 0 (muted + disarmed)
#   5 single pager    I-1: MUTE BLUE first (Kuma human channels detached +
#                     Discord webhook parked), THEN make green loud. Never two
#                     pagers; at worst a few seconds of none.
#   6 gate (I-5)      record blue's drop-in (for 55), DISARM BLUE, then assert
#                     green disarmed. Green is NEVER armed here: arming green is
#                     a later, separate operator act (D-5): --arm-green-gate.
#   7 front door      operator flips DNS / the front proxy upstream (D-6,
#                     QFLX-41), then the HEALTH GATE: 40-validate-green.sh --post
#                     (every manifest app + canary on green, green loud) retried
#                     until green or out of attempts, plus the front-door URL in
#                     secrets/migrate/front-door.url if present. No printed dig
#                     commands: the step passes only on evidence.
#   8 park blue comms blue's newsletter timer + listmonk-sync cron held, THEN
#                     green's released (I-1: one sender, blue keeps sending if the
#                     cutover is abandoned before this step).
#
# I-2 blue writes: the freeze (+55's mirror), the Kuma/webhook mute, the gate
#     drop-in removal, the comms hold. Nothing on blue is deleted.
# I-3 without --execute: the plan, no ssh. I-4: every step is idempotent.
#
# USAGE: 50-cutover.sh NEW_HOST [--old-host HOST] [--execute] [--yes]
#        50-cutover.sh NEW_HOST --arm-green-gate [--execute] [--yes]   (D-5, later)
# EXIT:  0 done | 1 a step failed / operator declined | 2 refused / could-not-assert
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

usage() { echo "usage: $0 NEW_HOST [--old-host HOST] [--execute] [--yes] [--arm-green-gate]" >&2; }
mig_args "--arm-green-gate" "$@"
need_new_host
SIB="${QFLIX_MIGRATE_SIBLINGS:-$MIG_DIR}"
SNAP="$MSTATE/freeze-snapshot.json"
GATE_REC="$MSTATE/blue-gate-dropin.conf"
GATE_WAS_OFF="$MSTATE/blue-gate-was-disarmed"
HEALTH_TRIES="${QFLIX_HEALTH_TRIES:-10}"
HEALTH_SLEEP="${QFLIX_HEALTH_SLEEP:-30}"

print_plan() {
  cat <<EOF
cutover plan, blue=${OLD_HOST:-<secrets/seedbox.ssh-host>} -> green=$NEW_HOST (dry-run: nothing below runs without --execute)

  1. freeze blue      : snapshot active qBit hashes + SAB state -> $SNAP (once), pause exactly those
  2. media delta      : 30-sync-media.sh $NEW_HOST --delta --execute
  3. appdata          : 35-sync-appdata.sh $NEW_HOST --execute
  4. validate green   : 40-validate-green.sh $NEW_HOST (must exit 0: muted, disarmed, all apps up)
  5. single pager     : MUTE blue (Kuma human channels, Discord webhook) THEN green loud (I-1)
  6. gate             : record + DISARM blue's drop-in, assert green disarmed; green stays disarmed (I-5)
  7. front door       : operator flip, then health gate 40-validate-green.sh --post (x$HEALTH_TRIES)
  8. park blue comms  : hold blue's ${COMMS_JOBS[*]}, THEN release green's (I-1)

Later, separately (D-5): 50-cutover.sh $NEW_HOST --arm-green-gate --execute
  refuses unless blue's drop-in is confirmed absent (disarm-blue-then-arm-green).
EOF
}

# --- D-5: arming green, a separate operator act -------------------------------
arm_green_gate() {
  if [ "$EXECUTE" -ne 1 ]; then
    plan "verify blue gate DISARMED ($(gate_state_cmd | cut -c1-40)...), then install green's --execute drop-in"
    echo "[dry-run] no ssh made."; exit 0
  fi
  window_guard
  local b; b="$(sshb "$(gate_state_cmd)" 2>/dev/null | tr -d '\r')"
  [ "$b" = disarmed ] || step_fail 1 gate-order "blue-gate-is-'${b:-unknown}';-I-5-refuses-to-arm-green-until-blue-is-confirmed-disarmed"
  step_done "blue-confirmed-disarmed"
  confirm "ARM GREEN's entitlement gate (install the --execute drop-in on green)"
  sshg "mkdir -p ~/$(dirname "$GATE_DROPIN_REL") && printf '[Service]\nExecStart=\nExecStart=/usr/bin/python3 %%h/scripts/maint/qflix-entitlement.py --execute\n' > ~/$GATE_DROPIN_REL && systemctl --user daemon-reload" \
    || step_fail 1 green-arm "drop-in-install-failed;-NEITHER-side-is-armed"
  step_done "green-drop-in-installed"
  log_warn "The roster is the second switch: set armed: true in green's ~/$MEMBERS_REL by hand when ready."
  exit 0
}

step1() {
  confirm "STEP 1/8 freeze blue (pause the currently-active torrents + SAB)"
  mkdir_state
  if [ ! -s "$SNAP" ]; then
    local s; s="$(on_box b freeze.py snapshot 2>/dev/null | tail -n 1)"
    printf '%s' "$s" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d["qbit"], list)' 2>/dev/null \
      || step_fail 1 freeze-snapshot "could-not-snapshot-blue-torrent-state"
    printf '%s\n' "$s" > "$SNAP.tmp" && mv -f "$SNAP.tmp" "$SNAP"
    log_info "freeze snapshot written: $SNAP"
  else
    log_info "reusing existing freeze snapshot $SNAP (I-4: never re-snapshot a frozen box)"
  fi
  on_box b freeze.py pause "$(cat "$SNAP")" >/dev/null 2>&1 || step_fail 1 freeze-blue "pause-not-verified"
  step_done 1-freeze-blue
}
step2() {
  confirm "STEP 2/8 final media delta"
  bash "$SIB/30-sync-media.sh" "$NEW_HOST" --old-host "$OLD_HOST" --delta --execute || step_fail 1 sync-media-delta "30-sync-media.sh-failed"
  step_done 2-media-delta
}
step3() {
  confirm "STEP 3/8 final appdata sync"
  bash "$SIB/35-sync-appdata.sh" "$NEW_HOST" --old-host "$OLD_HOST" --execute || step_fail 1 sync-appdata "35-sync-appdata.sh-failed"
  step_done 3-appdata
}
step4() {
  log_info "STEP 4/8 validate green (read-only)"
  bash "$SIB/40-validate-green.sh" "$NEW_HOST" --old-host "$OLD_HOST" || step_fail 1 validate-green "40-validate-green.sh-did-not-pass"
  step_done 4-validate-green
}
step5() {
  confirm "STEP 5/8 single pager: mute BLUE, then make GREEN loud (I-1)"
  on_box b kuma_channels.py mute >/dev/null 2>&1 || step_fail 1 mute-blue "blue-kuma-mute-failed"
  sshb "$(pager_cmd mute)" >/dev/null 2>&1 || step_fail 1 mute-blue "blue-webhook-park-failed"
  step_done 5a-blue-muted
  on_box g kuma_channels.py loud >/dev/null 2>&1 || step_fail 1 loud-green "green-kuma-attach-failed;-NO-side-pages-now"
  sshg "$(pager_cmd loud)" >/dev/null 2>&1 || step_fail 1 loud-green "green-webhook-unpark-failed;-NO-side-pages-now"
  step_done 5b-green-loud
}
step6() {
  confirm "STEP 6/8 gate: DISARM blue (green stays disarmed, I-5)"
  mkdir_state
  if [ ! -e "$GATE_REC" ] && [ ! -e "$GATE_WAS_OFF" ]; then
    local st; st="$(sshb "$(gate_state_cmd)" 2>/dev/null | tr -d '\r')"
    case "$st" in
      armed) sshb "cat ~/$GATE_DROPIN_REL" > "$GATE_REC.tmp" && mv -f "$GATE_REC.tmp" "$GATE_REC" \
               || step_fail 1 gate-record "could-not-record-blue-drop-in" ;;
      disarmed) : > "$GATE_WAS_OFF" ;;
      *) step_fail 1 gate-record "blue-gate-state-unreadable" ;;
    esac
  fi
  sshb "$(gate_disarm_cmd)" >/dev/null 2>&1 || step_fail 1 gate-disarm-blue "GATE-STATE-UNKNOWN-check-blue-by-hand"
  step_done 6a-blue-disarmed
  sshg "$(gate_disarm_cmd)" >/dev/null 2>&1 || step_fail 1 gate-green "green-not-confirmed-disarmed"
  step_done 6b-green-confirmed-disarmed
}
step7() {
  confirm "STEP 7/8 FRONT DOOR: flip DNS / the front proxy to green NOW (operator), answer y when done"
  local i
  for i in $(seq 1 "$HEALTH_TRIES"); do
    if bash "$SIB/40-validate-green.sh" "$NEW_HOST" --old-host "$OLD_HOST" --post; then
      if [ -s "$MSTATE/front-door.url" ]; then
        curl -fsS -o /dev/null -m 15 "$(tr -d '[:space:]' < "$MSTATE/front-door.url")" \
          && { step_done 7-front-door-healthy; return 0; }
        log_warn "front door not answering yet (try $i/$HEALTH_TRIES)"
      else
        step_done 7-green-healthy-post-flip; return 0
      fi
    fi
    [ "$i" -lt "$HEALTH_TRIES" ] && sleep "$HEALTH_SLEEP"
  done
  step_fail 1 health-gate "green-not-healthy-after-the-flip-($HEALTH_TRIES-tries);-run-55-rollback.sh"
}
step8() {
  confirm "STEP 8/8 hold blue's comms, then release green's (I-1)"
  sshb "$(comms_cmd hold)" >/dev/null 2>&1 || step_fail 1 park-blue-comms "hold-failed-on-blue"
  step_done 8a-blue-comms-held
  sshg "$(comms_cmd release)" >/dev/null 2>&1 || step_fail 1 release-green-comms "release-failed-on-green;-NO-side-sends-now"
  step_done 8b-green-comms-released
}

if [ -n "${FLAG[arm-green-gate]+x}" ]; then
  resolve_old_host; arm_green_gate
fi
if [ "$EXECUTE" -ne 1 ]; then
  print_plan; exit 0
fi
resolve_old_host
sshb true >/dev/null 2>&1 || stage 2 blue-unreachable "ssh-to-OLD_HOST-failed"
sshg true >/dev/null 2>&1 || stage 2 green-unreachable "ssh-to-NEW_HOST-failed"
window_guard
log_warn "EXECUTING cutover blue -> green ($NEW_HOST)"
step1; step2; step3; step4; step5; step6; step7; step8
log_info "CUTOVER COMPLETE: ${COMPLETED[*]}"
log_info "Blue stays intact for the 14-day hold (60-decommission-old.md). Rollback: 55-rollback.sh $NEW_HOST --execute"
exit 0
