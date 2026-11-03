#!/usr/bin/env bash
# QFLX-97 — install or refresh tdarr-uid-relay.service. Idempotent.
#
# Tdarr 2.17.01 has no working auth and this slot shares one network namespace
# with every other tenant, so ANY TCP listener of Tdarr's is reachable by ~150
# other uids. 50a moves Tdarr onto a unix socket in a 0700 directory; this
# relay holds the old address, 127.0.0.1:<serverPort>, and forwards only
# connections the kernel attributes to our uid (scripts/ops/tdarr-uid-relay.py
# says how). Tdarr Node, the pusher probe and every API script keep the address
# they already use.
#
# Called by 50-tdarr-install.sh (before the restart that moves Tdarr onto the
# socket). Safe to run alone: it never restarts Tdarr, and it restarts the
# relay only when its code or unit actually changed (a relay restart drops the
# Node's socket.io session for a few seconds; it reconnects on its own).
#
# Refuses to install relay code whose --selftest fails on THIS box: code that
# cannot tell uids apart must never take the port.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

sshm 'mkdir -p ~/scripts/ops ~/.config/systemd/user ~/.apps/tdarr/logs'
scpm_to "$HERE/../ops/tdarr-uid-relay.py" '~/scripts/ops/tdarr-uid-relay.py.qflix-new' >/dev/null

sshm 'bash -s' <<'RELAY'
set -euo pipefail
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"; export XDG_RUNTIME_DIR
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OPS=~/scripts/ops
UD=~/.config/systemd/user
CHANGED=0

python3 "$OPS/tdarr-uid-relay.py.qflix-new" --selftest
chmod 755 "$OPS/tdarr-uid-relay.py.qflix-new"
if cmp -s "$OPS/tdarr-uid-relay.py.qflix-new" "$OPS/tdarr-uid-relay.py"; then
  rm -f "$OPS/tdarr-uid-relay.py.qflix-new"
else
  [ -e "$OPS/tdarr-uid-relay.py" ] && cp -p "$OPS/tdarr-uid-relay.py" "$OPS/tdarr-uid-relay.py.qflix-bak-$STAMP"
  mv -f "$OPS/tdarr-uid-relay.py.qflix-new" "$OPS/tdarr-uid-relay.py"
  CHANGED=1
fi

# put <path>: stdin -> path, backing up a differing original. Sets CHANGED.
put() {
  local dst="$1" tmp="$1.qflix-new"
  mkdir -p "$(dirname "$dst")"
  cat > "$tmp"
  if cmp -s "$tmp" "$dst"; then rm -f "$tmp"; return 0; fi
  [ -e "$dst" ] && cp -p "$dst" "$dst.qflix-bak-$STAMP"
  mv -f "$tmp" "$dst"
  CHANGED=1
}

put "$UD/tdarr-uid-relay.service" <<'UNIT'
[Unit]
Description=Tdarr uid relay (127.0.0.1:serverPort -> Tdarr unix socket, our uid only)
After=network-online.target
# Never give up: a relay that stops restarting leaves Tdarr reachable by no
# one, and every restart is a cheap Python start, not a Tdarr boot.
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/scripts/ops/tdarr-uid-relay.py
Restart=always
RestartSec=3
UMask=0077
NoNewPrivileges=yes
StandardOutput=append:%h/.apps/tdarr/logs/relay.log
StandardError=append:%h/.apps/tdarr/logs/relay.log

[Install]
WantedBy=default.target
UNIT

# Drop-ins, not edits to the Tdarr units, so this script alone reproduces the
# wiring on a box whose units 50-tdarr-install.sh wrote earlier. Wants= only:
# the relay must keep running across Tdarr restarts, never stop with them.
put "$UD/tdarr-server.service.d/50-qflix-uid-relay.conf" <<'DROPIN'
# QFLX-97: Tdarr listens on a unix socket; its TCP address is the relay's.
[Unit]
Wants=tdarr-uid-relay.service
DROPIN
put "$UD/tdarr-node.service.d/50-qflix-uid-relay.conf" <<'DROPIN'
# QFLX-97: the Node reaches the server through the relay on 127.0.0.1.
[Unit]
Wants=tdarr-uid-relay.service
After=tdarr-uid-relay.service
DROPIN

systemctl --user daemon-reload
systemctl --user enable tdarr-uid-relay.service >/dev/null 2>&1
if [ "$CHANGED" = 1 ] || ! systemctl --user is-active --quiet tdarr-uid-relay.service; then
  systemctl --user restart tdarr-uid-relay.service
  echo "tdarr-uid-relay (re)started"
else
  echo "tdarr-uid-relay unchanged and active"
fi
RELAY
log_info "tdarr-uid-relay installed"
