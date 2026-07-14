#!/usr/bin/env python3
"""qflix-reaper — 60-day autodelete for the QFlix Plex libraries.

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

whose Plex addedAt is STRICTLY older than --threshold-days (default 60), that are
not excluded, and that POSITIVELY resolve to exactly one *arr id. Resolution is
mandatory: an item that does not map to a single Radarr movie / Sonarr series is
NEVER deleted — it is skipped, logged UNRESOLVED, and colors the run a partial
failure (exit 1). The *arr delete is the authority; Plex is then refreshed and
its trash emptied; finally Seerr is reconciled so deleted media becomes
re-requestable.

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
                  guard (never delete > N in one run) still holds.
  --max-pct  P    per-library TRIPWIRE: if candidates in any one library exceed P%
                  of that library's total item count, abort the WHOLE run before
                  any mutation (default 30). Prod disables it with --max-pct 100.
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
DEFAULT_THRESHOLD_DAYS = 60
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_PCT = 30
DAY_SECONDS = 86400

# Kuma push (bazarr2-sync model, reused verbatim in shape).
KUMA_BASE = os.environ.get("KUMA_BASE", "http://127.0.0.1:42005")
KUMA_PUSH_KEY = "qflix-reaper"           # key under ~/secrets/kuma-push-tokens.json

# Exit codes — distinct so the operator can tell a cap trip from a partial fail.
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CAP = 2
EXIT_FATAL = 3


# ===========================================================================
# Logging — print to stdout/stderr; systemd routes both to journal.
# ===========================================================================
def log(msg: str) -> None:
    print("[qflix-reaper] " + msg, flush=True)


def warn(msg: str) -> None:
    print("[qflix-reaper] WARNING: " + msg, file=sys.stderr, flush=True)


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
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


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
        size_bytes = 0
        for media in meta.get("Media", []) or []:
            for part in media.get("Part", []) or []:
                try:
                    size_bytes += int(part.get("size") or 0)
                except (TypeError, ValueError):
                    pass
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


def do_delete_movie(client, movie_id) -> bool:
    """DELETE a Radarr movie WITH files; addImportExclusion=false (stays
    re-requestable). Returns True iff 2xx. Non-2xx is a per-item failure."""
    status, _ = client.delete(
        "/movie/" + str(movie_id),
        query="deleteFiles=true&addImportExclusion=false",
    )
    return 200 <= status < 300


def do_delete_series(client, series_id) -> bool:
    """DELETE a Sonarr series WITH files; addImportListExclusion=false. Returns
    True iff 2xx."""
    status, _ = client.delete(
        "/series/" + str(series_id),
        query="deleteFiles=true&addImportListExclusion=false",
    )
    return 200 <= status < 300


# ===========================================================================
# Plex post-delete housekeeping (non-fatal warnings).
# ===========================================================================
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


def reconcile_seerr(execute: bool):
    """After all libraries are reaped, delete Seerr media rows whose backing arr
    item is gone, so the title becomes re-requestable. Returns (deleted, failed).

    A movie row (tmdbId) is reconciled away iff no Radarr (or radarr2) movie with
    that tmdbId has hasFile==true. A TV row (tvdbId) iff no Sonarr (or sonarr2)
    series has that tvdbId. Per-item, non-fatal, logged, exit-code-reflected.
    Tolerates an empty / unreachable Seerr without aborting."""
    deleted = 0
    failed = 0
    try:
        port, key = _seerr_creds()
    except FileNotFoundError:
        warn("seerr.port/seerr.key missing — skipping Seerr reconciliation")
        return deleted, failed
    if not port or not key:
        warn("seerr creds empty — skipping Seerr reconciliation")
        return deleted, failed

    status, body = _seerr_req("GET", port, key, "/api/v1/media",
                              query="take=400&filter=available")
    if status != 200 or not isinstance(body, dict):
        warn("Seerr media list unreachable/empty (HTTP " + str(status) + ") — skipping")
        return deleted, failed
    results = body.get("results") or []
    if not results:
        log("Seerr: no available media rows to reconcile")
        return deleted, failed

    # Build the live arr index once: movie tmdbIds with files, and series tvdbIds.
    radarr_with_file = set()
    sonarr_tvdbids = set()
    for entry in LIBRARIES:
        try:
            client = _arr_client(entry["slug"])
        except Exception:
            continue
        if entry["kind"] == "movie":
            st, mv = client.get("/movie")
            if st == 200 and isinstance(mv, list):
                for m in mv:
                    if m.get("hasFile") and m.get("tmdbId") is not None:
                        radarr_with_file.add(m.get("tmdbId"))
        else:
            st, sr = client.get("/series")
            if st == 200 and isinstance(sr, list):
                for s in sr:
                    if s.get("tvdbId") is not None:
                        sonarr_tvdbids.add(s.get("tvdbId"))

    for row in results:
        # Coerce the Seerr id to int before it can reach a URL path — a non-integer
        # id is invalid and skipped (defends against a reflected path-traversal id).
        try:
            media_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        media_type = row.get("mediaType")
        gone = False
        if media_type == "movie":
            tmdb = row.get("tmdbId")
            gone = tmdb is not None and tmdb not in radarr_with_file
        elif media_type == "tv":
            tvdb = row.get("tvdbId")
            gone = tvdb is not None and tvdb not in sonarr_tvdbids
        if not gone:
            continue
        log("Seerr: media " + str(media_id) + " (" + str(media_type) +
            ") backing arr item gone -> " + ("DELETE" if execute else "would delete"))
        if not execute:
            continue
        st, _ = _seerr_req("DELETE", port, key, "/api/v1/media/" + str(media_id))
        if 200 <= st < 300:
            deleted += 1
        else:
            failed += 1
            warn("Seerr delete media " + str(media_id) + " failed: HTTP " + str(st))
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
def write_manifest(manifest_dir: Path, args, per_lib_candidates):
    """Write the pre-execution audit record and return its Path. Called ONLY on
    --execute, BEFORE the first DELETE. Lists every intended deletion."""
    ts = datetime.now(timezone.utc)
    # PID suffix so two runs in the same second can't overwrite each other's
    # pre-deletion audit record.
    fname = "qflix-reaper-" + ts.strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid()) + ".json"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / fname

    flat = []
    total_gb = 0.0
    for title, cands in per_lib_candidates.items():
        for c in cands:
            total_gb += c.get("sizeGB", 0) or 0
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
    ap.add_argument("--threshold-days", type=int, default=DEFAULT_THRESHOLD_DAYS,
                    help="addedAt age cutoff in days; item is a candidate iff age > N (strict). Default 60.")
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
    return ap.parse_args(argv)


# ===========================================================================
# Main orchestration
# ===========================================================================
def run(args) -> int:
    execute = args.execute
    mode = "EXECUTE" if execute else "DRY-RUN"
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

    per_lib_candidates = {}   # plex_title -> [candidate dicts]
    per_lib_totals = {}       # plex_title -> total item count
    per_lib_section = {}      # plex_title -> section key
    partial = False           # any per-item resolve/delete/plex/seerr failure
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
        per_lib_totals[title] = len(items)

        # Build an arr client once per library for resolution.
        try:
            client = _arr_client(entry["slug"])
        except Exception as exc:
            warn("could not build arr client for '" + entry["slug"] + "': " + str(exc))
            client = None

        cands = []
        for it in items:
            # addedAt<=0 = Plex gave no/unparseable add-date; treat as UNKNOWN
            # age and NEVER a candidate (a metadata gap must not look ancient).
            if it["addedAt"] <= 0:
                continue
            age = now - it["addedAt"]
            if not (age > threshold_secs):     # strictly greater-than
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

            # MANDATORY positive resolve before any delete.
            arr_id = None
            if client is not None:
                if entry["kind"] == "movie":
                    arr_id = resolve_radarr_id(client, it["tmdbId"])
                else:
                    arr_id = resolve_sonarr_id(client, it["tvdbId"])
            if arr_id is None:
                warn("UNRESOLVED " + repr(it.get("title")) + " in '" + title +
                     "' (no unique *arr match) — SKIP, will not delete")
                partial = True
                continue
            it["arrId"] = arr_id
            cands.append(it)

        per_lib_candidates[title] = cands
        log("library '" + title + "': " + str(len(items)) + " items, " +
            str(len(cands)) + " resolved candidate(s)")

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
        oldest_first = sorted(all_cands, key=lambda c: c.get("addedAt", 0))
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

    # ---- Plan printout (always) ----
    log("PLAN: " + str(total_count) + " candidate(s), " + str(total_gb) + " GB reclaimable")
    for c in all_cands:
        log("  - " + repr(c.get("title")) + " (" + str(c.get("year")) + ") [" +
            str(c.get("kind")) + "] " + str(c.get("sizeGB")) + " GB  <" +
            str(c.get("library")) + ">")

    if args.emit_json:
        plan = {
            "mode": mode,
            "threshold_days": args.threshold_days,
            "total_count": total_count,
            "total_reclaim_gb": total_gb,
            "candidates": [{
                "title": c.get("title"), "year": c.get("year"), "type": c.get("kind"),
                "library": c.get("library"), "sizeGB": c.get("sizeGB"),
                "ratingKey": c.get("ratingKey"), "tmdbId": c.get("tmdbId"),
                "tvdbId": c.get("tvdbId"), "arrId": c.get("arrId"),
            } for c in all_cands],
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

    # ---- DRY-RUN: stop here. No manifest, no mutation. ----
    if not execute:
        log("DRY-RUN complete — no mutations performed, no manifest written.")
        # Dry-run is not an incident: never page; minimal optional heartbeat only.
        _push_kuma("up", "dry-run: " + str(total_count) + " candidate(s), " +
                   str(total_gb) + " GB")
        # A dry-run that hit per-item resolve failures still reports partial so the
        # operator sees UNRESOLVED items need attention before arming --execute.
        return EXIT_PARTIAL if partial else EXIT_OK

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
    manifest_path = write_manifest(Path(args.manifest_dir), args, per_lib_candidates)
    log("manifest written: " + str(manifest_path))

    deleted = 0
    libraries_touched = set()
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
                ok_del = do_delete_movie(client, c["arrId"])
            else:
                ok_del = do_delete_series(client, c["arrId"])
            if ok_del:
                deleted += 1
                lib_deleted += 1
                log("DELETED " + repr(c.get("title")) + " (arrId=" + str(c["arrId"]) + ")")
            else:
                partial = True
                warn("DELETE FAILED " + repr(c.get("title")) + " (arrId=" + str(c["arrId"]) + ")")

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

    # ---- Seerr reconciliation (after all libraries) ----
    s_deleted, s_failed = reconcile_seerr(execute=True)
    if s_failed > 0:
        partial = True
    log("Seerr reconciliation: " + str(s_deleted) + " deleted, " + str(s_failed) + " failed")

    # ---- Summary + notify ----
    summary = (str(deleted) + " deleted, " + str(total_gb) + " GB reclaimed across " +
               str(len(libraries_touched)) + " libraries")
    if partial:
        msg = "completed WITH partial failures — " + summary + " (see journal for UNRESOLVED/failed items)"
        warn(msg)
        _notify(msg, level="error")
        _push_kuma("down", msg)
        rc = EXIT_PARTIAL
    else:
        log("SUCCESS — " + summary)
        _notify(summary, level="info")
        _push_kuma("up", summary)
        rc = EXIT_OK

    if args.emit_json:
        print(json.dumps({
            "mode": mode, "deleted": deleted, "total_reclaim_gb": total_gb,
            "libraries_touched": sorted(libraries_touched),
            "seerr_deleted": s_deleted, "seerr_failed": s_failed,
            "partial": partial, "exit": rc, "manifest": str(manifest_path),
        }, indent=2), flush=True)
    _release_run_lock(lock)
    return rc


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        # Last-resort guard: an unexpected fatal must still page + color the exit,
        # not crash with an opaque traceback into journald.
        msg = "FATAL unexpected error: " + repr(exc)
        warn(msg)
        _notify(msg, level="error")
        _push_kuma("down", msg)
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
