#!/usr/bin/env bash
# prowlarr-proxy-link-fatal canary: assert Prowlarr never returns a FATAL
# (HTTP 500) to an *arr that asked it for releases.
#
# WHY THIS EXISTS
# ---------------
# 2026-09-06 22:18 through 2026-09-07 09:56, Prowlarr threw this eight times:
#
#   |Fatal|ProwlarrErrorPipeline|Request Failed. GET /27/api
#   [v2.5.2.5491] System.UriFormatException: Invalid URI: The Uri string is too long.
#      at NzbDrone.Core.Download.DownloadMappingService.ConvertToProxyLink(...)
#      at NzbDrone.Api.V1.Indexers.NewznabController.GetNewznabResponse(...)
#
# Indexer 27 is nekoBT. The fetch FROM nekoBT succeeded every time -- the debug
# log shows 100 reports parsed on the same millisecond -- and what failed was
# handing that response back to Sonarr. NewznabController pipes every torrent
# result through ConvertToProxyLink, which AES-encrypts the magnet and
# base64url-embeds it in a query string (roughly 1.4x expansion), and .NET
# refuses to construct a Uri past 65519 characters. One user-uploaded release
# with a bloated tracker list is therefore enough to 500 the ENTIRE
# hundred-result page, and there is no try/catch around the loop. Verified
# against Prowlarr source at the DEPLOYED tag v2.5.2.5491, not from memory:
# NewznabController.cs L193/L197 and DownloadMappingService.cs L28-L41.
#
# Member-visible effect: that *arr RSS sync silently returns nothing from the
# indexer for as long as the oversized release sits in its recent feed.
#
# preferMagnetUrl does NOT avoid it. That is worth writing down because it is
# the obvious-looking fix and it is wrong: both ConvertToProxyLink calls run
# unconditionally at L193/L197, and the flag is not read until L201, purely to
# choose which already-proxied URL goes into the XML.
#
# WHY NOTHING ELSE SEES IT
# ------------------------
#   * Prowlarr /api/v1/health was [] throughout and /api/v1/indexerstatus was []
#     -- the INDEXER is not failing, so Prowlarr never marks it down. That is
#     the numerator prowlarr-indexer-health.sh reads.
#   * No 429 is emitted, so that canary's cascade probe counts zero, correctly.
#   * prowlarr-app-sync.sh reads CONFIG state (sync membership, preferMagnetUrl).
#     This is a runtime response fault, not config drift.
#   * The *arr side sees a 500 and logs an indexer-unavailable backoff, which
#     REA deliberately suppresses (arr-indexer-unavailable-backoff), because a
#     relayed downstream symptom is not where a fault should be read.
# The only thing that caught it was REA reading the raw Prowlarr log two days
# later. A deterministic, greppable, local-file predicate must not have a
# log-reading LLM as its sole owner -- that is what a canary is for.
#
# BASELINE, measured 2026-09-10 over every retained rotation (2026-08-20 ->
# 2026-09-11, about 22 days): EIGHT "|Fatal|" lines in total, all eight this
# episode, all ProwlarrErrorPipeline, and no other logger has ever logged at
# Fatal. That is why the predicate is the bare level token rather than the
# UriFormatException text: the general shape has a clean 22-day zero baseline,
# so widening costs no noise and catches the next 500-to-an-arr whatever its
# cause. THRESHOLD is 1 for the same reason.
#
# WINDOW IS 8h, NOT one timer period. The episode fired about once per 87
# minutes. A window sized to the 30-minute cadence would have gone red, green,
# red, green and paged on every red edge -- replayed against the eight real
# timestamps, a 1h window produces THREE red edges, i.e. three Discord pages for
# one fault. A long window stays CONTINUOUSLY red for the life of the episode
# and self-clears once past the last fatal: one page per episode, no flap, no
# cross-run dedup ledger required. Same lesson as the 2026-09-03 alerting
# rebuild: retrying is never the bug, RE-PAGING is.
#
# WHY 8 AND NOT 6. The first draft said 6h and a replay showed one page -- but
# the largest real inter-fatal gap in the episode is 6h12m14s (03:43:52 ->
# 09:56:06), which EXCEEDS 6h. It produced one page only because that 12-minute
# overhang happened to fall between the :00/:30 timer anchors where no legal
# 0-120s jitter can land. That is grid-alignment luck, not margin, and a
# differently-phased gap would have split one episode into two pages. 8h clears
# the largest observed gap by ~1h45m. The cost is that recovery is confirmed
# ~2h later, which is the right trade for an indexer-feed fault nobody acts on
# in minutes.
#
# BOTH ROTATIONS ARE READ. prowlarr.txt rotates on size; measured spans
# (2026-09-01 17:01 -> 09-05 23:01, 09-05 23:01 -> 09-10 09:01) are ~102-106h,
# so a straddle is uncommon -- an earlier draft of this header said ~17h,
# extrapolated from a partial live file, and that number was simply wrong.
# Reading both files anyway is free and removes the failure mode entirely:
# reading only the live file would silently shorten the window to however long
# ago the last rotation was, the exact shape of a guard that looks like it
# works. prowlarr.0.txt is the immediately-previous rotation.
#
# TIMESTAMPS ARE BOX-LOCAL, NOT UTC. Verified 2026-09-11: the newest prowlarr
# line read 02:22:53 while `date` on the box read 02:24:42 CEST and `date -u`
# read 00:24:42. An earlier draft compared them as zero-padded STRINGS, which
# is correct under every locale tested and still WRONG ACROSS A DST FALL-BACK,
# where the local clock repeats an hour and the two passes are identical text:
# reproduced against the box's own Europe/Amsterdam tzdata, a fatal genuinely
# 5h05m old was EXCLUDED from a 6h window because the cutoff string resolved to
# the CEST pass while the event belonged to the CET pass. Every candidate is now
# resolved to an epoch with `date -d` -- affordable because the 22-day baseline
# is eight Fatal lines in total.
#
# The ambiguous hour itself is irreducible (the log carries no UTC offset), but
# the residual is now FAIL-SAFE, which is the part that matters. Measured on the
# box 2026-09-12: glibc resolves an ambiguous local time to the SECOND (standard-
# time) pass, so a parsed epoch is always >= the true instant, an event is never
# read as OLDER than it is, and the error can only ever over-include -- a page,
# never a silence. Both sides of the comparison go through the same parse.
#
# KNOWN, ACCEPTED, NOT THIS CANARY'S TO FIX: an episode still DOWN when the
# Monday maintenance window opens gets a false "[maint-window]" UP push from
# lib/cli.py and reds again when real checks resume -- a second page for one
# uninterrupted episode. That suppression is shared by every canary here, and
# the window is hard-capped at 4h by the stale-lock watchdog, comfortably inside
# this lookback, so detection is delayed and never lost.
#
# EXECUTION MODEL -- runs LOCALLY on the box, no sshm hop. Every input is a file
# on the box's own filesystem plus one loopback API call, and the timer's
# ExecStart already runs there. Not hopping is what makes the hermetic tests in
# tests/unit/test_prowlarr_proxy_link_fatal_canary.py able to drive the REAL
# artifact against fixture directories: env overrides do not survive an ssh hop.
# Same pattern as rea-liveness.sh, kometa-deploy-drift.sh and
# newsletter-digest-stale.sh.
#
# --- STAGE labels (stderr on failure -> Kuma msg) --------------------------
#   prowlarr-fatal-500          >=THRESHOLD Fatal lines inside the window. The
#                               msg names the count and, ONLY when the failing
#                               request was a newznab call, the indexer ids and
#                               names; a Fatal with no "GET /<id>/api" says so
#                               instead of misattributing it to the *arr path.
#   prowlarr-canary-bad-config  a numeric override is not a positive integer
#                               (exit 2) -- see Probe -1
#   prowlarr-log-missing        prowlarr.txt is not there (exit 2)
#   prowlarr-log-unreadable     prowlarr.txt, or a prowlarr.0.txt that EXISTS,
#                               cannot be read (exit 2)
#   prowlarr-request-path-silent the log is fresh but nothing on the
#                               request-handling path has logged, so a zero
#                               fatal count carries no information (exit 2)
#   prowlarr-log-stale          newest request-path line older than the
#                               staleness budget (exit 2)
#   prowlarr-log-unparseable    no timestamped line at all, or a bad clock
#                               override (exit 2)
# PASS stdout:
#   "PASS: prowlarr-proxy-link-fatal - 0 fatal in 8h (threshold=1 lines=N age=Mmin)"
#
# Exit contract: 0 = pass / UP; 1 = fail / DOWN; 2 = cannot assert / DOWN with a
# cannot-assert STAGE. The 1-vs-2 split is a MESSAGE distinction -- lib/cli.py
# pushes DOWN for any non-zero -- and it exists so triage can tell "Prowlarr is
# 500ing the *arrs" apart from "this canary could not read its own input".
# A zero count is never printed as health when the input cannot support it.
#
# --- Test/override env vars (hermetic acceptance tests) --------------------
#   PROWLARR_FATAL_LOG_DIR           dir holding prowlarr.txt + prowlarr.0.txt
#                                    (default ~/.apps/prowlarr/logs)
#   PROWLARR_FATAL_WINDOW_H          lookback hours, default 8
#   PROWLARR_FATAL_THRESHOLD         fatals in window that trip it, default 1
#   PROWLARR_FATAL_STALE_BUDGET_MIN  staleness budget, default 45 (the measured
#                                    max inter-line gap over a full rotation is
#                                    11.8 min, so this is ~4x the worst quiet)
#   All three are validated as POSITIVE INTEGERS before use (Probe -1). A bad
#   value is exit 2, never a silently-disabled probe.
#   PROWLARR_FATAL_TRAIL             durable log path
#   PROWLARR_FATAL_NOW               "YYYY-MM-DD HH:MM:SS" override for now, so
#                                    the tests can freeze the window on a fixture
#   PROWLARR_FATAL_SKIP_LOOKUP       1 = skip the Prowlarr API name lookup
#
# Every non-pass also appends its reason to
#   ~/.opt/maint/canary-prowlarr-proxy-link-fatal.log
# because Kuma heartbeats live in a Docker volume the SSH user cannot read, so
# triage needs a host-readable trail too.
set -uo pipefail

SECRETS="${PROWLARR_FATAL_SECRETS:-$HOME/secrets}"
LOGDIR="${PROWLARR_FATAL_LOG_DIR:-$HOME/.apps/prowlarr/logs}"
WINDOW_H="${PROWLARR_FATAL_WINDOW_H:-8}"
THRESHOLD="${PROWLARR_FATAL_THRESHOLD:-1}"
STALE_MIN="${PROWLARR_FATAL_STALE_BUDGET_MIN:-45}"
TRAIL="${PROWLARR_FATAL_TRAIL:-$HOME/.opt/maint/canary-prowlarr-proxy-link-fatal.log}"
SKIP_LOOKUP="${PROWLARR_FATAL_SKIP_LOOKUP:-0}"

LIVE="$LOGDIR/prowlarr.txt"
PREV="$LOGDIR/prowlarr.0.txt"
TS_RX='^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}'
# The loggers that only ever run because Prowlarr is SERVING REQUESTS. Freshness
# is asserted against these, not against any line at all -- see Probe 0.
ACTIVITY_RX='\|(ReleaseSearchService|NewznabController|Cardigann|IndexerStatusService|ApplicationIndexerSync)\|'

note() {
  mkdir -p "$(dirname "$TRAIL")" 2>/dev/null
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$TRAIL" 2>/dev/null
}

# --- Probe -1: the knobs themselves ----------------------------------------
# Every numeric override is validated BEFORE it is used, because bash's `[` and
# `$(( ))` fail in the most dangerous possible direction. `[ "$AGE" -gt abc ]`
# prints "integer expected" to stderr and evaluates FALSE, which reads as
# "not stale" -- so one typo in a systemd Environment= line silently disables
# the staleness guard and the canary is green forever. A negative WINDOW_H puts
# the cutoff in the FUTURE and excludes every real fatal, for the same silent
# green. Both were reproduced against this script before this block existed.
# Guard the STATE, never the exit status.
for _knob in WINDOW_H:PROWLARR_FATAL_WINDOW_H THRESHOLD:PROWLARR_FATAL_THRESHOLD \
             STALE_MIN:PROWLARR_FATAL_STALE_BUDGET_MIN; do
  _name="${_knob%%:*}"
  _env="${_knob##*:}"
  _val="${!_name}"
  case "x$_val" in
    x|x*[!0-9]*)
      printf 'STAGE=prowlarr-canary-bad-config msg=%s=%s-must-be-a-positive-integer-cannot-assert\n' \
        "$_env" "${_val:-EMPTY}" >&2
      note "bad-config $_env=${_val:-EMPTY}"
      exit 2 ;;
  esac
  if [ "$_val" -le 0 ]; then
    printf 'STAGE=prowlarr-canary-bad-config msg=%s=%s-must-be-greater-than-zero-cannot-assert\n' \
      "$_env" "$_val" >&2
    note "bad-config $_env=$_val"
    exit 2
  fi
done

if [ ! -f "$LIVE" ]; then
  printf 'STAGE=prowlarr-log-missing msg=no-file-at-%s-cannot-assert\n' "$LIVE" >&2
  note "prowlarr-log-missing $LIVE"
  exit 2
fi
if [ ! -r "$LIVE" ]; then
  printf 'STAGE=prowlarr-log-unreadable msg=cannot-read-%s-cannot-assert\n' "$LIVE" >&2
  note "prowlarr-log-unreadable $LIVE"
  exit 2
fi
# ABSENT and UNREADABLE are the same `cat 2>/dev/null` answer and want OPPOSITE
# verdicts. A fresh Prowlarr legitimately has no prowlarr.0.txt, so absence is
# fine; a prowlarr.0.txt that EXISTS but cannot be read means half the window is
# unobservable, and swallowing that error renders a real fatal as a clean zero.
# Reproduced: replacing prowlarr.0.txt with an unreadable object made a genuine
# 2h-old fatal print PASS.
if [ -e "$PREV" ] && { [ ! -f "$PREV" ] || [ ! -r "$PREV" ]; }; then
  printf 'STAGE=prowlarr-log-unreadable msg=previous-rotation-%s-exists-but-is-not-a-readable-file-cannot-assert\n' \
    "$PREV" >&2
  note "prowlarr-prev-unreadable $PREV"
  exit 2
fi

# The clock is the only mocked input; everything else is real bash, real grep,
# real date, real files.
NOW_OVERRIDE="${PROWLARR_FATAL_NOW:-}"
if [ -n "$NOW_OVERRIDE" ]; then
  NOW_EPOCH=$(date -d "$NOW_OVERRIDE" +%s 2>/dev/null || echo 0)
else
  NOW_EPOCH=$(date +%s)
fi
if [ -z "$NOW_EPOCH" ] || [ "$NOW_EPOCH" -le 0 ] 2>/dev/null; then
  printf 'STAGE=prowlarr-log-unparseable msg=bad-now-override=%s\n' "${NOW_OVERRIDE:-EMPTY}" >&2
  note "bad-now-override ${NOW_OVERRIDE:-EMPTY}"
  exit 2
fi
# No local-time cutoff STRING is computed on purpose. An earlier draft built one
# and compared timestamps as text; that is DST-unsafe (see Probe 1) and a dead
# string here would be a comment claiming a prefilter that does not exist.
# The cutoff is an epoch, derived at the point of use.

# --- Probe 0: is the REQUEST PATH saying anything? -------------------------
# A zero fatal count read out of a log nothing is writing to is not evidence of
# health, it is an absence of evidence -- the same blindness assertion
# prowlarr-indexer-health.sh makes about vlogs ingest lag.
#
# But "the file is fresh" is the WEAK version of that question and it was the
# first draft here. Prowlarr has background loggers (Housekeeper and friends)
# that tick on their own timer, so a log can look perfectly fresh while the
# request-handling path -- the only thing that can ever emit this fatal -- has
# been dead for hours. Reproduced: a fixture of nothing but Housekeeper ticks
# printed PASS with zero evidence that the pipeline under test was running.
# So freshness is asserted against ACTIVITY_RX, the loggers that exist only
# because Prowlarr is serving or syncing requests.
NEWEST=$(grep -E "$ACTIVITY_RX" "$LIVE" | grep -oE "$TS_RX" | tail -1)
if [ -z "$NEWEST" ]; then
  # Distinguish "no request-path line" from "not a Prowlarr log at all", because
  # they want different triage even though both are exit 2.
  if grep -qoE "$TS_RX" "$LIVE"; then
    printf 'STAGE=prowlarr-request-path-silent msg=no-request-path-line-in-%s-only-background-chatter-a-zero-fatal-count-would-be-meaningless\n' \
      "$LIVE" >&2
    note "prowlarr-request-path-silent $LIVE"
  else
    printf 'STAGE=prowlarr-log-unparseable msg=no-timestamped-line-in-%s-cannot-assert\n' "$LIVE" >&2
    note "prowlarr-log-unparseable $LIVE"
  fi
  exit 2
fi
NEWEST_EPOCH=$(date -d "$NEWEST" +%s 2>/dev/null || echo 0)
if [ -z "$NEWEST_EPOCH" ] || [ "$NEWEST_EPOCH" -le 0 ] 2>/dev/null; then
  printf 'STAGE=prowlarr-log-unparseable msg=newest-line-timestamp-unparseable=%s\n' "$NEWEST" >&2
  note "newest-line-unparseable $NEWEST"
  exit 2
fi
AGE_MIN=$(( ( NOW_EPOCH - NEWEST_EPOCH ) / 60 ))
[ "$AGE_MIN" -lt 0 ] && AGE_MIN=0
if [ "$AGE_MIN" -gt "$STALE_MIN" ]; then
  printf 'STAGE=prowlarr-log-stale msg=newest-request-path-line-%smin-old-budget=%smin-a-zero-fatal-count-would-be-meaningless\n' \
    "$AGE_MIN" "$STALE_MIN" >&2
  note "prowlarr-log-stale age=${AGE_MIN}m budget=${STALE_MIN}m"
  exit 2
fi

# --- Probe 1: Fatal lines inside the window --------------------------------
# EPOCH compare, not the string compare the first draft used. A zero-padded
# "YYYY-MM-DD HH:MM:SS" does sort chronologically, and that held under every
# locale tested -- but it is WRONG ACROSS A DST FALL-BACK, because the local
# clock repeats an hour and the two passes are indistinguishable as text.
# Reproduced against the box's own Europe/Amsterdam tzdata: a fatal genuinely
# 5h05m old was EXCLUDED from a 6h window because the cutoff string resolved to
# the CEST pass of the repeated hour while the event belonged to the CET pass.
# One hour of blindness a year is still a silent green, so the string compare is
# kept only as a cheap PREFILTER and every surviving candidate is resolved to an
# epoch. The cost is one `date -d` per Fatal line, and the 22-day baseline is
# eight Fatal lines total.
#
# Both rotations are concatenated: prowlarr.txt rotates on size (measured spans
# 2026-09-01..09-10 are ~102-106h, not the ~17h an earlier draft of this header
# claimed from a partial file), so a straddle is rare but reading both is free.
CUTOFF_EPOCH=$(( NOW_EPOCH - WINDOW_H * 3600 ))
IDS=""
COUNT=0
NEWZNAB=0
while IFS= read -r ln; do
  [ -n "$ln" ] || continue
  ts="${ln:0:19}"
  case "x$ts" in
    x[0-9][0-9][0-9][0-9]-[0-9][0-9]-*) : ;;
    *) continue ;;
  esac
  ts_epoch=$(date -d "$ts" +%s 2>/dev/null || echo "")
  case "x$ts_epoch" in
    x|x*[!0-9]*)
      # An unparseable timestamp on a line we already know is Fatal must not be
      # silently dropped -- count it and let the operator see it.
      COUNT=$(( COUNT + 1 ))
      continue ;;
  esac
  if [ "$ts_epoch" -gt "$CUTOFF_EPOCH" ]; then
    COUNT=$(( COUNT + 1 ))
    id=$(printf '%s' "$ln" | sed -nE 's|.*GET /([0-9]+)/.*|\1|p')
    if [ -n "$id" ]; then IDS="$IDS $id"; NEWZNAB=$(( NEWZNAB + 1 )); fi
  fi
done < <( { cat "$PREV" 2>/dev/null; cat "$LIVE"; } | grep -F '|Fatal|' )

LINES=$(wc -l < "$LIVE" | tr -d ' ')
if [ "$COUNT" -lt "$THRESHOLD" ]; then
  printf 'PASS: prowlarr-proxy-link-fatal - %d fatal in %sh (threshold=%s lines=%s age=%smin)\n' \
    "$COUNT" "$WINDOW_H" "$THRESHOLD" "$LINES" "$AGE_MIN"
  exit 0
fi

# --- Name the indexers, best effort ----------------------------------------
# A bare id is actionable but a name is what the operator recognises. This is
# the ONLY network call in the canary and it is deliberately not allowed to
# change the verdict: a lookup failure degrades the message, never the result.
UNIQ_IDS=$(printf '%s' "$IDS" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u | tr '\n' ' ')
NAMED=""
if [ "$SKIP_LOOKUP" != "1" ] && [ -n "$UNIQ_IDS" ]; then
  PROW_PORT=$(cat "$SECRETS/prowlarr.port" 2>/dev/null)
  PROW_URLBASE=$(cat "$SECRETS/prowlarr.urlbase" 2>/dev/null || echo prowlarr)
  PROW_KEY=$(cat "$SECRETS/prowlarr.key" 2>/dev/null)
  if [ -n "$PROW_PORT" ] && [ -n "$PROW_KEY" ]; then
    # The LIST endpoint, PARSED as JSON rather than regexed. IndexerResource
    # embeds a `fields` array whose every entry also carries a "name" key, so
    # any sed/grep for "name" over one indexer object picks an arbitrary field
    # LABEL (baseUrl, apiKey, ...) instead of the indexer. Parsing is the only
    # way to know which "name" is the indexer's.
    NAMED=$(curl -sf -m 8 -H "X-Api-Key: $PROW_KEY" \
      "http://127.0.0.1:$PROW_PORT/$PROW_URLBASE/api/v1/indexer" 2>/dev/null |
      python3 -c '
import sys, json, re
wanted = [w for w in sys.argv[1:] if w.isdigit()]
try:
    by_id = {str(i.get("id")): str(i.get("name", "")) for i in json.load(sys.stdin)}
except Exception:
    by_id = {}
out = []
for w in wanted:
    nm = re.sub(r"[^A-Za-z0-9._-]", "", by_id.get(w, ""))
    out.append(w + ":" + (nm if nm else "unknown"))
print(" " + " ".join(out))
' $UNIQ_IDS 2>/dev/null)
  fi
fi
[ -z "$NAMED" ] && NAMED=" $UNIQ_IDS"
# Trailing space is stripped BEFORE the space->comma pass: UNIQ_IDS comes off a
# `tr \n ' '` and ends in one, which otherwise renders as "27," in the page.
WHO=$(printf '%s' "$NAMED" | sed -E 's/^ +//; s/ +$//; s/ +/,/g')
[ -z "$WHO" ] && WHO="no-indexer-id-in-line"

# ProwlarrErrorPipeline.cs:82 is a catch-all for ANY unhandled API exception,
# and WindowsApp/ConsoleApp/UpdateApp log Fatal on a process-level crash. A
# Fatal is always worth paging, but asserting "the *arrs got a 500 instead of
# releases" is only TRUE when the failing request was a newznab/torznab call --
# i.e. when the line carried a "GET /<indexerId>/api". Claiming it either way
# would misattribute cause during triage, which is how an operator spends the
# first ten minutes of an incident in the wrong subsystem.
if [ "$NEWZNAB" -gt 0 ]; then
  printf 'STAGE=prowlarr-fatal-500 msg=%d-fatal-responses-in-%sh-indexers=%s-arrs-got-500-not-releases\n' \
    "$COUNT" "$WINDOW_H" "$WHO" >&2
  note "prowlarr-fatal-500 count=$COUNT window=${WINDOW_H}h indexers=$WHO"
else
  printf 'STAGE=prowlarr-fatal-500 msg=%d-fatal-responses-in-%sh-none-on-a-newznab-path-check-prowlarr-log-for-crash-or-ui-500\n' \
    "$COUNT" "$WINDOW_H" >&2
  note "prowlarr-fatal-500 count=$COUNT window=${WINDOW_H}h non-newznab"
fi
exit 1
