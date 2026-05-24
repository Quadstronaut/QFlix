#!/usr/bin/env bash
# Tautulli inside Ultra.cc's Docker container can't resolve Plex's
# `*.plex.direct` hostname (Docker's DNS doesn't have a path to plex.direct
# from inside the container). When pms_url is auto-populated with the
# plex.direct URI, every call to /library/metadata/<key> on the PMS dies
# with NameResolutionError → Tautulli's `get_metadata_details` returns None
# silently, which cascades to:
#   - API `cmd=get_metadata` responds 200/success with `data: {}`
#   - WebSocket session processing throws TypeError on `metadata['markers']`
#   - Anything downstream that needs Plex metadata (qflix-newsletter
#     enrichment, watch-history detail, etc.) silently no-ops.
#
# Fix: pin Tautulli to the docker bridge GATEWAY (172.17.0.1) + the canonical
# Plex port over plain HTTP, and set pms_url_manual=1 so Tautulli's 12-hourly
# resource refresh does NOT overwrite the URL with the plex.direct one again.
# Plex on 172.17.x.x is loopback-equivalent on this seedbox, so HTTP is safe.
#
# WHY THE GATEWAY, not the IP already in config: the previous version of this
# script re-pinned whatever pms_ip Tautulli's resource-refresh had last written.
# That is brittle — Ultra.cc's 2026-05-20 kernel-migration re-IP'd Plex from
# 172.17.1.250:32400 to the bridge gateway 172.17.0.1:17025, Tautulli's stale
# pin broke, and it stormed `Connection refused` for three days. Ultra.cc
# confirmed 172.17.0.1:<plex.port> is the binding "going forward". The gateway
# is stable across container reassignment (unlike a per-container IP), and is
# the same address the manifest already uses for FlareSolverr. So we derive the
# target from the gateway + secrets/plex.port, never from the live config.
#
# Idempotent: detects an already-correct config and exits 0.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

# Everything runs in a single remote shell. The remote script is
# heredoc'd verbatim so PMS IP/port get resolved from the live config,
# not from anything baked in here.
sshm "bash -s" <<'REMOTE'
set -euo pipefail
CFG=$HOME/.apps/tautulli/config.ini

# Canonical Plex target for a CONTAINERISED Tautulli: the docker bridge gateway
# (stable across container reassignment), NOT a per-container IP and NOT
# 127.0.0.1 (which is the *container's* loopback, not the host's). Derived from
# the gateway constant + secrets/plex.port — never from the possibly-drifted
# pms_ip already in config. See the header for the 2026-05-20 incident.
GATEWAY=172.17.0.1
PORT=$(cat ~/secrets/plex.port)
WANT_URL="http://${GATEWAY}:${PORT}"

readvar() { awk -F' = ' "/^$1 /{print \$2; exit}" "$CFG"; }
PMS_IP=$(readvar pms_ip)
PMS_PORT=$(readvar pms_port)
PMS_SSL=$(readvar pms_ssl)
PMS_URL=$(readvar pms_url)
PMS_URL_MANUAL=$(readvar pms_url_manual)

if [ "$PMS_IP" = "$GATEWAY" ] && [ "$PMS_PORT" = "$PORT" ] && \
   [ "$PMS_SSL" = "0" ] && [ "$PMS_URL_MANUAL" = "1" ] && [ "$PMS_URL" = "$WANT_URL" ]; then
  echo "Tautulli already pinned to $WANT_URL — nothing to do"
  exit 0
fi

echo "patching Tautulli config:"
echo "  pms_ip:         $PMS_IP -> $GATEWAY"
echo "  pms_port:       $PMS_PORT -> $PORT"
echo "  pms_ssl:        $PMS_SSL -> 0"
echo "  pms_url_manual: $PMS_URL_MANUAL -> 1"
echo "  pms_url:        $PMS_URL -> $WANT_URL"

cp "$CFG" "$CFG.bak.$(date +%s)"

app-tautulli stop >/dev/null 2>&1 || true
for _ in 1 2 3 4 5; do
  pgrep -f 'Tautulli.py' >/dev/null || break
  sleep 1
done

sed -i \
  -e "s|^pms_ip = .*|pms_ip = ${GATEWAY}|" \
  -e "s|^pms_port = .*|pms_port = ${PORT}|" \
  -e 's|^pms_ssl = .*|pms_ssl = 0|' \
  -e 's|^pms_url_manual = .*|pms_url_manual = 1|' \
  -e "s|^pms_url = .*|pms_url = ${WANT_URL}|" \
  "$CFG"

grep -E '^pms_(ip|port|ssl|url|url_manual) ' "$CFG"

app-tautulli start >/dev/null 2>&1 || true
sleep 12

# Smoke test: trigger the exact sequence that used to break.
KEY=$(cat ~/secrets/tautulli.key)
PORT=$(cat ~/secrets/tautulli.port)
RK=$(curl -s "http://127.0.0.1:$PORT/tautulli/api/v2?apikey=$KEY&cmd=get_recently_added&count=1" \
  | python3 -c 'import sys,json; r=json.load(sys.stdin)["response"]["data"]["recently_added"]; print(r[0].get("grandparent_rating_key") or r[0]["rating_key"])')
DLEN=$(curl -s "http://127.0.0.1:$PORT/tautulli/api/v2?apikey=$KEY&cmd=get_metadata&rating_key=$RK" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin)["response"]["data"]; print(len(d) if isinstance(d, dict) else 0)')

if [ "$DLEN" -gt 10 ]; then
  echo "smoke: PASS — get_metadata returned $DLEN keys immediately after get_recently_added"
else
  echo "smoke: FAIL — get_metadata returned $DLEN keys (plex.direct DNS bug still biting)" >&2
  exit 1
fi
REMOTE
