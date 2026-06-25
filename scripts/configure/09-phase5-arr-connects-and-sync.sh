#!/usr/bin/env bash
# Phase 5: Sonarr+Radarr Plex/Notifiarr Connects + Prowlarr Apps Sync
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

scpm_to "$HERE/configure/09-phase5-arr-connects-and-sync.py" /tmp/09-phase5.py

# PLEX_HOST is the docker bridge GATEWAY (172.17.0.1), NOT secrets/plex.host
# (=127.0.0.1, the *container's* own loopback) and NOT a per-container IP. The
# *arr run in Docker and reach host-side Plex via the bridge gateway + plex.port.
# Same lesson as 50-tautulli-pms-url-fix.sh after the 2026-05-20 Plex re-IP
# (172.17.1.250:32400 -> 172.17.0.1:17025); the gateway is stable across
# container reassignment. Using plex.host here is what left the *arr Plex
# Connects pointed at the dead 172.17.1.250:32400 (fixed live 2026-06-25).
sshm "
  PROW_KEY='$(secret_read prowlarr.key)' \
  PROW_PORT='$(secret_read prowlarr.port)' \
  PROW_BASE='$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)' \
  SONARR_KEY='$(secret_read sonarr.key)' \
  SONARR_PORT='$(secret_read sonarr.port)' \
  SONARR_BASE='$(secret_read sonarr.urlbase 2>/dev/null || echo sonarr)' \
  RADARR_KEY='$(secret_read radarr.key)' \
  RADARR_PORT='$(secret_read radarr.port)' \
  RADARR_BASE='$(secret_read radarr.urlbase 2>/dev/null || echo radarr)' \
  SONARR2_KEY='$(secret_read sonarr2.key)' \
  SONARR2_PORT='$(secret_read sonarr2.port)' \
  SONARR2_BASE='$(secret_read sonarr2.urlbase 2>/dev/null || echo sonarr2)' \
  RADARR2_KEY='$(secret_read radarr2.key)' \
  RADARR2_PORT='$(secret_read radarr2.port)' \
  RADARR2_BASE='$(secret_read radarr2.urlbase 2>/dev/null || echo radarr2)' \
  PLEX_HOST='172.17.0.1' \
  PLEX_PORT='$(secret_read plex.port)' \
  PLEX_TOKEN='$(secret_read plex.token)' \
  NOTIFIARR_KEY='$(secret_read notifiarr.key)' \
  python3 /tmp/09-phase5.py
"
