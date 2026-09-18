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
"""
from __future__ import annotations

import argparse
import base64
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

# ---------------------------------------------------------------------------
# Cross-run page dedup (council round 2, 2026-09-17). The re-grab/re-stick
# loop re-titles the SAME stuck item on nearly every attempt (measured: one
# Sonarr episode under up to eleven distinct release titles), so a title-hash
# dedup key mutates on every re-grab and reproduces the exact storm this
# closes. _page_key below uses the stable seriesId/movieId identity instead.
# UNSTICK_LOG makes manifest/jobs.yaml's existing (false, until now) claim
# that this job "Writes ~/.opt/maint/arr-unstick.log" true.
# ---------------------------------------------------------------------------
UNSTICK_PAGE_LEDGER = STATE_DIR / "arr-unstick-pages.json"
UNSTICK_LOG = STATE_DIR / "arr-unstick.log"
UNSTICK_PAGE_COOLDOWN_S = float(os.environ.get("ARR_UNSTICK_PAGE_COOLDOWN_S", str(24 * 3600)))
UNSTICK_PAGE_LABEL = "arr-housekeeping: unstick page cooldown"

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


def _load_page_dedup():
    """Lazy import of lib.page_dedup, mirroring _notify()'s sys.path-insert
    pattern so this works from a repo checkout (scripts/maint/lib) and a
    seedbox deploy (~/scripts/maint/lib) alike. Raises on failure — callers
    decide how to fail open; a deploy-time import error must degrade to
    noise, never to silence and never to a crashed sweep."""
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    from lib import page_dedup  # type: ignore
    return page_dedup


def _sanitize(value, max_len: int = 200) -> str:
    """Sanitize one field for the durable audit log / Discord body via
    page_dedup.sanitize_log_field. Falls back to a minimal CR/LF/TAB
    stripper (still never raises, still never lets a hostile *arr release
    title forge a second log row) if the import itself fails."""
    try:
        return _load_page_dedup().sanitize_log_field(value, max_len=max_len)
    except Exception as exc:
        print(f"page_dedup import failed for sanitization (non-fatal, "
              f"degraded): {exc}", file=sys.stderr)
        s = "" if value is None else str(value)
        s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        return s[:max_len]


def _page_key(slug: str, item: dict) -> str:
    """Stable page-dedup identity for a stuck queue item. `is not None`,
    NEVER truthiness (D3): seriesId==0 / movieId==0 are valid ids. bool is
    explicitly excluded — isinstance(True, int) is True in Python, and a
    malformed `seriesId: true` is not id 1. Id-less rows fold into ONE
    bounded per-slug key so they can never storm (no title-hash fallback —
    DELETED, see module docstring: one Sonarr episode was measured under up
    to eleven distinct release titles for the SAME re-grab)."""
    sid = item.get("seriesId")
    if sid is not None and isinstance(sid, int) and not isinstance(sid, bool):
        return f"unstick:{slug}:series:{sid}"
    mid = item.get("movieId")
    if mid is not None and isinstance(mid, int) and not isinstance(mid, bool):
        return f"unstick:{slug}:movie:{mid}"
    return f"unstick:unknown-items:{slug}"


def _audit_log(decision: str, *, slug: str = "", mode: str = "", key: str = "",
                age_h="", queue_id="", title: str = "") -> None:
    """Append exactly ONE physical line to UNSTICK_LOG: tab-delimited, 8
    fields, trailing newline. EVERY field passes through _sanitize() first —
    not just title — because the *arr queue `title` is attacker-chosen
    (whoever uploads a release to a public indexer, authenticated nowhere),
    and this log is the only record of a suppressed run. Best-effort: never
    raises, never aborts the sweep."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    fields = [ts, decision, slug, mode, key, age_h, queue_id, title]
    line = "\t".join(_sanitize(f) for f in fields) + "\n"
    try:
        UNSTICK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with UNSTICK_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        print(f"arr-unstick.log write failed (non-fatal): {exc}", file=sys.stderr)


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


def cmd_unstick(dry_run: bool) -> int:
    print(f"--- unstick-queue sweep ({'DRY-RUN' if dry_run else 'LIVE'}) ---")
    state = _load_state()
    now = time.time()
    # Per-item action RECORDS (not strings — council round 2). Each record:
    # {"key": page-dedup key, "line": rendered Discord line, "slug", "mode",
    #  "age_h", "queue_id", "title"}. Grouped by `key` after the sweep so the
    # SAME stuck item re-titled across runs (measured: up to eleven distinct
    # release titles for one Sonarr episode) pages at most once per window.
    actions: list[dict] = []
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
            pkey = _page_key(slug, item)

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
                _audit_log("cap-hit", slug=slug, mode=mode, key=pkey,
                           age_h=f"{age_hours:.1f}", queue_id=qid, title=title)
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
                safe_title = _sanitize(title, max_len=80)
                line = f"DRY {slug}: {safe_title} ({age_hours:.1f}h, {mode})"
                actions.append({"key": pkey, "line": line, "slug": slug, "mode": mode,
                                 "age_h": age_hours, "queue_id": qid, "title": title})
                _audit_log("dry-run", slug=slug, mode=mode, key=pkey,
                           age_h=f"{age_hours:.1f}", queue_id=qid, title=title)
                # Carry-forward in dry-run too so the second pass doesn't double-count
                new_state[sk] = prev
                continue

            dcode, dbody = _req("DELETE", del_url, key)
            if dcode in (200, 204):
                actions_total += 1
                actions_by_slug[slug] += 1
                msg = f"  ✓ {slug}: unstuck id={qid} age={age_hours:.1f}h mode={mode} — {title}"
                print(msg)
                safe_title = _sanitize(title, max_len=80)
                line = f"{slug}: {safe_title} ({age_hours:.1f}h, {mode}) → blocklisted+research"
                actions.append({"key": pkey, "line": line, "slug": slug, "mode": mode,
                                 "age_h": age_hours, "queue_id": qid, "title": title})
                _audit_log("acted", slug=slug, mode=mode, key=pkey,
                           age_h=f"{age_hours:.1f}", queue_id=qid, title=title)
                # Don't carry-forward — once removed, this hash is gone.
            else:
                print(f"  ! {slug}: DELETE id={qid} HTTP {dcode}: {dbody[:200]}")
                _audit_log("delete-failed", slug=slug, mode=mode, key=pkey,
                           age_h=f"{age_hours:.1f}", queue_id=qid, title=title)
                # Keep in state so we'll retry next run.
                new_state[sk] = prev

    _save_state(new_state)

    print(
        f"\nstuck items still tracked (carrying forward): {len(new_state)}, "
        f"actions taken: {len(actions)}"
    )

    # ---- notification assembly: cross-run page dedup ----------------------
    # Suppression is a NOTIFICATION POLICY ONLY — every action and every
    # suppression decision above is already durably logged via _audit_log,
    # independent of what happens below. A run whose entire Discord output
    # was suppressed is fully reconstructible from ~/.opt/maint/arr-unstick.log.
    try:
        page_dedup = _load_page_dedup()
    except Exception as exc:
        page_dedup = None
        print(f"page_dedup import failed — paging without dedup (non-fatal, "
              f"fail-open): {exc}", file=sys.stderr)

    def _page_due(pkey: str) -> bool:
        if page_dedup is None:
            return True  # fail open: import failure pages exactly as before
        return page_dedup.page_due(
            pkey, ledger_path=UNSTICK_PAGE_LEDGER,
            cooldown_s=UNSTICK_PAGE_COOLDOWN_S, label=UNSTICK_PAGE_LABEL,
        )

    # Group by page-dedup key, preserving first-seen order. NOT the same as
    # the state-tracking key (_state_key, keyed by downloadId hash, which
    # rotates on every re-grab — the exact storm this closes).
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for rec in actions:
        if rec["key"] not in groups:
            groups[rec["key"]] = []
            order.append(rec["key"])
        groups[rec["key"]].append(rec)

    body_lines: list[str] = []
    suppressed_count = 0
    for pkey in order:
        recs = groups[pkey]
        slug0 = recs[0]["slug"]
        mode0 = recs[0]["mode"]
        if pkey.startswith("unstick:unknown-items:"):
            # Id-less rows fold into ONE bounded line — never per-row. No
            # per-item key is ever minted for an id-less row, so this group
            # can only ever page once per window no matter how many rows.
            example_titles = [_sanitize(r["title"], max_len=80) for r in recs[:3]]
            rendered = (f"{slug0}: {len(recs)} id-less queue row(s) swept "
                        f"(no seriesId/movieId) — e.g. " + "; ".join(example_titles))
        else:
            rendered = "\n".join(r["line"] for r in recs)

        # The audit-log title here is a bounded COUNT, not the rendered text
        # itself: the underlying title(s) already have their own physical
        # "acted"/"dry-run" audit line each (with this same `key`, so an
        # operator can grep-correlate). Re-embedding the raw title a second
        # time here would let a hostile title's content land on MORE than
        # one physical log line — the exact thing D4 forbids.
        if _page_due(pkey):
            body_lines.append(rendered)
            _audit_log("paged", slug=slug0, mode=mode0, key=pkey,
                       title=f"{len(recs)} record(s) paged")
        else:
            suppressed_count += 1
            _audit_log("page-suppressed", slug=slug0, mode=mode0, key=pkey,
                       title=f"{len(recs)} record(s) suppressed")

    # Cap-hit carve-out: its OWN key, its OWN window, fires even when every
    # per-item key above was suppressed. Never folded into a per-item digest.
    cap_due = False
    if cap_hit:
        cap_due = _page_due("unstick:cap-hit")
        _audit_log("paged" if cap_due else "page-suppressed",
                   key="unstick:cap-hit",
                   title=f"cap hit this run (run>={max_per_run} or slug>={max_per_slug})")

    notify_lines: list[str] = []
    if cap_hit and cap_due:
        cap_line = f"⚠ cap hit (run≥{max_per_run} or slug≥{max_per_slug}) — systemic issue likely"
        if suppressed_count:
            cap_line += (f" ({suppressed_count} per-item line(s) suppressed by "
                         f"the {int(UNSTICK_PAGE_COOLDOWN_S)}s page dedup)")
        notify_lines.append(cap_line)
    notify_lines.extend(body_lines)

    if notify_lines:
        # error iff the cap block is present in THIS message — the string
        # "⚠ cap hit" never appears in a per-item-only digest. The cap block
        # leads the body with nothing ahead of it (visually distinct); the
        # per-item-only case keeps a friendly header.
        cap_present = cap_hit and cap_due
        level = "error" if cap_present else "warning"
        if cap_present:
            body = "\n".join(notify_lines)
        else:
            body = "arr-unstick swept:\n" + "\n".join(notify_lines)
        _notify(body, level=level)
    # else: nothing due this run — _notify is not called at all. Every
    # action and every suppression is still in the durable log.

    _audit_log("sweep-summary", title=(
        f"actions_total={actions_total} cap_hit={cap_hit} cap_due={cap_due} "
        f"tracked={len(new_state)} keys_due={len(body_lines)} "
        f"keys_suppressed={suppressed_count}"))

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
