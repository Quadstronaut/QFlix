#!/usr/bin/env bash
# Movie canary: drive a real Seerr request through to Radarr to verify
# the Seerr-in-container → Radarr-in-container netns hop is healthy.
#
# The prior probe ran on the host netns (`curl 127.0.0.1:17027/...`) and
# stayed green for ~9h on 2026-05-11 while every Seerr→Radarr request was
# failing with `ECONNREFUSED 127.0.0.1:17027` inside Seerr's container.
# That blind spot is the reason for this rewrite — see
# `reference_ucc-docker-host-loopback`.
#
# Method: pick the lowest-id movie already in Radarr, POST a Seerr request
# for its tmdbId, poll the request until Seerr populates
# `media.externalServiceId` (= it successfully reached Radarr), verify the
# id matches Radarr's record for that tmdbId, then DELETE the Seerr
# request. The Radarr movie is untouched.
#
# Stage labels (printed to stderr on failure → Kuma `msg=`):
#   seerr-up-fail        — Seerr API unreachable
#   radarr-up-fail       — Radarr API unreachable
#   seerr-api-fail       — a Seerr JSON read gave no JSON after 3 tries
#   radarr-api-fail      — a Radarr JSON read gave no JSON after 3 tries
#   seed-pick-fail      — a seed exists but couldn't resolve its tmdb id
#                          (an EMPTY but reachable library SKIPs green instead)
#   seerr-push-fail      — POST /api/v1/request returned non-2xx/409
#   arr-not-populated    — externalServiceId stayed null after timeout
#   verify-fail          — externalServiceId did not match Radarr movie id
#   cleanup-fail         — DELETE request returned non-2xx (warned, not fatal)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
JS_PORT=$(cat ~/secrets/seerr.port)
JS_KEY=$(cat ~/secrets/seerr.key)
RADARR_PORT=$(cat ~/secrets/radarr.port)
RADARR_KEY=$(cat ~/secrets/radarr.key)
RADARR_URLBASE=$(cat ~/secrets/radarr.urlbase 2>/dev/null || echo radarr)
JS=http://127.0.0.1:${JS_PORT}
RR=http://127.0.0.1:${RADARR_PORT}/${RADARR_URLBASE}/api/v3

# Retry transient 5xx/timeouts up to 3x before declaring a service down — a
# single shared-box 500 was firing this canary (tuned 2026-06-27).
http_up() { local c; for _ in 1 2 3; do
  c=$(curl -s -o /dev/null -w "%{http_code}" -m 6 -H "X-Api-Key: $2" "$1")
  [ "$c" = "200" ] && return 0; sleep 3; done; printf "%s" "$c"; return 1; }
H_JS=$(http_up "${JS}/api/v1/status" "$JS_KEY") || { printf "STAGE=seerr-up-fail msg=seerr-status-http-%s-after-3-tries\n" "$H_JS" >&2; exit 1; }
H_RR=$(http_up "${RR}/system/status" "$RADARR_KEY") || { printf "STAGE=radarr-up-fail msg=radarr-status-http-%s-after-3-tries\n" "$H_RR" >&2; exit 1; }

# Every JSON read goes through jget (QFLX-115). The old `curl -sf | python3
# json.load` pipelines turned one empty answer (a curl timeout on Seerr
# settings/radarr, 2026-11-11 05:31 UTC) into a Python traceback: Kuma got no
# STAGE= line and the failure looked like a canary bug. jget retries 3x like
# http_up, checks the body parses as JSON, and returns 1 so the CALLER names
# the stage. The parsers below then only ever see valid JSON.
fail() { printf "STAGE=%s msg=%s\n" "$1" "$2" >&2; exit 1; }
# On failure jget prints "curl-rc-<rc>-http-<code>" of the LAST try (no -f, so
# a 401/403 reads as http-401, not as a timeout) for the caller to put in msg.
jget() { local out rc code body; for _ in 1 2 3; do
  out=$(curl -s -m 8 -H "X-Api-Key: $2" -w "__HTTP__%{http_code}" "$1"); rc=$?
  code=${out##*__HTTP__}; body=${out%__HTTP__*}
  case "$out" in *__HTTP__*) ;; *) code=000 ;; esac
  case "$rc:$code" in
    0:2??) printf "%s" "$body" | python3 -c "import sys, json; json.load(sys.stdin)" 2>/dev/null \
             && { printf "%s" "$body"; return 0; } ;;
  esac
  sleep 3; done
  printf "curl-rc-%s-http-%s" "$rc" "${code:-000}"; return 1; }

MOVIES=$(jget "${RR}/movie" "$RADARR_KEY") || fail radarr-api-fail "movie-list-no-json-after-3-tries-${MOVIES}"
SEED=$(printf "%s" "$MOVIES" | python3 -c "import sys, json
ms = sorted(json.load(sys.stdin), key=lambda x: x[\"id\"])
print(ms[0][\"tmdbId\"], ms[0][\"id\"]) if ms else exit(2)" 2>/dev/null)
# rc 2 = empty library (SKIP below); anything else non-zero = a shape we cannot read.
SEED_RC=$?
[ "$SEED_RC" = "0" ] || [ "$SEED_RC" = "2" ] || fail seed-pick-fail "movie-list-unparseable-rc-${SEED_RC}"
# Empty-but-reachable library is a legitimate content state, NOT a Seerr->Radarr
# path failure. Radarr already passed its up-check above, so 0 movies = nothing
# to seed = inconclusive: pass with an explicit SKIP message rather than
# false-red the pipeline. A genuine Radarr outage trips radarr-up-fail earlier.
if [ -z "$SEED" ]; then
  printf "PASS: movie canary — SKIP: Radarr up but 0 movies (empty movie library); Seerr->Radarr path not exercised\n"
  exit 0
fi
TMDB_ID=$(printf "%s" "$SEED" | cut -d" " -f1)
RR_MID=$(printf "%s" "$SEED" | cut -d" " -f2)

RSET=$(jget "${JS}/api/v1/settings/radarr" "$JS_KEY") || fail seerr-api-fail "settings-radarr-no-json-after-3-tries-${RSET}"
SRV=$(printf "%s" "$RSET" | python3 -c "import sys, json
d=json.load(sys.stdin)
df=[s for s in d if s.get(\"isDefault\")]
print(df[0][\"id\"] if df else (d[0][\"id\"] if d else \"\"))" 2>/dev/null)
[ -n "$SRV" ] || { printf "STAGE=seerr-push-fail msg=no-default-radarr-server-in-seerr\n" >&2; exit 1; }

curl -s -o /dev/null -m 8 -H "X-Api-Key: $JS_KEY" "${JS}/api/v1/movie/${TMDB_ID}"

BODY=$(printf "{\"mediaType\":\"movie\",\"mediaId\":%s,\"serverId\":%s}" "$TMDB_ID" "$SRV")
RESP=$(curl -s -m 12 -X POST -H "X-Api-Key: $JS_KEY" -H "Content-Type: application/json" \
  --data "$BODY" "${JS}/api/v1/request" -w "\n__HTTP__=%{http_code}")
HTTP=$(printf "%s" "$RESP" | grep -oE "__HTTP__=[0-9]+" | cut -d= -f2)
BODYTXT=$(printf "%s" "$RESP" | sed -e "s/__HTTP__=[0-9]*//")
case "$HTTP" in
  200|201)
    REQ_ID=$(printf "%s" "$BODYTXT" | python3 -c "import sys, json; print(json.load(sys.stdin)[\"id\"])" 2>/dev/null)
    [ -n "$REQ_ID" ] || fail seerr-push-fail "http-${HTTP}-but-no-request-id-in-body"
    CREATED=1
    ;;
  409)
    REQS=$(jget "${JS}/api/v1/request?take=200" "$JS_KEY") || fail seerr-api-fail "request-list-no-json-after-3-tries-${REQS}"
    REQ_ID=$(printf "%s" "$REQS" | python3 -c "import sys, json
d=json.load(sys.stdin)
hits=[r for r in d.get(\"results\",[]) if r.get(\"media\",{}).get(\"tmdbId\")==${TMDB_ID}]
print(hits[0][\"id\"] if hits else \"\")" 2>/dev/null)
    [ -n "$REQ_ID" ] || { printf "STAGE=seerr-push-fail msg=409-but-no-existing-request-tmdb-%s\n" "$TMDB_ID" >&2; exit 1; }
    CREATED=0
    ;;
  *)
    SHORT=$(printf "%s" "$BODYTXT" | tr -d "\n" | cut -c1-80)
    printf "STAGE=seerr-push-fail msg=http-%s-body-%s\n" "$HTTP" "$SHORT" >&2
    exit 1
    ;;
esac

EXTID=""
for _ in 1 2 3 4 5 6; do
  EXTID=$(curl -sf -m 5 -H "X-Api-Key: $JS_KEY" "${JS}/api/v1/request/${REQ_ID}" \
    | python3 -c "import sys, json
r=json.load(sys.stdin)
v=r.get(\"media\",{}).get(\"externalServiceId\")
print(v if v is not None else \"\")" 2>/dev/null)
  if [ -n "$EXTID" ]; then break; fi
  sleep 5
done
if [ -z "$EXTID" ]; then
  printf "STAGE=arr-not-populated msg=externalServiceId-null-after-30s-req-%s\n" "$REQ_ID" >&2
  [ "$CREATED" = "1" ] && curl -s -m 5 -X DELETE -H "X-Api-Key: $JS_KEY" "${JS}/api/v1/request/${REQ_ID}" >/dev/null 2>&1 || true
  exit 1
fi

[ "$EXTID" = "$RR_MID" ] || {
  printf "STAGE=verify-fail msg=extid-%s-radarr-mid-%s\n" "$EXTID" "$RR_MID" >&2
  [ "$CREATED" = "1" ] && curl -s -m 5 -X DELETE -H "X-Api-Key: $JS_KEY" "${JS}/api/v1/request/${REQ_ID}" >/dev/null 2>&1 || true
  exit 1
}

if [ "$CREATED" = "1" ]; then
  CH=$(curl -s -m 5 -X DELETE -H "X-Api-Key: $JS_KEY" "${JS}/api/v1/request/${REQ_ID}" -o /dev/null -w "%{http_code}")
  case "$CH" in
    200|204) : ;;
    *) printf "WARN: cleanup-fail msg=delete-req-%s-http-%s\n" "$REQ_ID" "$CH" >&2 ;;
  esac
fi

printf "PASS: movie canary — seerr→radarr push verified (tmdb=%s req=%s rrMid=%s created=%s)\n" \
  "$TMDB_ID" "$REQ_ID" "$RR_MID" "$CREATED"
')
echo "$RES"
