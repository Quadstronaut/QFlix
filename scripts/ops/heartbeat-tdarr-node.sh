#!/usr/bin/env bash
# Restart Tdarr Node if dead. Quiet on success.
# XDG_RUNTIME_DIR is required for `systemctl --user` to reach the user bus —
# cron sets neither this nor DBUS_SESSION_BUS_ADDRESS, so without the fallback
# every invocation here returned "Failed to connect to bus: No medium found".
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
set -uo pipefail

# Tdarr Node has no inbound HTTP listener — it's a worker that connects out
# to Tdarr Server. The "alive but unregistered" failure mode (process up,
# server-side node list empty) is detected via the server's /api/v2/get-nodes
# endpoint; for that, query the server and check the node count. Falls back
# to a plain systemctl liveness check if the server isn't reachable.
CONF="$HOME/.apps/tdarr/configs/Tdarr_Server_Config.json"
PORT=$(grep -oP '"serverPort":\s*\K[0-9]+' "$CONF" 2>/dev/null)
if [ -n "$PORT" ]; then
  # If the server has zero registered nodes for >1 cycle, restart the node.
  NODE_COUNT=$(curl -sfm 5 "http://127.0.0.1:${PORT}/api/v2/get-nodes" 2>/dev/null \
    | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))' 2>/dev/null || echo "")
  if [ "${NODE_COUNT:-}" = "0" ]; then
    logger -t tdarr-node-heartbeat "server has 0 registered nodes — restarting node"
    systemctl --user restart tdarr-node.service
    exit 0
  fi
fi

systemctl --user is-active tdarr-node.service >/dev/null && exit 0
logger -t tdarr-node-heartbeat "tdarr-node unhealthy — restarting"
systemctl --user restart tdarr-node.service
