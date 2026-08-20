#!/usr/bin/env python3
"""qflix-remux-regrab - force Radarr main to re-grab the movies it already
holds as a Remux, at the capped (non-remux) tier.

WHY THIS EXISTS - the concrete failure it repairs
--------------------------------------------------
One member's Plex client could not play a single MOVIE from 2026-07-25 onward
while TV played fine on that same client. (Identity stays out of this repo by
operator directive - the affected account is recorded in the box-side roster
only.) Measured against the live stack on 2026-08-19:

  * Radarr main profile 6 "HD 720p/1080p" (112 of 114 movies) allowed
    Remux-1080p, so every re-grab landed on remux.
  * 23 of the 46 movies-with-a-file were Remux-1080p: 20-37 Mbps video with
    TrueHD / DTS-HD MA 6-8ch audio, 572.2 GB total, oldest file added
    2026-07-05T17:47Z, newest 2026-08-16T21:23Z.
  * The client negotiates targetBitrate 1927 kbps, videoDecision=transcode,
    HardwareAcceleratedCodecs=0, on a shared box with no GPU. Software
    transcoding 30 Mbps TrueHD down to ~1.9 Mbps never keeps up.

scripts/configure/58-remux-cap-enforce.py fixes the SOURCE (no future grab can
be a remux). It does NOT fix the 23 files already on disk, because Radarr never
downgrades an existing file on its own - not on a search, not on an RSS pass,
not with upgradeAllowed=false. Nothing in the stack will replace a file with a
WORSE one. So the only honest repair is destructive: delete the movie FILE and
re-search.

THE ZERO-ACTION ALTERNATIVE - read this before you run --execute
-----------------------------------------------------------------
You probably do not need this script.

qflix-reaper deletes on Plex addedAt with DEFAULT_THRESHOLD_DAYS = 45 (armed
on-box with --execute --max-pct 100 and NO --threshold-days override, so the
repo constant is what runs). Every one of the 23 remux files was added between
2026-07-05 and 2026-08-16, so the reaper ages the whole set out on its own
between roughly 2026-08-19 (the oldest - already at the line as this was
written) and 2026-09-30 (the newest). Anything re-requested afterwards is
re-grabbed under the 58 cap and comes back non-remux. No action required, no
bytes re-downloaded twice, no risk.

Running this script is therefore a SPEED choice, not a necessity: it trades
~572 GB of re-download (and the seedbox quota that costs) for having the
affected member's movie library playable in days instead of six weeks. If the
answer to "can they wait six weeks?" is yes, do nothing and let the reaper work.

A middle path exists and is the recommended one: run with a small --max-items
(the default is 10) so only the most-watched handful are repaired now and the
rest age out naturally.

WHAT IT DOES, per target
------------------------
  1. DELETE /api/v3/moviefile/{movieFileId}  - removes the FILE only. The movie
     RECORD, its monitoring, tags, and history all survive. This is not the
     reaper; nothing is unmonitored and nothing is removed from Radarr.
  2. POST /api/v3/command {"name": "MoviesSearch", "movieIds": [...]} - ONE
     batched command for every movie whose file deletion succeeded. Radarr then
     grabs the best release still ALLOWED, which post-58 is Bluray-1080p.
  3. The searched ids are persisted to a PENDING-SEARCH file first, and dropped
     only once their search actually lands. Read the next block for why that
     file is load-bearing rather than bookkeeping.

THE PENDING-SEARCH FILE - why a failed search cannot self-heal without it
-------------------------------------------------------------------------
An earlier version of this header claimed a failed MoviesSearch repairs itself
on the next run "because targets are re-derived live". That was FALSE, and it
was false in the one direction that loses a movie:

  the target rule is `hasFile AND the file is a remux`. Step 1 deletes the
  file. From that instant the movie has hasFile=false, so it is no longer a
  remux target, so it is NEVER re-derived and NEVER searched again. The member
  is left with a movie that is simply gone - no file, no search, no signal.

So the search is journalled, not retried by hope:

  * every successfully deleted movie_id is written to PENDING_NAME (in the
    state dir, ~/.opt/maint/remux-regrab) BEFORE the search command is issued,
    so a crash in the gap between DELETE and search still leaves the movie
    recorded;
  * if the BATCHED search fails, each movie is retried ONE AT A TIME, so one
    poisoned id cannot strand the other 9;
  * whatever still failed stays in the file, and the next --execute run DRAINS
    it before anything else - including on a run whose target set is empty,
    which is exactly the state a fully-deleted backlog produces.

INTERLOCK - it refuses to work against an uncapped profile
-----------------------------------------------------------
A movie is only a target if its quality profile NO LONGER allows any Remux
tier. Deleting a remux file under a profile that still allows remux just
re-downloads the same remux, having thrown away a perfectly good file and
614 GB of bandwidth for nothing. So: run 58-remux-cap-enforce.py FIRST. Movies
on a still-uncapped profile are excluded from the target set with a logged
reason, and if that empties the target set the run is a loud clean no-op.

The allowed-set walk is RECURSIVE. Radarr's profile items[] is a nested tree
(groups carry their own allowed flag plus a child items[] list); a non-recursive
read is what made an earlier audit agent blame the wrong profile entirely.

SAFETY ENVELOPE (reaper / torrent-janitor parity)
--------------------------------------------------
  - DRY-RUN IS THE DEFAULT. With no flags this enumerates, classifies, prints
    the full manifest and MUTATES NOTHING. --execute is the only flag that
    deletes.
  - --max-items N (default 10) is a per-run RATE LIMIT, not an abort. A backlog
    larger than N does not stop the run: the OLDEST N by file dateAdded are
    repaired this run and the rest are DEFERRED to the next, so a capped run
    always makes forward progress. Same abort->defer shape the reaper adopted
    at aab9e87.
  - Oldest-first is deliberate for determinism and reaper parity. Note the
    tension: the oldest files are also the NEAREST to ageing out of the
    reaper's 45-day window, so a small capped run does the most redundant work.
    That is accepted rather than adding a second ordering knob - one policy,
    one place. If you want the newest repaired first, raise --max-items and
    take the whole set.
  - --max-items must be >= 1 and the parser rejects anything else. PROVEN
    failure, 2026-08-19 review: `--execute --max-items -1` evaluated
    targets[:-1], which is not a cap at all - it took 22 of the 23 targets and
    issued 22 DELETEs. A negative cap does not shrink the blast radius, it
    inverts the slice and WIDENS it. run() clamps as a second belt.
  - UNLINK VS FREE DEPENDS ON THE FILE, NOT ON THE CONFIG FLAG. Radarr main runs
    copyUsingHardlinks=true with an EMPTY recycleBin, so a movie file that STILL
    has a seeding qBittorrent twin has st_nlink >= 2: DELETE /moviefile drops one
    link and zero bytes come back until qflix-torrent-janitor reaps the other at
    ratio >= 2.0. A file with st_nlink == 1 has no twin (usenet import, or the
    torrent was already reaped) and its bytes come back AT DELETE TIME.
    The first version of this script asserted the pessimistic case
    unconditionally, reasoning from the config flag alone. That was wrong on the
    2026-08-20 run: all 23 targets were st_nlink == 1, the quota went 2231G ->
    1658G the moment the deletes landed, and an operator who had believed the
    printed warning would have budgeted for a peak that could not occur. Reading
    a flag is not the same as measuring the file. So this script now STATS each
    target and reports the two buckets separately - reclaimed-now vs
    unlinked-pending-reap. Budget for both copies only for the pending bucket.
  - --force overrides --max-items (logged). It does NOT imply --execute.
  - MANIFEST written BEFORE the first delete, to --manifest-dir (default
    ~/.opt/maint/remux-regrab), and printed to stdout on every run including
    dry-run. The printed manifest IS the review artifact.
  - Durable per-run log under ~/.opt/maint/remux-regrab/ (30-day retention),
    because journald is not the audit trail on this box - see the reaper's
    Kuma-token incident (2026-07-19, 968c1fb): trust the durable logfile.
  - Run-lock (flock) so a manual run cannot race another.
  - Window-aware: deleting media files and firing searches is a box op, so the
    run skips cleanly inside the Monday maintenance window (operator directive)
    unless --ignore-window.

NOT INCLUDED, deliberately:
  - No Kuma push. This is an operator-run one-shot repair, not a timer; a push
    key would need registering in lib/kuma.py and would then sit permanently
    stale. Wire one up only if this ever becomes a scheduled job.
  - No exclude-file. The reaper's 45-day window deletes every one of these
    within six weeks anyway, so a "keep this remux forever" list would be
    fiction. Use --max-items and a manifest review instead.

EXIT CODES (reaper parity):
  0  clean (dry-run plan printed, or execute with zero failures)
  1  partial. Either a file delete failed (that movie keeps its file and is
     re-derived as a target next run), or a search failed after the per-movie
     retry - in which case the movie_id sits in the pending-search file and the
     NEXT --execute run drains it. Re-running is the fix in both cases, but
     only the delete half is re-derived; the search half needs that file.
  2  precondition refused: zero eligible targets and every remux file was
     skipped, either because its profile still allows remux (run 58 first) or
     because Radarr does not return its profile at all. Nothing was deleted.
  3  fatal (Radarr unreachable, secrets unreadable, or a quality-profile
     response that is empty / not a list - see split_profiles)

Stdlib only (urllib/json/argparse/os/sys/datetime/pathlib). `requests` may only
appear transitively via the guarded lib.notify import.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path nudge: lib.secrets / lib.notify (scripts/maint/lib) resolve as a
# merged `lib` namespace package, exactly as the reaper + janitor do it. Do NOT
# add an __init__.py anywhere in those lib dirs - it shadows the other half of
# the namespace and breaks collect.py.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent                       # scripts/maint
_REPO_ROOT = _HERE.parent.parent                              # repo root
_MCP_DIR = _REPO_ROOT / "scripts" / "mcp"
for _p in (str(_HERE), str(_MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.secrets import secrets_dir, read_secret  # noqa: E402

ARR_SLUG = "radarr"          # main Radarr only. radarr2 is a 6-movie anime/
                             # foreign instance with 1 remux file; sonarr main
                             # is ARMED but out of scope. See 58's SCOPE block.
ARR_VERSION = "v3"
TIMEOUT = 30

DEFAULT_MAX_ITEMS = 10
DAY_SECONDS = 86400
_LOG_RETENTION_DAYS = 30

# The search journal. Lives in the STATE dir (_log_dir()), never in
# --manifest-dir: a manifest is a per-run artifact an operator may point
# anywhere, while this file must be found again by the next run or the movies
# it names are lost.
PENDING_NAME = "pending-search.json"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_REFUSED = 2
EXIT_FATAL = 3

_LOG_FH = None
_LOCK_PATH = os.environ.get("QFLIX_REMUX_REGRAB_LOCK", "/tmp/qflix-remux-regrab.lock")


# ===========================================================================
# Logging (reaper parity): stdout/stderr + durable per-day logfile.
# ===========================================================================
def _log_dir() -> Path:
    return Path(os.environ.get(
        "QFLIX_REMUX_REGRAB_LOG_DIR",
        str(Path.home() / ".opt" / "maint" / "remux-regrab"),
    ))


def _setup_file_log() -> None:
    global _LOG_FH
    try:
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        _LOG_FH = open(d / ("remux-regrab-" + day + ".log"), "a", encoding="utf-8")
        cutoff = datetime.now(timezone.utc).timestamp() - _LOG_RETENTION_DAYS * DAY_SECONDS
        for old in d.glob("remux-regrab-*.log"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    except Exception:
        _LOG_FH = None


def _file_log(line: str) -> None:
    if _LOG_FH is None:
        return
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _LOG_FH.write(stamp + " " + line + "\n")
        _LOG_FH.flush()
    except Exception as _exc:
        sys.stderr.write("qflix-remux-regrab.py: durable log write failed "
                         "(best-effort, continuing): " + repr(_exc) + "\n")


def log(msg: str) -> None:
    line = "[qflix-remux-regrab] " + msg
    print(line, flush=True)
    _file_log(line)


def warn(msg: str) -> None:
    line = "[qflix-remux-regrab] WARNING: " + msg
    print(line, file=sys.stderr, flush=True)
    _file_log(line)


def _notify(msg: str, level: str = "info") -> None:
    """Best-effort operator notify. Never raises into the main flow; an absent
    `requests` degrades to a logged no-op (reaper parity)."""
    try:
        from lib.notify import notify  # noqa: WPS433 (lazy + guarded on purpose)
        notify(msg, level=level)
    except Exception as _exc:
        _file_log("notify skipped (" + repr(_exc) + ")")


# ===========================================================================
# Maintenance window (torrent-janitor parity). Deleting media files and firing
# searches are box ops; the operator directive is no box ops inside the window.
# ===========================================================================
def in_maintenance_window(now=None) -> bool:
    if now is None:
        now = datetime.now(timezone.utc)
    if now.weekday() == 0 and 11 <= now.hour < 15:
        return True
    try:
        lock = Path(os.environ.get("MANITOBA_STATE_DIR",
                                   str(Path.home() / ".opt" / "maint"))) / "lock"
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
    except Exception as _exc:
        sys.stderr.write("qflix-remux-regrab.py: window lock check failed "
                         "(best-effort, continuing): " + repr(_exc) + "\n")
    return False


# ===========================================================================
# Run-lock (flock).
# ===========================================================================
def _acquire_run_lock():
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


# ===========================================================================
# Radarr client (stdlib; secrets from ~/secrets/radarr.{key,port,urlbase}).
# ===========================================================================
class Radarr:
    def __init__(self) -> None:
        self.key = read_secret(ARR_SLUG + ".key")
        self.port = read_secret(ARR_SLUG + ".port")
        try:
            # urlbase files carry NO leading slash on this box.
            self.base = read_secret(ARR_SLUG + ".urlbase").strip("/") or ARR_SLUG
        except FileNotFoundError:
            self.base = ARR_SLUG

    def _url(self, path: str) -> str:
        return ("http://127.0.0.1:" + self.port + "/" + self.base
                + "/api/" + ARR_VERSION + path)

    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"X-Api-Key": self.key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self._url(path), data=data,
                                     method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def profiles(self):
        return self._req("GET", "/qualityprofile")

    def movies(self):
        return self._req("GET", "/movie")

    def delete_moviefile(self, file_id) -> None:
        self._req("DELETE", "/moviefile/" + str(file_id))

    def movies_search(self, movie_ids):
        return self._req("POST", "/command",
                         {"name": "MoviesSearch", "movieIds": list(movie_ids)})


# ===========================================================================
# Pure helpers.
#
# NOTE the deliberate duplication of the remux walk with
# scripts/configure/58-remux-cap-enforce.py. 58 is self-contained on purpose
# (it is piped over SSH as `sshm "python3 -" < 58-...py`), so it cannot be
# imported here, and its filename starts with a digit anyway. The shared truth
# is the RULE - "a name containing 'remux', matched recursively" - which is
# pinned for both by tests/unit/test_remux_cap.py.
# ===========================================================================
def is_remux_name(name) -> bool:
    return bool(name) and "remux" in str(name).lower()


def profile_allows_remux(items) -> bool:
    """Recursive. True iff any ALLOWED leaf quality in this profile is a remux
    tier. A group is only traversed, never counted - its allowed flag is a
    container flag, not a grabbable quality."""
    for item in items or []:
        if "id" in item and "name" in item and isinstance(item.get("items"), list):
            if profile_allows_remux(item.get("items")):
                return True
        else:
            if not item.get("allowed"):
                continue
            q = item.get("quality") or {}
            name = q.get("name") if isinstance(q, dict) else None
            if is_remux_name(name):
                return True
    return False


def _file_quality_name(movie_file) -> str:
    q = ((movie_file or {}).get("quality") or {}).get("quality") or {}
    return str(q.get("name") or "")


def _bytes_verdict(rows):
    """Report reclaimed-now vs unlinked-pending-reap from MEASURED link counts.

    The predecessor printed one unconditional "space reclaims only when
    qflix-torrent-janitor reaps" clause because Radarr's config says
    copyUsingHardlinks=true. That describes how files ARRIVE, not whether a
    given file still has its twin, and on 2026-08-20 every one of the 23
    targets was st_nlink == 1 -- the bytes came back at delete time and the
    warning was pure noise pointed at the operator's capacity planning.
    """
    now = round(sum(r["size_gb"] for r in rows if r.get("nlink") == 1), 2)
    pend = round(sum(r["size_gb"] for r in rows if (r.get("nlink") or 0) >= 2), 2)
    unk = round(sum(r["size_gb"] for r in rows if r.get("nlink") is None), 2)
    parts = []
    if now:
        parts.append("freed " + str(now) + " GB now (no seeding twin)")
    if pend:
        parts.append("unlinked " + str(pend) + " GB pending torrent-janitor "
                     "reap at ratio>=2.0 - both copies on disk until then")
    if unk:
        parts.append("removed " + str(unk) + " GB of unstat-able files "
                     "(link count unknown, assume pending)")
    return "; ".join(parts) if parts else "0 GB"


def _nlink(path):
    """st_nlink for a movie file path, or None if it cannot be stat'ed.

    Never raises: a target whose link count is unknown is reported in the
    unknown bucket rather than silently counted as either freed or pending.
    """
    if not path:
        return None
    try:
        return os.stat(path).st_nlink
    except OSError:
        return None


def _gb(nbytes) -> float:
    try:
        return round(int(nbytes) / (1024 ** 3), 2)
    except (TypeError, ValueError):
        return 0.0


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_profiles(profiles):
    """(remux_allowing_ids, capped_ids) from a /qualityprofile response.

    Raises ValueError on an empty or non-list response. That is deliberate and
    it is a FATAL, not a refusal: with zero profiles known, capped_profile_ids
    is empty, so every remux file falls through select_targets' final `else`
    and lands in skipped[] as "profile N not found". The run would then print
    "run 58 first" and exit REFUSED - a confident, wrong instruction produced
    by an API that told us nothing at all. Refusal means "the policy is not in
    place yet"; it must never mean "we could not read the policy".
    """
    if not isinstance(profiles, list) or not profiles:
        raise ValueError(
            "Radarr returned no quality profiles (" + repr(profiles)[:80]
            + ") - cannot tell a capped profile from an uncapped one")
    remux_ids = set()
    capped_ids = set()
    for p in profiles:
        if not isinstance(p, dict):
            continue
        if profile_allows_remux(p.get("items") or []):
            remux_ids.add(p.get("id"))
        else:
            capped_ids.add(p.get("id"))
    return remux_ids, capped_ids


def effective_max_items(value) -> int:
    """Clamp --max-items to >= 1.

    The parser already rejects < 1 (see _positive_int), so this is the second
    belt for a caller that builds an args object directly. It exists because
    the failure mode is not "cap too small" but "cap inverted": targets[:-1]
    keeps everything but the last row, so a -1 cap issued 22 DELETEs against a
    23-movie target set in the 2026-08-19 review.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ITEMS
    return n if n >= 1 else 1


# ---------------------------------------------------------------------------
# Pending-search journal. See the header block: once a file is deleted the
# movie stops being a target, so a lost search is a permanently lost movie.
# ---------------------------------------------------------------------------
def pending_path() -> Path:
    return _log_dir() / PENDING_NAME


def load_pending(path=None):
    """Rows still owed a search. Missing/garbage file degrades to [] with a
    warning - an unreadable journal must not block the deletes it guards."""
    p = Path(path) if path is not None else pending_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:
        warn("pending-search journal unreadable (" + str(exc)
             + ") - treating as empty; a prior failed search may be lost")
        return []
    rows = data.get("pending") if isinstance(data, dict) else data
    out = []
    seen = set()
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        mid = r.get("movie_id")
        if mid is None or mid in seen:
            continue
        seen.add(mid)
        out.append({"movie_id": mid,
                    "title": r.get("title"),
                    "queued_at": r.get("queued_at") or ""})
    return out


def save_pending(rows, path=None) -> None:
    """Atomic rewrite. Best-effort: a journal we cannot write is loudly warned
    about rather than raised, because raising here would abort a run whose
    deletes have already happened."""
    p = Path(path) if path is not None else pending_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(json.dumps({"updated_at": _utc_stamp(),
                                   "pending": list(rows)}, indent=2),
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
    except Exception as exc:
        warn("could not persist the pending-search journal " + str(p) + ": "
             + str(exc) + " - a failed search will NOT be retried next run")


def merge_pending(existing, newly):
    """Union by movie_id, existing rows first, order stable."""
    out = []
    seen = set()
    for r in list(existing or []) + list(newly or []):
        mid = r.get("movie_id")
        if mid is None or mid in seen:
            continue
        seen.add(mid)
        out.append(r)
    return out


def issue_searches(arr, rows):
    """Queue MoviesSearch for `rows`: ONE batch, then per-movie on failure.

    Returns (remaining, searched_ids). `remaining` is the rows whose search
    never landed - the caller persists exactly those, and the next --execute
    run drains them. The per-movie retry exists so one poisoned movie_id (a
    record deleted in Radarr between the two calls, say) cannot strand the
    other nine movies in a batch of ten.
    """
    if not rows:
        return [], []
    ids = [r["movie_id"] for r in rows]
    try:
        arr.movies_search(ids)
        log("MoviesSearch queued for " + str(len(ids)) + " movie(s): " + str(ids))
        return [], ids
    except Exception as exc:
        warn("batched MoviesSearch failed for " + str(len(ids)) + " movie(s) ("
             + str(exc) + ") - retrying one movie at a time")

    remaining = []
    searched = []
    for r in rows:
        mid = r.get("movie_id")
        try:
            arr.movies_search([mid])
            searched.append(mid)
            log("MoviesSearch queued (retry) movie=" + str(mid) + " "
                + str(r.get("title") or "")[:50])
        except Exception as exc:
            remaining.append(r)
            warn("MoviesSearch retry FAILED movie=" + str(mid) + " "
                 + str(r.get("title") or "")[:50] + ": " + str(exc)
                 + " - the file is already deleted, so this id is journalled to "
                 + PENDING_NAME + " and the next --execute run drains it")
    return remaining, searched


def select_targets(movies, remux_profile_ids, capped_profile_ids):
    """Split movies into (targets, skipped).

    A TARGET has a file whose quality is a remux tier AND sits on a profile
    that no longer allows remux. A movie on a still-uncapped profile is
    SKIPPED with a reason rather than silently dropped, because that skip is
    the operator's signal that 58 has not been run yet.

    Sorted oldest-file-first (movieFile.dateAdded ascending) so --max-items
    takes a deterministic prefix.
    """
    targets = []
    skipped = []
    for m in movies or []:
        mf = m.get("movieFile")
        if not m.get("hasFile") or not mf:
            continue
        qname = _file_quality_name(mf)
        if not is_remux_name(qname):
            continue
        pid = m.get("qualityProfileId")
        row = {
            "movie_id": m.get("id"),
            "movie_file_id": mf.get("id"),
            "title": m.get("title"),
            "year": m.get("year"),
            "quality": qname,
            "size_gb": _gb(mf.get("size")),
            "date_added": mf.get("dateAdded") or "",
            "quality_profile_id": pid,
            # MEASURED, not inferred from copyUsingHardlinks. st_nlink == 1
            # means no seeding twin, so the delete frees the bytes immediately;
            # >= 2 means the space waits on qflix-torrent-janitor. Reasoning
            # from the config flag alone got this backwards on 2026-08-20 (see
            # the UNLINK VS FREE note in the header). None = could not stat.
            "nlink": _nlink(mf.get("path")),
        }
        if pid in remux_profile_ids:
            # skip_class is the machine-readable half of `reason`. The two skip
            # classes demand OPPOSITE operator actions - "run 58" vs "fix this
            # movie's profile assignment" - and the refusal message used to
            # assert the first for both.
            row["skip_class"] = "uncapped_profile"
            row["reason"] = ("profile " + str(pid) + " still ALLOWS remux - "
                             "run scripts/configure/58-remux-cap-enforce.py first")
            skipped.append(row)
        elif pid in capped_profile_ids:
            targets.append(row)
        else:
            row["skip_class"] = "unknown_profile"
            row["reason"] = "quality profile " + str(pid) + " not found in Radarr"
            skipped.append(row)
    # movie_id is the tiebreaker so a same-second dateAdded pair still orders
    # deterministically; `or 0` keeps a null id from raising on the compare.
    targets.sort(key=lambda r: (str(r["date_added"]), r["movie_id"] or 0))
    skipped.sort(key=lambda r: (str(r["date_added"]), r["movie_id"] or 0))
    return targets, skipped


# ===========================================================================
# Manifest (reaper parity): written BEFORE any delete, printed always.
# ===========================================================================
def _write_manifest(rows, deferred_rows, skipped, *, dry_run, manifest_dir) -> Path:
    ts = datetime.now(timezone.utc)
    d = Path(manifest_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("remux-regrab-plan-" + ts.strftime("%Y%m%d-%H%M%S")
                + "-" + str(os.getpid()) + ".json")
    payload = {
        "generated_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run": dry_run,
        "arr": ARR_SLUG,
        "targets": rows,
        "deferred": deferred_rows,
        "skipped": skipped,
        "target_gb": round(sum(r["size_gb"] for r in rows), 2),
    }
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))
    return path


def _print_manifest(rows, header: str) -> None:
    if not rows:
        return
    log(header)
    for r in rows:
        log("    movie=" + str(r["movie_id"]).rjust(5)
            + " file=" + str(r["movie_file_id"]).rjust(5)
            + " " + str(r["size_gb"]).rjust(6) + " GB"
            + " " + str(r["quality"]).ljust(13)
            + " added=" + str(r["date_added"])[:10]
            + " profile=" + str(r["quality_profile_id"])
            + " " + str(r["title"])[:48]
            + ("  <- " + r["reason"] if r.get("reason") else ""))


# ===========================================================================
# Run.
# ===========================================================================
def run(args) -> int:
    _setup_file_log()
    mode = "DRY-RUN" if not args.execute else "EXECUTE"
    log("--- qflix-remux-regrab (" + mode + ") max-items=" + str(args.max_items)
        + (" FORCE" if args.force else "") + " ---")
    log("secrets dir: " + str(secrets_dir()))

    if in_maintenance_window() and not args.ignore_window:
        log("in maintenance window - skipping run (operator directive: no box ops)")
        return EXIT_OK

    dry_run = not args.execute
    run_lock = None
    if not dry_run:
        run_lock = _acquire_run_lock()
        if run_lock is None:
            warn("another --execute run holds the lock; skipping")
            return EXIT_OK

    try:
        arr = Radarr()
        profiles = arr.profiles()
        movies = arr.movies()
    except Exception as exc:
        warn("fatal: cannot read Radarr: " + str(exc))
        return EXIT_FATAL

    if not isinstance(movies, list):
        warn("fatal: Radarr /movie returned " + repr(movies)[:80]
             + ", not a list")
        return EXIT_FATAL

    try:
        remux_profile_ids, capped_profile_ids = split_profiles(profiles)
    except ValueError as exc:
        # NOT the refusal path: see split_profiles' docstring.
        warn("fatal: " + str(exc))
        return EXIT_FATAL
    log("profiles: " + str(len(profiles)) + " total, "
        + str(len(remux_profile_ids)) + " still allow remux "
        + str(sorted(x for x in remux_profile_ids if x is not None)))
    log("movies: " + str(len(movies)) + " total")

    targets, skipped = select_targets(movies, remux_profile_ids, capped_profile_ids)

    _print_manifest(skipped, "SKIPPED (" + str(len(skipped)) + "):")

    pending = load_pending()
    if pending:
        log("pending-search journal: " + str(len(pending)) + " movie(s) still "
            "owed a search from a previous run "
            + str([r["movie_id"] for r in pending]))

    if not targets:
        # A fully-deleted backlog produces EXACTLY this state - zero targets,
        # because every one of those movies now has hasFile=false. Drain before
        # returning or the journal is write-only.
        if not dry_run and pending:
            remaining, searched = issue_searches(arr, pending)
            save_pending(remaining)
            log("pending-search drain: " + str(len(searched)) + " queued, "
                + str(len(remaining)) + " still pending")
            if remaining:
                return EXIT_PARTIAL
            if not skipped:
                return EXIT_OK
        if skipped:
            uncapped = [r for r in skipped
                        if r.get("skip_class") == "uncapped_profile"]
            unknown = [r for r in skipped
                       if r.get("skip_class") == "unknown_profile"]
            if len(unknown) > len(uncapped):
                bad = sorted(str(r.get("quality_profile_id")) for r in unknown)
                warn("no eligible targets: " + str(len(unknown)) + " of "
                     + str(len(skipped)) + " remux file(s) sit on quality "
                     "profile id(s) Radarr does not return (" + ",".join(bad)
                     + "). Running 58 will NOT help - reassign those movies to "
                     "a profile that exists, then re-run this in dry-run.")
            else:
                warn("no eligible targets: " + str(len(uncapped)) + " of "
                     + str(len(skipped)) + " remux file(s) sit on a profile that "
                     "still ALLOWS remux. Run "
                     "scripts/configure/58-remux-cap-enforce.py first, then "
                     "re-run this in dry-run to review the plan.")
            return EXIT_REFUSED
        log("no remux files on capped profiles - nothing to do")
        return EXIT_OK

    # max-items: defer the excess, never abort (reaper aab9e87 shape).
    # effective_max_items, not args.max_items: a sub-1 cap inverts the slice.
    cap = effective_max_items(args.max_items)
    if cap != args.max_items:
        warn("max-items " + str(args.max_items) + " is not a cap (a slice from "
             "the END widens the blast radius) - clamped to " + str(cap))
    to_fix = targets
    deferred = []
    if not args.force and len(targets) > cap:
        to_fix = targets[:cap]
        deferred = targets[cap:]
        warn("max-items " + str(cap) + " reached; repairing the OLDEST "
             + str(len(to_fix)) + " and DEFERRING " + str(len(deferred))
             + " to the next run")
    elif args.force and len(targets) > cap:
        warn("--force: ignoring max-items " + str(cap)
             + ", taking all " + str(len(targets)))

    manifest_path = _write_manifest(to_fix, deferred, skipped,
                                    dry_run=dry_run,
                                    manifest_dir=args.manifest_dir or _log_dir())
    verb = "WOULD REPAIR" if dry_run else "REPAIRING"
    _print_manifest(to_fix, verb + " (" + str(len(to_fix)) + "):")
    _print_manifest(deferred, "DEFERRED (" + str(len(deferred)) + "):")
    log("plan manifest: " + str(manifest_path))
    log("re-download cost if executed: ~"
        + str(round(sum(r["size_gb"] for r in to_fix), 2)) + " GB")

    if dry_run:
        log("dry-run: " + str(len(to_fix)) + " movie file(s) would be DELETED and "
            "re-searched. Nothing was changed. Add --execute to arm.")
        log("note: " + _bytes_verdict(to_fix) + " (recycleBin is empty, so "
            "nothing is staged for later cleanup either way)")
        log("zero-action alternative: qflix-reaper's 45-day add-date retention "
            "removes these on its own by ~2026-09-30 - see the header.")
        return EXIT_OK

    deleted = []
    failures = 0
    for r in to_fix:
        try:
            arr.delete_moviefile(r["movie_file_id"])
            deleted.append(r)
            log("DELETED file=" + str(r["movie_file_id"]) + " ("
                + str(r["size_gb"]) + " GB) " + str(r["title"])[:60])
        except urllib.error.HTTPError as exc:
            failures += 1
            body = exc.read().decode("utf-8", errors="replace")[:200]
            warn("delete failed file=" + str(r["movie_file_id"]) + " "
                 + str(r["title"])[:50] + ": " + str(exc.code) + " " + body)
        except Exception as exc:
            failures += 1
            warn("delete failed file=" + str(r["movie_file_id"]) + " "
                 + str(r["title"])[:50] + ": " + str(exc))

    owed = merge_pending(pending, [{"movie_id": r["movie_id"],
                                    "title": r["title"],
                                    "queued_at": _utc_stamp()} for r in deleted])
    if owed:
        # Journal BEFORE the search: the window between DELETE and search is
        # exactly where a crash used to lose a movie forever.
        save_pending(owed)
        remaining, searched = issue_searches(arr, owed)
        save_pending(remaining)
        if remaining:
            failures += len(remaining)
            warn(str(len(remaining)) + " movie(s) are deleted with NO search "
                 "queued. They are journalled in " + str(pending_path())
                 + " and the next --execute run drains them; they will NOT be "
                 "re-derived as targets, because a file-less movie is not a "
                 "remux target.")

    summary = ("repaired " + str(len(deleted)) + " of " + str(len(to_fix))
               + " target(s), " + _bytes_verdict(deleted)
               + ", deferred " + str(len(deferred))
               + ", failures " + str(failures))
    log(summary)

    if failures:
        _notify("Remux regrab: " + summary, "error")
        return EXIT_PARTIAL
    if deleted:
        _notify("Remux regrab: " + summary, "info")
    return EXIT_OK


def _positive_int(value):
    """argparse type for --max-items. Rejects < 1 instead of trusting the
    slice.

    PROVEN failure, 2026-08-19 review: `--execute --max-items -1` evaluated
    targets[:-1] and issued 22 DELETEs against a 23-movie target set. A cap
    that can widen the blast radius is not a cap, so this is a parse error,
    not a clamp-and-continue.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "must be an integer >= 1, got " + repr(value))
    if n < 1:
        raise argparse.ArgumentTypeError(
            "must be >= 1: " + str(n) + " slices from the END of the target "
            "list and WIDENS the blast radius instead of capping it")
    return n


def build_parser():
    ap = argparse.ArgumentParser(
        description=("Delete Remux movie FILES on capped Radarr profiles and "
                     "re-search at the capped tier. Dry-run by default."))
    ap.add_argument("--execute", action="store_true",
                    help="perform real file deletes + searches (the ONLY way to "
                         "mutate). Default dry-run.")
    ap.add_argument("--max-items", type=_positive_int, default=DEFAULT_MAX_ITEMS,
                    help="per-run repair cap, must be >= 1; the OLDEST N are "
                         "repaired and the excess is DEFERRED to the next run. "
                         "Default " + str(DEFAULT_MAX_ITEMS) + ".")
    ap.add_argument("--force", action="store_true",
                    help="override --max-items (logged). Does NOT imply --execute.")
    ap.add_argument("--manifest-dir", default=None,
                    help="where to write the JSON plan manifest. "
                         "Default ~/.opt/maint/remux-regrab.")
    ap.add_argument("--ignore-window", action="store_true",
                    help="run even inside the Monday maintenance window (testing).")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        warn("fatal: " + str(exc))
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
