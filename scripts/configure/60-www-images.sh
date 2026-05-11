#!/usr/bin/env bash
# Phase 25 — ~/www/images/ self-hosted brand-asset path. Idempotent.
#
# Stands up:
#   ~/www/images/             (mode 0755)
#   ~/www/images/Q.png        (mode 0644, copied from repo root)
#   ~/www/images/_blank.png   (mode 0644, error_page mask)
#   ~/.apps/nginx/proxy.d/qflix-images.conf
#
# Also patches ~/.apps/nginx/nginx.conf to add `server_tokens off;` inside the
# existing http {} block, suppressing the nginx version in headers + default
# error pages. This affects the entire user-nginx (right blast radius — no
# app currently relies on the version header).
#
# Smoke-tests the public URL after restart.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# ── Step 1: copy assets to seedbox ──────────────────────────────────────────
log "deploying brand assets to ~/www/images/"
sshm 'mkdir -p ~/www/images && chmod 755 ~/www/images'
scpm_to "$REPO_ROOT/Q.png"               "~/www/images/Q.png"
scpm_to "$REPO_ROOT/scripts/data/_blank.png" "~/www/images/_blank.png"
sshm 'chmod 644 ~/www/images/*.png'

# ── Step 2: deploy nginx fragment ───────────────────────────────────────────
log "deploying nginx fragment"
scpm_to "$REPO_ROOT/scripts/data/qflix-images.conf" "~/.apps/nginx/proxy.d/qflix-images.conf"

# ── Step 3: ensure server_tokens off in nginx.conf (idempotent) ─────────────
log "patching nginx.conf for server_tokens off"
sshm 'bash -s' <<'REMOTE'
set -euo pipefail
CFG=$HOME/.apps/nginx/nginx.conf
if grep -qE '^\s*server_tokens\s+off;' "$CFG"; then
  echo "server_tokens off already present — nothing to do"
else
  cp "$CFG" "$CFG.bak.$(date +%s)"
  # Insert at the top of the first http { block.
  awk '
    /^[[:space:]]*http[[:space:]]*\{/ && !done {
      print
      print "    server_tokens off;"
      done = 1
      next
    }
    { print }
  ' "$CFG" > "$CFG.new"
  mv "$CFG.new" "$CFG"
  echo "added server_tokens off to nginx.conf"
fi
REMOTE

# ── Step 4: restart user-nginx ──────────────────────────────────────────────
log "restarting user-nginx"
sshm 'app-nginx restart'
sleep 5

# ── Step 5: smoke tests ─────────────────────────────────────────────────────
log "smoke tests"
PUB_HOST=$(cat "$REPO_ROOT/secrets/seedbox.host" 2>/dev/null || echo "quadstronaut.seedbox.example.com")

# Positive: Q.png returns 200.
HTTP=$(curl -s -o /dev/null -w '%{http_code}' "https://$PUB_HOST/images/Q.png")
if [ "$HTTP" != "200" ]; then
  echo "FAIL: Q.png expected 200, got $HTTP" >&2
  exit 1
fi
echo "  PASS: GET /images/Q.png → 200"

# Cache-Control header present.
if ! curl -sI "https://$PUB_HOST/images/Q.png" | grep -qi 'cache-control:.*immutable'; then
  echo "FAIL: Cache-Control immutable header not present on Q.png" >&2
  exit 1
fi
echo "  PASS: Cache-Control immutable present"

# Negative #1: directory listing returns 404.
HTTP=$(curl -s -o /dev/null -w '%{http_code}' "https://$PUB_HOST/images/")
if [ "$HTTP" != "404" ]; then
  echo "FAIL: /images/ expected 404, got $HTTP" >&2
  exit 1
fi
echo "  PASS: GET /images/ → 404"

# Negative #2: non-image extension returns 404.
HTTP=$(curl -s -o /dev/null -w '%{http_code}' "https://$PUB_HOST/images/foo.txt")
if [ "$HTTP" != "404" ]; then
  echo "FAIL: /images/foo.txt expected 404, got $HTTP" >&2
  exit 1
fi
echo "  PASS: GET /images/foo.txt → 404"

# Hardening: nginx version not exposed in Server header.
if curl -sI "https://$PUB_HOST/images/Q.png" | grep -iE '^server:.*nginx/[0-9]'; then
  echo "FAIL: nginx version visible in Server header" >&2
  exit 1
fi
echo "  PASS: nginx version not exposed (server_tokens off)"

log "deploy + smoke complete"
