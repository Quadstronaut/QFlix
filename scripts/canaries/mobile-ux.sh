#!/usr/bin/env bash
# Mobile-UX canary: the public root serves the QFlix Dashboard DIRECTLY (200 +
# the `data-qflix-dash` marker) and the page is small enough to load on mobile
# (<512KB HTML). Repointed 2026-06-27 from the retired Homarr board (which used
# a root 302 -> homarr-upstream board) to the SvelteKit dashboard at root.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
PUBLIC_HOST=$(cat ~/secrets/seedbox.host 2>/dev/null) || { echo "FAIL: ~/secrets/seedbox.host missing" >&2; exit 1; }
[ -n "$PUBLIC_HOST" ] || { echo "FAIL: ~/secrets/seedbox.host empty" >&2; exit 1; }
ROOT_CODE=$(curl -sk -m 8 -o /dev/null -w "%{http_code}" "https://${PUBLIC_HOST}/")
BODY=$(curl -sk -m 10 "https://${PUBLIC_HOST}/")
HTML_BYTES=$(printf "%s" "$BODY" | wc -c)
MARKER=$(printf "%s" "$BODY" | grep -o "data-qflix-dash" | head -1)
echo "ROOT_CODE=$ROOT_CODE"
echo "HTML_BYTES=$HTML_BYTES"
echo "MARKER=$MARKER"
')
echo "$RES"
ROOT_CODE=$(echo "$RES" | grep -oE 'ROOT_CODE=[0-9]+' | cut -d= -f2)
HTML_BYTES=$(echo "$RES" | grep -oE 'HTML_BYTES=[0-9]+' | cut -d= -f2)
MARKER=$(echo "$RES" | grep -oE 'MARKER=data-qflix-dash' | cut -d= -f2)

[ "${ROOT_CODE:-0}" = 200 ] || { echo "FAIL: root expected 200, got $ROOT_CODE" >&2; exit 1; }
[ "${MARKER:-}" = "data-qflix-dash" ] || { echo "FAIL: dashboard marker not found at root" >&2; exit 1; }
[ "${HTML_BYTES:-0}" -lt 524288 ] || { echo "FAIL: root HTML > 512KB ($HTML_BYTES)" >&2; exit 1; }
echo "PASS: mobile-ux canary — root 200 + dashboard marker, html $HTML_BYTES bytes"
