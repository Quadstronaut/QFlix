#!/usr/bin/env python3
"""arr-housekeeping — daily Find-Missing sweep + hourly stuck-queue unstick.

Two modes:
  --missing   Fire MissingSearch command on each *arr. Sched: 04:00 Tue–Sun
              (Monday is the cp.ultra.cc maintenance window; we skip it).
  --unstick   Scan each *arr's queue, classify stuck items, and after a
              per-mode grace period DELETE them with removeFromClient=true
              and blocklist=true — Sonarr/Radarr auto-search a replacement
              after the blocklist add. Sched: hourly.

Stall modes:
  poison-executable-payload
                          A STRICT SUBSET of completed-not-imported: the same
                          predicate PLUS a statusMessages line naming both an
                          executable-file rejection and an executable
                          extension. Threshold 0h — a release the importer has
                          already refused as malware-shaped will never import,
                          so the 6h grace buys nothing but six more hours of
                          the file sitting in the client.
  completed-not-imported  status=completed ∧ trackedDownloadState∈
                          {importPending, importBlocked, importFailed}
                          → ARR_STUCK_HOURS_IMPORT (default 6h)
  stalled-no-peers        status=warning ∧ errorMessage contains
                          'stalled' AND 'no connections'
                          → ARR_STUCK_HOURS_PEERS (default 4h)
  metadata-stuck          status=queued ∧ errorMessage contains
                          'downloading metadata'
                          → ARR_STUCK_HOURS_METADATA (default 6h)
  slow-cluster            ≥3 queue items share one downloadId ∧
                          ETA > ARR_STUCK_DAYS_CLUSTER_ETA (default 30d) ∧
                          sizeleft stable over
                          ARR_STUCK_DAYS_CLUSTER_NOPROGRESS (default 7d)
                          → triggers immediately when predicate matches

Caps: ARR_MAX_ACTIONS_PER_RUN (default 10), ARR_MAX_ACTIONS_PER_SLUG
(default 5). Cap-hit escalates Discord notification to error level.

State for stuck-tracking is keyed by qBit downloadId (hash) so it's stable
across queue-id renumberings. Stored at ~/.opt/maint/stuck-queue-state.json.
New fields ('mode', 'sizeleft_history') are backward-compatible — pre-
extension records still parse correctly.

RE-GRAB LOOP GUARD (2026-09-17)
-------------------------------
The hash key above is stable across queue-id renumbering and USELESS across
re-grabs: every replacement release is a new hash, so a title whose every
release is bad was blocklisted over and over with no memory of the previous
time. Measured over seven days: 200 blocklist adds across 54 episodes, worst
episode 13, top 8 episodes = 81 of the 200. lib/regrab_ledger.py adds the
missing memory, keyed on the *arr's own (instance, series, episodes) /
(instance, movie) identity. At ARR_REGRAB_MAX_ADDS (3) adds inside
ARR_REGRAB_WINDOW_HOURS (24) the item is PARKED: unmonitored, then deleted +
blocklisted, then reported once. It is unparked automatically when a later
sweep sees the episode/movie with a file or monitored again.

RULING 1 — skipRedownload is NOT the mechanism and is NOT relied upon.
The obvious-looking fix is a skipRedownload=true query parameter on the DELETE.
That is NOT what stops the loop here, and no code or comment in this file may
claim it is.
Evidence, recorded live by scripts/ops/remediate-2026-08-20-iso.py (lines
142-152, 1176-1221): this box runs config/downloadclient.autoRedownloadFailed
= true, and a replacement grab was observed SIXTEEN SECONDS after the failure
event (history 1218 downloadFailed 07:33:00Z -> 1219 grabbed 07:33:16Z), with
that script recording verbatim "Radarr auto-queued a replacement despite
skipRedownload=true". A query parameter that may or may not be honoured is not
a guard. An UNMONITORED episode/movie, by contrast, cannot be auto-searched by
autoRedownloadFailed, by RSS, or by MissingEpisodeSearch — so the park is an
unmonitor WRITE, issued BEFORE the destructive DELETE and gated on its return
code. A failed unmonitor means no delete and no blocklist at all that sweep.
This file therefore never sends skipRedownload.

RTFM EVIDENCE for the three facts RULING 1 depends on
-----------------------------------------------------
PROVENANCE WARNING, stated plainly rather than dressed up: the three readings
below are taken from artifacts INSIDE THIS REPO that record live reads, not
from a probe run while writing this change. This work was done in an isolated
worktree with no ~/secrets and no tunnel, and a fan-out of concurrent SSH
logins trips Ultra.cc fail2ban (2026-09-15). Each item names what would
confirm it live, read-only, before promotion.

(a) Queue episode identity — Sonarr v3 `GET /api/v3/queue`.
    The queue resource carries `seriesId` and a SINGULAR `episodeId`; a season
    pack appears as several rows sharing one `downloadId`, which is why
    _episode_ids_for() unions the identity across every row with that hash
    rather than trusting one row. Source in-repo: scripts/mcp/collect.py:256
    and :400 read the same queue resource, and every *arr write path in this
    repo that unmonitors uses the LIST form on the write side
    (`PUT /episode/monitor {"episodeIds":[...]}`, qflix-reaper.py:1241,
    quality_fallback.py:452, specials_policy.py:202) — read singular, write
    plural. The reader here deliberately accepts `episodeId`, `episodeIds` and
    `episodes[].id` so that a v4 resource shape cannot make it silently
    unkeyable. CONFIRM LIVE WITH:
      GET {arr}/api/v3/queue?pageSize=1  → inspect the identity field names.
(b) config/downloadclient.autoRedownloadFailed — per instance.
    scripts/configure/90b-usenet-all-arrs.py FORCES this to True on all four
    instances (sonarr, sonarr2, radarr, radarr2) via
    `GET /api/v3/config/downloadclient` then `PUT
    /api/v3/config/downloadclient/{id}`; it is a step of the usenet buildout
    that shipped 2026-06-22, is idempotent and is re-run on install, so the
    live value is True everywhere unless someone turned it off by hand. The
    2026-08-20 Radarr reading above is the corroborating live observation.
    CONFIRM LIVE WITH:
      GET {arr}/api/v3/config/downloadclient  → autoRedownloadFailed per arr.
(c) statusMessages wording for the executable rejection.
    POISON_PHRASES holds the single fragment "executable file with extension",
    matched case-insensitively and only ever as a SUBSTRING of a flattened
    statusMessages string — so a reworded prefix/suffix cannot break it, and a
    wholesale rewording degrades to today's 6h behaviour rather than to a
    wrong action (poison is a strict subset of completed-not-imported: see
    _classify_stuck). CONFIRM LIVE WITH, read-only:
      GET {arr}/api/v3/queue?pageSize=500 | statusMessages[].messages[]
    on a queue that currently holds such a rejection, and extend
    POISON_PHRASES only with wording read from that output.

Caps and knobs added by the guard (all env-tunable):
  ARR_REGRAB_MAX_ADDS               3    adds that trigger a park
  ARR_REGRAB_WINDOW_HOURS           24   the rolling window they count in
  ARR_REGRAB_MAX_PARKS_PER_RUN      5    unmonitor writes per sweep
  ARR_REGRAB_MAX_PARK_READS_PER_RUN 25   park-clearance GETs per sweep
  ARR_REGRAB_LEDGER_MAX_KEYS        500  hard ceiling on the ledger
  ARR_REGRAB_RETAIN_HOURS           168  idle-key retention
The ledger FAILS OPEN: unreadable, truncated, wrong-shaped or unwritable, the
sweep does exactly what it did before the guard existed. The only thing a bad
ledger can do is decline to park.

Reads creds from ~/secrets/{arr}.key + ~/secrets/{arr}.urlbase + the shared
htpasswd password. Posts a Discord summary via lib.notify on completion.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# Public host comes from secrets/seedbox.host (gitignored) — die loudly
# rather than silently hitting the sanitized placeholder if it's missing.
# Override via ARR_HOST env for tests.
def _resolve_host() -> str:
    env = os.environ.get("ARR_HOST")
    if env:
        return env
    try:
        fqdn = Path(os.environ.get("MANITOBA_SECRETS",
                                   str(Path.home() / "secrets"))).joinpath(
            "seedbox.host").read_text(encoding="utf-8").strip()
        return f"https://{fqdn}" if fqdn else ""
    except FileNotFoundError:
        return ""

HOST = _resolve_host()
SECRETS_DIR = Path(os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets")))
STATE_DIR = Path(os.environ.get("MANITOBA_STATE_DIR", str(Path.home() / ".opt" / "maint")))
STUCK_STATE_FILE = STATE_DIR / "stuck-queue-state.json"

STUCK_IMPORT_STATES = {"importPending", "importBlocked", "importFailed"}

# Modes returned by _classify_stuck. Each mode has its own grace-period
# threshold (see THRESHOLD_HOURS_BY_MODE below) and shows up in
# state file + Discord notification body.
MODE_IMPORT = "completed-not-imported"
MODE_PEERS = "stalled-no-peers"
MODE_METADATA = "metadata-stuck"
MODE_CLUSTER = "slow-cluster"
# A strict subset of MODE_IMPORT — see _classify_stuck and INV-1.
MODE_POISON = "poison-executable-payload"
# NOT a _classify_stuck return value: the label the ledger/notification use
# for an item the re-grab guard has unmonitored.
MODE_PARKED = "regrab-loop-parked"

# Extend ONLY with wording read from a live statusMessages payload (see the
# RTFM section of the module docstring). A phrase nobody has seen in the wild
# is a phrase that silently never matches.
POISON_PHRASES = frozenset({"executable file with extension"})
POISON_EXTENSIONS = frozenset({
    ".exe", ".bat", ".scr", ".lnk", ".cmd", ".com", ".msi",
    ".ps1", ".vbs", ".js", ".jar",
})

CLUSTER_MIN_ITEMS = 3
CLUSTER_ETA_DAYS = float(os.environ.get("ARR_STUCK_DAYS_CLUSTER_ETA", "30"))
CLUSTER_NOPROGRESS_DAYS = float(os.environ.get("ARR_STUCK_DAYS_CLUSTER_NOPROGRESS", "7"))

# Per-mode grace periods (hours). Set ARR_STUCK_HOURS for one-knob backward
# compat: if set, it overrides ARR_STUCK_HOURS_IMPORT only (the historical
# meaning of the var). Other modes use their own env vars.
_LEGACY_HOURS = os.environ.get("ARR_STUCK_HOURS")
THRESHOLD_HOURS_BY_MODE = {
    MODE_IMPORT:   float(os.environ.get("ARR_STUCK_HOURS_IMPORT",   _LEGACY_HOURS or "6")),
    MODE_PEERS:    float(os.environ.get("ARR_STUCK_HOURS_PEERS",    "4")),
    MODE_METADATA: float(os.environ.get("ARR_STUCK_HOURS_METADATA", "6")),
    # Cluster mode has its own time semantics — the threshold is implicit
    # in the 7-day-no-progress predicate, not a separate hours grace.
    # Setting this to 0 means "trigger immediately once predicate matches".
    MODE_CLUSTER:  0.0,
    # 0.0 BY CONSTRUCTION, not by tuning: the importer has already made a
    # terminal decision about this payload. Waiting 6h cannot change a
    # rejected executable into an importable episode, and the grace period
    # exists to avoid acting on items that might still resolve themselves.
    MODE_POISON:   0.0,
}

# (slug, api version, missing-search command name)
# Readarr removed 2026-05-16 — app purged 2026-05-11; secret_read on its
# .key file dies, hidden by _read()'s try/except (returns "") so the loop
# silently skipped Readarr anyway. Drop the entry for honesty.
ARRS = [
    ("sonarr",   "v3", "MissingEpisodeSearch"),
    ("sonarr2",  "v3", "MissingEpisodeSearch"),
    ("radarr",   "v3", "MissingMoviesSearch"),
    ("radarr2",  "v3", "MissingMoviesSearch"),
]

VER_BY_SLUG = {slug: ver for slug, ver, _ in ARRS}

# Re-grab loop guard memory. Imported exactly the way _notify() imports
# lib.notify so it resolves from a repo checkout AND from ~/scripts/maint on
# the box. An import failure is not fatal: _regrab stays None and every guard
# call site degrades to the pre-guard sweep (INV-6).
try:  # pragma: no cover - exercised by the absence test, not the happy path
    _HERE = Path(__file__).resolve().parent
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    from lib import regrab_ledger as _regrab  # type: ignore
except Exception as _regrab_exc:  # pragma: no cover
    print(f"regrab_ledger unavailable ({_regrab_exc}) — re-grab guard disabled",
          file=sys.stderr)
    _regrab = None  # type: ignore


def _ledger_path() -> Path:
    """Resolved at CALL time against the module-level STATE_DIR.

    STATE_DIR is bound at import, which is correct for this script: in
    production the env is set before the process starts, and the unit tests
    redirect the ledger by monkeypatching STATE_DIR itself. Reading the env
    var here instead would silently ignore that patch.

    The import-time hazard that DID bite (2026-09-17: fixture rows written
    into the developer's real ~/.opt/maint) lives in lib/regrab_ledger.py,
    whose module-level default path was captured before conftest's autouse
    fixture could set MANITOBA_STATE_DIR. That module resolves lazily now."""
    return STATE_DIR / "arr-regrab-ledger.json"


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


# Read lazily inside _basic() rather than at import time so the absence of
# secrets/htpasswd.password is reported by the first request (with a clear
# 401), not silently propagated as an empty Basic header that every *arr
# then 401s on while the script reports the requests as "skip".
def _htpw() -> str:
    pw = _read(SECRETS_DIR / "htpasswd.password")
    if not pw:
        raise RuntimeError(
            "secrets/htpasswd.password missing or empty — refusing to "
            "issue unauthenticated *arr requests"
        )
    return pw


def _basic() -> str:
    return "Basic " + base64.b64encode(f"quadstronaut:{_htpw()}".encode()).decode()


def _hdr(api_key: str, *, json_body: bool = False) -> dict:
    h = {"X-Api-Key": api_key, "Authorization": _basic(), "Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _req(method: str, url: str, api_key: str, body: dict | None = None,
         timeout: int = 30) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers=_hdr(api_key, json_body=body is not None))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:600]
    except Exception as e:
        return 0, str(e)[:300]


def _arr_url(slug: str, ver: str, path: str, query: str = "") -> str:
    urlbase = _read(SECRETS_DIR / f"{slug}.urlbase") or slug
    qs = f"?{query}" if query else ""
    return f"{HOST}/{urlbase}/api/{ver}/{path}{qs}"


def _arr_key(slug: str) -> str:
    return _read(SECRETS_DIR / f"{slug}.key")


def _notify(msg: str, level: str = "info") -> None:
    """Discord notification via lib.notify (Notifiarr was retired 2026-05-10).
    Best-effort; never raise. Adds the operator @ping for warning/error levels."""
    try:
        # Resolve the import path so this works both from a repo checkout
        # (scripts/maint/lib) and a seedbox deploy (~/scripts/maint/lib).
        here = Path(__file__).resolve().parent
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        from lib.notify import notify  # type: ignore
        notify(msg, level)
    except Exception as exc:
        print(f"notify failed (non-fatal): {exc}", file=sys.stderr)


# ----- mode: --missing ----------------------------------------------------

def cmd_missing(dry_run: bool) -> int:
    """Delegates to scripts/mcp/missing.py to keep one source of truth."""
    if dry_run:
        print("--- find-missing sweep (DRY-RUN, delegated to mcp/missing.py) ---")
        return 0
    here = Path(__file__).resolve().parent
    mcp = here.parent / "mcp" / "missing.py"
    # Validate the helper exists before subprocess — the prior FileNotFoundError
    # surfaced only as a non-zero returncode in the systemd journal, with no
    # signal that the path layout was the cause.
    if not mcp.is_file():
        print(f"FATAL: missing helper not found at {mcp} — "
              f"layout drift between scripts/maint and scripts/mcp",
              file=sys.stderr)
        return 2
    proc = subprocess.run(
        ["python3", str(mcp), "--emit-json"],
        capture_output=True, text=True, timeout=120,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
    return proc.returncode


# ----- mode: --unstick ----------------------------------------------------

def _load_state() -> dict:
    if not STUCK_STATE_FILE.exists():
        return {}
    try:
        return json.loads(STUCK_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STUCK_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def _parse_iso(ts: str | None) -> float | None:
    """Parse Sonarr-style ISO8601 (e.g. '2026-08-13T11:38:13Z') → epoch.
    Returns None on any parse failure rather than raising — bad timestamps
    just mean 'don't classify this as cluster-stuck'."""
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return None


def _cluster_no_progress(samples: list[dict], window_days: float) -> bool:
    """True iff the oldest sample is at least `window_days` old AND every
    sample shows the same sizeleft. Empty/single-sample histories → False
    (not enough data to conclude no-progress)."""
    if len(samples) < 2:
        return False
    now = time.time()
    oldest_ts = min(s.get("ts", now) for s in samples)
    if now - oldest_ts < window_days * 86400:
        # Observation window too short — can't conclude no-progress yet.
        return False
    sizes = {s.get("sizeleft") for s in samples}
    if None in sizes:
        # Treat missing sizeleft as "can't tell" rather than "stable at None".
        return False
    return len(sizes) == 1


def _status_message_texts(item: dict) -> list[str]:
    """Every string the queue row's statusMessages carries, flattened.

    Each entry contributes its `title` and every string in its `messages`.
    Tolerates a bare list of strings, a missing key, None, and non-dict
    entries — this feeds a DESTRUCTIVE decision, so a shape nobody predicted
    must yield "no evidence" rather than a traceback that kills the sweep.
    """
    out: list[str] = []
    try:
        raw = item.get("statusMessages")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        for entry in raw:
            if isinstance(entry, str):
                out.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            title = entry.get("title")
            if isinstance(title, str):
                out.append(title)
            msgs = entry.get("messages")
            if isinstance(msgs, str):
                msgs = [msgs]
            if isinstance(msgs, (list, tuple)):
                out.extend(m for m in msgs if isinstance(m, str))
    except Exception as exc:
        print(f"  ! statusMessages parse failed ({exc}) — treating as empty",
              file=sys.stderr)
        return []
    return out


def _has_ext_token(haystack: str, ext: str) -> bool:
    """`.js` must not match inside `.json`. Requires the extension token to be
    followed by end-of-string or a non-alphanumeric character."""
    start = 0
    while True:
        idx = haystack.find(ext, start)
        if idx < 0:
            return False
        end = idx + len(ext)
        if end >= len(haystack) or not haystack[end].isalnum():
            return True
        start = idx + 1


def _is_poison_payload(item: dict) -> bool:
    """True iff ONE flattened statusMessages string contains both a poison
    phrase and an executable extension.

    Both tokens, in the SAME string (INV-3): "executable" on its own is a word
    that turns up in benign messages, and a stray `.exe` in a different message
    is not a rejection. The release title, errorMessage, outputPath and any
    filename are deliberately NOT inputs (INV-2) — a release literally named
    `Some.Show.exe.1080p` must not be treated as poison, because the thing
    being detected is the IMPORTER REFUSING the payload, not a string.
    """
    for text in _status_message_texts(item):
        low = text.lower()
        if not any(p in low for p in POISON_PHRASES):
            continue
        if any(_has_ext_token(low, e) for e in POISON_EXTENSIONS):
            return True
    return False


def _classify_stuck(item: dict, by_downloadId: dict[str, list[dict]]) -> str | None:
    """Return the stall mode an item matches, or None if healthy.

    `by_downloadId` is a {downloadId-upper: [records...]} index of the
    full queue, needed by slow-cluster detection only (added in Task 5).
    Pass an empty dict if you don't care about cluster mode.
    """
    if (
        item.get("status") == "completed"
        and item.get("trackedDownloadState") in STUCK_IMPORT_STATES
    ):
        # MODE_POISON is checked first and is a STRICT SUBSET of MODE_IMPORT
        # (INV-1): it can only ever change 6h-grace into 0h-grace for an item
        # this branch already owned. It can never make a previously-healthy
        # item classifiable, so the cost of a false positive is bounded at
        # "acted six hours early", not "destroyed a good download".
        if _is_poison_payload(item):
            return MODE_POISON
        return MODE_IMPORT

    # Pre-completion peer starvation. Common when an indexer lists a
    # release whose tracker is gone or the swarm has fully dispersed.
    err = (item.get("errorMessage") or "").lower()
    if item.get("status") == "warning" and "stalled" in err and "no connections" in err:
        return MODE_PEERS

    # Magnet hash that never resolved to a torrent file. qBit holds it
    # in 'downloading metadata' state indefinitely.
    if item.get("status") == "queued" and "downloading metadata" in err:
        return MODE_METADATA

    # Slow-cluster: ≥CLUSTER_MIN_ITEMS items share this downloadId,
    # ETA pushed past CLUSTER_ETA_DAYS, sizeleft has not decreased over
    # the last CLUSTER_NOPROGRESS_DAYS (history injected by caller as
    # item["_sizeleft_history"]).
    dl = (item.get("downloadId") or "").upper()
    if not dl:
        # Items with no downloadId aren't real cluster members — they're
        # newly-queued items waiting for a hash assignment.
        return None
    cluster = by_downloadId.get(dl, [])
    if len(cluster) >= CLUSTER_MIN_ITEMS:
        eta = _parse_iso(item.get("estimatedCompletionTime"))
        if eta is not None and eta > time.time() + (CLUSTER_ETA_DAYS * 86400):
            history = item.get("_sizeleft_history") or []
            if _cluster_no_progress(history, CLUSTER_NOPROGRESS_DAYS):
                return MODE_CLUSTER
    return None


def _state_key(slug: str, download_id: str) -> str:
    return f"{slug}:{(download_id or 'no-hash').lower()}"


# ----- re-grab loop guard --------------------------------------------------

def _row_episode_ids(row: dict) -> list[int]:
    """Episode ids carried by ONE queue row, in whichever shape it uses.

    Sonarr v3 answers with the singular `episodeId`; the list form and the
    `episodes[]` form are accepted too so a resource-shape change makes the
    guard no WEAKER than it was (see RTFM (a) in the module docstring). An
    unrecognised shape yields [] → UNKEYABLE → the sweep proceeds as today.

    Episode id 0 is KEPT. The type checks below deliberately do not also test
    truthiness: dropping a 0 would silently shrink the identity set, so a row
    really covering {0, 5} would key identically to a row covering {5} and the
    two would co-accumulate strikes toward one park threshold. Contrast the
    SERIES id, where 0 correctly yields UNKEYABLE -- with no series id there is
    no key at all and the guard simply steps aside (INV-7), which is safe,
    whereas a wrong key is a silent mis-park. (Stage-2 boundaries lens,
    2026-09-17; *arr ids start at 1 in practice, so this is defensive.)
    """
    ids: list[int] = []
    single = row.get("episodeId")
    if isinstance(single, int) and not isinstance(single, bool):
        ids.append(single)
    many = row.get("episodeIds")
    if isinstance(many, (list, tuple)):
        ids.extend(e for e in many
                   if isinstance(e, int) and not isinstance(e, bool))
    eps = row.get("episodes")
    if isinstance(eps, (list, tuple)):
        for ep in eps:
            if isinstance(ep, dict):
                eid = ep.get("id")
                if isinstance(eid, int) and not isinstance(eid, bool):
                    ids.append(eid)
    return ids


def _episode_ids_for(item: dict, by_downloadId: dict[str, list[dict]]) -> list[int]:
    """Union of the episode identity across every queue row sharing this
    item's downloadId, falling back to the item alone.

    A season pack is several rows behind one hash. Keying on one row would
    give each episode of the pack its own count, so the pack would need
    MAX_ADDS re-grabs PER EPISODE before anything parked — the guard would
    look armed and do nothing.
    """
    rows = [item]
    dl = (item.get("downloadId") or "").upper()
    if dl:
        rows = by_downloadId.get(dl) or [item]
    ids: list[int] = []
    for row in rows:
        ids.extend(_row_episode_ids(row))
    return sorted(set(ids))


def _guard_key(slug: str, item: dict,
               by_downloadId: dict[str, list[dict]]) -> Optional[str]:
    """Ledger key for this queue row, or None when the row is UNKEYABLE.

    UNKEYABLE is a first-class answer, not an error (INV-7): rows pulled in by
    `includeUnknownSeriesItems=true` have no series to key on, and guessing
    would let one unrelated download borrow another item's strike count.
    """
    if _regrab is None:
        return None
    if slug.startswith("radarr"):
        return _regrab.movie_key(slug, item.get("movieId"))
    return _regrab.episode_key(slug, item.get("seriesId"),
                               _episode_ids_for(item, by_downloadId))


def _park(slug: str, ver: str, key_api: str, item: dict,
          by_downloadId: dict[str, list[dict]], dry_run: bool) -> bool:
    """THE UNMONITOR WRITE. Returns True only on a 2xx from the *arr.

    This — not any query parameter — is what stops the re-grab loop: an
    unmonitored episode/movie is not a candidate for autoRedownloadFailed, for
    RSS, or for MissingEpisodeSearch. It is issued BEFORE the DELETE and its
    return code gates the DELETE (INV-4), because destroying a download and
    then failing to park it is strictly worse than doing nothing: it feeds the
    exact loop this guard exists to end.
    """
    if slug.startswith("radarr"):
        mid = item.get("movieId")
        if not mid:
            return False
        url = _arr_url(slug, ver, "movie/editor")
        body = {"movieIds": [mid], "monitored": False, "moveFiles": False}
    else:
        ids = _episode_ids_for(item, by_downloadId)
        if not ids:
            return False
        url = _arr_url(slug, ver, "episode/monitor")
        body = {"episodeIds": ids, "monitored": False}
    if dry_run:
        print(f"  [dry-run] {slug}: would PUT {url.rsplit('/api/', 1)[-1]} {body}")
        return True
    code, resp = _req("PUT", url, key_api, body=body)
    if code in (200, 202):
        return True
    print(f"  ! {slug}: park (unmonitor) HTTP {code}: {resp[:200]}")
    return False


def _park_cleared(slug: str, ver: str, key_api: str,
                  ledger_key: str) -> Optional[bool]:
    """Has this parked item recovered? True=clear it, False=still parked,
    None=could not read, so leave it alone.

    "Recovered" is `hasFile` (something imported, by hand or by a later grab)
    or `monitored` (the operator re-armed it). A read failure must NEVER clear
    a park: clearing on silence would re-arm the search for a title we already
    know has no good release, which is the loop again with extra steps.
    """
    parts = ledger_key.split("|")
    try:
        if len(parts) == 2:
            path = f"movie/{int(parts[1])}"
        elif len(parts) == 3:
            first = parts[2].split(",")[0]
            path = f"episode/{int(first)}"
        else:
            # A key shape nothing in this file produces. Not a read failure —
            # it is garbage, and garbage that stays parked forever inflates
            # the canary's population. Drop it.
            return True
    except (TypeError, ValueError):
        return True
    code, body = _req("GET", _arr_url(slug, ver, path), key_api)
    if code != 200:
        return None
    try:
        rec = json.loads(body)
    except Exception:
        return None
    if not isinstance(rec, dict):
        return None
    return bool(rec.get("hasFile")) or bool(rec.get("monitored"))


def cmd_unstick(dry_run: bool) -> int:
    """Thin wrapper owning the lock scope; the sweep itself is below.

    The ExitStack lives here so the ledger lock is released on EVERY exit path
    out of the sweep -- early return on contention, an exception mid-sweep, or
    the normal end -- without threading a try/finally through a 200-line body.
    """
    with contextlib.ExitStack() as stack:
        return _cmd_unstick_locked(dry_run, stack)


def _cmd_unstick_locked(dry_run: bool, _lock_stack: "contextlib.ExitStack") -> int:
    print(f"--- unstick-queue sweep ({'DRY-RUN' if dry_run else 'LIVE'}) ---")
    state = _load_state()
    now = time.time()
    actions: list[str] = []
    new_state: dict = {}

    max_per_run  = int(os.environ.get("ARR_MAX_ACTIONS_PER_RUN",  "10"))
    max_per_slug = int(os.environ.get("ARR_MAX_ACTIONS_PER_SLUG", "5"))
    cap_hit = False
    actions_total = 0
    actions_by_slug: dict[str, int] = {}

    # ---- re-grab loop guard: load the memory --------------------------
    # Every failure here lands on the same branch: no ledger, no guard, and a
    # sweep that behaves exactly as it did before the guard existed (INV-6).
    ledger: dict = {}
    guard_on = False
    if _regrab is not None:
        # The lock spans read -> mutate -> write, which is the whole sweep. Two
        # overlapping sweeps (hourly timer + an operator running --unstick by
        # hand) otherwise both read one snapshot and the second write wins
        # outright: the loser's blocklist adds vanish and a cleared `notified`
        # flag re-pages a park that was already announced. Locking only the
        # write cannot close that -- the race lives in the minutes-wide gap.
        # Contention means we SKIP, not wait: the losing sweep would act on a
        # stale snapshot and issue duplicate DELETE+blocklist calls. The next
        # hourly run picks the work up.
        _held = _lock_stack.enter_context(_regrab.run_lock())
        if not _held:
            print("another --unstick sweep holds the ledger lock — "
                  "skipping this sweep (the next hourly run retries)")
            return 0
        try:
            ledger = _regrab.read(_ledger_path())
            guard_on = True
        except Exception as exc:  # read() already fails open; belt and braces
            print(f"  ! regrab ledger read raised ({exc}) — guard disabled this run",
                  file=sys.stderr)
            ledger, guard_on = {}, False

    parks_this_run = 0
    deferred_parks = 0
    park_failures = 0
    unkeyable = 0
    parked_now: list[tuple[str, str, int]] = []
    cleared = 0
    still_parked = 0
    park_reads = 0

    # ---- park clearance, BEFORE anything destructive ------------------
    # An item that has a file again, or that the operator re-monitored, is not
    # in a loop any more. Clearing first also means a recovered item can be
    # re-parked later and is news again (INV-9). Bounded by MAX_PARK_READS so a
    # large parked population cannot turn an hourly sweep into a crawl.
    if guard_on:
        for lkey in _regrab.parked_keys(ledger):
            if park_reads >= _regrab.MAX_PARK_READS:
                break
            lslug = _regrab.slug_of(lkey)
            lver = VER_BY_SLUG.get(lslug)
            lkeyapi = _arr_key(lslug) if lver else ""
            if not lver or not lkeyapi:
                continue
            park_reads += 1
            verdict = _park_cleared(lslug, lver, lkeyapi, lkey)
            if verdict is True:
                _regrab.clear(ledger, lkey)
                cleared += 1
                print(f"  ~ {lslug}: park cleared for {lkey} "
                      f"(has a file or is monitored again)")
            else:
                still_parked += 1
                if verdict is False:
                    # Still genuinely parked: keep it fresh so prune()'s age
                    # rule only ever reaps parks whose item has vanished.
                    _regrab.touch(ledger, lkey, now)

    for slug, ver, _ in ARRS:
        actions_by_slug[slug] = 0
        key = _arr_key(slug)
        if not key:
            continue
        url = _arr_url(slug, ver, "queue", query="pageSize=500&includeUnknownSeriesItems=true")
        code, body = _req("GET", url, key)
        if code != 200:
            print(f"  ! {slug}: GET queue HTTP {code}")
            continue
        try:
            payload = json.loads(body)
        except Exception:
            print(f"  ! {slug}: queue body parse fail")
            continue
        records = payload.get("records") if isinstance(payload, dict) else payload
        if not records:
            continue

        by_downloadId: dict[str, list[dict]] = {}
        for r in records:
            dl = (r.get("downloadId") or "").upper()
            by_downloadId.setdefault(dl, []).append(r)

        # Oldest-stuck first, so a per-run cap or the park budget spends itself
        # on the items that have waited longest rather than on whatever the
        # *arr happened to return first (the defer-oldest-N precedent,
        # aab9e87). Items never seen before sort last. The sort is stable, so
        # the order among equals is exactly what it was.
        def _first_seen(rec: dict, _slug: str = slug) -> float:
            prior = state.get(_state_key(_slug, rec.get("downloadId", "")), {}) or {}
            try:
                return float(prior.get("first_seen_stuck") or float("inf"))
            except (TypeError, ValueError):
                return float("inf")
        records = sorted(records, key=_first_seen)

        for item in records:
            sk = _state_key(slug, item.get("downloadId", ""))
            prev = state.get(sk, {}) or {}

            # Maintain a rolling sizeleft history (used by slow-cluster).
            # Trim entries older than CLUSTER_NOPROGRESS_DAYS+1 then cap
            # at 14 entries so the file stays bounded.
            prior_hist = prev.get("sizeleft_history") or []
            history_cutoff = now - ((CLUSTER_NOPROGRESS_DAYS + 1) * 86400)
            trimmed = [s for s in prior_hist if s.get("ts", 0) >= history_cutoff]
            trimmed.append({"ts": now, "sizeleft": item.get("sizeleft", 0)})
            # Cap retained samples generously enough to keep a full window
            # of hourly samples (CLUSTER_NOPROGRESS_DAYS + 1) + small headroom.
            # On the default 7d window this evaluates to 194 entries.
            history_cap = max(14, int((CLUSTER_NOPROGRESS_DAYS + 1) * 24) + 2)
            item["_sizeleft_history"] = trimmed[-history_cap:]

            mode = _classify_stuck(item, by_downloadId)
            if mode is None:
                continue

            qid = item.get("id")
            title = (item.get("title") or "?")[:80]

            first_sight = not prev.get("first_seen_stuck")
            if first_sight:
                # First time seeing this stuck item — record + carry forward.
                new_state[sk] = {
                    "title": title,
                    "queue_id": qid,
                    "first_seen_stuck": now,
                    "slug": slug,
                    "mode": mode,
                    "sizeleft_history": item["_sizeleft_history"],
                }
                # MODE_POISON alone bypasses the first-sight carry-forward: its
                # threshold is 0h BY CONSTRUCTION, so making it wait a whole
                # extra sweep would be the grace period it explicitly does not
                # have. Every other mode, MODE_CLUSTER included, is untouched.
                if mode != MODE_POISON:
                    continue
                prev = new_state[sk]

            first_seen = float(prev.get("first_seen_stuck", now))
            age_hours = (now - first_seen) / 3600
            mode_cutoff = now - (THRESHOLD_HOURS_BY_MODE[mode] * 3600)
            if not (first_sight and mode == MODE_POISON) and first_seen >= mode_cutoff:
                # Still stuck but hasn't aged out under THIS mode's grace — carry forward.
                prev["sizeleft_history"] = item["_sizeleft_history"]
                prev["mode"] = mode
                new_state[sk] = prev
                continue

            # Aged out: remove from client + blocklist. The *arr searches for a
            # replacement afterwards, which is the right answer for a one-off
            # bad release — and is exactly the loop the guard below interrupts
            # for a title that has no good release at all.
            if actions_total >= max_per_run or actions_by_slug[slug] >= max_per_slug:
                cap_hit = True
                print(f"  [cap-hit] {slug}: would-delete id={qid} mode={mode} — "
                      f"skipped (per-run={actions_total}/{max_per_run}, "
                      f"per-slug[{slug}]={actions_by_slug[slug]}/{max_per_slug})")
                prev["sizeleft_history"] = item["_sizeleft_history"]
                prev["mode"] = mode
                new_state[sk] = prev  # keep tracking so we retry next cycle
                continue

            # ---- the re-grab loop guard --------------------------------
            k = _guard_key(slug, item, by_downloadId) if guard_on else None
            if guard_on and k is None:
                unkeyable += 1
            if k is not None and _regrab.should_park(ledger, k, now):
                if parks_this_run >= _regrab.MAX_PARKS_RUN:
                    # DEFER, never abort (aab9e87 defer-oldest-N). Deferring
                    # the park defers the DELETE with it: blocklisting WITHOUT
                    # parking feeds the exact loop this branch exists to stop.
                    deferred_parks += 1
                    print(f"  [park-deferred] {slug}: {title} — park budget "
                          f"{_regrab.MAX_PARKS_RUN} exhausted, retrying next sweep")
                    prev["sizeleft_history"] = item["_sizeleft_history"]
                    prev["mode"] = mode
                    new_state[sk] = prev
                    continue
                # UNMONITOR FIRST, and proceed only on a 2xx (INV-4).
                if not _park(slug, ver, key, item, by_downloadId, dry_run):
                    park_failures += 1
                    print(f"  ! {slug}: park FAILED for {title} — no DELETE, "
                          f"no blocklist this sweep; will retry")
                    prev["sizeleft_history"] = item["_sizeleft_history"]
                    prev["mode"] = mode
                    new_state[sk] = prev
                    continue
                parks_this_run += 1
                adds = _regrab.adds_in_window(ledger, k, now)
                if dry_run:
                    print(f"  [dry-run] {slug}: would park {k} after {adds} "
                          f"blocklist add(s) ({MODE_PARKED})")
                else:
                    _regrab.mark_parked(ledger, k, now)
                    parked_now.append((k, title, adds))

            del_url = _arr_url(
                slug, ver, f"queue/{qid}",
                query="removeFromClient=true&blocklist=true",
            )
            if dry_run:
                actions_total += 1
                actions_by_slug[slug] += 1
                msg = f"  [dry-run] {slug}: would unstick (id={qid}, age={age_hours:.1f}h, mode={mode}) -> {title}"
                print(msg)
                actions.append(f"DRY {slug}: {title} ({age_hours:.1f}h, {mode})")
                # Carry-forward in dry-run too so the second pass doesn't double-count
                new_state[sk] = prev
                continue

            dcode, dbody = _req("DELETE", del_url, key)
            if dcode in (200, 204):
                actions_total += 1
                actions_by_slug[slug] += 1
                msg = f"  ✓ {slug}: unstuck id={qid} age={age_hours:.1f}h mode={mode} — {title}"
                print(msg)
                if mode == MODE_POISON:
                    # Name the mode so the operator reads WHY, not just WHAT.
                    actions.append(f"{slug}: {title} ({MODE_POISON}) → blocklisted+removed")
                else:
                    actions.append(f"{slug}: {title} ({age_hours:.1f}h, {mode}) → blocklisted+research")
                if k is not None:
                    # Count the add ONLY here — a blocklist that actually
                    # happened. INV-11: this counts what THIS script did, never
                    # what /api/v3/blocklist holds.
                    _regrab.record_blocklist_add(ledger, k, title, now)
                # Don't carry-forward — once removed, this hash is gone.
                new_state.pop(sk, None)
            else:
                print(f"  ! {slug}: DELETE id={qid} HTTP {dcode}: {dbody[:200]}")
                # Keep in state so we'll retry next run.
                new_state[sk] = prev

    _save_state(new_state)

    # ---- persist the guard memory -------------------------------------
    # DRY-RUN NEVER WRITES THE LEDGER, deliberately, and pinned by a test. The
    # `parked` flag is a record of an unmonitor that HAPPENED; stamping one in
    # dry-run would make the next LIVE run skip the write it never performed —
    # a destructive delete with no park behind it.
    if guard_on and not dry_run:
        _regrab.prune(ledger, now)
        _regrab.write(ledger, _ledger_path())

    print(
        f"\nstuck items still tracked (carrying forward): {len(new_state)}, "
        f"actions taken: {len(actions)}"
    )
    if guard_on:
        # unkeyable is printed even at zero: an item the guard cannot key is
        # an item the guard does not protect, and that population must be
        # visible rather than inferred from its absence (INV-7).
        print(
            f"regrab guard: parked={parks_this_run} deferred_parks={deferred_parks} "
            f"park_failures={park_failures} cleared={cleared} "
            f"still_parked={still_parked} unkeyable={unkeyable} "
            f"ledger_keys={len(ledger)}"
        )
    if actions or cap_hit:
        body = "arr-unstick swept:\n" + "\n".join(actions) if actions else "arr-unstick: cap hit with zero successful actions"
        if cap_hit:
            body += f"\n⚠ cap hit (run≥{max_per_run} or slug≥{max_per_slug}) — systemic issue likely"
        if dry_run:
            # INV-13: --dry-run is what an operator runs by hand to LOOK. It
            # used to post the "DRY ..." summary to Discord anyway, which made
            # every inspection indistinguishable from a real sweep in the one
            # channel the operator reads. Print it instead.
            print("[dry-run] would notify (warning):\n" + body)
        else:
            _notify(body, level="error" if cap_hit else "warning")

    # ---- park notification, exactly once per park (INV-9) --------------
    # Vocabulary matches quality_fallback.py:579 on purpose: the operator has
    # already learned what "parked ... unmonitored, manual intervention
    # needed" means, and a second dialect for the same state is a second thing
    # to learn.
    if guard_on and parked_now and not dry_run:
        groups = (
            ("TV", [p for p in parked_now
                    if not _regrab.is_movie_key(p[0])
                    and _regrab.should_notify_park(ledger, p[0])]),
            ("Movie", [p for p in parked_now
                       if _regrab.is_movie_key(p[0])
                       and _regrab.should_notify_park(ledger, p[0])]),
        )
        notified_any = False
        for label, group in groups:
            if not group:
                continue
            lines = [f"- {t} — {n} blocklist add(s) in {_regrab.WINDOW_HOURS:g}h"
                     for _, t, n in group]
            _notify(f"{label} parked (re-grab loop — unmonitored, manual "
                    "intervention needed):\n" + "\n".join(lines), "warning")
            for gk, _, _ in group:
                _regrab.mark_notified(ledger, gk)
            notified_any = True
        if notified_any:
            # Re-persist so the once-only flag survives a crash between the
            # Discord post and the next sweep. A duplicate page is the failure
            # this flag exists to prevent.
            _regrab.write(ledger, _ledger_path())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--missing", action="store_true",
                   help="trigger MissingSearch command on each *arr")
    g.add_argument("--unstick", action="store_true",
                   help="DELETE+blocklist queue items stuck past their "
                        "per-mode grace (see module docstring for modes)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show planned actions without executing")
    args = ap.parse_args()

    if args.missing:
        return cmd_missing(args.dry_run)
    if args.unstick:
        return cmd_unstick(args.dry_run)
    return 1


if __name__ == "__main__":
    sys.exit(main())
