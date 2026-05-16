#!/usr/bin/env bash
# Mobile-UX canary: the public Homarr board renders, root-domain redirects
# correctly, and the page is small enough to load on mobile (<500KB HTML).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
HTPW=$(cat ~/secrets/htpasswd.password)
PUBLIC_HOST=$(cat ~/secrets/seedbox.host 2>/dev/null) || { echo "FAIL: ~/secrets/seedbox.host missing" >&2; exit 1; }
[ -n "$PUBLIC_HOST" ] || { echo "FAIL: ~/secrets/seedbox.host empty" >&2; exit 1; }
# Per-app subdomain prefix is "<app>-<userpart>" where userpart = first dot-segment
USERPART=${PUBLIC_HOST%%.*}
DOMAIN=${PUBLIC_HOST#*.}
HOMARR_HOST="homarr-upstream-${USERPART}.${DOMAIN}"
# Root domain (302 → public board)
ROOT_CODE=$(curl -sk -m 5 -o /dev/null -w "%{http_code}" -u "quadstronaut:$HTPW" "https://${PUBLIC_HOST}/")
ROOT_LOC=$(curl -sk -m 5 -I -u "quadstronaut:$HTPW" "https://${PUBLIC_HOST}/" | grep -i ^location: | tr -d "\r" | head -1)
# Public board renders
PUB_CODE=$(curl -sk -m 5 -o /dev/null -w "%{http_code}" -u "quadstronaut:$HTPW" "https://${HOMARR_HOST}/boards/public")
# HTML size
HTML_BYTES=$(curl -sk -m 10 -u "quadstronaut:$HTPW" "https://${HOMARR_HOST}/boards/public" | wc -c)
echo "ROOT_CODE=$ROOT_CODE"
echo "ROOT_LOC=$ROOT_LOC"
echo "PUB_CODE=$PUB_CODE"
echo "HTML_BYTES=$HTML_BYTES"
')
echo "$RES"
ROOT_CODE=$(echo "$RES" | grep -oE 'ROOT_CODE=[0-9]+' | cut -d= -f2)
PUB_CODE=$(echo "$RES" | grep -oE 'PUB_CODE=[0-9]+' | cut -d= -f2)
HTML_BYTES=$(echo "$RES" | grep -oE 'HTML_BYTES=[0-9]+' | cut -d= -f2)

[ "${ROOT_CODE:-0}" = 302 ] || { echo "FAIL: root domain expected 302, got $ROOT_CODE" >&2; exit 1; }
[ "${PUB_CODE:-0}" = 200 ] || { echo "FAIL: public board expected 200, got $PUB_CODE" >&2; exit 1; }
[ "${HTML_BYTES:-0}" -lt 524288 ] || { echo "FAIL: public-board HTML > 512KB ($HTML_BYTES)" >&2; exit 1; }
echo "PASS: mobile-ux canary — root 302, public 200, html $HTML_BYTES bytes"
