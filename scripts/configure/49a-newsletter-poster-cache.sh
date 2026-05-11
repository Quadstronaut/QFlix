#!/usr/bin/env bash
# Phase 24a — qflix newsletter poster cache + daily prune timer. Idempotent.
#
# Stands up:
#   ~/www/images/newsletter/                 (mode 0755, served by /images/)
#   ~/.config/systemd/user/qflix-poster-cache-prune.{service,timer}
#
# Smoke-tests:
#   - cache dir is writable + served at https://<public_host>/images/newsletter/
#   - timer is enabled and active
#
# Depends on:
#   - scripts/configure/60-www-images.sh (provides the /images/ nginx route)
#   - scripts/configure/49-qflix-newsletter-install.sh (provides qflix-newsletter)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# ── Step 1: create the cache dir ────────────────────────────────────────────
log_info "creating ~/www/images/newsletter/"
sshm 'mkdir -p ~/www/images/newsletter && chmod 755 ~/www/images/newsletter'

# ── Step 2: deploy systemd units ────────────────────────────────────────────
log_info "deploying prune timer + service"
scpm_to "$REPO_ROOT/scripts/maint/systemd/qflix-poster-cache-prune.timer"   '~/.config/systemd/user/qflix-poster-cache-prune.timer'   >/dev/null
scpm_to "$REPO_ROOT/scripts/maint/systemd/qflix-poster-cache-prune.service" '~/.config/systemd/user/qflix-poster-cache-prune.service' >/dev/null
sshm 'systemctl --user daemon-reload'
sshm 'systemctl --user enable --now qflix-poster-cache-prune.timer'

# ── Step 3: smoke — write a probe and serve it ──────────────────────────────
log_info "smoke test: write probe + serve via nginx"
PUB_HOST=$(cat "$REPO_ROOT/secrets/seedbox.host" 2>/dev/null || echo "quadstronaut.seedbox.example.com")

# Recognizable 16-char hex probe filename matching the SHA pattern.
PROBE_SHA="deadbeefcafef00d"
sshm "printf '\xff\xd8\xff\xe0\x00\x00\x00\x00\x00\x00\x00\x00\x00' > ~/www/images/newsletter/${PROBE_SHA}.jpg"

HTTP=$(curl -s -o /dev/null -w '%{http_code}' "https://$PUB_HOST/images/newsletter/${PROBE_SHA}.jpg")
if [ "$HTTP" != "200" ]; then
  echo "FAIL: probe expected 200, got $HTTP" >&2
  exit 1
fi
echo "  PASS: GET /images/newsletter/${PROBE_SHA}.jpg → 200"

# Cache-Control immutable (inherited from /images/ config).
if ! curl -sI "https://$PUB_HOST/images/newsletter/${PROBE_SHA}.jpg" | grep -qi 'cache-control:.*immutable'; then
  echo "FAIL: Cache-Control immutable not present on probe" >&2
  exit 1
fi
echo "  PASS: Cache-Control immutable present"

# Clean up probe.
sshm "rm -f ~/www/images/newsletter/${PROBE_SHA}.jpg"

# ── Step 4: verify timer is enabled + active ────────────────────────────────
log_info "verifying timer state"
sshm 'systemctl --user list-timers --no-pager | grep -q qflix-poster-cache-prune || (echo "FAIL: timer not in list-timers" >&2 ; exit 1)'
echo "  PASS: timer is loaded"

log_info "Phase 24a complete — poster cache armed; next prune fires at 00:00 UTC"
log_info "Manual prune: ssh quadstronaut@seedbox 'systemctl --user start qflix-poster-cache-prune.service'"
