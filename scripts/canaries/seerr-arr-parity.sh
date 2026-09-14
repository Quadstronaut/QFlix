#!/usr/bin/env bash
# seerr-arr-parity canary: does every SETTLED Seerr request end in a state a
# member can actually act on? Spec section 4 (S-1..S-3),
# docs/superpowers/specs/2026-09-12-reaper-d-per-file-retention-spec.md.
#
# ============================================================================
# WHY THIS EXISTS
# ============================================================================
# `reconcile_seerr()` was built precisely to stop reaped titles being stuck
# un-re-requestable — its own comment says so — and as of commit eefcffd (an
# ANCESTOR of this canary) it has been fixed to page ALL statuses, not just
# `filter=available`, and to DELETE-cascade any status-7 (DELETED) season for
# a series still present in Sonarr via `_seerr_stuck_seasons()`. The 66
# status-7 / 763 season_request / Law-&-Order-class numbers measured live
# 2026-09-12 predate that fix and are NOT re-verified against the current
# reaper — see HONEST LIMITS below. Update the baseline (or confirm it has
# shrunk) on first live deploy after this lands.
#
# So this canary does NOT exist to catch a known-broken reconcile_seerr —
# that specific bug is fixed. It exists to assert the RESIDUAL: (a)
# reconciliation stops running (a timer disabled, a script erroring silently,
# an upstream API shape change reconcile_seerr's own tests don't cover), or
# (b) a strand shape reconcile_seerr's fix does not reach (e.g. a season
# stuck via a path other than the one `_seerr_stuck_seasons()` sweeps). Either
# way, correctness here is CONTINUOUS, not a side effect of a reap — "it gets
# its own canary, own timer, own Kuma check" (operator design law). OPERATOR
# RULING: this canary is REPORT ONLY. It never writes to Seerr or any *arr —
# a mutating "fix" here would be exactly the kind of unaudited repair rule
# 143 in the CLAUDE.md runbook forbids touching without review.
#
# ============================================================================
# THE PREDICATE
# ============================================================================
# For every Seerr media row (GET /api/v1/media, take=100/skip=N paged until a
# short page) whose status is SETTLED — i.e. NOT 2 PENDING, 3 PROCESSING, or
# 6 BLOCKLISTED, all three of which are legitimately mid-flight and saying
# nothing yet about requestability:
#
#   (a) TV rows. GET /api/v1/tv/<tmdbId> for mediaInfo.seasons[{seasonNumber,
#       status}] (the operator's explicit instruction — season status does
#       NOT ride along on the /api/v1/media list rows, only the per-title
#       detail call carries it). Cross-reference against Sonarr + Sonarr2:
#       GET /series, matched by tvdbId, using seasons[].statistics.
#       episodeFileCount.
#         - Seerr season status 7 (DELETED) or 5 (AVAILABLE) while Sonarr
#           reports that season's episodeFileCount==0            -> STRANDED
#           (the member sees "available" or nothing while there is nothing
#           to watch and no way to re-request it — exactly the Law & Order
#           class measured above).
#         - Seerr season status 1 or ABSENT from the season list while
#           Sonarr reports episodeFileCount>0 for that season number
#                                                              -> UNDERREPORTED
#           (the member has no idea the season exists — lower severity: it
#           costs nobody a broken re-request, it only hides content that is
#           already sitting on disk).
#
#   (b) Any settled row (movie OR tv) whose identifying id — tmdbId for a
#       movie, tvdbId for a tv show — is absent from the *arr side entirely
#                                                                   -> ORPHAN
#       (the reaper's reconcile should have cleared this row; if it
#       persists, reconciliation is not running for that row's status).
#
# ASSUMPTION (named, not hidden): "absent from ALL four *arrs" is implemented
# as type-appropriate matching — a movie's tmdbId is checked against the
# UNION of radarr+radarr2 movie tmdbIds, a tv show's tvdbId against the UNION
# of sonarr+sonarr2 series tvdbIds — rather than literally testing a movie's
# tmdbId against Sonarr's tvdbId space, which is a category error with no
# meaningful answer. This is strictly equivalent to "absent from every *arr
# that could possibly hold it" for this fleet's fixed movie/tv split.
#
# TVDBID COLLISION = UNTRUSTED, NEVER "keep the first one". If sonarr and
# sonarr2 both report a series under the same tvdbId (2026-09-13 council
# finding: this silently kept the FIRST-seen instance's — possibly stale,
# zero-file — season map and ran the full STRANDED predicate against it,
# which can page a false STRANDED for a season that unambiguously has files
# in the OTHER instance), the tvdbId is marked UNTRUSTED and excluded from
# EVERY per-row assertion for that id — no orphan, no stranded, no
# underreported, ever, for as long as the collision persists. This is the
# same "cannot know, so withhold rather than guess" contract as
# season-unknown-to-sonarr, just at the whole-series level. Counted as
# `tvdbid-collision-across-sonarr-instances` (at parse time, once per
# duplicate series seen) and `tvdbid-untrusted-row-excluded` (at predicate
# time, once per settled Seerr row that resolves to an untrusted id).
#
# ============================================================================
# NOISE CONTROL (same two mechanisms as arr-plex-parity.sh, same reasoning:
# the operator directive that false positives render the alert channel
# unusable applies here word for word)
# ============================================================================
#   1. 26h GRACE on the Seerr row's own `updatedAt`. A row that changed status
#      minutes ago may still be mid-reconcile; a scan/import cycle plus the
#      reaper's own daily cadence needs the better part of a day to settle.
#      KNOWN LIMIT (not confirmed live, MINOR, 2026-09-13 council): if Seerr
#      bumps `updatedAt` on a row for reasons unrelated to a real state
#      change (routine internal touch), a permanently-stranded row could stay
#      "within grace" forever and never confirm. arr-plex-parity.sh trusts
#      the same field the same way, so this is not a new assumption, but it
#      IS unverified against live Seerr behaviour. Left as-is per operator
#      ruling; if this is ever confirmed live, gate grace on a monotonic
#      "first seen by THIS canary" timestamp in the state file instead of
#      trusting Seerr's own `updatedAt`.
#   2. TWO-CONSECUTIVE-RUN gate. A state file under ~/.opt/maint/ records this
#      run's finding keys; a key seen on THIS run and the PREVIOUS run is
#      "confirmed" and pages. First sighting arms (exit 0, `watching=N`);
#      second consecutive sighting pages (exit 1). The read-modify-write of
#      that state file is wrapped in an flock-protected critical section —
#      the 2026-08-26 council found arr-plex-parity's identical state file
#      racing a manual invocation against the systemd timer and silently
#      dropping a page-worthy confirmation; the fix there is reused verbatim
#      here. (On a non-POSIX interpreter — i.e. never in production, only on
#      the operator's Windows workstation running the hermetic test suite —
#      `fcntl` is absent and the lock degrades to `msvcrt` or, failing that,
#      an unlocked read-modify-write with a NAMED, COUNTED skip. The box is
#      Linux; `fcntl.flock` is the only path that ever runs there.)
#
#      STATE-FILE INTEGRITY (2026-09-13 council finding, MAJOR): a state file
#      that fails to open or fails to parse as JSON is NOT silently treated
#      as "no prior findings" and forgotten — that used to swallow the event
#      with zero trace, contradicting rule 4 below. A genuinely MISSING file
#      (first run ever, `FileNotFoundError`) is the expected first-deploy
#      shape and is not a skip. Anything else unreadable-or-corrupt IS a
#      named, counted skip (`state-file-corrupt-or-unreadable`) with its own
#      trail line, and per OPERATOR RULING the gate still arms-this-run (same
#      behaviour as a first run) rather than being treated as a false clean
#      pass — it just no longer happens invisibly.
#
#      PERSISTENT PER-TITLE FAILURE ESCALATION (2026-09-13 council finding,
#      MAJOR): a `tv-detail-fetch-failed` skip on its own only withholds that
#      ONE row for THIS run — harmless for a transient 500. But a Seerr
#      title whose detail endpoint fails EVERY run forever would mask a real
#      STRANDED season indefinitely behind an ever-incrementing skip counter
#      nothing pages on. The state file now tracks CONSECUTIVE per-tmdbId
#      detail-fetch failures across runs; a tmdbId failing on 3 consecutive
#      runs escalates the whole run to CANNOT-ASSERT
#      (`seerr-arr-parity-tv-detail-persistent-failure`, exit 2), naming the
#      stuck id(s) rather than a silent, forever-skipped row. Recovering even
#      once resets that id's counter to 0.
#
# Every intentional exclusion (not-settled status, within-grace, unresolvable
# id, season *arr does not know about, tvdbId collision, state-file
# corruption) is COUNTED AND NAMED in `skips=N(reason:count,...)`, appended to
# every PASS and STAGE line — rule 4, "a suppression or skip must be counted
# and logged, never silent".
#
# LIVE BASELINE (to reproduce on first deploy, per the spec): 47 rows would
# reconcile today (42 orphan + 5 stuck-season/STRANDED). Because of the
# two-consecutive-run gate, the FIRST run after deploy is expected to ARM
# (exit 0, `watching=47(orphan=42,stranded=5,...)`) rather than page
# immediately — identical to how arr-plex-parity's first sighting behaves.
# After the reaper's next `--execute` run clears the backlog, this canary
# must read 0 — never a vacuous pass, because a genuinely empty side (Seerr
# or any *arr returning zero rows) is treated as CANNOT-ASSERT, not clean.
#
# ============================================================================
# EXIT CODES
# ============================================================================
#   0  pass — no confirmed orphan/stranded finding. May be PASS-WARN if a
#      confirmed UNDERREPORTED-only backlog exists (lower severity, reported
#      but never paging by itself).
#   1  finding — >=1 orphan or stranded season confirmed across two
#      consecutive runs.
#   2  CANNOT-ASSERT — Seerr unreachable, ANY of the four *arrs unreachable,
#      an empty *arr list, an empty Seerr list, a bad numeric override, a
#      genuine pagination overflow, or a tmdbId whose tv-detail lookup has
#      failed 3 consecutive runs. Empty-because-broken must never read as
#      empty-because-clean.
#
# STAGE labels (stderr -> Kuma msg=):
#   seerr-arr-parity-bad-config       a numeric override is not a positive
#                                     (or, for retries, non-negative) integer
#   seerr-arr-parity-config-missing   a required secret file is absent
#   seerr-arr-parity-seerr-unreachable  Seerr API failed or returned garbage
#   seerr-arr-parity-seerr-empty      Seerr's media list is empty (2 pages, 0
#                                     rows) — cannot be real on a live box
#   seerr-arr-parity-pagination-overflow  a page AFTER the MAX_PAGES cap came
#                                     back non-empty — Seerr answered every
#                                     request correctly, this is not
#                                     "unreachable", it is "there is more data
#                                     than the cap allows for"
#   seerr-arr-parity-arr-unreachable  one of the four *arrs failed
#   seerr-arr-parity-arr-empty        one of the four *arrs returned 0 items
#   seerr-arr-parity-tv-detail-persistent-failure  a tmdbId's /api/v1/tv
#                                     detail call has failed 3 consecutive
#                                     runs — cannot rule out a masked STRANDED
#   seerr-arr-parity                  >=1 orphan/stranded confirmed (exit 1)
#
# ============================================================================
# ENV OVERRIDES (operator + hermetic tests)
# ============================================================================
#   MANITOBA_SECRETS                secrets dir, default ~/secrets — same
#                                    knob as prowlarr-app-sync.sh
#   QFLIX_CANARY_SAP_GRACE_H        grace window, hours. default 26
#   QFLIX_CANARY_SAP_TIMEOUT_S      per-HTTP-request timeout, seconds. dflt 15
#   QFLIX_CANARY_SAP_RETRIES        transport-error retries per request. dflt 1
#   QFLIX_CANARY_SAP_PAGE_SIZE      Seerr /api/v1/media page size. default 100
#   QFLIX_CANARY_SAP_MAX_PAGES      pagination safety cap, in FULL pages.
#                                    default 200. One extra confirmation
#                                    request is always issued after MAX_PAGES
#                                    full pages — see HONEST LIMITS 4.
#   QFLIX_CANARY_SAP_STATE          two-run state file, default
#                                    ~/.opt/maint/seerr-arr-parity/state.json
#   QFLIX_CANARY_SAP_TRAIL          durable append-only trail, default
#                                    ~/.opt/maint/canary-seerr-arr-parity.log
#   QFLIX_CANARY_SAP_NOW            epoch seconds override for "now" (tests)
#
# ============================================================================
# EXECUTION MODEL — runs LOCALLY on the box, no sshm hop. Every target
# (Seerr, all four *arrs) is a loopback port on the same host this timer
# fires on, exactly like rea-liveness.sh and plex-decision-stable-file.sh —
# and it is what lets the hermetic test drive the real script against a
# fixture HTTP server instead of a live SSH session.
#
# HONEST LIMITS
#   1. Per-row TV lookups cost one extra Seerr HTTP call each (mediaInfo.
#      seasons is not on the list endpoint) — fine at this fleet's size and
#      an hourly cadence, but it is O(settled TV rows), not O(1).
#   2. This canary CANNOT VERIFY the spec's "47 rows today" baseline from a
#      sandboxed generator session with no live Seerr access; the predicate
#      is written to reproduce it once deployed, not proven to on the box.
#   3. UNDERREPORTED is tracked through the identical two-run/state-file
#      machinery as ORPHAN/STRANDED but deliberately never contributes to
#      the exit-1 decision — "report separately, lower severity" per spec.
#   4. PAGINATION BOUNDARY (2026-09-13 council finding, MINOR, fixed): the
#      original loop treated exactly MAX_PAGES full-length pages (a page
#      whose length == PAGE_SIZE, no short final page) as pagination
#      overflow, indistinguishable from a genuinely broken Seerr — a real
#      false CANNOT-ASSERT whenever the live table happens to sit at an exact
#      multiple of PAGE_SIZE at scan time. It now issues ONE more request
#      after the MAX_PAGES'th full page: a short-or-empty trailing page is a
#      clean stop (Seerr answered correctly, there was just a boundary-sized
#      table); a NON-empty page beyond the cap is the real overflow and is
#      its own STAGE (`seerr-arr-parity-pagination-overflow`), not
#      `seerr-unreachable` — Seerr answered every request fine, there is
#      simply more data than the configured cap allows for.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

MANITOBA_SECRETS="${MANITOBA_SECRETS:-$HOME/secrets}"
# `-` NOT `:-` on every numeric knob below is load-bearing, not a style
# choice: `:-` treats an explicitly-EMPTY override the same as UNSET and
# silently substitutes the default, which would let `QFLIX_CANARY_SAP_
# MAX_PAGES=` sail past the positive-integer gate below instead of being
# caught by it -- the exact "x-" vs ":-" trap plex-decision-stable-file.sh's
# header warns about, reproduced here because the fix is per-script, not
# structural. `-` only substitutes when the variable is truly unset.
QFLIX_CANARY_SAP_GRACE_H="${QFLIX_CANARY_SAP_GRACE_H-26}"
QFLIX_CANARY_SAP_TIMEOUT_S="${QFLIX_CANARY_SAP_TIMEOUT_S-15}"
QFLIX_CANARY_SAP_RETRIES="${QFLIX_CANARY_SAP_RETRIES-1}"
QFLIX_CANARY_SAP_PAGE_SIZE="${QFLIX_CANARY_SAP_PAGE_SIZE-100}"
QFLIX_CANARY_SAP_MAX_PAGES="${QFLIX_CANARY_SAP_MAX_PAGES-200}"
QFLIX_CANARY_SAP_STATE="${QFLIX_CANARY_SAP_STATE:-$HOME/.opt/maint/seerr-arr-parity/state.json}"
QFLIX_CANARY_SAP_TRAIL="${QFLIX_CANARY_SAP_TRAIL:-$HOME/.opt/maint/canary-seerr-arr-parity.log}"
QFLIX_CANARY_SAP_NOW="${QFLIX_CANARY_SAP_NOW:-}"

_note_bad_config() {
  mkdir -p "$(dirname "$QFLIX_CANARY_SAP_TRAIL")" 2>/dev/null
  printf '%s bad-config %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$QFLIX_CANARY_SAP_TRAIL" 2>/dev/null
}

# Positive-integer knobs. A bad value is cannot-assert, never a
# silently-disabled probe (same contract as plex-decision-stable-file.sh).
for _pair in GRACE_H:QFLIX_CANARY_SAP_GRACE_H TIMEOUT_S:QFLIX_CANARY_SAP_TIMEOUT_S \
             PAGE_SIZE:QFLIX_CANARY_SAP_PAGE_SIZE MAX_PAGES:QFLIX_CANARY_SAP_MAX_PAGES; do
  _name="${_pair%%:*}"; _env="${_pair##*:}"; _val="${!_env}"
  case "x$_val" in
    x|x*[!0-9]*)
      printf 'STAGE=seerr-arr-parity-bad-config msg=%s=%s-must-be-a-positive-integer-cannot-assert\n' \
        "$_env" "${_val:-EMPTY}" >&2
      _note_bad_config "$_env=${_val:-EMPTY}"; exit 2 ;;
  esac
  if [ "$_val" -le 0 ]; then
    printf 'STAGE=seerr-arr-parity-bad-config msg=%s=%s-must-be-greater-than-zero-cannot-assert\n' \
      "$_env" "$_val" >&2
    _note_bad_config "$_env=$_val"; exit 2
  fi
done
# RETRIES may legitimately be 0 (no retry), so it is validated as
# non-negative separately, same split as plex-decision-stable-file.sh's
# UNREACHABLE_MAX.
case "x$QFLIX_CANARY_SAP_RETRIES" in
  x|x*[!0-9]*)
    printf 'STAGE=seerr-arr-parity-bad-config msg=QFLIX_CANARY_SAP_RETRIES=%s-must-be-a-non-negative-integer-cannot-assert\n' \
      "${QFLIX_CANARY_SAP_RETRIES:-EMPTY}" >&2
    _note_bad_config "QFLIX_CANARY_SAP_RETRIES=${QFLIX_CANARY_SAP_RETRIES:-EMPTY}"; exit 2 ;;
esac

export MANITOBA_SECRETS QFLIX_CANARY_SAP_GRACE_H QFLIX_CANARY_SAP_TIMEOUT_S \
       QFLIX_CANARY_SAP_RETRIES QFLIX_CANARY_SAP_PAGE_SIZE QFLIX_CANARY_SAP_MAX_PAGES \
       QFLIX_CANARY_SAP_STATE QFLIX_CANARY_SAP_TRAIL QFLIX_CANARY_SAP_NOW

exec python3 - "$@" <<'PY'
import calendar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

EXIT_OK, EXIT_FINDING, EXIT_BROKEN = 0, 1, 2

SECRETS = os.environ.get("MANITOBA_SECRETS") or os.path.expanduser("~/secrets")
GRACE_H = int(os.environ["QFLIX_CANARY_SAP_GRACE_H"])
TIMEOUT = int(os.environ["QFLIX_CANARY_SAP_TIMEOUT_S"])
RETRIES = int(os.environ["QFLIX_CANARY_SAP_RETRIES"])
PAGE_SIZE = int(os.environ["QFLIX_CANARY_SAP_PAGE_SIZE"])
MAX_PAGES = int(os.environ["QFLIX_CANARY_SAP_MAX_PAGES"])
STATE_PATH = os.environ["QFLIX_CANARY_SAP_STATE"]
TRAIL = os.environ["QFLIX_CANARY_SAP_TRAIL"]
_now_override = (os.environ.get("QFLIX_CANARY_SAP_NOW") or "").strip()
NOW = int(_now_override) if _now_override.isdigit() else int(time.time())

# Statuses that are legitimately mid-flight and say nothing about
# requestability yet: PENDING, PROCESSING, BLOCKLISTED.
NOT_SETTLED = {2, 3, 6}
STATUS_DELETED = 7
STATUS_AVAILABLE = 5
STATUS_UNKNOWN = 1

SKIPS = {}


def skip(reason):
    SKIPS[reason] = SKIPS.get(reason, 0) + 1


def skip_str():
    if not SKIPS:
        return "skips=0"
    return "skips=%d(%s)" % (
        sum(SKIPS.values()),
        ",".join("%s:%d" % (k, v) for k, v in sorted(SKIPS.items())),
    )


def note(line):
    try:
        os.makedirs(os.path.dirname(TRAIL), exist_ok=True)
        with open(TRAIL, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW)) + " " + line + "\n")
    except OSError:
        pass


def cannot(stage, msg):
    line = "STAGE=%s msg=%s %s" % (stage, msg, skip_str())
    sys.stderr.write(line + "\n")
    note(line)
    sys.exit(EXIT_BROKEN)


def die(stage, msg):
    line = "STAGE=%s msg=%s %s" % (stage, msg, skip_str())
    sys.stderr.write(line + "\n")
    note(line)
    sys.exit(EXIT_FINDING)


def finish(msg, warn=False):
    prefix = "PASS-WARN" if warn else "PASS"
    line = "%s: seerr-arr-parity - %s %s" % (prefix, msg, skip_str())
    print(line)
    note(line)
    sys.exit(EXIT_OK)


def read_secret(name):
    try:
        with open(os.path.join(SECRETS, name), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def http_json(url, key=None, params=None):
    """(ok, payload_or_reason). Retries transport errors only -- identical
    contract to prowlarr-app-sync.sh's http_json(): an HTTP status is an
    answer, not a blip."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json"}
    if key:
        headers["X-Api-Key"] = key
    last = "unknown"
    for attempt in range(RETRIES + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            try:
                return True, json.loads(raw or "null")
            except ValueError:
                return False, "non-json-body"
        except urllib.error.HTTPError as exc:
            return False, "http-%s" % exc.code
        except Exception as exc:                      # noqa: BLE001 - boundary
            last = "transport-%s" % type(exc).__name__
            if attempt >= RETRIES:
                return False, last
    return False, last


def parse_iso_epoch(stamp):
    """None on anything unparseable -- the caller treats that as fail-closed
    (withheld + counted), never as "must be old" or "must be new"."""
    if not stamp:
        return None
    s = str(stamp).rstrip("Z").split(".")[0].replace("T", " ")
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


# --- secrets ----------------------------------------------------------------
SEERR_PORT = read_secret("seerr.port")
SEERR_KEY = read_secret("seerr.key")
missing = []
if not SEERR_PORT:
    missing.append("seerr.port")
if not SEERR_KEY:
    missing.append("seerr.key")

ARR_SPECS = (("sonarr", "tv"), ("sonarr2", "tv"), ("radarr", "movie"), ("radarr2", "movie"))
arrs = {}
for slug, kind in ARR_SPECS:
    port = read_secret(slug + ".port")
    key = read_secret(slug + ".key")
    if not port or not key:
        missing.append("%s.port/.key" % slug)
        continue
    urlbase = read_secret(slug + ".urlbase") or slug
    arrs[slug] = {"kind": kind, "port": port, "key": key, "urlbase": urlbase}

if missing:
    cannot("seerr-arr-parity-config-missing", "missing-secrets=%s" % ",".join(missing))

SEERR_BASE = "http://127.0.0.1:%s" % SEERR_PORT

# --- Seerr media, paged -------------------------------------------------
# HONEST LIMITS 4 / 2026-09-13 council finding (MINOR, boundary): a live
# Seerr table sitting at an EXACT multiple of PAGE_SIZE up to MAX_PAGES full
# pages used to be indistinguishable from genuine pagination breakage -- the
# for/else fired on the MAX_PAGES'th full page even though every single
# request up to that point succeeded and returned real data. Fetching one
# CONFIRMATION page after the cap tells the two cases apart: short-or-empty
# means the table just happened to land on a page boundary (clean stop, no
# STAGE at all); non-empty means there really is more data than the cap
# allows for, which is a genuine (if rare) overflow -- and is reported as
# such (`pagination-overflow`), NOT as `seerr-unreachable`, because Seerr
# answered every request correctly.
rows = []
skip_n = 0
for _page in range(MAX_PAGES):
    ok_, data = http_json(SEERR_BASE + "/api/v1/media", SEERR_KEY,
                           params={"take": PAGE_SIZE, "skip": skip_n})
    if not ok_:
        cannot("seerr-arr-parity-seerr-unreachable",
               "media-api-%s-at-skip=%d" % (data, skip_n))
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        cannot("seerr-arr-parity-seerr-unreachable",
               "media-api-malformed-response-at-skip=%d" % skip_n)
    page = data["results"]
    rows.extend(page)
    if len(page) < PAGE_SIZE:
        break
    skip_n += PAGE_SIZE
else:
    # Every one of the MAX_PAGES pages fetched so far was a FULL page -- ask
    # for exactly one more before concluding anything is actually wrong.
    ok_, data = http_json(SEERR_BASE + "/api/v1/media", SEERR_KEY,
                           params={"take": PAGE_SIZE, "skip": skip_n})
    if not ok_:
        cannot("seerr-arr-parity-seerr-unreachable",
               "media-api-%s-at-skip=%d" % (data, skip_n))
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        cannot("seerr-arr-parity-seerr-unreachable",
               "media-api-malformed-response-at-skip=%d" % skip_n)
    confirm_page = data["results"]
    if confirm_page:
        cannot("seerr-arr-parity-pagination-overflow",
               "media-pagination-exceeded-max-pages=%d-confirm-page-nonempty=%d"
               % (MAX_PAGES, len(confirm_page)))
    # else: a genuinely empty trailing page -- the table landed exactly on a
    # PAGE_SIZE*MAX_PAGES boundary. Clean stop, nothing to report.

if not rows:
    cannot("seerr-arr-parity-seerr-empty",
           "zero-media-rows-across-all-pages-cannot-be-real-on-a-live-box")

# --- the four *arrs, fetched once ---------------------------------------
sonarr_tvdb = {}     # tvdbId -> {seasonNumber: episodeFileCount}
untrusted_tvdb = set()  # tvdbId seen in >1 sonarr instance -- see below
radarr_tmdb = set()

for slug, spec in arrs.items():
    base = "http://127.0.0.1:%s/%s/api/v3" % (spec["port"], spec["urlbase"])
    path = "series" if spec["kind"] == "tv" else "movie"
    ok_, data = http_json(base + "/" + path, spec["key"])
    if not ok_:
        cannot("seerr-arr-parity-arr-unreachable", "%s-api-%s" % (slug, data))
    if not isinstance(data, list):
        cannot("seerr-arr-parity-arr-unreachable", "%s-api-not-a-list" % slug)
    if not data:
        cannot("seerr-arr-parity-arr-empty",
               "%s-returned-zero-items-cannot-be-real-on-a-live-box" % slug)

    if spec["kind"] == "tv":
        for s in data:
            tvdb = s.get("tvdbId")
            if not tvdb:
                skip("sonarr-series-no-tvdbid")
                continue
            seasons = {}
            for se in (s.get("seasons") or []):
                num = se.get("seasonNumber")
                fc = (se.get("statistics") or {}).get("episodeFileCount")
                if num is not None and fc is not None:
                    seasons[num] = fc
            if tvdb in sonarr_tvdb or tvdb in untrusted_tvdb:
                # 2026-09-13 council finding (MAJOR, boundary): keeping the
                # FIRST-seen instance's map here and running the predicate
                # against it produced a false STRANDED page for a season
                # that unambiguously had files in the OTHER sonarr instance.
                # There is no way to know from this side alone which
                # instance is authoritative for a duplicated tvdbId, so the
                # id is marked UNTRUSTED and its (possibly stale) map is
                # DROPPED entirely rather than kept -- every per-row
                # assertion for this tvdbId is withheld below, the same
                # "cannot know, so don't guess" contract as
                # season-unknown-to-sonarr, just at the whole-series level.
                skip("tvdbid-collision-across-sonarr-instances")
                untrusted_tvdb.add(tvdb)
                sonarr_tvdb.pop(tvdb, None)
                continue
            sonarr_tvdb[tvdb] = seasons
    else:
        for m in data:
            tmdb = m.get("tmdbId")
            if tmdb:
                radarr_tmdb.add(tmdb)
            else:
                skip("radarr-movie-no-tmdbid")

# --- the predicate, per settled Seerr row --------------------------------
GRACE_S = GRACE_H * 3600
orphans = {}
stranded = {}
underreported = {}
tv_detail_failed_ids = set()   # tmdbIds whose /api/v1/tv/<id> failed THIS run

for row in rows:
    status = row.get("status")
    if status in NOT_SETTLED:
        skip("not-settled-status-%s" % status)
        continue

    updated_epoch = parse_iso_epoch(row.get("updatedAt"))
    if updated_epoch is None:
        # Fail closed (spec section 5): cannot age it, so withhold rather
        # than guess "old enough" or "too new".
        skip("updatedat-unparseable")
        continue
    if NOW - updated_epoch < GRACE_S:
        skip("within-grace-window")
        continue

    mtype = row.get("mediaType")
    mid = row.get("id")
    tmdb = row.get("tmdbId")

    if mtype == "movie":
        if not tmdb:
            skip("movie-row-no-tmdbid")
            continue
        if tmdb not in radarr_tmdb:
            orphans["orphan:movie:%s" % tmdb] = (
                "orphan-movie tmdb=%s seerr_id=%s status=%s" % (tmdb, mid, status))
        continue

    if mtype != "tv":
        skip("unknown-media-type-%s" % mtype)
        continue

    if not tmdb:
        skip("tv-row-no-tmdbid")
        continue

    ok_, detail = http_json(SEERR_BASE + "/api/v1/tv/%s" % tmdb, SEERR_KEY)
    if not ok_ or not isinstance(detail, dict):
        # A single transient 500 is harmless -- withhold just this row. But
        # see PERSISTENT PER-TITLE FAILURE ESCALATION below: if the SAME
        # tmdbId fails 3 consecutive runs, that stops being transient and a
        # real STRANDED season behind it could be masked forever.
        skip("tv-detail-fetch-failed")
        tv_detail_failed_ids.add(tmdb)
        continue

    tvdb = row.get("tvdbId") or (detail.get("externalIds") or {}).get("tvdbId")
    if not tvdb:
        skip("tv-no-tvdbid-resolvable")
        continue

    if tvdb in untrusted_tvdb:
        # A duplicated tvdbId can never resolve to a trustworthy orphan/
        # stranded/underreported verdict from this side -- withhold the
        # WHOLE row, not just the seasons, rather than fabricate an answer
        # from data we already know is ambiguous.
        skip("tvdbid-untrusted-row-excluded")
        continue

    seerr_seasons = {}
    for s in ((detail.get("mediaInfo") or {}).get("seasons") or []):
        num = s.get("seasonNumber")
        if num is not None:
            seerr_seasons[num] = s.get("status")

    if tvdb not in sonarr_tvdb:
        orphans["orphan:tv:%s" % tvdb] = (
            "orphan-tv tvdb=%s tmdb=%s seerr_id=%s status=%s" % (tvdb, tmdb, mid, status))
        continue

    sonarr_seasons = sonarr_tvdb[tvdb]
    for num in sorted(set(seerr_seasons) | set(sonarr_seasons)):
        sfiles = sonarr_seasons.get(num)
        if sfiles is None:
            skip("season-unknown-to-sonarr")
            continue
        sstatus = seerr_seasons.get(num)  # None == absent from Seerr's list
        if sstatus in (STATUS_AVAILABLE, STATUS_DELETED) and sfiles == 0:
            key = "stranded:%s:%s" % (tvdb, num)
            stranded[key] = ("stranded tvdb=%s season=%s seerr_status=%s sonarr_files=0"
                              % (tvdb, num, sstatus))
        elif (sstatus is None or sstatus == STATUS_UNKNOWN) and sfiles > 0:
            key = "underreported:%s:%s" % (tvdb, num)
            underreported[key] = ("underreported tvdb=%s season=%s seerr_status=%s sonarr_files=%s"
                                   % (tvdb, num, sstatus, sfiles))

# --- persistence: two consecutive sightings page ------------------------
# flock spans the whole read-modify-write, same shape and same reasoning as
# arr-plex-parity.sh (2026-08-26 council, concurrency lens): a manual
# invocation racing the systemd timer must not silently clobber a
# page-worthy confirmation.
current = {}
current.update(orphans)
current.update(stranded)
current.update(underreported)

os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
lock_path = STATE_PATH + ".lock"
lock_fh = None
try:
    lock_fh = open(lock_path, "a+")
except OSError:
    skip("state-lock-file-unopenable")

try:
    import fcntl as _fcntl

    def _lock():
        if lock_fh is not None:
            _fcntl.flock(lock_fh, _fcntl.LOCK_EX)

    def _unlock():
        if lock_fh is not None:
            _fcntl.flock(lock_fh, _fcntl.LOCK_UN)
except ImportError:
    # Never the production path -- the box is Linux and fcntl is always
    # present there. This exists ONLY so the hermetic suite can exercise the
    # two-run gate unmodified on the operator's Windows workstation, where
    # python3 (a bare interpreter on PATH, not this repo's venv) has no
    # fcntl module at all.
    try:
        import msvcrt as _msvcrt

        def _lock():
            if lock_fh is None:
                return
            lock_fh.seek(0)
            try:
                lock_fh.write("x")
                lock_fh.flush()
            except OSError:
                pass
            lock_fh.seek(0)
            _msvcrt.locking(lock_fh.fileno(), _msvcrt.LK_LOCK, 1)

        def _unlock():
            if lock_fh is None:
                return
            lock_fh.seek(0)
            _msvcrt.locking(lock_fh.fileno(), _msvcrt.LK_UNLCK, 1)
    except ImportError:
        skip("flock-unavailable-non-atomic-state-update")

        def _lock():
            pass

        def _unlock():
            pass

_lock()
prev_findings = {}
prev_tv_failures = {}
try:
    with open(STATE_PATH, encoding="utf-8") as fh:
        loaded = json.load(fh) or {}
    prev_findings = loaded.get("findings") or {}
    prev_tv_failures = loaded.get("tv_detail_failures") or {}
except FileNotFoundError:
    # Expected shape on the very first run ever (fresh deploy, or a state
    # dir a human cleared) -- nothing to compare against, and NOT a
    # corruption event, so no skip is counted for this leg.
    pass
except (OSError, ValueError):
    # 2026-09-13 council finding (MAJOR, false-green): this used to swallow
    # a present-but-unreadable-or-corrupt state file the exact same way as
    # "no file yet", with zero trace anywhere -- silently resetting the
    # two-run confirmation gate every time it happened, contradicting rule 4
    # ("a suppression or skip must be counted and logged, never silent").
    # OPERATOR RULING: corruption still arms-this-run (same behaviour as a
    # true first run -- there is genuinely nothing trustworthy to compare
    # against), but it is now a NAMED, COUNTED skip with its own trail line
    # instead of an invisible reset.
    skip("state-file-corrupt-or-unreadable")
    note("STATE-CORRUPT state=%s unreadable-or-invalid-json -- treating as "
         "arm-this-run (no prior findings trusted), gate not silently reset"
         % STATE_PATH)
    prev_findings = {}
    prev_tv_failures = {}

# PERSISTENT PER-TITLE FAILURE ESCALATION (operator ruling 2, MAJOR): a
# tmdbId whose /api/v1/tv detail call fails on THIS run AND failed on the
# previous run(s) recorded in state gets its consecutive-failure counter
# bumped; anything that did NOT fail this run is dropped (one success resets
# it to zero, never "decays" toward the threshold). 3 consecutive failures
# for the SAME id means the row has been silently withheld for three whole
# runs running -- long enough that "transient 500" no longer covers it, and
# a real STRANDED season behind that id could be masked indefinitely.
new_tv_failures = {}
escalate_ids = []
for _tmdb in tv_detail_failed_ids:
    _key = str(_tmdb)
    _count = prev_tv_failures.get(_key, 0) + 1
    new_tv_failures[_key] = _count
    if _count >= 3:
        escalate_ids.append((_tmdb, _count))

tmp_path = STATE_PATH + ".tmp"
with open(tmp_path, "w", encoding="utf-8") as fh:
    json.dump({"checked": NOW, "findings": {k: True for k in current},
               "tv_detail_failures": new_tv_failures}, fh)
os.replace(tmp_path, STATE_PATH)
_unlock()
if lock_fh is not None:
    lock_fh.close()

if escalate_ids:
    escalate_ids.sort(key=lambda pair: -pair[1])
    named = ",".join("tmdb=%s(consecutive=%d)" % (t, c) for t, c in escalate_ids[:10])
    cannot("seerr-arr-parity-tv-detail-persistent-failure",
           "tv-detail-lookup-failed-3-plus-consecutive-runs=%s" % named)

confirmed = {k: v for k, v in current.items() if k in prev_findings}
c_orphan = {k: v for k, v in confirmed.items() if k.startswith("orphan:")}
c_stranded = {k: v for k, v in confirmed.items() if k.startswith("stranded:")}
c_under = {k: v for k, v in confirmed.items() if k.startswith("underreported:")}

counts = "orphan=%d stranded=%d underreported=%d" % (len(orphans), len(stranded), len(underreported))
confirmed_counts = ("confirmed_orphan=%d confirmed_stranded=%d confirmed_underreported=%d"
                     % (len(c_orphan), len(c_stranded), len(c_under)))

if c_orphan or c_stranded:
    names = list(c_orphan.values())[:5] + list(c_stranded.values())[:5]
    detail = "; ".join(names)
    if len(c_orphan) + len(c_stranded) > len(names):
        detail += "; +%d more" % (len(c_orphan) + len(c_stranded) - len(names))
    die("seerr-arr-parity",
        "%d-orphan+%d-stranded-confirmed-across-2-consecutive-runs(rows=%d %s %s): %s"
        % (len(c_orphan), len(c_stranded), len(rows), counts, confirmed_counts, detail[:400]))

finish("rows=%d %s %s" % (len(rows), counts, confirmed_counts), warn=bool(c_under))
PY
