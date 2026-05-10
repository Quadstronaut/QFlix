#!/usr/bin/env bash
# Deploy prune-text-libraries.sh to manitoba + install daily cron entry.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

# Push the prune script to manitoba
sshm 'mkdir -p ~/scripts/post-import ~/.opt/secrets ~/.cache/prune-text'
scpm_to "$HERE/prune-text-libraries.sh" "/home/quadstronaut/scripts/post-import/prune-text-libraries.sh"
sshm 'chmod +x ~/scripts/post-import/prune-text-libraries.sh'

# Push the notifiarr key (so the cron job can use it without controller intervention)
scpm_to "$SECRETS_DIR/notifiarr.key" "/home/quadstronaut/.opt/secrets/notifiarr.key"
sshm 'chmod 600 ~/.opt/secrets/notifiarr.key'

# Install cron entry — daily at 04:00 server time, idempotent (replaces any existing entry)
sshm 'crontab -l 2>/dev/null | grep -v prune-text-libraries.sh > /tmp/_crontab; echo "0 4 * * * /home/quadstronaut/scripts/post-import/prune-text-libraries.sh >> /home/quadstronaut/.cache/prune-text/prune.log 2>&1" >> /tmp/_crontab; crontab /tmp/_crontab; rm /tmp/_crontab'
log_info "cron installed:"
sshm 'crontab -l 2>/dev/null | tail -5'

# Verify with a dry-run
log_info "dry-run on remote:"
sshm '~/scripts/post-import/prune-text-libraries.sh --dry-run'
