#!/usr/bin/env python3
"""qflix-anime-janitor - daily corrector for anime-library misclassification.

WHY: Seerr routes anime to the dedicated anime *arr instances (Sonarr2 ->
~/media/Anime, Radarr2 -> ~/media/Anime Movies). Its keyword detection
misfires, so regular shows/movies land in the anime instances and show up in
the Plex Anime / Anime Movies libraries. This is a box-side, once-a-day janitor
- structurally a sibling of qflix-reaper - that re-homes confirmed non-anime
titles OUT of the anime libraries and FLAGS the reverse (real anime sitting in
the main libraries) for manual review.

The reaper deletes; this janitor MOVES (a reversible, non-destructive op) and
inherits the reaper's whole safety envelope.

CLASSIFIER (genre + origin, 4-quadrant; source of truth is the *arr record's
own `genres` + `originalLanguage`):

    has "Animation" genre?   origin in ANIME_LANGS (Japanese)?   verdict
    -----------------------  ---------------------------------   ---------------
    yes                      yes                                 anime -> leave
    yes                      no                                  FLAG (western cartoon)
    no                       no                                  AUTO re-home OUT
    no                       yes                                 FLAG (JP live-action / mislabel)
    (no genres at all)       -                                   SKIP + FLAG (missing metadata)

AUTO re-home fires ONLY on the narrowest, highest-precision quadrant: a title
with NO Animation genre AND non-Japanese origin - unambiguous foreign/Western
live-action that has no business in an anime library. Everything softer is
flagged, never moved. Missing metadata is never grounds to move.

REVERSE (flag-only): a title in a MAIN library (Sonarr/Radarr) that HAS the
Animation genre AND Japanese origin is likely a misplaced anime -> reported,
never auto-moved.

DRY-RUN IS THE DEFAULT. With no flags the janitor enumerates, classifies,
prints the plan + totals, and MUTATES NOTHING. The systemd unit ships in this
safe mode. Arm it with --execute (the ONLY flag that moves anything) once the
dry-run plan is trusted - same ritual as the reaper.

RE-HOME SEQUENCE (per auto-move title; tracked in a durable inflight ledger so a
crash mid-move is resumable):
  1. same-device guard: refuse (flag crossdev) if source/target roots differ in
     st_dev - a cross-device move would double disk usage / risk seeding.
  2. add the title to the TARGET instance (import-existing; no new search).
  3. rename() the folder from the anime root to the main root (same device).
  4. rescan the target so it imports the moved files.
  5. remove the title from the SOURCE instance WITHOUT deleting files.
  6. refresh both Plex libraries.

CAPS:
  --max-moves N   per-run rate limit on auto re-homes (default 10). Overflow is
                  DEFERRED to the next run (forward progress), not aborted.
  --max-pct  P    per-library tripwire (default 25). If auto-move candidates
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
  1  partial (a per-item step failed; re-run resumes from the ledger)
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
        "to_root": "/home/quadstronaut/media/TV",
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

# Main instances scanned for the reverse (flag-only) direction.
MAIN_LIBS = [
    {"kind": "series", "slug": "sonarr",  "idkey": "tvdbId", "plex": "QFlix - TV"},
    {"kind": "movie",  "slug": "radarr",  "idkey": "tmdbId", "plex": "QFlix - Movies"},
]

ANIMATION_GENRE = "Animation"
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
    except Exception:
        pass


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
    except Exception:
        pass
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
    has_anim = ANIMATION_GENRE in genres
    origin = ((record.get("originalLanguage") or {}) or {}).get("name") or ""
    is_anime_origin = origin in anime_langs
    if has_anim and is_anime_origin:
        return ("leave", "anime")
    if has_anim and not is_anime_origin:
        return ("flag", "animation-non-jp")
    if (not has_anim) and (not is_anime_origin):
        return ("auto_out", "live-action-non-jp")
    # not has_anim and is_anime_origin -> ambiguous (JP live-action or mislabel)
    return ("flag", "jp-live-action-or-mislabel")


def classify_main_lib(record, anime_langs) -> tuple:
    """Verdict for a title in a MAIN library (reverse direction). Returns
    (action, reason); action in {flag_reverse, ignore}."""
    genres = record.get("genres") or []
    has_anim = ANIMATION_GENRE in genres
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
def _resolve_root(client):
    """First root folder path from an arr's /rootfolder, or None."""
    code, roots = client.get("/rootfolder")
    if code == 200 and isinstance(roots, list) and roots:
        return roots[0].get("path")
    return None


def _default_quality_profile(client):
    code, profs = client.get("/qualityprofile")
    if code == 200 and isinstance(profs, list) and profs:
        return profs[0].get("id")
    return None


def rehome(pair, record, *, section_keys, plex_port, plex_token):
    """Re-home one title from its anime instance to the main instance (EXECUTE
    path only - callers must guard on --execute). Returns (ok, note). Never
    raises - failures return (False, reason)."""
    kind = pair["kind"]
    idkey = pair["idkey"]
    idval = record.get(idkey)
    title = record.get("title") or "?"
    src = _arr_client(pair["from_slug"])
    dst = _arr_client(pair["to_slug"])

    from_path = record.get("path")
    if not from_path:
        return (False, "no source path on record")
    to_root = _resolve_root(dst) or pair["to_root"]
    to_path = os.path.join(to_root, os.path.basename(from_path))

    # 1. same-device guard (skip -> flag). Compare the two ROOT dirs' st_dev.
    from_root = pair["from_root"]
    try:
        if os.stat(from_root).st_dev != os.stat(to_root).st_dev:
            return (False, "crossdev: " + from_root + " vs " + to_root)
    except OSError as exc:
        return (False, "stat failed: " + str(exc))

    if os.path.exists(to_path):
        return (False, "target path already exists: " + to_path)

    plan = ("rehome " + kind + " '" + title + "' " + idkey + "=" + str(idval)
            + " : " + pair["from_slug"] + " -> " + pair["to_slug"]
            + " | " + from_path + " -> " + to_path)

    _append_json_list(_ledger_path(), {
        "ts": _utc_now(), "step": "planned", "kind": kind, idkey: idval,
        "title": title, "from": pair["from_slug"], "to": pair["to_slug"],
        "from_path": from_path, "to_path": to_path,
    })
    log("EXECUTE " + plan)

    # 2. add to target (import existing; no search)
    new_id = _add_to_target(dst, pair, idval, record, to_root)
    if new_id is None:
        return (False, "add-to-target failed")

    # 3. move files (same-device rename)
    try:
        os.rename(from_path, to_path)
    except OSError as exc:
        return (False, "rename failed: " + str(exc))

    # 4. rescan target so it imports the moved files
    _rescan_target(dst, kind, new_id)

    # 5. remove from source WITHOUT deleting files
    if kind == "series":
        src.delete("/series/" + str(record.get("id")),
                   query="deleteFiles=false&addImportListExclusion=false")
    else:
        src.delete("/movie/" + str(record.get("id")),
                   query="deleteFiles=false&addImportExclusion=false")

    # 6. refresh both Plex libraries
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
    """Add the title to the target arr for import-existing. Idempotent: if it
    already exists (crash resume), returns the existing id."""
    kind = pair["kind"]
    qp = _default_quality_profile(dst)
    if qp is None:
        warn("no quality profile on target " + pair["to_slug"])
        return None
    if kind == "series":
        # already present?
        code, existing = dst.get("/series", query="tvdbId=" + str(idval))
        if code == 200 and isinstance(existing, list) and existing:
            return existing[0].get("id")
        code, look = dst.get("/series/lookup", query="term=tvdb:" + str(idval))
        if code != 200 or not isinstance(look, list) or not look:
            return None
        payload = look[0]
        payload["qualityProfileId"] = qp
        payload["rootFolderPath"] = to_root
        payload["monitored"] = bool(record.get("monitored", True))
        payload["seriesType"] = pair.get("series_type", "standard")
        payload["addOptions"] = {"searchForMissingEpisodes": False,
                                 "searchForCutoffUnmetEpisodes": False}
        code, resp = dst.post("/series", body=payload)
        if code in (200, 201) and isinstance(resp, dict):
            return resp.get("id")
        return None
    else:
        code, existing = dst.get("/movie", query="tmdbId=" + str(idval))
        if code == 200 and isinstance(existing, list) and existing:
            return existing[0].get("id")
        code, look = dst.get("/movie/lookup/tmdb", query="tmdbId=" + str(idval))
        if code != 200 or not isinstance(look, dict):
            return None
        payload = look
        payload["qualityProfileId"] = qp
        payload["rootFolderPath"] = to_root
        payload["monitored"] = bool(record.get("monitored", True))
        payload["minimumAvailability"] = record.get("minimumAvailability", "released")
        payload["addOptions"] = {"searchForMovie": False}
        code, resp = dst.post("/movie", body=payload)
        if code in (200, 201) and isinstance(resp, dict):
            return resp.get("id")
        return None


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
    plex_port, plex_token = _plex_creds()
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
                auto_candidates.append((pair, rec))
            elif action in ("flag", "skip"):
                flags.append({"lib": pair["from_slug"], "title": rec.get("title"),
                              pair["idkey"]: rec.get(pair["idkey"]), "reason": reason})

    # --- main libraries: reverse flag-only ---
    for lib in MAIN_LIBS:
        if only and lib["slug"] not in only:
            continue
        titles = _list_titles(lib["slug"], lib["kind"])
        if titles is None:
            warn("could not enumerate " + lib["slug"] + " (reverse) - skipping")
            continue
        for rec in titles:
            if is_excluded(rec, lib["idkey"], tokens):
                continue
            action, reason = classify_main_lib(rec, anime_langs)
            if action == "flag_reverse":
                flags.append({"lib": lib["slug"], "title": rec.get("title"),
                              lib["idkey"]: rec.get(lib["idkey"]), "reason": reason})

    if fatal and not auto_candidates:
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
    log("auto-move candidates (non-anime in anime libs): " + str(len(auto_candidates)))
    for p, r in auto_candidates:
        log("  MOVE OUT " + p["from_slug"] + ": '" + str(r.get("title")) + "'")
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
