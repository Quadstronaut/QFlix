#!/usr/bin/env bash
# Configure Mylar3: roots Comics+Manga, qBittorrent client, generate API key.
# Mylar3 is INI-based (~/.apps/mylar3/mylar/config.ini); patched via crudini.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

QBIT_USER="$(secret_read qbittorrent.user)"
QBIT_PASS="$(secret_read qbittorrent.password)"
SLOT_HOST="quadstronaut.seedbox.example.com"

sshm "QBIT_USER='$QBIT_USER' QBIT_PASS='$QBIT_PASS' SLOT_HOST='$SLOT_HOST' bash -s" <<'REMOTE'
set -euo pipefail
CFG=~/.apps/mylar3/mylar/config.ini
[ -f "$CFG.bak.$(date +%Y%m%d)" ] || cp "$CFG" "$CFG.bak.$(date +%Y%m%d)"

CRUDINI=$(which crudini || echo "$HOME/.local/bin/crudini")
[ -x "$CRUDINI" ] || { echo "crudini not on PATH"; exit 1; }

# Find which section holds qbittorrent_* keys (usually [Torrents] or [qBittorrent])
SECTION_FOR_QBIT=$(awk '/^\[/{s=$0} /^qbittorrent_host/{print s; exit}' "$CFG" | tr -d '[]')
SECTION_FOR_TORRENT=$(awk '/^\[/{s=$0} /^enable_torrents/{print s; exit}' "$CFG" | tr -d '[]')
SECTION_FOR_GENERAL=$(awk '/^\[/{s=$0} /^destination_dir/{print s; exit}' "$CFG" | tr -d '[]')
SECTION_FOR_API=$(awk '/^\[/{s=$0} /^api_key/{print s; exit}' "$CFG" | tr -d '[]')
echo "  sections: general=$SECTION_FOR_GENERAL torrents=$SECTION_FOR_TORRENT qbit=$SECTION_FOR_QBIT api=$SECTION_FOR_API"

# Settings
"$CRUDINI" --set "$CFG" "$SECTION_FOR_GENERAL" download_dir   "/home/quadstronaut/downloads/qbittorrent/mylar"
"$CRUDINI" --set "$CFG" "$SECTION_FOR_GENERAL" destination_dir "/home/quadstronaut/media/Comics"
"$CRUDINI" --set "$CFG" "$SECTION_FOR_GENERAL" manga_dir       "/home/quadstronaut/media/Manga"
"$CRUDINI" --set "$CFG" "$SECTION_FOR_GENERAL" enforce_perms   "False"

"$CRUDINI" --set "$CFG" "$SECTION_FOR_TORRENT" enable_torrents       "True"
"$CRUDINI" --set "$CFG" "$SECTION_FOR_TORRENT" enable_torrent_search "True"

"$CRUDINI" --set "$CFG" "$SECTION_FOR_QBIT" qbittorrent_host     "https://$SLOT_HOST/qbittorrent"
"$CRUDINI" --set "$CFG" "$SECTION_FOR_QBIT" qbittorrent_port     "443"
"$CRUDINI" --set "$CFG" "$SECTION_FOR_QBIT" qbittorrent_username "$QBIT_USER"
"$CRUDINI" --set "$CFG" "$SECTION_FOR_QBIT" qbittorrent_password "$QBIT_PASS"
"$CRUDINI" --set "$CFG" "$SECTION_FOR_QBIT" qbittorrent_label    "mylar"

# Generate or read API key
CUR_KEY=$("$CRUDINI" --get "$CFG" "$SECTION_FOR_API" api_key 2>/dev/null || echo "")
if [ -z "$CUR_KEY" ] || [ "$CUR_KEY" = "None" ]; then
  NEW_KEY=$(openssl rand -hex 16)
  "$CRUDINI" --set "$CFG" "$SECTION_FOR_API" api_key "$NEW_KEY"
  echo "  generated new mylar3 api_key"
else
  echo "  existing mylar3 api_key preserved"
fi

# Read back the API key for the controller
echo "MYLAR3_API_KEY=$("$CRUDINI" --get "$CFG" "$SECTION_FOR_API" api_key)"

# Restart to load new config
echo "  restarting mylar3..."
app-mylar3 restart 2>&1 | tail -3
sleep 5
REMOTE

# Capture API key locally
KEY=$(sshm "grep -A1 '^\[' ~/.apps/mylar3/mylar/config.ini | grep '^api_key' | head -1 | cut -d= -f2 | tr -d '[:space:]'" 2>/dev/null || true)
[ -z "$KEY" ] && KEY=$(sshm "$HOME/.local/bin/crudini --get ~/.apps/mylar3/mylar/config.ini API api_key 2>/dev/null || \$HOME/.local/bin/crudini --get ~/.apps/mylar3/mylar/config.ini General api_key" 2>/dev/null || true)
[ -n "$KEY" ] && [ "$KEY" != "None" ] && secret_write mylar3.key "$KEY" && log_info "mylar3.key captured (${KEY:0:8}...)" || log_warn "mylar3.key not captured — check ~/.apps/mylar3/mylar/config.ini"

# Reachability test
PORT="$(secret_read mylar3.port)"
status=$(sshm "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/mylar3/" 2>/dev/null || echo 000)
log_info "mylar3 reachability at http://127.0.0.1:$PORT/mylar3/ → HTTP $status"
