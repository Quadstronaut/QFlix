#!/usr/bin/env bash
# Restart Tdarr Server if dead; restart the uid relay in front of it if THAT is
# dead. Quiet on success.
# XDG_RUNTIME_DIR required for `systemctl --user` from cron (no user-bus
# inherited otherwise). The curl /api/v2/status probe usually succeeds before
# we reach the systemctl fallback, but if it doesn't, we need bus access.
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
set -uo pipefail

# QFLX-97: Tdarr listens on a unix socket and tdarr-uid-relay owns its TCP
# address. Two questions, asked separately, because they have different
# remedies: probing only through the relay would answer "Tdarr is down" when
# the relay is, and restart Tdarr (killing in-flight transcodes) to fix a
# process it does not touch.
SOCK="/run/user/$(id -u)/qflix-tdarr/tdarr.sock"
if curl -sfm 5 --unix-socket "$SOCK" -o /dev/null http://localhost/api/v2/status; then
  systemctl --user is-active --quiet tdarr-uid-relay.service && exit 0
  # Restart=always normally beats this tick; reaching here means it did not.
  logger -t tdarr-server-heartbeat "tdarr up but tdarr-uid-relay inactive — restarting the relay"
  systemctl --user restart tdarr-uid-relay.service
  exit 0
fi
systemctl --user is-active tdarr-server.service >/dev/null && exit 0

# Do not stack a restart on top of one already in flight. systemd's own
# on-failure loop (RestartSec=10s) plus this 5-minute tick used to be two
# independent restarters racing the same port; the unit's ExecStartPre drain
# absorbs that, but a heartbeat that fires mid-activation still turns one
# recovery into two. `activating` covers auto-restart AND the drain wait.
STATE=$(systemctl --user show -p ActiveState --value tdarr-server.service 2>/dev/null)
if [ "$STATE" = "activating" ] || [ "$STATE" = "deactivating" ]; then
  logger -t tdarr-server-heartbeat "tdarr-server is $STATE — leaving it alone this tick"
  exit 0
fi

logger -t tdarr-server-heartbeat "tdarr-server unhealthy (state=$STATE) — restarting"
systemctl --user restart tdarr-server.service
