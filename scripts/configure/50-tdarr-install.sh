#!/usr/bin/env bash
# Phase 27 + 28 — Tdarr Server + Node install. Idempotent.
#  - Tdarr_Updater downloads pinned Server + Node binaries
#  - Server patched to listen on a unix socket only (50a-tdarr-server-loopback.py)
#  - uid relay on 127.0.0.1:<port> -> that socket, our uid only (50d, QFLX-97)
#  - Server config: <port>, seeded API key
#  - Node config: points at local Server (through the relay)
#  - user-systemd services for both
#  - NO public route: Tdarr is tunnel-only (QFLX-96), any legacy one is retired
#  - heartbeat crons for both
#  - Phases 29-31 (library config + workflows + ops cleanup) stay operator-deferred
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"
source "$HERE/../lib/secrets.sh"
source "$HERE/../lib/ports.sh"

# Pinned to 2.17.01. The plan suggested 2.45.01 but:
#   1. storage.tdarr.io 404s on that version
#   2. Tdarr_Updater self-updates to whatever is "latest" (currently 2.71.01),
#      ignoring our pin
#   3. Tdarr 2.18.01-2.90.01 have a bypassable auth gate; any upgrade
#      target must be 2.91.01+ (own project, QFLX-97 item 4). GLIBC is
#      not the blocker (box runs glibc 2.41).
# 2.17.01 stays pinned until that upgrade is planned. We bypass Tdarr_Updater entirely and download the
# Server.zip + Node.zip artifacts directly.
TDARR_VER="2.17.01"
# Hard requirement — placeholder must never reach a live curl/nginx.
PUBLIC_HOST="$(secret_read seedbox.host)"

# ── Step 1: claim port. ONE port for UI + API + Node protocol, because
#    webUIPort == serverPort below. Stock Tdarr 2.17 assumes two: the second
#    listener (server2) dies on a swallowed EADDRINUSE at every start, and it
#    was NOT harmless — the UI's /webui socket.io namespace lived only there,
#    so the browser got "Invalid namespace" and every panel stayed empty. 50a
#    moves /webui onto the one listener and gates the self-redirect the same
#    equality would loop on. ───────────────────────────────────────────────
claim_port tdarr.server_port
SERVER_PORT=$(secret_read tdarr.server_port)
log_info "tdarr port (UI + API + Node) = $SERVER_PORT"

# ── Step 2: API key ─────────────────────────────────────────────────────────
if ! secret_exists tdarr.api_key; then
  KEY=$(sshm 'openssl rand -hex 16')
  secret_write tdarr.api_key "tapi_${KEY}"
fi
API_KEY=$(secret_read tdarr.api_key)

# ── Step 3: download Server + Node zips directly (NOT via Tdarr_Updater) ───
# Tdarr_Updater self-updates to latest (ignores our pin).
# Direct .zip downloads from storage.tdarr.io stay pinned.
sshm "TDARR_VER='${TDARR_VER}' bash -s" <<'INSTSCRIPT'
set -euo pipefail
mkdir -p ~/.apps/tdarr/{configs,logs,transcode_cache}
cd ~/.apps/tdarr
for asset in Tdarr_Server Tdarr_Node; do
  if [ ! -x "${asset}/${asset}" ] || ! grep -q "${TDARR_VER}" "${asset}/version" 2>/dev/null; then
    rm -rf "${asset}"
    curl -fsSL "https://storage.tdarr.io/versions/${TDARR_VER}/linux_x64/${asset}.zip" -o "${asset}.zip"
    unzip -q -o "${asset}.zip" -d "${asset}"
    rm -f "${asset}.zip"
    chmod +x "${asset}/${asset}"
    echo "${TDARR_VER}" > "${asset}/version"
  fi
done
ls -la Tdarr_Server/Tdarr_Server Tdarr_Node/Tdarr_Node

# ── QFlix worker1.js patch: null-guard the worker2 cleanup call in the Exit
# handler.  Tdarr 2.17.01 ships a minified worker1.js that calls
# `worker2[T(0x289)]()` after an Exit message arrives.  When worker2 has
# already disconnected (which is common because the child sends Exit on its
# way out), the property lookup returns undefined and the cleanup call
# crashes the WHOLE node with `TypeError: worker2[T(...)] is not a function`,
# leaving staged items orphaned and forcing the node to respawn.  The patch
# wraps the call in a typeof guard + try/catch.  Idempotent via marker.
python3 <<'PY'
import os, shutil, time
SRC = os.path.expanduser('~/.apps/tdarr/Tdarr_Node/srcug/workers/worker1.js')
MARK = '/*QFLIX-WORKER2-EXIT-NULLGUARD*/'
NEEDLE = "sendWorker2(g),worker2[T(0x289)](),"
with open(SRC) as f:
    txt = f.read()
if MARK in txt:
    raise SystemExit(0)
if NEEDLE not in txt:
    # Upstream changed the obfuscation; bail loudly so the operator notices.
    raise SystemExit("QFlix patch needle missing — Tdarr_Node/worker1.js layout changed")
repl = ("sendWorker2(g),"
        + "(" + MARK + "function(){try{var k=T(0x289);"
        + "if(worker2&&typeof worker2[k]==='function')worker2[k]()"
        + "}catch(_){}}()),")
shutil.copy2(SRC, SRC + '.qflix-bak-' + str(int(time.time())))
with open(SRC, 'w') as f:
    f.write(txt.replace(NEEDLE, repl))
print("QFlix worker1.js null-guard applied")
PY
INSTSCRIPT

# ── Step 3b: unix-socket listener, self-redirect gate, shared /webui ───────
# (QFLX-96, QFLX-97.) A re-unzip above restores the vendor files (servers.js,
# mainApi.js), so this runs on every install and before the restart in step 6d
# that loads it. Its own header says why. manifest/apps.yaml runs the same file
# as the tdarr-server zip_swap post step, so lifecycle upgrades keep it too.
sshm 'python3 -' < "$HERE/50a-tdarr-server-loopback.py"

# ── Step 4: server config ──────────────────────────────────────────────────
# `auth` and `seededApiKey` are INERT on 2.17.01: a keyless /api/v2/cruddb
# answers 200. `serverIP` does not bind anything either. Do not read this file
# as a security control; the unix socket from step 3b and the uid relay from
# step 6a2 are the only ones. serverPort is still the address every client
# uses: the relay listens on it.
sshm "cat > ~/.apps/tdarr/configs/Tdarr_Server_Config.json" <<JSON
{
  "serverIP": "127.0.0.1",
  "serverPort": ${SERVER_PORT},
  "webUIPort": ${SERVER_PORT},
  "openBrowser": false,
  "auth": true,
  "seededApiKey": "${API_KEY}",
  "handbrakePath": "",
  "ffmpegPath": "",
  "mkvpropeditPath": "",
  "ccextractorPath": "",
  "maxLogSizeMB": 10,
  "cronPluginUpdate": "0 4 * * 0"
}
JSON

# ── Step 5: node config (Tdarr_Node_Config.json — registers with local server) ─
# serverPort is a STRING here on purpose: the Node's schema validates it as
# string, while the Server's (Step 4) wants a number — mismatching either way
# logs "Config validation failed" at every start. Non-fatal (the node still
# registers) but it paged REA daily until fixed 2026-08-13. The heartbeat
# scripts' digits-only serverPort grep reads the SERVER config, so it is
# unaffected by the quotes here.
sshm "cat > ~/.apps/tdarr/configs/Tdarr_Node_Config.json" <<JSON
{
  "nodeName": "manitoba-local",
  "serverIP": "127.0.0.1",
  "serverPort": "${SERVER_PORT}",
  "handbrakePath": "",
  "ffmpegPath": "",
  "mkvpropeditPath": "",
  "ccextractorPath": "",
  "pathTranslators": [],
  "logger": {"level": "info"}
}
JSON

# ── Step 6: systemd units (server + node) ──────────────────────────────────
# QFLX-61: every remote step below is its OWN top-level local sshm call. This
# step used to be one big remote `bash -s` heredoc with a second
# `sshm "bash -s" <<'SHIMSCRIPT'` nested inside it. The inner heredoc is just
# text to the local shell, so it ran ON THE BOX, where `sshm` (a local function
# from scripts/lib/ssh.sh) does not exist: exit 127 under the remote `set -e`,
# the node unit was never rewritten, nothing was reloaded or restarted, and the
# local `set -e` killed the installer here. Never nest a local helper inside a
# remote heredoc; tests/unit/test_configure_no_nested_remote_ssh.py guards it.

# 6a: ship the box-side helpers the units and the shim install below USE.
# Both were referenced from ~/scripts/ops but no installer ever shipped them,
# so a fresh slot got a server unit whose ExecStartPre did not exist (start
# refused) and a shim install with nothing to copy.
sshm 'mkdir -p ~/scripts/ops ~/.config/systemd/user'
scpm_to "$HERE/../ops/tdarr-port-drain.sh"     '~/scripts/ops/tdarr-port-drain.sh' >/dev/null
scpm_to "$HERE/../ops/ffmpeg-threadcap-shim.sh" '~/scripts/ops/ffmpeg-threadcap-shim.sh' >/dev/null
sshm 'chmod +x ~/scripts/ops/tdarr-port-drain.sh ~/scripts/ops/ffmpeg-threadcap-shim.sh'

# 6a2: the uid relay (QFLX-97), BEFORE step 6d moves Tdarr onto its unix
# socket. Writes tdarr-uid-relay.socket (systemd holds the port) and .service,
# plus Wants= drop-ins for both Tdarr units; never touches Tdarr itself. While
# Tdarr still holds the port it installs without starting, and 6d hands over.
bash "$HERE/50d-tdarr-uid-relay.sh"

# 6b: server unit.
sshm "bash -s" <<'SERVERUNIT'
set -euo pipefail
cat > ~/.config/systemd/user/tdarr-server.service <<'UNIT'
[Unit]
Description=Tdarr Server (transcoding orchestrator)
After=network-online.target
Wants=network-online.target
# A crash-loop on a SHARED slot is antisocial, so cap it: 5 starts in 5
# minutes, then stay visibly failed and let "Tdarr Server" go red. Systemd's
# default burst is tuned for fast-failing units, not for one that takes ~20s
# to come up.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
# serverIP/HOST are INERT (Tdarr never reads HOST, and serverIP is only
# logged); the listener is pinned by the 50a patch, to a unix socket since
# QFLX-97. Kept so the unit text stays what the box has run since 2026-08-20.
Environment="HOST=127.0.0.1" "serverIP=127.0.0.1"
Type=simple
WorkingDirectory=%h/.apps/tdarr/Tdarr_Server
# Wait for the previous listener to actually let go of the port before binding.
# Without this, restart-then-bind races the old process out of the socket and
# the replacement dies on EADDRINUSE — 6 occurrences across 2026-08-18/20, on
# every restart path (deploy, heartbeat, on-failure). ExecStartPre is the one
# place all of them pass through. `-` prefix is deliberately ABSENT: a drain
# that cannot clear the port must block the start, not be ignored.
ExecStartPre=%h/scripts/ops/tdarr-port-drain.sh
ExecStart=%h/.apps/tdarr/Tdarr_Server/Tdarr_Server
Restart=on-failure
RestartSec=10s
TimeoutStopSec=30
# Kill the whole cgroup on stop, so a lingering child cannot keep the port and
# hand the next start the very race the drain exists to absorb.
KillMode=control-group
StandardOutput=append:%h/.apps/tdarr/logs/server.log
StandardError=append:%h/.apps/tdarr/logs/server.err

[Install]
WantedBy=default.target
UNIT
SERVERUNIT

# 6c: ffmpeg thread cap ------------------------------------------------------
# Install scripts/ops/ffmpeg-threadcap-shim.sh AS Tdarr's ffmpeg binary, in
# BOTH bundles. Same fragility class as the worker1.js null-guard patch above:
# a Tdarr upgrade unzips over node_modules and removes it, so this is
# re-applied on every install and asserted by smoke-test.sh.
#
# Not optional on this slot. `ulimit -u` is 2000 and uncapped ffmpeg threads to
# core count on 128 cores, so ONE job held 129-273 threads: a single transcode
# worker sat at 70.5% of the task ceiling and two took the box to 2000, where
# bash could not fork and cron plus every canary died with it. Capped to 8, a
# job holds ~34 and two workers sit at 52%. See manifest/apps.yaml
# tdarr-node.throttle for the measured table.
#
# Idempotent: the presence of ffmpeg.real means the real binary has already
# been moved aside, and re-copying the shim over the shim is a no-op.
sshm "bash -s" <<'SHIMSCRIPT'
set -uo pipefail
R=$(cd ~ && pwd -P)
for D in "$R/.apps/tdarr/Tdarr_Node/node_modules/ffmpeg-static" \
         "$R/.apps/tdarr/Tdarr_Server/node_modules/ffmpeg-static"; do
  [ -d "$D" ] || continue
  if [ ! -e "$D/ffmpeg.real" ]; then
    mv "$D/ffmpeg" "$D/ffmpeg.real"
  fi
  # Copy beside, then rename over: the node runs 24/7, and `cp` straight over
  # a bash script that a live job is still reading rewrites it under bash's
  # feet. rename(2) swaps the inode atomically; the old reader keeps the old one.
  cp "$R/scripts/ops/ffmpeg-threadcap-shim.sh" "$D/ffmpeg.qflix-new"
  chmod +x "$D/ffmpeg.qflix-new" "$D/ffmpeg.real"
  mv -f "$D/ffmpeg.qflix-new" "$D/ffmpeg"
  head -3 "$D/ffmpeg" | grep -q QFLIX-FFMPEG-THREADCAP     || { echo "shim copied to $D but marker absent" >&2; exit 1; }
done
# Prove the shim did not brick the binary BEFORE the node ever calls it — a
# broken ffmpeg here means every transcode fails, silently, at job time.
V=$("$R/.apps/tdarr/Tdarr_Node/node_modules/ffmpeg-static/ffmpeg" -version 2>&1 | head -1)
case "$V" in
  ffmpeg\ version*) echo "ffmpeg shim OK: $V" ;;
  *) echo "ffmpeg shim BROKEN: $V" >&2; exit 1 ;;
esac
SHIMSCRIPT

# 6d: node unit, then ONE daemon-reload covering both units, enable, restart.
sshm "bash -s" <<'NODEUNIT'
set -euo pipefail
cat > ~/.config/systemd/user/tdarr-node.service <<'UNIT'
[Unit]
Description=Tdarr Node (transcoding worker)
After=network-online.target tdarr-server.service
Wants=network-online.target
Requires=tdarr-server.service

[Service]
Type=simple
WorkingDirectory=%h/.apps/tdarr/Tdarr_Node
ExecStart=%h/.apps/tdarr/Tdarr_Node/Tdarr_Node
Restart=on-failure
RestartSec=10s
TimeoutStopSec=30
Nice=10
StandardOutput=append:%h/.apps/tdarr/logs/node.log
StandardError=append:%h/.apps/tdarr/logs/node.err

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable tdarr-server.service tdarr-node.service
# Stop, take the port, start (QFLX-97). A Tdarr still on the QFLX-96 TCP bind
# holds 127.0.0.1:<port> until it stops; the relay socket takes it the moment
# it is free, before the patched Tdarr comes up on its unix socket. On a box
# already moved over, the socket start is a no-op and this is a plain restart.
systemctl --user stop tdarr-server.service
systemctl --user start tdarr-uid-relay.socket tdarr-uid-relay.service
systemctl --user start tdarr-server.service
sleep 5
systemctl --user restart tdarr-node.service
NODEUNIT
sleep 5
sshm 'systemctl --user is-active tdarr-server.service' | grep -q active || die "tdarr-server not active"
sshm 'systemctl --user is-active tdarr-node.service'   | grep -q active || die "tdarr-node not active"
log_info "tdarr-server.service + tdarr-node.service active"

# ── Step 7: heartbeat crons ────────────────────────────────────────────────
sshm 'mkdir -p ~/scripts/ops'
scpm_to "$HERE/../ops/heartbeat-tdarr-server.sh" '~/scripts/ops/heartbeat-tdarr-server.sh' >/dev/null
scpm_to "$HERE/../ops/heartbeat-tdarr-node.sh"   '~/scripts/ops/heartbeat-tdarr-node.sh' >/dev/null
sshm 'chmod +x ~/scripts/ops/heartbeat-tdarr-*.sh
(crontab -l 2>/dev/null | grep -v "heartbeat-tdarr"; \
 echo "*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-tdarr-server.sh"; \
 echo "*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-tdarr-node.sh") | crontab -'
log_info "heartbeat crons installed"

# ── Step 8: no public route (QFLX-96) ──────────────────────────────────────
# This step used to write proxy.d/tdarr.conf, with a comment claiming the parent
# server block enforced auth_basic. It never did: nothing in nginx.conf or
# sites-available/default sets auth_basic for it, so the fragment published the
# admin UI and API to anyone. Tdarr has no auth of its own, so there is nothing
# for a route to lean on. Reach it over the tunnel (docs/internal-app-tunnels.md).
# Retiring here, not just no longer writing, means a re-run cleans an old slot.
sshm 'bash -s' <<'NGXSCRIPT'
set -euo pipefail
F=~/.apps/nginx/proxy.d/tdarr.conf
[ -e "$F" ] || exit 0
mv "$F" "$F.bak-$(date -u +%Y%m%dT%H%M%SZ)"
/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t
systemctl --user reload nginx
NGXSCRIPT
log_info "no public /tdarr/ route"

# ── Step 9: verify (allow server boot time) ────────────────────────────────
# Fails closed. A public 200 from either path is an open admin API, so it stops
# the install; the listener checks are what prove the running processes took
# the patch: Tdarr on its unix socket, and the ONLY TCP listener on the port
# being the relay.
#
# Polled, not sampled once. Only ~15s separate the step 6d restart from here,
# and this box has measured 10.8s, 52s, 54s and (first start after a reboot)
# ~124s from start to listen. One sample turned a slow-but-good start into a
# false failure AFTER every step had applied. The loop runs on the box in ONE
# session (no SSH per tick) and stops once Tdarr answers on its socket, so a
# normal start costs nothing.
SOCK_DIR='/run/user/$(id -u)/qflix-tdarr'
UP=$(sshm "for _ in \$(seq 1 60); do curl -sfm 5 --unix-socket ${SOCK_DIR}/tdarr.sock -o /dev/null http://localhost/api/v2/status && { echo up; break; }; sleep 3; done" || true)
[ "$UP" = up ] || die "tdarr not answering on its unix socket after 180s — a start problem; check ~/.apps/tdarr/logs/server.err"

# "Not listening", "listening somewhere else" and "a Tdarr process owns TCP"
# are different faults with different fixes, so they die with different words.
BIND=$(sshm "ss -ltnH 'sport = :${SERVER_PORT}' | awk '{print \$4}' | sort -u | tr '\n' ' '" || true)
[ -n "$BIND" ] || die "nothing listening on :${SERVER_PORT} — tdarr-uid-relay is down; check ~/.apps/tdarr/logs/relay.log"
[ "$BIND" = "127.0.0.1:${SERVER_PORT} " ] || die "listening on '${BIND}', not 127.0.0.1 only"
# The fd is shared by the systemd user manager (tdarr-uid-relay.socket) and the
# relay it was passed to; nobody else may hold it.
OWNER=$(sshm "ss -ltnpH 'sport = :${SERVER_PORT}' | grep -oP 'pid=\K[0-9]+' | sort -u | tr '\n' ' '" || true)
RELAY_PID=$(sshm 'systemctl --user show -p MainPID --value tdarr-uid-relay.service' || true)
MGR_PID=$(sshm "ps -o ppid= -p '${RELAY_PID:-0}' | tr -d ' '" || true)
[ "$(sshm 'systemctl --user is-active tdarr-uid-relay.socket' || true)" = active ] || die "tdarr-uid-relay.socket is not active — the port is not held by systemd"
case " $OWNER" in *" ${RELAY_PID} "*) ;; *) die "port :${SERVER_PORT} owned by pid '${OWNER}', not tdarr-uid-relay (${RELAY_PID}) — is 50a applied and tdarr-server restarted?" ;; esac
for P in $OWNER; do
  [ "$P" = "$RELAY_PID" ] || [ "$P" = "$MGR_PID" ] || die "port :${SERVER_PORT} also held by pid $P, which is neither the relay nor its systemd manager"
done
TCP_TDARR=$(sshm "ss -ltnpH | grep -c '\"Tdarr_' || true" || true)
[ "${TCP_TDARR:-1}" = 0 ] || die "a Tdarr process still owns a TCP listener — is 50a applied and tdarr-server restarted?"
MODE=$(sshm "stat -c %a ${SOCK_DIR}" || true)
[ "$MODE" = 700 ] || die "tdarr socket directory mode '${MODE}', expected 700"

LOOPBACK=$(sshm "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:${SERVER_PORT}/api/v2/status" || true)
[ "$LOOPBACK" = 200 ] || die "tdarr via the relay HTTP $LOOPBACK — check ~/.apps/tdarr/logs/relay.log"

ROUTE=$(curl -sk -m 10 -o /dev/null -w '%{http_code}' "https://${PUBLIC_HOST}/tdarr/api/v2/status" || true)
[ "$ROUTE" != 200 ] || die "tdarr answers 200 on the public route"

DIRECT=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://${PUBLIC_HOST}:${SERVER_PORT}/api/v2/status" || true)
[ "$DIRECT" != 200 ] || die "tdarr answers 200 on the public port"
log_info "✓ tdarr on a 0700 unix socket, relay owns 127.0.0.1:${SERVER_PORT} (route HTTP $ROUTE, port HTTP $DIRECT)"

log_info "Phase 27+28 complete — Tdarr Server + Node running. API/UI: 127.0.0.1:${SERVER_PORT} through tdarr-uid-relay (our uid only)"
log_info "Phase 29-31 (library config + workflows + ops) stay operator-deferred — see docs/operator-deferred.md"
