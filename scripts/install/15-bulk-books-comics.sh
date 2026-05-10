#!/usr/bin/env bash
# Phase 8: bulk-install Readarr + Mylar3 + Komga + Kavita + Calibre-Web + Audiobookshelf
# All 6 accept -p PASSWORD. Sequential to avoid Docker concurrency issues.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"
source "$HERE/install/lib/app-install.sh"

for app in readarr mylar3 komga kavita calibre-web audiobookshelf; do
  log_info "------ $app ------"
  app_install "$app"
  app_capture_port "$app"
done

log_info "------ summary ------"
for app in readarr mylar3 komga kavita calibre-web audiobookshelf; do
  port="$(secret_read "$app.port" 2>/dev/null || echo "?")"
  log_info "  $app: port=$port"
done
