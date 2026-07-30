#!/usr/bin/env python3
"""qflix-torrent-janitor — purge completed, *arr-UNTRACKED seeding leftovers.

WHY: On a box where EVERYTHING is purged after 60 days (qflix-reaper), a
completed torrent that the *arr already imported and dropped from its queue can
sit in qBittorrent seeding FOREVER — nobody deletes it. The 2026-07-27 audit
found ~40 GB of these (a 34 GB BDRemux + a 6 GB anime movie, both `arr=null`,
`stalledUP`, 0 peers, ratio already met). "Nothing should be forever" — so this
janitor reaps them, structurally a sibling of the reaper for the download client
instead of the Plex library.

WHAT IT REAPS (ALL must hold — the criteria are deliberately narrow):
  1. progress == 1.0 (complete — never touches an in-flight download)
  2. state is a DONE/seeding state (stalledUP/uploading/forcedUP/queuedUP/
     pausedUP/stoppedUP/checkingUP) — never downloading/checking/moving/error
  3. category names an *arr (contains "sonarr" or "radarr") — this PROTECTS any
     manually-added personal torrent, which has no *arr category
  4. the hash is NOT tracked by ANY *arr queue (i.e. already imported/orphaned)
  5. seeding duty is done: ratio >= --min-ratio (default 2.0) OR added >
     --max-seed-days ago (default 30, the "nothing forever" backstop for a
     0-seed torrent that can never reach ratio)

MIN-RATIO IS COUPLED TO qBITTORRENT'S OWN max_ratio. THEY MUST AGREE.
qBit is configured max_ratio=2.0 with max_ratio_act=pause: seed to 2.0, then
stop. This default was 1.0, i.e. HALF that, so the janitor deleted every
torrent at 1.0 and qBit's 2.0 target was unreachable dead config -- nothing
could ever survive to it. Measured 2026-07-30: both remaining torrents were
deleted on 07-28 at ratios 1.85 and 1.18, and every run since logged
"qBittorrent: 0 torrent(s)". The pool had been permanently drained for 6
days while the qBit setting said it should still be seeding.

Raised to 2.0 on operator instruction. The intended flow is now coherent:
seed to 2.0 -> qBit PAUSES the torrent (pausedUP/stoppedUP, both in the
DONE-state list above) -> the janitor reaps it. qBit owns the seeding
decision and the janitor only collects what qBit has finished with.

If you change qBit's max_ratio, change this too. They are two policy
surfaces describing one intent, and the lower one silently wins.

Note the 30-day backstop now does more work: raising the bar to 2.0 means
more torrents never reach it, so poorly-seeded content is reaped on age
rather than ratio. That is the intended "nothing seeds forever" rail.

DELETE is hardlink-SAFE: the *arr imports with hardlinks (guarded by the
hardlink-integrity canary), so removing the torrent's copy only drops a link —
the library file survives. deleteFiles=true reclaims the seed-only copy.

CRITICAL SAFETY RAIL — the untracked determination depends on reading EVERY
*arr's queue. If ANY *arr queue fetch fails, the tracked-hash set is incomplete
and every torrent would falsely look "untracked" → mass delete. So a single
queue-fetch failure ABORTS the whole run before any delete (exit 3). Same shape
as qflix-collect's ghost-prune "skip on errored section".

SAFETY ENVELOPE (reaper parity):
  - DRY-RUN IS THE DEFAULT. The unit ships safe; arm with --execute (via an
    on-box drop-in, like the reaper) only after reviewing a dry-run plan.
  - --max-items N   per-run delete cap (default 20). Overflow DEFERRED to the
                    next run (forward progress), not aborted.
  - --max-pct P     abort tripwire: if reap candidates exceed P% (default 90) of
                    the *arr-categorized COMPLETE torrents, abort before any
                    delete (exit 2) unless --force — a mass-delete circuit
                    breaker on top of the all-queues-failed abort.
  - audit manifest written BEFORE any delete (re-buildable record of intent).
  - run-lock (flock) so timer + manual runs can't double-delete.
  - window-aware: skips (clean, Kuma up "window") during the Monday maintenance
    window — deleting downloads is a box op.

EXCLUSIONS: --exclude-file (default qflix-torrent-janitor.exclude beside this
script). One qBit hash per line (case-insensitive); '#' comments + blanks
ignored. Protects a torrent you want to keep seeding indefinitely.

EXIT CODES (reaper parity):
  0  clean (dry-run plan, or execute with zero failures)
  1  partial (a delete failed; re-run retries)
  2  cap trip (max-pct exceeded without --force) — aborted, no delete
  3  fatal (qBit unreachable, or ANY *arr queue unreadable — cannot determine
     untracked status safely)

Python 3.9 on the box: stdlib only (urllib/json/argparse/os/sys/time/datetime/
pathlib); no match-statement, no backslashes inside f-strings. `requests` may
appear only transitively via the guarded lib.notify import.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path nudge: lib.secrets/lib.notify (scripts/maint/lib) + lib.qbit_client/
# lib.arr_client (scripts/mcp/lib) resolve as a merged `lib` namespace package,
# exactly as the reaper + anime-janitor do it.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent                       # scripts/maint
_REPO_ROOT = _HERE.parent.parent                              # repo root
_MCP_DIR = _REPO_ROOT / "scripts" / "mcp"                     # owns lib/qbit_client.py
for _p in (str(_HERE), str(_MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.secrets import secrets_dir, read_secret  # noqa: E402
from lib.qbit_client import QbitClient            # noqa: E402
from lib.arr_client import ArrClient              # noqa: E402

# *arr instances whose queues define "tracked". v3 API for all four.
ARR_SLUGS = ("sonarr", "sonarr2", "radarr", "radarr2")
ARR_VERSION = "v3"

# qBit states that mean "download finished, now seeding/idle". Only these are
# reap-eligible; a torrent mid-download/-check/-move or errored is never touched.
SEEDING_DONE_STATES = frozenset({
    "uploading", "stalledUP", "forcedUP", "queuedUP",
    "pausedUP", "stoppedUP", "checkingUP",
})

# MUST match qBittorrent's max_ratio (currently 2.0). See the module docstring:
# at 1.0 this deleted every torrent at half the ratio qBit was told to seed to,
# making qBit's own setting unreachable and draining the pool to zero.
DEFAULT_MIN_RATIO = 2.0
DEFAULT_MAX_SEED_DAYS = 30
DEFAULT_MAX_ITEMS = 20
DEFAULT_MAX_PCT = 90.0
DAY_SECONDS = 86400

KUMA_BASE = os.environ.get("KUMA_BASE", "http://127.0.0.1:42005")
KUMA_PUSH_KEY = "qflix-torrent-janitor"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CAP = 2
EXIT_FATAL = 3

_LOG_FH = None
_LOG_RETENTION_DAYS = 30


# ===========================================================================
# Logging (reaper parity): stdout/stderr + durable per-day logfile.
# ===========================================================================
def _log_dir() -> Path:
    return Path(os.environ.get(
        "QFLIX_TORRENT_JANITOR_LOG_DIR",
        str(Path.home() / ".opt" / "maint" / "torrent-janitor"),
    ))


def _setup_file_log() -> None:
    global _LOG_FH
    try:
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        _LOG_FH = open(d / ("torrent-janitor-" + day + ".log"), "a", encoding="utf-8")
        cutoff = datetime.now(timezone.utc).timestamp() - _LOG_RETENTION_DAYS * DAY_SECONDS
        for old in d.glob("torrent-janitor-*.log"):
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
    line = "[qflix-torrent-janitor] " + msg
    print(line, flush=True)
    _file_log(line)


def warn(msg: str) -> None:
    line = "[qflix-torrent-janitor] WARNING: " + msg
    print(line, file=sys.stderr, flush=True)
    _file_log(line)


# ===========================================================================
# Kuma push (reaper parity). Best-effort; never raises.
# ===========================================================================
def _read_kuma_token() -> str:
    env = os.environ.get("QFLIX_TORRENT_JANITOR_KUMA_TOKEN")
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
# Notify (guarded; matches reaper).
# ===========================================================================
def _notify(msg: str, level: str = "info") -> None:
    try:
        if str(_HERE) not in sys.path:
            sys.path.insert(0, str(_HERE))
        from lib.notify import notify
        notify(msg, level)
    except Exception as exc:
        warn("notify failed (non-fatal): " + str(exc))


# ===========================================================================
# Maintenance-window guard (Mon 11:00-15:00 UTC, or window lock held).
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
    except Exception:
        pass
    return False


# ===========================================================================
# Exclusions (pure).
# ===========================================================================
def load_exclusions(path: Path) -> set:
    """Return a set of lower-cased qBit hashes to protect. Missing file ->
    empty set (warned by caller)."""
    tokens = set()
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().lower()
        if line:
            tokens.add(line)
    return tokens


# ===========================================================================
# Tracked-hash set — the untracked determination depends on this being COMPLETE.
# ===========================================================================
def build_tracked_hashes(slugs=ARR_SLUGS):
    """Union of every download hash currently in any *arr queue.

    Returns (hashes:set[str-lower], ok:bool). ok is False if ANY *arr queue
    could not be fully read — the caller MUST abort on ok==False, because an
    incomplete set makes tracked torrents look untracked (mass-delete risk)."""
    tracked = set()
    ok = True
    for slug in slugs:
        client = ArrClient(slug, ARR_VERSION, secrets_dir=secrets_dir(), timeout=20)
        page = 1
        seen = 0
        while True:
            query = ("pageSize=200&page=" + str(page))
            code, payload = client.get("/queue", query=query, timeout=20)
            if code != 200 or not isinstance(payload, dict):
                warn(slug + ": queue fetch failed (HTTP " + str(code)
                     + ") — cannot confirm untracked, ABORTING to avoid mass-delete")
                ok = False
                break
            records = payload.get("records") or []
            for r in records:
                dlid = (r.get("downloadId") or "").strip().lower()
                if dlid:
                    tracked.add(dlid)
            seen += len(records)
            total = payload.get("totalRecords", seen)
            if seen >= total or not records:
                break
            page += 1
            if page > 50:
                break
        if not ok:
            break
    return tracked, ok


# ===========================================================================
# Classifier (pure — unit-tested).
# ===========================================================================
def classify_torrent(t, tracked_hashes, now_epoch, min_ratio, max_seed_days):
    """Verdict for one qBit torrent. Returns (action, reason);
    action in {"reap", "keep"}. Pure/deterministic (no I/O)."""
    if (t.get("progress") or 0) < 1.0:
        return ("keep", "incomplete")
    state = t.get("state") or ""
    if state not in SEEDING_DONE_STATES:
        return ("keep", "state:" + state)
    cat = (t.get("category") or "").lower()
    if "sonarr" not in cat and "radarr" not in cat:
        # No *arr category -> not ours to reap (manual/personal torrent).
        return ("keep", "non-arr-category:" + (cat or "<none>"))
    h = (t.get("hash") or "").lower()
    if not h:
        return ("keep", "no-hash")
    if h in tracked_hashes:
        return ("keep", "arr-tracked")
    ratio = t.get("ratio")
    if isinstance(ratio, (int, float)) and ratio >= min_ratio:
        return ("reap", "ratio-met (" + str(round(float(ratio), 2)) + " >= "
                + str(min_ratio) + ")")
    added = t.get("added_on")
    if added:
        try:
            age_s = now_epoch - int(added)
        except (TypeError, ValueError):
            age_s = 0
        if age_s >= max_seed_days * DAY_SECONDS:
            return ("reap", "aged-out (" + str(int(age_s / DAY_SECONDS))
                    + "d >= " + str(max_seed_days) + "d)")
    return ("keep", "seeding-duty-not-done")


def _gb(nbytes) -> float:
    try:
        return round(int(nbytes) / (1024 ** 3), 2)
    except (TypeError, ValueError):
        return 0.0


# ===========================================================================
# Run-lock (flock).
# ===========================================================================
_LOCK_PATH = os.environ.get("QFLIX_TORRENT_JANITOR_LOCK", "/tmp/qflix-torrent-janitor.lock")


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
# Audit manifest (reaper parity): written BEFORE any delete.
# ===========================================================================
def _write_manifest(candidates, *, dry_run, min_ratio, max_seed_days) -> Path:
    ts = datetime.now(timezone.utc)
    d = _log_dir()
    d.mkdir(parents=True, exist_ok=True)
    fname = "torrent-janitor-plan-" + ts.strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid()) + ".json"
    path = d / fname
    payload = {
        "generated_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run": dry_run,
        "min_ratio": min_ratio,
        "max_seed_days": max_seed_days,
        "candidates": [
            {"hash": (c.get("hash") or "").lower(), "name": c.get("name"),
             "category": c.get("category"), "state": c.get("state"),
             "ratio": c.get("ratio"), "size_gb": _gb(c.get("size")),
             "reason": c.get("_reason")}
            for c in candidates
        ],
    }
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))
    return path


# ===========================================================================
# Run.
# ===========================================================================
def run(args) -> int:
    _setup_file_log()
    mode = "DRY-RUN" if not args.execute else "EXECUTE"
    log("--- qflix-torrent-janitor (" + mode + ") min-ratio=" + str(args.min_ratio)
        + " max-seed-days=" + str(args.max_seed_days) + " max-items="
        + str(args.max_items) + " ---")

    if in_maintenance_window() and not args.ignore_window:
        log("in maintenance window - skipping run")
        _push_kuma("up", "skipped (maintenance window)")
        return EXIT_OK

    dry_run = not args.execute
    run_lock = None
    if not dry_run:
        run_lock = _acquire_run_lock()
        if run_lock is None:
            warn("another --execute run holds the lock; skipping")
            _push_kuma("up", "skipped (locked)")
            return EXIT_OK

    # Exclusions.
    excl_path = Path(args.exclude_file) if args.exclude_file else (
        _HERE / "qflix-torrent-janitor.exclude")
    try:
        excluded = load_exclusions(excl_path)
    except FileNotFoundError:
        warn("exclude file missing (" + str(excl_path) + ") - proceeding with none")
        excluded = set()
    log("loaded " + str(len(excluded)) + " exclusion(s) from " + str(excl_path))

    # qBit.
    qb = QbitClient(secrets_dir=secrets_dir())
    if not qb.login():
        warn("qBittorrent login failed — cannot enumerate torrents")
        _push_kuma("down", "fatal: qBittorrent unreachable")
        return EXIT_FATAL
    torrents = qb.list_torrents()
    log("qBittorrent: " + str(len(torrents)) + " torrent(s)")

    # Tracked set — ABORT if any *arr queue is unreadable (mass-delete guard).
    tracked, ok = build_tracked_hashes()
    if not ok:
        _push_kuma("down", "fatal: an *arr queue was unreadable — aborted to avoid mass-delete")
        return EXIT_FATAL
    log("tracked by *arr queues: " + str(len(tracked)) + " hash(es)")

    now_epoch = int(datetime.now(timezone.utc).timestamp())

    candidates = []
    arr_complete = 0     # denominator for the max-pct tripwire
    for t in torrents:
        # Count *arr-categorized COMPLETE torrents (the population the tripwire
        # protects). progress+category are cheap to recompute here.
        cat = (t.get("category") or "").lower()
        is_arr = ("sonarr" in cat or "radarr" in cat)
        if is_arr and (t.get("progress") or 0) >= 1.0:
            arr_complete += 1
        h = (t.get("hash") or "").lower()
        if h and h in excluded:
            continue
        action, reason = classify_torrent(
            t, tracked, now_epoch, args.min_ratio, args.max_seed_days)
        if action == "reap":
            t["_reason"] = reason
            candidates.append(t)

    log("reap candidates: " + str(len(candidates)) + " of " + str(arr_complete)
        + " *arr-complete torrent(s)")
    for c in candidates:
        log("  REAP " + str(c.get("name"))[:70] + " [" + _fmt(c) + "] — "
            + str(c.get("_reason")))

    # max-pct tripwire (mass-delete circuit breaker).
    if not args.force and arr_complete > 0:
        pct = 100.0 * len(candidates) / arr_complete
        if pct > args.max_pct:
            msg = ("cap trip: " + str(len(candidates)) + "/" + str(arr_complete)
                   + " (" + str(round(pct, 1)) + "%) > max-pct " + str(args.max_pct))
            if dry_run:
                warn(msg + " (dry-run: showing plan; would ABORT on --execute)")
            else:
                warn(msg + " - ABORTING before any delete")
                _push_kuma("down", msg)
                return EXIT_CAP

    # Audit manifest BEFORE any delete.
    manifest_path = _write_manifest(candidates, dry_run=dry_run,
                                    min_ratio=args.min_ratio,
                                    max_seed_days=args.max_seed_days)
    log("plan manifest: " + str(manifest_path))

    # Apply max-items (defer excess).
    to_reap = candidates
    deferred = 0
    if len(candidates) > args.max_items:
        deferred = len(candidates) - args.max_items
        to_reap = candidates[:args.max_items]
        warn("max-items " + str(args.max_items) + " reached; deferring "
             + str(deferred) + " to next run")

    reaped = 0
    failures = 0
    freed_gb = 0.0
    if not dry_run:
        for t in to_reap:
            h = (t.get("hash") or "").lower()
            ok_del = qb.delete_torrent(h, delete_files=True)
            if ok_del:
                reaped += 1
                freed_gb += _gb(t.get("size"))
                log("DELETED " + h[:12] + " " + str(t.get("name"))[:70]
                    + " (" + str(_gb(t.get("size"))) + " GB)")
            else:
                failures += 1
                warn("delete failed for " + h[:12] + " " + str(t.get("name"))[:70])

    # Emit + Kuma + exit.
    if args.emit_json:
        print(json.dumps({
            "dry_run": dry_run,
            "candidates": len(candidates),
            "arr_complete": arr_complete,
            "reaped": reaped, "deferred": deferred, "failures": failures,
            "freed_gb": round(freed_gb, 2),
            "manifest": str(manifest_path),
        }, indent=2))

    if failures:
        _push_kuma("down", "partial: " + str(failures) + " delete failure(s), "
                   + str(reaped) + " reaped")
        _notify("Torrent janitor: " + str(failures) + " delete failure(s), "
                + str(reaped) + " reaped (" + str(round(freed_gb, 2)) + " GB)", "error")
        return EXIT_PARTIAL

    if dry_run:
        summary = ("dry-run: " + str(len(candidates)) + " candidate(s) would free ~"
                   + str(round(sum(_gb(c.get('size')) for c in candidates), 2)) + " GB")
        log(summary)
        _push_kuma("up", summary)
    else:
        summary = ("reaped " + str(reaped) + " torrent(s), freed "
                   + str(round(freed_gb, 2)) + " GB, deferred " + str(deferred))
        log(summary)
        _push_kuma("up", summary)
        if reaped:
            _notify("Torrent janitor reaped " + str(reaped) + " seeding leftover(s), freed "
                    + str(round(freed_gb, 2)) + " GB", "info")
    return EXIT_OK


def _fmt(t) -> str:
    return ("ratio=" + str(t.get("ratio")) + " " + str(_gb(t.get("size")))
            + "GB state=" + str(t.get("state")))


def build_parser():
    ap = argparse.ArgumentParser(
        description="Purge completed *arr-untracked qBit seeding leftovers (reaper-parity).")
    ap.add_argument("--execute", action="store_true",
                    help="perform real deletes (the ONLY way to mutate). Default dry-run.")
    ap.add_argument("--min-ratio", type=float, default=DEFAULT_MIN_RATIO,
                    help="reap a completed untracked torrent once ratio >= this. Default 1.0.")
    ap.add_argument("--max-seed-days", type=int, default=DEFAULT_MAX_SEED_DAYS,
                    help="backstop: reap regardless of ratio once older than this. Default 30.")
    ap.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS,
                    help="per-run delete cap (excess deferred). Default 20.")
    ap.add_argument("--max-pct", type=float, default=DEFAULT_MAX_PCT,
                    help="abort tripwire: candidates as %% of *arr-complete torrents. Default 90.")
    ap.add_argument("--force", action="store_true",
                    help="override the max-pct tripwire (logged). Does NOT imply --execute.")
    ap.add_argument("--exclude-file", default=None,
                    help="hashes to protect (default qflix-torrent-janitor.exclude beside this script).")
    ap.add_argument("--json", dest="emit_json", action="store_true",
                    help="emit a structured JSON summary.")
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
