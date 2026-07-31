#!/usr/bin/env python3
"""scripts/mcp/app_status.py — Heartbeat v2 seedbox aggregator.

Single read-only aggregator invoked over a forced-command SSH key by the
Android Heartbeat app. Emits one JSON doc to stdout, stdlib only, target
<5s wall. Per-section failure isolation: a dead source degrades that one
section (ok=false) without killing the rest of the doc.

Spec:  docs/superpowers/specs/2026-07-15-heartbeat-android-design.md
Plan:  docs/superpowers/plans/2026-07-15-heartbeat-android.md

Box python is 3.9.2 -- no 3.10+ syntax (match, X | Y unions at runtime).
`from __future__ import annotations` defers annotation evaluation so
lowercase generics (list[dict], etc.) are safe to write in signatures.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# Self-locate scripts/mcp/lib + scripts/maint/lib on sys.path (identical
# bootstrap to collect.py:30-35 -- both `lib/` dirs merge into one
# namespace package, see tests/conftest.py's comment on the same trick).
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                   # for lib.qbit_client
sys.path.insert(0, str(HERE.parent / "maint"))  # for lib.secrets

from lib.qbit_client import QbitClient  # noqa: E402
from lib.secrets import read_secret     # noqa: E402

VERSION = 2
ALL_SECTIONS = ("quota", "kuma", "streams", "top5", "downloads")

# --- Config / paths (env-overridable, mirrors collect.py / qflix-collect.py) -
KUMA_DB = Path(os.environ.get(
    "QFLIX_KUMA_DB", str(Path.home() / ".apps" / "uptimekuma" / "kuma.db")))
MAINT_STATE_FILE = Path(os.environ.get(
    "MANITOBA_MAINT_STATE", str(Path.home() / ".opt" / "maint" / "state.json")))
QFLIX_COLLECT_DATA = Path(os.environ.get(
    "QFLIX_COLLECT_DATA", str(Path.home() / ".opt" / "qflix-collect")))
ANIME_JANITOR_DIR = Path(os.environ.get(
    "QFLIX_ANIME_JANITOR_DIR", str(Path.home() / ".opt" / "maint" / "anime-janitor")))

# The shared Kuma instance also carries another operator's monitors. A red
# "Quadstronix Node *" is that project's outage, not ours -- still worth a
# line in the alert feed, but the two nodes + their parent group monitor
# collapse into ONE line so one external blip doesn't spam three.
QUADSTRONIX_NAMES = frozenset({"Quadstronix", "Quadstronix Node 1", "Quadstronix Node 2"})

DISK_CRIT_PCT = 90.0
DISK_WARN_PCT = 80.0
BW_CRIT_AVAIL_PCT = 10.0
BW_WARN_AVAIL_PCT = 20.0
MAINT_FAILED_WINDOW_HOURS = 48


# =============================================================================
# Pure parsers -- network-free, unit tested directly with fixtures lifted
# from the plan's recon samples. Each one takes already-fetched text/JSON
# and returns a plain dict/list; all I/O lives in the _collect_* functions
# below.
# =============================================================================

_SIZE_TOKEN = re.compile(r'^(\d+(?:\.\d+)?)([KMGT])?B?$', re.IGNORECASE)
_UNIT_TO_GB = {"K": 1.0 / 1024 / 1024, "M": 1.0 / 1024, "G": 1.0, "T": 1024.0}


def _size_to_gb(token: str) -> Optional[float]:
    m = _SIZE_TOKEN.match(token.strip())
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "G").upper()
    return num * _UNIT_TO_GB[unit]


def _first_two_sizes(tokens) -> Optional[list]:
    out = []
    for tok in tokens:
        v = _size_to_gb(tok)
        if v is not None:
            out.append(v)
        if len(out) == 2:
            return out
    return None


def parse_quota(text: str) -> dict:
    """Parse `quota -s` output -> {"used_gb", "total_gb", "pct"}.

    Finds the row starting with a `/dev/...` filesystem path and reads its
    first two size columns (used, quota-limit). Handles both layouts quota
    -s can emit: numbers on the same line as the path, or wrapped to the
    next line when the device path is long.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "/dev/" not in line:
            continue
        rest_tokens = [t for t in line.split() if not t.startswith("/dev/")]
        pair = _first_two_sizes(rest_tokens)
        if pair is None and i + 1 < len(lines):
            pair = _first_two_sizes(lines[i + 1].split())
        if pair is None:
            continue
        used_gb, total_gb = pair
        pct = round(used_gb / total_gb * 100, 1) if total_gb else 0.0
        return {
            "used_gb": int(round(used_gb)),
            "total_gb": int(round(total_gb)),
            "pct": pct,
        }
    raise ValueError("quota -s: no /dev/ row found")


_TRAFFIC_AVAIL_RE = re.compile(r'Traffic available:\s*([\d.]+)\s*%', re.IGNORECASE)
_LAST_RESET_RE = re.compile(r'Last traffic reset:\s*(.+)', re.IGNORECASE)
_NEXT_RESET_RE = re.compile(r'Next traffic reset:\s*(.+)', re.IGNORECASE)
_DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})')


def _traffic_date_to_iso(raw: str) -> Optional[str]:
    m = _DATE_RE.search(raw)
    if not m:
        return None
    return "{}T{}".format(m.group(1), m.group(2))


def parse_traffic(text: str) -> dict:
    """Parse `app-traffic info` output.

    Ultra.cc user accounts expose no GB numbers for bandwidth -- only a
    percentage-available figure and reset dates (verified live recon,
    2026-07-15) -- so that's all this returns. used_pct is the complement
    of available_pct.
    """
    m = _TRAFFIC_AVAIL_RE.search(text)
    if not m:
        raise ValueError("app-traffic info: no 'Traffic available' line found")
    available_pct = round(float(m.group(1)), 2)
    used_pct = round(100.0 - available_pct, 2)
    last_m = _LAST_RESET_RE.search(text)
    next_m = _NEXT_RESET_RE.search(text)
    return {
        "used_pct": used_pct,
        "available_pct": available_pct,
        "last_reset": _traffic_date_to_iso(last_m.group(1)) if last_m else None,
        "next_reset": _traffic_date_to_iso(next_m.group(1)) if next_m else None,
    }


# qBit state vocabulary. qBit 5.x renamed pausedDL/pausedUP -> stoppedDL/
# stoppedUP (same rename collect.py's matches_stale_rule() already
# accounts for) -- both spellings map to the same bucket here so this
# classifier works unchanged across a 4.x -> 5.x qBit upgrade.
_QBIT_ACTIVE = frozenset({"downloading", "forcedDL"})
_QBIT_STALLED = frozenset({"stalledDL"})
_QBIT_ERRORED = frozenset({"error", "missingFiles"})
_QBIT_STOPPED_DL = frozenset({"stoppedDL", "pausedDL"})
_QBIT_SEEDING = frozenset({"uploading", "stalledUP", "forcedUP", "queuedUP", "checkingUP"})


def classify_qbit(torrents: list) -> dict:
    """Bucket qBit torrents by state.

    Returns a superset of what the contract's "downloads.qbit" section
    surfaces: total/active/stalled_dl/errored/seeding go straight into the
    doc; stopped_dl is tracked here (for classification-vocabulary
    coverage, incl. the qBit5 stoppedDL rename) but is represented in the
    final doc via the itemized "stuck" list instead of this summary --
    stopped_dl torrents are exactly the ones stale-state.json is tracking.

    Also sums transfer rates. The Heartbeat downloads card showed SAB's kbps but
    had NO qBit rate at all, so the two download clients could not be compared on
    the one axis an operator actually glances at -- is anything moving. Summed
    from the per-torrent `dlspeed`/`upspeed` already present in the
    /api/v2/torrents/info payload rather than a second call to
    /api/v2/transfer/info: the forced-command SSH channel runs exactly this
    script, so adding an API round-trip costs latency on every pull-to-refresh
    for a number we already have. Bytes/sec, as qBit reports them; the client
    formats.
    """
    counts = {"total": len(torrents), "active": 0, "stalled_dl": 0,
              "errored": 0, "stopped_dl": 0, "seeding": 0,
              "dl_bps": 0, "up_bps": 0}
    for t in torrents:
        state = t.get("state", "")
        if state in _QBIT_ACTIVE:
            counts["active"] += 1
        elif state in _QBIT_STALLED:
            counts["stalled_dl"] += 1
        elif state in _QBIT_ERRORED:
            counts["errored"] += 1
        elif state in _QBIT_STOPPED_DL:
            counts["stopped_dl"] += 1
        elif state in _QBIT_SEEDING:
            counts["seeding"] += 1
        # Missing/garbage rates are treated as 0 rather than skipping the
        # torrent: a rate is a nice-to-have and must never cost us the counts.
        for key, field in (("dl_bps", "dlspeed"), ("up_bps", "upspeed")):
            try:
                counts[key] += int(t.get(field) or 0)
            except (TypeError, ValueError):
                pass
    return counts


def _parse_iso(raw) -> Optional[dt.datetime]:
    """Parse an ISO8601 timestamp (with or without trailing Z) to an
    aware UTC datetime. Returns None on anything unparsable rather than
    raising -- callers treat that as 'exclude from window'."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def top5_requests(requests_json: dict, now: dt.datetime) -> list:
    """Seerr /api/v1/request results -> top 5 requesters in the last 30d.

    Filters to createdAt >= now-30d, groups by requestedBy.id, labels with
    displayName (falling back to plexUsername when Seerr has no display
    name set for that user), ranks by count desc (ties broken by name).
    """
    cutoff = now - dt.timedelta(days=30)
    results = (requests_json or {}).get("results") or []
    by_user = {}
    for r in results:
        created = _parse_iso(r.get("createdAt"))
        if created is None or created < cutoff:
            continue
        rb = r.get("requestedBy") or {}
        label = rb.get("displayName") or rb.get("plexUsername") or "unknown"
        key = rb.get("id", label)
        entry = by_user.setdefault(key, {"user": label, "count": 0})
        entry["count"] += 1
    ranked = sorted(by_user.values(), key=lambda e: (-e["count"], e["user"]))
    return ranked[:5]


def top5_watch(rows: list) -> list:
    """Tautulli get_home_stats(top_users, duration) rows -> top 5 by watch
    time. `rows` is the already-unwrapped `response.data.rows` list."""
    out = []
    for r in rows or []:
        secs = r.get("total_duration") or 0
        out.append({
            "user": r.get("friendly_name") or "unknown",
            "hours": round(secs / 3600.0, 1),
            "plays": r.get("total_plays") or 0,
        })
    out.sort(key=lambda e: e["hours"], reverse=True)
    return out[:5]


def parse_streams(activity_json: dict) -> dict:
    """Tautulli get_activity -> stream fraction + transcode/bandwidth.

    stream_count comes back as a STRING from Tautulli (verified recon) --
    must be int()'d. Distinct users = set of sessions[].user_id (a
    fractional streams/users, e.g. 3/2, means someone is multi-streaming).
    """
    data = ((activity_json or {}).get("response") or {}).get("data") or {}
    sessions = data.get("sessions") or []
    streams = int(data.get("stream_count") or 0)
    users = len({s.get("user_id") for s in sessions if s.get("user_id") is not None})
    transcodes = int(data.get("stream_count_transcode") or 0)
    wan_kbps = int(float(data.get("wan_bandwidth") or 0))
    return {"streams": streams, "users": users, "transcodes": transcodes,
            "wan_kbps": wan_kbps}


def parse_kuma_rows(rows: list) -> dict:
    """rows: list of {"name","status","msg","time"} dicts -- one latest
    heartbeat per active monitor. status: 0=down 1=up 2=pending
    3=maintenance (per recon; only 0/1 are counted, matching the box's
    live monitor mix)."""
    total = len(rows)
    up = sum(1 for r in rows if r.get("status") == 1)
    down_rows = [r for r in rows if r.get("status") == 0]
    red = [{"name": r.get("name"), "msg": r.get("msg") or "", "since": r.get("time")}
           for r in down_rows]
    return {"total": total, "up": up, "down": len(down_rows), "red": red}


def parse_sab_queue(payload: dict) -> dict:
    """SABnzbd `?mode=queue&output=json` -> the fields the doc needs.
    mbleft/mb/kbpersec/noofslots arrive as strings from the SAB API."""
    q = (payload or {}).get("queue") or {}
    return {
        "queued": int(q.get("noofslots") or 0),
        "paused": bool(q.get("paused")),
        "mb_left": round(float(q.get("mbleft") or 0), 1),
        "mb_total": round(float(q.get("mb") or 0), 1),
        "kbps": int(float(q.get("kbpersec") or 0)),
    }


def count_sab_failed(history_payload: dict, now_epoch: float,
                     window_hours: int = 24) -> int:
    """SABnzbd `?mode=history` -> count of jobs that FAILED inside the
    window. A failed Usenet job (missing articles, unpack/par2 failure) was
    previously invisible in the doc — the *arr re-searches, but repeated
    failures mean a dead indexer/provider and deserve an alert line.
    `completed` is a unix epoch per slot; malformed slots are skipped."""
    slots = ((history_payload or {}).get("history") or {}).get("slots") or []
    cutoff = now_epoch - window_hours * 3600
    n = 0
    for s in slots:
        if (s.get("status") or "").lower() != "failed":
            continue
        try:
            if float(s.get("completed") or 0) >= cutoff:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


_SAB_UNPACK_DISK_FULL_NEEDLE = "unpacking failed, write error or disk is full"


def has_sab_unpack_failure(history_payload: dict) -> bool:
    """SABnzbd `?mode=history` -> True iff any slot's fail_message matches
    the FDH blind spot (design doc research, 2026-07-19): SAB reports
    "Unpacking failed, write error or disk is full?" as a Warning, never a
    Failed status, so Sonarr/Radarr's FailedDownloadService never sees it
    and never re-searches -- the job just silently rots. Case-insensitive
    substring so trailing punctuation/wording variants still match; not
    time-windowed (reuses whichever history page failed_24h already
    fetched -- one occurrence anywhere in that page is disk-full evidence
    worth a page, this isn't a rate count)."""
    slots = ((history_payload or {}).get("history") or {}).get("slots") or []
    for s in slots:
        msg = s.get("fail_message") or ""
        if _SAB_UNPACK_DISK_FULL_NEEDLE in msg.lower():
            return True
    return False


def parse_sab_slots(queue_payload: dict) -> list:
    """SABnzbd `?mode=queue&output=json` -> per-slot list from `queue.slots`,
    renamed to the vocabulary build_stuck_list's second name-join source
    speaks (C6 contract): nzo_id -> id, filename -> name, status -> state.
    cat/mb/mbleft pass through verbatim (mb/mbleft arrive as strings from
    the SAB API, same as parse_sab_queue above -- no numeric conversion
    needed here, this helper only feeds a name/liveness join)."""
    slots = ((queue_payload or {}).get("queue") or {}).get("slots") or []
    return [{
        "id": s.get("nzo_id"),
        "name": s.get("filename"),
        "cat": s.get("cat"),
        "state": s.get("status"),
        "mb": s.get("mb"),
        "mbleft": s.get("mbleft"),
    } for s in slots]


# A real SAB nzo_id always carries this literal prefix (verified live,
# 2026-07-19) -- qBit hashes are 40-char hex and never start with it, so
# it doubles as both the id-shape classifier (C5's unstick.py `_id_kind`
# uses the identical check) and a collision-proof namespace: a SAB id and
# a qBit hash can never land on the same dict key even after the two name
# sources below are merged into one.
_SAB_ID_PREFIX = "SABnzbd_nzo"


def _short_id(key: str) -> Optional[str]:
    """Collision-proof 8-char label for a stuck/unstick id, decided by SHAPE:
    torrent hash -> first 8 (high-entropy hex); SAB nzo_id -> LAST 8 (every
    real nzo_id shares the literal "SABnzbd_nzo_" prefix, so first-8 would
    render "SABnzbd_" for every usenet row). Used by BOTH build_stuck_list and
    recent_unsticks_from_lines so the two doc sections label the same id
    identically (council 2026-07-20, Defect 3)."""
    if not key:
        return None
    return key[-8:] if key.startswith(_SAB_ID_PREFIX) else key[:8]


def build_stuck_list(stale_state: dict, torrents: list, sab_slots: list = None) -> list:
    """Join stale-state.json's `hashes` map (candidates only) to a name --
    qBit torrent name for torrent-shaped keys, SAB slot filename (via
    parse_sab_slots) for SAB-shaped keys. The two name sources merge into
    ONE dict (qBit hashes lowercased for the lookup, as before; SAB nzo_ids
    looked up verbatim since they're not hex and case is significant) --
    safe because the id-shape prefix makes the two namespaces disjoint.

    `acted` reflects whether the autonomous unstick loop already dispatched
    an action for this entry (acted_on_at set). `kind` and the `hash8`
    label are both decided by id SHAPE, matching unstick.py's `_id_kind`
    dispatch (C5): torrent -> first 8 chars; usenet -> LAST 8 (every real
    nzo_id shares the identical "SABnzbd_nzo_" prefix, so a first-8 label
    would collide across every usenet entry).

    Ghost guard: a candidate whose id is no longer live in its OWN source
    (qBit torrents for torrent-kind, SAB slots for usenet-kind) is already
    resolved -- skip it rather than report a phantom stuck row (regression
    guard for the 2026-07-19 phantom-stuck incident this already fixed for
    qBit; usenet gets the identical treatment now that it's a first-class
    stuck-list citizen too). `sab_slots` defaults to None/[] so existing
    2-arg call sites keep working unchanged."""
    names = {}
    for t in (torrents or []):
        names[(t.get("hash") or "").lower()] = t.get("name", "?")
    for s in (sab_slots or []):
        sid = s.get("id")
        if sid:
            names[sid] = s.get("name", "?")

    out = []
    for key, entry in ((stale_state or {}).get("hashes") or {}).items():
        if not entry.get("candidate_for_unstick"):
            continue
        is_sab = key.startswith(_SAB_ID_PREFIX)
        name = names.get(key if is_sab else key.lower())
        if name is None:
            continue  # ghost — id gone from its own live source
        out.append({
            "hash8": _short_id(key),
            "name": name,
            "hours": int(entry.get("consecutive_zero_hours") or 0),
            "rule": entry.get("rule_matched"),
            "acted": bool(entry.get("acted_on_at")),
            "kind": "usenet" if is_sab else "torrent",
        })
    return out


def recent_unsticks_from_lines(lines: list) -> list:
    """events/<date>.jsonl lines (unstick.py's _record_event schema) ->
    recent-unsticks list, newest first. Non-"unstick"-action lines
    (refusals, other event types) and malformed lines are skipped."""
    out = []
    for raw in lines:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if ev.get("action") != "unstick":
            continue
        h = ev.get("hash") or ""
        # Kind-aware label (Defect 3): a SAB unstick's hash is an nzo_id, so a
        # bare h[:8] would collapse every usenet unstick to "SABnzbd_". Use the
        # same shape-based short id as build_stuck_list.
        out.append({"ts": ev.get("ts"), "hash8": _short_id(h),
                     "result": ev.get("result"),
                     "kind": "usenet" if h.startswith(_SAB_ID_PREFIX) else "torrent"})
    out.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return out


def derive_alerts(doc: dict) -> list:
    """Derive the flat, ordered (crit before warn) alert list from an
    assembled doc.

    `doc` carries the 5 contract sections (quota/kuma/streams/top5/
    downloads) plus two internal-only keys this function needs that never
    reach the final JSON: "maint" (raw ~/.opt/maint/state.json apps map,
    for the auto-heal-failed rule) and "_now" (the aware UTC datetime run()
    anchored generated_at to, for deterministic window math in tests).

    Rules (exact thresholds from the plan):
      - each Kuma red = crit, EXCEPT the Quadstronix trio which rolls into
        one combined crit line
      - disk pct >=90 crit / >=80 warn
      - bandwidth available_pct <10 crit / <20 warn
      - maint apps[*].event == "failed" within 48h = crit
      - any stuck entries = warn
      - SAB paused = warn
      - SAB history has an "Unpacking failed, write error or disk is
        full?" row (the FDH blind spot — Sonarr/Radarr never see it as
        Failed) = crit
    Empty list = all clear.
    """
    crit = []
    warn = []

    kuma = doc.get("kuma") or {}
    red = kuma.get("red") or []
    quad = [r for r in red if r.get("name") in QUADSTRONIX_NAMES]
    other = [r for r in red if r.get("name") not in QUADSTRONIX_NAMES]
    for r in other:
        crit.append({"level": "crit",
                      "text": "Kuma down: {} — {}".format(r.get("name"), r.get("msg") or "")})
    if quad:
        names = ", ".join(sorted(r.get("name") for r in quad))
        crit.append({"level": "crit", "text": "Kuma down (external): {}".format(names)})

    quota = doc.get("quota") or {}
    disk = quota.get("disk") or {}
    pct = disk.get("pct")
    if isinstance(pct, (int, float)):
        if pct >= DISK_CRIT_PCT:
            crit.append({"level": "crit", "text": "Disk quota {}% used".format(pct)})
        elif pct >= DISK_WARN_PCT:
            warn.append({"level": "warn", "text": "Disk quota {}% used".format(pct)})

    bandwidth = quota.get("bandwidth") or {}
    avail = bandwidth.get("available_pct")
    if isinstance(avail, (int, float)):
        if avail < BW_CRIT_AVAIL_PCT:
            crit.append({"level": "crit", "text": "Bandwidth available {}%".format(avail)})
        elif avail < BW_WARN_AVAIL_PCT:
            warn.append({"level": "warn", "text": "Bandwidth available {}%".format(avail)})

    maint = doc.get("maint") or {}
    now = doc.get("_now") or dt.datetime.now(dt.timezone.utc)
    for slug, entry in (maint.get("apps") or {}).items():
        if not isinstance(entry, dict) or entry.get("event") != "failed":
            continue
        ts = _parse_iso(entry.get("updated_at"))
        if ts is not None and (now - ts) <= dt.timedelta(hours=MAINT_FAILED_WINDOW_HOURS):
            crit.append({"level": "crit", "text": "Auto-heal failed: {}".format(slug)})

    # Any systemd --user unit in a FAILED state (a maint job that crashed on its
    # last run - reaper, anime-janitor, a canary, an ingest, etc.). This is the
    # broadest maint-failure signal and is Kuma-independent: it catches a job
    # that broke before it could ever push a heartbeat.
    for unit in (maint.get("failed_units") or []):
        crit.append({"level": "crit", "text": "Maint unit failed: {}".format(unit)})

    downloads = doc.get("downloads") or {}
    stuck = downloads.get("stuck") or []
    if len(stuck) > 0:
        warn.append({"level": "warn",
                      "text": "{} download(s) stuck, pending unstick".format(len(stuck))})

    sab = downloads.get("sab") or {}
    if sab.get("unpack_disk_full"):
        crit.append({"level": "crit",
                      "text": "SAB unpack failed (disk full?) — FDH blind spot"})
    if sab.get("paused"):
        warn.append({"level": "warn", "text": "SABnzbd queue paused"})
    failed = sab.get("failed_24h")
    if isinstance(failed, int) and failed > 0:
        warn.append({"level": "warn",
                      "text": "{} Usenet download(s) failed (24h)".format(failed)})

    return crit + warn


# =============================================================================
# I/O collectors -- one per section, each catches its own exceptions and
# returns a full-shape dict so a dead source never breaks doc structure.
# =============================================================================

def _collect_quota() -> dict:
    try:
        q = subprocess.run(["quota", "-s"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": "quota_subprocess: {}".format(e),
                "disk": None, "bandwidth": None}
    try:
        t = subprocess.run(["app-traffic", "info"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": "traffic_subprocess: {}".format(e),
                "disk": None, "bandwidth": None}
    # Both already-fetched outputs are parsed independently below -- a
    # failure in one parser must not discard a successful parse of the
    # other (they're unrelated data: disk quota vs bandwidth). ok=false iff
    # either parser failed; the error string names which one(s) did, mirroring
    # the errors-list pattern the other multi-source collectors use (e.g.
    # _collect_top5, _collect_downloads).
    errors = []
    disk = None
    try:
        disk = parse_quota(q.stdout)
    except ValueError as e:
        errors.append("quota_parse: {}".format(e))

    bandwidth = None
    try:
        bandwidth = parse_traffic(t.stdout)
    except ValueError as e:
        errors.append("traffic_parse: {}".format(e))

    ok = len(errors) == 0
    return {"ok": ok, "error": "; ".join(errors) if errors else None,
            "disk": disk, "bandwidth": bandwidth}


def _sqlite_ro_uri(path: Path) -> str:
    """Build a `file:...?mode=ro` URI sqlite3 accepts cross-platform (the
    box is POSIX; dev/test may run on Windows -- both need a leading '/'
    before the resolved path per SQLite's URI filename rules)."""
    p = Path(path).resolve().as_posix()
    if not p.startswith("/"):
        p = "/" + p
    return "file:{}?mode=ro".format(p)


def _collect_kuma(db_path: Optional[Path] = None) -> dict:
    db_path = Path(db_path) if db_path else KUMA_DB
    try:
        con = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True, timeout=5)
        try:
            cur = con.execute(
                "SELECT m.name, h.status, h.msg, h.time FROM monitor m "
                "JOIN heartbeat h ON h.id = ("
                "  SELECT id FROM heartbeat WHERE monitor_id = m.id "
                "  ORDER BY time DESC LIMIT 1"
                ") WHERE m.active = 1"
            )
            rows = [{"name": n, "status": s, "msg": m, "time": tm}
                    for (n, s, m, tm) in cur.fetchall()]
        finally:
            con.close()
    except Exception as e:
        return {"ok": False, "error": "kuma_db: {}".format(e),
                "total": 0, "up": 0, "down": 0, "red": []}
    section = parse_kuma_rows(rows)
    section["ok"] = True
    section["error"] = None
    return section


def _tautulli_get(cmd: str, extra: str = "") -> dict:
    port = read_secret("tautulli.port")
    key = read_secret("tautulli.key")
    url = "http://127.0.0.1:{}/tautulli/api/v2?apikey={}&cmd={}".format(port, key, cmd)
    if extra:
        url += "&" + extra
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _collect_streams() -> dict:
    try:
        activity = _tautulli_get("get_activity")
        section = parse_streams(activity)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200],
                "streams": 0, "users": 0, "transcodes": 0, "wan_kbps": 0}
    section["ok"] = True
    section["error"] = None
    return section


def _collect_seerr_requests() -> dict:
    """Fetch all Seerr requests, paginating via pageInfo when the total
    exceeds one page's `take`."""
    port = read_secret("seerr.port")
    key = read_secret("seerr.key")
    take = 200
    skip = 0
    all_results = []
    page_info = {}
    while True:
        url = ("http://127.0.0.1:{}/api/v1/request"
               "?take={}&skip={}&sort=added").format(port, take, skip)
        req = urllib.request.Request(url, headers={"X-Api-Key": key, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
        results = payload.get("results") or []
        all_results.extend(results)
        page_info = payload.get("pageInfo") or {}
        total = page_info.get("results", len(all_results))
        if len(all_results) >= total or not results:
            break
        skip += take
    return {"pageInfo": page_info, "results": all_results}


def _collect_top5(now: dt.datetime) -> dict:
    errors = []
    requests_30d = []
    watch_30d = []
    try:
        req_json = _collect_seerr_requests()
        requests_30d = top5_requests(req_json, now)
    except Exception as e:
        errors.append("seerr: {}".format(e))
    try:
        payload = _tautulli_get(
            "get_home_stats", "time_range=30&stats_type=duration&stat_id=top_users")
        data = (payload.get("response") or {}).get("data") or {}
        watch_30d = top5_watch(data.get("rows") or [])
    except Exception as e:
        errors.append("tautulli: {}".format(e))
    ok = len(errors) == 0
    return {"ok": ok, "error": "; ".join(errors) if errors else None,
            "requests_30d": requests_30d, "watch_30d": watch_30d}


def _collect_sab_queue() -> dict:
    port = read_secret("sabnzbd.port")
    key = read_secret("sabnzbd.key")
    url = "http://127.0.0.1:{}/api?mode=queue&output=json&apikey={}".format(port, key)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _collect_sab_history() -> dict:
    """Last 60 history slots — enough to cover a day's completions; the
    failed_24h count filters by the per-slot `completed` epoch anyway."""
    port = read_secret("sabnzbd.port")
    key = read_secret("sabnzbd.key")
    url = ("http://127.0.0.1:{}/api?mode=history&start=0&limit=60"
           "&output=json&apikey={}").format(port, key)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _read_recent_event_lines() -> list:
    """Today + yesterday's events/<date>.jsonl lines (UTC dates)."""
    events_dir = QFLIX_COLLECT_DATA / "events"
    lines = []
    today = dt.datetime.now(dt.timezone.utc).date()
    for delta in (0, 1):
        day = today - dt.timedelta(days=delta)
        fp = events_dir / "{}.jsonl".format(day.isoformat())
        try:
            lines.extend(fp.read_text(encoding="utf-8").splitlines())
        except FileNotFoundError:
            continue
    return lines


def _collect_downloads() -> dict:
    errors = []

    qbit_summary = {"total": 0, "active": 0, "stalled_dl": 0, "errored": 0, "seeding": 0}
    torrents = []
    try:
        c = QbitClient()
        if not c.login():
            raise RuntimeError("qbit_login_failed")
        torrents = c.list_torrents()
        classified = classify_qbit(torrents)
        qbit_summary = {k: classified[k]
                         for k in ("total", "active", "stalled_dl", "errored", "seeding")}
    except Exception as e:
        errors.append("qbit: {}".format(e))

    sab_summary = {"queued": 0, "paused": False, "mb_left": 0.0, "mb_total": 0.0, "kbps": 0}
    sab_slots = []
    try:
        queue_payload = _collect_sab_queue()
        sab_summary = parse_sab_queue(queue_payload)
        sab_slots = parse_sab_slots(queue_payload)
    except Exception as e:
        errors.append("sab: {}".format(e))
    # failed_24h / unpack_disk_full ride the same summary dict (additive;
    # the Android app's parser ignores unknown keys). Best-effort: a
    # history fetch failure keeps the queue numbers and just logs the
    # error. Both derive from the SAME fetched history page -- one fetch,
    # two independent reads of it.
    try:
        history_payload = _collect_sab_history()
        now_epoch = dt.datetime.now(dt.timezone.utc).timestamp()
        sab_summary["failed_24h"] = count_sab_failed(history_payload, now_epoch)
        sab_summary["unpack_disk_full"] = has_sab_unpack_failure(history_payload)
    except Exception as e:
        errors.append("sab_history: {}".format(e))

    stuck = []
    try:
        stale_state = json.loads(
            (QFLIX_COLLECT_DATA / "stale-state.json").read_text(encoding="utf-8"))
        stuck = build_stuck_list(stale_state, torrents, sab_slots)
    except FileNotFoundError:
        pass  # no stale-state.json yet is a legitimate empty-doc state
    except Exception as e:
        errors.append("stuck: {}".format(e))
    # Passthrough count for the app: how many of the stuck entries are
    # usenet-kind (i.e. a SAB slot, not a qBit torrent) -- lets the Heartbeat
    # UI badge the SAB tile without re-deriving kind counts client-side.
    sab_summary["slots_stuck"] = sum(1 for e in stuck if e.get("kind") == "usenet")

    recent = []
    try:
        recent = recent_unsticks_from_lines(_read_recent_event_lines())
    except Exception as e:
        errors.append("events: {}".format(e))

    ok = len(errors) == 0
    return {"ok": ok, "error": "; ".join(errors) if errors else None,
            "qbit": qbit_summary, "sab": sab_summary,
            "stuck": stuck, "recent_unsticks": recent}


def _collect_failed_units() -> list:
    """`systemctl --user --failed` -> list of failed unit names. A failed
    oneshot/service stays 'failed' until its next run, so this is a direct,
    Kuma-independent signal that a maintenance job crashed (reaper,
    anime-janitor, a canary, an ingest, ...). Best-effort: any error -> []."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "--failed", "--no-legend", "--plain", "--no-pager"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    units = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts and (parts[0].endswith(".service") or parts[0].endswith(".timer")):
            units.append(parts[0])
    return units


def anime_janitor_summary(moved, now: dt.datetime, days: int = 7) -> dict:
    """Pure: recent anime-library-janitor activity from its moved.json list
    (newest entry last). recent_moves = re-homes in the last `days`; last_move
    = a compact record of the most recent one."""
    cutoff = now - dt.timedelta(days=days)
    recent = 0
    for m in (moved or []):
        ts = _parse_iso(m.get("ts"))
        if ts is not None and ts >= cutoff:
            recent += 1
    last = None
    if moved:
        m = moved[-1]
        last = {"title": m.get("title"), "from": m.get("from"),
                "to": m.get("to"), "ts": m.get("ts")}
    return {"recent_moves": recent, "last_move": last}


def _collect_anime_janitor(now: dt.datetime) -> dict:
    try:
        moved = json.loads((ANIME_JANITOR_DIR / "moved.json").read_text(encoding="utf-8"))
        if not isinstance(moved, list):
            moved = []
    except Exception:
        moved = []
    return anime_janitor_summary(moved, now)


def _collect_maint(now: Optional[dt.datetime] = None) -> dict:
    """Maintenance health: failed systemd --user units (any crashed maint job)
    + recent anime-library-janitor activity. Also carries the internal
    ~/.opt/maint/state.json `apps` map that feeds the auto-heal-failed alert
    rule (stripped from the output section). Best-effort throughout: a dead
    source degrades that field, never the doc."""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    try:
        state = json.loads(MAINT_STATE_FILE.read_text(encoding="utf-8"))
        apps = state.get("apps") or {}
    except Exception:
        apps = {}
    return {
        "ok": True,
        "error": None,
        "apps": apps,                       # internal only (auto-heal-failed rule)
        "failed_units": _collect_failed_units(),
        "anime_janitor": _collect_anime_janitor(now),
    }


# =============================================================================
# Aggregation entry point
# =============================================================================

def run(sections: Optional[list] = None) -> dict:
    """Fetch requested sections concurrently, isolate per-section failures,
    derive alerts, and return the contract dict. sections=None -> all 5.
    A restricted `sections` list still returns a doc with every contract
    key present -- unrequested sections come back as an explicit
    not-fetched stub rather than being omitted."""
    started = time.time()
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    want = set(sections) if sections else set(ALL_SECTIONS)

    jobs = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        if "quota" in want:
            jobs["quota"] = ex.submit(_collect_quota)
        if "kuma" in want:
            jobs["kuma"] = ex.submit(_collect_kuma)
        if "streams" in want:
            jobs["streams"] = ex.submit(_collect_streams)
        if "top5" in want:
            jobs["top5"] = ex.submit(_collect_top5, now)
        if "downloads" in want:
            jobs["downloads"] = ex.submit(_collect_downloads)
        jobs["maint"] = ex.submit(_collect_maint, now)  # always: alerts + output section

        results = {}
        for key, fut in jobs.items():
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = {"ok": False, "error": "collect_failed: {}".format(e)}

    doc = {name: results.get(name, {"ok": False, "error": "not_requested"})
           for name in ALL_SECTIONS}
    doc["maint"] = results.get("maint") or {"apps": {}, "failed_units": [],
                                            "anime_janitor": {}}
    doc["_now"] = now

    alerts = derive_alerts(doc)
    elapsed_ms = int(round((time.time() - started) * 1000))

    # Public maint section: strip the internal `apps` map (that only feeds the
    # auto-heal-failed rule) + keep the operator-facing failure signals.
    mfull = doc["maint"]
    maint_out = {
        "ok": mfull.get("ok", True),
        "error": mfull.get("error"),
        "failed_units": mfull.get("failed_units") or [],
        "anime_janitor": mfull.get("anime_janitor") or {},
    }

    return {
        "meta": {
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "elapsed_ms": elapsed_ms,
            "host": socket.gethostname(),
            "version": VERSION,
        },
        "quota": doc["quota"],
        "kuma": doc["kuma"],
        "streams": doc["streams"],
        "top5": doc["top5"],
        "downloads": doc["downloads"],
        "maint": maint_out,
        "alerts": alerts,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit-json", action="store_true",
                     help="write JSON to stdout (also the default with no "
                          "args -- the forced-command SSH channel invokes "
                          "this bare)")
    ap.add_argument("--sections", default=None,
                     help="comma list to restrict collection, e.g. "
                          "quota,kuma (default: all)")
    args = ap.parse_args()
    sections = ([s.strip() for s in args.sections.split(",") if s.strip()]
                if args.sections else None)
    try:
        result = run(sections=sections)
    except Exception as e:
        print("error: {}".format(e), file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, default=str)
    sys.stdout.write("\n")
    print("app_status.py: ok in {}ms".format(result["meta"]["elapsed_ms"]),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
