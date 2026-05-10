#!/usr/bin/env bash
# Restart Conjurr if dead. Quiet on success. Reads port from env/.env (Conjurr's app.py
# moves repo/.env -> repo/env/.env on first run, so we read the destination path).
set -uo pipefail
ENV_FILE="$HOME/.apps/conjurr/repo/env/.env"
PORT=$(grep -oP '^PORT=\K[0-9]+' "$ENV_FILE" 2>/dev/null)
[ -n "$PORT" ] || { logger -t conjurr-heartbeat "no PORT in env/.env"; exit 0; }
curl -sfm 5 "http://127.0.0.1:${PORT}/" >/dev/null && exit 0
systemctl --user is-active conjurr.service >/dev/null && exit 0
logger -t conjurr-heartbeat "conjurr unhealthy — restarting"
systemctl --user restart conjurr.service
