#!/usr/bin/env bash
# Restart Tdarr Server if dead. Quiet on success. Reads port from server config.
# XDG_RUNTIME_DIR required for `systemctl --user` from cron (no user-bus
# inherited otherwise). The curl /api/v2/status probe usually succeeds before
# we reach the systemctl fallback, but if it doesn't, we need bus access.
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
set -uo pipefail
CONF="$HOME/.apps/tdarr/configs/Tdarr_Server_Config.json"
PORT=$(grep -oP '"serverPort":\s*\K[0-9]+' "$CONF" 2>/dev/null)
[ -n "$PORT" ] || { logger -t tdarr-server-heartbeat "no webUIPort in config"; exit 0; }
curl -sfm 5 "http://127.0.0.1:${PORT}/api/v2/status" >/dev/null && exit 0
systemctl --user is-active tdarr-server.service >/dev/null && exit 0

# Do not stack a restart on top of one already in flight. systemd's own
# on-failure loop (RestartSec=10s) plus this 5-minute tick used to be two
# independent restarters racing the same port; the unit's ExecStartPre drain
# absorbs the port race, but a heartbeat that fires mid-activation still turns
# one recovery into two. `activating` covers auto-restart AND the drain wait.
STATE=$(systemctl --user show -p ActiveState --value tdarr-server.service 2>/dev/null)
if [ "$STATE" = "activating" ] || [ "$STATE" = "deactivating" ]; then
  logger -t tdarr-server-heartbeat "tdarr-server is $STATE — leaving it alone this tick"
  exit 0
fi

logger -t tdarr-server-heartbeat "tdarr-server unhealthy (state=$STATE) — restarting"
systemctl --user restart tdarr-server.service
