#!/usr/bin/env bash
# Install Radarr2 (anime movie automation). Captures API key from config.xml + port from nginx proxy.d.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"
source "$HERE/install/lib/app-install.sh"

app_install radarr2

for i in 1 2 3 4 5 6 7 8 9 10; do
  if sshm 'test -f ~/.apps/radarr2/config.xml'; then break; fi
  log_info "waiting for config.xml... ($i/10)"
  sleep 6
done

app_capture_arr_key radarr2
app_capture_port radarr2

KEY="$(secret_read radarr2.key 2>/dev/null || true)"
PORT="$(secret_read radarr2.port 2>/dev/null || true)"
BASE="$(secret_read radarr2.urlbase 2>/dev/null || echo radarr2)"

if [ -n "$KEY" ] && [ -n "$PORT" ]; then
  log_info "Verifying radarr2 API at http://127.0.0.1:$PORT/$BASE/api/v3/system/status..."
  status="$(sshm "curl -sf -m 10 -H 'X-Api-Key: $KEY' http://127.0.0.1:$PORT/$BASE/api/v3/system/status" || true)"
  if [ -z "$status" ]; then
    log_warn "Radarr2 API not responding yet — may still be starting up"
  else
    log_info "Radarr2 healthy (port=$PORT base=$BASE)"
  fi
else
  log_warn "Radarr2 key/port not captured — manual capture may be needed"
fi
