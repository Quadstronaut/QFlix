#!/usr/bin/env bash
# Phase 25 — Buildarr install (cron-class). Idempotent.
#
# Buildarr is a declarative-YAML state-converger for *arrs (Sonarr/Radarr/
# Prowlarr/Jellyseerr). Pure Python; runs once via `buildarr run` and exits,
# making it a perfect cron-class app — fired by a daily systemd timer at
# 04:30 (post-maintenance window).
#
# INTERNAL-only: no UI, no nginx fragment.
#
#  - Reuse Astral python-build-standalone Python 3.11 from earlier phases
#  - venv at ~/.apps/buildarr/.venv
#  - pip install buildarr + plugins for the *arrs we run
#  - systemd timer: daily 04:30 → buildarr run ~/.apps/buildarr/buildarr.yml
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

# ── Step 1: assert PBS Python 3.11 ──────────────────────────────────────────
sshm 'PY=$HOME/.local/python311/bin/python3.11; [ -x "$PY" ] || { echo "FATAL: python3.11 not found"; exit 1; }; $PY --version'

# ── Step 2: venv + pip install ──────────────────────────────────────────────
sshm "bash -s" <<'VENVSCRIPT'
set -euo pipefail
PY311=$HOME/.local/python311/bin/python3.11
mkdir -p ~/.apps/buildarr/logs
cd ~/.apps/buildarr
if [ ! -d .venv ] || ! .venv/bin/python --version 2>&1 | grep -q "Python 3.11"; then
  rm -rf .venv
  $PY311 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet \
  'buildarr>=0.7' \
  'buildarr-sonarr' \
  'buildarr-radarr' \
  'buildarr-prowlarr' \
  'buildarr-jellyseerr'
.venv/bin/pip show buildarr | grep -E "^(Name|Version):"
VENVSCRIPT

# ── Step 3: deploy starter buildarr.yml (empty stack — operator fills in) ───
sshm "bash -s" <<'CONFSCRIPT'
set -euo pipefail
CONF=~/.apps/buildarr/buildarr.yml
if [ -f "$CONF" ]; then
  echo "[skip] $CONF already exists — leaving as-is"
  exit 0
fi
cat > "$CONF" <<'YAML'
# Buildarr declarative *arr config — reconciles to all four ARRs nightly.
# Operator fills in api_keys + url_bases. Buildarr will read missing values from
# its prompts on first run if `secrets_file_path` is set; we use plain api_keys
# here for transparency since the seedbox-internal API keys live in ~/secrets.
buildarr:
  watch_config: false
  update_days:
    - monday
    - tuesday
    - wednesday
    - thursday
    - friday
    - saturday
    - sunday
  update_times:
    - "04:30"

# sonarr:
#   instances:
#     primary:
#       hostname: 127.0.0.1
#       port: 17026
#       url_base: sonarr
#       api_key: SONARR_API_KEY_HERE
#       version: latest
# radarr:
#   instances:
#     primary:
#       hostname: 127.0.0.1
#       port: 17027
#       url_base: radarr
#       api_key: RADARR_API_KEY_HERE
#       version: latest
YAML
chmod 600 "$CONF"
echo "[ok] wrote starter $CONF"
CONFSCRIPT

# ── Step 4: deploy systemd service + timer ──────────────────────────────────
sshm "bash -s" <<'UNITSCRIPT'
cat > ~/.config/systemd/user/buildarr.service <<'UNIT'
[Unit]
Description=Buildarr — declarative *arr state convergence
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/.apps/buildarr
ExecStart=%h/.apps/buildarr/.venv/bin/buildarr run %h/.apps/buildarr/buildarr.yml
StandardOutput=append:%h/.apps/buildarr/logs/buildarr.log
StandardError=append:%h/.apps/buildarr/logs/buildarr.err
UNIT

cat > ~/.config/systemd/user/buildarr.timer <<'UNIT'
[Unit]
Description=Run Buildarr nightly at 04:30 (post-maintenance window)

[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true
Unit=buildarr.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable buildarr.timer
systemctl --user start buildarr.timer
systemctl --user list-timers --user buildarr.timer --no-pager 2>/dev/null | head -5
UNITSCRIPT

log_info "Phase 25 complete — Buildarr armed; next fire = nightly 04:30"
log_info "Operator next: edit ~/.apps/buildarr/buildarr.yml to enable *arr instances"
