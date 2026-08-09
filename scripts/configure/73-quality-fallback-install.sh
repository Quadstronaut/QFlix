#!/usr/bin/env bash
# scripts/configure/73-quality-fallback-install.sh
# Deploy quality_fallback.py + units, bootstrap fallback profiles, sync
# manifest, restart pusher. Idempotent: re-runnable.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# shellcheck disable=SC1091
source "$REPO/scripts/lib/ssh.sh"   # provides $SSHM_HOST + sshm/scpm_to helpers

echo "-> tar+ssh scripts/mcp/ to ${SSHM_HOST}:~/scripts/mcp/"
sshm "mkdir -p ~/scripts/mcp"
( cd "$REPO/scripts/mcp" && tar --exclude='__pycache__' --exclude='*.pyc' -cf - . ) \
  | sshm "tar -C scripts/mcp -xf -"

echo "-> bootstrap fallback profiles on radarr + radarr2 (fail-loud)"
sshm "python3 ~/scripts/mcp/quality_fallback.py --bootstrap-profiles"

echo "-> install systemd-user units"
sshm "mkdir -p ~/.config/systemd/user/"
scpm_to "$REPO/scripts/mcp/systemd/qflix-quality-fallback.service" \
        ".config/systemd/user/qflix-quality-fallback.service"
scpm_to "$REPO/scripts/mcp/systemd/qflix-quality-fallback.timer" \
        ".config/systemd/user/qflix-quality-fallback.timer"

echo "-> sync manifest (pusher reads ~/.opt/maint/apps.yaml)"
scpm_to "$REPO/manifest/apps.yaml" ".opt/maint/apps.yaml"

echo "-> enable + start timer"
sshm "systemctl --user daemon-reload && systemctl --user enable --now qflix-quality-fallback.timer"

# NOTE: pusher restart clears recovery's permanently_failed marks (known,
# accepted hazard — push-suppression-and-resend memory).
echo "-> restart pusher to pick up new manifest entry"
sshm "systemctl --user restart manitoba-maint-pusher.service"

echo "-> verify"
sshm "systemctl --user list-timers qflix-quality-fallback.timer --all --no-pager"
sshm "python3 ~/scripts/mcp/quality_fallback.py --dry-run" | head -5

echo "OK: quality-fallback deployed; timer enabled; profiles bootstrapped."
echo "OPERATOR: create Kuma push monitor 'Qflix Quality Fallback' via"
echo "          scripts/maint/bootstrap-kuma-monitors.py (needs Kuma creds)."
