#!/usr/bin/env bash
# Phase 23 — Newsletterr install. Idempotent. Internal-only (same architecture
# as Conjurr — Flask app with hardcoded port + bind 0.0.0.0 + no WSGI prefix
# middleware, so we patch app + serve on 127.0.0.1:<port> and use SSH tunnel
# for admin browser access).
#  - Reuse the Astral python-build-standalone Python 3.11 from Phase 22
#  - clone jma1ice/newsletterr (default branch HEAD pinned by commit hash in
#    versions.env going forward; the repo doesn't tag releases consistently)
#  - venv + requirements + Playwright Chromium (~150 MB)
#  - patch newsletterr.py: host=127.0.0.1, port from PORT env, debug=False
#  - .env with PORT, PUBLIC_BASE_URL, DATA_ENC_KEY (Fernet key for encrypted
#    settings; auto-generated if missing)
#  - user-systemd service + heartbeat cron
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"
source "$HERE/../lib/secrets.sh"

PUBLIC_HOST="quadstronaut.seedbox.example.com"

# Pin: track in versions.env. The plan says v2026.1; if the upstream tag exists,
# we use it; otherwise the install falls back to default branch HEAD.
NL_REF="${NEWSLETTERR_VERSION:-2026.1}"

# ── Step 1: claim port ──────────────────────────────────────────────────────
if ! secret_exists newsletterr.port; then
  USED=$(printf '%s\n' "$(secret_read listmonk.port 2>/dev/null)" "$(secret_read conjurr.port 2>/dev/null)" | sort -u)
  PORT=$(sshm "app-ports free 2>/dev/null | grep -E '^[0-9]+\$'" | grep -vxF "$USED" | head -1)
  [ -n "$PORT" ] || die "no free port from app-ports"
  secret_write newsletterr.port "$PORT"
  log_info "claimed newsletterr port $PORT"
fi
PORT=$(secret_read newsletterr.port)
log_info "newsletterr port = $PORT"

# ── Step 2: assert Python 3.11 (PBS) is present (set up by Phase 22) ────────
sshm 'PY=$HOME/.local/python311/bin/python3.11; [ -x "$PY" ] || { echo "FATAL: python3.11 not found — run scripts/configure/47-conjurr-install.sh first or install python-build-standalone"; exit 1; }; $PY --version'

# ── Step 3: clone + venv + install ──────────────────────────────────────────
sshm "REF='${NL_REF}' bash -s" <<'CLONESCRIPT'
set -euo pipefail
PY311=$HOME/.local/python311/bin/python3.11

mkdir -p ~/.apps/newsletterr/logs
cd ~/.apps/newsletterr
if [ ! -d repo/.git ]; then
  git clone https://github.com/jma1ice/newsletterr.git repo
fi
cd repo
git fetch --tags --quiet
if git rev-parse --verify --quiet "v${REF}" >/dev/null; then
  git checkout --quiet "v${REF}"
elif git rev-parse --verify --quiet "${REF}" >/dev/null; then
  git checkout --quiet "${REF}"
else
  echo "[warn] ref ${REF} not found; falling back to default branch"
  DEFAULT=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|origin/||' || echo main)
  git checkout --quiet "${DEFAULT}"
  git pull --ff-only --quiet
fi

if [ ! -d .venv ] || [ ! -x .venv/bin/python ] || ! .venv/bin/python --version 2>&1 | grep -q "Python 3.11"; then
  rm -rf .venv
  $PY311 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
# Playwright Chromium (~150 MB). Skip if already installed.
if [ ! -d "$HOME/.cache/ms-playwright" ] || [ -z "$(ls $HOME/.cache/ms-playwright 2>/dev/null)" ]; then
  .venv/bin/python -m playwright install chromium
fi
.venv/bin/python --version
CLONESCRIPT

# ── Step 4: patch newsletterr.py (host + port + debug) ─────────────────────
sshm "bash -s" <<'PATCHSCRIPT'
cd ~/.apps/newsletterr/repo
if ! grep -q "MANITOBA_PATCHED" newsletterr.py; then
  # Original: app.run(host="0.0.0.0", port=6397, debug=debug)
  # Patched: localhost-only, port from env, debug forced off.
  sed -i 's|app\.run(host="0\.0\.0\.0", port=6397, debug=debug)|# MANITOBA_PATCHED\n    import os as _os\n    app.run(host="127.0.0.1", port=int(_os.environ.get("PORT", 6397)), debug=False)|' newsletterr.py
  echo "[ok] patched newsletterr.py port + host"
fi
tail -5 newsletterr.py
PATCHSCRIPT

# ── Step 5: write env/.env (newsletterr.py moves repo/.env -> repo/env/.env on
#    first run, same pattern as Conjurr; write directly to destination) ─────
sshm "PORT=${PORT} PUBLIC_HOST='${PUBLIC_HOST}' bash -s" <<'ENVSCRIPT'
mkdir -p ~/.apps/newsletterr/repo/env
ENV_FILE=~/.apps/newsletterr/repo/env/.env
if [ ! -f "$ENV_FILE" ] || ! grep -q '^DATA_ENC_KEY=' "$ENV_FILE"; then
  KEY=$(~/.apps/newsletterr/repo/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
else
  KEY=$(grep '^DATA_ENC_KEY=' "$ENV_FILE" | cut -d= -f2-)
fi
{
  echo "PORT=$PORT"
  # Internal-only — operators tunnel via ssh -L 8080:127.0.0.1:<port> to reach the UI.
  echo "PUBLIC_BASE_URL=http://localhost:8080"
  echo "DATA_ENC_KEY=$KEY"
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
ENVSCRIPT

# ── Step 6: user-systemd service ────────────────────────────────────────────
sshm "bash -s" <<'UNITSCRIPT'
set -euo pipefail
cat > ~/.config/systemd/user/newsletterr.service <<'UNIT'
[Unit]
Description=Newsletterr — Plex auto-newsletter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.apps/newsletterr/repo
EnvironmentFile=%h/.apps/newsletterr/repo/env/.env
ExecStart=%h/.apps/newsletterr/repo/.venv/bin/python newsletterr.py
Restart=on-failure
RestartSec=10s
StandardOutput=append:%h/.apps/newsletterr/logs/newsletterr.log
StandardError=append:%h/.apps/newsletterr/logs/newsletterr.err

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable newsletterr.service
systemctl --user restart newsletterr.service
UNITSCRIPT
sleep 6
sshm 'systemctl --user is-active newsletterr.service' | grep -q active || die "newsletterr not active — check ~/.apps/newsletterr/logs/newsletterr.err"
log_info "newsletterr.service active"

# ── Step 7: heartbeat cron ──────────────────────────────────────────────────
sshm 'mkdir -p ~/scripts/ops'
scpm_to "$HERE/../ops/heartbeat-newsletterr.sh" '~/scripts/ops/heartbeat-newsletterr.sh' >/dev/null
sshm 'chmod +x ~/scripts/ops/heartbeat-newsletterr.sh && (crontab -l 2>/dev/null | grep -v heartbeat-newsletterr; echo "*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-newsletterr.sh") | crontab -'
log_info "heartbeat cron installed"

# ── Step 8: drop any leftover nginx fragment from prior versions ───────────
sshm 'rm -f ~/.apps/nginx/proxy.d/newsletterr.conf; /usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t && systemctl --user reload nginx'

# ── Step 9: verify ──────────────────────────────────────────────────────────
HTTP=$(sshm "curl -sk -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:${PORT}/")
case "$HTTP" in
  200|302) log_info "✓ newsletterr internal endpoint reachable (HTTP $HTTP)" ;;
  *)       die "newsletterr not reachable on 127.0.0.1:${PORT}: HTTP $HTTP" ;;
esac

log_info "Phase 23 complete — Newsletterr at http://127.0.0.1:${PORT} (internal-only)"
log_info "Operator: ssh -L 8080:127.0.0.1:${PORT} quadstronaut@seedbox.example.com   then  http://localhost:8080/"
