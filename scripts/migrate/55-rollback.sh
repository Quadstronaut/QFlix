#!/usr/bin/env bash
# 55-rollback.sh -- abort an in-progress "migrate me" cutover, return to blue.
#
# Mirror of 50-cutover.sh, reversing only the steps that ARE reversible.
# Media sync (2), appdata sync (3) and the DNS flip (7) are skipped -- nothing
# was pointed at green yet, and a stale copy there is harmless. Mapping:
#   50 step 1 (freeze blue)          <-> here: unfreeze blue (resume qBit+SAB)
#   50 step 8 (park blue newsletter) <-> here: re-enable blue newsletter timers
#   50 step 6 (gate swap)            <-> here: confirm green disarmed, arm blue
#   50 step 5 (green goes loud)      <-> here: mute green (Discord + timers)
#
# Invariants upheld (spec section 3): I-1 (step 4 mutes green before step 3
# re-arms blue) · I-2 (only the freeze/unfreeze pair + the one gate drop-in
# touch blue) · I-3 (inert without --execute) · I-4 (every remote action
# idempotent; re-run after a partial failure) · I-5 (step 3 confirms green
# disarmed BEFORE arming blue, same load-bearing order as 50's swap).
#
# Convention (docs/superpowers/plans/2026-08-08-qflix-migration-plan.md):
# STAGE=<token> msg=<detail> on stderr; exit 0 ok, 1 failure/abort, 2
# could-not-assert. Spec section 6: returns to blue in under a minute --
# API calls and systemd unit edits only, no data sync.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"      # sshm() -- blue, via secrets/seedbox.*
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/log.sh"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/secrets.sh"

NEW_HOST="${1:-}"
EXECUTE=0
for a in "$@"; do [ "$a" = "--execute" ] && EXECUTE=1; done

usage() {
  echo "usage: $0 NEW_HOST [--execute]" >&2
  echo "  NEW_HOST: green's ssh target (user@host or an ssh-config alias)." >&2
  echo "  Without --execute: prints the full step-by-step plan, mutates nothing." >&2
}
if [ -z "$NEW_HOST" ] || [ "$NEW_HOST" = "--execute" ]; then
  usage
  echo "STAGE=usage msg=missing-NEW_HOST" >&2
  exit 2
fi

# green SSH -- mirrors SSHM_OPTS in lib/ssh.sh. Never a hardcoded FQDN:
# NEW_HOST is always the caller's argument.
SSHG_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30)
sshg() { ssh "${SSHG_OPTS[@]}" "$NEW_HOST" "$@"; }

COMPLETED=()
mark_done() { COMPLETED+=("$1"); log_info "[x] step $1 complete"; }

fail() {
  echo "STAGE=$1 msg=$2" >&2
  if [ "${#COMPLETED[@]}" -gt 0 ]; then
    log_error "Completed before failure: ${COMPLETED[*]}"
  else
    log_error "Failed before any step completed -- nothing on either box was touched."
  fi
  exit "${3:-1}"
}

confirm() {
  printf '\n>>> %s\n' "$1" >&2
  printf 'Proceed with this step? [y/N] ' >&2
  read -r ans
  case "$ans" in
    y|Y|yes|YES) return 0 ;;
    *) fail "operator-abort" "declined:$1" 1 ;;
  esac
}

# Same drop-in path 50-cutover.sh uses -- kept byte-identical across both
# files so a gate armed by one is recognized (and removable) by the other.
GATE_UNIT="manitoba-maint-entitlement.service"
GATE_DROPIN_REL=".config/systemd/user/${GATE_UNIT}.d/execute.conf"

# ---------------------------------------------------------------------- 1 --
# Undo 50's step 1. `hashes=all` on resume/start is the only bulk verb the
# WebAPI offers -- it cannot distinguish "paused by the freeze" from
# "the operator had already paused this one," so a torrent individually
# paused before cutover began comes back active too. Same blind spot 50's
# freeze already has in the other direction; accepted for the same reason
# (no per-torrent snapshot is taken, and this is an emergency-abort path).
step1_unfreeze_blue() {
  confirm "STEP 1/4 -- unfreeze blue: resume qBittorrent + SABnzbd (API only)"
  local qu qp qport sk sport out
  qu="$(secret_read qbittorrent.user)"; qp="$(secret_read qbittorrent.password)"
  qport="$(secret_read qbittorrent.port)"
  sk="$(secret_read sabnzbd.key)"; sport="$(secret_read sabnzbd.port)"
  out=$(sshm bash -s "$qu" "$qp" "$qport" "$sk" "$sport" <<'REMOTE'
set -uo pipefail
QU="$1"; QP="$2"; QPORT="$3"; SK="$4"; SPORT="$5"
QURL="http://127.0.0.1:$QPORT"
COOKIE="$(mktemp)"; trap 'rm -f "$COOKIE"' EXIT
curl -sSf -c "$COOKIE" --data-urlencode "username=$QU" --data-urlencode "password=$QP" \
  "$QURL/api/v2/auth/login" | grep -q "Ok." || { echo "qbit-auth-failed"; exit 1; }
# API renamed resume->start at WebUI API v2.11 (qBit 5.0), mirroring 50's
# pause->stop dual-fire. Whichever the running version understands wins.
curl -sS -b "$COOKIE" --data-urlencode "hashes=all" "$QURL/api/v2/torrents/start"  >/dev/null 2>&1
curl -sS -b "$COOKIE" --data-urlencode "hashes=all" "$QURL/api/v2/torrents/resume" >/dev/null 2>&1
sleep 2
# qBit >=5.0 renamed paused* -> stopped*; count either spelling as
# still-stopped so this works across the rename (mirrors 50-cutover.sh).
STILL=$(curl -sS -b "$COOKIE" "$QURL/api/v2/torrents/info" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(sum(1 for t in d if any(k in t.get("state","").lower() for k in ("paused","stopped"))))')
[ "$STILL" = "0" ] || { echo "qbit-still-paused:$STILL"; exit 1; }
echo "qbit-resumed"
curl -sS "http://127.0.0.1:$SPORT/api?mode=resume&apikey=$SK" >/dev/null
sleep 1
PAUSED=$(curl -sS "http://127.0.0.1:$SPORT/api?mode=queue&output=json&apikey=$SK" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin).get("queue",{}).get("paused"))')
[ "$PAUSED" = "False" ] || { echo "sab-still-paused:$PAUSED"; exit 1; }
echo "sab-resumed"
REMOTE
  ) || fail "unfreeze-blue" "qbit-or-sab-resume-failed:$out"
  log_info "$out"
  mark_done "1-unfreeze-blue"
}

# ---------------------------------------------------------------------- 2 --
# Undo 50's step 8. Blue's newsletter/listmonk timers are the first thing
# back on, since blue is (again) the only side that should ever send one.
step2_reenable_blue_newsletter() {
  confirm "STEP 2/4 -- re-enable blue's newsletter/listmonk timers (qflix-newsletter.timer, listmonk-sync.timer)"
  sshm "systemctl --user enable --now qflix-newsletter.timer listmonk-sync.timer" \
    || fail "reenable-blue-newsletter" "enable-failed-on-blue"
  mark_done "2-reenable-blue-newsletter"
}

# ---------------------------------------------------------------------- 3 --
# Undo 50's step 6, sides swapped. I-5 ordering: green must be CONFIRMED
# disarmed before blue's drop-in goes back in, never the reverse -- so this
# actively removes green's drop-in (idempotent no-op if 50 never got there)
# and reads the file back before touching blue at all.
step3_gate_swap_back() {
  confirm "STEP 3a/4 -- confirm GREEN gate disarmed (remove --execute drop-in if present)"
  sshg "rm -f ~/$GATE_DROPIN_REL && systemctl --user daemon-reload" \
    || fail "gate-swap-back" "green-disarm-failed-GATE-STATE-UNKNOWN-check-both-sides-by-hand"
  if sshg "test -f ~/$GATE_DROPIN_REL" 2>/dev/null; then
    fail "gate-swap-back" "green-still-armed-after-rm-refusing-to-arm-blue" 1
  fi
  mark_done "3a-confirm-green-disarmed"

  confirm "STEP 3b/4 -- re-arm BLUE gate (install --execute drop-in; green confirmed disarmed above)"
  sshm "mkdir -p ~/$(dirname "$GATE_DROPIN_REL") && cat > ~/$GATE_DROPIN_REL <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/python3 %h/scripts/maint/qflix-entitlement.py --execute
EOF
systemctl --user daemon-reload" \
    || fail "gate-swap-back" "blue-arm-failed-NEITHER-SIDE-IS-ARMED-arm-one-by-hand-now"
  mark_done "3b-arm-blue-gate"
}

# ---------------------------------------------------------------------- 4 --
# Undo 50's step 5. bootstrap-kuma-monitors.py only ever ATTACHES
# notifications (it has no detach mode), so the Discord-specific removal is
# done here directly against green's Kuma over its local socket.io API --
# the same `uptime-kuma-api` client bootstrap-kuma-monitors.py uses, run
# on-box exactly the way 50 already runs that script (sshg python3, default
# KUMA_URL=http://127.0.0.1:42005, which is loopback-valid ON green itself).
# Only the Discord notification IDs are stripped; the auto-heal webhook
# stays attached so green's own recovery loop keeps working while parked.
step4_mute_green() {
  confirm "STEP 4/4 -- mute green: detach Kuma Discord channels + disable green's newsletter/listmonk timers"
  # Green's Kuma port is slot-specific, not the blue-hardcoded 42005 -- read
  # it off green itself over the existing green-ssh helper.
  local kuma_port
  kuma_port="$(sshg 'tr -d "[:space:]" < ~/secrets/uptimekuma.port' 2>/dev/null)"
  [ -n "$kuma_port" ] || fail "mute-green" "green-uptimekuma-port-unresolved"
  sshg python3 - <<PY \
    || fail "mute-green" "kuma-discord-detach-failed-on-green"
import sys
from pathlib import Path
try:
    from uptime_kuma_api import UptimeKumaApi
except ImportError:
    print("uptime-kuma-api-missing", file=sys.stderr)
    sys.exit(2)

pw = None
for name in ("htpasswd.password", "shared-admin.password"):
    p = Path.home() / "secrets" / name
    if p.is_file():
        pw = p.read_text().strip()
        break
if not pw:
    print("no-candidate-password", file=sys.stderr)
    sys.exit(2)

# Construction now lives INSIDE the try/except too: a connection failure
# (bad port, Kuma not up) must land in the except path, not blow up
# unhandled before login is even attempted.
api = None
try:
    api = UptimeKumaApi("http://127.0.0.1:$kuma_port")
    api.login("quadstronaut", pw)
    discord_ids = {int(n["id"]) for n in api.get_notifications()
                   if str(n.get("type", "")).lower() == "discord"}
    if not discord_ids:
        print("no-discord-notification-configured-nothing-to-detach")
    changed = 0
    for m in api.get_monitors():
        cur = m.get("notificationIDList") or {}
        keep = {k: v for k, v in cur.items() if int(k) not in discord_ids}
        if keep != cur:
            api.edit_monitor(m["id"], notificationIDList=keep)
            changed += 1
    print(f"detached-discord-from={changed}-monitors")
except Exception as e:
    print(f"kuma-mute-failed:{e}", file=sys.stderr)
    sys.exit(1)
finally:
    if api is not None:
        api.disconnect()
PY
  sshg "systemctl --user disable --now qflix-newsletter.timer listmonk-sync.timer" \
    || fail "mute-green" "green-timer-disable-failed"
  mark_done "4-mute-green"
}

print_plan() {
  cat <<EOF
rollback plan for NEW_HOST=$NEW_HOST (dry-run -- nothing below runs without --execute)

  1. Unfreeze blue        : resume qBittorrent + SABnzbd via their APIs
  2. Re-enable blue comms : enable --now qflix-newsletter.timer listmonk-sync.timer (blue)
  3. Gate swap back       : confirm green's entitlement-gate drop-in is gone,
                             THEN reinstall blue's (never both armed -- I-5)
  4. Mute green           : detach Kuma Discord channels + disable green's
                             newsletter/listmonk timers

Every mutating step above also asks for an explicit y/N once run with
--execute. DNS is never touched by this script -- revert it by hand if you
had already flipped it. Re-run with --execute to perform the rollback.
EOF
}

main() {
  if [ "$EXECUTE" -ne 1 ]; then
    print_plan
    exit 0
  fi
  log_warn "EXECUTING rollback: green -> blue ($NEW_HOST). Ctrl-C at any confirm to stop cleanly."
  step1_unfreeze_blue
  step2_reenable_blue_newsletter
  step3_gate_swap_back
  step4_mute_green
  log_info "ROLLBACK COMPLETE: ${COMPLETED[*]}"
  log_info "Blue is fully live and the only side armed/loud again. Green is parked; re-run 50-cutover.sh when ready to retry."
}

main
