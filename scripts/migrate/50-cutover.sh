#!/usr/bin/env bash
# 50-cutover.sh -- the "migrate me" orchestrator.
#
# Runs the eight named steps in docs/superpowers/specs/2026-08-08-qflix-
# migration-blue-green-design.md section 4 (row 50), in order, on migration
# day. Stops on the FIRST failure and prints which steps already completed,
# so the operator knows how far cutover got before reaching for 55-rollback.sh.
#
# Invariants upheld here (spec section 3): I-1 only one side pages Discord/
# newsletter/Seerr at a time (step 5/8) -- I-2 blue stays read-only except the
# freeze (step 1) and the one gate write spec section 5 explicitly carves out
# for the swap (step 6a) -- I-3 every mutating action is inert without
# --execute -- I-4 idempotent/resumable, re-running a failed step is the fix
# -- I-5 the entitlement gate is armed on at most one side, ever (step 6).
#
# Convention (docs/superpowers/plans/2026-08-08-qflix-migration-plan.md):
# STAGE=<token> msg=<detail> on stderr for every failure; exit 0 ok, 1
# finding/failure/operator-abort, 2 could-not-assert (bad usage, a sibling
# script missing).
#
# Without --execute: prints the full ordered plan and exits 0, touching
# NOTHING on either box (I-3). With --execute: every mutating step still asks
# for an explicit y/N before acting, on top of the --execute gate.
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

# Stop-on-first-failure reporter. Always names what already succeeded so the
# operator's next move (rerun vs. 55-rollback.sh) is an informed one.
fail() {
  echo "STAGE=$1 msg=$2" >&2
  if [ "${#COMPLETED[@]}" -gt 0 ]; then
    log_error "Completed before failure: ${COMPLETED[*]}"
  else
    log_error "Failed before any step completed -- nothing on either box was touched."
  fi
  exit "${3:-1}"
}

# Per-step operator gate. Declining is a clean stop, not a crash.
confirm() {
  printf '\n>>> %s\n' "$1" >&2
  printf 'Proceed with this step? [y/N] ' >&2
  read -r ans
  case "$ans" in
    y|Y|yes|YES) return 0 ;;
    *) fail "operator-abort" "declined:$1" 1 ;;
  esac
}

need_script() { [ -x "$1" ] || fail "script-missing" "not-executable:$1" 2; }

# Step 1: pause qBit + SAB via their WebAPIs. This IS the *arr import freeze
# -- nothing new finishes downloading, so nothing is left to import. *arr
# services are never stopped (I-2). Idempotent + reversible (55-rollback.sh).
step1_freeze_blue() {
  confirm "STEP 1/8 -- freeze blue: pause qBittorrent + SABnzbd (API only, *arr services untouched)"
  local qu qp qport sk sport out
  qu="$(secret_read qbittorrent.user)"; qp="$(secret_read qbittorrent.password)"
  qport="$(secret_read qbittorrent.port)"
  sk="$(secret_read sabnzbd.key)"; sport="$(secret_read sabnzbd.port)"
  out=$(sshm bash -s "$qu" "$qp" "$qport" "$sk" "$sport" <<'REMOTE'
set -uo pipefail
QU="$1"; QP="$2"; QPORT="$3"; SK="$4"; SPORT="$5"
QURL="http://127.0.0.1:$QPORT"
COOKIE="$(mktemp)"; trap 'rm -f "$COOKIE"' EXIT
# Secrets never touch curl's own argv here (readable via /proc/<pid>/cmdline
# on a shared box): creds/apikey are fed to curl via `-K -`, a config read
# from stdin, instead of as --data-urlencode/URL command-line arguments.
# data-urlencode preserves the exact encoding --data-urlencode gave the login
# call before; the SAB calls keep their prior raw (unencoded) apikey= form.
printf 'url = "%s/api/v2/auth/login"\ndata-urlencode = "username=%s"\ndata-urlencode = "password=%s"\n' \
  "$QURL" "$QU" "$QP" | curl -sSf -c "$COOKIE" -K - | grep -q "Ok." || { echo "qbit-auth-failed"; exit 1; }
# API renamed pause->stop at WebUI API v2.11 (qBit 5.0). Fire both; whichever
# the running version understands wins, the other is a harmless 404/no-op.
curl -sS -b "$COOKIE" --data-urlencode "hashes=all" "$QURL/api/v2/torrents/stop"  >/dev/null 2>&1
curl -sS -b "$COOKIE" --data-urlencode "hashes=all" "$QURL/api/v2/torrents/pause" >/dev/null 2>&1
sleep 2
# qBit >=5.0 renamed paused* -> stopped*; count anything not carrying either
# spelling as still-active so this works across the rename.
STILL=$(curl -sS -b "$COOKIE" "$QURL/api/v2/torrents/info" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(sum(1 for t in d if not any(k in t.get("state","").lower() for k in ("paused","stopped"))))')
[ "$STILL" = "0" ] || { echo "qbit-still-active:$STILL"; exit 1; }
echo "qbit-paused"
printf 'url = "http://127.0.0.1:%s/api?mode=pause"\ndata = "apikey=%s"\n' "$SPORT" "$SK" \
  | curl -sS -K - >/dev/null
sleep 1
PAUSED=$(printf 'url = "http://127.0.0.1:%s/api?mode=queue&output=json"\ndata = "apikey=%s"\n' "$SPORT" "$SK" \
  | curl -sS -K - | python3 -c 'import json,sys;print(json.load(sys.stdin).get("queue",{}).get("paused"))')
[ "$PAUSED" = "True" ] || { echo "sab-not-paused:$PAUSED"; exit 1; }
echo "sab-paused"
REMOTE
  ) || fail "freeze-blue" "qbit-or-sab-pause-failed:$out"
  log_info "$out"
  mark_done "1-freeze-blue"
}

step2_sync_media() {
  local sc="$HERE/30-sync-media.sh"; need_script "$sc"
  confirm "STEP 2/8 -- delta media sync (blue -> green, the small cutover-day pass)"
  "$sc" "$NEW_HOST" --delta --execute || fail "sync-media-delta" "30-sync-media.sh-failed"
  mark_done "2-sync-media-delta"
}

step3_sync_appdata() {
  local sc="$HERE/35-sync-appdata.sh"; need_script "$sc"
  confirm "STEP 3/8 -- appdata sync (Seerr/Tautulli/Listmonk/*arr backups, roster+gate state)"
  "$sc" "$NEW_HOST" --execute || fail "sync-appdata" "35-sync-appdata.sh-failed"
  mark_done "3-sync-appdata"
}

# Step 4: read-only smoke on green, no confirm needed. Non-zero here (FAIL=1
# or could-not-assert=2) means NO CUTOVER -- blue stays frozen but never
# degraded (spec section 6); run 55-rollback.sh next.
step4_validate_green() {
  local sc="$HERE/40-validate-green.sh"; need_script "$sc"
  log_info "STEP 4/8 -- validate-green (must exit 0; no cutover otherwise)"
  "$sc" "$NEW_HOST" || fail "validate-green" "40-validate-green.sh-did-not-PASS-run-55-rollback.sh"
  mark_done "4-validate-green"
}

# Step 5, I-1's other half: green goes LOUD. Re-running bootstrap-kuma-
# monitors.py reuses the repo's existing idempotent tool for attaching the
# standard notification set (Discord + auto-heal webhook) to every monitor,
# instead of re-implementing Kuma's socket.io API here.
step5_attach_green() {
  confirm "STEP 5/8 -- attach green Kuma Discord channel + enable newsletter/listmonk timers"
  sshg python3 '~/scripts/maint/bootstrap-kuma-monitors.py' \
    || fail "attach-green-comms" "kuma-notification-attach-failed-on-green"
  sshg 'systemctl --user daemon-reload && systemctl --user enable --now qflix-newsletter.timer listmonk-sync.timer' \
    || fail "attach-green-comms" "green-timer-enable-failed"
  mark_done "5-attach-green-comms"
}

# Step 6, I-5: the gate is armed on at most one side, ever. Order is load-bearing --
# disarm blue BEFORE arming green (spec section 5) so there is never a window
# with two live gates. Same drop-in ritual as the reaper/anime-janitor: base
# unit ships blank ExecStart (safe), a .service.d/execute.conf overrides it
# with --execute. This is the ONE blue write beyond the freeze spec section 5
# explicitly authorizes, and it is fully reversible.
GATE_UNIT="manitoba-maint-entitlement.service"
GATE_DROPIN_REL=".config/systemd/user/${GATE_UNIT}.d/execute.conf"

step6_gate_swap() {
  confirm "STEP 6a/8 -- disarm BLUE gate (remove --execute drop-in, blue keeps its base dry-run unit)"
  sshm "rm -f ~/$GATE_DROPIN_REL && systemctl --user daemon-reload" \
    || fail "gate-swap" "blue-disarm-failed-GATE-STATE-UNKNOWN-check-both-sides-by-hand"
  mark_done "6a-disarm-blue-gate"

  confirm "STEP 6b/8 -- arm GREEN gate (install --execute drop-in; blue is disarmed, so exactly one side is armed after this)"
  sshg "mkdir -p ~/$(dirname "$GATE_DROPIN_REL") && cat > ~/$GATE_DROPIN_REL <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/python3 %h/scripts/maint/qflix-entitlement.py --execute
EOF
systemctl --user daemon-reload" \
    || fail "gate-swap" "green-arm-failed-NEITHER-SIDE-IS-ARMED-arm-one-by-hand-now"
  mark_done "6b-arm-green-gate"
}

# Step 7: pure printout, nothing mutates, so this always runs once we get here.
step7_dns_instructions() {
  local host="<your public FQDN>" ip=""
  secret_exists seedbox.host && host="$(secret_read seedbox.host)"
  ip="$(sshg "curl -s --max-time 5 https://ifconfig.me" 2>/dev/null || true)"
  [ -n "$ip" ] || ip="<look up green's public IP -- ifconfig.me was unreachable from here>"
  cat <<EOF

============================================================
STEP 7/8 -- DNS FLIP (manual; spec section 7: no automated DNS)
============================================================
1. At your DNS provider, point the A/AAAA record for:
       $host
   to green's address:
       $ip
   (keep the existing TTL -- it is what bounds the member-visible gap)
2. Wait for propagation. A stale answer right after flipping is normal,
   not a failure (spec section 6).
3. Post-flip verification one-liners (run from your workstation):
       dig +short $host
       curl -sSI https://$host/identity     # Plex on green, expect 200
       curl -sSI https://$host/healthz      # qflix-dash on green, expect 200
4. Blue stays up read-only through the TTL window. Do not decommission it
   yet -- that is scripts/migrate/60-decommission-old.md, an operator
   checklist, never a script.
============================================================
EOF
  mark_done "7-dns-instructions-printed"
}

# Step 8: blue's newsletter/listmonk timers are the LAST thing parked, so an
# abandoned cutover (failure in 1-7) leaves blue still able to send on its
# own. Reversible: `systemctl --user enable --now` the same units on blue.
step8_park_blue_newsletter() {
  confirm "STEP 8/8 -- park blue's newsletter/listmonk timers (qflix-newsletter.timer, listmonk-sync.timer)"
  sshm "systemctl --user disable --now qflix-newsletter.timer listmonk-sync.timer" \
    || fail "park-blue-newsletter" "disable-failed-on-blue"
  mark_done "8-park-blue-newsletter"
}

print_plan() {
  cat <<EOF
migrate-me plan for NEW_HOST=$NEW_HOST (dry-run -- nothing below runs without --execute)

  1. Freeze blue        : pause qBittorrent + SABnzbd via their APIs (this IS
                           the *arr import freeze; *arr services untouched)
  2. Sync media (delta)  : 30-sync-media.sh $NEW_HOST --delta --execute
  3. Sync appdata        : 35-sync-appdata.sh $NEW_HOST --execute
  4. Validate green      : 40-validate-green.sh $NEW_HOST (must exit 0)
  5. Green goes loud     : attach Kuma Discord channel + enable
                           qflix-newsletter.timer / listmonk-sync.timer on green
  6. Gate swap           : disarm blue's entitlement-gate drop-in, THEN arm
                           green's (never both armed -- I-5)
  7. DNS flip            : print the record change + verification commands
  8. Park blue           : disable blue's newsletter/listmonk timers

Every mutating step above also asks for an explicit y/N once run with
--execute. Re-run with --execute to perform the cutover for real.
EOF
}

main() {
  if [ "$EXECUTE" -ne 1 ]; then
    print_plan
    exit 0
  fi
  log_warn "EXECUTING migrate-me cutover: blue -> green ($NEW_HOST). Ctrl-C at any confirm to stop cleanly."
  step1_freeze_blue
  step2_sync_media
  step3_sync_appdata
  step4_validate_green
  step5_attach_green
  step6_gate_swap
  step7_dns_instructions
  step8_park_blue_newsletter
  log_info "CUTOVER SEQUENCE COMPLETE: ${COMPLETED[*]}"
  log_info "Green is now canonical pending DNS propagation. Blue stays read-only-serving until you retire it (60-decommission-old.md)."
}

main
