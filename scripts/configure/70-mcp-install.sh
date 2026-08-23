#!/usr/bin/env bash
# scripts/configure/70-mcp-install.sh
# Deploy scripts/mcp/ to seedbox + install qflix-missing-search systemd-user units.
# Idempotent: re-runnable.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# shellcheck disable=SC1091
source "$REPO/scripts/lib/ssh.sh"   # provides $SSHM_HOST + sshm/scpm_to helpers

echo "-> tar+ssh scripts/mcp/ to ${SSHM_HOST}:~/scripts/mcp/"
# Workstation Git Bash doesn't ship rsync; tar-over-ssh is built-in equivalent
# for fresh deploys (no --delete; that's fine since this is additive).
sshm "mkdir -p ~/scripts/mcp"
( cd "$REPO/scripts/mcp" && tar --exclude='__pycache__' --exclude='*.pyc' -cf - . ) \
  | sshm "tar -C scripts/mcp -xf -"

# RESTORE THE EXEC BIT. tar copies the mode it finds on disk, and the mode on
# disk here is a lie: this repo is checked out on Windows, where git sets
# core.filemode=false and writes every file 644 no matter what the index says.
# git has all 15 .py under scripts/mcp at 100755; the box got them at 644, and
# deploy-drift caught it as `deploy-mode-drift` on 2026-08-23. The systemd units
# are the only 644 files in the tree, and this leaves them alone.
sshm "find ~/scripts/mcp -name '*.py' -exec chmod 755 {} +"

echo "-> ensure ~/scripts/mcp/events/ exists on seedbox"
sshm "mkdir -p ~/scripts/mcp/events"

echo "-> install systemd-user units"
sshm "mkdir -p ~/.config/systemd/user/"
scpm_to "$REPO/scripts/mcp/systemd/qflix-missing-search.service" \
        ".config/systemd/user/qflix-missing-search.service"
scpm_to "$REPO/scripts/mcp/systemd/qflix-missing-search.timer" \
        ".config/systemd/user/qflix-missing-search.timer"

echo "-> enable + start timer"
sshm "systemctl --user daemon-reload && systemctl --user enable --now qflix-missing-search.timer"

echo "-> verify"
sshm "systemctl --user list-timers qflix-missing-search.timer --all --no-pager"

echo "OK: scripts/mcp/ deployed; qflix-missing-search.timer enabled."
