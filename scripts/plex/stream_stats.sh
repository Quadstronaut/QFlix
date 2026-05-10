#!/usr/bin/env bash
# stream_stats.sh — bash wrapper that injects Plex creds + invokes stream_stats.py.
# Mirrors kill_stream.sh in env-handling, locking, and log layout.
set -euo pipefail

SECRETS_DIR="${SECRETS_DIR:-$HOME/secrets}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${LOG_DIR:-$HOME/.apps/stream-stats/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/stream_stats.log"

PLEX_HOST=$(tr -d '[:space:]' < "$SECRETS_DIR/plex.host")
PLEX_PORT=$(tr -d '[:space:]' < "$SECRETS_DIR/plex.port")
PLEX_TOKEN=$(tr -d '[:space:]' < "$SECRETS_DIR/plex.token")

export PLEX_URL="http://${PLEX_HOST}:${PLEX_PORT}"
export PLEX_TOKEN
# Pin plexapi client identifier — see kill_stream.sh comment for rationale.
export PLEXAPI_CONFIG_PATH="${HOME}/.config/plexapi/config.ini"

VENV="$HOME/.apps/python-plexapi/venv/bin/python"
[ -x "$VENV" ] || { echo "missing python-plexapi venv at $VENV" >&2; exit 1; }

LOCKFILE="/tmp/stream_stats.lock"
exec 9>"$LOCKFILE"
flock -n 9 || exit 0

"$VENV" "$HERE/stream_stats.py" "$@" 2>>"$LOG"
