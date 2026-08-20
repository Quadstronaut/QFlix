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

whose Plex addedAt is STRICTLY older than --threshold-days (default
DEFAULT_THRESHOLD_DAYS, currently 45 — see the note on that constant), that are
not excluded, and that POSITIVELY resolve to exactly one *arr id. Resolution is
mandatory: an item that does not map to a single Radarr movie / Sonarr series is
NEVER deleted — it is skipped and logged UNRESOLVED. Such an item is an "orphan"
(no backing *arr record, or missing external guids). The *arr delete is the
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
# Seerr /api/v1/media page size for reconciliation. The list is paged; a single
# take=N would silently skip rows past N, leaving deleted titles stuck
# "Available" (not re-requestable). reconcile_seerr() loops until exhausted.
_SEERR_MEDIA_PAGE = 100

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
# NO unique *arr id is an "orphan" (no backing *arr record, or missing guids).
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
    return False


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

    # Page through ALL available media. A single take=N would silently skip
    # rows past the cap, leaving deleted titles stuck "Available" (members
    # couldn't re-request them). Loop skip+=PAGE until a short page arrives or
    # pageInfo.results is exhausted; a hard ceiling guards a misbehaving API.
    results = []
    skip = 0
    while True:
        status, body = _seerr_req(
            "GET", port, key, "/api/v1/media",
            query="take=" + str(_SEERR_MEDIA_PAGE) + "&skip=" + str(skip) +
            "&filter=available",
        )
        if status != 200 or not isinstance(body, dict):
            if skip == 0:
                warn("Seerr media list unreachable/empty (HTTP " + str(status) +
                     ") — skipping")
                return deleted, failed
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
        log("Seerr: no available media rows to reconcile")
        return deleted, failed
    log("Seerr: reconciling " + str(len(results)) + " available media row(s)")

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
                    help=("addedAt age cutoff in days; item is a candidate iff age > N "
                          "(strict). Default %(default)s."))
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
    partial = False           # OPERATIONAL failure only (delete/plex/seerr/arr);
                              # orphans are tracked separately (grace window).
    orphans_seen = []         # [{key,title,library}] aged items that resolve to
                              # NO unique *arr id — graced, not an instant red.
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
                # Not an instant partial: an orphan is graced (see reconcile_orphans).
                # A FRESH orphan still reds the run; a KNOWN one goes green.
                orphans_seen.append({"key": _orphan_key(it),
                                     "title": it.get("title"), "library": title})
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

    # ---- Summary + notify (grace-aware) ----
    # `partial` is now OPERATIONAL-only (delete/plex/seerr/arr). Orphans are graded
    # by classify_run against the grace clock: a fresh orphan reds like today, a
    # known one goes green with a throttled weekly WARN reminder.
    summary = (str(deleted) + " deleted, " + str(total_gb) + " GB reclaimed across " +
               str(len(libraries_touched)) + " libraries")
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
