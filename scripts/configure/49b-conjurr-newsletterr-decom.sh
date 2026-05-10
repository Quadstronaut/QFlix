#!/usr/bin/env bash
# Phase 24b — Decommission Conjurr + Newsletterr.
#
# Both apps are replaced by qflix-newsletter (Phase 24, scripts/qflix-newsletter/).
# Newsletterr's Playwright Chromium dependency frees ~150 MB.
#
# This script is idempotent and safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

for APP in conjurr newsletterr; do
  log_info "decommissioning $APP"

  # Stop + disable + remove the user-systemd unit
  sshm "systemctl --user stop ${APP}.service 2>/dev/null || true; \
        systemctl --user disable ${APP}.service 2>/dev/null || true; \
        rm -f ~/.config/systemd/user/${APP}.service; \
        systemctl --user daemon-reload"

  # Remove the install tree (this also drops Playwright's chromium for newsletterr)
  sshm "rm -rf ~/.apps/${APP}"

  # Drop heartbeat cron line + the heartbeat script itself
  sshm "(crontab -l 2>/dev/null | grep -v 'heartbeat-${APP}') | crontab - 2>/dev/null || true; \
        rm -f ~/scripts/ops/heartbeat-${APP}.sh"

  # Drop any stale nginx fragments (both apps were internal-only — fragment
  # should already be absent, but clear it just in case).
  sshm "rm -f ~/.apps/nginx/proxy.d/${APP}.conf"
done

# Reload nginx after fragment cleanup
sshm '/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t && systemctl --user reload nginx'

# Drop the listmonk → newsletterr DB sync cron + script (Newsletterr is gone)
sshm "(crontab -l 2>/dev/null | grep -v 'listmonk-to-newsletterr-sync') | crontab - 2>/dev/null || true; \
      rm -f ~/scripts/ops/listmonk-to-newsletterr-sync.py"

log_info "Phase 24b complete — Conjurr + Newsletterr removed"
log_info "Re-run scripts/maint/bootstrap-kuma-monitors.py to drop the orphaned Kuma monitors"
