#!/usr/bin/env python3
"""qflix-reaper — add-date autodelete for the QFlix Plex libraries.

WHY this exists: Maintainerr's 60-day "delete after N days" rule went broken on
the seedbox and was silently retaining items (or, worse, threatening to delete
the wrong ones with no audit trail). This script replaces that single rule with
a small, auditable, stdlib-only job whose entire center of gravity is the SAFETY
ENVELOPE around deleting real, irreplaceable media.

WHAT IT DELETES: items in the four QFlix Plex libraries

    'QFlix - Movies'        -> radarr   (movie,  match tmdbId)
    'QFlix - Anime Movies'  -> radarr2  (movie,  match tmdbId)
    'QFlix - TV'            -> sonarr   (series, match tvdbId)
    'QFlix - Anime'         -> sonarr2  (series, match tvdbId)

whose grading clock (see PER-FILE RETENTION below) is STRICTLY older than
--threshold-days (default DEFAULT_THRESHOLD_DAYS, currently 45, clamped by a
raise-only MIN_FILE_AGE_FLOOR_DAYS floor — see REQ-CLAMP), that are not
excluded, and that POSITIVELY resolve to exactly one *arr id. Resolution is
mandatory: an item that does not map to a single Radarr movie / Sonarr series is
NEVER deleted — it is skipped and logged UNRESOLVED. Such an item is an "orphan".

PER-FILE RETENTION (2026-09-13, spec docs/superpowers/specs/2026-09-12-reaper-
d-per-file-retention-spec.md — supersedes whole-container grading). The unit of
retention is the FILE, never the series: a movie's one movieFile, or EACH
Sonarr episodeFile independently. The Plex SHOW's own container addedAt (R-4)
and any file's mtime (R-3, Tdarr's 24/7 re-encodes rewrite it) are REJECTED as
grading inputs outright — this is the exact defect that reaped Futurama 70
minutes after it was requested, because a show's container addedAt is stamped
once when the show first enters the library and never moves again no matter
how many new files land under it later. Instead, each file is graded on its
own Plex LEAF addedAt corroborated by the *arr's own episodeFile/movieFile
dateAdded; where the two disagree by more than 24h the NEWER wins and the
disagreement is recorded. A file whose clock cannot be determined AT ALL is
WITHHELD — counted, named, logged loudly — never deleted (fail closed).
Deleting an episode file unmonitors its episode in the SAME operation (a
monitored episode with no file is re-grabbed by Sonarr's own wanted/missing
sweep — proven live, not assumed). A series record is removed only when it is
`ended`, carries zero files, and does NOT carry the `permanent` tag (set by the
separate scripts/maint/qflix-permanent.py, per the one-concern-one-module law)
— an unfinished ("continuing") show's record always survives at zero files so
future episodes keep arriving without a re-request.

THREE CAUSES, and the triage differs, so identify the cause before acting:
  1. GHOST ITEM — the files are gone and no *arr record exists. Fix: delete the
     Plex metadata. (Frieren, 2026-07-14.)
  2. NO EXTERNAL GUIDS — Plex never matched the item at all, so there is no id
     to resolve against. Fix: match it.
  3. WRONG MATCH — file and *arr record are both healthy, and Plex HAS a tmdb
     guid; it is simply the wrong film. Fix: re-match, never delete.
     ('The Furious' 2026, resolved 2026-08-23: Plex held tmdb://1510055 while
     Radarr held tmdb://1280738 for the same file.)
Cause 3 is the one that reads like the others and is not: "the guid lacks a tmdb
id" does NOT discriminate it, because the guid has one. The test that does is
whether the tmdb Plex holds resolves to a *arr record for THAT file — which is
exactly what resolution already does, and exactly why it came back UNRESOLVED.
Re-match with:
    GET  /library/metadata/<rk>/matches?manual=1&title=tmdb-<correct-id>
    PUT  /library/metadata/<rk>/match?guid=<guid from the search result>

The *arr delete is the
authority; Plex is then refreshed and its trash emptied; finally Seerr is
reconciled so deleted media becomes re-requestable.

ORPHAN GRACE (so one stuck item can't red the run forever — the 2026-07-14
Frieren incident): an orphan is tracked in a durable state file and put on a
time-grace. A FRESH orphan (first seen <= --orphan-grace-hours ago, default 24)
reds the run (exit 1) so the operator learns of newly-stranded media. A KNOWN
orphan (older) no longer reds — the run goes green and the orphan is surfaced via
--json, the durable log, and a throttled --orphan-remind-days WARN (default 7).
The safety rail (an orphan is NEVER deleted) is absolute either way. See
docs/superpowers/specs/2026-07-14-reaper-orphan-grace-design.md.

DRY-RUN IS THE DEFAULT. With no flags the reaper enumerates, resolves,
classifies, prints the plan + totals, and MUTATES NOTHING — no DELETE, no Plex
refresh/emptyTrash, no Seerr delete, no manifest file. The systemd unit ships in
this safe mode on purpose.

HOW TO ARM IT: add --execute. That is the ONLY flag that issues real deletions.
The operator edits ExecStart (or a drop-in) on manitoba-maint-reaper.service to
add --execute once they trust the dry-run plan.

CAPS (both default-on, both overridable with --force):
  --max-items N   per-run RATE LIMIT on deletions (default 50). A backlog larger
                  than N does NOT abort — the reaper deletes the OLDEST N this run
                  (addedAt ascending) and DEFERS the rest to the next run, so a
                  space-constrained box always makes forward progress. The runaway
                  guard (never delete > N in one run) still holds. This is ONE
                  SHARED budget across BOTH mutation classes: file/movie deletes
                  AND P-4 series-RECORD removals count against the same N. Files
                  ALWAYS take the budget first (they are picked oldest-first as
                  before); whatever is left over is what P-4 record removals may
                  spend this run (oldest-container-first, a tiebreak only — see
                  series_removal_state's container_added_at comment), with the
                  remainder deferred and counted exactly like an overflowing file
                  backlog is (2026-09-13 fix: P-4 previously had ZERO bound at all
                  and could fire every eligible removal — measured 200 in one run
                  — regardless of --max-items).
  --max-pct  P    per-library TRIPWIRE: if candidates in any one library exceed P%
                  of that library's total item count, abort the WHOLE run before
                  any mutation (default 30). Prod disables it with --max-pct 100.
                  For TV/anime libraries the denominator is the EPISODE FILE
                  count (per_lib_totals accumulates grade_series_files'
                  total_files per series, i.e. every episode file Sonarr
                  currently has on record for that library) — NOT the number of
                  Plex show items. This is a deliberate, accepted builder
                  decision: R-1 makes the episode FILE the unit of retention for
                  TV, so the tripwire's percentage is measured in that same unit
                  ("N of M episode files" reads the same as "N of M movie files"
                  for Movies), not in a unit (shows) the rest of the module
                  never grades against. P-4 series-record removals do NOT count
                  against max-pct (only against the shared --max-items budget
                  above) — a record removal is a bookkeeping cleanup of an
                  already-empty series, not itself a file-reclaiming mutation.
A max-pct trip aborts BEFORE any mutation with exit code 2 and pages the operator.
A max-items overflow just defers the excess (logged WARNING, exit unaffected).
--force overrides BOTH caps (logged WARNING) but does NOT imply --execute.

EXCLUSIONS: --exclude-file (default scripts/maint/qflix-reaper.exclude next to
this script). Lines: `tmdb:<id>`, `tvdb:<id>`, `plex:<ratingKey>`, or a bare
`title text` (case-insensitive). '#' comments and blank lines ignored, whitespace
stripped. A missing exclude file warns and proceeds with an empty set.

MANIFEST: on --execute, BEFORE the first DELETE, a JSON audit record of every
intended deletion is written to --manifest-dir (default ~) as
qflix-reaper-<UTC-YYYYMMDD-HHMMSS>.json. In dry-run no manifest is written.
Includes series_removals[] — every P-4 series-record removal SCHEDULED this
run (post shared-budget capping, see --max-items above) — alongside the
existing candidates[]/series_files[] file/movie sections (2026-09-13 fix:
P-4 removals were previously absent from this audit trail entirely).

EXIT CODES:
  0  clean (dry-run plan printed, or execute with zero failures)
  1  partial failure (a per-item resolve/delete/plex/seerr step failed; run
     still completed — re-running self-heals because candidates are re-derived
     from live Plex+arr each time)
  2  cap trip (max-pct exceeded without --force) — aborted, no mutation
     (max-items overflow does NOT cause exit 2 — it defers the excess and proceeds)
  3  fatal (could not read Plex creds / talk to Plex at all)

Notify + Kuma are best-effort and never raise into the main flow. lib.notify is
imported lazily and guarded so an absent `requests` degrades to a logged no-op.

Python 3.9 on the box: no f-string backslashes, no match-statement, stdlib only
(urllib/json/argparse/time/datetime/os/sys/pathlib/socket). `requests` may only
appear transitively via the guarded lib.notify import.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Make sibling lib/ importable (scripts/maint/lib) when run as a script, and
# the MCP arr_client (scripts/mcp/lib) for Radarr/Sonarr. Mirrors the canary's
# sys.path nudge so `from lib.notify import notify` resolves at runtime.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent                       # scripts/maint
_REPO_ROOT = _HERE.parent.parent                              # repo root
_MCP_DIR = _REPO_ROOT / "scripts" / "mcp"                     # owns lib/arr_client.py
for _p in (str(_HERE), str(_MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.secrets import secrets_dir, read_secret  # noqa: E402  (after sys.path setup)

# ---------------------------------------------------------------------------
# Fixed library -> arr mapping. Anime instances may be empty; that is normal
# and must not error. version is v3 for all four.
# ---------------------------------------------------------------------------
LIBRARIES = [
    {"plex": "QFlix - Movies",       "slug": "radarr",  "kind": "movie",  "idkey": "tmdb"},
    {"plex": "QFlix - Anime Movies", "slug": "radarr2", "kind": "movie",  "idkey": "tmdb"},
    {"plex": "QFlix - TV",           "slug": "sonarr",  "kind": "series", "idkey": "tvdb"},
    {"plex": "QFlix - Anime",        "slug": "sonarr2", "kind": "series", "idkey": "tvdb"},
]

ARR_VERSION = "v3"
# Retention window, days since Plex addedAt. Operator decision 2026-07-31:
# 60 -> 45.
#
# Why it moved. Disk sat at 82.1% (2294 of 2794 GB) and the 60-day window was
# releasing NOTHING -- measured that day, zero items in any library were older
# than 60 days by addedAt, because the library had been bulk-loaded: ~1593 GB
# arrived inside a 16-day window 14-30 days prior. At the then-current ingest of
# ~32 GB/day the remaining 500 GB of headroom was ~15 days out, while the first
# meaningful reap under a 60-day rule was ~30 days out. The window was not wrong
# in steady state (32 GB/day x 60d ~ 1900 GB ~ 69% of quota, which fits); it
# simply could not respond to a burst before the quota did.
#
# 45 was chosen over the more aggressive options as the conservative step: it
# frees ~124 GB now rather than the ~706 GB a 30-day window would. That is
# roughly 4 days of headroom, so this DEFERS the wall rather than removing it --
# recorded here so the next person does not read 45 as "solved".
#
# This is policy and lives in git deliberately. The on-box drop-in carries only
# the arming flags (--execute --max-pct 100) so that a repo clone cannot delete
# anything; a retention VALUE hidden there would leave the repo describing a
# window nobody runs -- the exact class the deploy-drift canary now exists to
# catch.
DEFAULT_THRESHOLD_DAYS = 45
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_PCT = 30
DAY_SECONDS = 86400

# ---------------------------------------------------------------------------
# REQ-CLAMP (spec docs/superpowers/specs/2026-09-12-reaper-d-per-file-retention-
# spec.md section 5, carried verbatim from the superseded grading spec). The
# minimum-age floor a --threshold-days may ever effectively use is a MODULE
# CONSTANT, never just a CLI default -- a default can be overridden to zero by
# a typo or a bad drop-in; a floor enforced in code cannot. The flag is
# RAISE-ONLY: max(FLOOR, requested). A request below the floor is CLAMPED UP,
# loudly (WARN naming both values), and this is NEVER fatal -- a mistaken
# --threshold-days=0 must not crash the box's autonomous 90% trigger, it must
# refuse to be as destructive as asked.
#
# WHY THIS IS ITS OWN FUNCTION, NOT INLINE IN run(). The last attempt at this
# exact requirement shipped a floor that only worked for a literal 0.0 -- the
# one value its own test happened to pin -- and silently let -1, 0 (int), and
# 0.5 all sail through unclamped. A free-standing, directly-unit-testable
# function is what makes "does -1 clamp" a one-line assertion instead of a
# full run() integration test that could just as easily be fooled the same
# way twice.
MIN_FILE_AGE_FLOOR_DAYS = 45


def clamp_threshold_days(requested_days):
    """Return (effective_days, was_clamped). effective_days is
    max(MIN_FILE_AGE_FLOOR_DAYS, requested_days); was_clamped is True iff that
    raised the value. requested_days may be int OR float -- --threshold-days
    accepts fractional days (0.5) specifically so this clamp has something to
    catch between 0 and 1."""
    effective = max(MIN_FILE_AGE_FLOOR_DAYS, requested_days)
    return effective, (effective != requested_days)


# R-2: Plex leaf addedAt and the *arr's own dateAdded are corroborating clocks
# for the SAME file. Measured 2026-09-12: they agree on 83/83 items live. Where
# they differ by more than this many seconds (24h), take the NEWER one and
# record the disagreement rather than silently trusting either -- a stale Plex
# leaf addedAt after a Radarr/Sonarr upgrade-replace is exactly the shape of
# clock error R-4 already proved this codebase is capable of shipping.
CLOCK_DISAGREEMENT_SECS = 24 * 3600

# P-1: the tag LABEL that exempts a series RECORD from removal (never its
# files -- see P-2). Resolved to an id PER INSTANCE via GET /tag, never
# hardcoded, because sonarr and sonarr2 are separate tag namespaces (measured
# 2026-09-12: sonarr's `permanent` is tag id 13, sonarr2's is tag id 1). Setting
# the tag is scripts/maint/qflix-permanent.py's job (P-3/P-5, a separate
# module per the compartmentalization law); the reaper only ever READS it.
PERMANENT_TAG_LABEL = "permanent"

# Seerr /api/v1/media page size for reconciliation. The list is paged; a single
# take=N would silently skip rows past N, leaving deleted titles stuck
# "Available" (not re-requestable). reconcile_seerr() loops until exhausted.
_SEERR_MEDIA_PAGE = 100

# Seerr MediaStatus values this module reasons about. PENDING/PROCESSING are
# in-flight requests a member is waiting on and are never reconciled away.
_SEERR_STATUS_PENDING = 2
_SEERR_STATUS_PROCESSING = 3
# BLOCKLISTED. An admin explicitly forbade this title, and a blocklisted title
# has NO backing *arr record BY DESIGN -- which is exactly the shape the "gone"
# check fires on. Deleting the media row cascades the blocklist row away
# (blocklist.mediaId is a CASCADE FK), silently un-blocking something a human
# deliberately blocked. Zero live rows today; latent, not theoretical.
_SEERR_STATUS_BLOCKLISTED = 6
# DELETED. What a reaped season lands on, and what strands it: Seerr shows the
# season as gone but offers no way to request it again.
_SEERR_STATUS_DELETED = 7

# Kuma push (bazarr2-sync model, reused verbatim in shape).
KUMA_BASE = os.environ.get("KUMA_BASE", "http://127.0.0.1:42005")
KUMA_PUSH_KEY = "qflix-reaper"           # key under ~/secrets/kuma-push-tokens.json

# Exit codes — distinct so the operator can tell a cap trip from a partial fail.
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CAP = 2
EXIT_FATAL = 3


# ===========================================================================
# Logging — print to stdout/stderr (systemd routes both to journal) AND append
# to a durable per-day logfile. The journal on this shared seedbox is
# permission-restricted + rotation-prone ("No entries" when debugging the
# 2026-07-13 failure), so a self-owned logfile is the only reliable record of
# why a run failed. File logging is BEST-EFFORT: any error degrades to
# journal-only and never breaks the delete job.
# ===========================================================================
_LOG_FH = None
_LOG_RETENTION_DAYS = 30


def _setup_file_log() -> None:
    """Open (append) today's reaper logfile and prune logs older than the
    retention window. Called once from main(); never raises."""
    global _LOG_FH
    try:
        log_dir = Path(os.environ.get(
            "QFLIX_REAPER_LOG_DIR",
            str(Path.home() / ".opt" / "maint" / "reaper"),
        ))
        log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        _LOG_FH = open(log_dir / ("reaper-" + day + ".log"), "a", encoding="utf-8")
        # Retention prune (best-effort): drop logfiles past the window.
        cutoff = datetime.now(timezone.utc).timestamp() - _LOG_RETENTION_DAYS * DAY_SECONDS
        for old in log_dir.glob("reaper-*.log"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    except Exception:
        _LOG_FH = None


def _file_log(line: str) -> None:
    """Append one timestamped line to the logfile if open. Never raises."""
    if _LOG_FH is None:
        return
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _LOG_FH.write(stamp + " " + line + "\n")
        _LOG_FH.flush()
    except Exception as _exc:
        sys.stderr.write("qflix-reaper.py: durable log write failed (best-effort, continuing): "
                         + repr(_exc) + "\n")


def log(msg: str) -> None:
    line = "[qflix-reaper] " + msg
    print(line, flush=True)
    _file_log(line)


def warn(msg: str) -> None:
    line = "[qflix-reaper] WARNING: " + msg
    print(line, file=sys.stderr, flush=True)
    _file_log(line)


# ===========================================================================
# Best-effort notify + Kuma (must never raise into the main flow).
# ===========================================================================
def _notify(msg: str, level: str = "info") -> None:
    """Discord via lib.notify (lazy + guarded). A missing `requests` (the box is
    stdlib-preferred and lib.notify top-imports requests) degrades to a logged
    no-op, exactly like flaresolverr-canary._notify. Never raises."""
    try:
        if str(_HERE) not in sys.path:
            sys.path.insert(0, str(_HERE))
        from lib.notify import notify
        notify(msg, level)
    except ImportError as exc:
        warn("notify unavailable (missing dep), continuing: " + str(exc))
    except Exception as exc:
        warn("notify failed (non-fatal): " + str(exc))


def _read_kuma_token() -> str:
    """Per-app Kuma push token from ~/secrets/kuma-push-tokens.json under the
    'qflix-reaper' key. Env override wins (bazarr2-sync convention). Best-effort."""
    env = os.environ.get("QFLIX_REAPER_KUMA_TOKEN")
    if env:
        return env
    try:
        path = secrets_dir() / "kuma-push-tokens.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get(KUMA_PUSH_KEY, "") or ""
    except Exception:
        return ""


def _push_kuma(status: str, msg: str) -> None:
    """Push a heartbeat to Kuma (stdlib urllib GET, bazarr2-sync shape). status is
    'up' or 'down'. Best-effort; swallows all errors."""
    token = _read_kuma_token()
    if not token:
        # Loud skip: a missing token means the monitor goes red on Kuma's
        # 25h watchdog with zero local trace — this exact silent gap red-
        # looped the monitor 3x (2026-07-13..15, 2026-07-19) before the
        # token was durably persisted into kuma-push-tokens.json.
        warn("no Kuma push token under '" + KUMA_PUSH_KEY
             + "' — heartbeat NOT pushed")
        return
    qs = urllib.parse.urlencode({"status": status, "msg": msg[:200]})
    url = KUMA_BASE + "/api/push/" + token + "?" + qs
    try:
        urllib.request.urlopen(url, timeout=5).read()
    except Exception as exc:
        warn("Kuma push failed (non-fatal): " + str(exc))


# ===========================================================================
# Run-lock — refuse concurrent --execute runs (a second overlapping run would
# double-DELETE -> 404s -> a spurious partial-failure page). flock auto-releases
# on process exit, so there is no stale-lock hazard. Where fcntl is unavailable
# (e.g. the Windows test host) this degrades to a no-op sentinel; real --execute
# only ever runs on the Linux seedbox.
# ===========================================================================
_LOCK_PATH = os.environ.get("QFLIX_REAPER_LOCK", "/tmp/qflix-reaper.lock")


def _acquire_run_lock():
    """Take an exclusive non-blocking lock. Returns an open file handle on
    success, the sentinel True where fcntl is unavailable, or None if the lock is
    already held by another run."""
    try:
        import fcntl
    except ImportError:
        return True
    try:
        fh = open(_LOCK_PATH, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (OSError, IOError):
        return None


def _release_run_lock(handle) -> None:
    """Release a lock from _acquire_run_lock (no-op for the sentinel/None)."""
    if handle is None or handle is True:
        return
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception as _exc:
        sys.stderr.write("qflix-reaper.py: run-lock release failed - run-lock degrades to a no-op: "
                         + repr(_exc) + "\n")
    try:
        handle.close()
    except Exception as _exc:
        sys.stderr.write("qflix-reaper.py: run-lock file close failed (best-effort, continuing): "
                         + repr(_exc) + "\n")


# ===========================================================================
# Exclusions
# ===========================================================================
def load_exclusions(path: Path):
    """Parse the exclude file into a set of normalized rule strings. Lenient:
    '#' comments + blank lines ignored, whitespace stripped. Forms:
      tmdb:<id> / tvdb:<id> / plex:<ratingKey> / bare title (case-insensitive).
    Missing file -> empty set + WARNING (not an error). Returns a set of rules:
      "tmdb:123", "tvdb:456", "plex:789", "title:some movie" (title lowercased)."""
    rules = set()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        warn("exclude file not found, proceeding with NO exclusions: " + str(path))
        return rules
    except Exception as exc:
        warn("could not read exclude file (" + str(exc) + "); NO exclusions: " + str(path))
        return rules
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if low.startswith("tmdb:") or low.startswith("tvdb:") or low.startswith("plex:"):
            prefix, _, val = line.partition(":")
            rules.add(prefix.strip().lower() + ":" + val.strip())
        else:
            rules.add("title:" + low)
    return rules


def is_excluded(item: dict, rules) -> bool:
    """True iff the item matches any exclusion rule (tmdb / tvdb / plex / title)."""
    if not rules:
        return False
    rk = item.get("ratingKey")
    if rk is not None and ("plex:" + str(rk)) in rules:
        return True
    tmdb = item.get("tmdbId")
    if tmdb is not None and ("tmdb:" + str(tmdb)) in rules:
        return True
    tvdb = item.get("tvdbId")
    if tvdb is not None and ("tvdb:" + str(tvdb)) in rules:
        return True
    title = item.get("title")
    if title and ("title:" + str(title).strip().lower()) in rules:
        return True
    return False


# ===========================================================================
# Orphan grace tracking — an item that ages past the threshold but resolves to
# NO unique *arr id is an "orphan" (three causes — ghost item, no guids, or a
# WRONG match that still carries a tmdb id; see the module docstring, and do not
# reach for delete before you know which).
# The safety rail (never delete an orphan) is absolute. The ALERTING, however,
# is graced: a fresh orphan reds the run like today so the operator learns of
# newly-stranded media; an orphan that has persisted past the grace window is
# reported green with a throttled weekly WARN reminder — so one stuck item can
# no longer red the reaper twice daily forever (the 2026-07-14 Frieren incident).
# See docs/superpowers/specs/2026-07-14-reaper-orphan-grace-design.md.
# ===========================================================================
def _orphan_key(item: dict) -> str:
    """Stable identity for an orphan, preferring external ids so it survives Plex
    ratingKey churn. series -> tvdb:<id>, movie -> tmdb:<id>, else plex:<rk>
    (the fallback covers items whose Plex metadata lacks external guids — a
    distinct UNRESOLVED cause that must still be tracked across runs)."""
    kind = item.get("kind")
    tvdb = item.get("tvdbId")
    tmdb = item.get("tmdbId")
    if kind == "series" and tvdb is not None:
        return "tvdb:" + str(tvdb)
    if kind == "movie" and tmdb is not None:
        return "tmdb:" + str(tmdb)
    return "plex:" + str(item.get("ratingKey"))


_ORPHAN_STATE_VERSION = 1


def _orphan_state_path(explicit=None) -> Path:
    """Resolve the orphan-state file: explicit flag > env > default beside the
    durable per-day logs."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("QFLIX_REAPER_ORPHAN_STATE")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint" / "reaper" / "orphan-state.json"


def _fmt_stamp(dt) -> str:
    """UTC, second precision — same shape as the durable-log timestamps."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(s):
    """Parse a _fmt_stamp string to an aware UTC datetime; None on anything odd."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _load_orphan_state(path: Path) -> dict:
    """Best-effort read -> {key: record}. ANY failure (missing / corrupt /
    unreadable) returns {} so every current orphan looks NEW and reds — we fail
    TOWARD alerting, never toward silence."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        orphans = data.get("orphans")
        return orphans if isinstance(orphans, dict) else {}
    except Exception:
        return {}


def _save_orphan_state(path: Path, orphans: dict) -> None:
    """Best-effort write. Never raises — mirrors the durable-log philosophy; a
    write failure just means next run re-observes and re-grades from scratch."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": _ORPHAN_STATE_VERSION, "orphans": orphans},
                       indent=2),
            encoding="utf-8",
        )
    except Exception as _exc:
        sys.stderr.write("qflix-reaper.py: audit manifest write failed (best-effort, continuing): "
                         + repr(_exc) + "\n")


def reconcile_orphans(current, now, grace_hours, remind_days, state_path=None,
                      emit_reminders=True):
    """Update the durable orphan-state and classify this run's orphans against a
    time-based grace clock.

    current: list of {key, title, library} observed this run.
    now:     aware UTC datetime.
    emit_reminders: True on the run that actually sends the WARN (execute). When
        False (dry-run) the grace clock still advances (first_seen/last_seen) but
        warn_due is always empty and last_warned is NOT stamped — so a dry-run
        can't silently swallow the reminder the execute run should fire.
    Returns (fresh, known, warn_due) — lists of info dicts
    {key, title, library, first_seen, age_hours}. warn_due is a subset of known
    (the ones whose weekly reminder came due this run). Orphans in prior state
    but absent from `current` are dropped (resolved -> forgotten, so a later
    re-appearance restarts the grace + alert cycle)."""
    path = _orphan_state_path(state_path)
    prior = _load_orphan_state(path)
    now_s = _fmt_stamp(now)
    grace_secs = grace_hours * 3600.0
    remind_secs = remind_days * DAY_SECONDS

    new_state = {}
    fresh, known, warn_due = [], [], []
    for o in current:
        key = o["key"]
        rec = dict(prior.get(key) or {})
        rec.setdefault("first_seen", now_s)   # stamped ONCE; never moved
        rec["last_seen"] = now_s
        rec["title"] = o.get("title")
        rec["library"] = o.get("library")

        first = _parse_stamp(rec["first_seen"]) or now
        age_secs = max(0.0, (now - first).total_seconds())
        info = {"key": key, "title": rec["title"], "library": rec["library"],
                "first_seen": rec["first_seen"],
                "age_hours": round(age_secs / 3600.0, 2)}

        if age_secs <= grace_secs:
            fresh.append(info)
        else:
            known.append(info)
            if emit_reminders:
                last_warned = _parse_stamp(rec.get("last_warned") or "")
                if last_warned is None or (now - last_warned).total_seconds() >= remind_secs:
                    warn_due.append(info)
                    rec["last_warned"] = now_s
        new_state[key] = rec

    _save_orphan_state(path, new_state)
    return fresh, known, warn_due


def _orphan_list(items, cap: int = 8) -> str:
    """Human-readable one-liner: "'Title' <Library> (aged Nh); ..." capped so a
    large backlog doesn't blow the notify/Kuma length budget."""
    parts = [repr(o.get("title")) + " <" + str(o.get("library")) + "> (aged " +
             str(int(o.get("age_hours", 0))) + "h)" for o in items[:cap]]
    if len(items) > cap:
        parts.append("+" + str(len(items) - cap) + " more")
    return "; ".join(parts)


def classify_run(operational_partial, fresh, known, warn_due):
    """Decide a run's outcome from operational failures + orphan classification.
    Returns (rc, severity, note):
      'error'   -> an operational failure OR a FRESH orphan (reds; EXIT_PARTIAL)
      'warning' -> only KNOWN orphans and the weekly reminder is DUE (green)
      'ok'      -> clean, or only known orphans not yet due (green)
    `note` is orphan-context text ('' when there are no orphans) for the operator
    message / Kuma / log. Operational-failure wording is owned by the caller
    (it already builds the deleted/failed summary)."""
    if fresh:
        note = str(len(fresh)) + " newly-stranded orphan(s): " + _orphan_list(fresh)
    elif known:
        note = str(len(known)) + " known orphan(s) still stranded: " + _orphan_list(known)
    else:
        note = ""
    if operational_partial or fresh:
        return EXIT_PARTIAL, "error", note
    if warn_due:
        return EXIT_OK, "warning", note
    return EXIT_OK, "ok", note


def _orphan_json(fresh, known):
    """Flatten the fresh/known orphan info dicts into a --json array, tagging each
    with its grace state so the dashboard/ops can surface stranded media."""
    return ([dict(o, state="fresh") for o in fresh] +
            [dict(o, state="known") for o in known])


# ===========================================================================
# Per-file grading clock (spec section 2, R-1..R-5). The unit of retention is
# the FILE, never the series/movie container. mtime and Plex CONTAINER addedAt
# are REJECTED as inputs here (R-3, R-4) -- R-4 is the exact defect that reaped
# Futurama 70 minutes after it was requested, because a show's own addedAt is
# stamped once when the show FIRST enters the library and never moves again no
# matter how many new files land under it later.
# ===========================================================================
def _norm_path(path):
    """Normalize a file path for the Plex<->*arr join. Both sides speak the
    identical /home/quadstronaut/media/... namespace on this box (the same
    join key arr-plex-parity.sh already uses live), so this is deliberately
    light-touch -- strip whitespace and flip any stray backslash, but do NOT
    case-fold (the filesystem is case-sensitive Linux) or resolve symlinks
    (that would silently paper over a real path mismatch instead of reporting
    it as withheld). Returns None for a falsy input so callers can treat "no
    path" and "path that fails to join" identically."""
    if not path:
        return None
    return str(path).strip().replace("\\", "/")


def _parse_arr_timestamp(raw):
    """Parse a Radarr/Sonarr `dateAdded` string to a UTC epoch int, or None if
    unparseable/absent. These come back as '2026-08-20T05:18:19Z', sometimes
    with a fractional-second suffix of UNPREDICTABLE width (observed both 3-
    and 7-digit) that stdlib's datetime.fromisoformat() rejects outright on
    anything but 3 or 6 digits. Truncating at the first '.' and parsing
    to-the-second sidesteps that entirely -- the exact shape already proven
    live against this same field by arr-plex-parity.sh's parse_iso_epoch,
    reused here rather than reinvented."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1]
    s = s.split(".")[0]
    try:
        return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
                   .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def grade_file_clock(plex_added_at, arr_date_added_raw):
    """Decide ONE file's grading clock per R-2/R-3/R-4.

    plex_added_at: Plex LEAF addedAt as an epoch int, or None/0/falsy if there
        is no Plex-side clock for this file (e.g. the join by path failed).
    arr_date_added_raw: the *arr's raw dateAdded string for the same file
        (episodeFile.dateAdded / movieFile.dateAdded), or None.

    Returns (gradedAt, gradeSource, clockDisagreementSec):
      gradedAt is an epoch int, or None if NEITHER clock resolved -- the
        caller MUST withhold the file in that case (fail closed, R-2).
      gradeSource is one of "both" | "plex-leaf" | "arr-dateadded" | "none".
      clockDisagreementSec is the absolute gap in seconds when both clocks
        resolved, else None. It is recorded EVERY time both resolve -- agree
        or disagree -- purely as a manifest measurement; it no longer gates
        which value is graded (see adversarial-finding note below).

    Only ONE of the two inputs resolving is NOT a withhold -- R-2 requires
    corroboration where available but does not require it; a file with a good
    Plex leaf and an *arr side that 404s (mid-scan, API hiccup) still has a
    perfectly good clock. Withholding is reserved for when NEITHER resolves.

    Adversarial finding (2026-09-13, false-delete/MAJOR): the original code
    only took max(plex_ts, arr_ts) when disagreement STRICTLY EXCEEDED
    CLOCK_DISAGREEMENT_SECS (24h); at exactly-24h it fell into an "agree"
    branch and returned plex_ts verbatim -- which could grade a file on the
    OLDER of two resolvable clocks even though the *arr side was
    independently inside the retention window. R-2's intent (corroboration
    must never let an older clock decide against a fresher one) does not
    depend on HOW MUCH the two disagree, so the branch split is gone: whenever
    BOTH clocks resolve, ALWAYS grade on max(plex_ts, arr_ts).
    """
    plex_ts = int(plex_added_at) if (plex_added_at and int(plex_added_at) > 0) else None
    arr_ts = _parse_arr_timestamp(arr_date_added_raw)
    if plex_ts is None and arr_ts is None:
        return None, "none", None
    if arr_ts is None:
        return plex_ts, "plex-leaf", None
    if plex_ts is None:
        return arr_ts, "arr-dateadded", None
    disagreement = abs(plex_ts - arr_ts)
    # R-2: take the newer, unconditionally, whenever both clocks resolve. A
    # stale Plex leaf addedAt after an *arr upgrade-replace (or vice versa, a
    # Plex rescan re-stamping a file the *arr already had on record) must
    # never win by being asked first -- regardless of how close the two are.
    return max(plex_ts, arr_ts), "both", disagreement


# ===========================================================================
# Plex (stdlib urllib + X-Plex-Token; mirror arr_client error handling — never
# raise into the main loop, return ([], err) shapes).
# ===========================================================================
def _plex_creds():
    """Return (port, token). Raises FileNotFoundError if either secret is absent
    (caller treats that as FATAL — can't safely do anything without Plex)."""
    port = read_secret("plex.port")
    token = read_secret("plex.token")
    return port, token


def _plex_get(port: str, token: str, path: str, query: str = "", timeout: int = 30):
    """GET against PMS at 127.0.0.1:{port}. Returns (status, body_text). Catches
    HTTPError/URLError/timeout and returns (0, errstr) — never raises."""
    qs = ("?" + query) if query else ""
    url = "http://127.0.0.1:" + str(port) + path + qs
    req = urllib.request.Request(url, headers={
        "X-Plex-Token": token,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="ignore")[:600]
    except (urllib.error.URLError, socket.timeout) as exc:
        return 0, str(exc)
    except Exception as exc:
        return 0, str(exc)


def _plex_put(port: str, token: str, path: str, query: str = "", timeout: int = 30):
    """PUT against PMS (for emptyTrash). Same error handling as _plex_get."""
    qs = ("?" + query) if query else ""
    url = "http://127.0.0.1:" + str(port) + path + qs
    req = urllib.request.Request(url, method="PUT", headers={
        "X-Plex-Token": token,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="ignore")[:600]
    except (urllib.error.URLError, socket.timeout) as exc:
        return 0, str(exc)
    except Exception as exc:
        return 0, str(exc)


def _mc(body):
    """Extract the Plex MediaContainer dict from a parsed JSON body. Plex returns
    {'MediaContainer': {...}}; tolerate a bare dict too."""
    if isinstance(body, dict):
        return body.get("MediaContainer", body)
    return {}


def plex_sections(port: str, token: str):
    """Return {library_title: sectionKey} for all Plex sections. On failure
    returns ({}, err)."""
    status, raw = _plex_get(port, token, "/library/sections")
    if status != 200:
        return {}, "sections HTTP " + str(status) + ": " + str(raw)[:200]
    try:
        mc = _mc(json.loads(raw))
    except Exception as exc:
        return {}, "sections JSON parse: " + str(exc)
    out = {}
    for d in mc.get("Directory", []) or []:
        title = d.get("title")
        key = d.get("key")
        if title is not None and key is not None:
            out[title] = str(key)
    return out, None


def _sum_media_parts(meta) -> int:
    """Sum Media/Part byte sizes on one Plex metadata object.

    Movies carry their parts directly. Shows do not -- see series_size_bytes.
    """
    total = 0
    for media in (meta.get("Media") or []):
        for part in (media.get("Part") or []):
            try:
                total += int(part.get("size") or 0)
            except (TypeError, ValueError):
                pass
    return total


def series_size_bytes(port: str, token: str, rating_key: str):
    """Total bytes of every episode file under one show. Returns (bytes, err).

    `/library/sections/<k>/all` returns shows WITHOUT Media/Part, so a show can
    only be sized through its leaves. One extra request per candidate series;
    the candidate set is small (tens of items) and this runs once a day, so the
    cost is irrelevant next to reporting a deletion size that is wrong by 2x.
    """
    status, raw = _plex_get(
        port, token, "/library/metadata/" + str(rating_key) + "/allLeaves")
    if status != 200:
        return 0, "allLeaves HTTP " + str(status)
    try:
        mc = _mc(json.loads(raw))
    except Exception as exc:                                   # noqa: BLE001
        return 0, "allLeaves JSON: " + str(exc)
    total = 0
    for episode in (mc.get("Metadata") or []):
        total += _sum_media_parts(episode)
    return total, None


def plex_series_leaves(port: str, token: str, rating_key: str):
    """Return (leaves, err) — the per-EPISODE-FILE detail needed for R-1/R-2
    grading, as opposed to series_size_bytes()'s single byte total. One extra
    /allLeaves call alongside series_size_bytes' own (both are O(shows), which
    the rest of this module already accepts as the cost of a once-a-day job —
    see series_size_bytes' docstring); kept as a separate function rather than
    merged into it because the two are read by different callers for different
    reasons and a byte-sum has no business also being the grading path.

    Each leaf: {"addedAt": int|None, "seasonNumber": int|None,
    "path": str|None, "sizeBytes": int}. addedAt is None (not 0) when Plex gave
    no/unparseable value, so grade_file_clock() can tell "no clock" apart from
    "epoch zero". path is Media[0].Part[0].file — the join key against the
    *arr's episodeFile.path (both speak the same /home/quadstronaut/media/...
    namespace on this box, per arr-plex-parity.sh)."""
    status, raw = _plex_get(
        port, token, "/library/metadata/" + str(rating_key) + "/allLeaves")
    if status != 200:
        return [], "allLeaves HTTP " + str(status)
    try:
        mc = _mc(json.loads(raw))
    except Exception as exc:                                   # noqa: BLE001
        return [], "allLeaves JSON: " + str(exc)
    leaves = []
    for meta in mc.get("Metadata") or []:
        try:
            added = int(meta.get("addedAt") or 0)
        except (TypeError, ValueError):
            added = 0
        path = None
        size_bytes = 0
        media_list = meta.get("Media") or []
        if media_list:
            parts = media_list[0].get("Part") or []
            if parts:
                path = parts[0].get("file")
                try:
                    size_bytes = int(parts[0].get("size") or 0)
                except (TypeError, ValueError):
                    size_bytes = 0
        try:
            season_num = (int(meta.get("parentIndex"))
                         if meta.get("parentIndex") is not None else None)
        except (TypeError, ValueError):
            season_num = None
        leaves.append({
            "addedAt": added if added > 0 else None,
            "seasonNumber": season_num,
            "path": path,
            "sizeBytes": size_bytes,
        })
    return leaves, None


def plex_items(port: str, token: str, section_key: str):
    """Return (items, err). Each item: {ratingKey,title,year,addedAt,sizeGB}.
    sizeGB = sum of Media/Part size bytes / 1024^3, 0 if unavailable. err is None
    on success, a string on failure (so the caller can mark partial + skip)."""
    status, raw = _plex_get(port, token, "/library/sections/" + str(section_key) + "/all")
    if status != 200:
        return [], "section " + str(section_key) + " /all HTTP " + str(status)
    try:
        mc = _mc(json.loads(raw))
    except Exception as exc:
        return [], "section " + str(section_key) + " /all JSON: " + str(exc)
    items = []
    for meta in mc.get("Metadata", []) or []:
        size_bytes = _sum_media_parts(meta)
        rk = str(meta.get("ratingKey")) if meta.get("ratingKey") is not None else None
        # A SHOW's own /all entry carries no Media/Part -- those live on the
        # episodes -- so the sum above is always 0 for series. Every series
        # therefore reported "0.0 GB" in the plan, and TV is 1.3T of a 2.3T
        # library: the "N GB reclaimable" an operator would use to judge a
        # retention change understated the truth by roughly the whole TV
        # library (measured 2026-07-31: at a 30-day threshold the tool said
        # 317 GB and the real on-disk figure was 706 GB).
        if size_bytes == 0 and str(meta.get("type")) == "show" and rk:
            size_bytes, serr = series_size_bytes(port, token, rk)
            if serr:
                # Loud, not silent: a 0 here is indistinguishable from a genuinely
                # empty series, and that ambiguity is what made this defect
                # survive. Say which series could not be sized.
                log("WARN: could not size series '" + str(meta.get("title"))
                    + "' (" + serr + ") - it will understate the plan total")
        try:
            added = int(meta.get("addedAt") or 0)
        except (TypeError, ValueError):
            added = 0
        try:
            year = int(meta.get("year")) if meta.get("year") is not None else None
        except (TypeError, ValueError):
            year = None
        items.append({
            "ratingKey": str(meta.get("ratingKey")) if meta.get("ratingKey") is not None else None,
            "title": meta.get("title"),
            "year": year,
            "addedAt": added,
            "sizeGB": round(size_bytes / (1024.0 ** 3), 2),
        })
    return items, None


def item_external_ids(port: str, token: str, rating_key: str):
    """Return {'tmdbId': int|None, 'tvdbId': int|None} for a Plex item by reading
    its Guid[] via /library/metadata/{rk}?includeGuids=1. On failure returns the
    dict with both None (caller's resolve step then skips -> UNRESOLVED)."""
    out = {"tmdbId": None, "tvdbId": None}
    status, raw = _plex_get(
        port, token,
        "/library/metadata/" + str(rating_key),
        query="includeGuids=1",
    )
    if status != 200:
        return out
    try:
        mc = _mc(json.loads(raw))
    except Exception:
        return out
    metas = mc.get("Metadata", []) or []
    if not metas:
        return out
    for guid in metas[0].get("Guid", []) or []:
        gid = guid.get("id") or ""
        if gid.startswith("tmdb://"):
            try:
                out["tmdbId"] = int(gid[len("tmdb://"):].split("?")[0])
            except (ValueError, IndexError):
                pass
        elif gid.startswith("tvdb://"):
            try:
                out["tvdbId"] = int(gid[len("tvdb://"):].split("?")[0])
            except (ValueError, IndexError):
                pass
    return out


# ===========================================================================
# *arr resolution + deletion (reuse ArrClient — pure urllib).
# ===========================================================================
def _arr_client(slug: str):
    """Build an ArrClient bound to our secrets dir. Imported lazily so the test
    suite can monkeypatch resolve_*/do_delete_* without the MCP path resolving."""
    from lib.arr_client import ArrClient
    return ArrClient(slug, ARR_VERSION, secrets_dir=secrets_dir())


def resolve_radarr_id(client, tmdb_id):
    """Return the Radarr movie id whose tmdbId == tmdb_id, or None if there is no
    unique positive match (zero or ambiguous -> None -> caller marks UNRESOLVED,
    never deletes)."""
    if tmdb_id is None:
        return None
    status, body = client.get("/movie")
    if status != 200 or not isinstance(body, list):
        return None
    matches = [m for m in body if m.get("tmdbId") == tmdb_id]
    if len(matches) == 1:
        return matches[0].get("id")
    return None


def resolve_sonarr_id(client, tvdb_id):
    """Return the Sonarr series id whose tvdbId == tvdb_id, or None if no unique
    positive match. Same skip-on-no-match contract as resolve_radarr_id."""
    if tvdb_id is None:
        return None
    status, body = client.get("/series")
    if status != 200 or not isinstance(body, list):
        return None
    matches = [s for s in body if s.get("tvdbId") == tvdb_id]
    if len(matches) == 1:
        return matches[0].get("id")
    return None


def sonarr_series_row(client, series_id):
    """GET /series/<id> — the full SeriesResource (ended, tags, title, ...).
    Returns None on any non-200/unparseable response; callers treat that as
    "cannot confirm P-4's conditions" and refuse to remove the record (fail
    closed — see run()'s record-removal pass)."""
    status, body = client.get("/series/" + str(series_id))
    if status != 200 or not isinstance(body, dict):
        return None
    return body


def sonarr_episode_files(client, series_id):
    """GET /episodefile?seriesId=N -> (files, err). Each file is Sonarr's raw
    EpisodeFileResource: {id, seasonNumber, path, dateAdded, size, ...}. This
    IS the per-file inventory R-1 grades against — never the series' own
    container listing."""
    status, body = client.get("/episodefile", query="seriesId=" + str(series_id))
    if status != 200 or not isinstance(body, list):
        return [], "episodefile HTTP " + str(status)
    return body, None


def sonarr_episodes(client, series_id):
    """GET /episode?seriesId=N -> (episodes, err). Used ONLY to map
    episodeFileId -> episode id for the R-5 unmonitor step; a failure here does
    not withhold grading (the file's clock is independent of this lookup) but
    DOES fail the eventual delete, because R-5 is a hard requirement — deleting
    a file without knowing which episode to unmonitor is not a safe delete."""
    status, body = client.get("/episode", query="seriesId=" + str(series_id))
    if status != 200 or not isinstance(body, list):
        return [], "episode HTTP " + str(status)
    return body, None


def radarr_movie_row(client, movie_id):
    """GET /movie/<id> — the full MovieResource, including the nested
    `movieFile` object (path, dateAdded, size) Radarr embeds for a movie that
    has a file. Returns None on any non-200/unparseable response (grading then
    falls back to the Plex clock alone via grade_file_clock)."""
    status, body = client.get("/movie/" + str(movie_id))
    if status != 200 or not isinstance(body, dict):
        return None
    return body


def resolve_permanent_tag_id(client):
    """GET /tag and return (tag_id, ok) — a TRI-STATE result, not a bare id.

      ok=True,  tag_id=<int>: the permanent tag exists on this instance, this
        is its id.
      ok=True,  tag_id=None:  the /tag call succeeded and the permanent tag
        GENUINELY does not exist on this instance.
      ok=False, tag_id=None:  the /tag call itself FAILED (non-200, timeout,
        unparseable body) — this says NOTHING about whether the tag exists.
        Callers MUST treat this as "could not confirm" and refuse removal,
        never as "tag absent" (see _series_would_be_removed's tag_lookup_ok).

    NEVER creates the tag — that is qflix-permanent.py's job (P-3/P-5); the
    reaper only ever reads P-1/P-2's exemption. Tag ids are PER INSTANCE
    (sonarr and sonarr2 do not share a tag namespace — measured 2026-09-12:
    sonarr's `permanent` is id 13, sonarr2's is id 1), so this must be called
    once per client, never cached across instances.

    Adversarial finding (2026-09-13, record-and-regrab/BLOCKER): the prior
    single-return-value contract collapsed "tag genuinely absent" and "could
    not ask" into the same None, so a transient /tag 500 read identically to
    "no permanent tag" to every caller — deleting a genuinely permanent-
    tagged, ended series RECORD outright on a network hiccup (a P-1/P-2
    bypass). Operator ruling 2026-09-13: on failure, refuse ALL series-record
    removals for this instance this run, fail closed, tri-state."""
    status, body = client.get("/tag")
    if status != 200 or not isinstance(body, list):
        return None, False
    for t in body:
        if str(t.get("label", "")).strip().lower() == PERMANENT_TAG_LABEL:
            return t.get("id"), True
    return None, True


def _delete_landed(client, path: str) -> bool:
    """Re-READ after a non-2xx delete: did the record actually go away?

    WHY THIS EXISTS. `status, _ = client.delete(...)` used to be the whole
    verdict, and an *arr DELETE that is slow is not the same as an *arr DELETE
    that failed. Radarr removes the record and unlinks the files first and
    answers afterwards, so a delete of a 7 GB movie on a busy instance can do
    all of the work and still hand back a timeout or a 500.

    Observed 2026-08-20: a 23-movie remux re-grab put 17 concurrent downloads
    and 23 queued MoviesSearch commands on Radarr main. The reaper's delete of
    'Greyhound' (arrId=407) took 30 seconds, came back non-2xx, and was logged
    DELETE FAILED -- yet GET /movie/407 returned Not Found and the directory was
    gone from disk. The whole run was then graded "completed WITH partial
    failures", exited 1, put the unit in systemd failed state and turned Kuma
    monitor #97 red, all for an operation that had succeeded. Six consecutive
    prior runs were clean, so the signal read as a real new fault.

    This is the house rule the *arr and SAB work keeps re-learning: these APIs
    lie, so verify by re-poll rather than by status code. A 404 on the re-read
    is proof the delete landed. Anything else -- a 200 (record still there), a
    transport error, an unreadable answer -- stays a failure, because the only
    safe default for "I could not confirm" is to report it.
    """
    try:
        status, _ = client.get(path)
    except Exception:
        return False
    return status == 404


def do_delete_movie(client, movie_id) -> bool:
    """DELETE a Radarr movie WITH files; addImportExclusion=false (stays
    re-requestable). 2xx is success; a non-2xx is re-read before being called a
    failure (see _delete_landed)."""
    path = "/movie/" + str(movie_id)
    status, _ = client.delete(
        path, query="deleteFiles=true&addImportExclusion=false")
    if 200 <= status < 300:
        return True
    if _delete_landed(client, path):
        warn("delete of movie " + str(movie_id) + " answered HTTP "
             + str(status) + " but the record is GONE on re-read - counting it "
             "as deleted (slow delete, not a failed one)")
        return True
    # Durable logfile is the audit trail, not journald: say WHY it failed here.
    warn("delete of movie " + str(movie_id) + " answered HTTP " + str(status)
         + " and the re-read did NOT return 404 (record still there, or unreadable)")
    return False


def do_delete_series(client, series_id) -> bool:
    """DELETE a Sonarr series WITH files; addImportListExclusion=false. Same
    re-read-before-failing contract as do_delete_movie."""
    path = "/series/" + str(series_id)
    status, _ = client.delete(
        path, query="deleteFiles=true&addImportListExclusion=false")
    if 200 <= status < 300:
        return True
    if _delete_landed(client, path):
        warn("delete of series " + str(series_id) + " answered HTTP "
             + str(status) + " but the record is GONE on re-read - counting it "
             "as deleted (slow delete, not a failed one)")
        return True
    warn("delete of series " + str(series_id) + " answered HTTP " + str(status)
         + " and the re-read did NOT return 404 (record still there, or unreadable)")
    return False


def do_delete_episode_file(client, episode_file_id) -> bool:
    """DELETE /episodefile/<id>. Same 2xx-or-re-read-404 contract as
    do_delete_movie/do_delete_series (see _delete_landed) — a slow delete on a
    busy Sonarr instance is not a failed one."""
    path = "/episodefile/" + str(episode_file_id)
    status, _ = client.delete(path)
    if 200 <= status < 300:
        return True
    if _delete_landed(client, path):
        warn("delete of episodefile " + str(episode_file_id) + " answered HTTP "
             + str(status) + " but the record is GONE on re-read - counting it "
             "as deleted (slow delete, not a failed one)")
        return True
    warn("delete of episodefile " + str(episode_file_id) + " answered HTTP "
         + str(status) + " and the re-read did NOT return 404 (record still "
         "there, or unreadable)")
    return False


def do_unmonitor_episode(client, episode_id) -> bool:
    """R-5, THE HARD REQUIREMENT: PUT /episode/monitor {episodeIds:[id],
    monitored:false}. A monitored episode with no file reappears in Sonarr's
    own /wanted/missing and gets re-grabbed on the next RSS sync — proven live
    2026-09-12 (see spec R-5) using the already-reaped Futurama S11 episodes as
    the negative control and a real /wanted/missing read as the positive one.
    Deleting a file without this is not an expiry, it is an infinite re-
    download loop against a paid Usenet block account.

    Verified by re-reading the episode afterward — these APIs lie the same way
    an *arr DELETE does (see _delete_landed): a 2xx PUT that did not actually
    flip `monitored` would otherwise look identical to one that did."""
    status, _ = client.put("/episode/monitor",
                           body={"episodeIds": [episode_id], "monitored": False})
    if not (200 <= status < 300):
        warn("unmonitor PUT for episode " + str(episode_id) + " answered HTTP "
             + str(status))
        return False
    try:
        st, body = client.get("/episode/" + str(episode_id))
    except Exception as exc:                                    # noqa: BLE001
        warn("unmonitor verify re-read for episode " + str(episode_id)
             + " raised: " + repr(exc))
        return False
    if st == 200 and isinstance(body, dict) and body.get("monitored") is False:
        return True
    warn("unmonitor for episode " + str(episode_id) + " did NOT verify on "
         "re-read (monitored != false) - treating as FAILED, not assuming success")
    return False


def do_delete_episode(client, episode_file_id, episode_ids) -> bool:
    """R-5, atomic IN EFFECT: delete the file, then unmonitor EVERY episode it
    backs. Both halves must land for this to count as a clean expiry — a file
    that is gone but whose episode is still monitored is exactly the re-grab
    loop R-5 exists to prevent, so that combination is reported as a FAILURE
    (partial), never as a silent half-success.

    episode_ids may be None (mapping could not be built at all — see
    sonarr_episodes' docstring), a single int (the common one-episode-per-file
    case, kept for caller convenience/back-compat), or a list/tuple/set of
    ints. That last shape matters: Sonarr's episodeFileId is ONE-TO-MANY for
    a combined-episode release (e.g. S01E01-E02.mkv is ONE episodeFile row
    backing TWO episode records that share its id). Adversarial finding
    (2026-09-13, false-delete/MAJOR): the prior single-id contract silently
    dropped every sibling episode but the last one written into the lookup
    dict, deleting the file while leaving the dropped episode(s) monitored
    with no file — the exact re-grab-loop state R-5 exists to prevent. Every
    id in episode_ids is now attempted (no short-circuit, so a failure on one
    sibling does not stop the others from being unmonitored), and the WHOLE
    delete is reported as a failure if even one could not be verified."""
    if not do_delete_episode_file(client, episode_file_id):
        return False
    if episode_ids is None:
        ids = []
    elif isinstance(episode_ids, (list, tuple, set)):
        ids = list(episode_ids)
    else:
        ids = [episode_ids]
    if not ids:
        warn("episodefile " + str(episode_file_id) + " was deleted but no "
             "episode id could be mapped - cannot unmonitor (R-5); "
             "counting this as a FAILURE even though the file is gone")
        return False
    all_ok = True
    for episode_id in ids:
        if not do_unmonitor_episode(client, episode_id):
            all_ok = False
    if not all_ok:
        warn("episodefile " + str(episode_file_id) + " was deleted but NOT "
             "every episode sharing it could be verified unmonitored (R-5); "
             "counting this as a FAILURE — a monitored-with-no-file sibling "
             "episode is a re-grab loop, not a partial success")
    return all_ok


# ===========================================================================
# Plex post-delete housekeeping (non-fatal warnings).
# ===========================================================================
def prune_empty_collections(port: str, token: str, section_key: str) -> int:
    """Delete collections in this section that now contain NOTHING. Returns the
    count deleted. Non-fatal throughout — this is hygiene, not the job.

    WHY THE REAPER OWNS THIS. Plex auto-creates a franchise collection once a
    section holds enough of its members (autoCollectionThreshold). This script
    then deletes the members, and Plex leaves the collection object behind as an
    empty husk. Nothing else on the box has any reason to look at it, so it
    accumulates: on 2026-08-25 the Movies library carried 14 empty collections —
    Deadpool, Dune, Gladiator, Godzilla, Guardians of the Galaxy, Mad Max,
    Moana, Sonic, Venom and more — and a member browsing on someone else's TV
    saw a shelf of franchise names with NOTHING behind any of them. That is not
    a cosmetic defect: it advertises content this server does not have.

    The reaper makes this mess, so the reaper cleans it. Doing it here rather
    than in a separate sweep is deliberate — it runs only for libraries this run
    actually deleted from, needs no discovery pass, and cannot drift out of sync
    with the deletions that cause it. (Husks from OTHER causes — a decommissioned
    app's collections outliving it, which is how four Maintainerr collections
    survived two months past its 2026-06-26 removal — are not visible from here
    and are swept by the poster janitor instead.)

    NEVER deletes a collection with members. The child count is re-read live at
    delete time rather than trusted from the section listing, because the
    listing's childCount is a cached column and this function's whole safety
    argument rests on that number being right.
    """
    status, raw = _plex_get(port, token,
                            "/library/sections/" + str(section_key) + "/collections")
    if status != 200:
        warn("collection prune: list failed for section " + str(section_key)
             + " (HTTP " + str(status) + ") -- skipped, non-fatal")
        return 0
    try:
        rows = _mc(json.loads(raw)).get("Metadata") or []
    except (ValueError, TypeError) as exc:
        warn("collection prune: unparseable collection list (" + str(exc)[:80] + ")")
        return 0

    pruned = 0
    for row in rows:
        rk = str(row.get("ratingKey") or "")
        if not rk:
            continue
        st, kraw = _plex_get(port, token, "/library/metadata/" + rk + "/children")
        if st != 200:
            continue                       # cannot prove empty -> do not touch
        try:
            kids = _mc(json.loads(kraw)).get("Metadata") or []
        except (ValueError, TypeError):
            continue
        if kids:
            continue                       # has members -> a real collection
        st, _ = _plex_delete(port, token, "/library/metadata/" + rk)
        if st in (200, 204):
            pruned += 1
            log("  pruned empty collection " + repr(row.get("title")))
        else:
            warn("collection prune: DELETE " + rk + " -> HTTP " + str(st)
                 + " (non-fatal)")
    return pruned


def _plex_delete(port: str, token: str, path: str, timeout: int = 30):
    """DELETE against PMS. Same never-raise contract as _plex_get/_plex_put."""
    url = "http://127.0.0.1:" + str(port) + path
    req = urllib.request.Request(url, method="DELETE", headers={
        "X-Plex-Token": token,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="ignore")[:600]
    except (urllib.error.URLError, socket.timeout) as exc:
        return 0, str(exc)
    except Exception as exc:
        return 0, str(exc)


def plex_refresh(port: str, token: str, section_key: str) -> bool:
    """GET /library/sections/{key}/refresh. True iff 2xx. Failure is non-fatal."""
    status, _ = _plex_get(port, token, "/library/sections/" + str(section_key) + "/refresh")
    return 200 <= status < 300


def plex_empty_trash(port: str, token: str, section_key: str) -> bool:
    """PUT /library/sections/{key}/emptyTrash. True iff 2xx. Non-fatal."""
    status, _ = _plex_put(port, token, "/library/sections/" + str(section_key) + "/emptyTrash")
    return 200 <= status < 300


# ===========================================================================
# Seerr reconciliation (stdlib urllib; secrets seerr.* — NEVER jellyseerr.*).
# ===========================================================================
def _seerr_creds():
    """Return (port, key) for Seerr (the app is SEERR). Reads ~/secrets/seerr.port
    + seerr.key. jellyseerr.* is STALE and must not be read. Raises
    FileNotFoundError if absent (caller treats Seerr step as best-effort)."""
    return read_secret("seerr.port"), read_secret("seerr.key")


def _seerr_req(method: str, port: str, key: str, path: str, query: str = "", timeout: int = 30):
    """Request against Seerr at 127.0.0.1:{port}, X-Api-Key header. Returns
    (status, body_text_or_parsed). Never raises (mirror arr_client._req)."""
    qs = ("?" + query) if query else ""
    url = "http://127.0.0.1:" + str(port) + path + qs
    req = urllib.request.Request(url, method=method, headers={
        "X-Api-Key": key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            code = resp.status
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="ignore")[:600]
    except (urllib.error.URLError, socket.timeout) as exc:
        return 0, str(exc)
    except Exception as exc:
        return 0, str(exc)
    try:
        return code, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return code, raw


def _seerr_stuck_seasons(port, key, row):
    """Season numbers this Seerr row reports as DELETED(7) or BLOCKLISTED-free
    stale, for a series that IS still present in its *arr.

    Returns [] on ANY doubt — an unreachable detail endpoint, an unparseable
    body, or no season data at all. Fail closed: a row we could not inspect is
    left alone rather than deleted, because the deletion cascades every season
    row with it. "I could not look" must never render as "it is stale".

    Only status 7 counts as stuck. A season at 1 was never requested, 4/5 are
    real availability, and 2/3 are in-flight and already excluded upstream.
    """
    tmdb = row.get("tmdbId")
    if tmdb is None:
        return []
    status, body = _seerr_req("GET", port, key, "/api/v1/tv/" + str(int(tmdb)))
    if status != 200 or not isinstance(body, dict):
        return []
    info = body.get("mediaInfo")
    if not isinstance(info, dict):
        return []
    seasons = info.get("seasons")
    if not isinstance(seasons, list) or not seasons:
        return []
    stuck = []
    for se in seasons:
        if not isinstance(se, dict):
            continue
        try:
            if int(se.get("status")) == _SEERR_STATUS_DELETED:
                stuck.append(int(se.get("seasonNumber")))
        except (TypeError, ValueError):
            continue
    return sorted(stuck)


def reconcile_seerr(execute: bool):
    """After all libraries are reaped, delete Seerr media rows whose backing arr
    item is gone, so the title becomes re-requestable. Returns (deleted, failed).

    A movie row (tmdbId) is reconciled away iff no Radarr (or radarr2) movie with
    that tmdbId has hasFile==true. A TV row (tvdbId) iff no Sonarr (or sonarr2)
    series has that tvdbId. Per-item, non-fatal, logged, exit-code-reflected.
    Tolerates an empty / unreachable Seerr without aborting."""
    deleted = 0
    failed = 0
    in_flight = 0
    blocklisted = 0
    would = 0
    try:
        port, key = _seerr_creds()
    except FileNotFoundError:
        warn("seerr.port/seerr.key missing — skipping Seerr reconciliation")
        return deleted, failed
    if not port or not key:
        warn("seerr creds empty — skipping Seerr reconciliation")
        return deleted, failed

    # Page through EVERY media row, not just the available ones. A single take=N
    # would silently skip rows past the cap, leaving deleted titles stuck and
    # members unable to re-request them. Loop skip+=PAGE until a short page
    # arrives or pageInfo.results is exhausted; a hard ceiling guards a
    # misbehaving API.
    #
    # `filter=available` WAS the query here, and it was the bug. A reaped title
    # does not stay "available" — Seerr moves it to status 7 (DELETED), which
    # that filter cannot see, so those rows were never reconciled and the title
    # stayed un-re-requestable forever. Measured 2026-09-12 in Seerr's own DB:
    # 66 seasons across 28 DISTINCT shows sat at status 7, Law & Order alone
    # holding 23, with 763 season_request rows still pointing at them. The
    # operator's father could not request Law & Order seasons 3 and 4; an admin
    # had to push them through by hand. The function's own docstring promised
    # exactly the outcome the filter prevented.
    results = []
    skip = 0
    while True:
        status, body = _seerr_req(
            "GET", port, key, "/api/v1/media",
            query="take=" + str(_SEERR_MEDIA_PAGE) + "&skip=" + str(skip),
        )
        if status != 200 or not isinstance(body, dict):
            if skip == 0:
                # OPERATOR RULING 2026-09-13: no tolerance — page.
                # This used to return (0, 0), which the caller cannot tell apart
                # from "nothing needed reconciling", so a Seerr outage during the
                # nightly run reported GREEN while zero titles were reconciled.
                # The repo's own test enshrined that as intended behaviour. It
                # was wrong: silence about work that did not happen is the same
                # class of defect as a canary that reads clean when it could not
                # look. failed=1 makes main() mark the run partial, which reds
                # Kuma and pages Discord.
                warn("Seerr media list unreachable (HTTP " + str(status) +
                     ") — reconciliation did NOT run; marking the run partial")
                return deleted, failed + 1
            # Mid-pagination failure: reconcile what we already fetched rather
            # than abort — a partial pass beats none, and it's logged.
            warn("Seerr media page at skip=" + str(skip) + " failed (HTTP " +
                 str(status) + ") — reconciling the " + str(len(results)) +
                 " row(s) fetched so far")
            break
        page = body.get("results") or []
        results.extend(page)
        total = (body.get("pageInfo") or {}).get("results")
        if len(page) < _SEERR_MEDIA_PAGE:
            break
        skip += _SEERR_MEDIA_PAGE
        if isinstance(total, int) and skip >= total:
            break
        if skip > 100000:   # safety valve: never loop unbounded
            warn("Seerr pagination exceeded 100000 rows — stopping")
            break
    if not results:
        log("Seerr: no media rows to reconcile")
        return deleted, failed
    log("Seerr: reconciling " + str(len(results)) + " media row(s) (all statuses)")

    # Build the live arr index once: movie tmdbIds with files, and series tvdbIds.
    #
    # THIS INDEX IS THE ONLY THING STANDING BETWEEN A SWEEP AND THE WHOLE TABLE.
    # `gone` is "absent from the index", so an EMPTY index means every settled
    # row looks orphaned. The original code swallowed a client construction
    # failure with `except Exception: continue` and carried on with whatever it
    # had — so if the *arrs were unreachable it would delete every settled Seerr
    # row in one pass.
    #
    # Not theoretical: observed 2026-09-13. A harness loaded this module from a
    # path where `lib` was not importable, all four clients raised
    # ModuleNotFoundError, the index came back empty, and the dry run went from
    # 42 rows to 131 — every non-in-flight row in the table. The dry run is the
    # only reason that was caught.
    #
    # Now: any instance that cannot be indexed makes the whole reconciliation
    # refuse. Same rule as everywhere else here — "I could not look" must never
    # render as "it is gone".
    radarr_with_file = set()
    sonarr_tvdbids = set()
    index_failures = []
    for entry in LIBRARIES:
        slug = entry["slug"]
        try:
            client = _arr_client(slug)
            if entry["kind"] == "movie":
                st, mv = client.get("/movie")
                if st != 200 or not isinstance(mv, list):
                    index_failures.append(slug + ":http" + str(st))
                    continue
                for m in mv:
                    if m.get("hasFile") and m.get("tmdbId") is not None:
                        radarr_with_file.add(m.get("tmdbId"))
            else:
                st, sr = client.get("/series")
                if st != 200 or not isinstance(sr, list):
                    index_failures.append(slug + ":http" + str(st))
                    continue
                for s in sr:
                    if s.get("tvdbId") is not None:
                        sonarr_tvdbids.add(s.get("tvdbId"))
        except Exception as exc:
            index_failures.append(slug + ":" + type(exc).__name__)

    if index_failures:
        warn("Seerr reconciliation REFUSED — could not index " +
             str(len(index_failures)) + " *arr instance(s): " +
             ", ".join(index_failures) +
             ". An incomplete index would make every settled row look orphaned.")
        return deleted, failed + 1

    for row in results:
        # Coerce the Seerr id to int before it can reach a URL path — a non-integer
        # id is invalid and skipped (defends against a reflected path-traversal id).
        try:
            media_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        media_type = row.get("mediaType")

        # NEVER touch an in-flight request. Widening off `filter=available`
        # brought PENDING(2) and PROCESSING(3) rows into scope for the first
        # time, and those are requests a member is currently waiting on — a row
        # can legitimately sit in PROCESSING with no *arr record yet while the
        # push is still in progress or has just failed. Deleting it would throw
        # the request away silently. Only settled rows are reconciled.
        try:
            row_status = int(row.get("status"))
        except (TypeError, ValueError):
            row_status = None
        if row_status in (_SEERR_STATUS_PENDING, _SEERR_STATUS_PROCESSING):
            in_flight += 1
            continue
        if row_status == _SEERR_STATUS_BLOCKLISTED:
            blocklisted += 1
            continue

        gone = False
        reason = "orphan"
        if media_type == "movie":
            tmdb = row.get("tmdbId")
            gone = tmdb is not None and tmdb not in radarr_with_file
        elif media_type == "tv":
            tvdb = row.get("tvdbId")
            gone = tvdb is not None and tvdb not in sonarr_tvdbids
            if not gone and tvdb is not None:
                # SEASON GRANULARITY — the actual reported bug.
                #
                # The reaper normally reaps PER SEASON, so the series stays in
                # Sonarr with some seasons full and some empty. The whole-series
                # check above then answers "present", the row is skipped, and any
                # season Seerr left at DELETED stays that way forever. Measured
                # 2026-09-12: Law & Order (tmdb 549) held TWENTY-THREE season
                # rows at status 7 while its media row sat at 4 and its tvdb was
                # very much still in Sonarr. The operator's father could not
                # request seasons 3 and 4; an admin pushed them through by hand.
                #
                # Seerr 3.4.1 exposes NO per-season lever — DELETE
                # /media/<id>/season/<n> is a 404, and GET /media/<id> is not
                # even allowed. The only lever is the media row, and deleting it
                # CASCADES every season row with it.
                #
                # That is safe because Seerr rebuilds the truth itself. PROVEN
                # live on Futurama 2026-09-13, not assumed:
                #   before  media 107  seasons 0:1 1:5 ... 10:1 11:7
                #   DELETE  -> 0 media rows, 0 season rows (cascade confirmed)
                #   sonarr-scan
                #   after   media 331  seasons 0:1 1:5 ... 10:1 11:1
                # The genuine "available" season came BACK from Sonarr; only the
                # stuck 7 was cleared, becoming 1 (never requested) and therefore
                # requestable again. Seerr runs sonarr-scan, plex-full-scan and
                # availability-sync daily on its own, so this self-heals even if
                # nothing triggers a scan.
                #
                # COST, stated plainly: the media row's request history goes with
                # it. For a title whose seasons are stuck that is the point.
                stuck = _seerr_stuck_seasons(port, key, row)
                if stuck:
                    gone = True
                    reason = "stuck-seasons=" + ",".join(str(n) for n in stuck[:8])
        if not gone:
            continue
        log("Seerr: media " + str(media_id) + " (" + str(media_type) +
            ", " + reason + ") -> " + ("DELETE" if execute else "would delete"))
        if not execute:
            # `deleted` keeps its literal meaning — rows actually DELETEd — so
            # the execute-path summary stays honest. The dry-run blast radius is
            # reported separately below instead of being folded into it.
            would += 1
            continue
        st, _ = _seerr_req("DELETE", port, key, "/api/v1/media/" + str(media_id))
        if 200 <= st < 300:
            deleted += 1
        else:
            failed += 1
            warn("Seerr delete media " + str(media_id) + " failed: HTTP " + str(st))
    if in_flight:
        log("Seerr: skipped " + str(in_flight) +
            " in-flight row(s) (pending/processing — a member is waiting on them)")
    if blocklisted:
        log("Seerr: skipped " + str(blocklisted) +
            " blocklisted row(s) (an admin blocked these on purpose)")
    if would:
        # A dry run that logs 42 "would delete" lines and then reports nothing
        # tells the operator the change is a no-op. Say the number out loud.
        log("Seerr: DRY RUN — " + str(would) + " stale row(s) would be cleared")
    return deleted, failed


# ===========================================================================
# Caps
# ===========================================================================
def check_caps(per_lib_candidates, per_lib_totals, max_items, max_pct, force):
    """Decide whether the run may proceed. Returns (ok, messages).

    per_lib_candidates: {plex_title: [candidate dicts]}
    per_lib_totals:     {plex_title: total_item_count_in_library}
    Trips if total candidates > max_items, OR candidates in any one library
    exceed max_pct% of that library's total. --force overrides both (ok=True but
    messages still describe what was overridden, logged at WARNING)."""
    msgs = []
    total = sum(len(v) for v in per_lib_candidates.values())
    tripped = False

    if total > max_items:
        msgs.append("max-items cap: " + str(total) + " candidates > " + str(max_items))
        tripped = True

    for title, cands in per_lib_candidates.items():
        n = len(cands)
        tot = per_lib_totals.get(title, 0)
        if tot > 0 and n > 0:
            pct = 100.0 * n / tot
            if pct > max_pct:
                msgs.append("max-pct cap: '" + title + "' " + str(n) + "/" + str(tot) +
                            " = " + str(round(pct, 1)) + "% > " + str(max_pct) + "%")
                tripped = True

    if tripped and force:
        return True, ["FORCE OVERRIDE of caps -> " + " | ".join(msgs)]
    return (not tripped), msgs


# ===========================================================================
# Manifest
# ===========================================================================
def write_manifest(manifest_dir: Path, args, per_lib_candidates, series_removal_state=None):
    """Write the pre-execution audit record and return its Path. Called ONLY on
    --execute, BEFORE the first DELETE. Lists every intended deletion.

    series_removal_state: the run() dict of (slug, arrId) -> state built for
    every resolved series (see run()'s docstring comment at its
    declaration). Adversarial finding (2026-09-13, envelope-and-manifest/
    MAJOR): P-4 series-RECORD removals were a whole distinct class of
    deletion this manifest never recorded at all — an operator inspecting
    the pre-execution manifest had zero way to see that N series records
    were about to be removed. Only entries flagged p4_scheduled (eligible
    AND within this run's shared --max-items budget, see run()'s P-4
    capping block) are written to series_removals[] — that flag IS the
    "about to be removed this run" predicate, identical to what the execute
    path evaluates moments later."""
    ts = datetime.now(timezone.utc)
    # PID suffix so two runs in the same second can't overwrite each other's
    # pre-deletion audit record.
    fname = "qflix-reaper-" + ts.strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid()) + ".json"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / fname

    flat = []
    series_files = {}   # "<slug>:<arrId>" -> {arrId,title,library,files:[...]}
    total_gb = 0.0
    for title, cands in per_lib_candidates.items():
        for c in cands:
            total_gb += c.get("sizeGB", 0) or 0
            # `addedAt` KEEPS its old meaning (the Plex CONTAINER clock) for
            # comparability with the ~50 manifests already on disk from before
            # per-file grading existed. It is informational only now — R-4
            # forbids using it for candidacy, and gradedAt/gradeSource below
            # are what actually decided this row belongs here.
            flat.append({
                "title": c.get("title"),
                "year": c.get("year"),
                "type": c.get("kind"),
                "library": title,
                "ratingKey": c.get("ratingKey"),
                "tmdbId": c.get("tmdbId"),
                "tvdbId": c.get("tvdbId"),
                "arrId": c.get("arrId"),
                "sizeGB": c.get("sizeGB"),
                "addedAt": c.get("addedAt"),
                "gradedAt": c.get("gradedAt"),
                "gradeSource": c.get("gradeSource"),
                "clockDisagreementSec": c.get("clockDisagreementSec"),
            })
            if "episodeFileId" in c:
                key = str(c.get("slug")) + ":" + str(c.get("arrId"))
                grp = series_files.setdefault(key, {
                    "arrId": c.get("arrId"), "title": c.get("title"),
                    "library": title, "files": [],
                })
                grp["files"].append({
                    "episodeFileId": c.get("episodeFileId"),
                    "path": c.get("path"),
                    "seasonNumber": c.get("seasonNumber"),
                    "gradedAt": c.get("gradedAt"),
                })

    # P-4 series-RECORD removals — a distinct mutation class from the file/
    # movie candidates above, scheduled by run()'s shared --max-items budget
    # pass. Named here, before the first DELETE, same as every other intended
    # mutation (2026-09-13 finding: this was previously absent entirely).
    series_removals = []
    for (slug, arr_id), st in (series_removal_state or {}).items():
        if st.get("p4_scheduled"):
            series_removals.append({
                "slug": slug,
                "arrId": arr_id,
                "title": st.get("title"),
                "library": st.get("library"),
            })

    doc = {
        "run_timestamp": ts.isoformat().replace("+00:00", "Z"),
        "flags": {
            "threshold_days": args.threshold_days,
            "max_items": args.max_items,
            "max_pct": args.max_pct,
            "force": args.force,
            "execute": args.execute,
        },
        "candidates": flat,
        "series_files": list(series_files.values()),
        "series_removals": series_removals,
        "total_count": len(flat),
        "total_reclaim_gb": round(total_gb, 2),
    }
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


# ===========================================================================
# CLI
# ===========================================================================
def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--execute", action="store_true",
                    help="perform real deletions (the ONLY way to mutate). Default is dry-run.")
    ap.add_argument("--threshold-days", type=float, default=DEFAULT_THRESHOLD_DAYS,
                    help=("per-file grading-clock age cutoff in days (fractional "
                          "allowed); a file is a candidate iff age > N (strict). "
                          "Default %(default)s. REQ-CLAMP: this is RAISE-ONLY — "
                          "any value below the MIN_FILE_AGE_FLOOR_DAYS module "
                          "constant (currently " + str(MIN_FILE_AGE_FLOOR_DAYS) +
                          ") is silently-never-fatal but LOUDLY clamped up to the "
                          "floor (max(FLOOR, requested)), logged as a WARNING "
                          "naming both the requested and effective values."))
    ap.add_argument("--exclude-file", default=None,
                    help="exclusion list (default scripts/maint/qflix-reaper.exclude beside this script).")
    ap.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS,
                    help="absolute cap on total candidates; exceeding aborts unless --force. Default 50.")
    ap.add_argument("--max-pct", type=float, default=DEFAULT_MAX_PCT,
                    help="per-library percent cap; exceeding in any library aborts unless --force. Default 30.")
    ap.add_argument("--force", action="store_true",
                    help="override BOTH caps (logged WARNING). Does NOT imply --execute.")
    ap.add_argument("--manifest-dir", default=str(Path.home()),
                    help="where the audit manifest JSON is written on --execute. Default ~.")
    ap.add_argument("--library", action="append", default=None,
                    help="repeatable: restrict to these Plex library names. Default = all 4.")
    ap.add_argument("--json", dest="emit_json", action="store_true",
                    help="also emit a machine-readable plan/result summary to stdout.")
    ap.add_argument("--orphan-grace-hours", type=float, default=24.0,
                    help="hours a NEW un-resolvable orphan reds the run before it "
                         "downgrades to a green weekly-reminder. Default 24.")
    ap.add_argument("--orphan-remind-days", type=float, default=7.0,
                    help="cadence of the WARN reminder for a KNOWN (aged-out) "
                         "orphan. Default 7.")
    ap.add_argument("--orphan-state", default=None,
                    help="orphan grace-state file (default env "
                         "QFLIX_REAPER_ORPHAN_STATE, else ~/.opt/maint/reaper/"
                         "orphan-state.json).")
    return ap.parse_args(argv)


# ===========================================================================
# Per-series file grading (R-1..R-5) and record-removal (P-1/P-2/P-4).
# ===========================================================================
def grade_series_files(port, token, client, series_item, arr_id, rules, now, threshold_secs):
    """R-1..R-5 for ONE resolved series: fetch its episode files + Plex leaves,
    grade each file's OWN clock independently of the series' container addedAt
    (which R-4 forbids using at all here), and return everything run() needs.

    Returns a dict:
      candidates: [{...}]  per-file candidate dicts, past-threshold, clock-
          resolved, not excluded. Each inherits title/library/kind/slug/
          tvdbId/tmdbId/ratingKey/addedAt from series_item (addedAt keeps its
          OLD container-clock meaning for manifest comparability — see the
          module-level note in the spec; it is NEVER consulted for candidacy)
          plus: arrId, episodeFileId, episodeIds (a LIST — one episodeFileId
          can back multiple episode records for a combined-episode release
          such as S01E01-E02.mkv; every id in the list must be unmonitored
          for the delete to count as clean, see do_delete_episode), seasonNumber,
          path, gradedAt, gradeSource, clockDisagreementSec, sizeGB.
      withheld: [{episodeFileId, path, seasonNumber}] — files whose clock could
          NOT be determined at all (fail closed, R-2). Named, never silent.
      total_files: int — this series' CURRENT episode-file count (the TV
          --max-pct denominator; see run()'s per_lib_totals accumulation).
      err: str|None — a failure grading this series AT ALL (allLeaves/
          episodefile unreachable). Non-None means nothing here is usable this
          run and the caller must mark the run partial, per the same
          "could not look must never render as nothing to do" rule
          reconcile_seerr's index-failure guard already enforces.
    """
    result = {"candidates": [], "withheld": [], "total_files": 0, "err": None}

    leaves, lerr = plex_series_leaves(port, token, series_item["ratingKey"])
    if lerr:
        result["err"] = "allLeaves: " + lerr
        return result
    files, ferr = sonarr_episode_files(client, arr_id)
    if ferr:
        result["err"] = "episodefile: " + ferr
        return result
    episodes, eerr = sonarr_episodes(client, arr_id)
    if eerr:
        # R-5 is a hard requirement, not best-effort: if we cannot map ANY
        # episodeFileId -> episode for this series, we cannot safely delete
        # ANY of its files this run. The alternative — grading files anyway
        # with episodeIds=[] — would let do_delete_episode delete the file
        # FIRST and only discover the missing mapping afterward, leaving the
        # exact monitored-with-no-file state R-5 exists to prevent. Fail
        # closed at the series level, same as an allLeaves/episodefile fetch
        # failure, rather than fail closed one file too late.
        result["err"] = "episode: " + eerr
        return result
    # One-to-many: a combined-episode release (S01E01-E02.mkv) is ONE
    # episodeFile row backing TWO+ episode records that share its id. A plain
    # dict here silently drops every sibling but the last one written, which
    # is how a real multi-episode file used to leave one episode monitored
    # with no file after delete (2026-09-13 adversarial finding, R-5 hazard).
    ep_by_file_id = {}
    for e in episodes:
        fid = e.get("episodeFileId")
        if fid:
            ep_by_file_id.setdefault(fid, []).append(e.get("id"))
    result["total_files"] = len(files)

    leaves_by_path = {}
    for lf in leaves:
        norm = _norm_path(lf.get("path"))
        if norm:
            leaves_by_path[norm] = lf

    for f in files:
        norm = _norm_path(f.get("path"))
        leaf = leaves_by_path.get(norm) if norm else None
        plex_added = leaf.get("addedAt") if leaf else None
        graded_at, source, disagreement = grade_file_clock(plex_added, f.get("dateAdded"))
        if graded_at is None:
            # R-2 fail-closed: neither clock resolved for this file. Counted
            # and named here; run() logs it loudly and surfaces it in --json —
            # "Withholding is never silent" (spec section 5).
            result["withheld"].append({
                "episodeFileId": f.get("id"), "path": f.get("path"),
                "seasonNumber": f.get("seasonNumber"),
            })
            continue
        if not (now - graded_at > threshold_secs):    # strictly greater-than
            continue
        size_bytes = (leaf.get("sizeBytes") if leaf else 0) or 0
        if not size_bytes:
            try:
                size_bytes = int(f.get("size") or 0)
            except (TypeError, ValueError):
                size_bytes = 0
        cand = dict(series_item)
        cand["arrId"] = arr_id
        cand["episodeFileId"] = f.get("id")
        cand["episodeIds"] = ep_by_file_id.get(f.get("id")) or []
        cand["seasonNumber"] = f.get("seasonNumber")
        cand["path"] = f.get("path")
        cand["gradedAt"] = graded_at
        cand["gradeSource"] = source
        cand["clockDisagreementSec"] = disagreement
        cand["sizeGB"] = round(size_bytes / (1024.0 ** 3), 4)
        # Per-file exclusion (spec: is_excluded "must apply per file" too) —
        # the candidate inherits tvdbId/ratingKey/title from series_item, so
        # this re-checks the SAME rules against the SAME series identity per
        # file. Deliberately redundant with the series-level check in run()'s
        # enumeration loop: defense in depth, not because the outcome differs.
        if is_excluded(cand, rules):
            continue
        result["candidates"].append(cand)
    return result


def _series_would_be_removed(row, permanent_tag_id, tag_lookup_ok, remaining_files) -> bool:
    """P-4, the ONE condition that removes a series RECORD: zero episode
    files, `ended` is true, and the series does NOT carry the permanent tag.
    `remaining_files` MUST be a freshly re-read post-delete count when called
    from the execute path — never a prediction — because a partial per-file
    delete failure must not be papered over by an assumed zero. `row` may be
    reused from grading time (ended/tags do not change from a file delete).

    tag_lookup_ok is resolve_permanent_tag_id's tri-state result for THIS
    instance, THIS run. Adversarial finding (2026-09-13, record-and-regrab/
    BLOCKER): a transient /tag failure used to collapse to
    permanent_tag_id=None, which was indistinguishable from "the tag
    genuinely does not exist" and let an ended+zero-file series that DID
    carry the real permanent tag get its record deleted outright on a
    network hiccup. tag_lookup_ok=False now refuses removal unconditionally
    (fail closed, operator ruling 2026-09-13) — "API down" must never be read
    as "tag doesn't exist".

    Movies are not handled here: do_delete_movie already removes the record
    with its one file, unchanged from prior behaviour (spec: "Movies: record
    deleted with the file")."""
    if not tag_lookup_ok:
        return False                     # could not confirm exemption -> refuse
    if remaining_files > 0:
        return False
    if row is None:
        return False                     # cannot confirm ended/tags -> leave it
    if not row.get("ended"):
        return False                     # P-3: unfinished shows must survive
    tags = row.get("tags") or []
    if permanent_tag_id is not None and permanent_tag_id in tags:
        return False                     # P-1/P-2: record exempted
    return True


# ===========================================================================
# Main orchestration
# ===========================================================================
def run(args) -> int:
    execute = args.execute
    mode = "EXECUTE" if execute else "DRY-RUN"

    # REQ-CLAMP (spec section 5): raise-only, loud, never fatal. Applied before
    # anything else touches args.threshold_days so every downstream consumer
    # (the age math below, the manifest, --json, the log line right after
    # this) sees the CLAMPED value — a below-floor request must never reach a
    # single line of code as anything but "45 or higher, and everyone knows it
    # was requested lower".
    requested_threshold_days = args.threshold_days
    args.threshold_days, was_clamped = clamp_threshold_days(args.threshold_days)
    if was_clamped:
        warn("REQ-CLAMP: --threshold-days=" + str(requested_threshold_days) +
             " is below the floor (MIN_FILE_AGE_FLOOR_DAYS=" +
             str(MIN_FILE_AGE_FLOOR_DAYS) + "d) - CLAMPING UP to " +
             str(args.threshold_days) + "d. A prior attempt at this exact "
             "requirement shipped a floor that only worked for a literal 0.0; "
             "this one is enforced for every value below the floor, not just "
             "that one.")

    log("--- qflix-reaper (" + mode + ") threshold=" + str(args.threshold_days) +
        "d max-items=" + str(args.max_items) + " max-pct=" + str(args.max_pct) +
        " force=" + str(args.force) + " ---")

    # Exclusions
    if args.exclude_file:
        exclude_path = Path(args.exclude_file)
    else:
        exclude_path = _HERE / "qflix-reaper.exclude"
    rules = load_exclusions(exclude_path)
    log("loaded " + str(len(rules)) + " exclusion rule(s) from " + str(exclude_path))

    # Plex creds (FATAL if absent — cannot safely enumerate without them)
    try:
        port, token = _plex_creds()
    except FileNotFoundError as exc:
        msg = "FATAL: Plex creds missing (" + str(exc) + ") — cannot enumerate; aborting"
        warn(msg)
        _notify(msg, level="error")
        _push_kuma("down", msg)
        return EXIT_FATAL

    sections, err = plex_sections(port, token)
    if err is not None:
        msg = "FATAL: cannot reach Plex /library/sections — " + err
        warn(msg)
        _notify(msg, level="error")
        _push_kuma("down", msg)
        return EXIT_FATAL

    wanted = set(args.library) if args.library else None

    per_lib_candidates = {}   # plex_title -> [candidate dicts] (FILE granularity)
    per_lib_totals = {}       # plex_title -> total item count (movies: shows/
                              # files 1:1; TV: total episode FILES across every
                              # resolved series -- see the series branch below)
    per_lib_section = {}      # plex_title -> section key
    partial = False           # OPERATIONAL failure only (delete/plex/seerr/arr);
                              # orphans are tracked separately (grace window).
    orphans_seen = []         # [{key,title,library}] aged items that resolve to
                              # NO unique *arr id — graced, not an instant red.
    withheld_files = []       # [{title,library,episodeFileId,path,seasonNumber}]
                              # R-2 fail-closed: clock undeterminable. Named,
                              # never silent, never a delete candidate.
    series_removal_state = {} # (slug, arrId) -> {client,row,permanent_tag_id,
                              # tag_lookup_ok,title,library,slug,arrId,
                              # candidate_file_count,total_files_before,
                              # container_added_at,p4_eligible,p4_scheduled}.
                              # Built here for EVERY resolved series (whether
                              # or not it had aged files this run) so P-4 can
                              # be evaluated for a series that was ALREADY at
                              # zero files. p4_eligible/p4_scheduled are filled
                              # in later by the shared --max-items budget pass
                              # (a series can be eligible but deferred).
    now = int(datetime.now(timezone.utc).timestamp())
    threshold_secs = args.threshold_days * DAY_SECONDS

    for entry in LIBRARIES:
        title = entry["plex"]
        if wanted is not None and title not in wanted:
            continue
        key = sections.get(title)
        if key is None:
            # Library not present in Plex at all — treat as empty, not an error.
            log("library '" + title + "' not found in Plex (treating as empty)")
            per_lib_candidates[title] = []
            per_lib_totals[title] = 0
            continue
        per_lib_section[title] = key

        items, ierr = plex_items(port, token, key)
        if ierr is not None:
            warn("could not list items for '" + title + "': " + ierr)
            partial = True
            per_lib_candidates[title] = []
            per_lib_totals[title] = 0
            continue

        # Build an arr client once per library for resolution.
        try:
            client = _arr_client(entry["slug"])
        except Exception as exc:
            warn("could not build arr client for '" + entry["slug"] + "': " + str(exc))
            client = None

        # ------------------------------------------------------------------
        # MOVIES — unit of retention already equals the file (R-1: "a movie
        # has one"). Plex's own addedAt is a valid grading clock for a movie
        # (R-2: "movies: the item itself" — there is no separate container
        # level to reject the way R-4 rejects a SHOW's addedAt), so a cheap
        # raw-addedAt pre-filter is safe here and keeps the common case from
        # paying for a resolve+GET on every fresh movie in the library. What
        # is NOT safe is trusting that raw clock uncorroborated: a Radarr
        # upgrade-replace can leave Plex's addedAt stale relative to the
        # CURRENT file, so every item that clears the raw pre-filter is still
        # corroborated against Radarr's own movieFile.dateAdded (R-2) and
        # RE-CHECKED against the threshold using whichever clock is newer
        # before it is allowed to become a candidate.
        # ------------------------------------------------------------------
        if entry["kind"] == "movie":
            per_lib_totals[title] = len(items)
            cands = []
            for it in items:
                # addedAt<=0 = Plex gave no/unparseable add-date; UNKNOWN age,
                # never a candidate (a metadata gap must not look ancient).
                if it["addedAt"] <= 0:
                    continue
                if not (now - it["addedAt"] > threshold_secs):
                    continue
                ids = item_external_ids(port, token, it["ratingKey"])
                it["tmdbId"] = ids.get("tmdbId")
                it["tvdbId"] = ids.get("tvdbId")
                it["library"] = title
                it["kind"] = entry["kind"]
                it["slug"] = entry["slug"]
                if is_excluded(it, rules):
                    log("EXCLUDED " + repr(it.get("title")) + " in '" + title + "'")
                    continue

                arr_id = resolve_radarr_id(client, it["tmdbId"]) if client else None
                if arr_id is None:
                    warn("UNRESOLVED " + repr(it.get("title")) + " in '" + title +
                         "' (no unique *arr match) — SKIP, will not delete")
                    orphans_seen.append({"key": _orphan_key(it),
                                         "title": it.get("title"), "library": title})
                    continue
                it["arrId"] = arr_id

                movie_row = radarr_movie_row(client, arr_id)
                arr_date_added = None
                if movie_row:
                    arr_date_added = (movie_row.get("movieFile") or {}).get("dateAdded")
                graded_at, source, disagreement = grade_file_clock(it["addedAt"], arr_date_added)
                it["gradedAt"] = graded_at
                it["gradeSource"] = source
                it["clockDisagreementSec"] = disagreement
                # R-2: the corroborated clock is the one that counts, even if
                # it RESCUES a movie the raw Plex addedAt alone made look old
                # (a stale addedAt after an upgrade-replace is exactly R-4's
                # failure class, just on the movie side of the fence).
                if not (now - graded_at > threshold_secs):
                    log("RESCUED (clock corroboration) " + repr(it.get("title")) +
                        " in '" + title + "' — raw Plex addedAt looked expired "
                        "but the corroborated clock (" + source + ") does not")
                    continue
                cands.append(it)

            per_lib_candidates[title] = cands
            log("library '" + title + "': " + str(len(items)) + " items, " +
                str(len(cands)) + " resolved candidate(s)")
            continue

        # ------------------------------------------------------------------
        # TV / ANIME — R-1: the unit of retention is the EPISODE FILE. R-4
        # forbids using the SHOW's own Plex addedAt for candidacy at all — it
        # is stamped once when the show first enters the library and never
        # moves again no matter how many files land under it later (the
        # Futurama defect). Every resolved series is therefore graded
        # file-by-file regardless of how old or new its container looks.
        # ------------------------------------------------------------------
        per_lib_totals[title] = 0
        if client:
            permanent_tag_id, tag_lookup_ok = resolve_permanent_tag_id(client)
        else:
            permanent_tag_id, tag_lookup_ok = None, True
        if not tag_lookup_ok:
            # Operator ruling 2026-09-13: a /tag lookup failure fails CLOSED
            # for this instance, this run — refuse every P-4 series-record
            # removal below rather than let "API down" read as "tag absent"
            # (see resolve_permanent_tag_id / _series_would_be_removed).
            warn("could not confirm permanent-tag status for '" + entry["slug"] +
                 "' this run (GET /tag failed) — refusing ALL series-record "
                 "removals (P-4) for this instance this run (fail closed)")
            partial = True
        cands = []
        for it in items:
            ids = item_external_ids(port, token, it["ratingKey"])
            it["tmdbId"] = ids.get("tmdbId")
            it["tvdbId"] = ids.get("tvdbId")
            it["library"] = title
            it["kind"] = entry["kind"]
            it["slug"] = entry["slug"]
            if is_excluded(it, rules):
                log("EXCLUDED " + repr(it.get("title")) + " in '" + title + "'")
                continue

            arr_id = resolve_sonarr_id(client, it["tvdbId"]) if client else None
            if arr_id is None:
                # Orphan REPORTING is still gated on the container looking
                # aged (same conservative window as before) so a freshly-
                # imported show that Plex has not yet guid-matched does not
                # instantly red the run — only resolution ITSELF is now
                # attempted unconditionally, because every resolved series
                # must be graded regardless of its container's age.
                if it["addedAt"] > 0 and (now - it["addedAt"] > threshold_secs):
                    warn("UNRESOLVED " + repr(it.get("title")) + " in '" + title +
                         "' (no unique *arr match) — SKIP, will not delete")
                    orphans_seen.append({"key": _orphan_key(it),
                                         "title": it.get("title"), "library": title})
                continue
            it["arrId"] = arr_id

            grade = grade_series_files(port, token, client, it, arr_id, rules,
                                       now, threshold_secs)
            if grade["err"]:
                warn("could not grade series '" + str(it.get("title")) + "' in '"
                     + title + "' (" + grade["err"] + ") — SKIP this run, "
                     "0 files graded (fail closed, not fail silent)")
                partial = True
                continue

            per_lib_totals[title] += grade["total_files"]
            for w in grade["withheld"]:
                withheld_files.append(dict(w, title=it.get("title"), library=title))
            cands.extend(grade["candidates"])

            series_removal_state[(entry["slug"], arr_id)] = {
                "client": client,
                "row": sonarr_series_row(client, arr_id),
                "permanent_tag_id": permanent_tag_id,
                "tag_lookup_ok": tag_lookup_ok,
                "title": it.get("title"),
                "library": title,
                "slug": entry["slug"],
                "arrId": arr_id,
                "candidate_file_count": len(grade["candidates"]),
                "total_files_before": grade["total_files"],
                # Deliberately the show's own container addedAt — NEVER used for
                # candidacy (R-4 forbids that) but reused here purely as a
                # deterministic, documented tiebreak for "which record goes
                # first when the shared --max-items budget is tight this run"
                # (see the P-4/max-items cap-sharing block below).
                "container_added_at": it.get("addedAt") or 0,
                # Filled in below by the shared-budget capping pass.
                "p4_eligible": False,
                "p4_scheduled": False,
            }

        per_lib_candidates[title] = cands
        log("library '" + title + "': " + str(len(items)) + " series, " +
            str(len(cands)) + " resolved candidate file(s)")

    if withheld_files:
        warn(str(len(withheld_files)) + " file(s) WITHHELD (no determinable "
             "clock, R-2 fail-closed): " +
             "; ".join(repr(w.get("title")) + "/S" + str(w.get("seasonNumber")) +
                      " " + str(w.get("path")) for w in withheld_files[:8]) +
             (" +" + str(len(withheld_files) - 8) + " more" if len(withheld_files) > 8 else ""))

    # Totals
    all_cands = [c for cands in per_lib_candidates.values() for c in cands]
    total_count = len(all_cands)
    total_gb = round(sum((c.get("sizeGB", 0) or 0) for c in all_cands), 2)

    # ---- max-items rate cap: DEFER the excess, process the OLDEST N ----
    # max-items is a per-run RATE LIMIT (runaway guard), NOT a tripwire. A backlog
    # larger than the cap must still make forward progress each run — aborting the
    # whole run to zero (the 2026-07-13 failure: >50 aged items after --max-pct was
    # disabled -> whole-run abort -> 0 GB freed while the box was space-constrained)
    # is the worst outcome. Delete the oldest max_items this run; the remainder ages
    # into the next run and self-heals. --force bypasses the cap entirely. (max-pct
    # keeps its whole-run-abort semantics via check_caps below; prod disables it
    # with --max-pct 100.)
    deferred_count = 0
    if not args.force and total_count > args.max_items:
        # Sort by the REAL per-file grading clock (gradedAt), not the
        # container-level addedAt every candidate also still carries (R-4:
        # a show's own addedAt is exactly the field this whole spec exists to
        # stop trusting). Movies always carry gradedAt too (set above), so
        # this sort key is uniform across both kinds.
        oldest_first = sorted(all_cands, key=lambda c: c.get("gradedAt") or c.get("addedAt", 0))
        keep_ids = set(id(c) for c in oldest_first[:args.max_items])
        deferred_count = total_count - args.max_items
        for _title in list(per_lib_candidates.keys()):
            per_lib_candidates[_title] = [
                c for c in per_lib_candidates[_title] if id(c) in keep_ids
            ]
        all_cands = [c for cands in per_lib_candidates.values() for c in cands]
        total_count = len(all_cands)
        total_gb = round(sum((c.get("sizeGB", 0) or 0) for c in all_cands), 2)
        warn("max-items cap: deferring " + str(deferred_count) +
             " candidate(s) to a future run; processing the oldest " +
             str(total_count) + " (" + str(total_gb) + " GB) this run")

    # Deferral can drop some of a series' candidate files back out of scope —
    # recompute each tracked series' actually-scheduled-this-run file count
    # from the POST-deferral truth in per_lib_candidates, so the dry-run
    # "would remove record" prediction (and nothing safety-relevant — the
    # execute path always re-reads the real post-delete count, see
    # _series_would_be_removed's docstring) never over-predicts a removal.
    if series_removal_state:
        by_key_counts = {}
        for cands in per_lib_candidates.values():
            for c in cands:
                if "episodeFileId" in c:
                    k = (c.get("slug"), c.get("arrId"))
                    by_key_counts[k] = by_key_counts.get(k, 0) + 1
        for k, st in series_removal_state.items():
            st["candidate_file_count"] = by_key_counts.get(k, 0)

    # ---- P-4 shared --max-items budget (operator ruling 2026-09-13,
    # envelope-and-manifest/BLOCKER): a series-RECORD removal is a mutation
    # exactly like a file/movie delete and must count against the SAME
    # --max-items runaway guard the module docstring calls "the runaway
    # guard (never delete > N in one run)" — it must never bypass it. This
    # used to be evaluated in its own pass with zero bound at all: 200
    # eligible series fired 200 DELETE /series/<id> calls in one run against
    # --max-items=1. Files go FIRST (the file/movie deferral above already
    # picked the oldest max_items candidates); whatever of the SAME budget is
    # left over is what P-4 record removals may spend this run, oldest-
    # container-first (see container_added_at's docstring at series_removal_
    # state's construction — a tiebreak only, never a candidacy signal), with
    # the remainder deferred and counted, never uncapped. --force bypasses
    # this exactly like the file/movie cap above.
    deferred_series_count = 0
    if series_removal_state:
        eligible_keys = {
            k for k, st in series_removal_state.items()
            if _series_would_be_removed(
                st["row"], st["permanent_tag_id"], st["tag_lookup_ok"],
                st["total_files_before"] - st["candidate_file_count"])
        }
        if args.force:
            scheduled_keys = set(eligible_keys)
        else:
            budget = max(0, args.max_items - total_count)
            oldest_first_keys = sorted(
                eligible_keys,
                key=lambda k: series_removal_state[k]["container_added_at"])
            scheduled_keys = set(oldest_first_keys[:budget])
            deferred_series_count = len(eligible_keys) - len(scheduled_keys)
            if deferred_series_count > 0:
                warn("max-items cap: deferring " + str(deferred_series_count) +
                     " series-record removal(s) (P-4) to a future run — "
                     "file/movie deletes take the shared budget first; " +
                     str(len(scheduled_keys)) + " record(s) scheduled this run")
        for k, st in series_removal_state.items():
            st["p4_eligible"] = k in eligible_keys
            st["p4_scheduled"] = k in scheduled_keys

    # ---- Orphan grace reconciliation (independent of caps + deletes: orphans
    # are never resolved, so never candidates and never deleted). This early pass
    # grades fresh/known + persists first_seen/last_seen + drops resolved orphans,
    # so --json and the dry-run exit code can use it. It is emit_reminders=FALSE:
    # it must NOT stamp last_warned here, because a cap-trip / lock-held abort
    # could return before the summary and silently swallow the weekly WARN. The
    # execute path re-reconciles at the summary (the guaranteed emit point) to
    # actually consume + fire reminders. ----
    now_dt = datetime.now(timezone.utc)
    fresh_orphans, known_orphans, warn_orphans = reconcile_orphans(
        orphans_seen, now_dt,
        grace_hours=args.orphan_grace_hours,
        remind_days=args.orphan_remind_days,
        state_path=args.orphan_state,
        emit_reminders=False,
    )
    for o in known_orphans:
        log("KNOWN ORPHAN (graced) " + repr(o.get("title")) + " <" +
            str(o.get("library")) + "> aged " + str(int(o.get("age_hours", 0))) + "h")

    # ---- Plan printout (always) — per-file counts + GB (movie files count as
    # 1 file each; TV candidates already ARE one row per episode file). ----
    file_count = sum(1 for c in all_cands if "episodeFileId" in c)
    movie_count = total_count - file_count
    log("PLAN: " + str(total_count) + " candidate(s) [" + str(movie_count) +
        " movie(s), " + str(file_count) + " episode file(s)], " +
        str(total_gb) + " GB reclaimable")
    if withheld_files:
        log("WITHHELD: " + str(len(withheld_files)) + " episode file(s) with no "
            "determinable clock (see WARNING above for names)")
    for c in all_cands:
        if "episodeFileId" in c:
            log("  - " + repr(c.get("title")) + " S" + str(c.get("seasonNumber")) +
                " [" + str(c.get("gradeSource")) + "] " + str(c.get("sizeGB")) +
                " GB  <" + str(c.get("library")) + ">  " + str(c.get("path")))
        else:
            log("  - " + repr(c.get("title")) + " (" + str(c.get("year")) + ") [" +
                str(c.get("kind")) + "] " + str(c.get("sizeGB")) + " GB  <" +
                str(c.get("library")) + ">")

    if args.emit_json:
        plan = {
            "mode": mode,
            "threshold_days": args.threshold_days,
            "requested_threshold_days": requested_threshold_days,
            "threshold_days_clamped": was_clamped,
            "total_count": total_count,
            "movie_count": movie_count,
            "episode_file_count": file_count,
            "total_reclaim_gb": total_gb,
            "candidates": [{
                "title": c.get("title"), "year": c.get("year"), "type": c.get("kind"),
                "library": c.get("library"), "sizeGB": c.get("sizeGB"),
                "ratingKey": c.get("ratingKey"), "tmdbId": c.get("tmdbId"),
                "tvdbId": c.get("tvdbId"), "arrId": c.get("arrId"),
                "episodeFileId": c.get("episodeFileId"), "seasonNumber": c.get("seasonNumber"),
                "gradedAt": c.get("gradedAt"), "gradeSource": c.get("gradeSource"),
                "clockDisagreementSec": c.get("clockDisagreementSec"),
            } for c in all_cands],
            "withheld": withheld_files,
            "withheld_count": len(withheld_files),
            "orphans": _orphan_json(fresh_orphans, known_orphans),
            "orphan_counts": {"fresh": len(fresh_orphans), "known": len(known_orphans)},
        }
        print(json.dumps(plan, indent=2), flush=True)

    # ---- Caps (checked BEFORE any mutation) ----
    ok, cap_msgs = check_caps(per_lib_candidates, per_lib_totals,
                              args.max_items, args.max_pct, args.force)
    if not ok:
        msg = "CAP TRIP — aborting before any mutation: " + " | ".join(cap_msgs)
        warn(msg)
        _notify(msg, level="error")
        _push_kuma("down", msg)
        return EXIT_CAP
    if cap_msgs:
        # force override path: log the overridden values at WARNING.
        for m in cap_msgs:
            warn(m)

    # ---- P-4 preview (dry-run only — no mutation, informational). Uses the
    # POST-deferral, POST-shared-budget p4_scheduled flag (set above) so the
    # preview matches exactly what --execute would actually attempt this run
    # — a series past its eligibility check but deferred by the shared
    # --max-items budget is reported as DEFERRED, not REMOVED. The execute
    # path below always re-reads the REAL post-delete count before actually
    # deleting, instead of trusting this prediction. ----
    for (_slug, arr_id), st in series_removal_state.items():
        if st.get("p4_scheduled"):
            log("PLAN: series record " + repr(st["title"]) + " in '" + st["library"]
                + "' (arrId=" + str(arr_id) + ") would be REMOVED after this run "
                "(ended, zero files, not permanent-tagged)")
        elif st.get("p4_eligible"):
            log("PLAN: series record " + repr(st["title"]) + " in '" + st["library"]
                + "' (arrId=" + str(arr_id) + ") is P-4 eligible but DEFERRED this "
                "run (shared --max-items budget)")

    # ---- DRY-RUN: stop here. No manifest, no mutation. ----
    if not execute:
        log("DRY-RUN complete — no mutations performed, no manifest written.")
        # Dry-run is not an incident: never page, heartbeat stays UP. The EXIT CODE
        # is graced though — a FRESH orphan (or operational issue) still returns
        # EXIT_PARTIAL so an operator running a dry-run sees it needs attention
        # before arming --execute; a KNOWN (aged-out) orphan returns EXIT_OK.
        rc, _sev, note = classify_run(partial, fresh_orphans, known_orphans, warn_orphans)
        kmsg = "dry-run: " + str(total_count) + " candidate(s), " + str(total_gb) + " GB"
        if note:
            kmsg += " | " + note
            log(note)
        _push_kuma("up", kmsg)
        return rc

    # ---- EXECUTE ----
    # Run-lock: refuse to overlap another live --execute (double DELETE -> 404 ->
    # spurious partial page). Auto-released on process exit; no stale-lock hazard.
    lock = _acquire_run_lock()
    if lock is None:
        msg = "another qflix-reaper --execute is already running (lock held) — aborting"
        warn(msg)
        _push_kuma("down", msg)
        return EXIT_FATAL
    # Manifest FIRST — the pre-execution record of intent, before any DELETE.
    # series_removal_state is passed through so P-4 record removals scheduled
    # this run land in series_removals[] BEFORE the first DELETE fires, same
    # guarantee the file/movie candidates already had (2026-09-13 finding:
    # this audit trail previously had zero record of P-4 at all).
    manifest_path = write_manifest(Path(args.manifest_dir), args, per_lib_candidates,
                                   series_removal_state)
    log("manifest written: " + str(manifest_path))

    deleted = 0
    libraries_touched = set()
    collections_pruned = 0    # empty husks this run removed (see prune_empty_collections)
    for entry in LIBRARIES:
        title = entry["plex"]
        cands = per_lib_candidates.get(title) or []
        if not cands:
            continue
        try:
            client = _arr_client(entry["slug"])
        except Exception as exc:
            warn("could not build arr client for delete on '" + entry["slug"] + "': " + str(exc))
            partial = True
            continue

        lib_deleted = 0
        for c in cands:
            if entry["kind"] == "movie":
                # R-1: "a movie has one [file]" — deleting the movie record
                # IS the file delete, unchanged prior behaviour.
                ok_del = do_delete_movie(client, c["arrId"])
                label = repr(c.get("title")) + " (arrId=" + str(c["arrId"]) + ")"
            else:
                # R-5, hard requirement: file delete + episode unmonitor as one
                # unit. do_delete_episode fails the WHOLE step if either half
                # fails, so a monitored-with-no-file episode is never left
                # behind for Sonarr's wanted/missing to re-grab.
                ok_del = do_delete_episode(client, c["episodeFileId"], c.get("episodeIds"))
                label = (repr(c.get("title")) + " S" + str(c.get("seasonNumber"))
                        + " episodeFileId=" + str(c["episodeFileId"]))
            if ok_del:
                deleted += 1
                lib_deleted += 1
                log("DELETED " + label)
            else:
                partial = True
                warn("DELETE FAILED " + label)

        # Plex refresh + emptyTrash per library that actually had deletes.
        if lib_deleted > 0:
            libraries_touched.add(title)
            sk = per_lib_section.get(title)
            if sk is not None:
                if not plex_refresh(port, token, sk):
                    partial = True
                    warn("Plex refresh failed for '" + title + "' (non-fatal)")
                if not plex_empty_trash(port, token, sk):
                    partial = True
                    warn("Plex emptyTrash failed for '" + title + "' (non-fatal)")
                pruned = prune_empty_collections(port, token, sk)
                if pruned:
                    collections_pruned += pruned
                    log("pruned " + str(pruned) + " empty collection(s) from '"
                        + title + "'")

    # ---- P-4: series record removal — ended + zero files + not permanent-
    # tagged. Evaluated for every resolved series flagged p4_scheduled (both
    # eligible AND within this run's shared --max-items budget — see the
    # capping block earlier in run()), so a series already parked at zero
    # files from a prior run is swept too, but one deferred by the shared
    # budget is correctly left alone this run (2026-09-13 finding: this pass
    # used to have ZERO bound and fired every eligible removal in one run
    # regardless of --max-items). `remaining` is a FRESH re-read here, never
    # the prediction used in the dry-run preview above — a partial per-file
    # delete failure earlier in this same run must not be papered over. ----
    records_removed = 0
    for (slug, arr_id), st in series_removal_state.items():
        if not st.get("p4_scheduled"):
            continue
        files_now, ferr = sonarr_episode_files(st["client"], arr_id)
        if ferr:
            warn("P-4: could not re-read episodefile count for " +
                 repr(st["title"]) + " (" + ferr + ") — leaving the record "
                 "alone (fail closed, cannot confirm zero files)")
            partial = True
            continue
        if _series_would_be_removed(st["row"], st["permanent_tag_id"],
                                    st["tag_lookup_ok"], len(files_now)):
            if do_delete_series(st["client"], arr_id):
                records_removed += 1
                libraries_touched.add(st["library"])
                log("P-4: removed series record " + repr(st["title"]) +
                    " (arrId=" + str(arr_id) + ", ended, zero files, "
                    "not permanent-tagged)")
            else:
                partial = True
                warn("P-4: record removal FAILED for " + repr(st["title"]) +
                     " (arrId=" + str(arr_id) + ")")

    # ---- Seerr reconciliation (after all libraries) ----
    s_deleted, s_failed = reconcile_seerr(execute=True)
    if s_failed > 0:
        partial = True
    log("Seerr reconciliation: " + str(s_deleted) + " deleted, " + str(s_failed) + " failed")

    # ---- Summary + notify (grace-aware) ----
    # `partial` is now OPERATIONAL-only (delete/plex/seerr/arr). Orphans are graded
    # by classify_run against the grace clock: a fresh orphan reds like today, a
    # known one goes green with a throttled weekly WARN reminder.
    summary = (str(deleted) + " deleted, " + str(total_gb) + " GB reclaimed across " +
               str(len(libraries_touched)) + " libraries")
    # Reported, not silent: an empty collection advertises content the server
    # does not have, and 14 of them accumulated unseen before 2026-08-25.
    if collections_pruned:
        summary += ", " + str(collections_pruned) + " empty collection(s) pruned"
    if records_removed:
        summary += ", " + str(records_removed) + " series record(s) removed (P-4)"
    if withheld_files:
        # Never silent (spec section 5): the summary that reaches Discord must
        # say a file could not be graded, not just the durable log line above.
        summary += ", " + str(len(withheld_files)) + " file(s) withheld (no clock)"
    # Re-reconcile at the guaranteed emit point (emit_reminders=True) so the weekly
    # WARN slot is consumed ONLY when we're about to actually send it — not on an
    # early cap/lock abort. first_seen/last_seen are idempotent under the same now.
    fresh_orphans, known_orphans, warn_orphans = reconcile_orphans(
        orphans_seen, now_dt,
        grace_hours=args.orphan_grace_hours,
        remind_days=args.orphan_remind_days,
        state_path=args.orphan_state,
        emit_reminders=True,
    )
    rc, severity, orphan_note = classify_run(partial, fresh_orphans,
                                             known_orphans, warn_orphans)
    if severity == "error":
        reasons = []
        if partial:
            reasons.append("partial failures")
        if fresh_orphans:
            reasons.append(orphan_note)
        msg = ("completed WITH " + "; ".join(reasons) + " — " + summary +
               " (see journal for details)")
        warn(msg)
        _notify(msg, level="error")
        _push_kuma("down", msg)
    elif severity == "warning":
        # Green run; the weekly orphan reminder came due this run.
        msg = "SUCCESS — " + summary + " | weekly orphan reminder: " + orphan_note
        log(msg)
        _notify(msg, level="warning")
        _push_kuma("up", msg)
    else:
        # Clean, or known orphans not yet due (surfaced, not paged).
        msg = "SUCCESS — " + summary
        if orphan_note:
            msg += " | " + orphan_note
        log(msg)
        _notify(msg, level="info")
        _push_kuma("up", msg)

    if args.emit_json:
        print(json.dumps({
            "mode": mode, "deleted": deleted, "total_reclaim_gb": total_gb,
            "libraries_touched": sorted(libraries_touched),
            "seerr_deleted": s_deleted, "seerr_failed": s_failed,
            "records_removed": records_removed,
            "withheld_count": len(withheld_files), "withheld": withheld_files,
            "partial": partial, "severity": severity,
            "orphans": _orphan_json(fresh_orphans, known_orphans),
            "orphan_counts": {"fresh": len(fresh_orphans), "known": len(known_orphans)},
            "exit": rc, "manifest": str(manifest_path),
        }, indent=2), flush=True)
    _release_run_lock(lock)
    return rc


def main(argv=None) -> int:
    args = parse_args(argv)
    _setup_file_log()
    rc = EXIT_FATAL
    try:
        rc = run(args)
        return rc
    except Exception as exc:
        # Last-resort guard: an unexpected fatal must still page + color the exit,
        # not crash with an opaque traceback into journald.
        msg = "FATAL unexpected error: " + repr(exc)
        warn(msg)
        _notify(msg, level="error")
        _push_kuma("down", msg)
        rc = EXIT_FATAL
        return rc
    finally:
        # Record the outcome in the durable logfile, then close it.
        log("exit code " + str(rc))
        if _LOG_FH is not None:
            try:
                _LOG_FH.close()
            except Exception as _exc:
                sys.stderr.write("qflix-reaper.py: durable log close failed (best-effort, continuing): "
                                 + repr(_exc) + "\n")


if __name__ == "__main__":
    sys.exit(main())
