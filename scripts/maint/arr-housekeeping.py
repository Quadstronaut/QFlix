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

Reads creds from ~/secrets/{arr}.key + ~/secrets/{arr}.urlbase + the shared
htpasswd password. Posts a Discord summary via lib.notify on completion.

Cross-run PAGE dedup (2026-09-17, Cluster C: alert hygiene): a hostile 24h
measurement showed 12/13 Discord messages from this function alone, hourly,
one per run that took any action, with no dedup — a re-grab loop mints a new
downloadId every time an item is deleted+blocklisted, so a naive "one message
per action" policy pages once per hour forever for one unchanged fault. The
fix reuses lib/page_ledger.py (the same wall-clock-in-a-file cooldown
recovery.py's escalation page already uses) keyed on CONTENT identity
(movieId / seriesId+episodeIds / a normalized-title hash for queue rows
carrying neither) + mode — deliberately NOT downloadId, which changes on
every re-grab by definition and would dedup nothing. See _content_key,
_page_key and the module's Stage-0 spec (Cluster C) for the full rationale.
Suppression is a NOTIFICATION policy only: every action and every
due/muted verdict is still written to arr-housekeeping.log and stdout on
every run, dedup or not.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# Resolve lib.* the same way as other scripts/maint/*.py entry points
# (qflix-audit.py, bootstrap-kuma-monitors.py): works both from a repo
# checkout (scripts/maint/lib) and a seedbox deploy (~/scripts/maint/lib).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import page_ledger  # noqa: E402

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
}

# ---------------------------------------------------------------------------
# Cross-run page dedup (see module docstring). One ledger file, two kinds of
# key sharing it: per-content-item keys ("unstick:<content>:<mode>") and the
# single systemic CAP_PAGE_KEY — independent cooldowns, same JSON file.
# ---------------------------------------------------------------------------
UNSTICK_PAGE_LEDGER = "arr-unstick-pages.json"
ARR_UNSTICK_PAGE_COOLDOWN_S = float(
    os.environ.get("ARR_UNSTICK_PAGE_COOLDOWN_S", str(24 * 3600)))
ARR_UNSTICK_CAP_PAGE_COOLDOWN_S = float(
    os.environ.get("ARR_UNSTICK_CAP_PAGE_COOLDOWN_S", str(24 * 3600)))
CAP_PAGE_KEY = "unstick:cap-hit"
UNSTICK_LOG_FILE = "arr-housekeeping.log"
_UNSTICK_LOG_MAX_LINES = 5000

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


# ---------------------------------------------------------------------------
# Page-dedup key: content identity, not download identity. See module
# docstring — a re-grab always mints a fresh downloadId, so _state_key above
# (which IS keyed on downloadId, and stays that way — it tracks the aging
# clock of one physical download, untouched by this change) is the wrong key
# for "have I already told the operator about this". This is a SEPARATE key.
# ---------------------------------------------------------------------------

_RELEASE_GROUP_SUFFIX_RE = re.compile(r"-[a-z0-9]+$")


def _normalize_title(title: str) -> str:
    """Cosmetic normalization for the title-hash fallback key (queue rows
    with neither movieId nor seriesId+episodeIds — includeUnknownSeriesItems
    rows). Casefold, collapse whitespace, strip a trailing release-group
    token, so a re-grab that lands a different release of the SAME content
    still collapses to the same key instead of minting a fresh one."""
    t = " ".join(title.split()).casefold()
    return _RELEASE_GROUP_SUFFIX_RE.sub("", t)


def _episode_ids(item: dict) -> list:
    """Sonarr queue records expose episode identity as either a plural
    `episodeIds` list (the shape this module's interface is specified
    against) or, on some API responses, a singular `episodeId` int. Normalize
    to a list so _content_key has exactly one shape to hash."""
    ids = item.get("episodeIds")
    if isinstance(ids, list) and ids:
        return list(ids)
    single = item.get("episodeId")
    return [single] if single is not None else []


def _content_key(slug: str, item: dict) -> str:
    """Stable identity of WHAT was swept, across re-grabs.

      movie:   f"{slug}:m{movieId}"
      tv:      f"{slug}:s{seriesId}e{'+'.join(sorted(episodeIds))}"
      neither: f"{slug}:t{sha1(normalized_title)[:12]}"  (unknown-series rows)

    NOT downloadId: a re-grab loop obtains a NEW downloadId/infohash on
    every grab (that is definitionally what the loop does), so a
    downloadId-keyed ledger would mint a fresh key every hour and dedup
    nothing — reproducing the exact storm this module exists to fix.
    movieId / seriesId+episodeIds come straight off the *arr queue record
    and are stable across re-grabs of the same content.
    """
    movie_id = item.get("movieId")
    if movie_id:
        return f"{slug}:m{movie_id}"

    series_id = item.get("seriesId")
    eids = _episode_ids(item)
    if series_id and eids:
        joined = "+".join(str(e) for e in sorted(eids))
        return f"{slug}:s{series_id}e{joined}"

    digest = hashlib.sha1(
        _normalize_title(item.get("title") or "").encode("utf-8")
    ).hexdigest()[:12]
    return f"{slug}:t{digest}"


def _page_key(slug: str, item: dict, mode: str) -> str:
    """`mode` is part of the key: the same content moving from
    stalled-no-peers to slow-cluster is a materially different fault about
    the same content and is worth its own page (AC-4)."""
    return f"unstick:{_content_key(slug, item)}:{mode}"


def _append_unstick_log(records: list) -> None:
    """Append EVERY action + every suppression decision to
    <state_dir>/arr-housekeeping.log, tab-delimited, rotation-capped at 5000
    lines exactly like lib/notify.py's _append_audit_log.

    `records` is a list of (page_key, verdict, action_line) tuples — verdict
    is 'due' or 'muted' — for every stuck item acted on this run. Called on
    EVERY cmd_unstick run, including fully-muted ones: suppression is a
    NOTIFICATION policy only (spec C1b), never a logging policy, so this
    file's fidelity must never depend on what Discord did. Best-effort;
    never raises — a logging hiccup must never abort the sweep."""
    if not records:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_DIR / UNSTICK_LOG_FILE
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        text = "".join(f"{now}\t{verdict}\t{key}\t{line}\n" for key, verdict, line in records)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(text)
        try:
            existing = log_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if len(existing) > _UNSTICK_LOG_MAX_LINES:
                log_path.write_text(
                    "".join(existing[-_UNSTICK_LOG_MAX_LINES:]), encoding="utf-8")
        except Exception as exc:
            print(f"arr-housekeeping: log rotation failed (best-effort): {exc}",
                  file=sys.stderr)
    except Exception as exc:
        print(f"arr-housekeeping: could not write {UNSTICK_LOG_FILE}: {exc}",
              file=sys.stderr)


def cmd_unstick(dry_run: bool) -> int:
    print(f"--- unstick-queue sweep ({'DRY-RUN' if dry_run else 'LIVE'}) ---")
    state = _load_state()
    now = time.time()
    # (page_key, action_line) for every item actually acted on this run
    # (live delete or dry-run "would unstick"). Cap-skipped items are NOT
    # actions — nothing happened to them — and never enter this list.
    action_pairs: list[tuple[str, str]] = []
    new_state: dict = {}

    max_per_run  = int(os.environ.get("ARR_MAX_ACTIONS_PER_RUN",  "10"))
    max_per_slug = int(os.environ.get("ARR_MAX_ACTIONS_PER_SLUG", "5"))
    cap_hit = False
    actions_total = 0
    actions_by_slug: dict[str, int] = {}

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
            pk = _page_key(slug, item, mode)

            if not prev.get("first_seen_stuck"):
                # First time seeing this stuck item — record + carry forward.
                new_state[sk] = {
                    "title": title,
                    "queue_id": qid,
                    "first_seen_stuck": now,
                    "slug": slug,
                    "mode": mode,
                    "sizeleft_history": item["_sizeleft_history"],
                }
                continue

            first_seen = float(prev.get("first_seen_stuck", now))
            age_hours = (now - first_seen) / 3600
            mode_cutoff = now - (THRESHOLD_HOURS_BY_MODE[mode] * 3600)
            if first_seen >= mode_cutoff:
                # Still stuck but hasn't aged out under THIS mode's grace — carry forward.
                prev["sizeleft_history"] = item["_sizeleft_history"]
                prev["mode"] = mode
                new_state[sk] = prev
                continue

            # Aged out: remove from client + blocklist. Sonarr/Radarr's
            # default behavior is to re-search after a blocklist add; we
            # don't pass skipRedownload so the *arr does that for us.
            if actions_total >= max_per_run or actions_by_slug[slug] >= max_per_slug:
                cap_hit = True
                print(f"  [cap-hit] {slug}: would-delete id={qid} mode={mode} — "
                      f"skipped (per-run={actions_total}/{max_per_run}, "
                      f"per-slug[{slug}]={actions_by_slug[slug]}/{max_per_slug})")
                prev["sizeleft_history"] = item["_sizeleft_history"]
                prev["mode"] = mode
                new_state[sk] = prev  # keep tracking so we retry next cycle
                continue

            del_url = _arr_url(
                slug, ver, f"queue/{qid}",
                query="removeFromClient=true&blocklist=true",
            )
            if dry_run:
                actions_total += 1
                actions_by_slug[slug] += 1
                msg = f"  [dry-run] {slug}: would unstick (id={qid}, age={age_hours:.1f}h, mode={mode}) -> {title}"
                print(msg)
                action_pairs.append((pk, f"DRY {slug}: {title} ({age_hours:.1f}h, {mode})"))
                # Carry-forward in dry-run too so the second pass doesn't double-count
                new_state[sk] = prev
                continue

            dcode, dbody = _req("DELETE", del_url, key)
            if dcode in (200, 204):
                actions_total += 1
                actions_by_slug[slug] += 1
                msg = f"  ✓ {slug}: unstuck id={qid} age={age_hours:.1f}h mode={mode} — {title}"
                print(msg)
                action_pairs.append((pk, f"{slug}: {title} ({age_hours:.1f}h, {mode}) → blocklisted+research"))
                # Don't carry-forward — once removed, this hash is gone.
            else:
                print(f"  ! {slug}: DELETE id={qid} HTTP {dcode}: {dbody[:200]}")
                # Keep in state so we'll retry next run.
                new_state[sk] = prev

    _save_state(new_state)

    print(
        f"\nstuck items still tracked (carrying forward): {len(new_state)}, "
        f"actions taken: {len(action_pairs)}"
    )

    # ---- cross-run page dedup (Cluster C, 2026-09-17) ---------------------
    ledger_path = STATE_DIR / UNSTICK_PAGE_LEDGER
    # Runs on EVERY invocation, dedup-hit or not, so the ledger stays bounded
    # (AC-13) regardless of how quiet or noisy the queue has been.
    page_ledger.prune(ledger_path, ARR_UNSTICK_PAGE_COOLDOWN_S, now=now)

    keys = [pk for pk, _ in action_pairs]
    due_keys, muted_keys = page_ledger.partition_due(
        ledger_path, keys, ARR_UNSTICK_PAGE_COOLDOWN_S, now=now)
    due_set = set(due_keys)

    # C1b: suppression is a NOTIFICATION policy only. Every action line and
    # every due/muted verdict is logged + printed on EVERY run, independent
    # of whether anything reaches Discord.
    log_records = [
        (pk, "due" if pk in due_set else "muted", line)
        for pk, line in action_pairs
    ]
    _append_unstick_log(log_records)
    for pk, verdict, line in log_records:
        print(f"  [{verdict}] {pk}: {line}")

    due_lines = [line for pk, line in action_pairs if pk in due_set]

    # C2: the cap escalation lives on its OWN key/cooldown — the per-item
    # ledger above can never mute it, and it is never muted by (or merged
    # into) the routine sweep body.
    cap_due = False
    if cap_hit:
        cap_due = page_ledger.page_due(
            ledger_path, CAP_PAGE_KEY, ARR_UNSTICK_CAP_PAGE_COOLDOWN_S, now=now)
    elif actions_total > 0:
        # A clean run (actions taken, cap not hit) clears the stamp: the
        # cooldown is per-OUTAGE, so the next cap-hit — a NEW systemic
        # episode — pages immediately rather than staying muted from a
        # resolved one.
        page_ledger.clear_page(ledger_path, CAP_PAGE_KEY)

    if due_lines:
        body_lines = ["arr-unstick swept:"] + due_lines
        if cap_hit and not cap_due:
            # The cap is still active but its own page already fired within
            # the cooldown — keep it visible in the routine body rather than
            # letting it go dark (AC-8).
            body_lines.append("cap still hit")
        if muted_keys:
            body_lines.append(
                f"(+{len(muted_keys)} ongoing condition(s) already paged in "
                f"the last 24h — full detail in ~/.opt/maint/arr-housekeeping.log)"
            )
        _notify("\n".join(body_lines), level="warning")

    if cap_hit and cap_due:
        # Separate message, separate level — never concatenated onto the
        # sweep body (AC-6). level="error" is what makes lib/notify.py add
        # the operator @ping.
        cap_lines = ["⚠ SYSTEMIC — arr-unstick CAP HIT"]
        if actions_total == 0:
            cap_lines.append("cap hit with zero successful actions")
        else:
            cap_lines.append(f"actions taken this run: {actions_total}")
        cap_lines.append(
            f"⚠ cap hit (run≥{max_per_run} or slug≥{max_per_slug}) — systemic issue likely"
        )
        cap_lines.append(
            f"per-run={actions_total}/{max_per_run}  per-slug={actions_by_slug}"
        )
        _notify("\n".join(cap_lines), level="error")

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
