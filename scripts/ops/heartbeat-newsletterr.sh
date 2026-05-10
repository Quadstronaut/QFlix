#!/usr/bin/env bash
# Restart Newsletterr if dead. Quiet on success. Reads port from .env.
set -uo pipefail
ENV_FILE="$HOME/.apps/newsletterr/repo/env/.env"
PORT=$(grep -oP '^PORT=\K[0-9]+' "$ENV_FILE" 2>/dev/null)
[ -n "$PORT" ] || { logger -t newsletterr-heartbeat "no PORT in .env"; exit 0; }
curl -sfm 5 "http://127.0.0.1:${PORT}/" >/dev/null && exit 0
systemctl --user is-active newsletterr.service >/dev/null && exit 0
logger -t newsletterr-heartbeat "newsletterr unhealthy — restarting"
systemctl --user restart newsletterr.service
