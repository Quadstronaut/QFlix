#!/usr/bin/env bash
# Restart Tdarr Node if dead. Quiet on success.
set -uo pipefail
systemctl --user is-active tdarr-node.service >/dev/null && exit 0
logger -t tdarr-node-heartbeat "tdarr-node unhealthy — restarting"
systemctl --user restart tdarr-node.service
