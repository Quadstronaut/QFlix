#!/usr/bin/env bash
# 30-sync-media.sh -- bulk media sync, blue -> green (spec section 4, script 30).
#
# Direction: this runs FROM wherever you invoke it, but the actual rsync is
# INITIATED ON BLUE via sshm (blue has outbound SSH; green does not need to
# reach blue at all). Two source trees, unchanged filenames, no --delete:
#   ~/media/                      the 2.2T library (TV/Movies/Anime/...)
#   ~/www/images/newsletter/      qflix-newsletter's poster cache
#
# Multi-pass by design (spec section 5): run this script as many times as you
# like while blue stays live -- rsync -aH --partial is incremental, so every
# rerun only moves what changed since the last pass (I-4: idempotent,
# resumable). The LAST pass, run with --delta during the cutover freeze, is
# expected to be tiny -- it is the exact same command, not a different mode.
#
# Invariants held here:
#   I-2  blue is read-only. This script never writes to blue -- it only
#        reads ~/media and ~/www/images/newsletter on blue, and it is blue's
#        *outbound* SSH session (blue -> green) that carries the bytes.
#   I-3  inert by default: no --execute means `rsync -n` (dry run). Nothing
#        moves; you get a manifest (file count + bytes) of what WOULD move.
#   I-4  idempotent + resumable: safe to Ctrl-C and rerun; --partial keeps
#        interrupted files for the next pass to pick up where it left off.
#
# Usage: 30-sync-media.sh NEW_HOST [--delta] [--execute]
#   NEW_HOST   green's SSH target AS BLUE WOULD REACH IT (user@host, or an
#              ssh-config alias defined in blue's own ~/.ssh/config). Never
#              a literal FQDN baked into this script -- that's the whole
#              point of taking it as an argument.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/log.sh"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

usage() {
  cat <<'USAGE' >&2
Usage: 30-sync-media.sh NEW_HOST [--delta] [--execute]

  NEW_HOST   green's SSH target, as BLUE would reach it. Blue has outbound
             SSH; this is a box-to-box copy, not a local-to-remote one.
  --delta    label this run as the small cutover-day re-sync. The command is
             IDENTICAL to a bulk pass (rsync is incremental by nature) -- this
             flag only changes the banner and reminds you that the download
             clients / *arr import must be frozen (50-cutover.sh) before you
             run the FINAL --delta --execute pass for real.
  --execute  actually transfer. Default is a dry run (rsync -n) that only
             reports what WOULD move: file count + total bytes, per tree.
USAGE
}

# ---- args: NEW_HOST is positional and required; flags may follow in any order ----
NEW_HOST="${1:-}"
if [ -z "$NEW_HOST" ] || [ "${NEW_HOST#--}" != "$NEW_HOST" ]; then
  usage
  printf 'STAGE=usage msg=missing-or-flag-where-NEW_HOST-belongs\n' >&2
  exit 2
fi
shift

DELTA=0; EXECUTE=0
for arg in "$@"; do
  case "$arg" in
    --delta) DELTA=1 ;;
    --execute) EXECUTE=1 ;;
    *) usage; printf 'STAGE=usage msg=unknown-argument:%s\n' "$arg" >&2; exit 2 ;;
  esac
done

if [ "$DELTA" -eq 1 ] && [ "$EXECUTE" -eq 1 ]; then
  log_warn "This is the FINAL --delta --execute pass. Confirm the cutover freeze"
  log_warn "(qBit + SAB paused, *arr import lists disabled on blue) is ACTIVE"
  log_warn "before this runs -- 50-cutover.sh owns arming that freeze, not this script."
fi

# ---- precondition: blue must be able to SSH to green unattended ----
# The rsync itself runs blue -> green, so it's BLUE's key (not this machine's)
# that needs to be in green's ~/.ssh/authorized_keys.
check_green_reachable() {
  log_info "Checking blue -> green SSH reachability ($NEW_HOST)..."
  local probe
  probe=$(sshm "ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new $NEW_HOST 'echo qflix-ssh-ok'" 2>&1)
  if printf '%s' "$probe" | grep -q 'qflix-ssh-ok'; then
    log_info "blue -> green SSH: OK"
    return 0
  fi
  log_error "blue cannot reach green over SSH as '$NEW_HOST':"
  printf '%s\n' "$probe" >&2
  log_error "Fix: authorize BLUE's key on green. Blue's public key(s):"
  sshm "cat ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub 2>/dev/null" >&2
  log_error "On green: mkdir -p ~/.ssh && append the key above to ~/.ssh/authorized_keys," \
            "chmod 600 ~/.ssh/authorized_keys, then re-run this script."
  printf 'STAGE=green-ssh-unauthorized msg=blue-cannot-ssh-to-green-%s\n' "$NEW_HOST" >&2
  return 1
}
check_green_reachable || exit 2

# ---- rsync flags: -aH --partial --info=progress2 pinned per spec/plan ----
# --stats is added so we get a parseable end-of-run summary in BOTH modes;
# -n (dry run) is appended only when --execute was not given.
RSYNC_BASE="rsync -aH --partial --info=progress2 --stats"
[ "$EXECUTE" -eq 1 ] || RSYNC_BASE="$RSYNC_BASE -n"

human_bytes() {
  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec-i --suffix=B "$1" 2>/dev/null || printf '%s bytes' "$1"
  else
    printf '%s bytes' "$1"
  fi
}

# Parse rsync --stats output for "what would/did move" -- files transferred
# and their total size, NOT the tree's grand total (already-synced files on
# a rerun correctly report as 0/0 here).
parse_stats() {
  local files bytes
  files=$(grep -oE 'Number of (regular files transferred|files transferred): [0-9,]+' "$1" \
            | tail -1 | grep -oE '[0-9,]+' | tr -d ',')
  bytes=$(grep -oE 'Total transferred file size: [0-9,]+ bytes' "$1" \
            | grep -oE '[0-9,]+' | tr -d ',')
  printf '%s %s' "${files:-0}" "${bytes:-0}"
}

# Run one source tree's sync. src_rel/dst_rel are paths relative to each
# box's home dir. $HOME below is escaped so it expands on BLUE (the remote
# side of sshm), never on the machine running this script.
run_leg() {
  local label="$1" src_rel="$2" dst_rel="$3" logfile="$4"
  log_info "== $label =="
  local remote_cmd
  remote_cmd=$(cat <<REMOTE
set -uo pipefail
SRC="\$HOME/$src_rel"
[ -d "\$SRC" ] || { printf 'STAGE=source-missing msg=no-such-dir-on-blue:%s\n' "\$SRC" >&2; exit 2; }
$RSYNC_BASE "\$SRC/" "$NEW_HOST:$dst_rel/"
REMOTE
)
  sshm "$remote_cmd" | tee "$logfile"
  return "${PIPESTATUS[0]}"
}

MODE_LABEL="dry-run"; [ "$EXECUTE" -eq 1 ] && MODE_LABEL="EXECUTE"
[ "$DELTA" -eq 1 ] && MODE_LABEL="$MODE_LABEL (delta pass)"
log_info "sync-media $MODE_LABEL -> $NEW_HOST"

LOG_MEDIA=$(mktemp); LOG_NEWS=$(mktemp)
trap 'rm -f "$LOG_MEDIA" "$LOG_NEWS"' EXIT

run_leg "media (~/media)" "media" "media" "$LOG_MEDIA"
rc=$?
if [ "$rc" -ne 0 ]; then
  [ "$rc" -eq 2 ] && { printf 'STAGE=source-missing msg=media-tree-absent-on-blue\n' >&2; exit 2; }
  printf 'STAGE=rsync-failed msg=media-leg-exit-%d\n' "$rc" >&2
  exit 1
fi

run_leg "newsletter posters (~/www/images/newsletter)" "www/images/newsletter" "www/images/newsletter" "$LOG_NEWS"
rc=$?
if [ "$rc" -ne 0 ]; then
  [ "$rc" -eq 2 ] && { printf 'STAGE=source-missing msg=newsletter-tree-absent-on-blue\n' >&2; exit 2; }
  printf 'STAGE=rsync-failed msg=newsletter-leg-exit-%d\n' "$rc" >&2
  exit 1
fi

read -r media_files media_bytes <<<"$(parse_stats "$LOG_MEDIA")"
read -r news_files news_bytes <<<"$(parse_stats "$LOG_NEWS")"
total_files=$((media_files + news_files))
total_bytes=$((media_bytes + news_bytes))

verb="would transfer"; [ "$EXECUTE" -eq 1 ] && verb="transferred"
printf 'PASS: sync-media %s -- media: %s files / %s; newsletter: %s files / %s; total: %s files / %s\n' \
  "$verb" \
  "$media_files" "$(human_bytes "$media_bytes")" \
  "$news_files" "$(human_bytes "$news_bytes")" \
  "$total_files" "$(human_bytes "$total_bytes")"
exit 0
