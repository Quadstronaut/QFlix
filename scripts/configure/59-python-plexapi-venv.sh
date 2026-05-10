#!/usr/bin/env bash
# Phase 40.1 — Shared python-plexapi venv at ~/.apps/python-plexapi/venv/.
# Idempotent. Pinned via secrets/python-plexapi.version (default: latest).
# Used by kill_stream + stream_stats + any future Plex script.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"
source "$HERE/../lib/secrets.sh"

if ! secret_exists python-plexapi.version; then
  TAG=$(curl -fsSL https://api.github.com/repos/pkkid/python-plexapi/releases/latest | grep -oP '"tag_name":\s*"\K[^"]+')
  [ -n "$TAG" ] || die "could not resolve plexapi latest tag"
  secret_write python-plexapi.version "$TAG"
fi
PLXVER=$(secret_read python-plexapi.version)
PLXVER_NUM="${PLXVER#v}"
log_info "python-plexapi version = $PLXVER"

# Use the Astral python-build-standalone Python 3.11 staged by Phase 22.
# System Python is 3.9 (Debian 11), but plexapi 4.18+ requires 3.10+.
sshm "PLXVER_NUM='${PLXVER_NUM}' bash -s" <<'EOF'
set -euo pipefail
PY311=$HOME/.local/python311/bin/python3.11
[ -x "$PY311" ] || { echo "FATAL: Astral python3.11 not found at $PY311 — run scripts/configure/47-conjurr-install.sh first"; exit 1; }
mkdir -p ~/.apps/python-plexapi
cd ~/.apps/python-plexapi
if [ ! -d venv ] || ! ./venv/bin/python --version 2>&1 | grep -q "Python 3.11"; then
  rm -rf venv
  $PY311 -m venv venv
fi
./venv/bin/pip install --quiet --upgrade pip wheel
./venv/bin/pip install --quiet --no-cache-dir "plexapi==${PLXVER_NUM}" requests
./venv/bin/python -c "import plexapi; print('plexapi', plexapi.VERSION)"
EOF

# Pin the X-Plex-Client-Identifier so every invocation registers as the
# SAME device on plex.tv, not a fresh "Linux device" per run. Without
# this, plexapi calls uuid.getnode() which falls back to a random
# multicast MAC inside the seedbox's container, generating a sign-in
# notification per run. See commit afb86cf for the full diagnosis.
sshm 'bash -s' <<'PLEXCFG'
set -euo pipefail
mkdir -p ~/.config/plexapi
CFG=~/.config/plexapi/config.ini
if [ ! -s "$CFG" ] || ! grep -q "^identifier" "$CFG"; then
  IDENT="manitoba-seedbox-plexapi-$(openssl rand -hex 4)"
  cat > "$CFG" <<INI
[header]
identifier = ${IDENT}
INI
  chmod 600 "$CFG"
  echo "wrote stable plexapi identifier to $CFG"
else
  echo "plexapi identifier already pinned in $CFG"
fi
PLEXCFG

log_info "Phase 40.1 complete — shared python-plexapi venv + pinned client identifier"
