#!/usr/bin/env bash
# Restart Tdarr Server if dead. Quiet on success. Reads port from server config.
set -uo pipefail
CONF="$HOME/.apps/tdarr/configs/Tdarr_Server_Config.json"
PORT=$(grep -oP '"serverPort":\s*\K[0-9]+' "$CONF" 2>/dev/null)
[ -n "$PORT" ] || { logger -t tdarr-server-heartbeat "no webUIPort in config"; exit 0; }
curl -sfm 5 "http://127.0.0.1:${PORT}/api/v2/status" >/dev/null && exit 0
systemctl --user is-active tdarr-server.service >/dev/null && exit 0
logger -t tdarr-server-heartbeat "tdarr-server unhealthy — restarting"
systemctl --user restart tdarr-server.service
