#!/usr/bin/env bash
# Tdarr 24/7 — retire the fair-use quiet-hours pause.
#
# WAS: Phase 29.3 installed tdarr-node-pause.timer (18:00 UTC -> stop) and
# tdarr-node-resume.timer (23:00 UTC -> start) so the node's workers did not
# compete with streaming users for I/O and CPU during peak watch hours.
#
# NOW (2026-08-20, operator directive): the node runs 24/7 and fair-use is
# enforced by CONCURRENCY instead — manifest/apps.yaml tdarr-node.throttle,
# written to both Tdarr layers by 50b-tdarr-config.py, plus Nice=10 on the
# unit. A throttle the box feels every hour beats a five-hour hole in coverage.
#
# WHY THE PAUSE WAS THE WRONG LEVER
#   * It cost ~21% of transcode capacity, which is why 39 hevc/av1 files were
#     still sitting unconverted. Plex then had to transcode those on the fly
#     for every client — including the low-bandwidth ones that cannot keep up.
#     The pause was nominally protecting streamers from Tdarr and was in fact
#     handing them a library Plex had to transcode. Backwards.
#   * It blinded every monitoring surface for exactly those five hours: the
#     pusher pushed "Tdarr Node" UP and skipped probe + auto-heal, the
#     health-check canary held its stall threshold above the pause, and a whole
#     canary existed only to watch the pause itself. Deleting the window
#     un-blinds all of them at once.
#
# This script is the DECOMMISSION half and is idempotent: stop, disable, and
# DELETE both units, then assert tdarr-node is enabled and running. Deleting
# rather than merely disabling is deliberate — manifest/jobs.yaml no longer
# declares these timers, and timer-liveness.sh enumerates in BOTH directions,
# so a unit left on the box with no ledger entry is itself a finding.
#
# Safe to re-run. Safe on a box that never had the timers.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

log_info "Retiring Tdarr quiet-hours timers; asserting 24/7 node"

sshm "bash -s" <<'UNITSCRIPT'
set -uo pipefail
UDIR=~/.config/systemd/user

REMOVED=0
for T in tdarr-node-pause tdarr-node-resume; do
  # `disable` on an absent unit is an error, not a no-op, hence the guards.
  if systemctl --user list-unit-files "$T.timer" 2>/dev/null | grep -q "$T.timer"; then
    systemctl --user stop    "$T.timer"   2>/dev/null || true
    systemctl --user disable "$T.timer"   2>/dev/null || true
    REMOVED=$((REMOVED+1))
  fi
  systemctl --user stop "$T.service" 2>/dev/null || true
  rm -f "$UDIR/$T.timer" "$UDIR/$T.service"
done

systemctl --user daemon-reload
systemctl --user reset-failed 2>/dev/null || true

echo "removed_timer_units=$REMOVED"

# The pause timer's last act may have been to stop the node. Bring it back and
# leave it enabled — from here the pusher owns liveness around the clock.
systemctl --user enable --now tdarr-node.service 2>/dev/null || true
# `enable --now` does NOT restart an already-running unit and does not start a
# unit that is loaded-but-dead in every systemd version we have run here, so
# assert the state rather than trusting the verb (the same trap that made
# on-disk dash assets 404 — see the dash-build-without-restart note).
if [ "$(systemctl --user is-active tdarr-node.service 2>/dev/null)" != "active" ]; then
  systemctl --user restart tdarr-node.service || true
fi

echo
echo "=== residual quiet-hours units (expect NONE) ==="
systemctl --user list-unit-files 2>/dev/null | grep -E "tdarr-node-(pause|resume)" || echo "none"
echo
echo "=== tdarr units ==="
systemctl --user is-active tdarr-server.service tdarr-node.service 2>&1
UNITSCRIPT

# Fail loudly if the node did not come back — a silent "retired the pause and
# left the node stopped" is strictly worse than the pause it replaced.
STATE=$(sshm 'systemctl --user is-active tdarr-node.service' 2>/dev/null | tr -d "[:space:]")
[ "$STATE" = "active" ] || die "tdarr-node is '$STATE' after retiring quiet hours — expected active"

RESIDUAL=$(sshm 'systemctl --user list-unit-files 2>/dev/null | grep -cE "tdarr-node-(pause|resume)"' 2>/dev/null | tr -d "[:space:]")
[ "${RESIDUAL:-0}" = "0" ] || die "$RESIDUAL quiet-hours unit(s) still present on the box"

log_info "Tdarr 24/7 — quiet-hours timers retired, tdarr-node active"
