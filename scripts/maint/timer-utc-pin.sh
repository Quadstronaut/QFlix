#!/usr/bin/env bash
# timer-utc-pin — pin every user-systemd timer's OnCalendar to UTC. Idempotent.
#
# WHY (QFLX-75): systemd resolves a bare OnCalendar (no timezone suffix) in the
# box's LOCAL zone, and this box runs Europe/Amsterdam. At the DST fall-back
# (2026-10-25 01:00 UTC, CEST -> CET) the local 02:xx hour happened twice and
# systemd fired it only once: every bare `*:N/15`-style canary jumped from its
# 02:50 CEST run straight to 03:05 CET. ~40 push monitors went an hour without a
# heartbeat and Kuma paged the whole fleet red, with nothing actually broken.
# Spring-forward is the mirror image (one local hour that never exists).
# UTC has no DST, so a UTC-pinned timer can never skip or repeat an hour.
#
# The repo's own units are pinned at the source (`OnCalendar=... UTC`, enforced
# by tests/unit/test_timer_timezone.py). This script covers what the repo does
# NOT own: timers Ultra.cc ships (upgradinatorr.timer, logrotate.timer), units
# written by older installers still on disk, and anything added later. It
# never edits a unit file. It writes a drop-in that resets the OnCalendar list
# and re-states every entry with ` UTC` appended:
#
#   ~/.config/systemd/user/<timer>.d/50-qflix-utc.conf
#     [Timer]
#     OnCalendar=                    <- empty assignment clears the base list
#     OnCalendar=*-*-* 04:30:00 UTC
#
# Same clock digits, read as UTC. A daily job moves by the box's UTC offset
# (1h in winter, 2h in summer). None of the affected jobs has a wall-clock
# contract that this breaks.
#
# Idempotent and self-retiring: a drop-in is rewritten only when its content
# would change, and it is removed once the base unit carries its own timezone.
# daemon-reload runs at most once and only if something changed (QFLX-48:
# every reload wedges the user manager for 4-8s on this box).
#
# Usage: timer-utc-pin.sh [--no-reload]
#   --no-reload   the caller does its own daemon-reload (240 Step 7)
# Output: one `pinned|repinned|unpinned <timer>` line per change, then a summary.
# Exit: 0 ok (changed or not), 1 could not write a drop-in.
set -euo pipefail

UNIT_DIR="${QFLIX_TIMER_UNIT_DIR:-$HOME/.config/systemd/user}"
DROPIN_NAME="50-qflix-utc.conf"
RELOAD=1
[ "${1:-}" = "--no-reload" ] && RELOAD=0

# A spec already carries a zone when its last token is `UTC` or an IANA
# Area/City name (qflix-newsletter.timer uses America/Phoenix on purpose).
has_tz() {
  [[ "$1" =~ (^|[[:space:]])(UTC|[A-Za-z]+/[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)?)$ ]]
}

changed=0
scanned=0
for unit in "$UNIT_DIR"/*.timer; do
  [ -f "$unit" ] || continue
  scanned=$((scanned + 1))
  name="${unit##*/}"
  dropin_dir="$UNIT_DIR/$name.d"
  dropin="$dropin_dir/$DROPIN_NAME"

  # Read the base unit's OnCalendar list in order. An empty assignment in the
  # base file resets the list, the same way systemd reads it.
  specs=()
  bare=0
  while IFS= read -r line; do
    spec="${line#*=}"
    spec="${spec#"${spec%%[![:space:]]*}"}"     # ltrim
    spec="${spec%"${spec##*[![:space:]]}"}"     # rtrim
    if [ -z "$spec" ]; then specs=(); bare=0; continue; fi
    if ! has_tz "$spec"; then bare=1; spec="$spec UTC"; fi
    specs+=("$spec")
  done < <(grep -E '^[[:space:]]*OnCalendar[[:space:]]*=' "$unit" | tr -d '\r')

  if [ "$bare" -eq 0 ]; then
    # Nothing bare in the base unit: our drop-in (if any) has outlived its job.
    if [ -f "$dropin" ]; then
      rm -f "$dropin"
      rmdir "$dropin_dir" 2>/dev/null || true   # only if we were its sole tenant
      echo "unpinned $name"
      changed=1
    fi
    continue
  fi

  want=$(printf '# QFLX-75: managed by scripts/maint/timer-utc-pin.sh - do not edit.\n[Timer]\nOnCalendar=\n'
         printf 'OnCalendar=%s\n' "${specs[@]}")
  if [ -f "$dropin" ] && [ "$(cat "$dropin")" = "$want" ]; then
    continue
  fi
  verb=pinned
  [ -f "$dropin" ] && verb=repinned
  mkdir -p "$dropin_dir"
  tmp=$(mktemp "$dropin_dir/.utc.XXXXXX")
  printf '%s\n' "$want" > "$tmp"
  mv -f "$tmp" "$dropin"
  # Check the resulting file, not mv's exit status.
  [ "$(cat "$dropin")" = "$want" ] || { echo "error: failed to write $dropin" >&2; exit 1; }
  echo "$verb $name"
  changed=1
done

if [ "$changed" -eq 1 ] && [ "$RELOAD" -eq 1 ]; then
  systemctl --user daemon-reload
fi
echo "timer-utc-pin: scanned=$scanned changed=$changed"
