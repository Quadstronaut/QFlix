#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"

log_info "Creating new media + download directories on manitoba..."
sshm bash -s <<'REMOTE'
set -euo pipefail
mkdir -p \
  ~/downloads/qbittorrent/radarr-anime \
  ~/downloads/qbittorrent/sonarr-anime \
  ~/downloads/qbittorrent/readarr \
  ~/downloads/qbittorrent/mylar \
  ~/media/Anime \
  ~/media/'Anime Movies' \
  ~/media/Books \
  ~/media/Comics

# Existing-empty dirs we just confirm:
ls -dF ~/media/Audiobooks ~/media/Manga ~/media/Podcasts >/dev/null
echo "Directories OK."
REMOTE
