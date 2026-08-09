#!/usr/bin/env bash
# scripts/configure/73b-specials-policy-install.sh
# Deploy specials_policy.py + units, sync manifest, restart pusher. Idempotent:
# re-runnable. Standalone from 73-quality-fallback-install.sh by design — the
# specials janitor is its own compartmentalized unit (spec
# 2026-07-18-tv-fallback-v2-design.md).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# shellcheck disable=SC1091
source "$REPO/scripts/lib/ssh.sh"   # provides $SSHM_HOST + sshm/scpm_to helpers

echo "-> tar+ssh scripts/mcp/ to ${SSHM_HOST}:~/scripts/mcp/"
sshm "mkdir -p ~/scripts/mcp"
( cd "$REPO/scripts/mcp" && tar --exclude='__pycache__' --exclude='*.pyc' -cf - . ) \
  | sshm "tar -C scripts/mcp -xf -"

echo "-> install systemd-user units"
sshm "mkdir -p ~/.config/systemd/user/"
scpm_to "$REPO/scripts/mcp/systemd/qflix-specials-policy.service" \
        ".config/systemd/user/qflix-specials-policy.service"
scpm_to "$REPO/scripts/mcp/systemd/qflix-specials-policy.timer" \
        ".config/systemd/user/qflix-specials-policy.timer"

echo "-> sync manifest (pusher reads ~/.opt/maint/apps.yaml)"
scpm_to "$REPO/manifest/apps.yaml" ".opt/maint/apps.yaml"

echo "-> enable + start timer"
sshm "systemctl --user daemon-reload && systemctl --user enable --now qflix-specials-policy.timer"

# NOTE: pusher restart clears recovery's permanently_failed marks (known,
# accepted hazard — push-suppression-and-resend memory).
echo "-> restart pusher to pick up new manifest entry"
sshm "systemctl --user restart manitoba-maint-pusher.service"

echo "-> verify (dry-run should be a no-op JSON — S00 already swept 2026-07-18)"
sshm "systemctl --user list-timers qflix-specials-policy.timer --all --no-pager"
sshm "python3 ~/scripts/mcp/specials_policy.py --dry-run" | head -5

echo "OK: specials-policy deployed; timer enabled."
echo "OPERATOR: create Kuma push monitor 'Qflix Specials Policy' via"
echo "          scripts/maint/bootstrap-kuma-monitors.py (needs Kuma creds)."
