#!/usr/bin/env bash
# Library rescan helper. Called by Mylar3 (extra_scripts) and Readarr (Custom
# Script Connect) on successful import. Best-effort: silently skips when API
# keys/ports aren't yet captured.
#
# Usage:
#   library-rescan.sh komga
#   library-rescan.sh kavita
#   library-rescan.sh audiobookshelf
#   library-rescan.sh calibre-web
set -uo pipefail

TARGET="${1:-}"
SECRETS=/home/quadstronaut/.opt/secrets

read_secret() {
  local f="$SECRETS/$1"
  [ -f "$f" ] || return 1
  tr -d '[:space:]' < "$f"
}

case "$TARGET" in
  komga)
    PORT=$(read_secret komga.port) || exit 0
    KEY=$(read_secret komga.key)   || exit 0
    # Komga: X-API-Key header, base path /komga/, no bulk endpoint — POST scan per library.
    LIBS=$(curl -sf --max-time 10 -H "X-API-Key: $KEY" \
      "http://172.17.0.1:$PORT/komga/api/v1/libraries" 2>/dev/null) || { echo "komga list failed"; exit 0; }
    for id in $(printf '%s' "$LIBS" | python3 -c 'import json,sys; [print(l["id"]) for l in json.load(sys.stdin)]' 2>/dev/null); do
      curl -sf --max-time 10 -X POST -H "X-API-Key: $KEY" \
        "http://172.17.0.1:$PORT/komga/api/v1/libraries/$id/scan" >/dev/null && \
        echo "komga scan $id triggered" || echo "komga scan $id failed"
    done
    ;;
  kavita)
    PORT=$(read_secret kavita.port) || exit 0
    KEY=$(read_secret kavita.key)   || exit 0
    # Kavita: ?apiKey=<>; POST /api/Library/scan-all
    curl -sf --max-time 10 -X POST \
      "http://172.17.0.1:$PORT/api/Library/scan-all?apiKey=$KEY" >/dev/null && \
      echo "kavita rescan triggered" || echo "kavita rescan failed"
    ;;
  audiobookshelf)
    PORT=$(read_secret audiobookshelf.port) || exit 0
    KEY=$(read_secret audiobookshelf.key)   || exit 0
    # ABS: Bearer token; POST /api/libraries/{id}/scan per library
    LIBS=$(curl -sf --max-time 10 -H "Authorization: Bearer $KEY" \
      "http://172.17.0.1:$PORT/api/libraries" 2>/dev/null) || { echo "abs list failed"; exit 0; }
    for id in $(printf '%s' "$LIBS" | python3 -c 'import json,sys; [print(l["id"]) for l in json.load(sys.stdin).get("libraries",[])]' 2>/dev/null); do
      curl -sf --max-time 10 -X POST -H "Authorization: Bearer $KEY" \
        "http://172.17.0.1:$PORT/api/libraries/$id/scan" >/dev/null && \
        echo "abs scan $id triggered" || echo "abs scan $id failed"
    done
    ;;
  calibre-web)
    PORT=$(read_secret calibre-web.port) || exit 0
    # Calibre-Web has no public API key — rescan via session-auth admin URL.
    # Not feasible from a script without storing credentials. Best-effort only.
    curl -sf --max-time 5 "http://172.17.0.1:$PORT/calibre-web/" >/dev/null 2>&1 && \
      echo "calibre-web reachable (no scan trigger available)" || echo "calibre-web unreachable"
    ;;
  *)
    echo "usage: $0 {komga|kavita|audiobookshelf|calibre-web}" >&2
    exit 2
    ;;
esac
