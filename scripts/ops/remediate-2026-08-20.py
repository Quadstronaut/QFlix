#!/usr/bin/env python3
"""One-off (2026-08-20): four live-app hygiene defects the 2026-08-19/20 audit confirmed.

Runs ON the seedbox (it reads ~/secrets directly):

    python3 ~/scripts/ops/remediate-2026-08-20.py             # dry-run, prints the plan
    python3 ~/scripts/ops/remediate-2026-08-20.py --execute    # writes

Dry-run is the default for the same reason qflix-reaper.py and
qflix-torrent-janitor.py default to it: every write here lands in a live app
that members are using, and none of them is undone by re-running the script.
Every write is verified by RE-READING the app afterwards, because both the
*arr API and the Bazarr settings API return success for writes that did not
land -- Bazarr's POST returns a bare 204 with no echo of what it stored, and
Radarr's /movie/editor controller silently null-skips fields.

---------------------------------------------------------------------------
TASK A -- BAZARR: disable two permanently dead subtitle providers
---------------------------------------------------------------------------
greeksubtitles returns 403 Forbidden on every search; hosszupuska drops the
connection mid-request. Measured on the box 2026-08-19:

    Throttling greeksubtitles for 10 minutes ... "'403 Client Error:
      Forbidden for url: https://gr.greek-subtitles.com/search.'
      ~ greeksubtitles.py@83"
    Throttling hosszupuska for 10 minutes ... "ConnectionError
      ('Connection aborted.', RemoteDisconnected(...)) ~ hosszupuska.py@159"

    37 x "Throttling greeksubtitles"  across the retained bazarr-1 logs
    33 x "Throttling hosszupuska"
    ( 7 x "Throttling tvsubtitles" -- ALREADY disabled 2026-08-14 for a
      domain SERVFAIL. This script never adds a provider back, and the
      dead-provider list below deliberately does not name it. )

Bazarr throttles for 10 minutes, un-throttles, fails again immediately, and
repeats forever. That is a permanent per-search latency tax plus log noise on
an instance nobody watches.

PREMISE CORRECTION, recorded rather than papered over: the audit said the
cycle runs on BOTH instances. It does not. bazarr2's retained logs contain
ZERO "Throttling" lines -- bazarr2 is the anime instance and has not searched
enough to trip them. What IS true on both is the cause: both carry
greeksubtitles and hosszupuska in general.enabled_providers, so bazarr2 is a
loaded gun rather than a firing one. Both get disabled.

HOW BAZARR TAKES THIS WRITE -- the part that was burned once already.
POST /api/system/settings ends in (api/system/settings.py):

    save_settings(zip(request.form.keys(), request.form.listvalues()))

so a list-valued setting is expressed as the SAME FORM KEY REPEATED, one
occurrence per element. enabled_providers is in app/config.py's array_keys,
which means a single-element list is NOT unwrapped. Post a JSON array as one
value and Bazarr stores [["a","b",...]] -- a nested list -- and the provider
list is broken. That is exactly what happened on 2026-08-14 and had to be
repaired by hand. So: urlencode a list of (key, value) pairs, never json.
The verification step re-reads the list and fails if any element is not a
plain string, which is the signature of that corruption.

enabled_providers is NOT one of the keys that sets reset_providers in
save_settings (only credential keys do), so the two dead providers keep their
rows in the throttle ledger. That is harmless: get_providers only ever
consults the enabled list.

Endpoints (from ~/secrets, verified live 2026-08-19):
  bazarr-1 is a CONTAINER. Its own 6767 is dead. It is reachable only through
  the nginx map at 127.0.0.1:17031/bazarr. bazarr2 is a normal user unit at
  127.0.0.1:17032/bazarr2. Do not "fix" a bazarr-1 timeout by trying 6767.

---------------------------------------------------------------------------
TASK B -- RADARR: one record that holds a file it can never re-acquire
---------------------------------------------------------------------------
"Untracked" is not a field in the Radarr API; it is Radarr's own wording in
the decision engine. From ~/.apps/radarr/logs/, 2026-08-17..19:

    MonitoredMovieSpecification|[Bull Durham (1988)][tt0094812, 287]
        is present in the DB but not tracked. Rejecting

That message is emitted by MonitoredMovieSpecification when movie.Monitored
is false, and it comes back as a PERMANENT rejection on every candidate:

    rejected for the following reasons: [Permanent] Existing file meets
    cutoff: HDTV-720p [], [Permanent] Movie is not monitored

Exhaustive count of that log line over the whole retained window -- exactly
three movies, no more:

    76 x [Bull Durham (1988)][tt0094812, 287]        -> radarr movie id 447
    19 x [M3GAN 2.0 (2025)][tt26342662, 1071585]     -> radarr movie id 309
    12 x [Avatar (2009)][tt0499549, 19995]           -> radarr movie id 281

TWO OF THE THREE ARE NOT A DEFECT AND WERE CUT FROM THIS SCRIPT (review,
2026-08-20). The first draft targeted all three. Re-checked against the live
API before shipping:

    tmdb 19995   Avatar (2009)    monitored=false hasFile=false
                 collection="Avatar Collection"   qualityProfileId=6
    tmdb 1071585 M3GAN 2.0 (2025) monitored=false hasFile=false
                 collection="M3GAN Collection"    qualityProfileId=6

Those are TMDB collection siblings that Radarr adds unmonitored on purpose --
57 of this instance's 114 movies are unmonitored for exactly that reason. A
permanent rejection on an unmonitored sibling is the decision engine WORKING,
not a fault, so "it appears in the rejection log" is a refuted premise for
them. Monitoring them is not hygiene, it is a request to download two movies
nobody asked for. Worse, both sit on quality profile 6 -- the profile this
same session is capping because it allowed Remux-1080p and produced a
26-day movie-playback outage -- so a re-monitor would have queued two fresh
grabs at the top allowed tier and rebuilt the failure by hand. Both targets
are gone from RADARR_UNTRACKED, and hasFile=false is now a hard refusal in
code (see the FILE-BEARING GUARD) so a later edit cannot put them back
without also removing that guard on purpose.

What is left is the one real defect. tmdb 287, Bull Durham (1988), radarr id
447: no collection, added 2026-08-12 with a search, already holds a 5.35 GB
file (movieFileId 513, HDTV-720p, which Radarr itself grades as meeting the
profile cutoff), and was unmonitored afterwards by something unrecorded.
Because the file meets cutoff, re-monitoring downloads NOTHING today -- the
second [Permanent] rejection reason stays in force. What it fixes is the
future: qflix-reaper deletes on add-date, and an unmonitored record whose file
has been reaped can never re-acquire it, so the title would silently vanish
from the library with a Radarr row still claiming to track it.

MINIMAL REMEDY. The only field producing the rejection is `monitored`, so the
only field this script writes is `monitored`. It goes through
PUT /movie/editor with movieIds + monitored + moveFiles:false -- the same
call quality_fallback.py uses to park a movie, in reverse. Profile, path,
root folder, tags and movieFileId are read before the write and asserted
unchanged after it, because /movie/editor is a bulk endpoint and a mis-shaped
body there can stomp a quality profile.

THREE SAFETY GUARDS, all because the naive version of this is harmful:

 0. FILE-BEARING GUARD (added by the 2026-08-20 review). A target with
    hasFile=false is REFUSED, loudly, and reported as a precondition failure.
    Re-monitoring a record that already holds a satisfying file is a no-op at
    the download layer; re-monitoring an empty record is a download order.
    Those are different acts and only the first one is hygiene. This is the
    guard that keeps the two collection siblings out for good.

 1. FALLBACK-PARK GUARD. scripts/mcp/quality_fallback.py deliberately
    unmonitors a movie at PARK_DAYS=15 ("UNFINDABLE after N days ...
    unmonitored. Manual intervention needed.") and records it in
    ~/.apps/qflix-fallback/state.json as parked:true. Re-monitoring a parked
    movie silently re-enters the ramp the policy just exited. None of the
    three are in that ledger today (checked 2026-08-19: 11 other radarr keys
    plus one radarr2 key), but the guard is unconditional so a re-run months
    later cannot undo a park.

 2. IDENTITY GUARD. Targets are matched by tmdbId, never by movie id, and the
    title+year read back from the API must match the audited evidence or the
    movie is skipped and reported. Radarr ids are not stable across a re-add.

--skip-tmdb is kept so an operator can drop a target at the command line
without editing the script, even though only one target remains.

---------------------------------------------------------------------------
TASK C -- SEERR: nightly Plex scan duplicate key (REPORT ONLY, NEVER WRITES)
---------------------------------------------------------------------------
Deliberately read-only. See the printed report; the fix is a single-row
sqlite UPDATE against a live Jellyseerr database and that needs operator eyes
and a stopped service, not an automated write buried in a batch script.

---------------------------------------------------------------------------
TASK D -- SONARR: block the LimeTorrents fake-release family
---------------------------------------------------------------------------
Three grabs, three .exe payloads, three ~7h queue slots burned before the
QFlix janitor blocklisted them. Sonarr history, read live:

    2026-08-09 18:28 grabbed  Ted Lasso S04E02 1080p HEVC x265 MeGusta  LimeTorrents (Prowlarr)
    2026-08-10 02:15 downloadFailed
    2026-08-12 18:41 grabbed  Ted Lasso S04E03 1080p HEVC x265 MeGusta  LimeTorrents (Prowlarr)
    2026-08-13 01:15 downloadFailed
    2026-08-19 12:13 grabbed  Ted Lasso S04E04 1080p HEVC x265 MeGusta  LimeTorrents (Prowlarr)
    2026-08-19 20:15 downloadFailed

WHY NOT THE PROWLARR PRIORITY CHANGE. Indexer priority in Prowlarr/*arr is a
TIE-BREAKER between releases that already scored equally; it never rejects
anything. All 23 Prowlarr indexers currently sit at the default priority 25,
and at each grab moment the fake was the only 1080p HEVC candidate on the
torrent side -- the good NZBgeek WEB-DL for the same episode arrived later,
behind the usenet delay profile. Demoting LimeTorrents 25 -> 50 would have
blocked exactly zero of the three. It is both the bigger change (it re-ranks
every category on that indexer for every *arr) and the ineffective one.

WHY NOT DISABLE LIMETORRENTS. It is not an all-bad indexer: on 2026-08-09 it
delivered "Ted Lasso S04E01 PROPER 1080p WEB h264 ETHEL", which imported
cleanly. Disabling it costs real grabs.

WHAT THIS DOES INSTEAD. One Sonarr release profile, scoped to the
LimeTorrents indexer only, with a single Must-Not-Contain term:

    /\bHEVC x265 MeGusta\b/i

The space-delimited form is the signature. LimeTorrents strips punctuation
from every title it publishes, so the good ETHEL grab above is space-form
too -- space-form alone is NOT the discriminator and blocking it would blind
the indexer entirely. Genuine MeGusta releases from every other indexer are
dot-and-dash form (".HEVC.x265-MeGusta") and do not match this term. Scoping
to indexerId keeps the blast radius to the one indexer that produced all
three payloads.

sonarr2 is checked and skipped: its indexer list (7 entries, all anime plus
NZBgeek) contains no LimeTorrents, so a profile there would guard nothing.

---------------------------------------------------------------------------
EXIT CODES
    0  every selected task is green (already-correct, or written and verified)
    1  a write was attempted and failed, failed its read-back, or its outcome
       is UNKNOWN because the transport died mid-request
    2  a precondition could not be established (app unreachable, secret
       missing, evidence no longer matches). Nothing was written for that
       task, and "could not tell" is never reported as success.

    1 OUTRANKS 2 -- deliberately, and it is not what max() would do. The first
    draft accumulated `rc = max(rc, ...)`, which made a benign skip (2) mask a
    real failed write (1) in the same run, i.e. the script's own exit code
    contradicted the contract above. Failures and skips are now tracked
    separately and 1 wins whenever both happened.

    A transport error DURING a write is graded 1, never 2. A connection that
    dies after the request left the socket may well have landed the change; "I
    do not know whether I wrote" is a failure state, not a clean skip.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
SECRETS = HOME / "secrets"
FALLBACK_STATE = HOME / ".apps" / "qflix-fallback" / "state.json"
TIMEOUT = 30

# --- task A -------------------------------------------------------------
BAZARR_DEAD_PROVIDERS = ["greeksubtitles", "hosszupuska"]
BAZARR_FORM_KEY = "settings-general-enabled_providers"
BAZARR_INSTANCES = ["bazarr", "bazarr2"]

# --- task B -------------------------------------------------------------
# tmdbId -> (expected title, expected year), straight from the
# MonitoredMovieSpecification "not tracked" grep. See the header.
#
# ONE entry, not three. tmdb 19995 (Avatar) and 1071585 (M3GAN 2.0) also appear
# in that grep and were cut on review 2026-08-20: both are fileless TMDB
# collection siblings that Radarr unmonitors by design, so re-monitoring them
# orders two downloads rather than fixing anything. Anything added back here
# must also survive the hasFile guard in task_b().
RADARR_UNTRACKED = {
    287: ("Bull Durham", 1988),
}

# --- task D -------------------------------------------------------------
SONARR_PROFILE_NAME = "QFlix: block LimeTorrents space-form MeGusta fakes"
SONARR_BLOCK_TERM = r"/\bHEVC x265 MeGusta\b/i"
SONARR_INDEXER_PREFIX = "LimeTorrents"


class Fail(Exception):
    """Precondition could not be established -> exit 2 for this task."""


class WriteFail(Exception):
    """A write left the socket and its outcome is UNKNOWN -> exit 1.

    Deliberately NOT a Fail. A urllib transport error raised out of a POST/PUT
    is indistinguishable, from here, from "the app applied it and then the
    connection died", so it can never be graded as the clean did-not-write skip
    that Fail means. The first draft let these fall into the same `except Fail`
    arm as an unreachable app and printed "SKIP precondition", which asserts
    something the script does not know.
    """


def _grade(fail, skip):
    """Collapse a task's two flags into its exit code.

    1 (a write failed, or its outcome is unknown) OUTRANKS 2 (a precondition
    was never established). The obvious `rc = max(rc, n)` inverts that and lets
    a benign skip in one instance hide a real failed write in another, so the
    exit code would contradict the contract in the module docstring.
    """
    if fail:
        return 1
    return 2 if skip else 0


def out(msg):
    print(msg, flush=True)


def secret(name):
    p = SECRETS / name
    if not p.exists():
        raise Fail("missing secret " + str(p))
    return p.read_text().strip()


def http(url, *, headers=None, method="GET", body=None, ctype=None):
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        raise Fail(method + " " + url + " -> " + repr(exc))


def get_json(url, headers):
    code, raw = http(url, headers=headers)
    if code != 200:
        raise Fail("GET " + url + " -> HTTP " + str(code))
    try:
        return json.loads(raw)
    except ValueError:
        raise Fail("GET " + url + " -> non-JSON body")


def write(url, *, headers, method, body, ctype):
    """Every POST/PUT in this script goes through here.

    Its only job is to re-label a transport error so it cannot be caught by an
    `except Fail` arm and reported as a clean precondition skip. See WriteFail.
    """
    try:
        return http(url, headers=headers, method=method, body=body, ctype=ctype)
    except Fail as exc:
        raise WriteFail(str(exc))


# ---------------------------------------------------------------- task A
def bazarr_base(slug):
    port = secret(slug + ".port")
    urlbase = secret(slug + ".urlbase").lstrip("/")
    return "http://127.0.0.1:" + port + "/" + urlbase


def task_a(execute):
    """Disable the two dead providers on both Bazarr instances."""
    fail = skip = False
    for slug in BAZARR_INSTANCES:
        try:
            base = bazarr_base(slug)
            hdr = {"X-API-KEY": secret(slug + ".key")}
            settings = get_json(base + "/api/system/settings", hdr)
            current = (settings.get("general") or {}).get("enabled_providers")
            if not isinstance(current, list) or not all(isinstance(x, str) for x in current):
                raise Fail(slug + ": enabled_providers is not a flat list of strings "
                           "(nested-list corruption?): " + repr(current)[:200])

            removed = [p for p in current if p in BAZARR_DEAD_PROVIDERS]
            keep = [p for p in current if p not in BAZARR_DEAD_PROVIDERS]
            if not removed:
                out("A " + slug + ": already clean (" + str(len(current)) +
                    " providers, neither dead provider present)")
                continue
            if not keep:
                # An empty list posts as [] and would leave the instance with no
                # subtitle providers at all. Never do that automatically.
                raise Fail(slug + ": removing " + ",".join(removed) +
                           " would empty enabled_providers -- refusing")

            # NEVER-ENABLE INVARIANT, checked rather than promised. `keep` is
            # built by filtering `current`, so it can only ever shrink -- but
            # "this script never turns a provider back on" is exactly the kind
            # of prose promise a later edit breaks silently, and the specific
            # thing it must never re-enable is tvsubtitles (disabled on both
            # instances 2026-08-14 for a domain SERVFAIL; nothing in the app
            # would stop it coming back). Assert the subset relation instead.
            added = sorted(set(keep) - set(current))
            if added:
                raise Fail(slug + ": refusing to POST -- would ENABLE " +
                           ",".join(added) + "; this script only ever removes")

            out("A " + slug + ": remove " + ",".join(removed) + "  (" +
                str(len(current)) + " providers -> " + str(len(keep)) + ")")
            if not execute:
                continue

            # REPEATED FORM KEYS -- one (key, value) pair per provider. A JSON
            # array here stores a nested list and breaks the provider list
            # (burned 2026-08-14).
            body = urllib.parse.urlencode([(BAZARR_FORM_KEY, p) for p in keep]).encode()
            code, raw = write(base + "/api/system/settings", headers=hdr, method="POST",
                              body=body, ctype="application/x-www-form-urlencoded")
            if code not in (200, 204):
                out("A " + slug + ": FAIL POST -> HTTP " + str(code) + " " +
                    raw[:200].decode("utf-8", "replace"))
                fail = True
                continue

            # Bazarr's POST returns a bare 204 and echoes nothing. Re-read.
            # A read-back that itself fails is graded 1, not 2: the write has
            # already landed, so "could not verify" is a failure of this run,
            # not a precondition that stopped it starting.
            try:
                after = ((get_json(base + "/api/system/settings", hdr).get("general") or {})
                         .get("enabled_providers"))
            except Fail as exc:
                out("A " + slug + ": FAIL wrote but could not read back -- " + str(exc))
                fail = True
                continue
            if not isinstance(after, list) or not all(isinstance(x, str) for x in after):
                out("A " + slug + ": FAIL read-back is not a flat string list -- "
                    "nested-list corruption: " + repr(after)[:200])
                fail = True
            elif sorted(after) != sorted(keep):
                still = sorted(set(after) & set(BAZARR_DEAD_PROVIDERS))
                out("A " + slug + ": FAIL read-back mismatch (expected " +
                    str(len(keep)) + ", got " + str(len(after)) +
                    (", dead still present: " + ",".join(still) if still else "") + ")")
                fail = True
            else:
                out("A " + slug + ": OK verified " + str(len(after)) +
                    " providers, " + ",".join(removed) + " gone")
        except WriteFail as exc:
            out("A " + slug + ": FAIL POST outcome UNKNOWN (transport died "
                "mid-request; re-run the dry-run to see what landed) -- " + str(exc))
            fail = True
        except Fail as exc:
            out("A " + slug + ": SKIP precondition -- " + str(exc))
            skip = True
    return _grade(fail, skip)


# ---------------------------------------------------------------- task B
def arr_base(slug, api="v3"):
    port = secret(slug + ".port")
    # NOTE: the *arr urlbase secrets carry NO leading slash. lstrip is belt
    # and braces so a hand-edited file with one does not produce a "//" path.
    urlbase = secret(slug + ".urlbase").lstrip("/")
    return "http://127.0.0.1:" + port + "/" + urlbase + "/api/" + api


def _parked_tmdb_ids():
    """tmdbIds quality_fallback.py has deliberately parked (unmonitored)."""
    try:
        state = json.loads(FALLBACK_STATE.read_text())
    except Exception:
        return set()          # an absent ledger just means nothing is parked yet
    parked = set()
    for key, rec in (state.get("movies") or {}).items():
        if rec.get("parked") and key.startswith("radarr:"):
            try:
                parked.add(int(key.split(":", 1)[1]))
            except ValueError:
                pass
    return parked


def task_b(execute, skip_tmdb):
    """Restore `monitored` on the records Radarr calls 'not tracked'."""
    fail = skip = False
    try:
        base = arr_base("radarr")
        hdr = {"X-Api-Key": secret("radarr.key")}
        movies = get_json(base + "/movie", hdr)
    except Fail as exc:
        out("B radarr: SKIP precondition -- " + str(exc))
        return 2

    by_tmdb = {m.get("tmdbId"): m for m in movies}
    parked = _parked_tmdb_ids()

    for tmdb, (title, year) in RADARR_UNTRACKED.items():
        tag = "B radarr tmdb:" + str(tmdb) + " " + title + " (" + str(year) + ")"
        if tmdb in skip_tmdb:
            out(tag + ": SKIP requested by --skip-tmdb")
            continue
        m = by_tmdb.get(tmdb)
        if m is None:
            out(tag + ": SKIP no longer in radarr (record removed since the audit)")
            skip = True
            continue
        if m.get("title") != title or m.get("year") != year:
            out(tag + ": SKIP identity mismatch, API says " +
                repr(m.get("title")) + " (" + str(m.get("year")) + ")")
            skip = True
            continue
        if tmdb in parked:
            out(tag + ": SKIP parked by quality_fallback -- re-monitoring would "
                "re-enter the ramp the policy exited")
            continue
        if m.get("monitored"):
            out(tag + ": already monitored")
            continue
        if not m.get("hasFile"):
            # FILE-BEARING GUARD. Re-monitoring a record that already holds a
            # cutoff-meeting file changes nothing at the download layer; doing
            # it to an EMPTY record is a download order for a title nobody
            # requested. This is why the two collection siblings were cut on
            # review 2026-08-20 -- see the header -- and the guard is in code so
            # putting them back in RADARR_UNTRACKED is not enough to make the
            # script act on them.
            out(tag + ": SKIP hasFile=false -- re-monitoring an empty record "
                      "orders a download, which is a policy call, not hygiene")
            skip = True
            continue

        coll = (m.get("collection") or {}).get("title")
        note = ("collection sibling of " + coll) if coll else "standalone record"
        out(tag + ": monitored false -> true  [" + note + "; hasFile=True]")
        if not execute:
            continue

        before = {k: m.get(k) for k in
                  ("qualityProfileId", "path", "rootFolderPath",
                   "minimumAvailability", "tags", "movieFileId")}
        body = json.dumps({"movieIds": [m["id"]], "monitored": True,
                           "moveFiles": False}).encode()
        try:
            code, raw = write(base + "/movie/editor", headers=hdr, method="PUT",
                              body=body, ctype="application/json")
        except WriteFail as exc:
            out(tag + ": FAIL PUT outcome UNKNOWN (transport died mid-request) -- "
                + str(exc))
            fail = True
            continue
        if code not in (200, 202):
            out(tag + ": FAIL PUT /movie/editor -> HTTP " + str(code) + " " +
                raw[:200].decode("utf-8", "replace"))
            fail = True
            continue

        # /movie/editor is a bulk endpoint that null-skips fields; read back and
        # prove nothing but `monitored` moved.
        try:
            after = get_json(base + "/movie/" + str(m["id"]), hdr)
        except Fail as exc:
            out(tag + ": FAIL read-back -- " + str(exc))
            fail = True
            continue
        if not after.get("monitored"):
            out(tag + ": FAIL read-back still monitored=false")
            fail = True
            continue
        drift = [k for k, v in before.items() if after.get(k) != v]
        if drift:
            out(tag + ": FAIL editor stomped unrelated fields: " + ",".join(drift))
            fail = True
            continue
        out(tag + ": OK verified monitored=true, no collateral field change")
    return _grade(fail, skip)


# ---------------------------------------------------------------- task C
SEERR_REPORT = """C jellyseerr: REPORT ONLY -- this script never writes to the Seerr DB.

  Symptom  the 01:00 Plex scan fails on the same title every night,
           2026-08-14 through 2026-08-20 (7 of 7 days, one line per run):

    [error][Plex Scan]: Failed to process Plex media
      {"errorMessage":"SQLITE_CONSTRAINT: UNIQUE constraint failed: media.tvdbId",
       "title":"Monster (2022)"}

  Constraint  UQ_41a289eb1fa489c1bc6f38d9c3c  UNIQUE ("tvdbId")  on table media
  Key         tvdbId = 389492

  Root cause  the row is ALREADY THERE and it is the only broken one of its
              kind. media.id=47: mediaType=tv, tmdbId=113988, tvdbId=389492,
              status=5 (available), ratingKey IS NULL (ratingKey4k NULL too).
              Nine tv rows are status=5; row 47 is the ONLY one without a
              ratingKey. With no ratingKey the Plex scan does not recognise
              the row as the library item in front of it, tries to INSERT a
              second row for Plex ratingKey 7693, and collides on the unique
              tvdbId constraint. There is no duplicate in the table -- the
              insert is rejected every time, so the scan simply never
              finishes that title. Verified 2026-08-20 with
              `sqlite3 -readonly` (which does see the 4 MB WAL).

  Plex side   ratingKey 7693, "Monster (2022)", section 2 (QFlix - TV).
              Its Guid list carries FIVE ids: imdb://tt13207736,
              tmdb://113988, tmdb://138807, tmdb://225634, tvdb://389492 --
              a merged anthology show, which is why the scan's tmdb-side
              lookup does not land back on row 47.

  Fix, for the operator to run BY HAND. Note the mechanics, because the
  obvious commands are wrong on this box:

    * seerr has NO systemd --user unit (`systemctl --user list-unit-files`
      matches nothing for seerr/jellyseerr) and the docker socket is
      permission-denied, so it is neither `systemctl --user stop jellyseerr`
      nor `docker stop`. It is an Ultra.cc PANEL app: control it with the
      panel wrapper /usr/bin/app-jellyseerr (run it with no arguments to see
      the verb list) or from the panel UI.
    * the DB is in WAL mode with a live 4 MB -wal beside it, so `cp
      db.sqlite3` alone is an INCOMPLETE backup. Use sqlite3 .backup, which
      folds the WAL in.

      app-jellyseerr stop      # verb per the wrapper's own usage output
      sqlite3 ~/.apps/seerr/db/db.sqlite3 ".backup '$HOME/.apps/seerr/db/db.sqlite3.bak-20260820'"
      sqlite3 ~/.apps/seerr/db/db.sqlite3 "UPDATE media SET ratingKey='7693', updatedAt=datetime('now') WHERE id=47 AND tvdbId=389492;"
      app-jellyseerr start

  Then confirm at the next 01:00 run that seerr-2026-08-21.log carries no
  'UNIQUE constraint failed: media.tvdbId' line (every day 2026-08-14
  through 2026-08-20 has exactly one). Do NOT delete row 47: it is status=5
  and owns two season rows (id 137 seasonNumber=1 status=5, id 370
  seasonNumber=0 status=1), so dropping it would revoke the title's
  availability and any request history attached."""


def task_c(execute):
    out(SEERR_REPORT)
    return 0


# ---------------------------------------------------------------- task D
def task_d(execute):
    """One indexer-scoped Must-Not-Contain on sonarr; sonarr2 has no LimeTorrents."""
    fail = skip = False
    for slug in ("sonarr", "sonarr2"):
        try:
            base = arr_base(slug)
            hdr = {"X-Api-Key": secret(slug + ".key")}
            indexers = get_json(base + "/indexer", hdr)
            lime = [i for i in indexers
                    if str(i.get("name", "")).startswith(SONARR_INDEXER_PREFIX)]
            if not lime:
                out("D " + slug + ": SKIP no " + SONARR_INDEXER_PREFIX +
                    " indexer on this instance (" + str(len(indexers)) +
                    " indexers) -- a profile here would guard nothing")
                continue
            if len(lime) > 1:
                raise Fail(slug + ": " + str(len(lime)) + " indexers match " +
                           SONARR_INDEXER_PREFIX + "; refusing to guess")
            lime_id = lime[0]["id"]

            desired = {
                "name": SONARR_PROFILE_NAME,
                "enabled": True,
                "required": [],
                "ignored": [SONARR_BLOCK_TERM],
                "indexerId": lime_id,
                "tags": [],
            }
            profiles = get_json(base + "/releaseprofile", hdr)
            existing = next((p for p in profiles
                             if p.get("name") == SONARR_PROFILE_NAME), None)

            if (existing and existing.get("enabled")
                    and existing.get("indexerId") == lime_id
                    and list(existing.get("ignored") or []) == [SONARR_BLOCK_TERM]):
                out("D " + slug + ": already correct (profile id " +
                    str(existing["id"]) + ", scoped to indexer " + str(lime_id) + ")")
                continue

            verb = ("update profile id " + str(existing["id"])) if existing else "create profile"
            out("D " + slug + ": " + verb + " " + repr(SONARR_PROFILE_NAME) +
                " ignored=" + SONARR_BLOCK_TERM +
                " indexerId=" + str(lime_id) + " (" + lime[0]["name"] + ")")
            if not execute:
                continue

            if existing:
                desired["id"] = existing["id"]
                code, raw = write(base + "/releaseprofile/" + str(existing["id"]),
                                  headers=hdr, method="PUT",
                                  body=json.dumps(desired).encode(),
                                  ctype="application/json")
                ok = code in (200, 202)
            else:
                code, raw = write(base + "/releaseprofile", headers=hdr, method="POST",
                                  body=json.dumps(desired).encode(),
                                  ctype="application/json")
                ok = code in (200, 201, 202)
            if not ok:
                out("D " + slug + ": FAIL HTTP " + str(code) + " " +
                    raw[:300].decode("utf-8", "replace"))
                fail = True
                continue

            # Read-back failure after a landed write is graded 1, not 2 -- the
            # profile may exist and be wrong, which is not a clean skip.
            try:
                after = get_json(base + "/releaseprofile", hdr)
            except Fail as exc:
                out("D " + slug + ": FAIL wrote but could not read back -- " + str(exc))
                fail = True
                continue
            got = next((p for p in after if p.get("name") == SONARR_PROFILE_NAME), None)
            if not got:
                out("D " + slug + ": FAIL read-back, profile absent after write")
                fail = True
            elif (not got.get("enabled") or got.get("indexerId") != lime_id
                  or SONARR_BLOCK_TERM not in (got.get("ignored") or [])):
                out("D " + slug + ": FAIL read-back mismatch: enabled=" +
                    str(got.get("enabled")) + " indexerId=" + str(got.get("indexerId")) +
                    " ignored=" + repr(got.get("ignored")))
                fail = True
            else:
                out("D " + slug + ": OK verified profile id " + str(got["id"]) +
                    " blocks " + SONARR_BLOCK_TERM + " on indexer " + str(lime_id))
        except WriteFail as exc:
            out("D " + slug + ": FAIL write outcome UNKNOWN (transport died "
                "mid-request; re-run the dry-run to see what landed) -- " + str(exc))
            fail = True
        except Fail as exc:
            out("D " + slug + ": SKIP precondition -- " + str(exc))
            skip = True
    return _grade(fail, skip)


TASKS = {"a": task_a, "b": task_b, "c": task_c, "d": task_d}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="2026-08-20 live-app hygiene remediation (dry-run by default)")
    ap.add_argument("--execute", action="store_true",
                    help="actually write. Without it nothing is written and the "
                         "plan is printed.")
    ap.add_argument("--only", default="abcd",
                    help="subset of tasks to run, e.g. --only=ad (default: abcd)")
    ap.add_argument("--skip-tmdb", default="",
                    help="task B only: comma-separated tmdbIds to leave unmonitored, "
                         "e.g. --skip-tmdb=19995,1071585")
    args = ap.parse_args(argv)

    selected = [t for t in "abcd" if t in args.only.lower()]
    if not selected:
        print("no tasks selected by --only=" + args.only, file=sys.stderr)
        return 2
    skip_tmdb = set()
    for chunk in args.skip_tmdb.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            skip_tmdb.add(int(chunk))
        except ValueError:
            print("--skip-tmdb: not an integer: " + chunk, file=sys.stderr)
            return 2

    out("remediate-2026-08-20  mode=" + ("EXECUTE" if args.execute else "DRY-RUN") +
        "  tasks=" + "".join(selected))
    out("-" * 72)

    worst_fail = worst_skip = False
    for t in selected:
        fn = TASKS[t]
        got = fn(args.execute, skip_tmdb) if t == "b" else fn(args.execute)
        worst_fail = worst_fail or got == 1
        worst_skip = worst_skip or got == 2
        out("-" * 72)

    if not args.execute:
        out("DRY-RUN: nothing was written. Re-run with --execute to apply.")
    rc = _grade(worst_fail, worst_skip)
    out("exit " + str(rc))
    return rc


if __name__ == "__main__":
    sys.exit(main())
