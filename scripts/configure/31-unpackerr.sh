#!/usr/bin/env bash
# Phase 11: Render Unpackerr TOML from template + push + start service.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

TMPL="$HERE/data/unpackerr.conf.tmpl"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

sed \
  -e "s|{{SONARR_PORT}}|$(secret_read sonarr.port)|g" \
  -e "s|{{SONARR_BASE}}|$(secret_read sonarr.urlbase 2>/dev/null || echo sonarr)|g" \
  -e "s|{{SONARR_KEY}}|$(secret_read sonarr.key)|g" \
  -e "s|{{SONARR2_PORT}}|$(secret_read sonarr2.port)|g" \
  -e "s|{{SONARR2_BASE}}|$(secret_read sonarr2.urlbase 2>/dev/null || echo sonarr2)|g" \
  -e "s|{{SONARR2_KEY}}|$(secret_read sonarr2.key)|g" \
  -e "s|{{RADARR_PORT}}|$(secret_read radarr.port)|g" \
  -e "s|{{RADARR_BASE}}|$(secret_read radarr.urlbase 2>/dev/null || echo radarr)|g" \
  -e "s|{{RADARR_KEY}}|$(secret_read radarr.key)|g" \
  -e "s|{{RADARR2_PORT}}|$(secret_read radarr2.port)|g" \
  -e "s|{{RADARR2_BASE}}|$(secret_read radarr2.urlbase 2>/dev/null || echo radarr2)|g" \
  -e "s|{{RADARR2_KEY}}|$(secret_read radarr2.key)|g" \
  -e "s|{{READARR_PORT}}|$(secret_read readarr.port)|g" \
  -e "s|{{READARR_BASE}}|$(secret_read readarr.urlbase 2>/dev/null || echo readarr)|g" \
  -e "s|{{READARR_KEY}}|$(secret_read readarr.key)|g" \
  "$TMPL" > "$OUT"

log_info "Backing up existing config + pushing new..."
sshm 'cp ~/.apps/unpackerr/unpackerr.conf ~/.apps/unpackerr/unpackerr.conf.bak.$(date +%Y%m%d) 2>/dev/null || true'
scpm_to "$OUT" "/home/quadstronaut/.apps/unpackerr/unpackerr.conf"
sshm 'chmod 600 ~/.apps/unpackerr/unpackerr.conf'

log_info "Restarting unpackerr..."
sshm 'app-unpackerr restart 2>&1 || app-unpackerr start 2>&1' | head -5
sleep 8

log_info "Service status:"
sshm 'systemctl --user is-active unpackerr 2>&1'

log_info "Tail of log:"
sshm 'tail -20 ~/.apps/unpackerr/unpackerr.log 2>/dev/null'
