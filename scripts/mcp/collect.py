#!/usr/bin/env python3
"""scripts/mcp/collect.py — read-only farm snapshot.

Modes:
  --emit-json   write one JSON blob to stdout (MCP/PS1 callers)
  --cron        log only, no stdout (systemd-timer caller)

Optional:
  --include logs,plex,arrs,qbit,seerr,sab  (default: all)
  --recent-hours N   (Plex recently-added window; default 24)

Reuses scripts/maint/lib/manifest.py + scripts/maint/lib/notify.py.
Reuses scripts/mcp/lib/{qbit_client,arr_client,sab_client}.
Plex section delegates to scripts/mcp/plex.py via subprocess (uses python-plexapi venv).

SAB (Usenet) parity note (2026-07-19 sab-stuck-parity spec, C2): SAB is a
second, protocol-different download client feeding the SAME stuck-download
pipeline as qBit. The `sab` section below is deliberately shaped to mirror
`qbit` (slots ~ torrents, totals.count, an error shape on failure) so the
stale-state loop in qflix-collect.py can walk both with shared logic.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Self-locate scripts/mcp/lib + scripts/maint/lib on sys.path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                  # for lib.qbit_client
sys.path.insert(0, str(HERE.parent / "maint")) # for lib.notify

from lib.qbit_client import QbitClient  # noqa: E402
from lib.arr_client import ArrClient    # noqa: E402
from lib.sab_client import SabClient, MIB  # noqa: E402

ARRS = [
    ("sonarr", "v3"),
    ("sonarr2", "v3"),
    ("radarr", "v3"),
    ("radarr2", "v3"),
]

DEAD_SLOW_BYTES_S = 10_000  # <10 kB/s averaged → dead-slow
SUSPICIOUS_MOVIE_BYTES = 100 * 1024 * 1024  # 100 MB
SUSPICIOUS_EPISODE_BYTES = 50 * 1024 * 1024  # 50 MB


def normalize_qbit_torrent(t: dict) -> dict:
    """qBit field names → spec field names."""
    return {
        "hash": t["hash"],
        "name": t.get("name", ""),
        "added_on": t.get("added_on", 0),
        "size_bytes": t.get("size", 0),
        "downloaded_bytes": t.get("downloaded", 0),
        "progress": t.get("progress", 0.0),
        "dl_speed_bytes_s": t.get("dlspeed", 0),
        "up_speed_bytes_s": t.get("upspeed", 0),
        "state": t.get("state", ""),
        "category": t.get("category", ""),
        "tags": [x.strip() for x in (t.get("tags") or "").split(",") if x.strip()],
        "ratio": t.get("ratio", 0.0),
        "eta_seconds": t.get("eta", 0) or None,
        "seeds": t.get("num_seeds", 0),
        "leeches": t.get("num_leechs", 0),
        "last_activity": t.get("last_activity", 0),
    }


def matches_stale_rule(t: dict) -> Optional[str]:
    """Returns rule name if eligible (workstation enforces 3-hour rule on top)."""
    if t.get("progress", 0.0) >= 1.0:
        return None  # completed → not eligible (uploading is fine)
    state = t.get("state", "")
    if state == "stalledDL":
        return "stalledDL"
    if state == "downloading" and t.get("dl_speed_bytes_s", 0) < DEAD_SLOW_BYTES_S:
        return "dead-slow"
    # qBit 5.x renamed pausedDL/pausedUP → stoppedDL/stoppedUP. A *DL torrent
    # that is stopped/paused while still INCOMPLETE is a dead or ratio-auto-
    # stopped download (the 2026-05 "Unforgettable" incident: stoppedDL at
    # 35%, 0 seeds, ratio-limit auto-paused). The progress >= 1.0 guard above
    # already excludes completed torrents (stoppedUP/pausedUP), so matching
    # only the *DL variants here never touches finished content/hardlinks.
    if state in ("stoppedDL", "pausedDL"):
        return "stopped-incomplete"
    return None


# --- SAB (Usenet) normalize + classify --------------------------------------
# Second download client, same stuck-download pipeline (2026-07-19 spec, C2).
# SAB's "mb"/"mbleft" queue-slot fields are MiB despite the name (see
# lib/sab_client.MIB) and arrive as STRINGS ("4801.69") — _sab_float coerces.

def _sab_float(v, default: float = 0.0) -> float:
    """SAB emits numeric fields as strings. A malformed/missing field must
    never crash the hourly collector — fall back to `default` instead."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize_sab_slot(s: dict, kbpersec: float = 0.0) -> dict:
    """SAB queue-slot field names -> spec field names (mirrors
    normalize_qbit_torrent). `kbpersec` is the QUEUE-level speed (from
    queue_meta()); it's only attributed to this slot's dl_speed_bytes_s when
    the slot itself is the one actively Downloading (SAB downloads one nzb
    at a time — every other slot sits at 0 regardless of queue speed)."""
    mb = _sab_float(s.get("mb"))
    mbleft = _sab_float(s.get("mbleft"))
    downloaded_mb = max(mb - mbleft, 0.0)
    state = s.get("status", "")
    progress = (1.0 - (mbleft / mb)) if mb > 0 else 0.0
    return {
        "id": s.get("nzo_id", ""),
        "name": s.get("filename", ""),
        "cat": s.get("cat", ""),
        "state": state,
        "size_bytes": int(round(mb * MIB)),
        "downloaded_bytes": int(round(downloaded_mb * MIB)),
        "progress": round(progress, 4),
        "dl_speed_bytes_s": int(round(kbpersec * 1024)) if state == "Downloading" else 0,
    }


# SAB Status strings eligible for each stale rule (C2 table). "Paused" is
# handled separately in matches_stale_sab_rule — it's only stuck when the
# QUEUE isn't paused (the object.py force-pause wedge, not an operator pause).
_SAB_ZERO_MOVEMENT_STATES = {"Downloading", "Queued", "Grabbing", "Fetching", "Propagating"}
_SAB_PP_HUNG_STATES = {"Verifying", "Repairing", "Extracting", "Moving", "Running",
                        "QuickCheck", "Checking"}


def matches_stale_sab_rule(slot_state: str, queue_paused: bool,
                           has_started: Optional[bool] = None) -> Optional[str]:
    """SAB analogue of matches_stale_rule (qBit). STATE eligibility only —
    the stale loop (qflix-collect.py, C3) still requires 3 zero-delta
    samples before treating a match as a real candidate; byte-delta is out
    of scope here.

    A slot Paused while the queue is RUNNING is the object.py force-pause
    wedge (rule sab-paused-pinned): Sonarr's FailedDownloadService never
    fires on Paused, so nothing else will ever unstick it. A slot Paused
    because the OPERATOR paused the whole queue is not stuck — that's
    intentional and excluded.
    """
    if slot_state == "Paused":
        return None if queue_paused else "sab-paused-pinned"
    if slot_state in _SAB_ZERO_MOVEMENT_STATES:
        # TWO EXEMPTIONS, both added 2026-08-07 after this rule deleted AND
        # BLOCKLISTED 10 legitimate releases in one run.
        #
        # 1. A PAUSED QUEUE. The Paused branch above already refuses to flag an
        #    operator-paused queue -- but that branch is unreachable for this
        #    case, because a paused QUEUE leaves its slots reporting
        #    "Downloading", not "Paused" (measured live: queue paused=True with
        #    slot statuses {"Downloading": 148}). The guard was on the one
        #    branch that cannot trigger it.
        #
        # 2. NEVER STARTED. SAB transfers ONE nzb at a time while labelling
        #    every queued slot "Downloading" -- normalize_sab_slot's own
        #    docstring says so ("every other slot sits at 0 regardless of queue
        #    speed"), and it was measured live at 1 of 146 slots holding any
        #    bytes. So zero byte-movement is the NORMAL condition for everything
        #    behind the head, and this rule was flagging queue_depth-1 items on
        #    every deep queue, forever, at 10 destructive actions per day.
        #    An item that has never received a single byte has not stalled; it
        #    is waiting its turn.
        #
        # has_started=None means the caller cannot tell (e.g. the pinned-strike
        # re-check, which sees one slot and no sample history). That preserves
        # the prior behaviour rather than silently widening the exemption.
        #
        # NOT A BLIND SPOT: "nothing is starting at all" is a QUEUE-level fault
        # and belongs to canaries/sab-stall.sh (`sab-stalled`: queue speed ~0
        # with slots waiting). Item-level stall detection answers a different
        # question -- did something that WAS moving stop.
        if queue_paused:
            return None
        if has_started is False:
            return None
        return "sab-zero-movement"
    if slot_state in _SAB_PP_HUNG_STATES:
        return "sab-pp-hung"
    return None


def is_suspicious_size(t: dict) -> bool:
    if t.get("progress", 0.0) < 1.0:
        return False  # only judge final size on completed downloads
    cat = t.get("category", "")
    sz = t.get("size_bytes", 0)
    if cat in ("radarr", "radarr2") and sz < SUSPICIOUS_MOVIE_BYTES:
        return True
    if cat in ("sonarr", "sonarr2") and sz < SUSPICIOUS_EPISODE_BYTES:
        return True
    return False


def is_orphan(t: dict, arr_queues: dict) -> bool:
    cat = t.get("category", "")
    if cat not in ("sonarr", "sonarr2", "radarr", "radarr2"):
        return False
    queue = arr_queues.get(cat) or []
    h = t["hash"].lower()
    return not any((q.get("downloadId") or "").lower() == h for q in queue)


def find_zombies(qbit_hashes: set, arr_queues: dict) -> list:
    """*arr queue items whose hash isn't in qBit."""
    out = []
    for slug, items in arr_queues.items():
        for q in items:
            h = (q.get("downloadId") or "").lower()
            if h and h not in qbit_hashes:
                out.append({
                    "slug": slug,
                    "queue_id": q.get("id"),
                    "hash": q.get("downloadId"),
                    "title": q.get("title", "?"),
                })
    return out


_STUCK_IMPORT_STATES = {"importPending", "importBlocked", "importFailed"}


def find_stuck_imports(arr_queues: dict) -> list:
    """*arr queue items in importPending/Blocked/Failed (rule 4).

    Visibility-only: the collector reports these but never auto-acts on them
    — `arr-housekeeping.py --unstick` hourly cron handles the actual action
    based on its own 6-hour aging logic. Surfacing here lets the MCP read
    tools show them and lets operators see them in snapshot health.
    """
    out = []
    for slug, items in arr_queues.items():
        for q in items:
            ts = q.get("trackedDownloadState")
            if ts in _STUCK_IMPORT_STATES:
                out.append({
                    "slug": slug,
                    "queue_id": q.get("id"),
                    "hash": q.get("downloadId"),
                    "title": q.get("title", "?"),
                    "tracked_state": ts,
                    "status_messages": q.get("statusMessages") or [],
                })
    return out


def compute_bad_grab_signals(t: dict) -> dict:
    """Rule 3: bad-grab signals for one enriched torrent.

    Returns {"suspicious_size": bool, "negative_cf": bool, "any": bool}.

    These are 'bad grab' indicators on completed (or near-completed) torrents:
    the *arr accepted a release that turned out to be a sample/scam (rule
    3a: suspicious size) or that the project's Custom Format rules now score
    negative (rule 3b: negative_cf). Surfaced per-torrent so the PS1
    workstation collector can flag rule-3 hits as immediate-action
    candidates (no 3-hour wait needed — the file is already done).
    """
    suspicious = is_suspicious_size(t)
    arr = t.get("arr") or {}
    cf_score = arr.get("cf_score") if isinstance(arr, dict) else 0
    negative_cf = isinstance(cf_score, (int, float)) and cf_score < 0
    return {
        "suspicious_size": bool(suspicious),
        "negative_cf": bool(negative_cf),
        "any": bool(suspicious or negative_cf),
    }


# --- Live-system collection (mocked out in tests) ---------------------------

def _collect_qbit() -> dict:
    c = QbitClient()
    if not c.login():
        return {"error": "login_failed", "torrents": [], "totals": {}}
    raw = c.list_torrents()
    norm = [normalize_qbit_torrent(t) for t in raw]
    totals = {
        "count": len(norm),
        "dl_mbps": round(sum(t["dl_speed_bytes_s"] for t in norm) / 125_000.0, 2),
        "up_mbps": round(sum(t["up_speed_bytes_s"] for t in norm) / 125_000.0, 2),
    }
    return {"torrents": norm, "totals": totals}


def _collect_sab() -> dict:
    """SAB section, qbit-parity shape: {"slots": [...], "queue": {...},
    "totals": {...}} on success, {"error": ..., "slots": [], "queue": {},
    "totals": {}} on any failure (missing secrets OR transport error —
    SabClient raises on transport error by design, so this is the one place
    that catches it, same job _collect_qbit's `if not c.login()` check does
    for qBit's failure mode)."""
    c = SabClient()
    if not c.host or not c.apikey:
        return {"error": "no_secrets", "slots": [], "queue": {}, "totals": {}}
    try:
        meta = c.queue_meta()
        raw_slots = c.list_slots()
    except Exception as e:
        return {"error": str(e)[:200], "slots": [], "queue": {}, "totals": {}}
    norm = [normalize_sab_slot(s, meta.get("kbpersec", 0.0)) for s in raw_slots]
    return {
        "slots": norm,
        "queue": meta,
        "totals": {"count": len(norm)},
    }


def _collect_arr(slug: str, version: str) -> dict:
    """Collect one *arr's queue + missing-count + system status.

    Returns just the per-arr dict (slug is known to the caller via
    the ThreadPoolExecutor futures map; no need to round-trip it).
    """
    c = ArrClient(slug, version)
    code_q, queue = c.get("/queue", query="pageSize=500&includeUnknownSeriesItems=true")
    code_m, miss = c.get("/wanted/missing", query="pageSize=1")
    code_s, status = c.get("/system/status")
    if code_q != 200 or code_s != 200:
        return {"error": "auth_failed", "queue": [], "missing_count": 0}
    records = (queue.get("records") if isinstance(queue, dict) else queue) or []
    miss_total = (miss.get("totalRecords", 0) if isinstance(miss, dict) else 0)
    return {
        "queue": records,
        "missing_count": miss_total,
        "system_status": status if isinstance(status, dict) else {},
    }


def _collect_seerr() -> dict:
    """Build externalServiceId → request map for cross-referencing."""
    secrets = Path(os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets")))
    try:
        port = (secrets / "seerr.port").read_text().strip()
        key = (secrets / "seerr.key").read_text().strip()
    except FileNotFoundError:
        return {"error": "no_secrets", "by_external_id": {}}
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/request?take=500",
        headers={"X-Api-Key": key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)[:200], "by_external_id": {}}
    by_eid = {}
    for r in data.get("results", []):
        media = r.get("media") or {}
        eid = media.get("externalServiceId")
        if eid is None:
            continue
        by_eid[str(eid)] = {
            "id": r.get("id"),
            "requested_by": (r.get("requestedBy") or {}).get("email"),
            "requested_at": r.get("createdAt"),
        }
    return {"by_external_id": by_eid}


def _enrich(qbit_torrents: list, arr_queues: dict, seerr_idx: dict) -> list:
    """Join qBit hash → *arr queue item → Seerr request."""
    # Build hash → (slug, queue_item) index
    hash_to_arr = {}
    for slug, items in arr_queues.items():
        for q in items:
            h = (q.get("downloadId") or "").lower()
            if h:
                hash_to_arr[h] = (slug, q)
    by_eid = (seerr_idx or {}).get("by_external_id", {})
    for t in qbit_torrents:
        slug_q = hash_to_arr.get(t["hash"].lower())
        if slug_q:
            slug, q = slug_q
            ext_id = (
                str(q.get("seriesId"))
                if "Episode" in (q.get("title", "")) or slug.startswith("sonarr")
                else str(q.get("movieId"))
            )
            t["arr"] = {
                "slug": slug,
                "queue_id": q.get("id"),
                "title": q.get("title", "?"),
                "tracked_state": q.get("trackedDownloadState"),
                "status_messages": q.get("statusMessages") or [],
                "cf_score": q.get("customFormatScore", 0),
            }
            t["seerr_request"] = by_eid.get(ext_id)
        else:
            t["arr"] = None
            t["seerr_request"] = None
    return qbit_torrents


def _collect_plex(recent_hours: int) -> dict:
    """Delegates to ./plex.py via subprocess (needs python-plexapi venv).

    plexapi isn't on the seedbox's system python3 — it lives in a dedicated
    venv at ~/.apps/python-plexapi/venv/. Try the venv first; fall back to
    system python3 only if the venv doesn't exist (lets the script still
    work in test environments).
    """
    plex_script = HERE / "plex.py"
    if not plex_script.exists():
        return {"error": "plex_script_missing", "libraries": []}
    venv_py = Path.home() / ".apps" / "python-plexapi" / "venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else "python3"
    try:
        proc = subprocess.run(
            [py, str(plex_script), "--emit-json",
             "--recent-hours", str(recent_hours)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"error": "plex_timeout", "libraries": []}
    if proc.returncode != 0:
        return {"error": f"plex_exit_{proc.returncode}", "stderr": proc.stderr[:300],
                "libraries": []}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "plex_bad_json", "libraries": []}


def run(include: set, recent_hours: int) -> dict:
    # Aware-UTC: the old naive utcnow().astimezone(-7) silently assumed the
    # naive value was *local* time (correct only on a UTC box). Anchoring to
    # UTC makes captured_at_az right regardless of the host clock.
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    az = now.astimezone(dt.timezone(dt.timedelta(hours=-7)))
    out = {
        "captured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "captured_at_az": az.isoformat(),
    }

    # Parallel qBit + SAB + 4 *arrs
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {}
        if "qbit" in include:
            futs["qbit"] = ex.submit(_collect_qbit)
        if "sab" in include:
            futs["sab"] = ex.submit(_collect_sab)
        if "arrs" in include:
            for slug, ver in ARRS:
                futs[slug] = ex.submit(_collect_arr, slug, ver)
        # Wrap each future result so a single _collect_arr exception (e.g.
        # connection reset mid-collect) doesn't drop the entire snapshot.
        # Surface as a structured error dict matching the in-band failure
        # shape — the consumer can inspect arrs[slug].get('error').
        results = {}
        for k, f in futs.items():
            try:
                results[k] = f.result()
            except Exception as exc:
                results[k] = {"error": f"collect_failed: {exc}",
                              "queue": [], "missing_count": 0}

    qbit = results.get("qbit", {"torrents": [], "totals": {}})
    arrs = {slug: results.get(slug, {}) for slug, _ in ARRS}

    if "seerr" in include:
        seerr_idx = _collect_seerr()
    else:
        seerr_idx = {"by_external_id": {}}

    # Enrich qBit torrents in-place
    arr_queues = {slug: a.get("queue", []) for slug, a in arrs.items()}
    qbit_hashes = {t["hash"].lower() for t in qbit["torrents"]}
    qbit["torrents"] = _enrich(qbit["torrents"], arr_queues, seerr_idx)

    # Rule 3 (bad grab) signals — per-torrent annotation. Computed AFTER
    # enrichment so cf_score from the *arr is available.
    for t in qbit["torrents"]:
        t["bad_grab_signals"] = compute_bad_grab_signals(t)

    out["qbit"] = qbit
    # The sab key is emitted ONLY when requested: qflix-collect.py's ghost
    # prune treats a MISSING sab section as "no evidence, keep sab entries"
    # — an always-present healthy-empty shape would read as "queue empty,
    # prune everything" on any snapshot collected without --include sab.
    if "sab" in include:
        out["sab"] = results.get("sab", {"slots": [], "queue": {}, "totals": {}})
    out["arrs"] = arrs

    if "plex" in include:
        out["plex"] = _collect_plex(recent_hours)
    else:
        out["plex"] = {"libraries": [], "active_sessions": 0, "last_scan": None}

    out["health"] = {
        "kuma_red": _kuma_red_list(),
        "zombies": find_zombies(qbit_hashes, arr_queues),
        "stuck_imports": find_stuck_imports(arr_queues),  # rule 4 — visibility only
    }
    return out


def _kuma_red_list() -> list:
    """Return list of Kuma monitor names currently down per
    ~/.opt/maint/state.json."""
    sf = Path(os.environ.get(
        "MANITOBA_MAINT_STATE",
        str(Path.home() / ".opt" / "maint" / "state.json"),
    ))
    try:
        state = json.loads(sf.read_text())
    except Exception:
        return []
    return [name for name, d in (state.get("monitors") or {}).items()
            if d.get("status") != "up"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--cron", action="store_true")
    ap.add_argument("--include", default="qbit,arrs,seerr,plex,sab",
                    help="comma list: qbit,arrs,seerr,plex,sab")
    ap.add_argument("--recent-hours", type=int, default=24)
    args = ap.parse_args()
    include = {x.strip() for x in args.include.split(",") if x.strip()}

    started = time.time()
    try:
        result = run(include, args.recent_hours)
    except Exception as e:
        if args.cron:
            try:
                from lib.notify import notify  # type: ignore
                notify(f"collect.py failed: {e}", "error")
            except Exception as _exc:
                sys.stderr.write("collect.py: notify failed - alerts unavailable from this script: "
                                 + repr(_exc) + "\n")
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.emit_json:
        json.dump(result, sys.stdout, default=str)
        sys.stdout.write("\n")
    elapsed = round(time.time() - started, 2)
    print(f"collect.py: ok in {elapsed}s ({len(result.get('qbit', {}).get('torrents', []))} torrents)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
