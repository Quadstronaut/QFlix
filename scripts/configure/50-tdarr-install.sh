#!/usr/bin/env bash
# Phase 27 + 28 — Tdarr Server + Node install. Idempotent.
#  - Tdarr_Updater downloads pinned Server + Node binaries
#  - Server config: 127.0.0.1:<port>, auth=true, seeded API key
#  - Node config: points at local Server
#  - user-systemd services for both
#  - nginx /tdarr/ fragment (htpasswd-protected by parent server block)
#  - heartbeat crons for both
#  - Phases 29-31 (library config + workflows + ops cleanup) stay operator-deferred
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"
source "$HERE/../lib/secrets.sh"

# Pinned to 2.17.01. The plan suggested 2.45.01 but:
#   1. storage.tdarr.io 404s on that version
#   2. Tdarr_Updater self-updates to whatever is "latest" (currently 2.71.01),
#      ignoring our pin
#   3. Tdarr_Server 2.71.01 requires GLIBC_2.34, but Debian 11 (Ultra.cc)
#      has GLIBC_2.31 — hard runtime incompatibility
# 2.17.01 is the last version that ships with Server + Node binaries built
# against an older glibc. We bypass Tdarr_Updater entirely and download the
# Server.zip + Node.zip artifacts directly.
TDARR_VER="2.17.01"
# Hard requirement — placeholder must never reach a live curl/nginx.
PUBLIC_HOST="$(secret_read seedbox.host)"

# ── Step 1: claim port (Tdarr 2.17 serves Web UI + API + Node-protocol on
#    a single serverPort. webUIPort is NOT bound, but the server's
#    internal redirect builder defaults to :8265 unless we set it — so
#    pin it to the same value as serverPort below). ──────────────────────
if ! secret_exists tdarr.server_port; then
  # Cross-check against every existing secrets/*.port to avoid double-claim.
  # The earlier conjurr/newsletterr-specific dedup was wrong (those secrets
  # were purged 2026-05-11; secret_read on a missing file dies, so the
  # 2>/dev/null hid the error and the dedup silently degraded).
  USED=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../secrets" && cat *.port 2>/dev/null | sort -u)
  PORT=$(sshm "app-ports free 2>/dev/null | grep -E '^[0-9]+\$'" | grep -vxF "$USED" | head -1)
  [ -n "$PORT" ] || die "no free port for tdarr"
  secret_write tdarr.server_port "$PORT"
fi
SERVER_PORT=$(secret_read tdarr.server_port)
log_info "tdarr port (UI + API + Node) = $SERVER_PORT"

# ── Step 2: API key ─────────────────────────────────────────────────────────
if ! secret_exists tdarr.api_key; then
  KEY=$(sshm 'openssl rand -hex 16')
  secret_write tdarr.api_key "tapi_${KEY}"
fi
API_KEY=$(secret_read tdarr.api_key)

# ── Step 3: download Server + Node zips directly (NOT via Tdarr_Updater) ───
# Tdarr_Updater self-updates to latest (currently incompatible with glibc 2.31).
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

# ── Step 4: server config ──────────────────────────────────────────────────
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
sshm "bash -s" <<'UNITSCRIPT'
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
# serverIP/HOST pin the listener to loopback (nginx fronts it). This lived only
# on the box until 2026-08-20 — the installer wrote a unit without it, so any
# re-run silently un-pinned the listener.
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

# --- ffmpeg thread cap ------------------------------------------------------
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
  cp "$R/scripts/ops/ffmpeg-threadcap-shim.sh" "$D/ffmpeg"
  chmod +x "$D/ffmpeg" "$D/ffmpeg.real"
done
# Prove the shim did not brick the binary BEFORE the node ever calls it — a
# broken ffmpeg here means every transcode fails, silently, at job time.
V=$("$R/.apps/tdarr/Tdarr_Node/node_modules/ffmpeg-static/ffmpeg" -version 2>&1 | head -1)
case "$V" in
  ffmpeg\ version*) echo "ffmpeg shim OK: $V" ;;
  *) echo "ffmpeg shim BROKEN: $V" >&2; exit 1 ;;
esac
SHIMSCRIPT

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
systemctl --user restart tdarr-server.service
sleep 5
systemctl --user restart tdarr-node.service
UNITSCRIPT
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

# ── Step 8: nginx /tdarr/ fragment (Tdarr supports webUIBaseUrl natively) ──
sshm "PORT=${SERVER_PORT} bash -s" <<'NGXSCRIPT'
cat > ~/.apps/nginx/proxy.d/tdarr.conf <<NGX
location /tdarr/ {
    # Parent server block enforces auth_basic. Tdarr's own auth=true is
    # defense in depth on the API itself.
    proxy_pass http://127.0.0.1:${PORT}/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-Prefix /tdarr;

    # WebSockets — Tdarr Web UI streams job updates live
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 1d;
    proxy_send_timeout 1d;
    proxy_buffering off;
}
NGX
/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t
systemctl --user reload nginx
NGXSCRIPT
log_info "nginx /tdarr/ fragment live"

# ── Step 9: verify (allow server boot time) ────────────────────────────────
sleep 5
HTPW=$(secret_read htpasswd.password)
HTTP=$(curl -sk -m 10 -u "quadstronaut:${HTPW}" -o /dev/null -w '%{http_code}' "https://${PUBLIC_HOST}/tdarr/api/v2/status")
case "$HTTP" in
  200|401) log_info "✓ tdarr reachable through nginx (HTTP $HTTP — 401 is OK if Tdarr auth gates the path)" ;;
  *)       log_warn "tdarr returned HTTP $HTTP — give it 60s and try again, or check ~/.apps/tdarr/logs/server.err" ;;
esac

log_info "Phase 27+28 complete — Tdarr Server + Node running. Web UI: https://${PUBLIC_HOST}/tdarr/"
log_info "Phase 29-31 (library config + workflows + ops) stay operator-deferred — see docs/operator-deferred.md"
