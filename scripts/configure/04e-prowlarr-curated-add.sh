#!/usr/bin/env bash
# Add the operator's curated 21 indexers, force-enable, audit (real test),
# disable any that fail. Tags TorrentGalaxyClone/1337x/Kickass/ExtraTorrent
# as `cloudflare` so they route through FlareSolverr.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

PROW_KEY="$(secret_read prowlarr.key)"
PROW_PORT="$(secret_read prowlarr.port)"
PROW_BASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"

scpm_to "$HERE/data/prowlarr-indexers-curated.json" "/tmp/prowlarr-curated.json"
scpm_to "$HERE/configure/04e-prowlarr-curated-add.py" "/tmp/04e.py"

sshm "PROW_KEY='$PROW_KEY' PROW_URL='http://127.0.0.1:$PROW_PORT/$PROW_BASE/api/v1' python3 /tmp/04e.py"
