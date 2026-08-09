#!/usr/bin/env bash
# bazarr-ingest canary: can Bazarr still INGEST what its *arr holds?
#
# WHY THIS EXISTS
# ---------------
# On 2026-07-25 bazarr2's table_languages_profiles was emptied while its
# config.yaml kept pointing at profile 1. Every series and movie row Bazarr
# tried to write carried profileId=1, the FK to a now-empty profiles table
# failed, table_shows stayed at ZERO, and every episode insert then failed on
# its own FK to that empty table_shows. Anime + Anime Movies had no subtitle
# coverage at all for TWELVE DAYS, at 8485 logged FOREIGN KEY errors, and
# nothing went red: bazarr2's app monitor probes the web UI, which answered
# 200 the whole time. A subtitle daemon that is up and holding zero series is
# indistinguishable from a healthy one at the HTTP layer — that is the blind
# spot this canary closes.
#
# The alert that eventually surfaced it was REA flagging the FK line at 1/3
# model confidence, twelve days late and by luck. This canary is the
# deterministic replacement for that luck.
#
# PREDICATES (evaluated per instance, bazarr AND bazarr2)
#
#   1. DANGLING DEFAULT PROFILE -- the precise 2026-07-25 fault.
#      config.yaml's serie_default_profile / movie_default_profile names a
#      profileId that does not exist in table_languages_profiles. Deliberately
#      NOT written as "profiles table is empty": the dangling-reference form
#      also catches a profile RENUMBERED out from under the config (delete +
#      recreate in the UI assigns a fresh id), which an emptiness test reads as
#      perfectly healthy. Only checked when the matching *_default_enabled is
#      true — an operator who turns default assignment off is not broken.
#
#   2. NO ENABLED LANGUAGES -- bazarr2 lost table_settings_languages.enabled in
#      the same event. A profile can exist and still subtitle nothing if no
#      language is enabled, so this is a separate predicate rather than an
#      assumed consequence of #1.
#
#   3. INGEST STALLED -- the *arr holds >0 items and Bazarr holds ZERO. This is
#      the backstop for any cause we have not seen yet (auth drift, path
#      mapping, a future migration): it asserts the OUTCOME rather than a known
#      mechanism. Fires only on total stall, never on a count MISMATCH --
#      Bazarr legitimately tracks only movies that have files (radarr2 held 6
#      movies and bazarr2 correctly held the 3 with files, 2026-08-06), and a
#      count-equality test would flap on that by design.
#
# EMPTY IS NOT BROKEN. An *arr with zero series is a legitimate content state
# on this box — the reaper's add-date retention has emptied the anime library
# before — so predicate 3 is SKIPPED, counted and named when the *arr itself
# reports zero. Predicates 1 and 2 still run: a dangling profile is broken
# whether or not any content has arrived yet, and catching it on an empty
# library is precisely how this fires on day one instead of day twelve.
#
# EXIT CODES
#   0 — pass (PASS: line on stdout becomes Kuma msg=)
#   1 — a predicate fired; STAGE=/msg= on stderr
#   2 — could not assert: DB or config missing/unreadable, *arr unreachable.
#       An instance whose DB cannot be read is NOT a clean pass — that is the
#       same empty-because-broken-wearing-empty-because-clean's-clothes trap
#       plex-unmatched.sh guards against.
#
# READING THE DB. sqlite3 -readonly is used deliberately, NOT a cp of
# db+wal+shm: Bazarr writes continuously, so a three-file copy can tear, while
# a read-only SQLite connection is WAL-correct by construction. Verified
# 2026-08-06 on the live box — -readonly, file:?mode=ro and a WAL-copy all
# returned the identical profiles=1/shows=3 immediately after the repair, so
# the simple form genuinely sees WAL content rather than a stale main-file
# snapshot. (A canary blind to the WAL would report profiles=0 forever: a
# permanent false red.)
#
# THE TWO INSTANCES HAVE DIFFERENT LAYOUTS. bazarr-1 keeps its db at
# ~/.apps/bazarr/db/ and config at ~/.apps/bazarr/config/; bazarr2 nests both
# under data/ (~/.apps/bazarr2/data/db, ~/.apps/bazarr2/data/config). Both
# candidate paths are probed rather than hardcoded, so neither layout drifting
# turns into a silent skip.
#
# TEST OVERRIDES (resolved on the box, so they apply when the script runs there
# — under systemd, or copied over for a mutation test):
#   QFLIX_CANARY_BAZARR_DB    default ~/.apps/bazarr/db/bazarr.db
#   QFLIX_CANARY_BAZARR_CFG   default ~/.apps/bazarr/config/config.yaml
#   QFLIX_CANARY_BAZARR2_DB   default ~/.apps/bazarr2/data/db/bazarr.db
#   QFLIX_CANARY_BAZARR2_CFG  default ~/.apps/bazarr2/data/config/config.yaml
#
# Lives on the seedbox at ~/scripts/canaries/bazarr-ingest.sh (deployed by
# 240-maintenance-install.sh). Invoked by manitoba-maint-canary-bazarr-ingest,
# which pushes status=up/down to Kuma monitor "Canary Bazarr Ingest".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail

# Path overrides, resolved REMOTELY (same idiom as hardlink-integrity.sh) so
# the mutation test can point the canary at a deliberately-broken scratch copy
# and prove it fails, rather than trusting a script that has only ever been
# observed to pass. Defaults are the production layouts.
B1_DB=${QFLIX_CANARY_BAZARR_DB:-$HOME/.apps/bazarr/db/bazarr.db}
B1_CFG=${QFLIX_CANARY_BAZARR_CFG:-$HOME/.apps/bazarr/config/config.yaml}
B2_DB=${QFLIX_CANARY_BAZARR2_DB:-$HOME/.apps/bazarr2/data/db/bazarr.db}
B2_CFG=${QFLIX_CANARY_BAZARR2_CFG:-$HOME/.apps/bazarr2/data/config/config.yaml}

LOG="$HOME/.opt/maint/canary-bazarr-ingest.log"
logfail() {
  # Durable local reason trail. Kumas heartbeat table lives inside the Kuma
  # Docker container and is unreadable by this SSH user, so without this file a
  # past failure cannot be diagnosed after the fact.
  mkdir -p "$(dirname "$LOG")" 2>/dev/null
  printf "%s %s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG" 2>/dev/null
  if [ -f "$LOG" ] && [ "$(wc -l < "$LOG" 2>/dev/null)" -gt 300 ]; then
    tail -n 200 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null
  fi
}

# arr_count ENDPOINT PORT_SECRET KEY_SECRET URLBASE_SECRET FILTER
#   Prints the item count an *arr reports, or nothing on transport failure.
#   FILTER=hasfile restricts to items with a file on disk (radarr: Bazarr only
#   tracks movies it can actually subtitle). Retries: this is a shared seedbox
#   and a single neighbour I/O storm must not read as an outage.
arr_count() {
  local ep="$1" ps="$2" ks="$3" us="$4" filter="$5" port key urlbase body try
  port=$(cat "$HOME/secrets/$ps" 2>/dev/null)
  key=$(cat "$HOME/secrets/$ks" 2>/dev/null)
  # Default the urlbase to the secret NAME minus its suffix (sonarr2.urlbase ->
  # sonarr2), matching how anime.sh falls back, and strip any leading slash the
  # secret may carry. An empty urlbase would otherwise build a double-slash URL
  # that 404s, which arr_count would report as an unreachable *arr — a
  # config-shape problem wearing an outage costume.
  urlbase=$(cat "$HOME/secrets/$us" 2>/dev/null)
  [ -n "$urlbase" ] || urlbase="${us%.urlbase}"
  urlbase="${urlbase#/}"
  [ -n "$port" ] && [ -n "$key" ] || return 1
  for try in 1 2 3; do
    body=$(curl -sf -m 10 -H "X-Api-Key: $key" \
      "http://127.0.0.1:${port}/${urlbase}/api/v3/${ep}" 2>/dev/null)
    if [ $? -eq 0 ] && [ -n "$body" ]; then
      printf "%s" "$body" | python3 -c "
import sys, json
d = json.load(sys.stdin)
f = \"$filter\"
print(sum(1 for m in d if m.get(\"hasFile\")) if f == \"hasfile\" else len(d))
" 2>/dev/null && return 0
    fi
    sleep 3
  done
  return 1
}

FAILED=0
BROKEN=0
SUMMARY=""

# check_instance NAME DBPATH CFGPATH S_PORT S_KEY S_URLBASE M_PORT M_KEY M_URLBASE
check_instance() {
  local name="$1" db="$2" cfg="$3"
  local sp="$4" sk="$5" su="$6" mp="$7" mk="$8" mu="$9"

  if [ ! -r "$db" ]; then
    printf "STAGE=bazarr-db-unreadable msg=%s-db-not-readable-at-%s\n" "$name" "$db" >&2
    logfail "bazarr-db-unreadable $name $db"
    BROKEN=1; return
  fi
  if [ ! -r "$cfg" ]; then
    printf "STAGE=bazarr-config-unreadable msg=%s-config-not-readable-at-%s\n" "$name" "$cfg" >&2
    logfail "bazarr-config-unreadable $name $cfg"
    BROKEN=1; return
  fi

  # --- read the DB once; a query failure here is BROKEN, not clean ---
  local row profiles langs shows movies
  row=$(sqlite3 -readonly "$db" "
    select (select count(*) from table_languages_profiles),
           (select count(*) from table_settings_languages where enabled=1),
           (select count(*) from table_shows),
           (select count(*) from table_movies);" 2>/dev/null)
  if [ -z "$row" ]; then
    printf "STAGE=bazarr-db-query-fail msg=%s-counts-query-returned-nothing\n" "$name" >&2
    logfail "bazarr-db-query-fail $name"
    BROKEN=1; return
  fi
  profiles=$(printf "%s" "$row" | cut -d"|" -f1)
  langs=$(printf "%s" "$row" | cut -d"|" -f2)
  shows=$(printf "%s" "$row" | cut -d"|" -f3)
  movies=$(printf "%s" "$row" | cut -d"|" -f4)

  # --- predicate 1: dangling default profile ---
  # Read the ids config claims, then ask the DB whether they exist. The
  # comparison is what matters; an emptiness test would miss a renumber.
  local kind enabled want hit
  for kind in serie movie; do
    enabled=$(grep -E "^[[:space:]]*${kind}_default_enabled:" "$cfg" 2>/dev/null | head -1 | sed "s/.*:[[:space:]]*//" | tr -d "[:space:]")
    [ "$enabled" = "true" ] || continue
    want=$(grep -E "^[[:space:]]*${kind}_default_profile:" "$cfg" 2>/dev/null | head -1 | sed "s/.*:[[:space:]]*//" | tr -d "[:space:]\"")
    # An enabled default with no id at all is its own misconfiguration.
    if [ -z "$want" ]; then
      printf "STAGE=bazarr-profile-unset msg=%s-%s_default_enabled-true-but-no-profile-id\n" "$name" "$kind" >&2
      logfail "bazarr-profile-unset $name $kind"
      FAILED=1; continue
    fi
    hit=$(sqlite3 -readonly "$db" "select count(*) from table_languages_profiles where profileId=$want;" 2>/dev/null)
    if [ "${hit:-0}" -lt 1 ]; then
      printf "STAGE=bazarr-profile-dangling msg=%s-%s_default_profile-%s-absent-profiles-in-db-%s-every-insert-will-FK-fail\n" \
        "$name" "$kind" "$want" "$profiles" >&2
      logfail "bazarr-profile-dangling $name $kind want=$want profiles=$profiles"
      FAILED=1
    fi
  done

  # --- predicate 2: no enabled languages ---
  if [ "${langs:-0}" -lt 1 ]; then
    printf "STAGE=bazarr-no-languages msg=%s-zero-enabled-languages-nothing-can-be-subtitled\n" "$name" >&2
    logfail "bazarr-no-languages $name"
    FAILED=1
  fi

  # --- predicate 3: ingest stalled (outcome backstop) ---
  local aseries amovies sskip mskip
  sskip=""; mskip=""
  if aseries=$(arr_count "series" "$sp" "$sk" "$su" "all"); then
    if [ "${aseries:-0}" -gt 0 ] && [ "${shows:-0}" -lt 1 ]; then
      printf "STAGE=bazarr-ingest-stalled msg=%s-arr-has-%s-series-bazarr-holds-0\n" "$name" "$aseries" >&2
      logfail "bazarr-ingest-stalled $name series arr=$aseries bazarr=0"
      FAILED=1
    fi
    [ "${aseries:-0}" -eq 0 ] && sskip=" skip:series-arr-empty"
  else
    # Cannot reach the *arr => cannot assert this predicate. Named, not passed
    # over: an unreachable *arr making "nothing is missing" trivially true is
    # the exact shape prowlarr-app-sync.sh treats as BROKEN.
    sskip=" inconclusive:series-arr-unreachable"
    BROKEN=1
  fi
  if amovies=$(arr_count "movie" "$mp" "$mk" "$mu" "hasfile"); then
    if [ "${amovies:-0}" -gt 0 ] && [ "${movies:-0}" -lt 1 ]; then
      printf "STAGE=bazarr-ingest-stalled msg=%s-arr-has-%s-movies-with-files-bazarr-holds-0\n" "$name" "$amovies" >&2
      logfail "bazarr-ingest-stalled $name movies arr=$amovies bazarr=0"
      FAILED=1
    fi
    [ "${amovies:-0}" -eq 0 ] && mskip=" skip:movie-arr-empty"
  else
    mskip=" inconclusive:movie-arr-unreachable"
    BROKEN=1
  fi

  SUMMARY="$SUMMARY ${name}[profiles=${profiles} langs=${langs} shows=${shows}/${aseries:-?} movies=${movies}/${amovies:-?}]${sskip}${mskip}"
}

# bazarr-1: flat layout, main TV + movie stack.
check_instance "bazarr" "$B1_DB" "$B1_CFG" \
  sonarr.port sonarr.key sonarr.urlbase \
  radarr.port radarr.key radarr.urlbase

# bazarr2: data/-nested layout, anime stack (Sonarr2 + Radarr2).
check_instance "bazarr2" "$B2_DB" "$B2_CFG" \
  sonarr2.port sonarr2.key sonarr2.urlbase \
  radarr2.port radarr2.key radarr2.urlbase

# A fired predicate outranks an inconclusive leg: if we PROVED something is
# broken, report exit 1 even when some other probe could not be evaluated.
if [ "$FAILED" -eq 1 ]; then exit 1; fi
if [ "$BROKEN" -eq 1 ]; then
  printf "STAGE=bazarr-ingest-inconclusive msg=could-not-assert:%s\n" "$SUMMARY" >&2
  exit 2
fi
printf "PASS: bazarr ingest healthy —%s\n" "$SUMMARY"
exit 0
')
RC=$?
echo "$RES"
exit $RC
