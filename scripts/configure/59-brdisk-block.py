#!/usr/bin/env python3
"""Block full-disc payloads (BR-DISK / ISO / BDMV / VIDEO_TS) at GRAB time on
the movie instances. Idempotent - safe to re-run. Sibling of
57-no-4k-enforce.py and 58-remux-cap-enforce.py, same secrets loading, same
print-what-changed, same apply-directly convention, no argparse.

WHY THIS EXISTS - the concrete failure it prevents
--------------------------------------------------
2026-08-20 04:52:08Z Radarr main grabbed, for "In the Mouth of Madness":

    In.the.Mouth.of.Madness.1994.1080p.Blu-ray.CE.4K.REMASTERED.DTS-HD.MA.5.1-NOGRP-Obfuscated
    indexer NZBgeek, reported size 51,311,448,000 B (48,934 MiB / 47.79 GiB)
    graded at grab time as:  Bluray-1080p    (modifier none)

At 07:14:03Z the same release logged downloadFolderImported graded

    BR-DISK (modifier brdisk), payload 47,649,253,376 B (45,442 MiB / 44.38 GiB)

as a single .iso. The release NAME said "1080p Blu-ray"; the BYTES were a full
BD-50 disc image. Radarr re-graded on import from the file extension and
imported it anyway.

This landed inside the cascade started the day before: 58-remux-cap-enforce.py
capped Remux on Radarr main, and scripts/maint/qflix-remux-regrab.py deleted
and re-searched all 23 remux movies. Each re-search took the best still-allowed
release, and for this title that was the mislabelled disc image.

WHY THE PROFILE COULD NOT HAVE STOPPED *THIS* GRAB (AND WHERE IT WAS STILL OPEN)
--------------------------------------------------------------------------------
PREMISE CORRECTION, recorded rather than quietly patched. An earlier draft of
this header claimed "BR-DISK is allowed on NO profile, on either Radarr" and
used that to argue no profile edit was needed. Re-measured live 2026-08-20,
that claim was FALSE on two of the three instances in scope:

    radarr   p6/p7/p8/p9        BR-DISK allowed=False    (claim held here)
    radarr2  p1 "Any"           BR-DISK allowed=True  <- 3 of its 6 movies
    sonarr   p6 "HD 720p/1080p" Raw-HD  allowed=True  <- ALL 36 series

So the profile WAS a lever, on the instances the draft never checked. It is now
LEVER 3 below, and the claim that replaced it is narrower and true: the profile
could not have stopped THIS PARTICULAR GRAB, because the grab landed on radarr
main p6, where BR-DISK was already disallowed and the release still got in.

On radarr main the profile gate WORKS and fires constantly - from the same
06:51 search batch that produced the bad grab:

    Release 'The.Thing.1982.1080p.Blu-ray.AVC.FAN-RES...-NOGRP-Obfuscated'
      rejected: [Permanent] BR-DISK is not wanted in profile
    Release 'In the Mouth of Madness 1995 DVDRip XviD BigPerm LKRG'
      rejected: [Permanent] DVD is not wanted in profile

Radarr also runs RawDiskSpecification at grab, which rejects independently of
any profile:

    RawDiskSpecification|Release contains raw Bluray/DVD, rejecting.

Both of those key off the quality Radarr PARSED FROM THE RELEASE NAME. Our
release parsed as Bluray-1080p, so on radarr main neither could fire. For the
incident release specifically, NAME-BASED PARSING is the hole, and no amount of
profile editing closes it - which is why 58 alone could not have prevented this
and why LEVER 1 (size) is the one that actually catches it.

That is a statement about ONE grab, not a licence to skip the profiles. A
profile that allows a disc tier is a SECOND, independent hole: there the gate
never even has to be fooled, because a correctly-parsed BR-DISK release is
simply accepted. LEVER 3 closes that one. Two different holes, two levers;
neither substitutes for the other.

THE IMPORT SIDE HAS NO LEVER. PROVEN, NOT ASSUMED.
---------------------------------------------------
Tested live 2026-08-20 before writing this script. The existing TRaSH custom
format "BR-DISK" was scored -10000 on Radarr main profile 6 (minFormatScore 0),
then the import decision engine was re-queried against the offending file:

    GET /api/v3/manualimport?folder=<movie folder>&filterExistingFiles=false
    -> {"q":"BR-DISK","cfs":["BR-DISK"],"customFormatScore":-10000,"rejections":[]}

customFormatScore -10000, minFormatScore 0, and ZERO rejections. Custom-format
score gates the GRAB only; Radarr's import decision engine does not consult it,
and (per the incident) does not re-check the quality profile either. There is
no supported import-time gate short of useScriptImport, which replaces the
whole import path with a shell script - one bug there stops every import on the
box, so it is not worth trading a rare 44 GB file for a routine total outage.

Consequence, stated plainly: EVERYTHING BELOW IS A GRAB-TIME GATE. That is not
a compromise, it is the stronger position - a release that is never grabbed is
never downloaded and never reaches the import step at all. The residual risk is
recorded near the bottom of this header rather than papered over.

WHAT THIS SCRIPT DOES - three levers, all re-read after writing
----------------------------------------------------------------
LEVER 1 - config/indexer.maximumSize (the one that catches THIS bug).
  Radarr runs MaximumSizeSpecification on every release at grab. On this box it
  logged, for every release, for years:

      MaximumSizeSpecification|Maximum size is not set.

  because /api/v3/config/indexer had maximumSize = 0 (unlimited) on both Radarr
  instances. It is an absolute per-release ceiling in MiB, evaluated BEFORE the
  download starts, against the size the indexer reports. It is the only signal
  available at grab time that separated the mislabelled disc from a real 1080p
  rip, because the title carried no disc indicator at all.

  Ceilings, set from measured grab history read 2026-08-20, not from taste.
  maximumSize is in MiB; Radarr's UI and its rejection text render the same
  number as "GB" while actually meaning GiB, so both units are given here to
  stop that mismatch reading as a bug:

    radarr  25000 MiB = 24.41 GiB   largest legit grab 17,985 MiB (17.56 GiB)
                                    -> 1.39x headroom      n = 40 grabs
    radarr2 42000 MiB = 41.02 GiB   largest legit grab 34,930 MiB (34.11 GiB)
                                    -> 1.20x headroom      n = 1 GRAB

  THE TWO SAMPLE SIZES ARE NOT COMPARABLE AND THE HEADER USED TO PRETEND THEY
  WERE. An earlier draft said both ceilings came from "the last 40 grabs per
  instance". radarr main really does have 40. radarr2's ENTIRE history is FOUR
  rows containing exactly ONE grab (/api/v3/history totalRecords=4, re-read
  2026-08-20):

      2026-07-25T03:41:38Z  Remux-1080p  36,627,251,000 B
      Cowboy.Bebop.The.Movie.2001.1080p.REMUX

  So radarr2's "largest legit grab" is the ONLY legit grab. n=1 is a data
  point, not a distribution, and 1.20x headroom computed against it carries
  none of the confidence the radarr number does. This is disclosed rather than
  fixed because both available fixes are worse than the honesty:

    * RAISING radarr2 toward the BD-50 floor (~46.6 GiB / 47,700 MiB) would buy
      headroom against unmeasured legitimate grabs, but it loosens the disc
      block for everything in the 41-46 GiB band and the script's own
      never-loosen rule means a raise cannot be walked back by re-running it.
    * LOWERING it would reject the one grab actually measured.

  Neither has an incident behind it. See RESIDUAL RISK item 3; re-derive the
  number honestly once radarr2 has a real grab history.

  "LEGIT" MEANS "STILL ALLOWED BY POLICY", AND THE DIFFERENCE MATTERS. Radarr
  main's raw grab history does contain 7 releases larger than 17,985 MiB, up to
  25,471 MiB - every one of them a Remux-1080p, the tier 58 banned the previous
  day. Read the raw history without splitting on quality and 25000 looks
  recklessly tight; split on it and the largest grab this profile can still
  legally take is 17,985 MiB. Do not "fix" the ceiling upward off the unsplit
  numbers. A side effect worth knowing: because the ceiling lands inside the
  now-banned 18,413-25,471 MiB remux band, it also rejects remux a second time,
  independently of 58's profile edit.

  ONE LIBRARY FILE THIS CEILING WOULD HAVE BLOCKED, AND WHY THAT IS CORRECT.
  The 40-grab window above only reaches back past 2026-07-12, and a full
  library scan of radarr main finds exactly one NON-disc file above the
  ceiling:

    movieId 412  Interstellar (2014)  38,118 MiB (37.23 GiB)  Bluray-1080p
    grabbed 2026-07-12 as a "UHD BDRip 1080p HDR10 IMAX" x265 multi-dub pack,
    169 min -> about 31.5 Mbps

  It is graded Bluray-1080p, so the profile allowed it and this ceiling would
  not have. Call that a false positive only if you stop at the label: 31.5 Mbps
  is a remux-class bitrate wearing a rip's name, and it is exactly the file the
  1927 kbps transcode-only client documented in 58 cannot play. 58 removed that
  class by quality NAME; the ceiling removes what is left of it by SIZE, which
  is the only property the client actually cares about. Expect this file to be
  listed by the library scan on every run until it is re-grabbed or aged out.
  That is the scan reporting a real condition, not noise - which is why it
  prints a per-file reason instead of a bare count.

  The two ceilings differ because the two instances have different policy.
  58 capped Remux on radarr main, so nothing legitimate there approaches 25 GB.
  radarr2 (anime/foreign) still ALLOWS Remux-1080p by deliberate decision - see
  58's SCOPE block - and its largest honest grab is a 34.11 GiB Cowboy Bebop
  remux, so its ceiling has to clear that. Both ceilings reject the 48,934 MiB
  grab and the 45,442 MiB payload.

  The script never LOOSENS an existing ceiling. If an operator has already set
  a tighter value by hand, that value wins; only 0 (unlimited) or a value above
  the target is rewritten. A policy enforcer that can raise a limit is a policy
  enforcer that can be used to defeat the policy.

LEVER 2 - score the existing "BR-DISK" custom format at -10000.
  Both Radarr instances and Sonarr main already carry the TRaSH "BR-DISK"
  custom format, installed by recyclarr. Its regex catches disc-shaped release
  TITLES (COMPLETE.BLURAY, BD25/50/66/100, BDMV, 3D-BD, Blu-ray plus AVC with
  no codec token, ...). On the recyclarr-MANAGED profiles it is already scored
  -10000. On every other profile it sat at 0 - including the profiles that hold
  essentially the entire library:

    radarr  profile 6 "HD 720p/1080p"   109 of 111 movies   BR-DISK score 0
    radarr2 profiles 1 "Any" / 4 "HD-1080p"   3 + 3 movies  BR-DISK score 0
    sonarr  profile 6 "HD 720p/1080p"   all 36 series       BR-DISK score 0

  With minFormatScore 0, a -10000 score is a hard grab rejection. This is a
  title-based gate and it would NOT have caught the incident release (proven:
  that title contains no AVC/HEVC/VC-1/MVC/MPEG-2/BDMV/ISO token, so the TRaSH
  regex's first alternative cannot match). It is here because it costs one
  integer, it covers the conventionally-named disc releases that Radarr's own
  parser might still grade as Bluray-1080p, and it makes an existing dead
  policy surface live.

  Writing -10000 is SAFE on recyclarr-managed profiles specifically because
  -10000 is the value recyclarr itself writes. This script does not introduce a
  new custom format, so recyclarr's reset_unmatched_scores (enabled: true on
  every managed profile in the box's recyclarr.yml) has nothing of ours to
  reset. That is a deliberate design constraint, not a coincidence - see
  REJECTED below.

LEVER 3 - disallow the disc-class QUALITY on every profile that still allows it
  Added 2026-08-20 after review caught the false premise quoted at the top of
  this header. LEVERS 1 and 2 are both CONDITIONAL: one compares a reported
  size, the other sums custom-format scores against minFormatScore. The
  quality-profile allowed flag is UNCONDITIONAL - a release whose parsed
  quality is not allowed is rejected outright, with no score to tune and no
  size to misreport. It is the cheapest and strongest of the three, and it was
  the one actually switched OFF where it mattered:

    radarr2 p1 "Any"                BR-DISK  allowed True -> False
    sonarr  p6 "HD 720p/1080p"      Raw-HD   allowed True -> False

  Raw-HD IS the disc-class tier on Sonarr. An earlier draft of the SCOPE block
  below said "Sonarr has NO BR-DISK quality at all, so the parse-based half of
  this problem cannot exist there" and stopped there. That was a NAMING error
  reasoned into a COVERAGE decision: Sonarr's raw-disc tier is simply called
  something else, is_disc_quality() in this very file already classifies it as
  disc, disc_offenders() already reports it, and Radarr's own
  RawDiskSpecification treats BR-DISK and Raw-HD as one class. Sonarr p6
  carries 100 percent of the TV library, so the tier this script itself calls
  unplayable was permitted on every series on the box.

  The ban walks items[] RECURSIVELY (a group entry nests its own items[]) and
  refuses to touch a quality that is the profile CUTOFF, because the API
  rejects a profile whose cutoff is disallowed - see ban_disc_qualities() for
  the full reasoning on both points.

REJECTED ALTERNATIVES - each killed by evidence, not by preference
-------------------------------------------------------------------
* qualitydefinition maxSize (a MB-per-minute ceiling on Bluray-1080p).
  Would work - AcceptableSizeSpecification already runs and every HD tier has
  maxSize null. REJECTED because recyclarr.yml declares
  `quality_definition: type: movie` on BOTH radarr instances (and series/anime
  on both sonarrs). Any value written here is reverted on the next recyclarr
  sync, producing a script that passes its own re-read check while prod
  silently drifts back. Identical trap to the sonarr2 remux profile documented
  in 58's SCOPE block.

* A NEW custom format on Source or Quality Modifier (BRDISK / RAWHD).
  REJECTED as a duplicate policy surface. Radarr's RawDiskSpecification already
  rejects exactly that class natively at grab, on every profile, and cannot be
  switched off - verified in the debug log quoted above. A new CF would restate
  an unconditional native rule as a conditional per-profile score, and
  reset_unmatched_scores would zero it on the managed profiles anyway. Three
  surfaces for one intent is how policy rots.

* Release profile "Must Not Contain" terms (/api/v3/releaseprofile).
  REJECTED on two independent grounds. First, it could not have caught this:
  the offending title carries no disc indicator, and the only token that
  distinguishes it from a good release ("Blu-ray" hyphenated) also appears in a
  legitimate grab from the same corpus (Oceans.Eight.2018.REMUX.1080p.Blu-ray.
  AVC.TrueHD...), so any term list tight enough to fire is also a false
  positive. Second, Radarr does not VALIDATE terms: POSTing the ignored term
  "/[unclosed/" returned HTTP 201 and stored it verbatim. A malformed regex
  term is accepted silently and then does nothing - a structurally dead rule
  that reads as protection. The TRaSH BR-DISK custom format already covers the
  title class with a regex that is maintained upstream and actually exercised.

SCOPE - three instances, and the two exclusions are measured
-------------------------------------------------------------
  radarr   LEVERS 1 + 2 + 3. The instance that took the hit. All four of its
           profiles already disallowed BR-DISK, so LEVER 3 is a no-op HERE -
           which is precisely why the earlier draft generalised from it and got
           the other two instances wrong.
  radarr2  LEVERS 1 + 2 + 3. An ISO is unplayable in the anime/foreign movie
           library exactly as it is in the main one; same Radarr 6.3.0.10514,
           same endpoints, same BR-DISK custom format, and its profiles 1 and 4
           (the two actually in use) had BR-DISK at 0. Its profile 1 "Any" also
           ALLOWED the BR-DISK quality outright and holds 3 of its 6 movies -
           LEVER 3 closed that.
  sonarr   LEVERS 2 + 3. Checked, not assumed:
             - Sonarr has no quality literally NAMED "BR-DISK", and its
               customformat/schema has no QualityModifierSpecification. Do not
               conclude from that (as an earlier draft did) that the disc class
               is absent: Sonarr's disc/raw tier is Raw-HD, it was ALLOWED on
               profile 6, and profile 6 carries all 36 series. LEVER 3 now
               disallows it. See the LEVER 3 block for why the naming
               difference is not a coverage difference.
             - It DOES carry the TRaSH BR-DISK custom format (id 12) and every
               one of its 36 series sits on profile 6, where that format scored
               0. TV can absolutely be offered a disc-shaped release, so the
               title gate is worth arming.
             - LEVER 1 IS DELIBERATELY NOT APPLIED. maximumSize is per RELEASE,
               and a legitimate Sonarr season pack is routinely larger than any
               single movie. An absolute ceiling that works for movies would
               silently starve TV. Leaving it at 0 is the correct answer, not
               an oversight.
  sonarr2  EXCLUDED. It has no BR-DISK custom format (0 matches), so LEVER 2
           has nothing to score, and creating one would be the rejected
           "new custom format" path above. Its single series sits on profile 1.
           Re-measured 2026-08-20 for LEVER 3 as well: no profile on sonarr2
           allows Raw-HD or any disc tier, so LEVER 3 has nothing to close
           there either. Recorded as a known gap rather than fixed badly.

RESIDUAL RISK - what this does NOT close
-----------------------------------------
  1. A mislabelled SINGLE-LAYER BD-25 image (~23.28 GiB / 23,841 MiB) slips
     under BOTH ceilings - 24.41 GiB on radarr and 41.02 GiB on radarr2. Only
     the dual-layer BD-50 class (~46.6 GiB), which is what actually landed, is
     stopped by size. Closing the BD-25 case means dropping radarr's ceiling to
     roughly 22000 MiB (21.48 GiB), which sits between "large legitimate 1080p
     rip" and "smallest full disc" - but that leaves only 1.22x headroom over
     the largest legitimate grab measured, versus 1.39x today. That is an
     availability-for-coverage trade with no measured incident behind it yet,
     so it is left as a documented knob rather than made silently. Change the
     number in INSTANCES if a BD-25 ever does land.
  2. Import remains ungated, by proof rather than by omission. If a disc image
     ever does get grabbed, nothing stops it landing.
  3. radarr2's ceiling rests on n=1. See the LEVER 1 block for the numbers and
     for why raising or lowering it today would both be worse than saying so.
     Revisit once radarr2 has more than one grab in its history.
  4. NOTHING HERE SEES A FILE THAT WAS NEVER GRABBED. Measured on this box the
     same day, and it is the sharpest limit on all three levers:

       09:14:03Z  Radarr imports  ...BR-DISK.iso  47,649,253,376 B
       09:14:39Z  Tdarr picks the .iso up  ("File detected, adding to queue")
       11:03:14Z  Tdarr writes   ...BR-DISK.mkv  42,341,133,540 B (39.43 GiB)
                  into the library folder, replacing the .iso

     Radarr's history for movieId 441 contains NO event after the 09:14 import
     - that .mkv is a Tdarr transcode output, not a grab, so it passed through
     no indexer, no MaximumSizeSpecification, no custom format and no quality
     profile. Every lever in this file is a GRAB-time gate by proof (see the
     import section above), and a locally PRODUCED file enters the library
     downstream of all of them. It even kept the "BR-DISK" token in its
     filename, so a Radarr rescan re-parses the disc quality straight back out
     of a file that is now an ordinary mkv.

     This is out of scope for this script by construction, not by oversight -
     the fix lives on the Tdarr side (do not queue disc images) and in the
     library-container-sanity canary, which scans what is ON DISK rather than
     what was grabbed. Recorded here so nobody reads "grab gate armed" as
     "library cannot acquire an unplayable file".

  All four residuals are DETECTION problems, not prevention problems. This
  script reports any BR-DISK / Raw-HD file already in either movie library at
  the end of every run (read-only, never fatal) so the condition is visible.

VERIFIED LIVE 2026-08-20, END TO END
-------------------------------------
After this script ran, an interactive search on the affected movie
(GET /api/v3/release?movieId=441, which evaluates releases WITHOUT grabbing)
returned 49 releases, 0 accepted, and scored the exact incident release:

  In.the.Mouth.of.Madness.1994.1080p.Blu-ray.CE.4K.REMASTERED.DTS-HD.MA.5.1-NOGRP-Obfuscated
    rejected: "47.8 GB is too big, maximum size is 24.4 GB
               (Settings->Indexers->Maximum Size)"        <- LEVER 1

and two sibling full-disc releases of the same film:

  In.the.Mouth.of.Madness.1994.1080p.Blu-ray.AVC.DTS-HD.MA.5.1-CultFilms
    rejected: "Custom Formats BR-DISK have score -10000 below Movie's profile
               minimum 0"                                 <- LEVER 2
              "50.4 GB is too big, maximum size is 24.4 GB"
              "BR-DISK is not wanted in profile"          <- native, pre-existing

That CultFilms pair is the case LEVER 2 exists for: same film, same disc, but
its title carries the AVC token the TRaSH regex needs, so the title gate fires
where it could not on the -NOGRP- release. Neither lever is redundant.

IS THE CEILING ACTUALLY ENFORCED ON GRABS? YES. TIMELINE, FROM THE LOGS.
-------------------------------------------------------------------------
The question was raised because the file now sitting in the library is 40,378
MiB, well ABOVE radarr's new 25000 MiB ceiling. Either it predates the ceiling
or the ceiling does not fire. It is the first, and the logs settle it - all
times CEST, from ~/.apps/radarr/logs/:

  06:52:07  DownloadService   grabbed the incident release          <- the grab
  09:14:03  MovieService      assigned ...BR-DISK.iso to movie 441
  09:32:11  MaximumSizeSpecification|Maximum size is not set.       <- LAST
                                                                   such line
  ~09:32-11:10  this script runs; maximumSize 0 -> 25000
  11:10:21  MaximumSizeSpecification|63.7 GB is too big, maximum    <- FIRST
            size is 24.4 GB (Settings->Indexers->Maximum Size)      rejection
  11:57:27  ...|72.5 GB is too big... and |32.2 GB is too big...

26 "is too big" rejections are logged in the retained window, the most recent
minutes before this was written. THE CEILING WORKS. The grab beat it by roughly
four and a half hours; "Maximum size is not set." was still being logged 2h40m
AFTER the grab, so no ceiling existed to be enforced at 06:52.

And the 42,341,133,540-byte file in the folder today was never grabbed at all -
see RESIDUAL RISK item 4. It is a Tdarr output. Its size is not evidence about
LEVER 1 in either direction.

VERIFICATION SURFACE - this script is a WRITE, the gate is elsewhere
--------------------------------------------------------------------
scripts/smoke-test.sh check 13o "brdisk-block" re-reads config/indexer and
qualityprofile live and FAILS if either lever is missing. It is the twin of
13n (remux cap) and 13f (no-4k). If you widen INSTANCES here, widen 13o with
it.

Run on the seedbox (reads ~/secrets/<arr>.{key,port,urlbase}). Or pipe via
SSH:  sshm "python3 -" < scripts/configure/59-brdisk-block.py
Self-contained by design (no lib.* imports) precisely so that pipe works.

EXIT: 0 clean, 1 if any GET/PUT failed or any write did not survive its
re-read.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


# Per-instance policy. max_size_mb None means "do not touch maximumSize" - see
# the SCOPE block for why Sonarr is None rather than some large number.
INSTANCES = {
    "radarr":  {"api": "v3", "max_size_mb": 25000},
    "radarr2": {"api": "v3", "max_size_mb": 42000},
    "sonarr":  {"api": "v3", "max_size_mb": None},
}

# The score recyclarr/TRaSH itself writes for this format. Matching it exactly
# is what makes writing to a recyclarr-managed profile a no-op instead of a
# fight. Do not "improve" this number.
BLOCK_SCORE = -10000

# The TRaSH custom format already installed on radarr, radarr2 and sonarr.
DISC_CF_NAME = "BR-DISK"

TIMEOUT = 15


def secret(name: str) -> str:
    with open(os.path.expanduser("~/secrets/" + name)) as f:
        return f.read().strip()


def secret_or(name: str, fallback: str) -> str:
    try:
        return secret(name)
    except FileNotFoundError:
        return fallback


# ---------------------------------------------------------------------------
# Pure policy logic. Everything in this block is offline and side-effect-free
# apart from the in-place mutation it advertises, so
# tests/unit/test_remux_cap.py can drive it with no box and no network.
# ---------------------------------------------------------------------------
def needs_size_cap(current_mb, target_mb) -> bool:
    """True iff maximumSize must be rewritten.

    Rewrite when it is 0/None (Radarr's "not set" = unlimited) or when it is
    LOOSER than the target. Never when it is already tighter: an operator who
    hand-set 20000 has made a stricter choice than policy, and an enforcer that
    relaxes limits is an enforcer that can be used to defeat the policy.
    """
    if target_mb is None:
        return False
    try:
        cur = int(current_mb or 0)
    except (TypeError, ValueError):
        # An unparseable value is not a limit. Treat it as unset.
        return True
    return cur == 0 or cur > int(target_mb)


def apply_size_cap(cfg: dict, target_mb) -> dict:
    """Set config/indexer.maximumSize in place. Returns a report dict."""
    before = cfg.get("maximumSize")
    report = {"before": before, "after": before, "changed": False, "reason": ""}
    if target_mb is None:
        report["reason"] = "policy: this instance is deliberately uncapped"
        return report
    if not needs_size_cap(before, target_mb):
        report["reason"] = "already capped at or below policy"
        return report
    cfg["maximumSize"] = int(target_mb)
    report["after"] = int(target_mb)
    report["changed"] = True
    return report


def format_score(profile: dict, cf_name: str):
    """Current score of one custom format on one profile, or None if the
    profile carries no formatItem with that name."""
    for item in profile.get("formatItems") or []:
        if item.get("name") == cf_name:
            return item.get("score")
    return None


def min_format_score_is_dead(min_score, block_score: int) -> bool:
    """A -10000 score only rejects if minFormatScore sits ABOVE it.

    Radarr rejects a release when its total format score < minFormatScore. Set
    minFormatScore to -10000 or lower and the block silently stops blocking
    while every field still reads correct in the UI. That is the failure this
    predicate exists to catch; it is not hypothetical, it is one number away at
    all times.
    """
    try:
        return int(min_score or 0) <= int(block_score)
    except (TypeError, ValueError):
        return False


def profile_quality_leaves(items, group_id=None) -> list:
    """Flatten a profile's items[] to (leaf, enclosing_group_id) pairs.

    items[] is a NESTED tree: a group entry carries its own items[] list and no
    quality of its own. A FLAT walk silently misses any quality that lives
    inside a group. That is not hypothetical - measured on this box 2026-08-19
    while building 58's gate, sonarr2 scored flat=0 / recursive=1 remux
    entries, so the flat walk called an instance clean while it allowed a
    remux tier. Same tree, same trap, so the same recursion.

    The group id is carried out with each leaf because `cutoff` names EITHER a
    quality id or a GROUP id (group ids start at 1000), and a profile whose
    cutoff is not allowed is rejected by the API. See ban_disc_qualities.
    """
    out = []
    for it in items or []:
        if it.get("items"):
            out.extend(profile_quality_leaves(it.get("items"), it.get("id")))
        else:
            out.append((it, group_id))
    return out


def ban_disc_qualities(profile: dict) -> dict:
    """THIRD REPAIR: set allowed=false on every disc-class quality in place.

    WHY THIS EXISTS AT ALL - a false premise found by review 2026-08-20.
    An earlier draft of this header asserted "BR-DISK is allowed on NO profile,
    on either Radarr" and used that to argue a profile edit was unnecessary.
    Measured live the same day, that was WRONG on two instances:

        radarr2 profile 1 "Any"        BR-DISK  allowed=True   3 of 6 movies
        sonarr  profile 6 "HD 720p/1080p"  Raw-HD  allowed=True  ALL 36 series

    Raw-HD is Sonarr's disc-class tier - this very file already treats it as
    one in is_disc_quality() and reports it in disc_offenders(). Leaving a
    quality the script itself calls unplayable switched ON, on the profile that
    carries 100 percent of the TV library, is a hole no amount of custom-format
    scoring covers: the profile gate is UNCONDITIONAL and fires before scores
    are ever summed.

    THE CUTOFF INTERLOCK IS LOAD-BEARING. Radarr/Sonarr reject (HTTP 400) any
    profile whose `cutoff` names a quality that is not allowed. Banning a
    cutoff quality would fail the PUT and take the OTHER repairs in the same
    PUT down with it. Measured on the two live profiles above, neither cutoff
    is a disc tier (radarr2 p1 cutoff=20 Bluray-480p, sonarr p6 cutoff=1002
    "WEB 1080p"), so the interlock never fires today - it is here so a future
    profile whose cutoff IS a disc tier gets REPORTED instead of silently
    breaking every other repair on that instance.
    """
    banned = []
    blocked = []
    cutoff = profile.get("cutoff")
    for leaf, group_id in profile_quality_leaves(profile.get("items")):
        q = leaf.get("quality") or {}
        name = q.get("name")
        if not is_disc_quality(name) or not leaf.get("allowed"):
            continue
        # The cutoff may name this leaf directly, or the group holding it.
        if cutoff is not None and cutoff in (q.get("id"), group_id):
            blocked.append(str(name))
            continue
        leaf["allowed"] = False
        banned.append(str(name))
    return {"banned": sorted(banned), "blocked_by_cutoff": sorted(blocked)}


def apply_disc_block(profile: dict, cf_name: str = DISC_CF_NAME,
                     block_score: int = BLOCK_SCORE) -> dict:
    """Arm the disc block on ONE profile in place. Returns a report dict.

    THREE independent repairs, any one of which can be the only change:
      * disallow every disc-class quality outright (the unconditional gate);
      * score the custom format at block_score (only if it is currently
        higher, so a hand-set -20000 is left alone);
      * lift minFormatScore back to 0 if it has been pushed to or below
        block_score, which would neutralise the score.

    The quality ban runs FIRST and runs unconditionally, deliberately BEFORE
    the "no such custom format here" early return below. A profile that allows
    BR-DISK is a hole whether or not it happens to carry the TRaSH custom
    format, and gating the ban on the format's presence would have left
    exactly that hole open on any profile recyclarr has not touched.
    """
    report = {
        "profile_id": profile.get("id"),
        "profile_name": profile.get("name"),
        "score_before": None,
        "score_after": None,
        "score_changed": False,
        "min_before": profile.get("minFormatScore"),
        "min_after": profile.get("minFormatScore"),
        "min_repaired": False,
        "qualities_banned": [],
        "qualities_blocked_by_cutoff": [],
        "changed": False,
        "absent": False,
        "reason": "",
    }

    ban = ban_disc_qualities(profile)
    report["qualities_banned"] = ban["banned"]
    report["qualities_blocked_by_cutoff"] = ban["blocked_by_cutoff"]

    current = format_score(profile, cf_name)
    report["score_before"] = current
    report["score_after"] = current

    if current is None:
        # No format here means no CUSTOM-FORMAT gate of ours here, so the
        # score and minFormatScore repairs have nothing to act on. Returning
        # early keeps this script from making an unrelated scoring change on a
        # profile it has no stake in. It does NOT skip the quality ban above -
        # `changed` still carries it, so the caller still PUTs this profile.
        report["absent"] = True
        report["reason"] = "profile carries no '" + cf_name + "' custom format"
        report["changed"] = bool(report["qualities_banned"])
        return report
    if int(current) > block_score:
        for item in profile.get("formatItems") or []:
            if item.get("name") == cf_name:
                item["score"] = block_score
        report["score_after"] = block_score
        report["score_changed"] = True
    else:
        report["reason"] = "already blocked at " + str(current)

    if min_format_score_is_dead(profile.get("minFormatScore"), block_score):
        profile["minFormatScore"] = 0
        report["min_after"] = 0
        report["min_repaired"] = True

    report["changed"] = bool(report["score_changed"] or report["min_repaired"]
                             or report["qualities_banned"])
    return report


def release_exceeds_cap(size_bytes, cap_mb) -> bool:
    """Reproduces Radarr's MaximumSizeSpecification decision.

    Radarr computes `MaximumSize.Megabytes()` - which is MiB, 1024*1024, NOT
    1000*1000 - and rejects when `release.Size > maxSize`. Strictly greater, so
    a release exactly on the ceiling is accepted. The unit is the whole reason
    this exists as a named function: Radarr's own UI prints the result as "GB"
    while meaning GiB, and a ~7 percent error in that conversion is invisible
    right up until it silently rejects a wanted release.
    """
    if not cap_mb:
        return False
    try:
        return int(size_bytes or 0) > int(cap_mb) * 1024 * 1024
    except (TypeError, ValueError):
        return False


def is_disc_quality(name) -> bool:
    """True for the quality names that mean 'this is a disc, not a video file'.

    Substring on 'disk'/'disc' rather than a literal list because Radarr ships
    BR-DISK today and the family (BD-DISK, UHD-DISK) is the same problem.
    Raw-HD is named separately: it is a raw transport stream, not a disc, but
    it is the same unplayable-container class and Radarr's own
    RawDiskSpecification treats the two together.
    """
    if not name:
        return False
    low = str(name).lower()
    return "disk" in low or "disc" in low or low == "raw-hd"


def disc_offenders(movies: list, cap_mb=None) -> list:
    """Read-only library scan: movies whose CURRENT file violates policy.

    This is the detection half. Prevention is entirely grab-time, so a file
    that already landed can only be found by looking for it - nothing in the
    grab path will ever mention it again.

    Two independent reasons, reported together because they are the same
    symptom (a file the low-bandwidth client cannot play):
      * disc-class quality (BR-DISK / Raw-HD) - what actually landed;
      * larger than the instance ceiling - a file that policy would refuse to
        grab today, so it is out of policy even if its quality name is fine.
    """
    out = []
    for mv in movies or []:
        if not mv.get("hasFile"):
            continue
        mf = mv.get("movieFile") or {}
        q = ((mf.get("quality") or {}).get("quality") or {})
        reasons = []
        if is_disc_quality(q.get("name")):
            reasons.append("disc-class quality")
        if release_exceeds_cap(mf.get("size"), cap_mb):
            reasons.append("over the " + str(cap_mb) + " MiB ceiling")
        if reasons:
            out.append({
                "id": mv.get("id"),
                "title": mv.get("title"),
                "quality": q.get("name"),
                "size": mf.get("size"),
                "reasons": reasons,
            })
    return out


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------
def _req(url: str, key: str, data=None, method="GET"):
    headers = {"X-Api-Key": key}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=TIMEOUT).read()


def _get(url: str, key: str):
    return json.loads(_req(url, key))


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def cap_instance_size(base_url: str, key: str, arr: str, target_mb) -> int:
    """LEVER 1. Returns a failure count (0 or 1)."""
    if target_mb is None:
        print("[" + arr + "] maximumSize: left unset by policy (see SCOPE)")
        return 0
    url = base_url + "/config/indexer"
    try:
        cfg = _get(url, key)
    except Exception as exc:
        _err("[" + arr + "] GET config/indexer failed: " + str(exc))
        return 1

    rep = apply_size_cap(cfg, target_mb)
    if not rep["changed"]:
        print("[" + arr + "] maximumSize " + str(rep["before"])
              + " MiB unchanged (" + rep["reason"] + ")")
        return 0

    try:
        _req(url + "/" + str(cfg.get("id", 1)), key, data=cfg, method="PUT")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        _err("[" + arr + "] PUT config/indexer failed: " + str(exc.code)
             + " " + body)
        return 1
    except Exception as exc:
        _err("[" + arr + "] PUT config/indexer failed: " + str(exc))
        return 1

    # RE-READ. A 200 from an *arr is not evidence the value stuck.
    try:
        after = _get(url, key).get("maximumSize")
    except Exception as exc:
        _err("[" + arr + "] re-read config/indexer failed: " + str(exc))
        return 1
    if int(after or 0) != int(target_mb):
        _err("[" + arr + "] maximumSize did NOT stick: wrote " + str(target_mb)
             + ", re-read " + str(after))
        return 1
    print("[" + arr + "] maximumSize " + str(rep["before"]) + " -> "
          + str(after) + " MiB (grab-time ceiling ARMED)")
    return 0


def block_instance_formats(base_url: str, key: str, arr: str) -> tuple:
    """LEVER 2. Returns (profiles_changed, failures, absent_profile_ids)."""
    url = base_url + "/qualityprofile"
    try:
        profiles = _get(url, key)
    except Exception as exc:
        _err("[" + arr + "] GET qualityprofile failed: " + str(exc))
        return (0, 1, [])

    changed = 0
    failures = 0
    absent = []
    carriers = 0

    for p in profiles:
        rep = apply_disc_block(p)
        pid = rep["profile_id"]
        pname = rep["profile_name"] or "?"
        if rep["absent"]:
            absent.append(str(pid))
        else:
            carriers += 1
        if rep["qualities_blocked_by_cutoff"]:
            # Not fatal, but never silent: this profile still allows a disc
            # tier and the script cannot fix it without also moving the
            # cutoff, which is a policy decision it must not make alone.
            _err("[" + arr + "] profile '" + str(pname) + "' (id=" + str(pid)
                 + ") still allows "
                 + ",".join(rep["qualities_blocked_by_cutoff"])
                 + " - it is the profile CUTOFF; change the cutoff by hand "
                   "first, then re-run")
            failures += 1
        # NOTE: no `absent` early-continue here. A profile with no BR-DISK
        # custom format can still have had a disc QUALITY banned above, and
        # that change only reaches the box if this loop PUTs it.
        if not rep["changed"]:
            continue
        try:
            _req(url + "/" + str(pid), key, data=p, method="PUT")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:200]
            _err("[" + arr + "] PUT profile '" + str(pname) + "' failed: "
                 + str(exc.code) + " " + body)
            failures += 1
            continue
        except Exception as exc:
            _err("[" + arr + "] PUT profile '" + str(pname) + "' failed: "
                 + str(exc))
            failures += 1
            continue
        msg = "[" + arr + "] profile '" + str(pname) + "' (id=" + str(pid) + "):"
        if rep["qualities_banned"]:
            msg += (" DISALLOWED " + ",".join(rep["qualities_banned"]))
        if rep["score_changed"]:
            msg += (" " + DISC_CF_NAME + " " + str(rep["score_before"])
                    + " -> " + str(rep["score_after"]))
        if rep["min_repaired"]:
            msg += (" minFormatScore REPAIRED " + str(rep["min_before"])
                    + " -> " + str(rep["min_after"]))
        print(msg)
        changed += 1

    # RE-READ every profile once, after all writes. Verifies the ones we wrote
    # AND catches a profile some other process reverted mid-run.
    try:
        for p in _get(url, key):
            # The quality ban is verified for EVERY profile, with no
            # custom-format precondition - it is the unconditional gate and it
            # is the one an operator can flip back with a single UI checkbox.
            still = sorted({
                str((leaf.get("quality") or {}).get("name"))
                for leaf, _ in profile_quality_leaves(p.get("items"))
                if leaf.get("allowed")
                and is_disc_quality((leaf.get("quality") or {}).get("name"))
            })
            if still:
                _err("[" + arr + "] profile '" + str(p.get("name")) + "' (id="
                     + str(p.get("id")) + ") STILL ALLOWS " + ",".join(still))
                failures += 1
            score = format_score(p, DISC_CF_NAME)
            if score is None:
                continue
            if int(score) > BLOCK_SCORE:
                _err("[" + arr + "] profile '" + str(p.get("name")) + "' (id="
                     + str(p.get("id")) + ") did NOT stick: " + DISC_CF_NAME
                     + " re-read as " + str(score))
                failures += 1
            if min_format_score_is_dead(p.get("minFormatScore"), BLOCK_SCORE):
                _err("[" + arr + "] profile '" + str(p.get("name")) + "' (id="
                     + str(p.get("id")) + ") minFormatScore "
                     + str(p.get("minFormatScore"))
                     + " neutralises the block")
                failures += 1
    except Exception as exc:
        _err("[" + arr + "] re-read qualityprofile failed: " + str(exc))
        failures += 1

    # LEVER 2 CAN GO MISSING WITHOUT ANYTHING ELSE FAILING. Every per-profile
    # check above is written as "if the format is here, assert its score", so
    # if the BR-DISK custom format is ever deleted, or recyclarr prunes it off
    # every profile, the loops iterate, match nothing, and this function
    # reports a clean run. Zero carriers on an instance that is IN INSTANCES
    # means the title gate is gone, not that it passed.
    if carriers == 0:
        _err("[" + arr + "] NO profile carries the '" + DISC_CF_NAME
             + "' custom format - LEVER 2 is absent, not passing")
        failures += 1

    return (changed, failures, absent)


def report_disc_files(base_url: str, key: str, arr: str, cap_mb=None) -> None:
    """Read-only detection pass. Never fatal - prevention is grab-time, and a
    file that already landed is a remediation task, not a config error."""
    try:
        movies = _get(base_url + "/movie", key)
    except Exception as exc:
        print("[" + arr + "] library scan skipped: " + str(exc))
        return
    bad = disc_offenders(movies, cap_mb)
    if not bad:
        print("[" + arr + "] library scan: no out-of-policy files present")
        return
    print("[" + arr + "] library scan: " + str(len(bad))
          + " out-of-policy file(s) ALREADY PRESENT (grab-time gates cannot "
            "retroactively remove these):")
    for row in bad:
        gib = ""
        try:
            gib = " " + str(round(int(row["size"]) / 1073741824.0, 2)) + " GiB"
        except (TypeError, ValueError):
            pass
        print("    movieId=" + str(row["id"]) + " [" + str(row["quality"])
              + "]" + gib + " " + str(row["title"])
              + " -- " + ", ".join(row["reasons"]))


def main() -> int:
    failures = 0
    profiles_changed = 0

    for arr, policy in INSTANCES.items():
        try:
            key = secret(arr + ".key")
            port = secret(arr + ".port")
        except FileNotFoundError as exc:
            print("[" + arr + "] skipped: " + str(exc))
            continue
        # urlbase files carry NO leading slash on this box.
        base = secret_or(arr + ".urlbase", arr).strip("/")
        base_url = ("http://127.0.0.1:" + port + "/" + base
                    + "/api/" + policy["api"])

        failures += cap_instance_size(base_url, key, arr, policy["max_size_mb"])
        ch, fa, absent = block_instance_formats(base_url, key, arr)
        profiles_changed += ch
        failures += fa
        if absent:
            print("[" + arr + "] no '" + DISC_CF_NAME + "' custom format on "
                  "profile(s) " + ",".join(absent)
                  + " - nothing to score there")
        if arr.startswith("radarr"):
            report_disc_files(base_url, key, arr, policy["max_size_mb"])

    print()
    print("Scope: " + ", ".join(sorted(INSTANCES))
          + " (sonarr2 excluded - no BR-DISK custom format; see header)")
    print("Profiles armed this run: " + str(profiles_changed))
    if failures:
        print("Failures: " + str(failures))
    # A re-run on an already-armed stack prints only "unchanged" lines and
    # exits 0. That IS the idempotence check - there is no separate verify step
    # to forget.
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
