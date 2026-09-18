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

PAGING POLICY (2026-09-17): the sweep runs hourly and a re-grab loop gives it
something to do every hour, so notifying on every action taken produced 12 of
the 13 Discord messages in a measured 24h window for ONE ongoing condition.
Actions are now deduped for NOTIFICATION purposes only, on content identity
(movieId / seriesId+episodeIds, stable across re-grabs) + slug + mode, via
lib/page_ledger.py (ARR_UNSTICK_PAGE_COOLDOWN_S, default 24h). The cap-hit
escalation keeps its own key and its own clock so it can never be muted by
routine chatter. EVERY action and every suppression decision is still written
to ~/.opt/maint/arr-housekeeping.log and to stdout on every run.

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
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import timezone
from pathlib import Path
from typing import Optional

# lib/ lives next to this script both in the repo checkout (scripts/maint/lib)
# and on the seedbox (~/scripts/maint/lib). Resolve it the same way _notify
# does, but at import time, because the page-dedup ledger is needed by
# cmd_unstick's notification policy.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
try:
    from lib import page_ledger as _page_ledger  # type: ignore
except Exception as _exc:  # pragma: no cover - layout drift only
    # FAIL OPEN, loudly: no ledger means no suppression, i.e. the pre-2026-09-17
    # behaviour. Never a crash, and never silent suppression.
    _page_ledger = None
    sys.stderr.write("arr-housekeeping: page_ledger unavailable, page dedup "
                     "DISABLED (failing open): " + repr(_exc) + "\n")

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
# Page-dedup policy for the hourly sweep
# ---------------------------------------------------------------------------
# MEASURED 2026-09-17: 12 of the 13 Discord messages in a 24h window came from
# THIS function. cmd_unstick notified on every run that took any action, and a
# re-grab loop takes an action every hour, forever, about the same title. The
# one message that carried escalation value - the cap-hit @ping, "systemic
# issue likely" - was buried among eleven look-alike warnings, which is how a
# channel gets muted and how the NEXT real alert goes unread.
#
# The re-grab loop is not the bug here and is not touched: deleting a stuck
# grab hourly is what this job is FOR. The bug is the PAGING POLICY. So the
# sweep keeps its cadence and the page gets its own clock, in the repo's one
# page-dedup mechanism (lib/page_ledger.py, the code extracted from
# recovery.py's 2026-09-02 escalation cooldown).
UNSTICK_PAGE_LEDGER = "arr-unstick-pages.json"

# Per-item cooldown. A day: an ongoing stuck title is ONE line in the channel,
# and a title still stuck tomorrow is re-surfaced rather than forgotten.
ARR_UNSTICK_PAGE_COOLDOWN_S = float(
    os.environ.get("ARR_UNSTICK_PAGE_COOLDOWN_S", str(24 * 3600)))
# Cap-hit gets its OWN key and its OWN clock, so a run full of muted per-item
# keys can never mute the systemic signal.
ARR_UNSTICK_CAP_PAGE_COOLDOWN_S = float(
    os.environ.get("ARR_UNSTICK_CAP_PAGE_COOLDOWN_S", str(24 * 3600)))

CAP_PAGE_KEY = "unstick:cap-hit"
# Fixed first line so the escalation is glanceable in a channel of sweeps.
CAP_BANNER = "⚠ SYSTEMIC — arr-unstick CAP HIT"

UNSTICK_LOG_FILE = "arr-housekeeping.log"
_UNSTICK_LOG_MAX_LINES = 5000

# A trailing release-group token: "-NTb", "-RARBG", "[GROUP]". Cosmetic
# variation between re-grabs of the same release must not mint a new key.
_RELEASE_GROUP_TAIL = re.compile(r"(?:[-\[]\s*[A-Za-z0-9._]{2,20}\]?)\s*$")


def _env_float(name: str, default: float) -> float:
    """Read at CALL time, not import time - the hourly unit can be re-tuned by
    a drop-in without a redeploy, and tests set these with monkeypatch."""
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _page_ledger_path() -> Path:
    # STATE_DIR is read through the module global on purpose: tests monkeypatch
    # it, and a drop-in can move the state dir.
    return STATE_DIR / UNSTICK_PAGE_LEDGER


def _normalize_title(title: str) -> str:
    """Casefold, collapse whitespace, drop a trailing release-group token."""
    t = " ".join((title or "").split()).casefold()
    t = _RELEASE_GROUP_TAIL.sub("", t).strip()
    return t


def _content_key(slug: str, item: dict) -> str:
    """Stable identity of WHAT was swept, ACROSS re-grabs.

    NOT downloadId. A re-grab loop obtains a new downloadId/infohash on every
    grab - that is definitionally what the loop does - so a downloadId-keyed
    ledger would mint a fresh key every hour and dedup NOTHING, reproducing
    the exact storm being fixed. movieId / seriesId+episodeIds come straight
    off the *arr queue record and are stable across re-grabs of the same
    content, while still being distinct for DIFFERENT content, so a genuinely
    new stuck title pages on the very next hourly run.

    The title hash is the fallback for queue rows that carry neither id - the
    includeUnknownSeriesItems=true rows. It is normalized so release-name
    cosmetics do not defeat it.
    """
    movie_id = item.get("movieId")
    if movie_id:
        return f"{slug}:m{movie_id}"
    series_id = item.get("seriesId")
    if series_id:
        eps = item.get("episodeIds")
        if not eps:
            single = item.get("episodeId")
            eps = [single] if single else []
        joined = "+".join(sorted(str(e) for e in eps))
        return f"{slug}:s{series_id}e{joined}"
    digest = hashlib.sha1(
        _normalize_title(item.get("title") or "").encode("utf-8")).hexdigest()
    return f"{slug}:t{digest[:12]}"


def _page_key(slug: str, item: dict, mode: str) -> str:
    """Content identity + slug + mode.

    `mode` is in the key because a title that moves from stalled-no-peers to
    slow-cluster is a materially different fault about the same content, and
    that transition is worth exactly one page.
    """
    return f"unstick:{_content_key(slug, item)}:{mode}"


def _append_unstick_log(lines: list[str], suppressed: list[str]) -> None:
    """Durable, NEVER-suppressed record of what the sweep did and what it
    decided not to say.

    Suppression is a NOTIFICATION policy, not a logging policy: the operator
    must always be able to reconstruct a quiet run. Tab-delimited, rotation
    capped, best-effort - exactly like lib/notify.py's _append_audit_log.
    """
    if not lines and not suppressed:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = STATE_DIR / UNSTICK_LOG_FILE
        now = datetime.datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rows = [f"{now}\taction\t{ln}\n" for ln in lines]
        rows += [f"{now}\tdecision\t{sp}\n" for sp in suppressed]
        with path.open("a", encoding="utf-8") as fh:
            fh.writelines(rows)
        if (hash(now) & 0xFF) == 0:
            try:
                existing = path.read_text(encoding="utf-8").splitlines(keepends=True)
                if len(existing) > _UNSTICK_LOG_MAX_LINES:
                    path.write_text("".join(existing[-_UNSTICK_LOG_MAX_LINES:]),
                                    encoding="utf-8")
            except Exception as exc:
                sys.stderr.write("arr-housekeeping: log rotation failed "
                                 "(best-effort, continuing): " + repr(exc) + "\n")
    except Exception as exc:
        print(f"WARNING: could not write {UNSTICK_LOG_FILE}: {exc}", file=sys.stderr)


def _partition_due(keys: list[str], cooldown_s: float) -> tuple[list[str], list[str]]:
    """partition_due with the no-ledger case folded in. Fails open both ways."""
    if _page_ledger is None:
        uniq, seen = [], set()
        for k in keys:
            if k not in seen:
                seen.add(k)
                uniq.append(k)
        return uniq, []
    return _page_ledger.partition_due(_page_ledger_path(), keys, cooldown_s)


def cmd_unstick(dry_run: bool) -> int:
    print(f"--- unstick-queue sweep ({'DRY-RUN' if dry_run else 'LIVE'}) ---")
    state = _load_state()
    now = time.time()
    # (page_key, action_line) pairs, not bare strings: the page key decides
    # whether this line reaches Discord; the line is logged either way.
    actions: list[tuple[str, str]] = []
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
                actions.append((_page_key(slug, item, mode),
                                f"DRY {slug}: {title} ({age_hours:.1f}h, {mode})"))
                # Carry-forward in dry-run too so the second pass doesn't double-count
                new_state[sk] = prev
                continue

            dcode, dbody = _req("DELETE", del_url, key)
            if dcode in (200, 204):
                actions_total += 1
                actions_by_slug[slug] += 1
                msg = f"  ✓ {slug}: unstuck id={qid} age={age_hours:.1f}h mode={mode} — {title}"
                print(msg)
                actions.append((
                    _page_key(slug, item, mode),
                    f"{slug}: {title} ({age_hours:.1f}h, {mode})"
                    f" → blocklisted+research"))
                # Don't carry-forward — once removed, this hash is gone.
            else:
                print(f"  ! {slug}: DELETE id={qid} HTTP {dcode}: {dbody[:200]}")
                # Keep in state so we'll retry next run.
                new_state[sk] = prev

    _save_state(new_state)

    print(
        f"\nstuck items still tracked (carrying forward): {len(new_state)}, "
        f"actions taken: {len(actions)}"
    )
    # -----------------------------------------------------------------
    # NOTIFICATION POLICY (see the page-dedup block above for the measured
    # storm this exists to stop). Three rules, in this order:
    #   1. LOG EVERYTHING, ALWAYS. Suppression is a notification policy.
    #   2. Routine sweep lines are deduped per content+mode, 24h.
    #   3. The cap-hit escalation has its own key and its own clock, so a
    #      run of entirely-muted items can never mute the systemic signal.
    # Every step is wrapped: a bug in the suppressor must never take down
    # the sweep or swallow a page.
    # -----------------------------------------------------------------
    item_cooldown = _env_float("ARR_UNSTICK_PAGE_COOLDOWN_S",
                               ARR_UNSTICK_PAGE_COOLDOWN_S)
    cap_cooldown = _env_float("ARR_UNSTICK_CAP_PAGE_COOLDOWN_S",
                              ARR_UNSTICK_CAP_PAGE_COOLDOWN_S)

    # Keep the ledger bounded: content keys are minted per title, so without a
    # prune the file is an append-only list of everything ever swept. Prune at
    # the widest cooldown in play so nothing still-live is dropped.
    if _page_ledger is not None:
        pruned = _page_ledger.prune(_page_ledger_path(),
                                    max(item_cooldown, cap_cooldown))
        if pruned:
            print(f"  [page-ledger] pruned {pruned} expired stamp(s)")

    keys = [k for k, _ in actions]
    try:
        due_keys, muted_keys = _partition_due(keys, item_cooldown)
    except Exception as exc:
        # Belt and braces: _partition_due already fails open internally.
        print(f"  ! page-ledger partition failed, paging everything: {exc}",
              file=sys.stderr)
        due_keys, muted_keys = keys, []
    due_set = set(due_keys)
    due_lines = [line for k, line in actions if k in due_set]

    # Durable trail first, unconditionally - including on runs that say nothing.
    _append_unstick_log(
        [f"{k}\t{line}" for k, line in actions],
        [f"{k}\tdue" for k in due_keys] + [f"{k}\tmuted" for k in muted_keys],
    )
    for k in due_keys:
        print(f"  [page-ledger] due   {k}")
    for k in muted_keys:
        print(f"  [page-ledger] muted {k}  (already paged within "
              f"{item_cooldown:.0f}s; see {UNSTICK_LOG_FILE})")

    # Cap-hit: own key, own cooldown, per-OUTAGE (a clean run clears it).
    cap_due = False
    if cap_hit:
        if _page_ledger is None:
            cap_due = True
        else:
            cap_due = _page_ledger.page_due(_page_ledger_path(), CAP_PAGE_KEY,
                                            cap_cooldown)
    elif actions and _page_ledger is not None:
        # Actions taken and the cap NOT hit: the systemic condition is over,
        # so the next cap-hit is news again.
        _page_ledger.clear_page(_page_ledger_path(), CAP_PAGE_KEY)

    if due_lines:
        body = "arr-unstick swept:\n" + "\n".join(due_lines)
        if muted_keys:
            body += (f"\n(+{len(muted_keys)} ongoing condition(s) already paged "
                     f"in the last 24h — full detail in "
                     f"~/.opt/maint/{UNSTICK_LOG_FILE})")
        if cap_hit and not cap_due:
            # The escalation is muted but STILL ACTIVE - never let that be
            # invisible while a routine message is going out anyway.
            body += (f"\ncap still hit (run≥{max_per_run} or "
                     f"slug≥{max_per_slug}) — escalation already paged")
        _notify(body, level="warning")

    if cap_due:
        # SEPARATE message, level=error, so lib/notify.py adds the operator
        # @ping and the banner is the first thing read. Never concatenated
        # onto the sweep body - that is how the 2026-09-17 escalation got
        # buried among eleven look-alike warnings in the first place.
        detail = (f"{len(actions)} action(s) completed this run"
                  if actions else "cap hit with zero successful actions")
        _notify(
            CAP_BANNER
            + f"\n{detail}"
            + f"\ncap: run≥{max_per_run} or slug≥{max_per_slug}"
              f" — systemic issue likely"
            + "\nper-slug: "
            + ", ".join(f"{k}={v}" for k, v in sorted(actions_by_slug.items())),
            level="error")
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
