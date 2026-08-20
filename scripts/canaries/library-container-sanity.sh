#!/usr/bin/env bash
# Library-container-sanity canary: assert every payload in the member-visible
# media libraries is a container a Plex client can actually open.
#
# WHY THIS EXISTS - the 2026-08-20 BR-DISK import
# On 2026-08-20 at 09:14 CEST a 47,649,253,376-byte file landed in the Movies
# library:
#
#   media/Movies/In the Mouth of Madness (1995)/
#       In the Mouth of Madness (1995) BR-DISK.iso
#
# It is a full Blu-ray disc image. No Plex client can play it: Plex will not
# mount an ISO, will not walk its UDF filesystem, and will not find a playlist
# inside it. The member sees the title in the library and nothing happens when
# they press play. Not "it buffers", not "it looks bad" - nothing.
#
# The mechanism is worth writing down because a quality profile CANNOT prevent
# it. Radarr grabbed the release on its NAME - "In.the.Mouth.of.Madness.1994.
# 1080p.Blu-ray.CE.4K.REMASTERED..." parses as Bluray-1080p, which every profile
# on this box allows. The payload was a disc image. At import time (07:14:03Z)
# Radarr re-graded the file to BR-DISK and imported it anyway: the import step
# does not re-check the quality profile against the re-graded quality. BR-DISK
# is allowed on NO profile here - id6 "HD 720p/1080p" permits only HDTV-720p,
# WEBDL-720p, WEBRip-720p, Bluray-720p, HDTV-1080p, WEBDL-1080p, WEBRip-1080p,
# Bluray-1080p, and id7/8/9 are the same shape - and it landed regardless. So
# tightening profiles (which is what scripts/configure/58-remux-cap-enforce.py
# did the day before, capping Remux) is necessary for grabs and structurally
# unable to stop this class. The grab passed on the name; the file was something
# else.
#
# WHY NOTHING NOTICED
# hardlink-integrity.sh saw the same family of object arrive and treated it as a
# TORRENT-side question: its VIDEO_EXTS list gained .m2ts on 2026-08-19 precisely
# so a BDMV rip in the qBit pool would stop reading as a vanished filesystem. It
# asks "did the import hardlink", never "is the imported thing playable". Every
# other monitor is orthogonal by construction: the Plex app monitor answers
# /identity, plex-transcoder.sh answers three transcode HTTP handlers,
# plex-playback.sh transcodes the WORST-CASE item it can select - and an ISO is
# not selectable as a worst case, because Plex never built a media/part row for
# it. The reaper counts bytes, the quota canary counts bytes, and 47.6 GB of
# unplayable bytes counts exactly the same as 47.6 GB of playable ones.
#
# So the fleet had 33 monitors and not one of them asserted the single most
# basic property of a media library: that the files in it are media files. That
# is the gap this closes.
#
# WHAT IT ASSERTS - two independent legs, deliberately not one
#   LEG 1 (filesystem, primary). Walk the member-visible library roots and
#   classify every file. A payload must carry a playable container extension; a
#   disc-image extension is a finding on sight; a BDMV / VIDEO_TS / AUDIO_TS
#   DIRECTORY is a finding on sight.
#
#   LEG 2 (*arr grading, corroborating). Ask Radarr, Radarr2, Sonarr and Sonarr2
#   what quality they recorded for each file they own, and fire on BR-DISK or
#   Raw-HD.
#
# Leg 2 is not redundant with leg 1, and this is the whole reason it is here.
# Radarr imports a BDMV FOLDER by renaming its largest .m2ts into the movie
# directory: the result is "Title (1995).m2ts", a playable extension, with no
# BDMV directory left anywhere in the library. Leg 1 is silent on that file and
# correct to be - .m2ts genuinely plays. Leg 2 is the only thing that can say
# "the *arr itself already knows this came off a disc". Conversely leg 1 is the
# only leg that can see a file no *arr owns at all: a hand-copied ISO, a leftover
# from a manual import, a VIDEO_TS tree an unpack dropped in place. Neither leg
# subsumes the other, so both run and either one can red.
#
# THE WHITELIST, AND WHY IT IS THREE LISTS AND NOT ONE
# Naively "not a video extension" is a finding. That is wrong: a healthy library
# is FULL of non-video files, and a canary that reds on them gets muted in a day.
# Measured live on 2026-08-20 across Movies / TV Shows / Anime / Anime Movies /
# Welcome: 423 .mkv, 8 .mp4, 47 .srt+.nfo, 6 .jpg, 3 .txt, 35 .plexmatch, 2
# .stignore, 1 .plexignore, 1 .iso. Every one of those except the ISO is either
# a payload or a legitimate sidecar.
#
#   PLAYABLE_EXTS  - containers Plex opens. .m2ts and .ts are IN this list for
#                    the same reason hardlink-integrity.sh lists them: they are
#                    real, playable, and a broadcast capture or a single-stream
#                    Blu-ray remux legitimately arrives that way. A disc RIP is
#                    caught by the directory rule below, not by banning .m2ts.
#   DISC_IMAGE_EXTS- shapes that are a whole disc in one file, or the index
#                    files that only ever accompany one. .iso/.img/.bin/.nrg/
#                    .mdf/.mds are images; .cue is their index; .vob/.ifo/.bup
#                    are the three files a DVD VIDEO_TS rip is made of. Plex can
#                    technically play a lone .vob, but a .vob in a library means
#                    a DVD rip arrived un-remuxed, which is the same incident
#                    wearing a different extension. All are findings on sight,
#                    regardless of size - a 4 KB .ifo is proof of a disc rip
#                    even though it is far too small to trip the size rule.
#   SIDECAR_EXTS   - subtitles (.srt .sub .idx .ass .ssa .vtt .smi .sup - Bazarr
#                    writes the first, the rest arrive with releases), metadata
#                    (.nfo .xml .json - *arr and Kodi), artwork (.jpg .jpeg .png
#                    .webp .tbn - Plex local assets and Kometa), and human text
#                    (.txt .log .md - release notes and scene logs). Ignored,
#                    counted, never a finding.
#
# Plus one rule that is not an extension: a file whose NAME starts with a dot
# (.plexmatch, .plexignore, .stignore) is a sidecar. splitext() reports NO
# extension for those, so an extension-only classifier would push all 38 of them
# into the unknown bucket. They are metadata by convention on every one of these
# tools, and they are tiny.
#
# THE ANTI-ENUMERATION-GAP RULE, AND ITS SIZE GATE
# Three enumerated lists cannot be complete - that is the defect that put .m2ts
# into hardlink-integrity.sh eight hours too late and turned a healthy box into a
# six-hour storm. So an extension in NONE of the three lists is not silently
# ignored here. It is judged by SIZE, because size is the property that actually
# separates the two things an unknown extension can be:
#
#   under MIN_PAYLOAD_BYTES (default 64 MiB) - it cannot be a feature or an
#       episode. It is a sidecar shape nobody wrote down yet (.sfv, .par2, a
#       stray .m3u). Counted as sidecar_unknown, NAMED in the pass line so the
#       list can be extended deliberately, and NOT a finding.
#   at or over MIN_PAYLOAD_BYTES - a payload-sized object that is not a
#       container anyone listed. That is the incident shape, whatever the
#       extension happens to be, including no extension at all. It is a finding.
#
# This is what makes the canary survive the NEXT unplayable shape rather than
# only the one that already happened.
#
# THE ONE PAYLOAD-SIZED SHAPE THAT IS NOT A FINDING: IN-FLIGHT STAGING
# Size alone would red on a remux that is halfway through being written, and
# this repo writes those on purpose. audio-disposition-janitor.py remuxes in
# place through a temp beside the target, and its own docstring
# (TmpVanishedError, lines 280-284) records Tdarr replaceOriginalFile staging
# renaming that temp to a "*.tmp" name mid-write on 2026-08-08. A canary that
# fires on it would page for a healthy nightly job that clears within minutes,
# every night, until somebody muted the monitor.
#
# So a trailing ".tmp" whose STEM still carries a playable extension -
# "Movie (1995).mkv.tmp" - is transient by construction: something is mid-write
# on a container, not a container that is unplayable. It is counted as a small
# unknown under the key ".mkv.tmp" so it is still NAMED in the pass line (a
# staging file that never clears is visible to an operator reading the green
# line) and is never a finding. A BARE ".tmp" with no playable stem is NOT
# excused: nothing here writes one, and payload-sized bytes with no evidence of
# what container they belong to are exactly the anti-enumeration-gap case.
#
# THE VACUITY TRAP, WHICH THIS REPO KEEPS WALKING INTO
# hardlink-integrity.sh has been retired and rewritten twice over exactly one
# mistake: a denominator small enough to be meaningless, wired straight to an
# alarm. Its current design times its own blindness instead, and that is the
# pattern copied here verbatim.
#
# Zero findings can mean two completely different things:
#   (a) every file was examined and every file was playable  - VERIFIED GOOD
#   (b) nothing was examined                                 - ASSERTED NOTHING
# and (b) is reachable here in ordinary operation: the reaper empties libraries
# (it emptied Anime entirely once, see memory reaper-maxpct-cap-disabled), a
# mount can vanish, and os.walk() SWALLOWS PermissionError by default, so an
# unreadable root yields zero files and is indistinguishable from an empty one
# unless an onerror handler is passed - which is why one is passed.
#
# So this canary tracks whether it ASSERTED, not whether it liked what it saw. A
# run that did not assert exits 0 as INCONCLUSIVE and stamps the start of a
# blind streak in ~/.opt/maint/library-container-sanity/vacuity.json. Past
# MAX_VACUOUS_DAYS the blindness itself is the alert (STAGE=container-blind).
# Any run that really examined something clears the clock - on the failing path
# too, because a firing canary is the opposite of a blind one.
#
# THE CLOCK IS PER LEG, AND THE FIRST DRAFT GOT THIS WRONG (2026-08-20 review)
# The first version asked `payloads > 0 or arr_graded > 0`. One OR, and the
# filesystem leg could be satisfied entirely by the *arr leg. Proved live before
# ship: point MEDIA_ROOT at a directory that does not exist, leave the four
# *arrs up, and the canary reported
#     PASS ... scanned=0 payloads=0 roots=0/5 arr_graded=432
# - a green filesystem check with zero filesystem examined. That is the exact
# trap this file spends thirty lines warning about, reintroduced one line below
# the warning, and it is the third time this repo has shipped it.
#
# The clock now asks BOTH legs independently and goes vacuous unless BOTH
# asserted:
#   fs_asserted  - every named root was present, os.walk raised nothing, and at
#                  least one file was classified (payloads + known sidecars +
#                  small unknowns). A MISSING root counts against it: a root
#                  that is not there is unexamined, not empty, and "the library
#                  moved" must never read as "the library is clean".
#   arr_asserted - at least one *arr told us the quality of at least one file.
#                  It is the ONLY leg that can see a BDMV folder import renamed
#                  to a perfectly ordinary .m2ts, so a run with all four *arrs
#                  down has genuinely not checked that class.
# Both legs unasserted, or either one, means inconclusive - named, timed, and
# red at MAX_VACUOUS_DAYS. LCS_SKIP_ARR waives only the arr half, because that
# switch exists to run the filesystem leg alone in a test where no *arr is
# reachable by construction; requiring an arr assertion there would make every
# offline run vacuous and prove nothing. It is exported by the prelude only and
# is never set in production.
#
# EVIDENCE BEATS VACUITY. Findings are evaluated BEFORE the vacuity check. If
# three of five roots are unreadable and the two that are readable hold an ISO,
# that is a red, not an inconclusive - a missing sibling root does not make a
# real ISO less real.
#
# WHICH ROOTS, AND WHY NOT ALL OF THEM
# Movies, TV Shows, Anime, Anime Movies, Welcome. The first four are the member
# libraries; Welcome is the single-video library un-entitled members see
# (section 145397557), and it is included precisely because it holds ONE file
# that nobody looks at - if that became an ISO the first person to notice would
# be a prospective member. media/Music and media/Playlists are deliberately
# excluded: .flac and .m3u are correct there and would be findings here.
#
# WHY DAILY, AND ITS OWN MONITOR
# Daily at 04:30 UTC. This fault is per-title, permanent until an operator acts,
# and does not cascade - one movie does not play, and it will still not play in
# an hour. Detecting it within 24 h costs at most one member one title for one
# day. Running a whole-library walk plus ~45 *arr calls hourly buys nothing,
# because a file does not become more unplayable while you watch it. The cost
# measured live on 2026-08-20: 39 Sonarr calls in 0.5 s, ~490 library files.
# The schedule token daily-0430 is reused rather than invented so that
# lib/cli.py _CANARY_INTERVAL_MIN already knows this cadence and `status --json`
# does not mislabel a healthy 24 h gap as stale.
# Its own module, timer and monitor per the operator design law (memory
# qflix-compartmentalize-for-migration): folding this into hardlink-integrity.sh
# would couple a 30-minute inode check to a daily library walk and leave the
# operator one monitor meaning two unrelated things.
#
# MAINTENANCE WINDOW
# Suppressed during the Monday 11:00-15:00 UTC window, by the same two OR-ed
# legs plex-playback.sh and dash-asset-integrity.sh use: the UTC wall clock, and
# a window lock whose owning pid is still ALIVE (a leaked lock whose owner is
# gone must not mute a canary forever). Leg 2 reads four *arr APIs that are
# restarted on purpose in that window. Reported as a named SKIP, never a silent
# pass.
#
# Stage labels (stderr -> Kuma msg=). When several fire at once the reported
# stage is the most specific, in this order:
#   container-disc-image    a disc-image / disc-index extension is in a library
#                           (.iso .img .bin .nrg .mdf .mds .cue .vob .ifo .bup).
#                           THE 2026-08-20 label.
#   container-disc-dir      a BDMV / VIDEO_TS / AUDIO_TS directory is in a
#                           library - a disc structure was imported whole. The
#                           payload inside may well be .m2ts, which the
#                           extension leg passes and must pass.
#   arr-disc-quality        an *arr recorded BR-DISK or Raw-HD for a file it
#                           owns. Fires even when the filename looks perfect,
#                           which is the folder-import case leg 1 cannot see.
#                           Tagged /missing when the recorded path is no longer
#                           on disk - a stale disc grading survives the file
#                           being replaced (seen live hours after the
#                           2026-08-20 incident), and the remedy is an *arr
#                           rescan rather than a delete.
#   container-unknown-payload
#                           a file at or above MIN_PAYLOAD_BYTES whose extension
#                           is in none of the three lists (including no
#                           extension). The anti-enumeration-gap leg.
#   container-blind         nothing has been examined for MAX_VACUOUS_DAYS -
#                           empty libraries, unreadable roots, or a media root
#                           that is not there. The guard stopped guarding.
#
# THE 200-CHAR MESSAGE BUDGET, MEASURED RATHER THAN ASSUMED
# lib/cli.py:609 stores `msg = stderr.strip()[:200]` and that string is what
# Kuma shows and what the operator reads on their phone. It is a hard cut, not
# an ellipsis, so whatever is written last is simply not delivered. Measured on
# the live one-finding STAGE line as first drafted: 233 characters - the
# scanned=/arr_graded= summary fell off the end entirely, and MAX_NAMED=3 was
# budgeting for two paths that could never arrive.
#
# Three changes keep the important half inside the cut:
#   * summary is written BEFORE paths=. Counts are what tell an operator whether
#     this is one bad file or a collapsed library; a truncated path list still
#     names the first offender, a truncated summary names nothing.
#   * walk_errors= and arr_unreachable= appear only when NON-ZERO. On a healthy
#     box that is 32 characters of "=0" that displaced a real path.
#   * MAX_NAMED defaults to 2 and each path is cut to 45 characters (marked with
#     a trailing ~ so a truncated path is never mistaken for a real one).
# Live arithmetic after the change: stage+counts 57 + summary 61 + " paths=" 7
# = 125, leaving ~75 for the first path and its [instance/QUALITY] tag.
#
# DEPLOYING THIS INTO A KNOWN-RED STATE
# As of 2026-08-20 this canary is RED ON ARRIVAL and correctly so: Radarr still
# holds a BR-DISK movieFile for In the Mouth of Madness (1995) - the .iso was
# replaced at 11:03 by a 42,341,133,540-byte .mkv that is STILL graded BR-DISK,
# which is leg 2 firing exactly as designed on a real 40 Mbps payload that the
# low-bandwidth client this whole effort exists for cannot play. The predicate
# is NOT to be weakened to make the first run green.
# The dependency is on the remediation landing, not on this file: another change
# in this same round replaces that file and clears the Radarr grading. Until it
# does, 240-maintenance-install.sh installs the unit but will NOT enable the
# timer - it gates the first enable behind one clean manual run, so the fleet
# does not gain a monitor that is born red and gets muted in a week. See the
# gate at the library-container-sanity enable in that installer for the exact
# two-command sequence.
#
# Exits:
#   0 - every payload is a playable container, or a named SKIP, or a named
#       INCONCLUSIVE inside the blind budget
#   2 - any stage above
#
# Env overrides (they exist so the branches are reachable in a test, not so the
# thresholds can be tuned by guess):
#   QFLIX_CANARY_CONTAINER_MEDIA_ROOT        default $(realpath ~)/media. $HOME is
#                                            a symlink on this box, so it is
#                                            resolved - see memory
#                                            box-find-home-symlink-and-docker-blindspot
#   QFLIX_CANARY_CONTAINER_ROOTS             pipe-separated root names, default
#                                            "Movies|TV Shows|Anime|Anime Movies|Welcome"
#   QFLIX_CANARY_CONTAINER_MIN_PAYLOAD_BYTES default 67108864 (64 MiB)
#   QFLIX_CANARY_CONTAINER_DISC_QUALITIES    pipe-separated *arr quality names,
#                                            default "BR-DISK|Raw-HD"
#   QFLIX_CANARY_CONTAINER_MAX_VACUOUS_DAYS  default 7
#   QFLIX_CANARY_CONTAINER_STATE_DIR         default ~/.opt/maint/library-container-sanity
#   QFLIX_CANARY_CONTAINER_MAX_NAMED         default 2 offending paths in msg=,
#                                            each cut to 45 chars - see the
#                                            message-budget section above
#   QFLIX_CANARY_CONTAINER_ARR_TIMEOUT       per-call seconds, default 20
#   QFLIX_CANARY_CONTAINER_SKIP_ARR          test-only: 1 = filesystem leg only
#   QFLIX_CANARY_CONTAINER_FORCE_WINDOW      1 = force in-window, 0 = force out
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

MEDIA_ROOT=${QFLIX_CANARY_CONTAINER_MEDIA_ROOT:-}
ROOTS=${QFLIX_CANARY_CONTAINER_ROOTS:-}
MIN_PAYLOAD_BYTES=${QFLIX_CANARY_CONTAINER_MIN_PAYLOAD_BYTES:-}
DISC_QUALITIES=${QFLIX_CANARY_CONTAINER_DISC_QUALITIES:-}
# Bytes above which a single movie file is a finding regardless of how the
# *arr grades it. Default 26843545600 = 25 GiB, just above radarr's 25000 MiB
# grab ceiling (scripts/configure/59-brdisk-block.py), so anything already in
# the library that the ceiling would refuse to grab today is named.
OVERSIZE_BYTES=${QFLIX_CANARY_CONTAINER_OVERSIZE_BYTES:-26843545600}
MAX_VACUOUS_DAYS=${QFLIX_CANARY_CONTAINER_MAX_VACUOUS_DAYS:-}
STATE_DIR=${QFLIX_CANARY_CONTAINER_STATE_DIR:-}
MAX_NAMED=${QFLIX_CANARY_CONTAINER_MAX_NAMED:-}
ARR_TIMEOUT=${QFLIX_CANARY_CONTAINER_ARR_TIMEOUT:-}
SKIP_ARR=${QFLIX_CANARY_CONTAINER_SKIP_ARR:-}
FORCE_WINDOW=${QFLIX_CANARY_CONTAINER_FORCE_WINDOW:-}

# Two-part sshm shape, same as plex-playback.sh: a DOUBLE-quoted prelude that
# interpolates local config into the remote environment, glued to a
# SINGLE-quoted body whose regexes and format strings then need no escaping.
# tests/unit/test_canary_sshm_quoting.py extracts and `bash -n`s the
# single-quoted half, so there must be NO apostrophe anywhere below the
# opening quote - not in code, not in a comment, not in a possessive.
RES=$(sshm "
set -uo pipefail
export LCS_MEDIA_ROOT='${MEDIA_ROOT}'
export LCS_ROOTS='${ROOTS}'
export LCS_MIN_PAYLOAD_BYTES='${MIN_PAYLOAD_BYTES}'
export LCS_DISC_QUALITIES='${DISC_QUALITIES}'
export LCS_OVERSIZE_BYTES='${OVERSIZE_BYTES}'
export LCS_MAX_VACUOUS_DAYS='${MAX_VACUOUS_DAYS}'
export LCS_STATE_DIR='${STATE_DIR}'
export LCS_MAX_NAMED='${MAX_NAMED}'
export LCS_ARR_TIMEOUT='${ARR_TIMEOUT}'
export LCS_SKIP_ARR='${SKIP_ARR}'
export LCS_FORCE_WINDOW='${FORCE_WINDOW}'
"'
# ---- maintenance window ---------------------------------------------------
# Two OR-ed legs, copied from plex-playback.sh: the UTC wall clock (which
# depends on nothing having been written correctly) and a LIVE window lock whose
# owning pid is still alive. A leaked lock whose owner is gone must NOT suppress
# this canary forever - that is how a probe goes quiet without anyone deciding.
in_window() {
  [ "$LCS_FORCE_WINDOW" = "1" ] && { echo "forced-on"; return 0; }
  [ "$LCS_FORCE_WINDOW" = "0" ] && return 1
  DOW=$(date -u +%u); HOUR=$(date -u +%H); HOUR=${HOUR#0}
  if [ "$DOW" = "1" ] && [ "${HOUR:-0}" -ge 11 ] && [ "${HOUR:-0}" -lt 15 ]; then
    echo "wallclock-mon-1100-1500-utc"; return 0
  fi
  LOCK=${MANITOBA_STATE_DIR:-$HOME/.opt/maint}/lock
  if [ -f "$LOCK" ]; then
    PID=$(head -1 "$LOCK" 2>/dev/null | tr -dc 0-9)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      echo "window-lock-held-pid-$PID"; return 0
    fi
  fi
  return 1
}
WHY=$(in_window) && {
  echo "PASS: library-container-sanity - SKIP: maintenance window ($WHY) - the *arr APIs are restarted on purpose"
  exit 0
}

python3 <<"PYEOF"
import json
import os
import sys
import time
import urllib.error
import urllib.request


def env(name, default):
    """Env override, treating the empty string as absent. The prelude exports
    every knob unconditionally, so unset knobs arrive as empty strings."""
    value = os.environ.get(name)
    return default if value is None or value == "" else value


# $HOME is a symlink on this slot and `find $HOME` returns zero rows because of
# it (memory box-find-home-symlink-and-docker-blindspot). realpath first so
# every path this canary prints is the one the operator will see in *arr, in
# Plex and in ls.
HOME_REAL = os.path.realpath(os.path.expanduser("~"))
MEDIA_ROOT = env("LCS_MEDIA_ROOT", os.path.join(HOME_REAL, "media"))
ROOT_NAMES = [r for r in env(
    "LCS_ROOTS", "Movies|TV Shows|Anime|Anime Movies|Welcome").split("|") if r]
MIN_PAYLOAD_BYTES = int(env("LCS_MIN_PAYLOAD_BYTES", "67108864"))
MAX_VACUOUS_DAYS = float(env("LCS_MAX_VACUOUS_DAYS", "7"))
STATE_DIR = env("LCS_STATE_DIR",
                os.path.join(HOME_REAL, ".opt", "maint", "library-container-sanity"))
STATE_PATH = os.path.join(STATE_DIR, "vacuity.json")
MAX_NAMED = int(env("LCS_MAX_NAMED", "2"))
# Per-path cut inside msg=. See the message-budget section in the header: the
# 200-char cli.py cut is hard, and a one-finding line measured 233 before this.
PATH_CHARS = 45
ARR_TIMEOUT = float(env("LCS_ARR_TIMEOUT", "20"))
SKIP_ARR = env("LCS_SKIP_ARR", "") == "1"
# SIZE IS THE ONLY SIGNAL A RELABEL CANNOT ERASE.
#
# The quality leg above reads the grading the *arr itself recorded, and on 2026-08-20 that
# proved erasable. Tdarr transcoded a 47.6 GB BR-DISK .iso into a 42.3 GB
# .mkv; Radarr rescanned, re-parsed the new container, and re-graded it
# Bluray-1080p. At that instant every disc-LABEL check on this box went blind
# to it, while the file was still a 34.6 Mbps disc-scale payload that no
# bandwidth-capped client can play - the exact shape the remux cap exists to
# keep out. A second, unrelated file (Interstellar, 40.0 GB, also graded
# Bluray-1080p) was found by the same measurement and had been sitting
# unnoticed.
#
# So this leg asserts a physical fact rather than a label: no single movie
# file should exceed what the grab-time size ceiling would allow today. It
# cannot be defeated by re-parsing, re-grading, or changing the extension.
OVERSIZE_BYTES = int(env("LCS_OVERSIZE_BYTES", "26843545600") or 0)

DISC_QUALITIES = set(
    q.strip().upper()
    for q in env("LCS_DISC_QUALITIES", "BR-DISK|Raw-HD").split("|")
    if q.strip()
)

# --- the three lists. See the module header for why there are three. ---------
# Containers a Plex client opens. .m2ts and .ts stay HERE, not in the disc list:
# they are genuinely playable, and hardlink-integrity.sh learned the hard way
# (2026-08-19, six hours of false red) that dropping a real extension from a
# list does not tighten a check, it silently removes files from the sample.
PLAYABLE_EXTS = (
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".m2ts", ".mts", ".ts",
    ".mpg", ".mpeg", ".webm", ".wmv", ".flv", ".ogm", ".divx",
)
# A whole disc in one file, or an index that only ever accompanies one. Findings
# on sight at ANY size - a 4 KB .ifo is proof a DVD rip landed even though it is
# far below the payload-size gate.
DISC_IMAGE_EXTS = (
    ".iso", ".img", ".bin", ".nrg", ".mdf", ".mds", ".cue",
    ".vob", ".ifo", ".bup",
)
# Legitimate companions of a healthy library: subtitles, metadata, artwork,
# human text. Ignored and counted, never findings.
SIDECAR_EXTS = (
    ".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".smi", ".sup",
    ".nfo", ".xml", ".json",
    ".jpg", ".jpeg", ".png", ".webp", ".tbn",
    ".txt", ".log", ".md",
)
# Directory names that ARE a disc structure. Checked because the payload inside
# a BDMV rip is .m2ts, which the extension leg passes and must keep passing.
DISC_DIRS = ("BDMV", "VIDEO_TS", "AUDIO_TS")

# Finding kinds, most specific first. The reported STAGE is the first kind with
# at least one hit, so an ISO never hides behind a generic label.
KIND_ORDER = (
    ("disc-image", "container-disc-image"),
    ("disc-dir", "container-disc-dir"),
    ("arr-disc-quality", "arr-disc-quality"),
    ("arr-oversized-file", "arr-oversized-file"),
    ("unknown-payload", "container-unknown-payload"),
)

findings = []          # (kind, display_path) - the evidence
payloads = 0           # files with a playable container extension
sidecars_known = 0     # whitelisted sidecars
unknown_small = {}     # ext -> count, sub-payload-size unknowns (named, not fired)
roots_present = []
roots_missing = []
walk_errors = []
arr_graded = 0         # files an *arr told us the quality of
arr_unreachable = []


def rel(path):
    """Path relative to the media root, so msg= stays inside the 200-char cap
    cli.py truncates at while still naming the title.

    Both sides are realpath-ed first because they do NOT agree by default: the
    *arr report paths through the $HOME symlink (/home/quadstronaut/media/...)
    while the walk resolves it (/home28/quadstronaut/media/...). relpath across
    that mismatch emits a ../../../ ladder that eats the whole message budget
    before the title is reached - observed on the very first live run."""
    real_root = os.path.realpath(MEDIA_ROOT)
    try:
        real = os.path.realpath(path)
    except OSError:
        real = path
    for base in (real_root, MEDIA_ROOT):
        if base and real.startswith(base.rstrip(os.sep) + os.sep):
            return real[len(base.rstrip(os.sep)) + 1:]
    # Outside the media root entirely (a manual-import path, a moved mount):
    # keep the last two components so the title is still identifiable without
    # the full ladder.
    parts = real.split(os.sep)
    return os.sep.join(parts[-2:]) if len(parts) > 2 else real


def add(kind, path, tag=""):
    """Record one piece of evidence. `tag` carries the *arr instance and the
    recorded quality, appended AFTER the path is shortened - passing the whole
    composite string through rel() would defeat the prefix match.

    The trailing ~ marks a cut path. Without it a truncated name is a plausible
    real name, and an operator pasting it into a find(1) gets zero rows and
    concludes the file is gone."""
    display = rel(path)
    if len(display) > PATH_CHARS:
        display = display[:PATH_CHARS] + "~"
    findings.append((kind, display + (" [%s]" % tag if tag else "")))


def transient_stage_ext(fname):
    """Playable stem extension of an in-flight staging file, else None.

    "Movie (1995).mkv.tmp" is something mid-write on a container, not an
    unplayable container. audio-disposition-janitor.py remuxes in place and its
    TmpVanishedError docstring records Tdarr replaceOriginalFile staging leaving
    exactly this shape on 2026-08-08. A bare ".tmp" returns None and stays a
    finding: nothing here writes one, and payload-sized bytes with no evidence
    of their container are the anti-enumeration-gap case, not staging."""
    if os.path.splitext(fname)[1].lower() != ".tmp":
        return None
    inner = os.path.splitext(os.path.splitext(fname)[0])[1].lower()
    return inner if inner in PLAYABLE_EXTS else None


# --- vacuity clock -----------------------------------------------------------
# Copied in shape from hardlink-integrity.sh, which arrived at it after two
# retired designs. The clock measures "did this guard EXAMINE anything", never
# "did it like what it saw": a run that fires has plainly examined something and
# clears the streak.
def read_vacuity():
    """Epoch of the first vacuous run in the current streak, or None."""
    try:
        with open(STATE_PATH) as fh:
            since = int(json.load(fh)["since"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Unreadable state re-arms the clock rather than crashing. A canary that
        # dies on its own bookkeeping is a false page, which is worse than
        # forgetting one streak.
        sys.stderr.write("note: vacuity state unreadable (%s: %s), re-arming\n"
                         % (type(exc).__name__, exc))
        return None
    now = int(time.time())
    # Clock skew or a bad write: a future timestamp would compute a negative age
    # and silently never trip. Treat it as starting now.
    return now if since > now else since


def clear_vacuity():
    try:
        os.remove(STATE_PATH)
    except FileNotFoundError:
        pass
    except OSError as exc:
        sys.stderr.write("note: vacuity state clear failed (%s)\n" % exc)


def vacuous_exit(reason, detail):
    """Pass-but-blind, unless it has been blind too long."""
    since = read_vacuity()
    now = int(time.time())
    if since is None:
        since = now
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(STATE_PATH, "w") as fh:
                json.dump({"since": since, "reason": reason}, fh)
        except OSError as exc:
            # Cannot persist -> cannot ever trip. Say so out loud instead of
            # degrading silently into the exact blindness this code exists for.
            sys.stderr.write("note: vacuity state write failed (%s) - the "
                             "blind-timer cannot arm\n" % exc)
    days = (now - since) / 86400.0
    if days >= MAX_VACUOUS_DAYS:
        sys.stderr.write(
            "STAGE=container-blind msg=no-assertion-for-%.1fd-max-%.0fd "
            "reason=%s %s\n" % (days, MAX_VACUOUS_DAYS, reason, detail))
        sys.exit(2)
    print("PASS: library-container-sanity - inconclusive (%s; %s) [blind %.1fd "
          "of %.0fd allowed]" % (reason, detail, days, MAX_VACUOUS_DAYS))
    sys.exit(0)


# --- leg 1: the filesystem ---------------------------------------------------
def on_walk_error(exc):
    """os.walk SWALLOWS PermissionError by default, which turns an unreadable
    root into something indistinguishable from an empty one. That is precisely
    the vacuity trap, so errors are collected instead of dropped."""
    walk_errors.append(str(exc))


for name in ROOT_NAMES:
    root = os.path.join(MEDIA_ROOT, name)
    if not os.path.isdir(root):
        roots_missing.append(name)
        continue
    roots_present.append(name)
    for dirpath, dirnames, files in os.walk(root, onerror=on_walk_error):
        for d in list(dirnames):
            if d.upper() in DISC_DIRS:
                add("disc-dir", os.path.join(dirpath, d))
                # Prune it. The finding already names the directory, and walking
                # into a BDMV tree only yields dozens of .clpi/.mpls/.bdmv index
                # files that would flood the unknown-sidecar bucket with noise
                # about a fault already reported.
                dirnames.remove(d)
        for fname in files:
            path = os.path.join(dirpath, fname)
            ext = os.path.splitext(fname)[1].lower()
            try:
                size = os.path.getsize(path)
            except (FileNotFoundError, PermissionError, OSError):
                # A file that vanished mid-walk (the reaper runs on its own
                # schedule) is not evidence of anything.
                continue
            if ext in DISC_IMAGE_EXTS:
                add("disc-image", path)
            elif ext in PLAYABLE_EXTS:
                payloads += 1
            elif ext in SIDECAR_EXTS or (fname.startswith(".")
                                         and size < MIN_PAYLOAD_BYTES):
                # The dotfile arm covers .plexmatch / .plexignore / .stignore,
                # for which splitext reports NO extension at all - 38 files live
                # on this box. Size-gated so a hidden payload cannot use a
                # leading dot to walk past the check.
                sidecars_known += 1
            elif transient_stage_ext(fname):
                # In-flight staging beside the file it will replace. Counted as
                # a small unknown - it is examined (so it feeds the vacuity
                # denominator) and NAMED in the pass line, so a staging file
                # that never clears is still visible on a green run.
                key = transient_stage_ext(fname) + ".tmp"
                unknown_small[key] = unknown_small.get(key, 0) + 1
            elif size < MIN_PAYLOAD_BYTES:
                # Unknown, but too small to be a feature or an episode. Named in
                # the pass line so the whitelist can be extended deliberately
                # rather than by a red at 04:30.
                unknown_small[ext or "(none)"] = unknown_small.get(
                    ext or "(none)", 0) + 1
            else:
                # Payload-sized and in none of the three lists. Whatever the
                # next unplayable shape turns out to be, it lands here.
                add("unknown-payload", path)


# --- leg 2: what the *arr thinks it imported ---------------------------------
def secret(name):
    with open(os.path.join(HOME_REAL, "secrets", name)) as fh:
        return fh.read().strip()


def arr_base(app):
    """http://127.0.0.1:PORT/URLBASE/api/v3 - urlbase carries NO leading slash
    in ~/secrets, and may legitimately be absent for a loopback-direct app."""
    port = secret(app + ".port")
    try:
        urlbase = secret(app + ".urlbase")
    except (OSError, IOError):
        urlbase = ""
    prefix = "/" + urlbase.strip("/") if urlbase.strip("/") else ""
    return "http://127.0.0.1:%s%s/api/v3" % (port, prefix)


def arr_get(base, key, path):
    req = urllib.request.Request(base + path, headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=ARR_TIMEOUT) as resp:
        return json.load(resp)


def quality_name(record):
    return (((record.get("quality") or {}).get("quality") or {}).get("name")
            or "")


def arr_tag(app, record):
    """app/QUALITY, plus /missing when the *arr reported path is not on disk.

    Observed live within hours of the 2026-08-20 incident: an operator replaced
    the .iso with a remuxed .mkv, and Radarr went on holding a BR-DISK record
    pointing at the deleted path. That is STILL a red - the *arr genuinely
    believes it owns a disc image, so it will not search for an upgrade - but
    the remedy is a rescan, not a delete, and an operator who cannot tell those
    apart from the message goes to the wrong place. Tagged rather than
    suppressed: suppressing it would let a stale disc grading sit unnoticed,
    which is the whole failure this canary exists for.
    """
    path = record.get("path") or ""
    try:
        present = bool(path) and os.path.exists(path)
    except OSError:
        present = False
    return "%s/%s%s" % (app, quality_name(record),
                        "" if present else "/missing")


if not SKIP_ARR:
    for app in ("radarr", "radarr2"):
        try:
            key = secret(app + ".key")
            base = arr_base(app)
            movies = arr_get(base, key, "/movie")
        except Exception as exc:
            # An *arr that is merely down is NOT this canary incident, and
            # reding on it would duplicate the app monitor that already covers
            # it. Counted and named; only a run where NOTHING at all could be
            # examined is treated as blind.
            arr_unreachable.append("%s:%s" % (app, type(exc).__name__))
            continue
        for movie in movies:
            mfile = movie.get("movieFile") or {}
            if not mfile:
                continue
            arr_graded += 1
            if quality_name(mfile).upper() in DISC_QUALITIES:
                add("arr-disc-quality",
                    mfile.get("path") or movie.get("title") or "?",
                    arr_tag(app, mfile))
            elif OVERSIZE_BYTES > 0 and (mfile.get("size") or 0) > OVERSIZE_BYTES:
                # elif, not if: a disc-graded file is already named by the
                # branch above and naming it twice would burn the 200 char
                # Kuma message budget on one title.
                add("arr-oversized-file",
                    mfile.get("path") or movie.get("title") or "?",
                    "%s/%.1fGB" % (app, (mfile.get("size") or 0) / 1e9))
    for app in ("sonarr", "sonarr2"):
        try:
            key = secret(app + ".key")
            base = arr_base(app)
            series = arr_get(base, key, "/series")
        except Exception as exc:
            arr_unreachable.append("%s:%s" % (app, type(exc).__name__))
            continue
        for show in series:
            # Sonarr v3 has no all-files endpoint: /api/v3/episodefile answers
            # 400 "seriesId or episodeFileIds must be provided". Measured live
            # 2026-08-20 at 39 calls in 0.5 s across both instances, which is
            # why the per-series loop is affordable at a daily cadence.
            try:
                efiles = arr_get(base, key, "/episodefile?seriesId=%d"
                                 % show.get("id", 0))
            except Exception:
                continue
            for efile in efiles:
                arr_graded += 1
                if quality_name(efile).upper() in DISC_QUALITIES:
                    add("arr-disc-quality",
                        efile.get("path") or show.get("title") or "?",
                        arr_tag(app, efile))


# --- verdict -----------------------------------------------------------------
counts = {}
for kind, _ in findings:
    counts[kind] = counts.get(kind, 0) + 1

# walk_errors= and arr_unreachable= are omitted when zero. They cost 32 chars of
# "=0" on every healthy run, and msg= is cut at 200 - that is a whole offending
# path displaced to report two non-events. When they are non-zero they are the
# most important thing on the line and they appear.
summary = (
    "scanned=%d payloads=%d sidecars=%d roots=%d/%d arr_graded=%d"
    % (payloads + sidecars_known + sum(unknown_small.values()) + len(findings),
       payloads, sidecars_known, len(roots_present), len(ROOT_NAMES),
       arr_graded)
)
if walk_errors:
    summary += " walk_errors=%d" % len(walk_errors)
if arr_unreachable:
    summary += " arr_unreachable=%d" % len(arr_unreachable)

if findings:
    # EVIDENCE BEATS VACUITY. Evaluated before the blind check on purpose: a
    # real ISO found in two readable roots is a red even if the other three
    # roots could not be walked at all.
    clear_vacuity()
    stage = "container-unplayable"
    for kind, label in KIND_ORDER:
        if counts.get(kind):
            stage = label
            break
    # Name paths in KIND_ORDER, not in discovery order. cli.py truncates msg= at
    # 200 chars, so whichever paths land first are the only ones the operator
    # ever sees - and a run reporting STAGE=container-disc-dir while naming
    # three unrelated unknown-payload files sends them to the wrong file
    # entirely. Observed on the live BDMV tree during development.
    rank = {kind: i for i, (kind, _) in enumerate(KIND_ORDER)}
    ordered = sorted(findings, key=lambda f: rank.get(f[0], len(KIND_ORDER)))
    named = ";".join(p for _, p in ordered[:MAX_NAMED])
    # summary BEFORE paths=. cli.py cuts at 200 with no ellipsis, so the tail is
    # simply not delivered; counts must be on the surviving side of that cut.
    sys.stderr.write(
        "STAGE=%s msg=findings=%d %s %s paths=%s\n"
        % (stage, len(findings),
           " ".join("%s=%d" % (k, counts[k]) for k in sorted(counts)),
           summary, named))
    sys.exit(2)

# Nothing found. Did EACH LEG examine anything at all?
#
# One clock per leg, deliberately. The first draft asked
#     asserted = payloads > 0 or arr_graded > 0
# and that single OR let leg 2 satisfy leg 1: with MEDIA_ROOT pointed at a
# directory that does not exist and the *arrs up, this printed
#     PASS ... scanned=0 payloads=0 roots=0/5 arr_graded=432
# - a filesystem check reporting green with zero filesystem. Each leg now has to
# have done its own work, and the run is inconclusive unless both did.
fs_asserted = (
    bool(roots_present)
    and not roots_missing          # a missing root is unexamined, not empty
    and not walk_errors            # os.walk swallows PermissionError by default
    and (payloads + sidecars_known + sum(unknown_small.values())) > 0
)
# SKIP_ARR waives ONLY the arr half: it exists so the filesystem leg can run
# where no *arr is reachable by construction (the unit tests), and requiring an
# arr assertion there would make every offline run vacuous.
arr_asserted = arr_graded > 0
if not (fs_asserted and (arr_asserted or SKIP_ARR)):
    reasons = []
    if not fs_asserted:
        if not roots_present:
            reasons.append("no-library-roots-readable")
        elif roots_missing:
            reasons.append("library-roots-missing:" + ",".join(roots_missing))
        elif walk_errors:
            reasons.append("roots-unreadable")
        else:
            reasons.append("empty-library")
    if not arr_asserted and not SKIP_ARR:
        reasons.append("no-arr-grading%s"
                       % ("-all-arrs-down" if len(arr_unreachable) >= 4 else ""))
    # Capped: root names are operator-chosen and the reason is echoed into a
    # msg= that is cut at 200 chars.
    vacuous_exit("+".join(reasons)[:80], summary)

clear_vacuity()
extras = ""
if unknown_small:
    # Split so the two mean different things to a reader: unlisted_sidecars= is
    # a whitelist that wants extending, staging= is a remux in flight (and, if
    # the same entry is still there tomorrow, one that never finished).
    staging = sorted((e, n) for e, n in unknown_small.items()
                     if e.endswith(".tmp"))
    unlisted = sorted((e, n) for e, n in unknown_small.items()
                      if not e.endswith(".tmp"))
    if unlisted:
        extras += " unlisted_sidecars=" + ",".join(
            "%s:%d" % (e, n) for e, n in unlisted)
    if staging:
        extras += " staging=" + ",".join("%s:%d" % (e, n) for e, n in staging)
# No missing_roots= here on purpose: a missing root now fails fs_asserted, so it
# exits inconclusive above and can never reach this green line. The reason
# string names it there instead.
if arr_unreachable:
    extras += " arr_down=" + ",".join(arr_unreachable)
print("PASS: library-container-sanity - every payload is a playable container "
      "(%s)%s" % (summary, extras))
sys.exit(0)
PYEOF
') || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC
