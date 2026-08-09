#!/usr/bin/env bash
# Phase 24 — qflix-newsletter install. Idempotent.
#
# Replaces Conjurr (Gemini AI rec engine) + Newsletterr (Tautulli weekly digest).
# Standalone Python script triggered by a Mon-08:00 systemd timer; reads
# ~/secrets/* at runtime; posts a Listmonk campaign per send.
#
#  - Reuse Astral python-build-standalone Python 3.11 from Phase 22
#  - rsync scripts/qflix-newsletter/ to ~/.apps/qflix-newsletter/
#  - venv + requirements
#  - deploy systemd service + timer (scripts/maint/systemd/qflix-newsletter.*)
#  - dry-run smoke before enabling timer
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

# ── Step 1: assert Python 3.11 (PBS) is present ─────────────────────────────
sshm 'PY=$HOME/.local/python311/bin/python3.11; [ -x "$PY" ] || { echo "FATAL: python3.11 not found — run scripts/configure/47-... or install python-build-standalone"; exit 1; }; $PY --version'

# ── Step 2: deploy required secrets to seedbox ──────────────────────────────
# tmdb keys are local-only currently; Listmonk + Tautulli + arr keys may
# already be present from earlier phases. Pushing all of them is idempotent.
sshm 'mkdir -p ~/secrets && chmod 700 ~/secrets'
for f in tautulli.key tautulli.port \
         sonarr.key sonarr.port sonarr.urlbase \
         radarr.key radarr.port radarr.urlbase \
         tmdb.api_key tmdb.read_token \
         listmonk.api_user listmonk.api_token; do
  if [ -f "secrets/$f" ]; then
    scpm_to "secrets/$f" "secrets/$f" >/dev/null
  fi
done
# Optional anime *arr secrets (only if present locally)
for f in sonarr2.key sonarr2.port sonarr2.urlbase radarr2.key radarr2.port radarr2.urlbase; do
  if [ -f "secrets/$f" ]; then
    scpm_to "secrets/$f" "secrets/$f" >/dev/null
  fi
done
sshm 'chmod 600 ~/secrets/*'

# ── Step 3: tar+ssh the package tree to ~/.apps/qflix-newsletter ───────────
# Repo path scripts/qflix-newsletter/ becomes ~/.apps/qflix-newsletter/.
# Using tar pipe (rsync isn't always available on the operator's workstation).
sshm 'mkdir -p ~/.apps/qflix-newsletter/logs && rm -rf ~/.apps/qflix-newsletter/qflix_newsletter ~/.apps/qflix-newsletter/requirements.txt'
(cd "$HERE/.." && tar czf - \
  --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' --exclude='logs' \
  qflix-newsletter/qflix_newsletter qflix-newsletter/requirements.txt) | \
  ssh -o BatchMode=yes -o ConnectTimeout=10 "${SSHM_HOST}" 'cd ~/.apps && tar xzf -'

# ── Step 4: venv + requirements ─────────────────────────────────────────────
sshm "bash -s" <<'VENVSCRIPT'
set -euo pipefail
PY311=$HOME/.local/python311/bin/python3.11
cd ~/.apps/qflix-newsletter
if [ ! -d .venv ] || [ ! -x .venv/bin/python ] || ! .venv/bin/python --version 2>&1 | grep -q "Python 3.11"; then
  rm -rf .venv
  $PY311 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/python --version
VENVSCRIPT

# ── Step 5: deploy systemd unit + timer ─────────────────────────────────────
scpm_to "$HERE/../maint/systemd/qflix-newsletter.service" '~/.config/systemd/user/qflix-newsletter.service' >/dev/null
scpm_to "$HERE/../maint/systemd/qflix-newsletter.timer"   '~/.config/systemd/user/qflix-newsletter.timer'   >/dev/null
sshm 'systemctl --user daemon-reload && systemctl --user enable --now qflix-newsletter.timer'

# ── Step 6: dry-run smoke (renders without sending) ─────────────────────────
log_info "running dry-run smoke (renders + writes /tmp/qflix-newsletter-smoke.html)"
sshm 'cd ~/.apps/qflix-newsletter && .venv/bin/python -m qflix_newsletter --dry-run --out-html /tmp/qflix-newsletter-smoke.html --verbose 2>&1 | tail -20'
sshm 'test -s /tmp/qflix-newsletter-smoke.html && head -2 /tmp/qflix-newsletter-smoke.html | head -1'

# ── Step 7: start timer ─────────────────────────────────────────────────────
sshm 'systemctl --user start qflix-newsletter.timer && systemctl --user list-timers --user qflix-newsletter.timer --no-pager 2>/dev/null | head -5'

log_info "Phase 24 complete — qflix-newsletter armed; next fire = Mon 08:00"
log_info "Manual run:    ssh quadstronaut@seedbox 'systemctl --user start qflix-newsletter.service'"
log_info "Manual dry:    ssh quadstronaut@seedbox 'cd ~/.apps/qflix-newsletter && .venv/bin/python -m qflix_newsletter --dry-run'"
