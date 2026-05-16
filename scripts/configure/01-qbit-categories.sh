#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

QBIT_USER="$(secret_read qbittorrent.user)"
QBIT_PASS="$(secret_read qbittorrent.password)"
QBIT_PORT="$(secret_read qbittorrent.port)"
QBIT_URL="http://127.0.0.1:$QBIT_PORT"

log_info "Configuring qBit categories on manitoba (running curl remotely)..."

sshm bash -s "$QBIT_USER" "$QBIT_PASS" "$QBIT_URL" <<'REMOTE'
set -euo pipefail
QBIT_USER="$1"; QBIT_PASS="$2"; QBIT_URL="$3"
COOKIE="$(mktemp)"
trap 'rm -f "$COOKIE"' EXIT

# Try 127.0.0.1 first; fall back to bond0.27 if it fails
if ! curl -sSf -c "$COOKIE" --data-urlencode "username=$QBIT_USER" --data-urlencode "password=$QBIT_PASS" "$QBIT_URL/api/v2/auth/login" 2>/dev/null | grep -q "Ok."; then
  echo "127.0.0.1 auth failed; trying public bond IP" >&2
  PUBLIC_IP="$(ip -4 addr show bond0.27 | awk '/inet /{split($2,a,"/"); print a[1]; exit}')"
  QBIT_URL="http://$PUBLIC_IP:${QBIT_URL##*:}"
  curl -sSf -c "$COOKIE" --data-urlencode "username=$QBIT_USER" --data-urlencode "password=$QBIT_PASS" "$QBIT_URL/api/v2/auth/login" | grep -q "Ok." || { echo "qBit auth failed on both endpoints"; exit 1; }
fi
echo "Auth OK at $QBIT_URL"

declare -A cats=(
  [radarr-anime]='/home/quadstronaut/downloads/qbittorrent/radarr-anime'
  [sonarr-anime]='/home/quadstronaut/downloads/qbittorrent/sonarr-anime'
  # readarr / mylar categories removed 2026-05-15 — both apps purged
  # 2026-05-11. Re-add when/if those apps are reinstalled.
)

for cat in "${!cats[@]}"; do
  echo "  -> creating category $cat at ${cats[$cat]}"
  curl -sS -b "$COOKIE" \
    --data-urlencode "category=$cat" \
    --data-urlencode "savePath=${cats[$cat]}" \
    "$QBIT_URL/api/v2/torrents/createCategory" >/dev/null
done

echo "--- final category list:"
curl -sS -b "$COOKIE" "$QBIT_URL/api/v2/torrents/categories" > /tmp/cats.json
python3 << PYSCRIPT
import json
with open('/tmp/cats.json') as f:
    d = json.load(f)
for name in sorted(d.keys()):
    path = d[name].get('savePath', 'N/A')
    print(f"  {name} -> {path}")
PYSCRIPT
REMOTE
