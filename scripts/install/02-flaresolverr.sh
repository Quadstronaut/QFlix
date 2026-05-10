#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"
source "$HERE/install/lib/app-install.sh"

if sshm "test -d ~/.apps/flaresolverr" 2>/dev/null; then
  log_info "flaresolverr already installed (~/.apps/flaresolverr exists); skipping install"
  PORT="17011"
else
  log_info "Installing flaresolverr via app-flaresolverr install (no -p flag; FlareSolverr has no admin password)"
  install_output="$(sshm "app-flaresolverr install")"
  log_info "Install response: $(printf '%s' "$install_output" | head -c 200)"
  PORT="$(printf '%s' "$install_output" | grep -oP '"port":\s*\K[0-9]+' || true)"
  if [ -z "$PORT" ]; then
    die "FlareSolverr install did not return a port number"
  fi
  log_info "flaresolverr install completed on port $PORT"
fi

secret_write flaresolverr.port "$PORT"
log_info "FlareSolverr port: $PORT (on Docker gateway 172.17.0.1)"

log_info "Health-checking FlareSolverr at http://172.17.0.1:$PORT/ ..."
body="$(sshm "curl -sf -m 5 http://172.17.0.1:$PORT/")" || die "FlareSolverr health endpoint did not respond on 172.17.0.1:$PORT"
log_info "Response: $(printf '%s' "$body" | head -c 200)"
printf '%s' "$body" | grep -qi flaresolverr || die "Health response doesn't mention FlareSolverr — wrong service on this port?"
log_info "FlareSolverr is healthy."
