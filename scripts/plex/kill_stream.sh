#!/usr/bin/env bash
# kill_stream.sh — bash wrapper that injects Plex creds + invokes kill_stream.py.
# Lives on the seedbox at ~/scripts/plex/kill_stream.sh.
set -euo pipefail

SECRETS_DIR="${SECRETS_DIR:-$HOME/secrets}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${LOG_DIR:-$HOME/.apps/stream-stats/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/kill_stream.log"

PLEX_HOST=$(tr -d '[:space:]' < "$SECRETS_DIR/plex.host")
PLEX_PORT=$(tr -d '[:space:]' < "$SECRETS_DIR/plex.port")
PLEX_TOKEN=$(tr -d '[:space:]' < "$SECRETS_DIR/plex.token")

export PLEX_URL="http://${PLEX_HOST}:${PLEX_PORT}"
export PLEX_TOKEN
# Pin plexapi client identifier so plex.tv doesn't fire "new Linux device" on every run.
# uuid.getnode() returns random MACs in container environments; a fixed identifier
# prevents 1-notification-per-invocation spam. Config created by 43-stream-stats-install.sh.
export PLEXAPI_CONFIG_PATH="${HOME}/.config/plexapi/config.ini"

VENV="$HOME/.apps/python-plexapi/venv/bin/python"
[ -x "$VENV" ] || { echo "missing python-plexapi venv at $VENV" >&2; exit 1; }

LOCKFILE="/tmp/kill_stream.lock"
exec 9>"$LOCKFILE"
flock -n 9 || { echo "$(date -Iseconds) skip — previous run still holding lock" >> "$LOG"; exit 0; }

echo "$(date -Iseconds) starting kill_stream.py $*" >> "$LOG"
"$VENV" "$HERE/kill_stream.py" "$@" 2>&1 | tee -a "$LOG"
