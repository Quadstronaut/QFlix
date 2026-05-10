#!/usr/bin/env bash
# Phase 22 — Conjurr v4.1.0 install. Idempotent.
#  - pyenv-managed Python 3.11.8 (Debian 11 ships only 3.9; Conjurr requires 3.11+)
#  - clone yungsnuzzy/conjurr v4.1.0 to ~/.apps/conjurr/repo
#  - venv with python 3.11
#  - .env wired to Tautulli + Jellyseerr (Overseerr-compatible) + Gemini
#  - user-systemd service
#  - nginx /conjurr/ fragment (htpasswd-protected — admin-only UI)
#  - heartbeat cron
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"
source "$HERE/../lib/secrets.sh"

CONJURR_REF="v4.1.0"  # pin (the install script will checkout this exact ref)
PY_VER="3.11.8"
PBS_VER="20240224"  # python-build-standalone release date
PUBLIC_HOST="quadstronaut.seedbox.example.com"

# ── Step 1: install Python 3.11 via Astral python-build-standalone ──────────
# Why not pyenv? Ultra.cc's pkg-config returns a broken sqlite3 path
# (`/usr/local/include` not present), so pyenv-built Python has no sqlite3,
# which Conjurr requires. python-build-standalone ships pre-built Pythons
# with all batteries (sqlite, ssl, ffi, etc) and works without sudo.
sshm "PBS_VER='${PBS_VER}' PY_VER='${PY_VER}' bash -s" <<'PYINSTALL'
set -euo pipefail
PY311_BIN="$HOME/.local/python311/bin/python3.11"
NEED_INSTALL=1
if [ -x "$PY311_BIN" ]; then
  V=$($PY311_BIN --version 2>&1 | awk '{print $2}')
  [ "$V" = "${PY_VER}" ] && NEED_INSTALL=0
fi
if [ "$NEED_INSTALL" = "1" ]; then
  TARBALL="cpython-${PY_VER}+${PBS_VER}-x86_64-unknown-linux-gnu-install_only.tar.gz"
  URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_VER}/${TARBALL}"
  mkdir -p ~/.local/python311
  cd /tmp
  curl -fsSL "$URL" -o pbs.tgz
  tar -xzf pbs.tgz -C ~/.local/python311 --strip-components=1
  rm -f pbs.tgz
fi
$PY311_BIN --version
$PY311_BIN -c "import sqlite3; print('sqlite3:', sqlite3.sqlite_version)"
PYINSTALL

# ── Step 2: claim port ──────────────────────────────────────────────────────
if ! secret_exists conjurr.port; then
  USED=$(printf '%s\n' "$(secret_read listmonk.port 2>/dev/null)")
  PORT=$(sshm "app-ports free 2>/dev/null | grep -E '^[0-9]+\$'" | grep -vxF "$USED" | head -1)
  [ -n "$PORT" ] || die "no free port from app-ports"
  secret_write conjurr.port "$PORT"
  log_info "claimed conjurr port $PORT"
fi
PORT=$(secret_read conjurr.port)
log_info "conjurr port = $PORT"

# ── Step 3: deploy required secrets to seedbox ──────────────────────────────
sshm 'mkdir -p ~/secrets && chmod 700 ~/secrets'
for f in tautulli.key tautulli.port jellyseerr.key jellyseerr.port gemini.api_key htpasswd.password; do
  scpm_to "secrets/$f" "secrets/$f" >/dev/null
done
sshm 'chmod 600 ~/secrets/*'

# ── Step 4: clone + venv + install requirements ─────────────────────────────
sshm "PORT=${PORT} REF='${CONJURR_REF}' PY_VER='${PY_VER}' bash -s" <<'CLONESCRIPT'
set -euo pipefail
PY311=$HOME/.local/python311/bin/python3.11

mkdir -p ~/.apps/conjurr/logs
cd ~/.apps/conjurr
if [ ! -d repo/.git ]; then
  git clone https://github.com/yungsnuzzy/conjurr.git repo
fi
cd repo
git fetch --tags --quiet
# Pin: try the requested ref; if not a tag, fall back to default branch HEAD.
if git rev-parse --verify --quiet "${REF}" >/dev/null; then
  git checkout --quiet "${REF}"
else
  echo "[warn] ${REF} not found; falling back to default branch"
  DEFAULT=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|origin/||' || echo main)
  git checkout --quiet "${DEFAULT}"
  git pull --ff-only --quiet
fi

if [ ! -d .venv ] || [ ! -x .venv/bin/python ] || ! .venv/bin/python --version 2>&1 | grep -q "Python ${PY_VER%.*}"; then
  rm -rf .venv
  $PY311 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/python --version
.venv/bin/python -c "import sqlite3"

# Patch app.py: it hardcodes `app.run(host="0.0.0.0", port=2665, debug=True)`,
# which is OUTSIDE this seedbox's port allocation (Fair Use violation) and
# binds publicly. Replace with localhost + PORT env var + debug off.
# Patch is idempotent: marker comment prevents re-patching.
if ! grep -q "MANITOBA_PATCHED" app.py; then
  sed -i 's|app\.run(host="0\.0\.0\.0", port=2665, debug=True)|# MANITOBA_PATCHED\n    import os as _os\n    app.run(host="127.0.0.1", port=int(_os.environ.get("PORT", 2665)), debug=False)|' app.py
  echo "[ok] patched app.py port binding"
fi
CLONESCRIPT

# ── Step 5: write .env directly to env/.env (Conjurr's startup code MOVES
#    repo/.env -> repo/env/.env on first run; writing to the final location
#    avoids the move-on-restart churn that confused systemd's EnvironmentFile.
#    SCRIPT_NAME is NOT set — Conjurr lacks WSGI prefix middleware, so we
#    serve it on localhost-only and let Newsletterr call it directly.)
sshm "PORT=${PORT} bash -s" <<'ENVSCRIPT'
TAUTULLI_KEY=$(cat ~/secrets/tautulli.key)
TAUTULLI_PORT=$(cat ~/secrets/tautulli.port)
JSR_KEY=$(cat ~/secrets/jellyseerr.key)
JSR_PORT=$(cat ~/secrets/jellyseerr.port)
GEMINI_KEY=$(cat ~/secrets/gemini.api_key)
mkdir -p ~/.apps/conjurr/repo/env
{
  echo "PORT=$PORT"
  echo "TAUTULLI_URL=http://127.0.0.1:$TAUTULLI_PORT/tautulli"
  echo "TAUTULLI_API_KEY=$TAUTULLI_KEY"
  echo "GOOGLE_API_KEY=$GEMINI_KEY"
  echo "USER_MODE=1"
  echo "OVERSEERR_URL=http://127.0.0.1:$JSR_PORT"
  echo "OVERSEERR_API_KEY=$JSR_KEY"
} > ~/.apps/conjurr/repo/env/.env
chmod 600 ~/.apps/conjurr/repo/env/.env
ENVSCRIPT

# ── Step 6: user-systemd service ────────────────────────────────────────────
sshm "bash -s" <<'UNITSCRIPT'
set -euo pipefail
mkdir -p ~/.apps/conjurr/logs
cat > ~/.config/systemd/user/conjurr.service <<'UNIT'
[Unit]
Description=Conjurr AI recommendation engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.apps/conjurr/repo
EnvironmentFile=%h/.apps/conjurr/repo/env/.env
ExecStart=%h/.apps/conjurr/repo/.venv/bin/python app.py
Restart=on-failure
RestartSec=10s
StandardOutput=append:%h/.apps/conjurr/logs/conjurr.log
StandardError=append:%h/.apps/conjurr/logs/conjurr.err

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable conjurr.service
systemctl --user restart conjurr.service
UNITSCRIPT
sleep 5
sshm 'systemctl --user is-active conjurr.service' | grep -q active || die "conjurr not active — check ~/.apps/conjurr/logs/conjurr.err"
log_info "conjurr.service active"

# ── Step 7: heartbeat cron ──────────────────────────────────────────────────
scpm_to "$HERE/../ops/heartbeat-conjurr.sh" '~/scripts/ops/heartbeat-conjurr.sh' >/dev/null
sshm 'chmod +x ~/scripts/ops/heartbeat-conjurr.sh && (crontab -l 2>/dev/null | grep -v heartbeat-conjurr; echo "*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-conjurr.sh") | crontab -'
log_info "heartbeat cron installed"

# ── Step 8: NO nginx fragment — Conjurr is internal-only ───────────────────
# Conjurr's app.py has no WSGI prefix middleware (no SCRIPT_NAME / ProxyFix),
# so it can't be hosted under /conjurr/ via a proxy. We serve it on
# 127.0.0.1:<port> only. Newsletterr (also seedbox-local) calls it directly.
# Admin browser access is via SSH tunnel:
#   ssh -L 8080:127.0.0.1:${PORT} quadstronaut@seedbox.example.com
#   open http://localhost:8080/
# Idempotency: drop any leftover proxy.d/conjurr.conf from prior versions.
sshm 'rm -f ~/.apps/nginx/proxy.d/conjurr.conf; /usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t && systemctl --user reload nginx'

# ── Step 9: verify (direct localhost via sshm) ──────────────────────────────
HTTP=$(sshm "curl -sk -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:${PORT}/")
case "$HTTP" in
  200|302) log_info "✓ conjurr internal endpoint reachable (HTTP $HTTP)" ;;
  *)       die "conjurr not reachable on 127.0.0.1:${PORT}: HTTP $HTTP — check logs" ;;
esac

log_info "Phase 22 complete — Conjurr at http://127.0.0.1:${PORT} (internal-only; SSH tunnel for admin UI)"