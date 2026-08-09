#!/usr/bin/env bash
# Install an Ultra.cc app non-interactively. Replaces the (now-unneeded) expect wrapper:
# `app-<name> install` accepts `-p PASSWORD` as a CLI flag. Confirmed via:
#   ssh manitoba 'app-jellyfin install --help-all' → "Switches: -p, --password PASSWORD:str  Password for remote access; required"
# Some apps also accept --silent-install (e.g. jellyfin); --reuse-db skips DB wipe on re-install.
#
# Usage: app_install <name> [extra app-install flags...]
#   secrets/shared-admin.password must be present.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

app_install() {
  local app="$1"; shift
  local password
  password="$(secret_read shared-admin.password)"

  if sshm "test -d ~/.apps/$app" 2>/dev/null; then
    log_info "$app already installed (~/.apps/$app exists); skipping install"
    return 0
  fi

  log_info "Installing $app via app-$app install -p ******** $*"
  sshm "app-$app install -p '$password' $*"
  log_info "$app install completed"
}

# Capture the post-install port from nginx proxy.d (re-uses the bootstrap-discover convention).
app_capture_port() {
  local app="$1"
  local port
  port="$(sshm "grep -hoE 'proxy_pass[[:space:]]+http://127\\.0\\.0\\.1:[0-9]+' ~/.apps/nginx/proxy.d/$app.conf 2>/dev/null | head -1 | grep -oE '[0-9]+\$'" || true)"
  if [ -n "$port" ]; then
    secret_write "$app.port" "$port"
    log_info "$app port: $port"
  else
    log_warn "$app: nginx proxy.d/$app.conf has no proxy_pass; port not captured"
  fi
}

# Capture API key for *arr apps from config.xml.
app_capture_arr_key() {
  local app="$1"
  local cfg
  cfg="$(sshm "cat ~/.apps/$app/config.xml 2>/dev/null" || true)"
  [ -z "$cfg" ] && { log_warn "$app: no config.xml"; return 1; }
  local key base
  key="$(printf '%s' "$cfg" | grep -oP '(?<=<ApiKey>)[^<]+')"
  base="$(printf '%s' "$cfg" | grep -oP '(?<=<UrlBase>)[^<]+' || true)"
  [ -n "$key" ] && secret_write "$app.key" "$key"
  [ -n "$base" ] && secret_write "$app.urlbase" "${base#/}"
  log_info "$app: key=${key:0:8}... base=${base:-(none)}"
}
