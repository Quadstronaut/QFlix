#!/usr/bin/env bash
# Restart Listmonk if dead. Quiet on success. Reads port from config.toml.
# XDG_RUNTIME_DIR required for `systemctl --user` from cron (no user-bus
# inherited otherwise). The curl /health probe usually succeeds before we
# reach the systemctl fallback, but if it doesn't, we need bus access.
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
set -uo pipefail
PORT=$(grep -oP 'address\s*=\s*"127\.0\.0\.1:\K[0-9]+' "$HOME/.apps/listmonk/etc/config.toml" 2>/dev/null)
[ -n "$PORT" ] || { logger -t listmonk-heartbeat "no port in config.toml"; exit 0; }
curl -sfm 5 "http://127.0.0.1:${PORT}/health" >/dev/null && exit 0
systemctl --user is-active listmonk.service >/dev/null && exit 0
logger -t listmonk-heartbeat "listmonk unhealthy — restarting"
systemctl --user restart listmonk.service
