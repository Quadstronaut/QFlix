#!/usr/bin/env bash
# Plex-transcoder canary: assert Plex's transcode-related HTTP ENDPOINTS are
# responsive. It is an API liveness probe. It is NOT a playback test.
#
# ── WHAT THIS DOES NOT COVER (read this first) ───────────────────────────────
# Every leg below is a GET that a healthy HTTP handler satisfies. Nothing here
# decodes a frame, spawns Plex Transcoder, runs EasyAudioEncoder, or moves one
# byte of transcoded output. That is not a tuning gap — it is a structural one:
# this probe and a member's playback failure are ORTHOGONAL signals, so no
# threshold change to this file could ever make it see one.
#
# It was measured. During a 26-day window in which EVERY movie failed to play for
# a real member, this canary was green on every single tick, and correctly so:
# /identity, /transcode/sessions and /:/prefs were all genuinely fine the whole
# time. Same shape as tdarr-healthcheck (transcodes succeeded while 100% of
# health checks failed for 68 days) and dash-asset-integrity (/healthz answered
# while the served shell could not hydrate for ~22h).
#
# The actual playback assertion lives in its own module, its own timer and its
# own Kuma monitor — scripts/canaries/plex-playback.sh, "Canary Plex Playback" —
# which picks the library's worst-case item (highest-bitrate title with lossless
# multichannel audio), forces a full software downscale plus an EAE downmix, and
# fails unless real MPEG-TS segment bytes come back. Separate module per the
# operator compartmentalise-for-migration law: a 10-minute liveness probe and a
# 30-minute load test have different cadences, different failure modes and
# different remedies, and one monitor meaning two things helps nobody.
#
# KEEP BOTH. This one is cheap, runs 3x more often, and distinguishes "the
# transcode API is wedged" (this file reds, playback reds) from "the API is fine
# and the transcoder produces nothing" (only plex-playback reds) — different
# faults, different operator action. Its logic is deliberately unchanged.
#
# ── WHAT THIS DOES COVER ─────────────────────────────────────────────────────
# If Plex's session manager stalls or its transcode endpoints hang, every
# playback that needs a transcode (per Tautulli, ~60% of recent sessions) will
# buffer or fail with "Conversion failed" while /identity still returns 200 OK
# and the app monitor stays green.
#
# Probe: hit /transcode/sessions (lists active transcodes; an empty array is a
# healthy answer) AND /:/prefs (the long config blob, which exercises the
# metadata subsystem the transcoder reads on every session start). If either
# hangs >10s or returns non-2xx, the transcode API is degraded.
#
# Stage labels (failure messages on stderr → Kuma msg=):
#   STAGE=plex-up-fail          Plex /identity returned non-200 (server-down)
#   STAGE=transcode-api-fail    /transcode/sessions returned non-200 or hung
#   STAGE=prefs-api-fail        /:/prefs returned non-200 or hung
#
# Exits:
#   0 — pass (both endpoints respond in <10s with 2xx). NOTE: this says the
#       endpoints answered, and nothing whatsoever about whether a member can
#       play a file. plex-playback.sh answers that question.
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
