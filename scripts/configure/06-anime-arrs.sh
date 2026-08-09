#!/usr/bin/env bash
# Configure Sonarr2 + Radarr2: root folders + qBit download client.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

scpm_to "$HERE/configure/06-anime-arrs.py" /tmp/06-anime.py

sshm "
  SONARR2_KEY='$(secret_read sonarr2.key)' \
  SONARR2_PORT='$(secret_read sonarr2.port)' \
  SONARR2_BASE='$(secret_read sonarr2.urlbase 2>/dev/null || echo sonarr2)' \
  RADARR2_KEY='$(secret_read radarr2.key)' \
  RADARR2_PORT='$(secret_read radarr2.port)' \
  RADARR2_BASE='$(secret_read radarr2.urlbase 2>/dev/null || echo radarr2)' \
  QBIT_USER='$(secret_read qbittorrent.user)' \
  QBIT_PASS='$(secret_read qbittorrent.password)' \
  python3 /tmp/06-anime.py
"
