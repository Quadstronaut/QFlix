#!/usr/bin/env bash
# Restart manitoba-maint-webhook if dead. Quiet on success. Reads port from
# ~/.opt/maint/maintenance.port (rendered by 240-maintenance-install.sh).
set -uo pipefail
PORT_FILE="$HOME/.opt/maint/maintenance.port"
[ -f "$PORT_FILE" ] || { logger -t maint-webhook-heartbeat "no port file"; exit 0; }
PORT=$(tr -d '[:space:]' < "$PORT_FILE")
[ -n "$PORT" ] || { logger -t maint-webhook-heartbeat "empty port file"; exit 0; }
curl -sfm 5 "http://127.0.0.1:${PORT}/health" >/dev/null && exit 0
systemctl --user is-active manitoba-maint-webhook.service >/dev/null && exit 0
logger -t maint-webhook-heartbeat "webhook unhealthy — restarting"
systemctl --user restart manitoba-maint-webhook.service
