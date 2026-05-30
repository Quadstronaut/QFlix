#!/usr/bin/env bash
# Restart Tdarr Node if dead. Quiet on success.
# XDG_RUNTIME_DIR is required for `systemctl --user` to reach the user bus —
# cron sets neither this nor DBUS_SESSION_BUS_ADDRESS, so without the fallback
# every invocation here returned "Failed to connect to bus: No medium found".
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
set -uo pipefail

# Fair-use quiet hours: tdarr-node is *intentionally* stopped 18:00-23:00 UTC by
# tdarr-node-pause.timer (see scripts/configure/50c-tdarr-quiet-hours.sh) so its
# worker threads don't fight streamers during peak watch hours. Without this guard
# the watchdog below sees the paused node as a fault ("0 registered nodes" /
# inactive) and revives it on the very next tick, defeating the 5-hour pause
# (observed 2026-05-30: stopped 18:00:01 UTC, back up 18:02:27 UTC). Skip all
# restart paths during the pause window. 10# forces base-10 so "08" isn't read as
# octal. Keep this window in sync with 50c's OnCalendar values.
HOUR_UTC=$((10#$(date -u +%H)))
if [ "$HOUR_UTC" -ge 18 ] && [ "$HOUR_UTC" -lt 23 ]; then
  exit 0
fi

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
