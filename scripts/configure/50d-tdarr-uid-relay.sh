#!/usr/bin/env bash
# QFLX-97 — install or refresh tdarr-uid-relay (.socket + .service). Idempotent.
#
# Tdarr 2.17.01 has no working auth and this slot shares one network namespace
# with every other tenant, so ANY TCP listener of Tdarr's is reachable by ~150
# other uids. 50a moves Tdarr onto a unix socket in a 0700 directory; this
# relay holds the old address, 127.0.0.1:<serverPort>, and forwards only
# connections the kernel attributes to our uid (scripts/ops/tdarr-uid-relay.py
# says how). Tdarr Node, the pusher probe and every API script keep the address
# they already use.
#
# The PORT belongs to tdarr-uid-relay.socket, i.e. to the systemd user manager,
# not to the relay process: a relay crash or restart queues connections instead
# of freeing the port for a co-tenant to bind and impersonate Tdarr to our Node.
#
# Called by 50-tdarr-install.sh. Safe to run alone: it never touches Tdarr. If
# Tdarr still holds the port (the one-time cutover from the QFLX-96 TCP bind),
# the units are installed and enabled but NOT started, and it says so; the
# caller stops Tdarr, starts the socket, then starts Tdarr (50-tdarr-install.sh
# step 6d). The relay service is restarted only when its code or unit changed
# (the Node's socket.io session drops for a moment and reconnects); the socket
# only when its own unit changed, because restarting it frees the port.
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
CODE_CHANGED=0
SVC_CHANGED=0
SOCK_CHANGED=0
DROPIN_CHANGED=0
PORT=$(python3 -c 'import json,os,sys; print(int(json.load(open(os.path.expanduser(sys.argv[1])))["serverPort"]))' \
  ~/.apps/tdarr/configs/Tdarr_Server_Config.json)

python3 "$OPS/tdarr-uid-relay.py.qflix-new" --selftest
chmod 755 "$OPS/tdarr-uid-relay.py.qflix-new"
if cmp -s "$OPS/tdarr-uid-relay.py.qflix-new" "$OPS/tdarr-uid-relay.py"; then
  rm -f "$OPS/tdarr-uid-relay.py.qflix-new"
else
  [ -e "$OPS/tdarr-uid-relay.py" ] && cp -p "$OPS/tdarr-uid-relay.py" "$OPS/tdarr-uid-relay.py.qflix-bak-$STAMP"
  mv -f "$OPS/tdarr-uid-relay.py.qflix-new" "$OPS/tdarr-uid-relay.py"
  CODE_CHANGED=1
fi

# put <path>: stdin -> path, backing up a differing original. Returns 1 when
# it changed something (callers read $?; `|| X=1` keeps set -e quiet).
put() {
  local dst="$1" tmp="$1.qflix-new"
  mkdir -p "$(dirname "$dst")"
  cat > "$tmp"
  if cmp -s "$tmp" "$dst"; then rm -f "$tmp"; return 0; fi
  [ -e "$dst" ] && cp -p "$dst" "$dst.qflix-bak-$STAMP"
  mv -f "$tmp" "$dst"
  return 1
}

put "$UD/tdarr-uid-relay.socket" <<UNIT || SOCK_CHANGED=1
[Unit]
Description=Tdarr uid relay listener (127.0.0.1:${PORT}, held by systemd so it is never free)

[Socket]
# The port Tdarr Node and every API caller use (Tdarr_Server_Config.json
# serverPort). Loopback only; no ReusePort, so nobody can share it.
ListenStream=127.0.0.1:${PORT}
Backlog=128

[Install]
WantedBy=sockets.target
UNIT

put "$UD/tdarr-uid-relay.service" <<'UNIT' || SVC_CHANGED=1
[Unit]
Description=Tdarr uid relay (127.0.0.1:serverPort -> Tdarr unix socket, our uid only)
Requires=tdarr-uid-relay.socket
After=tdarr-uid-relay.socket
# Never give up: every restart is a cheap Python start, and the socket unit
# keeps the port while it happens.
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
# wiring on a box whose units 50-tdarr-install.sh wrote earlier. They only
# change dependencies, so a change needs a daemon-reload, never a relay
# restart (DROPIN_CHANGED is deliberately not one of the restart triggers).
# The dependencies point one way: the relay must keep running across Tdarr
# restarts, so nothing here makes the relay depend on Tdarr.
put "$UD/tdarr-server.service.d/50-qflix-uid-relay.conf" <<'DROPIN' || DROPIN_CHANGED=1
# QFLX-97: Tdarr listens on a unix socket; its TCP address is the relay's.
[Unit]
Wants=tdarr-uid-relay.socket tdarr-uid-relay.service
DROPIN
# The Node REQUIRES the socket. Whoever holds 127.0.0.1:<port> is "the server"
# to the Node, which runs that server's flows and plugins as our uid. If a
# co-tenant binds the port first (at boot, before sockets.target), the socket
# unit fails; with Wants= the Node started anyway and served the squatter
# (reproduced under user systemd, PR #131 review). Requires= + After= fails the
# Node's start job instead: an outage the "Tdarr Node" monitor reds, not a
# hijack. Not BindsTo: that would also stop the Node, killing in-flight
# jobs, on any later socket state change, and the threat is a socket that
# never started. The relay service stays Wants=: the socket alone holds the
# port, and a relay restart must not bounce the Node.
put "$UD/tdarr-node.service.d/50-qflix-uid-relay.conf" <<'DROPIN' || DROPIN_CHANGED=1
# QFLX-97: the Node reaches the server through the relay on 127.0.0.1, and
# must never start unless OUR socket unit holds that port.
[Unit]
Requires=tdarr-uid-relay.socket
Wants=tdarr-uid-relay.service
After=tdarr-uid-relay.socket tdarr-uid-relay.service
DROPIN

systemctl --user daemon-reload
if [ "$DROPIN_CHANGED" = 1 ]; then
  echo "Tdarr drop-ins updated: daemon-reload only, they apply to the next start"
fi
systemctl --user enable tdarr-uid-relay.socket tdarr-uid-relay.service >/dev/null 2>&1

if ss -ltnpH "sport = :$PORT" | grep -q '"Tdarr_'; then
  echo "tdarr-uid-relay installed, NOT started: Tdarr still holds 127.0.0.1:$PORT (cutover). Stop tdarr-server, start tdarr-uid-relay.socket + .service, then start tdarr-server."
  exit 0
fi
if [ "$SOCK_CHANGED" = 1 ] && systemctl --user is-active --quiet tdarr-uid-relay.socket; then
  # Only when the socket unit itself changed (a new port): this frees the port
  # for as long as the restart takes.
  systemctl --user restart tdarr-uid-relay.socket
fi
systemctl --user start tdarr-uid-relay.socket
if [ "$CODE_CHANGED$SVC_CHANGED$SOCK_CHANGED" != 000 ] || ! systemctl --user is-active --quiet tdarr-uid-relay.service; then
  systemctl --user restart tdarr-uid-relay.service
  echo "tdarr-uid-relay (re)started"
else
  echo "tdarr-uid-relay unchanged and active"
fi
RELAY
log_info "tdarr-uid-relay installed"
