#!/usr/bin/env python3
"""qflix-anime-janitor - daily corrector for anime-library misclassification.

WHY: Seerr routes anime to the dedicated anime *arr instances (Sonarr2 ->
~/media/Anime, Radarr2 -> ~/media/Anime Movies). Its keyword detection
misfires, so regular shows/movies land in the anime instances and show up in
the Plex Anime / Anime Movies libraries. This is a box-side, once-a-day janitor
- structurally a sibling of qflix-reaper - that re-homes confirmed non-anime
titles OUT of the anime libraries and, since 2026-07-30, re-homes real anime
sitting in the main libraries INTO them. Both directions MOVE.

The reaper deletes; this janitor MOVES (a reversible, non-destructive op) and
inherits the reaper's whole safety envelope.

CLASSIFIER (genre + origin, 4-quadrant; source of truth is the *arr record's
own `genres` + `originalLanguage`):

    Anime/Animation?     originalLanguage name             verdict
    -------------------  ------------------------------    -----------------------------
    yes                  in ANIME_LANGS (Japanese)         anime -> leave
    yes                  anything else (incl. absent)      FLAG (western/unknown cartoon)
    no                   PRESENT, not in ANIME_LANGS       AUTO re-home OUT (live-action)
    no                   in ANIME_LANGS (Japanese)         FLAG (JP live-action / mislabel)
    no                   absent / blank                    FLAG (missing-origin; never move)
    (no genres at all)   -                                 SKIP + FLAG (missing metadata)

AUTO re-home fires ONLY on the narrowest, highest-precision case: NEITHER the
"Animation" NOR the "Anime" genre, AND a PRESENT originalLanguage that is not
Japanese - unambiguous
foreign/Western live-action with no business in an anime library. A missing
language is NOT evidence of non-anime (co-productions, English-dub-primary
entries, metadata gaps) -> flagged, never moved. Missing genres never move.

REVERSE (AUTO-MOVE, since 2026-07-30): a title in a MAIN library (Sonarr/Radarr)
that HAS either the "Animation" OR the "Anime" genre AND Japanese origin is a
misfiled anime -> re-homed INTO the anime library through the same rehome()
path as the forward direction, inheriting its full safety envelope. This was
report-only until the operator corrected it: flagging a misfiled title and
leaving it in place is not a correction. TheTVDB treats "Anime" and "Animation"
as DISTINCT genres and does not always tag both, so matching only "Animation"
silently missed real anime -- see ANIME_GENRES below.

DRY-RUN IS THE DEFAULT. With no flags the janitor enumerates, classifies,
prints the plan + totals, and MUTATES NOTHING. The systemd unit ships in this
safe mode. Arm it with --execute (the ONLY flag that moves anything) once the
dry-run plan is trusted - same ritual as the reaper.

RE-HOME SEQUENCE (per auto-move title; tracked in a durable inflight ledger so a
crash mid-move is resumable):
  0. NOTE: the inflight ledger is WRITE-ONLY. _ledger_path() has exactly one
     call site (a write) and no reader, so a crash mid-move is NOT resumed
     from it -- recovery is the next scheduled run re-deriving the plan from
     live *arr state, which is self-healing but is not what 'resumable'
     implies. The ledger is an audit trail, not a recovery journal.
  1. same-device guard: refuse (flag crossdev) if source/target roots differ in
     st_dev - a cross-device move would double disk usage / risk seeding.
  2. add the title to the TARGET instance (import-existing; no new search).
  3. rename() the folder from the source root to the destination root (same
     device) - anime->main in the forward direction, main->anime in reverse.
  4. rescan the target so it imports the moved files.
  5. remove the title from the SOURCE instance WITHOUT deleting files.
  6. refresh both Plex libraries.

CAPS:
  --max-moves N   per-run rate limit on auto re-homes (default 10). Overflow is
                  DEFERRED to the next run (forward progress), not aborted.
  --max-pct  P    per-library tripwire (default 25). If auto-move candidates
                  LIVE ARMING (council finding, 2026-07-31): the box runs this
                  with an on-box drop-in passing `--execute --max-pct 80`, so the
                  effective live tripwire is 80, NOT the 25 below. The drop-in is
                  deliberately out of git -- a repo clone must not be able to arm
                  a mass move -- but the VALUE was undocumented anywhere, so the
                  repo understated the live blast radius by more than 3x. Same
                  pattern as the reaper (`--max-pct 100`) and the torrent-janitor
                  (`--execute`), both of which were already written down.
                  Check what is actually armed with:
                    systemctl --user cat manitoba-maint-anime-janitor.service
                  exceed P% of an anime library's title count, ABORT the whole
                  run before any mutation (exit 2) - the mass-mislabel circuit
                  breaker (a metadata provider dropping `genres` fleet-wide would
                  otherwise make everything look live-action). --force overrides.

EXCLUSIONS: --exclude-file (default qflix-anime-janitor.exclude next to this
script). Lines: `tvdb:<id>`, `tmdb:<id>`, or a bare `title` (case-insensitive).
'#' comments and blanks ignored. Protects deliberate operator placements.

WINDOW-AWARE: skips (clean exit, Kuma up "window") during the Monday
maintenance window - file moves + arr/Plex mutation are box ops.

EXIT CODES (reaper parity):
  0  clean (dry-run plan, or execute with zero failures)
  1  partial (a per-item step failed; the next run re-derives the plan from
     live *arr state -- it does NOT replay the ledger, which has no reader)
  2  cap trip (max-pct exceeded without --force) - aborted, no mutation
  3  fatal (could not reach an arr / read creds)

Python 3.9 on the box: stdlib only (urllib/json/argparse/os/sys/socket/time/
datetime/pathlib); no match-statement, no backslashes inside f-strings.
`requests` may appear only transitively via the guarded lib.notify import.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path nudge: lib.secrets (scripts/maint/lib) + lib.arr_client
# (scripts/mcp/lib) resolve as a merged namespace package, exactly as the
# reaper does it. _MCP_DIR is inserted last so it wins ties, but both dirs
# contribute their modules to the `lib` namespace.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent                       # scripts/maint
_REPO_ROOT = _HERE.parent.parent                              # repo root
_MCP_DIR = _REPO_ROOT / "scripts" / "mcp"                     # owns lib/arr_client.py
for _p in (str(_HERE), str(_MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.secrets import secrets_dir, read_secret  # noqa: E402

ARR_VERSION = "v3"

# ---------------------------------------------------------------------------
# Instance topology. `from_*` is the anime instance we correct OUT of; `to_*`
# the main instance. Roots are resolved live from each arr's /rootfolder at
# runtime (these are fallbacks / for the same-device guard when the arr is
# empty). Plex section titles match qflix-reaper's canonical mapping.
# ---------------------------------------------------------------------------
ANIME_PAIRS = [
    {
        "kind": "series", "idkey": "tvdbId",
        "from_slug": "sonarr2", "to_slug": "sonarr",
        "from_root": "/home/quadstronaut/media/Anime",
        "to_root": "/home/quadstronaut/media/TV Shows",
        "plex_from": "QFlix - Anime", "plex_to": "QFlix - TV",
        "series_type": "standard",
    },
    {
        "kind": "movie", "idkey": "tmdbId",
        "from_slug": "radarr2", "to_slug": "radarr",
        "from_root": "/home/quadstronaut/media/Anime Movies",
        "to_root": "/home/quadstronaut/media/Movies",
        "plex_from": "QFlix - Anime Movies", "plex_to": "QFlix - Movies",
    },
]

# Main instances scanned for the reverse direction.
MAIN_LIBS = [
    {"kind": "series", "slug": "sonarr",  "idkey": "tvdbId", "plex": "QFlix - TV"},
    {"kind": "movie",  "slug": "radarr",  "idkey": "tmdbId", "plex": "QFlix - Movies"},
]

# REVERSE pairs: real anime sitting in a MAIN library, moved INTO the anime
# library. Same shape as ANIME_PAIRS, so they run through the identical rehome()
# path and inherit its whole safety envelope -- same-device guard, inflight
# ledger, verified import before the source record is touched, rollback of a
# record we created, per-library tripwire and per-run rate limit.
#
# This direction was originally report-only. It is an AUTO-MOVE per operator
# instruction (2026-07-30): flagging a misfiled title and leaving it sitting
# there is not a correction, and five titles had been flagged daily for six days
# with nothing moved.
#
# `to_root` matters more here than in the forward direction: sonarr2 and radarr2
# each expose TWO root folders and the FIRST is the main one, so resolving by
# roots[0] would register the title with the anime *arr while leaving the files
# under the main folder. _resolve_root(prefer=...) pins the intended root.
#
# series_type flips to "anime" so Sonarr uses absolute/scene numbering, which is
# the whole reason the anime instance exists -- the mirror of the forward pair
# setting "standard".
REVERSE_PAIRS = [
    {
        "kind": "series", "idkey": "tvdbId",
        "from_slug": "sonarr", "to_slug": "sonarr2",
        "from_root": "/home/quadstronaut/media/TV Shows",
        "to_root": "/home/quadstronaut/media/Anime",
        "plex_from": "QFlix - TV", "plex_to": "QFlix - Anime",
        "series_type": "anime",
    },
    {
        "kind": "movie", "idkey": "tmdbId",
        "from_slug": "radarr", "to_slug": "radarr2",
        "from_root": "/home/quadstronaut/media/Movies",
        "to_root": "/home/quadstronaut/media/Anime Movies",
        "plex_from": "QFlix - Movies", "plex_to": "QFlix - Anime Movies",
    },
]

ANIMATION_GENRE = "Animation"
# TheTVDB carries "Anime" as a genre SEPARATE from "Animation", and does not
# always tag both. Sonarr surfaces that taxonomy verbatim, so a literal
# `"Animation" in genres` check silently misses anime whose TVDB entry only
# carries "Anime".
#
# Found 2026-07-30 via "Mob Psycho 100" (seriesType=anime, originalLanguage
# Japanese, genres ['Action','Anime','Comedy','Fantasy'] -- no "Animation").
# classify_main_lib fell straight through to ("ignore", "") so it was never even
# flagged for review, while "Chainsaw Man" (which carries BOTH tags) was flagged
# every day. A silent miss, not a visible disagreement.
#
# Widening this is safe in BOTH directions, and strictly reduces risk:
#   - reverse: more genuine anime gets flagged for manual re-route (the point)
#   - forward: `auto_out` requires NOT has_anim, so making has_anim true more
#     often can only REDUCE auto-moves, never cause a false one. It also fixes a
#     false flag -- a JP title tagged only "Anime" currently reaches
#     ("flag", "jp-live-action-or-mislabel") and now correctly returns
#     ("leave", "anime").
# TMDB (Radarr) has no separate "Anime" genre, so this is a Sonarr/TVDB-side gap.
ANIME_GENRES = frozenset({ANIMATION_GENRE, "Anime"})
DEFAULT_ANIME_LANGS = ["Japanese"]
DEFAULT_MAX_MOVES = 10
DEFAULT_MAX_PCT = 25.0

KUMA_BASE = os.environ.get("KUMA_BASE", "http://127.0.0.1:42005")
KUMA_PUSH_KEY = "qflix-anime-janitor"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CAP = 2
EXIT_FATAL = 3

_LOG_FH = None
_LOG_RETENTION_DAYS = 30
DAY_SECONDS = 86400


# ===========================================================================
# Logging (reaper parity): stdout/stderr + durable per-day logfile.
# ===========================================================================
def _log_dir() -> Path:
    return Path(os.environ.get(
        "QFLIX_ANIME_JANITOR_LOG_DIR",
        str(Path.home() / ".opt" / "maint" / "anime-janitor"),
    ))


def _setup_file_log() -> None:
    global _LOG_FH
    try:
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        _LOG_FH = open(d / ("anime-janitor-" + day + ".log"), "a", encoding="utf-8")
        cutoff = datetime.now(timezone.utc).timestamp() - _LOG_RETENTION_DAYS * DAY_SECONDS
        for old in d.glob("anime-janitor-*.log"):
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
        sys.stderr.write("qflix-anime-janitor.py: durable log write failed (best-effort, continuing): "
                         + repr(_exc) + "\n")


def log(msg: str) -> None:
    line = "[qflix-anime-janitor] " + msg
    print(line, flush=True)
    _file_log(line)


def warn(msg: str) -> None:
    line = "[qflix-anime-janitor] WARNING: " + msg
    print(line, file=sys.stderr, flush=True)
    _file_log(line)


# ===========================================================================
# Kuma push (reaper parity). Best-effort; never raises.
# ===========================================================================
def _read_kuma_token() -> str:
    env = os.environ.get("QFLIX_ANIME_JANITOR_KUMA_TOKEN")
    if env:
        return env
    try:
        path = secrets_dir() / "kuma-push-tokens.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get(KUMA_PUSH_KEY, "") or ""
    except Exception:
        return ""


def _push_kuma(status: str, msg: str) -> None:
    import urllib.parse
    import urllib.request
    token = _read_kuma_token()
    if not token:
        warn("no Kuma push token under '" + KUMA_PUSH_KEY + "' - heartbeat NOT pushed")
        return
    qs = urllib.parse.urlencode({"status": status, "msg": msg[:200]})
    url = KUMA_BASE + "/api/push/" + token + "?" + qs
    try:
        urllib.request.urlopen(url, timeout=5).read()
    except Exception as exc:
        warn("Kuma push failed (non-fatal): " + str(exc))


# ===========================================================================
# Maintenance-window guard. Suppress mutation during the Monday window
# (Mon 11:00-15:00 UTC) OR while the window orchestrator holds its lock.
# Time-based check is self-contained and matches the documented window.
# ===========================================================================
def in_maintenance_window(now=None) -> bool:
    if now is None:
        now = datetime.now(timezone.utc)
    if now.weekday() == 0 and 11 <= now.hour < 15:   # Monday, 11:00-14:59 UTC
        return True
    # Also defer if the window orchestrator lockfile is present + alive.
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
        sys.stderr.write("qflix-anime-janitor.py: window lock check failed (best-effort, continuing): "
                         + repr(_exc) + "\n")
    return False


# ===========================================================================
# *arr + Plex clients (reaper parity).
# ===========================================================================
def _arr_client(slug: str):
    from lib.arr_client import ArrClient
    return ArrClient(slug, ARR_VERSION, secrets_dir=secrets_dir())


def _plex_creds():
    return read_secret("plex.port"), read_secret("plex.token")


def _plex_get(port, token, path, query="", timeout=30):
    import urllib.request
    import urllib.error
    host = os.environ.get("PLEX_HOST", "127.0.0.1")
    qs = ("?" + query) if query else ""
    url = "http://" + host + ":" + str(port) + path + qs
    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="ignore")
            code = resp.status
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, {"error": str(e)[:200]}
    try:
        return code, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return code, raw


def plex_refresh(port, token, section_key) -> bool:
    status, _ = _plex_get(port, token, "/library/sections/" + str(section_key) + "/refresh")
    return 200 <= status < 300


def _plex_section_keys(port, token):
    """Return {section_title: key} for all Plex library sections."""
    status, body = _plex_get(port, token, "/library/sections")
    out = {}
    if not isinstance(body, dict):
        return out
    for d in ((body.get("MediaContainer") or {}).get("Directory") or []):
        title = d.get("title")
        key = d.get("key")
        if title is not None and key is not None:
            out[title] = key
    return out


# ===========================================================================
# Classifier (pure functions - unit-tested).
# ===========================================================================
def classify_anime_lib(record, anime_langs) -> tuple:
    """Verdict for a title currently in an ANIME library. Returns
    (action, reason) where action in {auto_out, flag, leave, skip}."""
    genres = record.get("genres") or []
    if not genres:
        return ("skip", "missing-metadata")
    has_anim = bool(ANIME_GENRES.intersection(genres))
    origin = ((record.get("originalLanguage") or {}) or {}).get("name") or ""
    is_anime_origin = origin in anime_langs
    if has_anim and is_anime_origin:
        return ("leave", "anime")
    if has_anim and not is_anime_origin:
        return ("flag", "animation-non-jp")
    # No Animation genre. AUTO-move demands a POSITIVE non-anime signal: a
    # PRESENT originalLanguage whose name is not an anime language. A missing/
    # blank language is NOT evidence of non-anime (co-productions, English-dub-
    # primary entries, or plain *arr metadata gaps) -> flag, never move. This is
    # the council B4 false-positive fix (was: auto_out on mere absence of JP).
    if not origin:
        return ("flag", "missing-origin")
    if is_anime_origin:
        return ("flag", "jp-live-action-or-mislabel")
    return ("auto_out", "live-action-non-jp")


def classify_main_lib(record, anime_langs) -> tuple:
    """Verdict for a title in a MAIN library (reverse direction). Returns
    (action, reason); action in {flag_reverse, ignore}."""
    genres = record.get("genres") or []
    has_anim = bool(ANIME_GENRES.intersection(genres))
    origin = ((record.get("originalLanguage") or {}) or {}).get("name") or ""
    if has_anim and origin in anime_langs:
        return ("flag_reverse", "anime-in-main-lib")
    return ("ignore", "")


# ===========================================================================
# Exclusions (pure).
# ===========================================================================
def load_exclusions(path: Path):
    """Return a set of exclusion tokens: 'tvdb:<id>', 'tmdb:<id>', 'title:<lc>'.
    Missing file -> empty set (warned by caller)."""
    tokens = set()
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("tvdb:") or low.startswith("tmdb:"):
            tokens.add(low.replace(" ", ""))
        else:
            tokens.add("title:" + low)
    return tokens


def is_excluded(record, idkey, tokens) -> bool:
    if not tokens:
        return False
    idval = record.get(idkey)
    if idval is not None:
        prefix = "tvdb:" if idkey == "tvdbId" else "tmdb:"
        if (prefix + str(idval)) in tokens:
            return True
    title = (record.get("title") or "").strip().lower()
    if title and ("title:" + title) in tokens:
        return True
    return False


# ===========================================================================
# Ledger (durable, crash-resumable).
# ===========================================================================
def _ledger_path() -> Path:
    return _log_dir() / "inflight.json"


def _moved_path() -> Path:
    return _log_dir() / "moved.json"


def _append_json_list(path: Path, entry: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = []
        data.append(entry)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception as exc:
        warn("ledger write failed (non-fatal): " + str(exc))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ===========================================================================
# Re-home (execute path).
# ===========================================================================
def _resolve_root(client, prefer=None):
    """Root folder path from an arr's /rootfolder, preferring `prefer`.

    `roots[0]` alone is WRONG for the anime instances. sonarr2 and radarr2 each
    carry TWO roots and the first is the MAIN one:

        sonarr2  -> ['/home/.../media/TV Shows', '/home/.../media/Anime']
        radarr2  -> ['/home/.../media/Movies',   '/home/.../media/Anime Movies']

    So a reverse re-home (main -> anime) that trusted roots[0] would register the
    title with the anime *arr but drop the files back under the MAIN folder --
    the arr would look right while Plex's Anime library never saw it. The pair
    declares where it intends to land; honour that when the destination really
    offers it, and fall back to roots[0] only when it does not.
    """
    code, roots = client.get("/rootfolder")
    if code != 200 or not isinstance(roots, list) or not roots:
        return None
    paths = [r.get("path") for r in roots if r.get("path")]
    if prefer and prefer in paths:
        return prefer
    if prefer:
        # COUNCIL FINDING: falling back to paths[0] here fails OPEN in exactly
        # the direction this function's own comment warns about -- on sonarr2 /
        # radarr2 roots[0] is the MAIN root, so a missing Anime root would send
        # the files back under the main tree while the record lived in the anime
        # *arr. _is_contained() cannot catch it: it compares against the
        # ALREADY-RESOLVED root, so a wrong-but-consistent root passes.
        #
        # A caller that named a destination gets that destination or nothing.
        # Returning None aborts the move (rehome treats it as unresolvable),
        # which costs one deferred title instead of misfiling media.
        return None
    return paths[0] if paths else None


def _default_quality_profile(client):
    code, profs = client.get("/qualityprofile")
    if code == 200 and isinstance(profs, list) and profs:
        return profs[0].get("id")
    return None


def _valid_id(idval) -> bool:
    """A usable external id (tvdbId/tmdbId) is a POSITIVE integer. Reject the
    falsy sentinel (None/0/''/'0') that real *arr installs assign to unmatched
    titles - an id-match guard against it would adopt an unrelated record
    (council B-IDVAL)."""
    try:
        return int(idval) > 0
    except (TypeError, ValueError):
        return False


def _is_contained(path, root) -> bool:
    """True iff realpath(path) is strictly inside realpath(root). Guards
    os.rename from being driven outside the target library by an arr-supplied
    path (council B-SEC path traversal)."""
    if not path or not root:
        return False
    try:
        rp = os.path.realpath(str(path))
        rr = os.path.realpath(str(root))
        return rp != rr and os.path.commonpath([rp, rr]) == rr
    except (ValueError, OSError):
        return False


def _pinned_rename(src_path, dst_path, dst_root):
    """os.rename into a directory whose identity was VERIFIED, not re-resolved.

    COUNCIL FINDING 14 (TOCTOU). _is_contained() resolves PATHS, then os.rename
    resolves them again independently. Anything that changes a symlink component
    of dst_path's ancestry between those two resolutions redirects the write, and
    the containment check has already passed -- so media lands outside the
    library root with every guard reporting success.

    Renaming relative to an open directory descriptor closes that window: the fd
    refers to a specific directory INODE, so re-resolution never happens and a
    swapped symlink simply is not consulted. The fstat/stat comparison catches a
    swap that landed between our own realpath() and open().

    Returns None on success, or an error string. Never raises.

    Residual, stated honestly: an adversary who can MOVE the validated directory
    itself (not just re-point a symlink at it) still wins, because the fd follows
    the inode. Closing that needs the whole ancestry walked with O_NOFOLLOW, which
    is not worth the complexity on a single-tenant box -- the realistic threat
    here is a buggy or hostile *arr-supplied path, not a local attacker.
    """
    base = os.path.basename(dst_path)
    if not base:
        return "refusing rename: destination has no final component: " + str(dst_path)
    parent = os.path.dirname(os.path.abspath(dst_path))

    try:
        real_parent = os.path.realpath(parent)
        real_root = os.path.realpath(str(dst_root))
    except (OSError, ValueError) as exc:
        return "refusing rename: cannot resolve destination parent (" + str(exc) + ")"

    # The parent may BE the root (the usual case: <root>/<Title>) or sit under it.
    if real_parent != real_root and not _is_contained(real_parent, real_root):
        return ("refusing rename: destination parent " + real_parent
                + " is not under " + real_root)

    if os.rename not in getattr(os, "supports_dir_fd", set()):
        # Windows / any platform without renameat. Behaves exactly as before --
        # the pin is a hardening, and its absence must not stop a legitimate move.
        try:
            os.rename(src_path, dst_path)
        except OSError as exc:
            return "rename failed: " + str(exc)
        return None

    try:
        fd = os.open(real_parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        return "refusing rename: cannot open destination parent (" + str(exc) + ")"
    try:
        # Prove the fd is the directory we just validated, not one swapped in
        # between realpath() and open().
        fst = os.fstat(fd)
        st = os.stat(real_parent)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            return ("refusing rename: destination parent changed identity "
                    "mid-check (" + real_parent + ")")
        try:
            os.rename(src_path, base, dst_dir_fd=fd)
        except OSError as exc:
            return "rename failed: " + str(exc)
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            sys.stderr.write("qflix-anime-janitor.py: closing destination dir fd "
                             "failed (best-effort, continuing): "
                             + repr(exc) + "\n")
    return None


def _verify_import(dst, kind, new_id, *, attempts=15, delay=2, sleeper=None) -> bool:
    """Poll the target record until it reports imported files, or give up.
    RescanSeries/RescanMovie are ASYNC; removing the source before the import
    lands orphans the media (council B-IMPORT). Returns True iff files imported."""
    if sleeper is None:
        sleeper = time.sleep
    path = ("/series/" if kind == "series" else "/movie/") + str(new_id)
    for i in range(attempts):
        code, rec = dst.get(path)
        if code == 200 and isinstance(rec, dict):
            if kind == "series":
                stats = rec.get("statistics") or {}
                if (stats.get("episodeFileCount") or 0) > 0:
                    return True
            elif rec.get("hasFile"):
                return True
        if i < attempts - 1:
            sleeper(delay)
    return False


_LOCK_PATH = os.environ.get("QFLIX_ANIME_JANITOR_LOCK", "/tmp/qflix-anime-janitor.lock")


def _acquire_run_lock():
    """Exclusive non-blocking flock so two overlapping --execute runs can't both
    add+rename the same title (council B5; mirrors reaper._acquire_run_lock).
    Returns an open handle on success, True where fcntl is unavailable (test
    host), or None if another run holds it."""
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


def rehome(pair, record, *, section_keys, plex_port, plex_token):
    """Re-home one title from its anime instance to the main instance (EXECUTE
    path only). Returns (ok, note); never raises.

    Ordering is FAIL-SAFE: the source record and its files survive until the
    move is VERIFIED imported at the target, so a failure at any step leaves
    recoverable state, never an orphan. A record WE created is rolled back on
    abort; an ADOPTED (pre-existing) record is never destroyed (council)."""
    kind = pair["kind"]
    idkey = pair["idkey"]
    idval = record.get(idkey)
    title = record.get("title") or "?"

    if not _valid_id(idval):
        return (False, "invalid id (" + str(idval) + ")")
    from_path = record.get("path")
    if not from_path:
        return (False, "no source path on record")

    src = _arr_client(pair["from_slug"])
    dst = _arr_client(pair["to_slug"])
    to_root = _resolve_root(dst, prefer=pair.get("to_root")) or pair["to_root"]
    from_root = pair["from_root"]

    # same-device guard (fast-path; os.rename also raises EXDEV as a backstop).
    try:
        if os.stat(from_root).st_dev != os.stat(to_root).st_dev:
            return (False, "crossdev: " + from_root + " vs " + to_root)
    except OSError as exc:
        return (False, "stat failed: " + str(exc))

    _append_json_list(_ledger_path(), {
        "ts": _utc_now(), "step": "planned", "kind": kind, idkey: idval,
        "title": title, "from": pair["from_slug"], "to": pair["to_slug"],
        "from_path": from_path,
    })

    # 1. add to target (import-existing; no search). created=True iff WE created
    #    the record. to_path is the arr-assigned folder (used only after a
    #    containment check); fall back to basename under to_root if absent.
    new_id, created, to_path = _add_to_target(dst, pair, idval, record, to_root)
    if new_id is None:
        return (False, "add-to-target failed")
    if not to_path:
        to_path = os.path.join(to_root, os.path.basename(from_path))

    def _rollback():
        # Only ever undo a record WE created; never delete an adopted real one.
        if created:
            ep = ("/series/" if kind == "series" else "/movie/") + str(new_id)
            try:
                dst.delete(ep, query="deleteFiles=false")
            except Exception as _exc:
                sys.stderr.write("qflix-anime-janitor.py: rollback: target record delete failed (best-effort, continuing): "
                                 + repr(_exc) + "\n")

    # FILELESS record: no folder on disk (a monitored title with no downloaded
    # files) - there is nothing to move. Migrate the RECORD only (remove from
    # source, no rename/import) so a future download lands in the correct
    # instance/library. This is the common "misrouted but not yet grabbed" case.
    if not os.path.exists(from_path):
        if kind == "series":
            code, _ = src.delete("/series/" + str(record.get("id")),
                                 query="deleteFiles=false&addImportListExclusion=false")
        else:
            code, _ = src.delete("/movie/" + str(record.get("id")),
                                 query="deleteFiles=false&addImportExclusion=false")
        if not (200 <= (code or 0) < 300):
            _rollback()
            return (False, "fileless: remove-source failed HTTP " + str(code))
        _append_json_list(_moved_path(), {
            "ts": _utc_now(), "kind": kind, idkey: idval, "title": title,
            "from": pair["from_slug"], "to": pair["to_slug"], "new_id": new_id,
            "note": "record-only (fileless source)",
        })
        log("MIGRATED (record-only, fileless) '" + title + "' "
            + pair["from_slug"] + " -> " + pair["to_slug"])
        return (True, "moved (record-only, fileless)")

    # 2. containment: the destination MUST be inside to_root (council B-SEC).
    if not _is_contained(to_path, to_root):
        _rollback()
        return (False, "path escape: " + str(to_path) + " not under " + str(to_root))

    log("EXECUTE rehome " + kind + " '" + title + "' " + idkey + "=" + str(idval)
        + " : " + pair["from_slug"] + " -> " + pair["to_slug"]
        + " | " + from_path + " -> " + to_path)

    # 3. reconcile destination: reclaim the arr's empty stub; refuse an occupant.
    if os.path.exists(to_path):
        try:
            if os.path.isdir(to_path) and not os.listdir(to_path):
                os.rmdir(to_path)
            else:
                _rollback()
                return (False, "target path occupied: " + to_path)
        except OSError as exc:
            _rollback()
            return (False, "target reclaim failed: " + str(exc))

    # 4. move files (same-device rename; EXDEV/other -> abort + rollback).
    #    Pinned to a validated destination directory -- the containment check
    #    above resolves paths, this resolves them once more, and _pinned_rename
    #    closes the gap between the two (council finding 14).
    err = _pinned_rename(from_path, to_path, to_root)
    if err:
        _rollback()
        return (False, err)

    # 5. rescan, then VERIFY the import landed BEFORE touching the source.
    _rescan_target(dst, kind, new_id)
    if not _verify_import(dst, kind, new_id):
        # COUNCIL FINDING (undisputed, proven by executed test): this used to
        # return here having ALREADY renamed, leaving files at the destination
        # while the SOURCE *arr record still pointed at a path that no longer
        # exists. That is not "leave both" -- the source record is stale on disk,
        # so the source *arr can drop its MediaFile rows on the next disk scan,
        # mark the episodes missing, and hand them to the missing-search sweep to
        # re-grab. Re-downloading media we already hold is a real cost, and the
        # comment claiming it avoided orphaning was describing the opposite of
        # what the code did.
        #
        # Put the files BACK. The rename is the only mutation so far that the
        # source cares about, and reverting it restores the exact pre-move state
        # the source record still describes. Only if the revert ITSELF fails is
        # there genuinely nothing safe left to do -- and that is reported loudly
        # with both paths, because it is the one case a human must resolve.
        #
        # Deliberately NOT _pinned_rename: council finding 14 is about the
        # FORWARD move. Pinning can REFUSE, and a refusal here would convert a
        # recoverable state into a stranded one. This restores files to a path
        # the source record still describes and that existed seconds ago, so the
        # containment question is already settled -- declining to put media back
        # because its parent failed a re-validation is strictly worse than
        # putting it back.
        try:
            os.rename(to_path, from_path)
        except OSError as exc:
            _rollback()
            return (False, "import-unverified AND revert failed: files stranded at "
                    + to_path + " while " + str(record.get("title"))
                    + " still points at " + from_path + " (" + str(exc) + ")")
        _rollback()
        return (False, "import-unverified (reverted; files back at " + from_path + ")")

    # 6. remove source WITHOUT deleting files; a failed delete is partial.
    if kind == "series":
        code, _ = src.delete("/series/" + str(record.get("id")),
                             query="deleteFiles=false&addImportListExclusion=false")
    else:
        code, _ = src.delete("/movie/" + str(record.get("id")),
                             query="deleteFiles=false&addImportExclusion=false")
    if not (200 <= (code or 0) < 300):
        return (False, "remove-source failed HTTP " + str(code)
                + " (imported at target; source record now stale)")

    # 7. refresh both Plex libraries (non-load-bearing).
    if plex_token:
        for plex_title in (pair["plex_from"], pair["plex_to"]):
            key = section_keys.get(plex_title)
            if key is not None:
                plex_refresh(plex_port, plex_token, key)

    _append_json_list(_moved_path(), {
        "ts": _utc_now(), "kind": kind, idkey: idval, "title": title,
        "from": pair["from_slug"], "to": pair["to_slug"],
        "from_path": from_path, "to_path": to_path, "new_id": new_id,
    })
    return (True, "moved")


def _add_to_target(dst, pair, idval, record, to_root):
    """Add the title to the target arr for import-existing. Returns
    (id, created, path):
      - created=True iff we POSTed a NEW record (safe to roll back on abort);
        an ADOPTED pre-existing record has created=False and must NEVER be
        rollback-deleted (council B-ROLLBACK).
      - path is the target arr's assigned folder (council B2), consumed by the
        caller only after a containment check.
    Returns (None, False, None) on failure. The 'already present' branch adopts
    ONLY a record whose id MATCHES the request - some *arr builds ignore the
    query filter and return everything (council B8); a falsy id never matches
    (council B-IDVAL)."""
    kind = pair["kind"]
    idkey = pair["idkey"]
    if not _valid_id(idval):
        return (None, False, None)
    qp = _default_quality_profile(dst)
    if qp is None:
        warn("no quality profile on target " + pair["to_slug"])
        return (None, False, None)

    coll = "/series" if kind == "series" else "/movie"
    qkey = "tvdbId" if kind == "series" else "tmdbId"

    code, existing = dst.get(coll, query=qkey + "=" + str(idval))
    if code == 200 and isinstance(existing, list):
        for r in existing:
            rid = r.get(idkey)
            if _valid_id(rid) and int(rid) == int(idval):
                return (r.get("id"), False, r.get("path"))

    if kind == "series":
        code, look = dst.get("/series/lookup", query="term=tvdb:" + str(idval))
        payload = look[0] if (code == 200 and isinstance(look, list) and look) else None
    else:
        code, look = dst.get("/movie/lookup/tmdb", query="tmdbId=" + str(idval))
        payload = look if (code == 200 and isinstance(look, dict)) else None
    if not isinstance(payload, dict):
        return (None, False, None)
    # The lookup must be the SAME title we asked for.
    if not (_valid_id(payload.get(idkey)) and int(payload.get(idkey)) == int(idval)):
        return (None, False, None)

    payload["qualityProfileId"] = qp
    payload["rootFolderPath"] = to_root
    payload["monitored"] = bool(record.get("monitored", True))
    if kind == "series":
        payload["seriesType"] = pair.get("series_type", "standard")
        payload["addOptions"] = {"searchForMissingEpisodes": False,
                                 "searchForCutoffUnmetEpisodes": False}
    else:
        payload["minimumAvailability"] = record.get("minimumAvailability", "released")
        payload["addOptions"] = {"searchForMovie": False}
    code, resp = dst.post(coll, body=payload)
    if code in (200, 201) and isinstance(resp, dict):
        return (resp.get("id"), True, resp.get("path"))
    return (None, False, None)


def _rescan_target(dst, kind, new_id):
    if kind == "series":
        dst.post("/command", body={"name": "RescanSeries", "seriesId": new_id})
    else:
        dst.post("/command", body={"name": "RescanMovie", "movieIds": [new_id]})


# ===========================================================================
# Enumerate + plan.
# ===========================================================================
def _list_titles(slug, kind):
    client = _arr_client(slug)
    path = "/series" if kind == "series" else "/movie"
    code, body = client.get(path)
    if code != 200 or not isinstance(body, list):
        return None
    return body


def run(args) -> int:
    _setup_file_log()
    anime_langs = set(args.anime_lang or DEFAULT_ANIME_LANGS)

    # Window guard
    if in_maintenance_window() and not args.ignore_window:
        log("in maintenance window - skipping run")
        _push_kuma("up", "skipped (maintenance window)")
        return EXIT_OK

    # Exclusions
    excl_path = Path(args.exclude_file) if args.exclude_file else (
        _HERE / "qflix-anime-janitor.exclude")
    try:
        tokens = load_exclusions(excl_path)
    except FileNotFoundError:
        warn("exclude file missing (" + str(excl_path) + ") - proceeding with none")
        tokens = set()

    dry_run = not args.execute

    # Run-lock only matters when armed: two overlapping --execute runs (timer +
    # manual) could both add+rename the same title (council B5).
    run_lock = None
    if not dry_run:
        run_lock = _acquire_run_lock()
        if run_lock is None:
            warn("another --execute run holds the lock; skipping")
            _push_kuma("up", "skipped (locked)")
            return EXIT_OK

    # Plex is NOT load-bearing (design 10): classify + re-home never need it, so
    # missing plex secrets must NOT be fatal (council B7) - refreshes just skip.
    try:
        plex_port, plex_token = _plex_creds()
    except Exception:
        plex_port, plex_token = None, None
        warn("Plex creds unavailable - refreshes skipped (non-fatal)")
    section_keys = _plex_section_keys(plex_port, plex_token) if plex_token else {}

    auto_candidates = []   # (pair, record)
    flags = []             # dicts for report
    lib_counts = {}        # slug -> total titles
    fatal = False

    # --- anime libraries: find non-anime to move out ---
    only = set(args.library or [])
    for pair in ANIME_PAIRS:
        if only and pair["from_slug"] not in only:
            continue
        titles = _list_titles(pair["from_slug"], pair["kind"])
        if titles is None:
            warn("could not enumerate " + pair["from_slug"] + " - skipping")
            fatal = True
            continue
        lib_counts[pair["from_slug"]] = len(titles)
        for rec in titles:
            if is_excluded(rec, pair["idkey"], tokens):
                continue
            action, reason = classify_anime_lib(rec, anime_langs)
            if action == "auto_out":
                # A falsy id can't be re-homed safely (id-match guard is
                # meaningless at the sentinel) - flag it, never move (B-IDVAL).
                if _valid_id(rec.get(pair["idkey"])):
                    auto_candidates.append((pair, rec))
                else:
                    flags.append({"lib": pair["from_slug"], "title": rec.get("title"),
                                  pair["idkey"]: rec.get(pair["idkey"]), "reason": "invalid-id"})
            elif action in ("flag", "skip"):
                flags.append({"lib": pair["from_slug"], "title": rec.get("title"),
                              pair["idkey"]: rec.get(pair["idkey"]), "reason": reason})

    # --- main libraries: reverse direction, AUTO-MOVE into the anime library ---
    # Candidates join the same auto_candidates list as the forward direction, so
    # the tripwire, the rate limit and rehome() treat both identically. An
    # enumeration failure here is NOT fatal: the main instances are not the
    # subject of the forward correction, so a main-arr outage degrades this run
    # to forward-only rather than aborting a run that could still do useful work.
    for pair in REVERSE_PAIRS:
        if only and pair["from_slug"] not in only:
            continue
        titles = _list_titles(pair["from_slug"], pair["kind"])
        if titles is None:
            warn("could not enumerate " + pair["from_slug"] + " (reverse) - skipping")
            continue
        lib_counts[pair["from_slug"]] = len(titles)
        for rec in titles:
            if is_excluded(rec, pair["idkey"], tokens):
                continue
            action, reason = classify_main_lib(rec, anime_langs)
            if action == "flag_reverse":
                auto_candidates.append((pair, rec))

    # An anime-instance enumeration failure is EXIT_FATAL per design 10 - never
    # masked just because another instance yielded candidates (council B6).
    if fatal:
        _emit(args, auto_candidates, flags, aborted=True)
        _push_kuma("down", "fatal: could not enumerate an anime instance")
        return EXIT_FATAL

    # --- max-pct tripwire (per anime library) ---
    per_lib_auto = {}
    for pair, rec in auto_candidates:
        per_lib_auto[pair["from_slug"]] = per_lib_auto.get(pair["from_slug"], 0) + 1
    # The tripwire ABORTS only when armed (--execute): its job is to stop a
    # mass-mislabel MUTATION. In dry-run there is nothing to mutate, so we warn
    # but still show the full plan (that is the whole point of a dry-run).
    if not args.force:
        for slug, n in per_lib_auto.items():
            total = lib_counts.get(slug, 0)
            if total > 0 and (100.0 * n / total) > args.max_pct:
                msg = ("cap trip: " + str(n) + "/" + str(total) + " ("
                       + str(round(100.0 * n / total, 1)) + "%) of " + slug
                       + " flagged non-anime > max-pct " + str(args.max_pct))
                if dry_run:
                    warn(msg + " (dry-run: showing plan; would ABORT on --execute)")
                else:
                    warn(msg + " - ABORTING before any mutation")
                    _emit(args, auto_candidates, flags, aborted=True)
                    _push_kuma("down", msg)
                    return EXIT_CAP

    # --- apply max-moves (defer excess) ---
    deferred = 0
    to_move = auto_candidates
    if len(auto_candidates) > args.max_moves:
        deferred = len(auto_candidates) - args.max_moves
        to_move = auto_candidates[:args.max_moves]
        warn("max-moves " + str(args.max_moves) + " reached; deferring "
             + str(deferred) + " to next run")

    # --- execute (only when armed; dry-run mutates nothing and never calls rehome) ---
    moved = 0
    failures = 0
    if not dry_run:
        for pair, rec in to_move:
            ok, note = rehome(pair, rec, section_keys=section_keys,
                              plex_port=plex_port, plex_token=plex_token)
            if ok:
                moved += 1
            else:
                failures += 1
                flags.append({"lib": pair["from_slug"], "title": rec.get("title"),
                              pair["idkey"]: rec.get(pair["idkey"]),
                              "reason": "rehome-failed: " + note})
                warn("rehome failed '" + str(rec.get("title")) + "': " + note)

    _emit(args, auto_candidates, flags, moved=moved, deferred=deferred,
          failures=failures)

    # --- Kuma + exit code ---
    if failures:
        _push_kuma("down", "partial: " + str(failures) + " rehome failure(s), "
                   + str(len(flags)) + " flagged")
        return EXIT_PARTIAL
    if dry_run:
        _push_kuma("up", "dry-run: " + str(len(auto_candidates))
                   + " auto-move candidate(s), " + str(len(flags)) + " flagged")
    else:
        _push_kuma("up", "moved " + str(moved) + ", deferred " + str(deferred)
                   + ", flagged " + str(len(flags)))
    return EXIT_OK


def _emit(args, auto_candidates, flags, *, moved=0, deferred=0, failures=0,
          aborted=False):
    if args.emit_json:
        out = {
            "aborted": aborted,
            "auto_move_candidates": [
                {"lib": p["from_slug"], "title": r.get("title"),
                 p["idkey"]: r.get(p["idkey"])}
                for (p, r) in auto_candidates
            ],
            "flags": flags,
            "moved": moved, "deferred": deferred, "failures": failures,
            "dry_run": not args.execute,
        }
        print(json.dumps(out, indent=2))
        return
    log("=== plan ===")
    # Direction-aware wording. Both directions move, so a single "MOVE OUT"
    # label misread the reverse ones as leaving the anime library when they are
    # entering it -- on the exact screen an operator reads before arming a
    # mutation.
    log("auto-move candidates (misfiled in either direction): "
        + str(len(auto_candidates)))
    for p, r in auto_candidates:
        _arrow = "INTO ANIME" if p["to_slug"].endswith("2") else "OUT OF ANIME"
        log("  MOVE " + _arrow + " " + p["from_slug"] + " -> " + p["to_slug"]
            + ": '" + str(r.get("title")) + "'")
    log("flags (manual review): " + str(len(flags)))
    for f in flags:
        log("  FLAG " + str(f.get("lib")) + ": '" + str(f.get("title"))
            + "' - " + str(f.get("reason")))
    if not args.execute:
        log("(dry-run - nothing moved; add --execute to arm)")
    else:
        log("moved=" + str(moved) + " deferred=" + str(deferred)
            + " failures=" + str(failures))


def build_parser():
    ap = argparse.ArgumentParser(description="Correct anime-library misclassification (reaper-parity).")
    ap.add_argument("--execute", action="store_true",
                    help="perform real re-homes (the ONLY way to mutate). Default is dry-run.")
    ap.add_argument("--max-moves", type=int, default=DEFAULT_MAX_MOVES,
                    help="per-run rate limit on auto re-homes (excess deferred). Default 10.")
    ap.add_argument("--max-pct", type=float, default=DEFAULT_MAX_PCT,
                    help="per-library abort tripwire (percent). Default 25.")
    ap.add_argument("--force", action="store_true",
                    help="override the max-pct tripwire (logged). Does NOT imply --execute.")
    ap.add_argument("--exclude-file", default=None,
                    help="exclusions (tvdb:/tmdb:/title). Default qflix-anime-janitor.exclude.")
    ap.add_argument("--anime-lang", action="append", default=None,
                    help="origin language(s) counted as anime (repeatable). Default Japanese.")
    ap.add_argument("--library", action="append", default=None,
                    help="restrict to specific arr slug(s) (repeatable).")
    ap.add_argument("--json", dest="emit_json", action="store_true",
                    help="emit a structured JSON summary instead of text.")
    ap.add_argument("--ignore-window", action="store_true",
                    help="run even inside the maintenance window (testing).")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        warn("fatal: " + str(exc))
        _push_kuma("down", "fatal: " + str(exc)[:150])
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
