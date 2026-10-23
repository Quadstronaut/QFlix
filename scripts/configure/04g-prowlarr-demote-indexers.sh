#!/usr/bin/env bash
# QFLX-74: demote known-bad indexers (LimeTorrents) to the lowest Prowlarr
# priority, then ask Prowlarr to re-sync so the *arrs pick it up. Idempotent.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

PROW_KEY="$(secret_read prowlarr.key)"
PROW_PORT="$(secret_read prowlarr.port)"
PROW_BASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"
URL="http://127.0.0.1:$PROW_PORT/$PROW_BASE/api/v1"

scpm_to "$HERE/configure/04g-prowlarr-demote-indexers.py" "/tmp/04g.py"
sshm "PROW_KEY='$PROW_KEY' PROW_URL='$URL' python3 /tmp/04g.py; rc=\$?; rm -f /tmp/04g.py; exit \$rc"
sshm "curl -fsS -o /dev/null -X POST -H 'X-Api-Key: $PROW_KEY' -H 'Content-Type: application/json' \
  -d '{\"name\":\"ApplicationIndexerSync\"}' '$URL/command'"
echo "OK: indexer priorities pinned; app sync queued."
