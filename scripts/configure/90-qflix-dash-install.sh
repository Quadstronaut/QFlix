#!/usr/bin/env bash
# Provision the QFlix Dashboard on the seedbox — STAGED. Installs + runs the app
# on its loopback port; does NOT touch the nginx root or Homarr (that's the
# cutover, scripts/configure/91-nginx-root-to-dash.sh). Idempotent. Runs from the
# workstation and SSHes in. Codifies the validated 2026-06-27 bring-up.
#
# Pre-req: ~/secrets/qflix-dash.discord_webhook must already exist on the box
# (operator-placed). The port/session_secret/plex_client_id are auto-generated.
#
# KEY ULTRA.CC GOTCHA — undici WASM OOM:
#   The slot caps `ulimit -v` ~10 GB (hard) but reports ~515 GB RAM, so Node
#   auto-sizes a huge heap and undici's WASM HTTP parser can't reserve its ~8 GB
#   trap guard region -> "Cannot allocate Wasm memory" crash on the first fetch().
#   Fix: NODE_OPTIONS=--disable-wasm-trap-handler (+ --max-old-space-size=512),
#   baked into the env file below. Applies to ANY Node app on this box that uses
#   global fetch(). See docs/superpowers/specs/2026-06-27-qflix-dashboard-design.md §3.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$HERE/apps/qflix-dash"
HOST="$(tr -d '[:space:]' < "$HERE/secrets/seedbox.ssh-host" 2>/dev/null \
        || tr -d '[:space:]' < "$HERE/secrets/seedbox.host")"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 "quadstronaut@$HOST")

echo "[1/5] secrets (auto-gen) + Node 20 via nvm"
"${SSH[@]}" 'bash -l -s' <<'REMOTE'
set -e
[ -f ~/secrets/qflix-dash.port ]           || echo 42020 > ~/secrets/qflix-dash.port
[ -f ~/secrets/qflix-dash.session_secret ] || openssl rand -hex 32 > ~/secrets/qflix-dash.session_secret
[ -f ~/secrets/qflix-dash.plex_client_id ] || python3 -c "import uuid;print(uuid.uuid4())" > ~/secrets/qflix-dash.plex_client_id
[ -f ~/secrets/qflix-dash.discord_webhook ] || { echo "FATAL: place ~/secrets/qflix-dash.discord_webhook first" >&2; exit 1; }
chmod 600 ~/secrets/qflix-dash.*
export NVM_DIR=$HOME/.nvm
[ -s "$NVM_DIR/nvm.sh" ] || curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash >/dev/null 2>&1
. "$NVM_DIR/nvm.sh"; nvm install 20 >/dev/null 2>&1; nvm alias default 20 >/dev/null 2>&1
mkdir -p ~/.apps/qflix-dash/logs ~/.config/qflix-dash ~/.config/systemd/user
REMOTE

echo "[2/5] build (workstation) + ship"
( cd "$APP" && npm ci >/dev/null 2>&1 && npm run build >/dev/null 2>&1 )
scp -q -o BatchMode=yes -r "$APP/build" "$APP/package.json" "$APP/package-lock.json" "quadstronaut@$HOST":.apps/qflix-dash/
scp -q -o BatchMode=yes "$HERE/scripts/qflix-dash/plex_members.py"        "quadstronaut@$HOST":.apps/qflix-dash/
scp -q -o BatchMode=yes "$HERE/scripts/qflix-dash/qflix-dash.service.tmpl" "quadstronaut@$HOST":.apps/qflix-dash/

echo "[3/5] prod deps + env file + unit"
"${SSH[@]}" 'bash -l -s' <<'REMOTE'
set -e
cd ~/.apps/qflix-dash
export NVM_DIR=$HOME/.nvm; . "$NVM_DIR/nvm.sh"; nvm use 20 >/dev/null
npm ci --omit=dev >/dev/null 2>&1 || true   # app has no prod deps; harmless
FQDN=$(cat ~/secrets/seedbox.host); NODE=$(ls ~/.nvm/versions/node/v20*/bin/node | head -1)
cat > ~/.config/qflix-dash/qflix-dash.env <<ENV
PORT=$(cat ~/secrets/qflix-dash.port)
HOST=127.0.0.1
PROTOCOL_HEADER=x-forwarded-proto
HOST_HEADER=x-forwarded-host
XFF_DEPTH=2
NODE_OPTIONS=--disable-wasm-trap-handler --max-old-space-size=512
UV_THREADPOOL_SIZE=2
MANITOBA_MAINT_BIN=$HOME/bin/manitoba-maint
PLEX_TOKEN=$(cat ~/secrets/plex.token)
PLEX_CLIENT_ID=$(cat ~/secrets/qflix-dash.plex_client_id)
PLEX_MEMBERS_PY=$HOME/.apps/python-plexapi/venv/bin/python $HOME/.apps/qflix-dash/plex_members.py
SEERR_URL=http://127.0.0.1:42011
SEERR_API_KEY=$(cat ~/secrets/jellyseerr.key)
DISCORD_WEBHOOK=$(cat ~/secrets/qflix-dash.discord_webhook)
SESSION_SECRET=$(cat ~/secrets/qflix-dash.session_secret)
Q_AVATAR_URL=https://raw.githubusercontent.com/Quadstronaut/QFlix/master/Q.png
FAQ_PROBE_URL=https://$FQDN/faq/
ENV
chmod 600 ~/.config/qflix-dash/qflix-dash.env
sed "s#@@NODE@@#$NODE#g" ~/.apps/qflix-dash/qflix-dash.service.tmpl > ~/.config/systemd/user/qflix-dash.service
REMOTE

echo "[4/5] enable + start"
"${SSH[@]}" 'systemctl --user daemon-reload && systemctl --user enable --now qflix-dash.service'

echo "[5/5] verify (loopback)"
"${SSH[@]}" 'p=$(cat ~/secrets/qflix-dash.port); echo "healthz=$(curl -s -m5 localhost:$p/healthz)"; echo "status=$(curl -s -m9 localhost:$p/api/status)"'
echo "Done — staged. Dashboard runs on loopback; cutover is scripts/configure/91-nginx-root-to-dash.sh."
