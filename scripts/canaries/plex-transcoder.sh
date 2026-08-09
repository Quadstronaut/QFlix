#!/usr/bin/env bash
# Plex-transcoder canary: assert Plex's transcoding subsystem is responsive.
#
# Why this exists: if Plex's transcoder daemon dies or its session manager
# stalls, every customer playback that needs a transcode (per Tautulli's
# 60% recent rate) will buffer or fail with "Conversion failed." The Plex
# server's main API will still return 200 OK on /identity, and Kuma will
# show green, so the issue is invisible until a customer complains.
#
# Probe: hit /transcode/sessions (lists active transcodes; returns empty
# array if none, which is fine) AND /:/prefs (returns the long config
# blob, which exercises the metadata subsystem the transcoder needs). If
# either hangs >10s or returns non-2xx, transcoder is degraded.
#
# Stage labels (failure messages on stderr → Kuma msg=):
#   STAGE=plex-up-fail          Plex /identity returned non-200 (server-down)
#   STAGE=transcode-api-fail    /transcode/sessions returned non-200 or hung
#   STAGE=prefs-api-fail        /:/prefs returned non-200 or hung
#
# Exits:
#   0 — pass (both endpoints respond in <10s with 2xx)
#   1 — fail (STAGE label on stderr)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
PLEX_HOST=$(cat ~/secrets/plex.host 2>/dev/null)
PLEX_PORT=$(cat ~/secrets/plex.port 2>/dev/null)
TOKEN=$(cat ~/secrets/plex.token 2>/dev/null)
BASE="http://${PLEX_HOST}:${PLEX_PORT}"

# Identity — server-up probe. Cheap, no auth needed.
ID_CODE=$(curl -sk -m 10 -o /dev/null -w "%{http_code}" "${BASE}/identity")
echo "ID_CODE=$ID_CODE"
[ "$ID_CODE" = "200" ] || { echo "STAGE=plex-up-fail msg=identity=$ID_CODE" >&2; exit 1; }

# Transcode sessions — exercises the transcoder process namespace.
TS_OUT=$(curl -sk -m 10 -o /dev/null -w "%{http_code} %{time_total}" \
  -H "Accept: application/json" -H "X-Plex-Token: $TOKEN" \
  "${BASE}/transcode/sessions")
TS_CODE=$(echo "$TS_OUT" | awk "{print \$1}")
TS_TIME=$(echo "$TS_OUT" | awk "{print \$2}")
echo "TS_CODE=$TS_CODE TS_TIME=$TS_TIME"
[ "$TS_CODE" = "200" ] || { echo "STAGE=transcode-api-fail msg=code=$TS_CODE-time=$TS_TIME" >&2; exit 1; }

# Prefs — exercises Plex Media Server config subsystem (the transcoder
# reads these on every session start). If this hangs, transcoder will too.
PR_OUT=$(curl -sk -m 10 -o /dev/null -w "%{http_code} %{time_total}" \
  -H "Accept: application/json" -H "X-Plex-Token: $TOKEN" \
  "${BASE}/:/prefs")
PR_CODE=$(echo "$PR_OUT" | awk "{print \$1}")
PR_TIME=$(echo "$PR_OUT" | awk "{print \$2}")
echo "PR_CODE=$PR_CODE PR_TIME=$PR_TIME"
[ "$PR_CODE" = "200" ] || { echo "STAGE=prefs-api-fail msg=code=$PR_CODE-time=$PR_TIME" >&2; exit 1; }
') || RC=$?
RC=${RC:-0}
echo "$RES"

STAGE_LINE=$(printf "%s\n" "$RES" | grep "^STAGE=" || true)
if [ -n "$STAGE_LINE" ] || [ "$RC" != "0" ]; then
  [ -n "$STAGE_LINE" ] && echo "$STAGE_LINE" >&2
  exit 1
fi

TS_TIME=$(printf "%s\n" "$RES" | grep -oE 'TS_TIME=[0-9.]+' | cut -d= -f2)
PR_TIME=$(printf "%s\n" "$RES" | grep -oE 'PR_TIME=[0-9.]+' | cut -d= -f2)
echo "PASS: plex-transcoder — sessions=${TS_TIME}s prefs=${PR_TIME}s"
