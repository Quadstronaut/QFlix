#!/usr/bin/env bash
# Tautulli<->Plex connectivity canary: assert the Plex address Tautulli is
# CONFIGURED to use is a live Plex server — not just that Tautulli's own web
# port answers.
#
# Why this exists: on 2026-05-20 the Ultra.cc kernel migration re-IP'd Plex
# (172.17.1.250:32400 -> 172.17.0.1:17025). Tautulli's pinned pms_url broke and
# it stormed `[Errno 111] Connection refused` for three days — but its OWN web
# port stayed up, so the Tautulli app monitor showed green and Kuma never
# alerted. This canary closes that gap: it reads Tautulli's configured pms
# target and probes THAT for a real Plex /identity. If Tautulli is pointed at a
# dead/old address, this fails within one cadence (~15 min) instead of never.
#
# The probe runs from the host but targets Tautulli's exact configured
# pms_ip:port — the docker bridge gateway is reachable from the host too — so a
# green here means the address Tautulli will dial is a live Plex. This is
# independent of whether the Tautulli process is up (the app monitor owns that);
# it validates the *configuration*, which is the thing that silently rotted.
#
# Stage labels (failure msg on stderr -> Kuma msg=):
#   STAGE=no-config         Tautulli config.ini / pms fields unreadable
#   STAGE=plex-unreachable  Tautulli's configured pms target refused/timed-out/non-200
#   STAGE=not-plex          target answered but is not a Plex server (no MediaContainer)
#
# Exits: 0 pass · 1 fail (STAGE label on stderr)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
CFG=$HOME/.apps/tautulli/config.ini
[ -r "$CFG" ] || { echo "STAGE=no-config msg=cannot-read-config" >&2; exit 1; }
readvar() { awk -F" = " "/^$1 /{print \$2; exit}" "$CFG"; }
PMS_IP=$(readvar pms_ip)
PMS_PORT=$(readvar pms_port)
PMS_SSL=$(readvar pms_ssl)
[ -n "$PMS_IP" ] && [ -n "$PMS_PORT" ] || { echo "STAGE=no-config msg=ip=$PMS_IP-port=$PMS_PORT" >&2; exit 1; }
SCHEME=http; [ "$PMS_SSL" = "1" ] && SCHEME=https
BASE="${SCHEME}://${PMS_IP}:${PMS_PORT}"
echo "TARGET=$BASE"

# Capture body + code in one call: /identity needs no token and returns a
# <MediaContainer ... machineIdentifier=...> on a real Plex.
OUT=$(curl -sk -m 10 -w "\nHTTP=%{http_code}" "${BASE}/identity" 2>/dev/null) || true
CODE=$(printf "%s" "$OUT" | sed -n "s/^HTTP=//p" | tail -1)
echo "CODE=$CODE"
[ "$CODE" = "200" ] || { echo "STAGE=plex-unreachable msg=target=$BASE-code=${CODE:-000}" >&2; exit 1; }
printf "%s" "$OUT" | grep -q "MediaContainer" || { echo "STAGE=not-plex msg=target=$BASE-no-MediaContainer" >&2; exit 1; }
') || RC=$?
RC=${RC:-0}
echo "$RES"

STAGE_LINE=$(printf "%s\n" "$RES" | grep "^STAGE=" || true)
if [ -n "$STAGE_LINE" ] || [ "$RC" != "0" ]; then
  [ -n "$STAGE_LINE" ] && echo "$STAGE_LINE" >&2
  exit 1
fi

TARGET=$(printf "%s\n" "$RES" | grep -oE 'TARGET=[^ ]+' | cut -d= -f2-)
echo "PASS: tautulli-plex-link — Tautulli's configured Plex (${TARGET}) is a live server"
