#!/usr/bin/env bash
# 30-sync-media.sh -- bulk media sync, blue -> green (spec section 8 row 30:
# "Keep: rsync -aH --partial, bulk passes, then one --delta pass in the freeze").
#
# Re-cut AS-IS from origin/feature/migration; the only changes are the shared
# argument/host contract (_common.sh: NEW_HOST/OLD_HOST arguments, explicit
# sshb, window guard) and the path constants, which now come from
# migrate.conf (MEDIA_ROOT_REL, NEWSLETTER_POSTERS_REL) instead of literals.
#
# Direction: the rsync is INITIATED ON BLUE (blue has outbound SSH; green never
# needs to reach blue). NEW_HOST is therefore green AS BLUE REACHES IT (user@host
# or an alias in blue's own ~/.ssh/config). No --delete: a pass only adds.
#
# Multi-pass by design: run it as often as you like while blue is live; every
# rerun only moves what changed (I-4). The LAST pass, run by 50-cutover.sh
# step 2 with --delta during the freeze, is the same command.
#
# I-2 blue is only READ (blue's outbound session carries the bytes).
# I-3 the one deliberate difference from the other scripts: without --execute
#     this still connects, but runs `rsync -n` (a read) so the dry run can say
#     how much WOULD move. That is why the window guard runs in both modes.
#
# USAGE: 30-sync-media.sh NEW_HOST [--old-host HOST] [--delta] [--execute]
# EXIT:  0 ok | 1 rsync failed | 2 refused / blue cannot reach green / source missing
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

usage() { echo "usage: $0 NEW_HOST [--old-host HOST] [--delta] [--execute]" >&2; }
mig_args "" "$@"
need_new_host
resolve_old_host

if [ "$DELTA" -eq 1 ] && [ "$EXECUTE" -eq 1 ]; then
  log_warn "FINAL --delta --execute pass: the cutover freeze (qBit + SAB paused) must be ACTIVE;"
  log_warn "50-cutover.sh step 1 owns the freeze, not this script."
fi

sshb true >/dev/null 2>&1 || stage 2 blue-unreachable "ssh-to-OLD_HOST-failed"
window_guard

# blue -> green must work unattended; it is BLUE's key that green must trust.
probe="$(sshb "ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new $NEW_HOST 'echo qflix-ssh-ok'" 2>&1)"
if ! printf '%s' "$probe" | grep -q qflix-ssh-ok; then
  log_error "blue cannot ssh to green as '$NEW_HOST'. Authorize blue's public key on green:"
  sshb "cat ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub 2>/dev/null" >&2
  stage 2 green-ssh-unauthorized "blue-cannot-ssh-to-NEW_HOST"
fi

RSYNC_BASE="rsync -aH --partial --info=progress2 --stats"
[ "$EXECUTE" -eq 1 ] || RSYNC_BASE="$RSYNC_BASE -n"

human_bytes() { numfmt --to=iec-i --suffix=B "$1" 2>/dev/null || printf '%s bytes' "$1"; }
parse_stats() {  # files + bytes actually (or would-be) transferred, not tree totals
  local files bytes
  files=$(grep -oE 'Number of (regular files transferred|files transferred): [0-9,]+' "$1" | tail -1 | grep -oE '[0-9,]+' | tr -d ',')
  bytes=$(grep -oE 'Total transferred file size: [0-9,]+ bytes' "$1" | grep -oE '[0-9,]+' | tr -d ',')
  printf '%s %s' "${files:-0}" "${bytes:-0}"
}
run_leg() {  # label src_rel dst_rel logfile ($HOME expands on BLUE)
  log_info "== $1 =="
  sshb "set -uo pipefail
SRC=\"\$HOME/$2\"
[ -d \"\$SRC\" ] || { printf 'STAGE=source-missing msg=no-such-dir-on-blue:%s\n' \"\$SRC\" >&2; exit 2; }
$RSYNC_BASE --rsync-path='mkdir -p $3 && rsync' \"\$SRC/\" $(printf '%q' "$NEW_HOST:$3/")" | tee "$4"
  return "${PIPESTATUS[0]}"
}

MODE="dry-run"; [ "$EXECUTE" -eq 1 ] && MODE="EXECUTE"; [ "$DELTA" -eq 1 ] && MODE="$MODE (delta pass)"
log_info "sync-media $MODE -> $NEW_HOST"
LOG_MEDIA="$(mktemp)"; LOG_NEWS="$(mktemp)"
trap 'rm -f "$LOG_MEDIA" "$LOG_NEWS"' EXIT

run_leg "media (~/$MEDIA_ROOT_REL)" "$MEDIA_ROOT_REL" "$MEDIA_ROOT_REL" "$LOG_MEDIA"; rc=$?
[ "$rc" -eq 2 ] && stage 2 source-missing "media-tree-absent-on-blue"
[ "$rc" -eq 0 ] || stage 1 rsync-failed "media-leg-exit-$rc"
run_leg "newsletter posters (~/$NEWSLETTER_POSTERS_REL)" "$NEWSLETTER_POSTERS_REL" "$NEWSLETTER_POSTERS_REL" "$LOG_NEWS"; rc=$?
[ "$rc" -eq 2 ] && stage 2 source-missing "newsletter-tree-absent-on-blue"
[ "$rc" -eq 0 ] || stage 1 rsync-failed "newsletter-leg-exit-$rc"

read -r mf mb <<<"$(parse_stats "$LOG_MEDIA")"
read -r nf nb <<<"$(parse_stats "$LOG_NEWS")"
verb="would transfer"; [ "$EXECUTE" -eq 1 ] && verb="transferred"
printf 'PASS: sync-media %s -- media: %s files / %s; newsletter: %s files / %s\n' \
  "$verb" "$mf" "$(human_bytes "$mb")" "$nf" "$(human_bytes "$nb")"
exit 0
