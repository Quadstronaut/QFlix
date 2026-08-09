#!/usr/bin/env bash
# Phase 8.9 + 8.10: Deploy library-rescan helper to manitoba.
# Mylar3 + Readarr's "extra_scripts" / Connect entries call this on import.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

# 1. Push secrets to a manitoba-side cache the helper can read
sshm 'mkdir -p ~/.opt/secrets ~/scripts/post-import'
for s in komga.key komga.user komga.port \
         kavita.key kavita.port \
         audiobookshelf.key audiobookshelf.port \
         calibre-web.port; do
  if [ -f "$SECRETS_DIR/$s" ]; then
    scpm_to "$SECRETS_DIR/$s" "/home/quadstronaut/.opt/secrets/$s"
    sshm "chmod 600 ~/.opt/secrets/$s"
  fi
done

# 2. Push the helper script
scpm_to "$HERE/post-import/library-rescan.sh" "/home/quadstronaut/scripts/post-import/library-rescan.sh"
sshm 'chmod +x ~/scripts/post-import/library-rescan.sh'

# 3. Smoke test: trigger Komga + Kavita rescan from the helper
log_info "Smoke test: trigger each library rescan..."
for target in komga kavita audiobookshelf calibre-web; do
  out=$(sshm "~/scripts/post-import/library-rescan.sh $target 2>&1" || true)
  log_info "  $target -> $(printf '%s' "$out" | head -c 120)"
done
