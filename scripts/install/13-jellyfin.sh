#!/usr/bin/env bash
# Install Jellyfin. Port comes from app-jellyfin install JSON output and/or nginx proxy.d.
# First-run wizard remains operator-manual; this script just gets the binary listening.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"
source "$HERE/install/lib/app-install.sh"

app_install jellyfin --silent-install

# Wait for Jellyfin to start listening
PORT="$(sshm 'grep -hoE "proxy_pass[[:space:]]+http://127\.0\.0\.1:[0-9]+" ~/.apps/nginx/proxy.d/jellyfin.conf 2>/dev/null | head -1 | grep -oE "[0-9]+\$"' || true)"
if [ -z "$PORT" ]; then
  log_warn "Jellyfin nginx proxy port not found; check installation"
  exit 1
fi
secret_write jellyfin.port "$PORT"
log_info "Jellyfin port: $PORT"

# Health check
for i in 1 2 3 4 5 6 7 8 9 10; do
  status="$(sshm "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/jellyfin/System/Info/Public" || echo 000)"
  if [ "$status" = "200" ]; then
    log_info "Jellyfin healthy at http://127.0.0.1:$PORT/jellyfin/"
    break
  fi
  log_info "  waiting for Jellyfin to come up... ($i/10, last code=$status)"
  sleep 6
done

# Check if first-run wizard is needed
startup_state="$(sshm "curl -s -m 5 http://127.0.0.1:$PORT/jellyfin/Startup/Configuration" || true)"
if [ -n "$startup_state" ] && printf '%s' "$startup_state" | grep -q "Locale\|MetadataCountry"; then
  log_warn "Jellyfin first-run wizard NOT YET completed."
  log_warn "Either:"
  log_warn "  (a) Operator: browse https://quadstronaut.seedbox.example.com/jellyfin/ and complete wizard manually"
  log_warn "  (b) Run scripts/install/13b-jellyfin-autosetup.sh to complete via API (experimental)"
fi
