#!/usr/bin/env bash
# Phase 41 — Upgradinatorr (bash rewrite). Re-search N stale grabs in the named
# *arr instance against the current quality profile + custom-format scoring.
# After Recyclarr changes scores, this is what actually upgrades existing
# files in the library.
#
# Adapted from Just-A-Bunch-Of-Starr-Scripts/Upgradinatorr (PowerShell, MIT).
# https://github.com/angrycuban13/Just-A-Bunch-Of-Starr-Scripts
#
# Usage: upgradinatorr.sh --app <sonarr|sonarr2|radarr|radarr2> [--count N] [--dry-run]
set -euo pipefail

SECRETS_DIR="${SECRETS_DIR:-$HOME/secrets}"
LOG_DIR="${LOG_DIR:-$HOME/.apps/upgradinatorr/logs}"
mkdir -p "$LOG_DIR"

APP=""
COUNT=5
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --count) COUNT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -n "$APP" ] || { echo "--app required (sonarr|sonarr2|radarr|radarr2)" >&2; exit 2; }

read_sec() { tr -d '[:space:]' < "$SECRETS_DIR/$1"; }
KEY=$(read_sec "${APP}.key")
PORT=$(read_sec "${APP}.port")
BASE=$(read_sec "${APP}.urlbase")

case "$APP" in
  sonarr|sonarr2|radarr|radarr2) API_VERSION=v3 ;;
  *) echo "unsupported app: $APP" >&2; exit 2 ;;
esac

URL="http://127.0.0.1:${PORT}/${BASE}/api/${API_VERSION}"
LOG="$LOG_DIR/${APP}.log"

log() { printf '%s [%s] %s\n' "$(date -Iseconds)" "$APP" "$*" | tee -a "$LOG" >&2; }

api() {
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -fsS -m 30 -X "$method" -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' -d "$data" "$URL/$path"
  else
    curl -fsS -m 30 -X "$method" -H "X-Api-Key: $KEY" "$URL/$path"
  fi
}

case "$APP" in
  sonarr|sonarr2)
    QUERY="wanted/cutoff?sortKey=episodes.lastSearchTime&sortDirection=ascending&pageSize=$COUNT"
    SEARCH_TMPL='{"name":"EpisodeSearch","episodeIds":[%s]}'
    ;;
  radarr|radarr2)
    QUERY="wanted/cutoff?sortKey=movieFile.dateAdded&sortDirection=ascending&pageSize=$COUNT"
    SEARCH_TMPL='{"name":"MoviesSearch","movieIds":[%s]}'
    ;;
esac

ITEMS_JSON=$(api GET "$QUERY")
IDS=$(echo "$ITEMS_JSON" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(",".join(str(r["id"]) for r in d.get("records",[])))')

if [ -z "$IDS" ]; then
  log "no items below cutoff — nothing to do"
  exit 0
fi

ITEM_COUNT=$(echo "$IDS" | tr ',' '\n' | wc -l)
log "found $ITEM_COUNT items below cutoff: $IDS"

if [ "$DRY_RUN" -eq 1 ]; then
  log "DRY RUN — would search items: $IDS"
  exit 0
fi

PAYLOAD=$(printf "$SEARCH_TMPL" "$IDS")
RESP=$(api POST "command" "$PAYLOAD")
COMMAND_ID=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))')
log "search command queued: id=$COMMAND_ID"

# Best-effort poll. *arr's command queue can take minutes. Don't fail if
# still running after 5 min — it'll finish async.
for _ in $(seq 1 30); do
  STATE=$(api GET "command/$COMMAND_ID" 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null || echo "")
  case "$STATE" in
    completed) log "command completed"; exit 0 ;;
    failed) log "command FAILED — check $APP logs"; exit 1 ;;
  esac
  sleep 10
done
log "command still running after 5 min — exiting (will finish async)"
exit 0
