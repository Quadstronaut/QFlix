#!/usr/bin/env python3
"""One-off (2026-08-20): evict DISC-CLASS movie files that Radarr imported past
its own quality profile, blocklist the release that produced each one, and
re-search for a playable replacement.

Runs ON the seedbox (it reads ~/secrets directly):

    python3 ~/scripts/ops/remediate-2026-08-20-iso.py            # dry-run plan
    python3 ~/scripts/ops/remediate-2026-08-20-iso.py --execute   # writes

Dry-run is the default and --execute is the only flag that mutates, for the
same reason qflix-reaper.py, qflix-torrent-janitor.py and
qflix-remux-regrab.py default to it: every write here deletes a member-visible
media file or adds a permanent blocklist row, and none of it is undone by
re-running the script. Every write is verified by RE-READING the app
afterwards -- the *arr API returns 200 for things that did not land, and the
whole reason this script exists is that Radarr reported a grab as one quality
and then stored a different one.

===========================================================================
THE DEFECT, round 2, measured live 2026-08-20 12:0x CEST
===========================================================================
Round 1 of this script was written against a 44.38 GiB .iso. That file is
GONE and the problem is not. On disk right now:

    ~/media/Movies/In the Mouth of Madness (1995)/
        In the Mouth of Madness (1995) BR-DISK.mkv
    42,341,133,540 bytes (39.43 GiB), st_nlink = 1, mtime 2026-08-20 11:03:14

Radarr main, movie id 441, tmdb 2654, quality profile 6 "HD 720p/1080p",
monitored=false. Its history (/api/v3/history/movie?movieId=441), read live:

    hid 1165  04:49:00Z  movieFileDeleted        q=Remux-1080p   (the regrab run)
    hid 1190  04:52:08Z  grabbed                 q=Bluray-1080p
    hid 1214  07:14:03Z  downloadFolderImported  q=BR-DISK
      1190 + 1214: downloadId 02b7b06b-fe3d-498c-aaa6-06867b72ef6e, NZBgeek,
      sourceTitle In.the.Mouth.of.Madness.1994.1080p.Blu-ray.CE.4K.
                  REMASTERED.DTS-HD.MA.5.1-NOGRP-Obfuscated

The release NAME says "1080p Blu-ray", so the grab decision graded it
Bluray-1080p and allowed it. The PAYLOAD was a full-disc image. On import
Radarr re-graded the actual bytes BR-DISK -- and imported them anyway,
because the import step does not re-check the quality profile.

Then Tdarr picked the disc image up two minutes after it landed and spent two
hours turning it into an .mkv:

    09:14:39 Tdarr_Server | oBWkbmn0a: File detected, adding to queue:
             .../In the Mouth of Madness (1995) BR-DISK.iso
    11:03:14 Tdarr_Server | FFprobe was unable to extract any data ...
             .../In the Mouth of Madness (1995) BR-DISK.mkv
    11:03:40 Tdarr_Server | File detected, adding to queue: ... BR-DISK.mkv

So the SECOND round of this problem wears an .mkv extension. An extension
check catches nothing now. That is precisely why the detector below does not
key on extension alone -- see WHAT COUNTS AS A TARGET.

===========================================================================
"ABOVE THE 25000 MiB CEILING" -- WHICH IS IT? ESTABLISHED, NOT GUESSED
===========================================================================
The brief asked: the 39.43 GiB file is above radarr's new 25000 MiB ceiling,
so was it grabbed before the ceiling landed, or is the ceiling not being
applied? The answer is BOTH HALVES OF THE FIRST OPTION, plus a third thing
that matters more than either.

  1. THE CEILING IS LIVE AND IT IS BEING APPLIED. Proven from radarr's own
     decision log rather than from the config read-back, because a stored
     setting and an enforced setting are different claims:

       09:32:11 MaximumSizeSpecification | Maximum size is not set.   (x23)
       11:57:27 MaximumSizeSpecification | Checking if release meets maximum
                size requirements. 72.5 GB
       11:57:27 MaximumSizeSpecification | 72.5 GB is too big, maximum size
                is 24.4 GB (Settings->Indexers->Maximum Size)
       11:57:27 MaximumSizeSpecification | 32.2 GB is too big, maximum size
                is 24.4 GB (Settings->Indexers->Maximum Size)

     GET /api/v3/config/indexer agrees: maximumSize 25000 on radarr, 42000 on
     radarr2. Two releases were rejected on size at 11:57 today. The ceiling
     works.

  2. THE GRAB PREDATES IT. The grab is hid 1190 at 04:52:08Z = 06:52 CEST.
     At 09:32 CEST radarr was still logging "Maximum size is not set", so the
     ceiling landed somewhere between 09:32:11 and 11:57:27 CEST -- roughly
     three hours AFTER the grab and about half an hour before this was
     written. Nothing was mis-applied. The disc simply got in first.

  3. AND THE 39.43 GiB FILE WAS NEVER GRABBED AT ALL. This is the part worth
     internalising. maximumSize is a GRAB-TIME specification: it scores a
     release's advertised size before the download starts. The .mkv on disk
     is not a release, it is Tdarr's local output. No indexer ever offered
     it, so no size specification was ever going to see it. A ceiling cannot
     police bytes that arrive by transcode.

     The release that DID have to pass a ceiling is the .iso, advertised at
     roughly 47.6 GB decimal. Against today's 24.4 GB it would now be
     rejected outright -- so of the four guards on this movie (quality
     profile, BR-DISK custom format at -10000, the size ceiling, and the
     blocklist this script writes) the size ceiling is the only one that
     would have stopped THIS release at grab time, because the other two
     grade off the parsed NAME and the name was a lie.

===========================================================================
HOW YOU BLOCKLIST AN ALREADY-IMPORTED RELEASE
===========================================================================
Not the way you blocklist a queued one. The two obvious calls are both wrong:

  * DELETE /api/v3/queue/{id}?blocklist=true -- queue only. This release left
    the queue hours ago; SABnzbd's history_limit is 0, so there is no queue
    row and no client-side history row to fail.
  * POST /api/v3/blocklist -- there is no create verb. Radarr's blocklist is
    not a table you append to over the API; it is a SIDE EFFECT of a download
    being failed.

That second claim is not taken on faith, it is visible in this instance's own
data. /api/v3/blocklist holds 44 rows; every recent one pairs 1:1 with a
`downloadFailed` history row on the same movie, at the same second:

    blocklist 104  2026-08-16T20:47:00Z  Oceans.Eight...Atmos-eXcommunicado
    history  1146  2026-08-16T20:47:00Z  downloadFailed, same sourceTitle
    blocklist 107  2026-08-20T07:33:01Z  Joker...DD.7.1.x264-playHD
    history  1218  2026-08-20T07:33:00Z  downloadFailed, same sourceTitle

Rows arrive when a download fails, and never otherwise. So the correct call
is the one behind the History view's "Mark as Failed" button:

    POST /api/v3/history/failed/{grabbedHistoryId}

MarkAsFailed resolves the history row's downloadId, finds every `grabbed` row
sharing it, and publishes DownloadFailedEvent; BlocklistService handles that
event and writes the row. It does NOT delete the imported file -- this script
deletes that itself, explicitly, so the delete is visible in the plan instead
of being an invisible side effect.

This script therefore targets the GRABBED history row (hid 1190 above), not
the imported one, and it finds that row by matching
`downloadFolderImported.data.fileId` to the movieFileId it is about to
delete. That is a per-FILE identity, so on a movie with several import cycles
(441 has two in 16 days) it blocklists the release that produced the file in
front of it and not merely the most recent grab.

skipRedownload=true IS SENT AND IS NOT TRUSTED. This box runs
config/downloadclient.autoRedownloadFailed = true, and the live evidence is
that the auto-search is FAST: history 1218 downloadFailed at 07:33:00Z, 1219
grabbed at 07:33:16Z -- sixteen seconds. If Radarr honours the query
parameter that race is suppressed; if it ignores it, a replacement is already
in flight before this script gets to its own search. Rather than assert which,
the script SETTLES for --settle seconds after the blocklist and then re-reads
both the queue and the history. If a replacement appeared it says so and does
not add a second search of its own. A replacement racing the delete is
harmless: it takes minutes to download and the delete lands in seconds, and
the blocklist is already in place so the racing grab cannot be the same disc.

PROOF OF THE BLOCKLIST, two reads. /api/v3/blocklist is PAGED and
instance-wide (44 rows), so proving a negative against it means walking every
page; /api/v3/blocklist/movie?movieId=N returns a plain unpaged list for one
movie and is the read that carries the assertion. The paged endpoint is then
walked as a cross-check so the run can print the actual blocklist ROW ID it
created -- "verified" that names a row id is worth more than "verified".

ORDER IS THE WHOLE POINT: blocklist -> delete file -> search. Search before
blocklist and the very next grab is the same disc again, at which point the
run has spent 40+ GiB of quota to arrive back where it started. That has now
happened to this title twice; it does not get a third turn.

===========================================================================
WHAT COUNTS AS A TARGET (enumerated, never hardcoded)
===========================================================================
Every movie WITH A FILE on radarr and radarr2 is walked. THREE independent
signals, because round 1 keyed too much on the extension and the second
instance of the same defect walked straight through it as an .mkv:

  1. QUALITY, as *arr reports it -- the load-bearing one. Any quality name in
     DISC_QUALITY_TOKENS: BR-DISK, Remux, Raw-HD (matched as a token,
     case-insensitively, so Bluray-1080p is untouched and a future
     "BR-DISK-1080p" is still caught). This is the leg that catches the .mkv,
     and it works for a slightly ridiculous reason worth writing down: this
     Radarr's naming format is

         {Movie Title} ({Release Year}) {Quality Full}

     so the quality is baked into the FILENAME, Tdarr's output is literally
     called "... BR-DISK.mkv", and a rescan re-parses BR-DISK straight back
     out of it. The grading survives the transcode.
  2. CONTAINER -- path ends .iso / .img / .bin / .nrg. A disc image that
     *arr has somehow graded as something benign is still not playable.
  3. LAYOUT -- a /BDMV/ or /VIDEO_TS/ PATH COMPONENT (an unpacked disc).
     Matched as a component, not a substring, so a movie honestly titled
     "Bdmv" cannot be swept up.

Any one signal is enough. They are deliberately redundant: signal 1 without 2
is today's .mkv, signal 2 without 1 is a disc image *arr mis-graded, and
signal 3 is the case where neither of the first two fires because the movie
"file" is a directory tree.

THE INTERLOCK, and why it splits the disc class in two. The three quality
tokens are all disc-derived, but they do not deserve the same treatment:

  * BR-DISK and Raw-HD are CONTAINER class -- raw disc payloads. These are
    NEVER skipped on profile grounds. An unplayable 40 GiB disc image is not
    a working file, so "you might get another one" is not a reason to keep
    this one.
  * Remux is QUALITY class -- it plays, just at a tier the library has
    decided against. qflix-remux-regrab.py refuses to delete a remux whose
    profile still ALLOWS remux, because the re-search just downloads another
    remux and the run has destroyed a working file for nothing. That
    reasoning is right and it is inherited here verbatim. Live today it skips
    radarr2 movie 1, "Cowboy Bebop: The Movie", 33.43 GiB Remux-1080p on
    radarr2 profile 4 "HD-1080p", which still allows Remux -- yesterday's cap
    ran on radarr MAIN only. Cap radarr2 profile 4 first if that file is
    actually unwanted; nothing here touches it.

Be honest about what the blocklist buys: it is PER RELEASE, so it stops this
exact disc coming back and nothing else. A different mislabelled disc from a
different release can still land tomorrow -- that is what the size ceiling is
for, and it is now live.

===========================================================================
TDARR REWRITES FILES UNDER RADARR -- THE STALE-PATH STEP
===========================================================================
This is not over-engineering, it is the live state of the target. Radarr has
not noticed the transcode: /api/v3/movie/441 still reports movieFileId 534,
path "... BR-DISK.iso", size 47,649,253,376. That path does not exist. The
39.43 GiB .mkv beside it is invisible to Radarr.

That is a way to lose 39 GiB. DELETE /moviefile/534 tells Radarr to remove a
path that is already gone; Radarr drops the row and reports success, and the
.mkv becomes an orphan with no *arr record pointing at it -- the exact "ghost
item" shape the reaper's UNRESOLVED class documents. A post-delete existence
check of Radarr's recorded path would also PASS, because that path was
already gone, so the run would report space it never reclaimed.

So: a target whose recorded path is not on disk is never deleted blind. The
run issues RescanMovie first, waits for the command to settle, RE-DERIVES the
file id / path / size / link count / quality from Radarr, and re-classifies.
Only then does the repair proceed -- against the file that is actually there.

In dry-run the rescan cannot be run, so the plan is a PROJECTION: the folder
is listed, each sibling is classified by the same three signals (with the
quality read out of the filename, which is sound here only because of the
naming format quoted above), and the full blocklist/delete/search plan is
printed against the projected file. It is still graded 2 (unknown), because
what Radarr's own re-parse will say is not knowable from outside Radarr. The
projection is there so the operator reads a real plan instead of "run it and
find out" -- not so the run can claim green.

Note also that the rewrite did NOT solve the problem it looks like it solved.
Plex has indexed the .mkv (ratingKey 8855, container=mkv, bitrate 34618 kbps,
part 14743). 34.6 Mbps to a client that negotiates 1927 kbps is the identical
failure the remux cap was built for -- the disc image became a
playable-in-principle file that the affected member still cannot watch.

===========================================================================
MONITORING IS DELIBERATELY NOT TOUCHED
===========================================================================
Movie 441 is monitored=false, and the tempting move -- flip it so the search
works -- is both unnecessary and a policy write this script has no mandate
for (see remediate-2026-08-20.py task B: re-monitoring an EMPTY record is a
download order, not hygiene).

Unnecessary, because a user-invoked MoviesSearch bypasses
MonitoredMovieSpecification on this Radarr. That is measured, not assumed: of
the grabs Radarr made on 2026-08-20, most are on movies that are unmonitored
right now, movie 441's own 04:52Z grab among them -- all produced by
qflix-remux-regrab's MoviesSearch. An unmonitored movie is only skipped by
the RSS/cutoff passes.

The consequence is reported loudly instead of papered over: after the file is
deleted, an unmonitored movie that finds NO release is not retried by RSS
either. So --search-wait polls for a grab and the run refuses to grade itself
green if none arrived; the movie is named in the summary as needing an
operator search.

===========================================================================
SPACE: MEASURED, NOT INFERRED
===========================================================================
Radarr main runs copyUsingHardlinks=true with an empty recycle bin, so the
tempting sentence is "the space only comes back when qflix-torrent-janitor
reaps the seeding twin". That sentence is a CONFIG READING, and it was wrong
on the 2026-08-20 regrab run (corrected in 0009066): all 23 targets were
st_nlink == 1 and the quota fell the moment the deletes landed.

So this script stats every target. st_nlink == 1 -> the bytes come back at
delete time. st_nlink >= 2 -> a twin is still seeding and the space waits on
qflix-torrent-janitor at ratio >= 2.0. None -> unstat-able, reported in its
own bucket and never counted as freed. _nlink() and _bytes_verdict() below
are the same shape qflix-remux-regrab.py uses, on purpose. It is then CHECKED
after the fact: each deleted path is re-stat'ed, and only paths that are
actually gone are reported as reclaimed.

Measured today: the .mkv is st_nlink = 1 (usenet import, then rewritten in
place by Tdarr -- no torrent twin ever existed), so its 39.43 GiB returns the
moment the delete lands.

Unit note: _gb() divides by 1024**3, so the numbers are GiB and are labelled
GiB here. qflix-remux-regrab.py prints the identical quantity as "GB".

===========================================================================
NEVER PRINTED
===========================================================================
Radarr history `data` blobs carry data.downloadUrl, which on this box embeds
the NZBgeek API key in a query string. This script prints only id, date,
eventType, quality and sourceTitle from a history row, and never the data
blob. Do not add a debug dump of it.

===========================================================================
EXIT CODES (remediate-2026-08-20.py contract)
    0  every selected target is green (already clean, or repaired and
       verified end to end including a grab landing)
    1  a write was attempted and failed, failed its read-back, or its outcome
       is UNKNOWN because the transport died mid-request
    2  a precondition could not be established, a target was skipped, or the
       repair completed but no replacement grab could be confirmed. Nothing
       claimed green that was not proven green.

    1 OUTRANKS 2, which is not what max() would do: a benign skip on one
    instance must never mask a failed write on the other.
===========================================================================
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
SECRETS = HOME / "secrets"
TIMEOUT = 60

ARRS = ("radarr", "radarr2")

# --------------------------------------------------------------------------
# THE DETECTOR. Three independent signals; see WHAT COUNTS AS A TARGET.
# --------------------------------------------------------------------------
# Signal 1, the load-bearing one: what *arr itself says the bytes are. These
# are matched as TOKENS against the quality name, not as substrings, so
# "Bluray-1080p" is untouched while "BR-DISK", "Remux-1080p", "Raw-HD" and any
# future "BR-DISK-2160p" all hit.
#
# The split is the interlock, not cosmetic:
#   container = raw disc payload, deleted regardless of what the profile says
#   quality   = plays fine, wrong tier, and is SKIPPED while its own profile
#               still allows it (qflix-remux-regrab's rule, inherited)
DISC_QUALITY_TOKENS = {
    "br-disk": "container",
    "brdisk": "container",
    "raw-hd": "container",
    "remux": "quality",
}
# Signal 2: containers no Plex client plays as a movie file. Path suffix.
DEAD_CONTAINERS = (".iso", ".img", ".bin", ".nrg")
# Signal 3: unpacked disc structures, matched as a PATH COMPONENT so a movie
# honestly titled "Bdmv" cannot be swept up by a loose substring match.
DISC_DIR_MARKERS = ("bdmv", "video_ts")

DEFAULT_MAX_ITEMS = 5
DEFAULT_SEARCH_WAIT = 120
# Seconds to let Radarr's own autoRedownloadFailed search show itself after a
# blocklist. Measured on this box: downloadFailed 07:33:00Z -> grabbed
# 07:33:16Z, i.e. 16s. 25 gives that a margin without stalling the run.
DEFAULT_SETTLE = 25

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNKNOWN = 2


class Fail(Exception):
    """Precondition could not be established -> 2 for this target."""


class WriteFail(Exception):
    """A write left the socket and its outcome is UNKNOWN -> 1.

    Deliberately NOT a Fail. A transport error raised out of a POST/DELETE is
    indistinguishable, from here, from "Radarr applied it and then the socket
    died", so it can never be graded as the clean did-not-write skip that Fail
    means.
    """


def _grade(fail, skip):
    """Collapse two flags into an exit code. 1 outranks 2 -- not max()."""
    if fail:
        return EXIT_FAIL
    return EXIT_UNKNOWN if skip else EXIT_OK


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
        # url.split("?")[0]: query strings on this box's *arr calls are
        # harmless, but keeping the habit means a future call carrying a key
        # in a query string cannot leak it into a log line.
        raise Fail(method + " " + url.split("?")[0] + " -> " + repr(exc))


def get_json(url, headers):
    code, raw = http(url, headers=headers)
    if code != 200:
        raise Fail("GET " + url.split("?")[0] + " -> HTTP " + str(code))
    try:
        return json.loads(raw)
    except ValueError:
        raise Fail("GET " + url.split("?")[0] + " -> non-JSON body")


def write(url, *, headers, method, body=None, ctype=None):
    """Every mutating call goes through here.

    Its only job is to re-label a transport error so it cannot be caught by an
    `except Fail` arm and reported as a clean precondition skip. See WriteFail.
    """
    try:
        return http(url, headers=headers, method=method, body=body, ctype=ctype)
    except Fail as exc:
        raise WriteFail(str(exc))


# ---------------------------------------------------------------------------
# Endpoints. urlbase secrets carry NO leading slash on this box; the strip is
# belt and braces so a hand-edited file with one does not produce "//".
# ---------------------------------------------------------------------------
def arr_base(slug):
    return ("http://127.0.0.1:" + secret(slug + ".port") + "/"
            + secret(slug + ".urlbase").strip("/") + "/api/v3")


def arr_headers(slug):
    return {"X-Api-Key": secret(slug + ".key"), "Accept": "application/json"}


# ---------------------------------------------------------------------------
# Maintenance window (qflix-remux-regrab parity). Deleting media files and
# firing searches are box ops and the operator directive is no box ops inside
# the Monday window. The guard is applied to --execute ONLY: a dry-run reads
# and prints, which is safe at any hour and is the thing an operator most
# wants to be able to do while the window is running.
# ---------------------------------------------------------------------------
def in_maintenance_window(now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    if now.weekday() == 0 and 11 <= now.hour < 15:
        return True
    try:
        lock = Path(os.environ.get("MANITOBA_STATE_DIR",
                                   str(HOME / ".opt" / "maint"))) / "lock"
        if lock.exists():
            pid = int(lock.read_text(encoding="utf-8").splitlines()[0].strip())
            if os.name == "posix":
                try:
                    os.kill(pid, 0)
                    return True
                except ProcessLookupError:
                    return False
                except PermissionError:
                    return True
    except Exception as exc:
        sys.stderr.write("window lock check failed (best-effort, continuing): "
                         + repr(exc) + "\n")
    return False


# ---------------------------------------------------------------------------
# Measurement helpers -- same shape as qflix-remux-regrab.py on purpose.
# ---------------------------------------------------------------------------
def _nlink(path):
    """st_nlink for a movie file path, or None if it cannot be stat'ed.

    Never raises: a target whose link count is unknown belongs in the unknown
    bucket, not silently counted as either freed or pending.
    """
    if not path:
        return None
    try:
        return os.stat(path).st_nlink
    except OSError:
        return None


def _gb(nbytes):
    """Bytes -> GiB (1024**3). Labelled GiB everywhere this is printed."""
    try:
        return round(int(nbytes) / (1024 ** 3), 2)
    except (TypeError, ValueError):
        return 0.0


def _bytes_verdict(rows):
    """Reclaimed-now vs unlinked-pending-reap, from MEASURED link counts.

    Reasoning from copyUsingHardlinks alone gets this backwards -- that flag
    describes how files ARRIVE, not whether a given file still has a seeding
    twin today. See the SPACE block in the header.
    """
    now = round(sum(r["size_gb"] for r in rows if r.get("nlink") == 1), 2)
    pend = round(sum(r["size_gb"] for r in rows if (r.get("nlink") or 0) >= 2), 2)
    unk = round(sum(r["size_gb"] for r in rows if r.get("nlink") is None), 2)
    parts = []
    if now:
        parts.append("freed " + str(now) + " GiB now (no seeding twin)")
    if pend:
        parts.append("unlinked " + str(pend) + " GiB pending torrent-janitor "
                     "reap at ratio>=2.0 - both copies on disk until then")
    if unk:
        parts.append("removed " + str(unk) + " GiB of unstat-able files "
                     "(link count unknown, assume pending)")
    return "; ".join(parts) if parts else "0 GiB"


def _reclaimed_verdict(rows):
    """Post-delete AUDIT: re-stat every path we deleted and report only what
    is actually gone. _bytes_verdict is the prediction; this is the check."""
    if not rows:
        return "nothing deleted"
    gone = [r for r in rows if not os.path.exists(r["path"] or "")]
    still = [r for r in rows if r["path"] and os.path.exists(r["path"])]
    freed = round(sum(r["size_gb"] for r in gone if r.get("nlink") == 1), 2)
    unlinked = round(sum(r["size_gb"] for r in gone
                         if (r.get("nlink") or 0) >= 2), 2)
    msg = ("verified gone: " + str(len(gone)) + "/" + str(len(rows))
           + " path(s); reclaimed " + str(freed) + " GiB immediately")
    if unlinked:
        msg += ("; " + str(unlinked) + " GiB unlinked but still held by a "
                "seeding twin (waits on qflix-torrent-janitor)")
    if still:
        msg += ("; STILL ON DISK: "
                + ", ".join(os.path.basename(r["path"]) for r in still))
    return msg


# ---------------------------------------------------------------------------
# Classification -- three signals, any one is enough.
# ---------------------------------------------------------------------------
def _quality_token(text):
    """(token, class) for the first disc token appearing in `text`, else None.

    Token-matched with a non-alphanumeric boundary rather than `in`, so
    "Bluray-1080p" does not trip on anything and a hypothetical future
    "BR-DISK-2160p" still does. re.escape because the tokens contain "-".
    """
    low = str(text or "").lower()
    for token, klass in DISC_QUALITY_TOKENS.items():
        if re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", low):
            return token, klass
    return None


def classify(path, quality_name):
    """(class, why) for a movie file, or (None, "") if it is fine.

    "container" = a raw disc payload; never spared on profile grounds.
    "quality"   = plays, but at a tier the library has decided against, and is
                  subject to the still-allowed-by-profile interlock.

    ORDER MATTERS. The quality signal is checked FIRST because it is the one
    that survives a transcode: the .mkv Tdarr left behind on 2026-08-20 has a
    perfectly ordinary extension and is only catchable by what Radarr graded
    it. Round 1 of this script led with the extension check and that is
    exactly how the second instance of this defect walked through it.
    """
    low = str(path or "").replace("\\", "/").lower()
    q = str(quality_name or "")

    hit = _quality_token(q)
    if hit:
        token, klass = hit
        why = ("graded " + q
               + (" (raw disc payload)" if klass == "container"
                  else " (playable, wrong tier)"))
        return klass, why

    if low.endswith(DEAD_CONTAINERS):
        # A disc image *arr somehow graded as something benign. Still not
        # playable, so still container class.
        return "container", ("non-playable container "
                             + os.path.splitext(low)[1] + " (graded "
                             + (q or "?") + ", which did not flag it)")

    parts = low.split("/")
    for mark in DISC_DIR_MARKERS:
        if mark in parts:
            return "container", "sits under a " + mark.upper() + "/ disc structure"
    return None, ""


def allowed_quality_names(items, acc=None):
    """Every ALLOWED leaf quality name in a profile, walked RECURSIVELY.

    Radarr's items[] is a nested tree: a group carries its own allowed flag
    plus a child items[] list. A non-recursive read misses every grouped
    quality, which is what made an earlier audit blame the wrong profile
    entirely. A group is traversed, never counted -- its flag is a container
    flag, not a grabbable quality.
    """
    if acc is None:
        acc = set()
    for item in items or []:
        if isinstance(item.get("items"), list) and item.get("items"):
            allowed_quality_names(item.get("items"), acc)
        else:
            if not item.get("allowed"):
                continue
            q = item.get("quality") or {}
            name = q.get("name") if isinstance(q, dict) else None
            if name:
                acc.add(str(name))
    return acc


def _file_quality_name(movie_file):
    q = ((movie_file or {}).get("quality") or {}).get("quality") or {}
    return str(q.get("name") or "")


def _folder_entries(path):
    """(name, bytes, nlink) for everything in a movie folder, sorted by size.

    Used only when Radarr's recorded path is missing -- it is how the operator
    sees at a glance that 39 GiB did not evaporate, it was renamed.
    """
    rows = []
    try:
        folder = os.path.dirname(path or "")
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rows.append((name, st.st_size, st.st_nlink))
    except OSError:
        pass
    rows.sort(key=lambda r: -r[1])
    return rows


def project_stale(path):
    """Best-effort PROJECTION of what a rescan will find, for the dry-run.

    Returns (name, bytes, nlink, klass, why) for the largest offending entry
    in the folder, or None.

    The quality here is read out of the FILENAME. That is only sound because
    this Radarr's naming format is "{Movie Title} ({Release Year}) {Quality
    Full}", so the file Radarr writes carries its own grading -- which is also
    why Tdarr's output is called "... BR-DISK.mkv" and why the rescan will
    re-parse BR-DISK back out of it. It is a projection, never an assertion:
    the caller still grades the target UNKNOWN in dry-run and lets Radarr's
    own re-parse be the authority under --execute.
    """
    best = None
    for name, size, nlink in _folder_entries(path):
        stem = os.path.splitext(name)[0]
        klass, why = classify(name, stem)
        if not klass or (best is not None and size <= best[1]):
            continue
        # classify() was handed the whole stem as the "quality name", so its
        # own `why` would echo the entire filename back. Re-word it around the
        # TOKEN that actually matched, which is the readable form and the one
        # an operator can compare against Radarr's grading after the rescan.
        hit = _quality_token(stem)
        if hit:
            why = ("filename carries the quality token " + hit[0].upper()
                   + " (this Radarr names files {Movie Title} ({Release Year}) "
                     "{Quality Full}, so the grading is in the name)")
        best = (name, size, nlink, klass,
                why + " [PROJECTED from the filename, not from Radarr]")
    return best


# ---------------------------------------------------------------------------
# History -> the release that produced THIS file.
# ---------------------------------------------------------------------------
def _history_quality(row):
    return str((((row or {}).get("quality") or {}).get("quality")
                or {}).get("name") or "")


def find_grab_for_file(history, movie_file_id, file_quality=None):
    """(grabbed_history_row, imported_history_row, provenance) for a file.

    PRIMARY MATCH is downloadFolderImported.data.fileId, NOT "the newest
    grab": movie 441 has two import cycles in 16 days, so "newest" would
    blocklist whichever release ran last rather than the one that produced the
    file about to be deleted.

    FALLBACK, and it exists because of the Tdarr rewrite in the header: a file
    that was transcoded in place after import gets a NEW movieFileId on
    rescan, and no import row will ever claim it. The origin release is still
    the newest import, so that row is used -- but only when its graded quality
    matches the file's, which is the cheap check that stops the fallback
    blocklisting a release that has nothing to do with what is on disk. The
    caller is told, and prints, which of the two matched.

    On movie 441 today both routes land on the same answer, which is the
    useful property: before the rescan the primary match works (import hid
    1214 claims fileId 534), after it the fallback does (newest import is
    still 1214, graded BR-DISK, and the rescanned file grades BR-DISK too).
    Either way the release blocklisted is grab hid 1190.

    data.fileId comes back as a STRING in the API payload, hence str(). It is
    the only field read out of `data`, and nothing from `data` is ever printed
    -- see the NEVER PRINTED block in the header.
    """
    want = str(movie_file_id)
    imported = None
    provenance = "fileId"
    for row in history or []:
        if row.get("eventType") != "downloadFolderImported":
            continue
        if str((row.get("data") or {}).get("fileId")) == want:
            imported = row
            break
    if imported is None:
        imports = [r for r in history or []
                   if r.get("eventType") == "downloadFolderImported"]
        imports.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
        if not imports:
            raise Fail("no downloadFolderImported history row at all -- this "
                       "file did not come from a grab Radarr remembers, so "
                       "there is nothing to blocklist and it is left alone")
        newest = imports[0]
        if file_quality and _history_quality(newest) != str(file_quality):
            raise Fail("no import row claims fileId " + want
                       + ", and the newest import graded "
                       + (_history_quality(newest) or "?") + " while the file "
                       "on disk grades " + str(file_quality)
                       + " -- refusing to guess which release to blocklist")
        imported = newest
        provenance = "newest-import (file was rewritten in place after import)"
    dlid = imported.get("downloadId")
    if not dlid:
        raise Fail("import row " + str(imported.get("id"))
                   + " has no downloadId -- MarkAsFailed has nothing to fail")
    grabs = [r for r in history or []
             if r.get("eventType") == "grabbed" and r.get("downloadId") == dlid]
    if not grabs:
        raise Fail("no grabbed history row shares downloadId " + str(dlid)[:12]
                   + " -- the grab record was pruned, nothing to mark failed")
    grabs.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return grabs[0], imported, provenance


# ---------------------------------------------------------------------------
# Blocklist reads. Two of them, for two different jobs.
# ---------------------------------------------------------------------------
def blocklisted_titles(base, hdr, movie_id):
    """sourceTitles already blocklisted for one movie -- the ASSERTION read.

    /api/v3/blocklist/movie?movieId=N returns a plain unpaged list for one
    movie, which is what proving a negative needs. The instance-wide
    /api/v3/blocklist is paged (44 rows today) and proving absence there means
    walking every page.
    """
    rows = get_json(base + "/blocklist/movie?movieId=" + str(movie_id), hdr)
    if not isinstance(rows, list):
        raise Fail("blocklist/movie returned " + repr(rows)[:80] + ", not a list")
    return set(str(r.get("sourceTitle") or "") for r in rows)


def blocklist_row_id(base, hdr, movie_id, source_title, max_pages=20):
    """Row id in the INSTANCE-WIDE paged blocklist -- the CROSS-CHECK read.

    Purely so the run can print the id of the row it created. "verified, row
    108" is a materially better audit line than "verified", and it is the read
    that literally answers "did /api/v3/blocklist take it". Returns None
    rather than raising: a cross-check that cannot be completed must not fail
    a write the assertion read already confirmed.
    """
    for page in range(1, max_pages + 1):
        try:
            j = get_json(base + "/blocklist?page=" + str(page)
                         + "&pageSize=100&sortKey=date&sortDirection=descending",
                         hdr)
        except Fail:
            return None
        recs = j.get("records") if isinstance(j, dict) else None
        if not recs:
            return None
        for r in recs:
            if (r.get("movieId") == movie_id
                    and str(r.get("sourceTitle") or "") == source_title):
                return r.get("id")
        if len(recs) < 100:
            return None
    return None


def wait_command(base, hdr, cmd_id, timeout=180):
    """Poll /command/{id} until it leaves the running states.

    Returns the last status string seen, or None if it never settled. A
    command that is still 'started' when the clock runs out is reported as
    exactly that rather than assumed finished -- the caller then re-reads the
    thing the command was supposed to change and decides from the data.
    """
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        try:
            status = str(get_json(base + "/command/" + str(cmd_id), hdr)
                         .get("status") or "")
        except Fail:
            status = None          # transient read failure is not a verdict
        if status in ("completed", "failed", "aborted", "cancelled"):
            return status
        time.sleep(3)
    return status


def queue_rows_for(base, hdr, movie_id):
    """Queue records for one movie.

    Paged and filtered here rather than by query parameter: this API silently
    IGNORES filters it does not implement instead of erroring -- /api/v3/
    history?movieId=N returns the WHOLE instance history and looks like a
    successful filter, which is exactly how a reader gets a confident wrong
    answer out of it.
    """
    j = get_json(base + "/queue?page=1&pageSize=200&includeMovie=false", hdr)
    recs = j.get("records") if isinstance(j, dict) else j
    return [r for r in (recs or []) if r.get("movieId") == movie_id]


# ---------------------------------------------------------------------------
# Enumeration.
# ---------------------------------------------------------------------------
def enumerate_targets(slug):
    """Every offending movie file on one instance, with its verdict.

    Rows carry both the classification and the interlock outcome so the plan
    can show WHY something is skipped; nothing is silently dropped.
    """
    base = arr_base(slug)
    hdr = arr_headers(slug)
    profiles = get_json(base + "/qualityprofile", hdr)
    if not isinstance(profiles, list) or not profiles:
        # With zero profiles known we cannot tell a capped profile from an
        # uncapped one, and acting on that basis would produce a confident,
        # wrong instruction. Refuse the whole instance instead.
        raise Fail(slug + ": /qualityprofile returned " + repr(profiles)[:80]
                   + " -- cannot tell a capped profile from an uncapped one")
    allowed = {p.get("id"): allowed_quality_names(p.get("items") or [])
               for p in profiles if isinstance(p, dict)}
    movies = get_json(base + "/movie", hdr)
    if not isinstance(movies, list):
        raise Fail(slug + ": /movie returned " + repr(movies)[:80] + ", not a list")

    rows = []
    for m in movies:
        mf = m.get("movieFile")
        if not m.get("hasFile") or not mf:
            continue
        path = mf.get("path") or ""
        qname = _file_quality_name(mf)
        klass, why = classify(path, qname)
        if not klass:
            continue
        pid = m.get("qualityProfileId")
        prof_allowed = allowed.get(pid)
        row = {
            "arr": slug,
            "movie_id": m.get("id"),
            "movie_file_id": mf.get("id"),
            "tmdb_id": m.get("tmdbId"),
            "title": m.get("title"),
            "year": m.get("year"),
            "monitored": bool(m.get("monitored")),
            "quality": qname,
            "quality_profile_id": pid,
            "path": path,
            "size_gb": _gb(mf.get("size")),
            # MEASURED here, re-checked after the delete. Never inferred from
            # copyUsingHardlinks -- see the SPACE block in the header.
            "nlink": _nlink(path),
            # Radarr's row and the filesystem disagree whenever Tdarr rewrites
            # a file in place. That is a rescan, not a delete -- see the TDARR
            # block in the header and step 0 of repair().
            "path_exists": bool(path) and os.path.exists(path),
            "klass": klass,
            "why": why,
            "skip_reason": None,
        }
        if prof_allowed is None:
            row["skip_reason"] = ("quality profile " + str(pid) + " is not in "
                                  "Radarr's profile list -- reassign this movie "
                                  "to a profile that exists, then re-run")
        elif klass == "quality" and qname in prof_allowed:
            # The qflix-remux-regrab interlock, and it applies HERE ONLY. A
            # container-class row is never spared on this ground: an
            # unplayable disc payload is not a working file, so "you might get
            # another one" is not a reason to keep this one.
            row["skip_reason"] = ("profile " + str(pid) + " still ALLOWS "
                                  + qname + " -- deleting this PLAYABLE file "
                                  "would just re-grab the same tier; cap the "
                                  "profile first")
        rows.append(row)
    # Largest first: the point of the run is reclaiming space, and --max-items
    # should take the rows that matter most, deterministically.
    rows.sort(key=lambda r: (r["arr"], -(r["size_gb"] or 0), r["movie_id"] or 0))
    return rows


def print_rows(rows, header):
    if not rows:
        return
    out(header)
    for r in rows:
        out("    " + r["arr"] + " movie=" + str(r["movie_id"]).rjust(4)
            + " file=" + str(r["movie_file_id"]).rjust(4)
            + " " + str(r["size_gb"]).rjust(7) + " GiB"
            + " nlink=" + str(r["nlink"])
            + " prof=" + str(r["quality_profile_id"])
            + " " + r["klass"].ljust(9)
            + " " + str(r["title"])[:38] + " (" + str(r["year"]) + ")")
        out("        " + r["why"] + "; monitored=" + str(r["monitored"])
            + "; " + os.path.basename(r["path"]))
        if not r.get("path_exists"):
            out("        STALE <- Radarr's recorded path is NOT on disk. "
                "RescanMovie runs first; deleting blind would orphan whatever "
                "replaced it (see the TDARR block in the header).")
            for name, size, nlink in _folder_entries(r["path"]):
                out("           on disk instead: " + name + "  "
                    + str(_gb(size)) + " GiB  nlink=" + str(nlink))
        if r["skip_reason"]:
            out("        SKIP <- " + r["skip_reason"])


# ---------------------------------------------------------------------------
# Repair, one target.
# ---------------------------------------------------------------------------
def _print_plan(row, grab, imported, provenance, already):
    src = str(grab.get("sourceTitle") or "")
    out("    file    " + os.path.basename(row["path"]) + "  "
        + str(row["size_gb"]) + " GiB  nlink=" + str(row["nlink"]))
    out("    import  hid=" + str(imported.get("id")) + " "
        + str(imported.get("date")) + " graded " + _history_quality(imported))
    out("    grab    hid=" + str(grab.get("id")) + " " + str(grab.get("date"))
        + " graded " + _history_quality(grab))
    out("    release " + src[:100])
    out("    matched by " + provenance)
    need = src not in already
    out("    plan    1. " + ("POST /history/failed/" + str(grab.get("id"))
                             + "?skipRedownload=true  (the ONLY way to add a "
                               "blocklist row for an already-imported release)"
                             if need else
                             "blocklist already carries this release, no write"))
    out("            2. DELETE /moviefile/" + str(row["movie_file_id"])
        + "   (the FILE, never the movie record)")
    out("            3. POST /command MoviesSearch movieIds=["
        + str(row["movie_id"]) + "]   -- unless step 1 already auto-queued one")
    if not row["monitored"]:
        out("    note    monitored=false. A user-invoked MoviesSearch still "
            "grabs (measured), but if it finds nothing RSS will NOT retry -- "
            "this run refuses to report green in that case.")
    return need


def repair(row, execute, search_wait, settle):
    """blocklist -> delete file -> search, each verified by re-reading.

    Returns (rc, deleted_row_or_None). rc follows the module contract: 1 for a
    failed or unknown-outcome write, 2 for a precondition or an unconfirmed
    outcome, 0 only when the whole chain is proven.
    """
    slug = row["arr"]
    base = arr_base(slug)
    hdr = arr_headers(slug)
    tag = (slug + " movie=" + str(row["movie_id"]) + " "
           + str(row["title"])[:40] + " (" + str(row["year"]) + ")")

    # -- IDENTITY / DRIFT GUARD -------------------------------------------
    # Enumeration and repair are separate reads, and Radarr imports things in
    # between. A stale movie_file_id could name a DIFFERENT, good file by the
    # time we get here, so re-read and refuse on any drift rather than delete
    # what we did not plan to delete.
    movie = get_json(base + "/movie/" + str(row["movie_id"]), hdr)
    if not movie.get("hasFile"):
        out(tag + ": SKIP the movie no longer has a file (something else "
                  "removed it between enumeration and now)")
        return EXIT_UNKNOWN, None
    if movie.get("movieFileId") != row["movie_file_id"]:
        out(tag + ": SKIP movieFileId moved " + str(row["movie_file_id"])
            + " -> " + str(movie.get("movieFileId"))
            + " between enumeration and now; a replacement may already have "
              "imported. Re-run the dry-run and look again.")
        return EXIT_UNKNOWN, None
    if movie.get("tmdbId") != row["tmdb_id"]:
        out(tag + ": SKIP tmdbId changed " + str(row["tmdb_id"]) + " -> "
            + str(movie.get("tmdbId")) + "; radarr ids are not stable across "
            "a remove-and-re-add")
        return EXIT_UNKNOWN, None

    history = get_json(base + "/history/movie?movieId=" + str(row["movie_id"]),
                       hdr)

    # -- 0. STALE PATH -> RESCAN FIRST ------------------------------------
    # Radarr's row and the filesystem disagree whenever something rewrites a
    # file in place under it (Tdarr did exactly that to this movie at 11:03,
    # see the header). Deleting on a stale row orphans the real file and
    # reports space that was never reclaimed, so the row is refreshed first
    # and everything downstream is re-derived from the refreshed one.
    if not row.get("path_exists"):
        out(tag + ": STALE Radarr path is not on disk")
        out("    radarr  " + row["path"] + "  " + str(row["size_gb"]) + " GiB")
        for name, size, nlink in _folder_entries(row["path"]):
            out("    disk    " + name + "  " + str(_gb(size))
                + " GiB  nlink=" + str(nlink))
        proj = project_stale(row["path"])
        if proj:
            out("    project " + proj[0] + "  " + str(_gb(proj[1]))
                + " GiB  nlink=" + str(proj[2]) + "  -> " + proj[3]
                + " class: " + proj[4])
        else:
            out("    project nothing in the folder classifies as offending. "
                "The rescan may well leave this movie clean.")

        if not execute:
            # Print the FULL projected plan rather than "run it and find out".
            # The blocklist target resolves the same either way on this movie
            # (see find_grab_for_file), so the operator can read the actual
            # history ids before authorising the write.
            out("    plan    0. POST /command RescanMovie movieId="
                + str(row["movie_id"]) + "  -- Radarr re-parses whatever is "
                  "actually in the folder and issues a fresh movieFileId")
            try:
                grab, imported, provenance = find_grab_for_file(
                    history, row["movie_file_id"], row["quality"])
                already = blocklisted_titles(base, hdr, row["movie_id"])
                out("    then, against the rescanned file:")
                _print_plan(row, grab, imported, provenance, already)
                out("    NOTE after the rescan the file id changes, so the "
                    "grab will be matched by the newest-import fallback "
                    "instead of by fileId. Both routes resolve to grab hid="
                    + str(grab.get("id")) + " on this movie -- if they ever "
                    "disagree the script raises rather than guesses.")
            except Fail as exc:
                out("    plan    could not resolve the originating release "
                    "yet: " + str(exc))
            out("    verdict UNKNOWN (2) in dry-run BY DESIGN. What Radarr's "
                "own re-parse will grade the file is not knowable from "
                "outside Radarr; the projection above is read off the "
                "filename and is not an assertion.")
            return EXIT_UNKNOWN, None

        code, raw = write(base + "/command", headers=hdr, method="POST",
                          body=json.dumps({"name": "RescanMovie",
                                           "movieId": row["movie_id"]}).encode(),
                          ctype="application/json")
        if code not in (200, 201, 202):
            out("    FAIL POST /command RescanMovie -> HTTP " + str(code) + " "
                + raw[:200].decode("utf-8", "replace"))
            return EXIT_FAIL, None
        try:
            cmd_id = json.loads(raw).get("id")
        except ValueError:
            cmd_id = None
        status = wait_command(base, hdr, cmd_id) if cmd_id else None
        out("    OK   RescanMovie id=" + str(cmd_id) + " status=" + str(status))
        # Trust the movie record, not the command status: a "completed" scan
        # that changed nothing and a scan still running both look the same
        # from here, and only the re-read tells us which.
        movie = get_json(base + "/movie/" + str(row["movie_id"]), hdr)
        if not movie.get("hasFile"):
            out("    SKIP after the rescan Radarr reports no file at all. The "
                "path really is gone and nothing replaced it -- this movie "
                "needs a search, not a delete. Nothing was written beyond the "
                "rescan.")
            return EXIT_UNKNOWN, None
        mf = movie.get("movieFile") or {}
        new_path = mf.get("path") or ""
        row = dict(row)
        row.update({
            "movie_file_id": mf.get("id"),
            "path": new_path,
            "quality": _file_quality_name(mf),
            "size_gb": _gb(mf.get("size")),
            "nlink": _nlink(new_path),
            "path_exists": bool(new_path) and os.path.exists(new_path),
        })
        klass, why = classify(row["path"], row["quality"])
        out("    after   file=" + str(row["movie_file_id"]) + " "
            + str(row["size_gb"]) + " GiB nlink=" + str(row["nlink"])
            + " graded " + str(row["quality"]) + "  "
            + os.path.basename(row["path"]))
        if not klass:
            out("    OK   the rescan found a file that is NOT offending ("
                + str(row["quality"]) + "). Nothing to delete; leaving it "
                "alone. Re-run the dry-run to confirm the library is clean.")
            return EXIT_OK, None
        if not row["path_exists"]:
            out("    FAIL the rescan left Radarr pointing at a path that is "
                "still not on disk (" + row["path"] + "). Refusing to delete "
                "a row whose file cannot be found -- that orphans bytes.")
            return EXIT_FAIL, None
        row["klass"], row["why"] = klass, why
        out("    still offending: " + why)
        # The rescan changed the file id, so history must be re-read against
        # the NEW id (it will land on the newest-import fallback).
        history = get_json(base + "/history/movie?movieId="
                           + str(row["movie_id"]), hdr)

    grab, imported, provenance = find_grab_for_file(
        history, row["movie_file_id"], row["quality"])
    src = str(grab.get("sourceTitle") or "")
    out(tag + ":")
    already = blocklisted_titles(base, hdr, row["movie_id"])
    need_blocklist = _print_plan(row, grab, imported, provenance, already)

    if not execute:
        return EXIT_OK, None

    # -- 1. BLOCKLIST, before anything is deleted or searched --------------
    if need_blocklist:
        url = (base + "/history/failed/" + str(grab.get("id"))
               + "?" + urllib.parse.urlencode({"skipRedownload": "true"}))
        code, raw = write(url, headers=hdr, method="POST",
                          body=b"", ctype="application/json")
        if code not in (200, 201, 202, 204):
            out("    FAIL blocklist -> HTTP " + str(code) + " "
                + raw[:200].decode("utf-8", "replace"))
            return EXIT_FAIL, None
        # MarkAsFailed answers with an empty object and echoes nothing about
        # the blocklist row it did or did not create. Re-read -- twice.
        try:
            after = blocklisted_titles(base, hdr, row["movie_id"])
        except Fail as exc:
            out("    FAIL blocklisted but could not read back -- " + str(exc))
            return EXIT_FAIL, None
        if src not in after:
            out("    FAIL blocklist read-back: " + str(len(after))
                + " row(s) for this movie and none matches the release. "
                  "NOT deleting the file -- an unblocklisted release would be "
                  "re-grabbed by the search below.")
            return EXIT_FAIL, None
        bid = blocklist_row_id(base, hdr, row["movie_id"], src)
        out("    OK   blocklisted, verified via /blocklist/movie ("
            + str(len(after)) + " row(s) now)"
            + ("; /blocklist row id=" + str(bid) if bid is not None
               else "; instance-wide cross-check inconclusive (paged read "
                    "did not find the row -- the movie-scoped read above is "
                    "the assertion and it passed)"))
    else:
        out("    OK   blocklist already carries this release")

    # -- 1b. autoRedownloadFailed race check -------------------------------
    # skipRedownload=true is SUPPOSED to suppress Radarr's own replacement
    # search (this box runs autoRedownloadFailed=true, and measured live it
    # fires ~16s after a failure). Verified, not trusted: settle, then re-read
    # BOTH the queue and the history. If a replacement is already in flight,
    # adding our own search on top would double-grab.
    known_grabs = set(r.get("id") for r in history
                      if r.get("eventType") == "grabbed")
    # Read the queue UNCONDITIONALLY first. The settle loop below only runs
    # when this run wrote a blocklist row, but a replacement can already be in
    # flight for reasons that have nothing to do with this run -- an earlier
    # aborted attempt, an operator search, a re-run over an already-blocklisted
    # release. Skipping the check on the need_blocklist=False path would issue
    # a second MoviesSearch on top of a live download.
    try:
        queued = queue_rows_for(base, hdr, row["movie_id"])
    except Fail as exc:
        out("    WARN could not read the queue (" + str(exc)
            + "); proceeding as if nothing is in flight")
        queued = []
    auto_grab = None
    if need_blocklist and not queued and settle > 0:
        out("    ...    settling " + str(settle) + "s to see whether "
            "autoRedownloadFailed fires despite skipRedownload=true")
        deadline = time.time() + settle
        while time.time() < deadline:
            time.sleep(5)
            try:
                queued = queue_rows_for(base, hdr, row["movie_id"])
            except Fail:
                queued = []
            try:
                for r in get_json(base + "/history/movie?movieId="
                                  + str(row["movie_id"]), hdr):
                    if (r.get("eventType") == "grabbed"
                            and r.get("id") not in known_grabs):
                        auto_grab = r
                        break
            except Fail:
                pass
            if queued or auto_grab:
                break
    if queued or auto_grab:
        detail = ", ".join(str(q.get("title"))[:50] for q in queued) or \
            str((auto_grab or {}).get("sourceTitle"))[:50]
        out("    NOTE Radarr auto-queued a replacement despite "
            "skipRedownload=true: " + detail
            + " -- this run will NOT add a second search. The blocklist is "
              "already in place, so this cannot be the same disc.")

    # -- 2. DELETE THE FILE (never the movie record) -----------------------
    code, raw = write(base + "/moviefile/" + str(row["movie_file_id"]),
                      headers=hdr, method="DELETE")
    if code not in (200, 202, 204):
        out("    FAIL DELETE /moviefile -> HTTP " + str(code) + " "
            + raw[:200].decode("utf-8", "replace"))
        return EXIT_FAIL, None
    try:
        after_movie = get_json(base + "/movie/" + str(row["movie_id"]), hdr)
    except Fail as exc:
        out("    FAIL deleted but could not read the movie back -- " + str(exc))
        return EXIT_FAIL, None
    if after_movie.get("hasFile"):
        out("    FAIL read-back still hasFile=true (movieFileId "
            + str(after_movie.get("movieFileId")) + ")")
        return EXIT_FAIL, None
    # Radarr dropping its row and the bytes leaving the disk are two different
    # events; a permissions problem produces the first without the second.
    if os.path.exists(row["path"]):
        out("    FAIL Radarr dropped the record but the path is still on "
            "disk: " + row["path"])
        return EXIT_FAIL, None
    out("    OK   file deleted, verified hasFile=false and the path is gone")

    # -- 3. SEARCH, only now -----------------------------------------------
    if queued or auto_grab:
        out("    SKIP MoviesSearch: a replacement is already in flight (above)")
        return EXIT_OK, row
    body = json.dumps({"name": "MoviesSearch",
                       "movieIds": [row["movie_id"]]}).encode()
    code, raw = write(base + "/command", headers=hdr, method="POST",
                      body=body, ctype="application/json")
    if code not in (200, 201, 202):
        out("    FAIL POST /command MoviesSearch -> HTTP " + str(code) + " "
            + raw[:200].decode("utf-8", "replace")
            + "  (the file is ALREADY DELETED -- re-run, or search by hand)")
        return EXIT_FAIL, row
    try:
        cmd_id = json.loads(raw).get("id")
    except ValueError:
        cmd_id = None
    if cmd_id is None:
        out("    FAIL MoviesSearch accepted but returned no command id, so "
            "the search cannot be verified. File is deleted; search by hand.")
        return EXIT_FAIL, row
    out("    OK   MoviesSearch queued, command id " + str(cmd_id))

    if search_wait <= 0:
        out("    NOTE --search-wait 0: no grab confirmation attempted. Check "
            "Radarr history before calling this movie repaired.")
        return EXIT_UNKNOWN, row

    # -- 4. CONFIRM A GRAB LANDED ------------------------------------------
    # The point of the run is a playable file, and a search that finds nothing
    # on an unmonitored movie is a dead end RSS never revisits. So poll for a
    # NEW grabbed history row rather than assuming the command implies one.
    deadline = time.time() + search_wait
    grabbed_new = None
    while time.time() < deadline:
        time.sleep(5)
        try:
            hist2 = get_json(base + "/history/movie?movieId="
                             + str(row["movie_id"]), hdr)
        except Fail:
            continue          # a transient read failure is not a verdict
        for r in hist2:
            if r.get("eventType") == "grabbed" and r.get("id") not in known_grabs:
                grabbed_new = r
                break
        if grabbed_new:
            break
    if grabbed_new is None:
        out("    UNCONFIRMED no new grab within " + str(search_wait)
            + "s. The file is gone and the movie has none. If this movie is "
              "unmonitored, RSS will NOT retry it -- search it by hand in "
              "Radarr. Re-running this script will NOT help: a file-less "
              "movie is not a target.")
        return EXIT_UNKNOWN, row
    new_src = str(grabbed_new.get("sourceTitle") or "")
    new_q = _history_quality(grabbed_new)
    out("    OK   replacement grabbed: " + new_q + " " + new_src[:80])
    if new_src == src:
        out("    FAIL the replacement is the SAME release that was just "
            "blocklisted -- the blocklist did not take effect")
        return EXIT_FAIL, row
    # The grab is graded off the NAME, which is the whole defect. Say so.
    bad_class, bad_why = classify(new_src, new_q)
    if bad_class:
        out("    UNCONFIRMED the replacement already grades as " + bad_why
            + ". Watch the import; it may need the same treatment.")
        return EXIT_UNKNOWN, row
    out("    NOTE the grab was graded from the release NAME. Confirm the "
        "IMPORT quality once it lands -- that is where this defect hides. "
        "The size ceiling (24.4 GB on radarr) is now the guard that catches "
        "a lying name, and it is live.")
    return EXIT_OK, row


# ---------------------------------------------------------------------------
# Report-only: things this script deliberately does not own.
# ---------------------------------------------------------------------------
REPORT = """REPORT ONLY -- this script writes to Radarr and to nothing else.
All of the below was read live on 2026-08-20 and NOTHING was written.

  PLEX -- YES, there is a stale item, and it needs no manual step.
    section 4 "QFlix - Movies", 41 items. ratingKey 8855 "In the Mouth of
    Madness" (1994) exists, Part id 14743, file
      .../In the Mouth of Madness (1995) BR-DISK.mkv
    size 42,341,133,540, container=mkv, bitrate=34618 kbps, width=1920.
    So Plex is pointing straight at the file this script deletes, and at
    34.6 Mbps it is the same unplayable-on-a-1.9-Mbps-client shape the remux
    cap exists to prevent, just wearing an .mkv extension.

    (For contrast: at 10:47 the section held 37 items and this title was
    absent -- Plex does not index a bare .iso as a movie. Tdarr's transcode is
    what made the disc visible to members at all.)

    No manual Plex step: radarr's "Plex Media Server" notification has
    onDownload / onUpgrade / onMovieFileDelete / onMovieDelete ALL true
    (verified live), so Radarr pokes Plex on the delete AND on the replacement
    import, and the server has autoEmptyTrash=1, so the stale item is removed
    on that scan rather than lingering as a broken entry. The scheduled scan
    is the backstop. Only intervene if 8855 is still present, or the
    replacement still missing, an hour after the import lands.

  BAZARR -- one stale row, zero subtitle rows, nothing to purge.
    bazarr-1 is the movies instance (a CONTAINER; reachable only through the
    nginx map, its own 6767 is dead). Read with `sqlite3 -readonly`, which
    does see the 4 MB -wal:

      table_movies            radarrId=441  path=".../BR-DISK.iso"   1 row
      table_movies_subtitles  radarrId=441                           0 rows

    Two things follow. (1) There is nothing to purge -- Bazarr could not read
    subtitle tracks out of a disc image, so it never created any. (2) The row
    it does hold is ALREADY STALE in exactly the same way Radarr's is: it
    still names the .iso, which has not existed since 11:03. Bazarr's own
    Radarr sync rewrites that path when the replacement imports. Note
    table_movies has UNIQUE(path), so a wedged sync surfaces as an integrity
    error in the Bazarr log rather than as silence. Do not hand-edit the row
    -- and either way it is not this script's file.

  THE SIZE CEILING IS THE REAL GUARD, AND IT IS LIVE. Established from
    radarr's decision log, not from the config read-back: at 09:32 CEST it
    still logged "Maximum size is not set"; at 11:57 CEST it rejected a
    72.5 GB and a 32.2 GB release against "maximum size is 24.4 GB". The
    04:52Z grab that produced this disc predates that by three hours. Of the
    four guards on movie 441 -- profile 6 (BR-DISK not allowed), the BR-DISK
    custom format at -10000, the 25000 MiB ceiling, and the blocklist row this
    script writes -- only the ceiling grades off SIZE rather than off the
    parsed release NAME, and the name was the thing that lied. It is the one
    that would have stopped this release at grab time.

  RADARR2 REMUX. radarr2 profile 4 "HD-1080p" still allows Remux, so the
    33.43 GiB "Cowboy Bebop: The Movie" remux (radarr2 movie 1) is skipped by
    the interlock rather than deleted. radarr2's own ceiling is 42000 MiB, so
    it would not stop a 40 GiB disc either. Cap profile 4 first if that file
    is unwanted; nothing here touches it.

  TDARR TOOK THE DISC IMAGE AS WORK. Tdarr queued the .iso at 09:14:39, two
    minutes after Radarr imported it, and at 11:03 replaced it with a 39.43
    GiB .mkv -- roughly two hours of node time spent transcoding a payload
    nothing should have accepted, and the output still grades BR-DISK because
    the quality is baked into the Radarr-generated filename ({Movie Title}
    ({Release Year}) {Quality Full}, verified live). Two follow-ups belong to
    whoever owns the Tdarr library config, not to this script: (1) the movie
    library will happily hand Tdarr a disc image, so a container filter on the
    Tdarr side would stop the wasted work at the source; (2) Tdarr rewriting a
    file in place leaves Radarr's row stale until something rescans, which is
    a general condition on this box and not specific to this title -- nothing
    currently watches for *arr rows whose recorded path is not on disk."""


# ---------------------------------------------------------------------------
def _positive_int(value):
    """argparse type for --max-items. Rejects < 1 instead of trusting a slice.

    A negative cap does not shrink a blast radius, it INVERTS the slice and
    widens it: targets[:-1] keeps everything but the last row. That is a
    proven failure (qflix-remux-regrab, 2026-08-19 review: --max-items -1
    issued 22 deletes against 23 targets), so it is a parse error here.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be an integer >= 1, got "
                                         + repr(value))
    if n < 1:
        raise argparse.ArgumentTypeError(
            "must be >= 1: " + str(n) + " slices from the END of the target "
            "list and WIDENS the blast radius instead of capping it")
    return n


def _non_negative_int(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be an integer >= 0, got "
                                         + repr(value))
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0: " + str(n))
    return n


def build_parser():
    ap = argparse.ArgumentParser(
        description=("2026-08-20: evict disc-class movie files Radarr imported "
                     "past its own profile, blocklist the release that "
                     "produced each one, re-search. Dry-run by default."))
    ap.add_argument("--execute", action="store_true",
                    help="actually blocklist, delete and search. Without it "
                         "nothing is written and the plan is printed.")
    ap.add_argument("--arr", default=",".join(ARRS),
                    help="comma-separated instances to walk (default "
                         + ",".join(ARRS) + ")")
    ap.add_argument("--max-items", type=_positive_int, default=DEFAULT_MAX_ITEMS,
                    help="per-run cap, must be >= 1; the LARGEST N are "
                         "repaired and the rest are DEFERRED to the next run. "
                         "Default " + str(DEFAULT_MAX_ITEMS) + ".")
    ap.add_argument("--skip", default="",
                    help="comma-separated slug:movieId to leave alone, "
                         "e.g. --skip=radarr2:1")
    ap.add_argument("--search-wait", type=_non_negative_int,
                    default=DEFAULT_SEARCH_WAIT,
                    help="seconds to poll for a replacement grab after the "
                         "search (0 disables, and the run then cannot report "
                         "green). Default " + str(DEFAULT_SEARCH_WAIT) + ".")
    ap.add_argument("--settle", type=_non_negative_int, default=DEFAULT_SETTLE,
                    help="seconds to wait after a blocklist for Radarr's own "
                         "autoRedownloadFailed search to show itself before "
                         "deciding whether to add one. Default "
                         + str(DEFAULT_SETTLE) + ".")
    ap.add_argument("--ignore-window", action="store_true",
                    help="allow --execute inside the Monday maintenance "
                         "window (operator directive: no box ops in it).")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    slugs = [s.strip() for s in args.arr.split(",") if s.strip()]
    unknown = [s for s in slugs if s not in ARRS]
    if unknown:
        print("--arr: unknown instance(s): " + ",".join(unknown),
              file=sys.stderr)
        return EXIT_UNKNOWN
    skip_keys = set(s.strip() for s in args.skip.split(",") if s.strip())

    out("remediate-2026-08-20-iso  mode="
        + ("EXECUTE" if args.execute else "DRY-RUN")
        + "  arr=" + ",".join(slugs) + "  max-items=" + str(args.max_items)
        + "  search-wait=" + str(args.search_wait) + "s"
        + "  settle=" + str(args.settle) + "s")
    out("-" * 72)

    if args.execute and in_maintenance_window() and not args.ignore_window:
        out("in the Monday maintenance window - refusing to write (operator "
            "directive: no box ops). Dry-run is always allowed; "
            "--ignore-window overrides.")
        return EXIT_OK

    fail = skip = False
    candidates = []
    for slug in slugs:
        try:
            rows = enumerate_targets(slug)
        except Fail as exc:
            out(slug + ": SKIP precondition -- " + str(exc))
            skip = True
            continue
        out(slug + ": " + str(len(rows)) + " offending file(s)")
        candidates.extend(rows)
    out("-" * 72)

    actionable = []
    for r in candidates:
        key = r["arr"] + ":" + str(r["movie_id"])
        if key in skip_keys:
            r["skip_reason"] = "requested by --skip=" + key
        if r["skip_reason"]:
            continue
        actionable.append(r)

    skipped = [r for r in candidates if r["skip_reason"]]
    print_rows(skipped, "SKIPPED (" + str(len(skipped)) + "):")
    if skipped:
        skip = True

    deferred = actionable[args.max_items:]
    actionable = actionable[:args.max_items]
    verb = "WOULD REPAIR" if not args.execute else "REPAIRING"
    print_rows(actionable, verb + " (" + str(len(actionable)) + "):")
    print_rows(deferred, "DEFERRED by --max-items (" + str(len(deferred)) + "):")
    if deferred:
        skip = True
    out("-" * 72)

    if not actionable:
        out("nothing actionable.")
    else:
        out("predicted space: " + _bytes_verdict(actionable))
        stale = [r for r in actionable if not r.get("path_exists")]
        if stale:
            # Sizes for a stale row are Radarr's stale sizes, so the number
            # above is fiction for those rows. Say so instead of printing a
            # confident total. The MEASURED figure at the end is the real one.
            out("  ...but " + str(len(stale)) + " of those row(s) point at a "
                "path that is not on disk, so their sizes are Radarr's stale "
                "numbers. Real sizes are re-derived after the rescan and the "
                "MEASURED line at the end of an --execute run is the truth.")
            # Radarr's stale size lands every stale row in the "unstat-able"
            # bucket, which reads as if the bytes are unaccounted for. They are
            # not -- they are sitting in the folder under a different name, and
            # they ARE stat-able. Show that measured figure beside the fiction.
            proj_rows = []
            for r in stale:
                p = project_stale(r["path"])
                if p:
                    proj_rows.append({"size_gb": _gb(p[1]), "nlink": p[2]})
                    out("  projected real target: " + p[0] + "  "
                        + str(_gb(p[1])) + " GiB  nlink=" + str(p[2]))
            if proj_rows:
                out("  projected space if the rescan agrees: "
                    + _bytes_verdict(proj_rows))
    out("-" * 72)

    deleted = []
    for r in actionable:
        try:
            rc, done = repair(r, args.execute, args.search_wait, args.settle)
        except WriteFail as exc:
            out("    FAIL write outcome UNKNOWN (transport died mid-request; "
                "re-run the dry-run to see what landed) -- " + str(exc))
            rc, done = EXIT_FAIL, None
        except Fail as exc:
            out("    SKIP precondition -- " + str(exc))
            rc, done = EXIT_UNKNOWN, None
        fail = fail or rc == EXIT_FAIL
        skip = skip or rc == EXIT_UNKNOWN
        if done:
            deleted.append(done)
        out("-" * 72)

    if args.execute and deleted:
        out("space, MEASURED after the deletes: " + _reclaimed_verdict(deleted))
        out("-" * 72)
    out(REPORT)
    out("-" * 72)
    if not args.execute:
        out("DRY-RUN: nothing was written. Re-run with --execute to apply.")
    rc = _grade(fail, skip)
    out("exit " + str(rc))
    return rc


if __name__ == "__main__":
    sys.exit(main())
