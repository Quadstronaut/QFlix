#!/usr/bin/env bash
# Phase 29.3 — Tdarr fair-use quiet hours.
#
# Pauses tdarr-node.service every evening (18:00-23:00 UTC) so its 2 worker
# threads don't compete with streaming users for I/O / CPU during peak watch
# hours. Two systemd user timers handle the pause + resume:
#
#   tdarr-node-pause.timer   — fires at 18:00 UTC -> systemctl stop tdarr-node
#   tdarr-node-resume.timer  — fires at 23:00 UTC -> systemctl start tdarr-node
#
# Why systemd timers (not Tdarr's built-in 168-slot schedule grid):
#   * Deterministic: `systemctl list-timers` shows next-fire, the in-DB
#     schedule does not.
#   * Survives Tdarr config wipes / re-installs.
#   * Visible in journalctl when something goes wrong.
#
# Idempotent. Safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

log_info "Installing Tdarr quiet-hours pause/resume timers"

sshm "bash -s" <<'UNITSCRIPT'
set -euo pipefail
UDIR=~/.config/systemd/user
mkdir -p "$UDIR"

cat > "$UDIR/tdarr-node-pause.service" <<'UNIT'
[Unit]
Description=Tdarr node pause (fair-use quiet hours start)
After=tdarr-node.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'systemctl --user stop tdarr-node.service'
UNIT

cat > "$UDIR/tdarr-node-pause.timer" <<'UNIT'
[Unit]
Description=Pause Tdarr node at 18:00 UTC daily (start of streaming peak)

[Timer]
OnCalendar=*-*-* 18:00:00 UTC
Persistent=true
Unit=tdarr-node-pause.service

[Install]
WantedBy=timers.target
UNIT

cat > "$UDIR/tdarr-node-resume.service" <<'UNIT'
[Unit]
Description=Tdarr node resume (fair-use quiet hours end)

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'systemctl --user start tdarr-node.service'
UNIT

cat > "$UDIR/tdarr-node-resume.timer" <<'UNIT'
[Unit]
Description=Resume Tdarr node at 23:00 UTC daily (end of streaming peak)

[Timer]
OnCalendar=*-*-* 23:00:00 UTC
Persistent=true
Unit=tdarr-node-resume.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
# enable + start so the timer is armed AND the next-fire is computed. Re-runs
# are no-op — systemd treats enable --now as idempotent.
systemctl --user enable --now tdarr-node-pause.timer
systemctl --user enable --now tdarr-node-resume.timer

echo
echo "=== tdarr-node quiet-hours timers ==="
systemctl --user list-timers tdarr-node-pause.timer tdarr-node-resume.timer --no-pager 2>/dev/null | head -10
UNITSCRIPT

log_info "Phase 29.3 complete — Tdarr quiet hours armed (18:00-23:00 UTC daily)"
