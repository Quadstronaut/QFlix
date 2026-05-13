#!/usr/bin/env bash
# scripts/configure/70-mcp-install.sh
# Deploy scripts/mcp/ to seedbox + install qflix-missing-search systemd-user units.
# Idempotent: re-runnable.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# shellcheck disable=SC1091
source "$REPO/scripts/lib/secrets.sh"
# shellcheck disable=SC1091
source "$REPO/scripts/lib/ssh.sh"

REMOTE="$(seedbox_ssh)"  # quadstronaut@seedbox.example.com

echo "→ rsync scripts/mcp/ to ${REMOTE}:~/scripts/mcp/"
rsync -avz --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$REPO/scripts/mcp/" "${REMOTE}:scripts/mcp/"

echo "→ ensure ~/scripts/mcp/events/ exists on seedbox"
ssh "$REMOTE" "mkdir -p ~/scripts/mcp/events"

echo "→ install systemd-user units"
ssh "$REMOTE" "mkdir -p ~/.config/systemd/user/"
scp "$REPO/scripts/mcp/systemd/qflix-missing-search.service" \
    "${REMOTE}:.config/systemd/user/qflix-missing-search.service"
scp "$REPO/scripts/mcp/systemd/qflix-missing-search.timer" \
    "${REMOTE}:.config/systemd/user/qflix-missing-search.timer"

echo "→ enable + start timer"
ssh "$REMOTE" "systemctl --user daemon-reload && \
               systemctl --user enable --now qflix-missing-search.timer"

echo "→ verify"
ssh "$REMOTE" "systemctl --user list-timers qflix-missing-search.timer --all --no-pager"

echo "OK: scripts/mcp/ deployed; qflix-missing-search.timer enabled."
