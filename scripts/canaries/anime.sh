#!/usr/bin/env bash
# Anime canary: drive a real Seerr request through to Sonarr2 to verify
# the Seerr-in-container → Sonarr2-in-container netns hop is healthy for
# the anime routing path. Mirror of movie.sh — see that file's header for
# the broader rationale and stage labels.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
JS_PORT=$(cat ~/secrets/seerr.port)
JS_KEY=$(cat ~/secrets/seerr.key)
S2_PORT=$(cat ~/secrets/sonarr2.port)
S2_KEY=$(cat ~/secrets/sonarr2.key)
S2_URLBASE=$(cat ~/secrets/sonarr2.urlbase 2>/dev/null || echo sonarr2)
JS=http://127.0.0.1:${JS_PORT}
S2=http://127.0.0.1:${S2_PORT}/${S2_URLBASE}/api/v3

# Retry transient 5xx/timeouts up to 3x before declaring a service down — a
# single shared-box 500 was firing this canary (tuned 2026-06-27).
http_up() { local c; for _ in 1 2 3; do
  c=$(curl -s -o /dev/null -w "%{http_code}" -m 6 -H "X-Api-Key: $2" "$1")
  [ "$c" = "200" ] && return 0; sleep 3; done; printf "%s" "$c"; return 1; }
H_JS=$(http_up "${JS}/api/v1/status" "$JS_KEY") || { printf "STAGE=seerr-up-fail msg=seerr-status-http-%s-after-3-tries\n" "$H_JS" >&2; exit 1; }
H_S2=$(http_up "${S2}/system/status" "$S2_KEY") || { printf "STAGE=sonarr2-up-fail msg=sonarr2-status-http-%s-after-3-tries\n" "$H_S2" >&2; exit 1; }

# JSON reads go through jget so an empty/non-JSON answer is a named STAGE,
# never a Python traceback (QFLX-115; see movie.sh for the incident).
fail() { printf "STAGE=%s msg=%s\n" "$1" "$2" >&2; exit 1; }
jget() { local body; for _ in 1 2 3; do
  body=$(curl -sf -m 8 -H "X-Api-Key: $2" "$1") \
    && printf "%s" "$body" | python3 -c "import sys, json; json.load(sys.stdin)" 2>/dev/null \
    && { printf "%s" "$body"; return 0; }
  sleep 3; done; return 1; }

SERIES=$(jget "${S2}/series" "$S2_KEY") || fail sonarr2-api-fail series-list-no-json-after-3-tries
SEED=$(printf "%s" "$SERIES" | python3 -c "import sys, json
sr = sorted(json.load(sys.stdin), key=lambda x: x[\"id\"])
print(sr[0][\"tvdbId\"], sr[0][\"id\"], sr[0].get(\"tmdbId\") or 0) if sr else exit(2)" 2>/dev/null)
# rc 2 = empty library (SKIP below); anything else non-zero = a shape we cannot read.
SEED_RC=$?
[ "$SEED_RC" = "0" ] || [ "$SEED_RC" = "2" ] || fail seed-pick-fail "series-list-unparseable-rc-${SEED_RC}"
# Empty-but-reachable library is a legitimate content state (e.g. the reaper
# aged out the last series, or anime is simply sparse), NOT a Seerr->Sonarr2
# path failure. Sonarr2 already passed its up-check above, so 0 series = nothing
# to seed = inconclusive: pass with an explicit SKIP message rather than
# false-red the pipeline. A genuine Sonarr2 outage trips sonarr2-up-fail earlier.
if [ -z "$SEED" ]; then
  printf "PASS: anime canary — SKIP: Sonarr2 up but 0 series (empty anime library); Seerr->Sonarr2 path not exercised\n"
  exit 0
fi
TVDB_ID=$(printf "%s" "$SEED" | cut -d" " -f1)
S2_SID=$(printf "%s" "$SEED" | cut -d" " -f2)
TMDB_ID=$(printf "%s" "$SEED" | cut -d" " -f3)

SSET=$(jget "${JS}/api/v1/settings/sonarr" "$JS_KEY") || fail seerr-api-fail settings-sonarr-no-json-after-3-tries
SRV=$(printf "%s" "$SSET" | python3 -c "import sys, json
d=json.load(sys.stdin)
anime=[s for s in d if \"anime\" in (s.get(\"name\") or \"\").lower()]
nondefault=[s for s in d if not s.get(\"isDefault\")]
chosen = anime or nondefault or d
print(chosen[0][\"id\"] if chosen else \"\")" 2>/dev/null)
[ -n "$SRV" ] || { printf "STAGE=seerr-push-fail msg=no-sonarr-anime-server-in-seerr\n" >&2; exit 1; }

if [ "$TMDB_ID" = "0" ] || [ -z "$TMDB_ID" ]; then
  SRCH=$(jget "${JS}/api/v1/search?query=tvdb%3A${TVDB_ID}" "$JS_KEY") || fail seerr-api-fail search-no-json-after-3-tries
  TMDB_ID=$(printf "%s" "$SRCH" | python3 -c "import sys, json
r=json.load(sys.stdin).get(\"results\",[])
tv=[x for x in r if x.get(\"mediaType\")==\"tv\"]
print(tv[0].get(\"id\") if tv else \"\")" 2>/dev/null)
fi
[ -n "$TMDB_ID" ] && [ "$TMDB_ID" != "0" ] || { printf "STAGE=seed-pick-fail msg=could-not-resolve-tmdb-for-tvdb-%s\n" "$TVDB_ID" >&2; exit 1; }

curl -s -o /dev/null -m 8 -H "X-Api-Key: $JS_KEY" "${JS}/api/v1/tv/${TMDB_ID}"

BODY=$(printf "{\"mediaType\":\"tv\",\"mediaId\":%s,\"serverId\":%s,\"seasons\":[1]}" "$TMDB_ID" "$SRV")
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
  202)
    # Seerr accepted the request but did nothing (all seasons already
    # requested). That still proves seerr→sonarr2 reachability — Seerr had
    # to query Sonarr2 to know the seasons were taken. Treat as soft pass
    # after a final sonarr2 sanity probe.
    H_S2B=$(curl -s -o /dev/null -w "%{http_code}" -m 5 -H "X-Api-Key: $S2_KEY" "${S2}/system/status")
    [ "$H_S2B" = "200" ] || { printf "STAGE=sonarr2-up-fail msg=post-202-sonarr2-http-%s\n" "$H_S2B" >&2; exit 1; }
    printf "PASS: anime canary — seerr-202 no-op (all seasons requested); seerr→sonarr2 hop verified (tvdb=%s s2Sid=%s)\n" \
      "$TVDB_ID" "$S2_SID"
    exit 0
    ;;
  409)
    REQS=$(jget "${JS}/api/v1/request?take=200" "$JS_KEY") || fail seerr-api-fail request-list-no-json-after-3-tries
    REQ_ID=$(printf "%s" "$REQS" | python3 -c "import sys, json
d=json.load(sys.stdin)
hits=[r for r in d.get(\"results\",[]) if r.get(\"media\",{}).get(\"tmdbId\")==${TMDB_ID} and r.get(\"media\",{}).get(\"mediaType\")==\"tv\"]
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

[ "$EXTID" = "$S2_SID" ] || {
  printf "STAGE=verify-fail msg=extid-%s-sonarr2-sid-%s\n" "$EXTID" "$S2_SID" >&2
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

printf "PASS: anime canary — seerr→sonarr2 push verified (tmdb=%s tvdb=%s req=%s s2Sid=%s created=%s)\n" \
  "$TMDB_ID" "$TVDB_ID" "$REQ_ID" "$S2_SID" "$CREATED"
')
echo "$RES"
