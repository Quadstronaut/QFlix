#!/usr/bin/env bash
# Install Sonarr2 (anime TV automation). Captures API key from config.xml + port from nginx proxy.d.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"
source "$HERE/install/lib/app-install.sh"

app_install sonarr2

# Wait briefly for config.xml to be written (Docker startup may take a few seconds)
for i in 1 2 3 4 5 6 7 8 9 10; do
  if sshm 'test -f ~/.apps/sonarr2/config.xml'; then break; fi
  log_info "waiting for config.xml... ($i/10)"
  sleep 6
done

app_capture_arr_key sonarr2
app_capture_port sonarr2

KEY="$(secret_read sonarr2.key 2>/dev/null || true)"
PORT="$(secret_read sonarr2.port 2>/dev/null || true)"
BASE="$(secret_read sonarr2.urlbase 2>/dev/null || echo sonarr2)"

if [ -z "$KEY" ] || [ -z "$PORT" ]; then
  log_warn "Could not capture sonarr2.key or sonarr2.port — manual capture may be needed"
else
  log_info "Verifying sonarr2 API at http://127.0.0.1:$PORT/$BASE/api/v3/system/status..."
  status="$(sshm "curl -sf -m 10 -H 'X-Api-Key: $KEY' http://127.0.0.1:$PORT/$BASE/api/v3/system/status" || true)"
  if [ -z "$status" ]; then
    log_warn "Sonarr2 API not responding yet — may still be starting up"
  else
    log_info "Sonarr2 healthy (port=$PORT base=$BASE)"
  fi
fi
