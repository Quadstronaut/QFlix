#!/usr/bin/env bash
# Restart Tdarr Node if dead. Quiet on success.
# XDG_RUNTIME_DIR is required for `systemctl --user` to reach the user bus —
# cron sets neither this nor DBUS_SESSION_BUS_ADDRESS, so without the fallback
# every invocation here returned "Failed to connect to bus: No medium found".
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
set -uo pipefail
systemctl --user is-active tdarr-node.service >/dev/null && exit 0
logger -t tdarr-node-heartbeat "tdarr-node unhealthy — restarting"
systemctl --user restart tdarr-node.service
