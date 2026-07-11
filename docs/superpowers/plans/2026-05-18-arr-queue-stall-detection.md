# Arr Queue Stall Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `scripts/maint/arr-housekeeping.py --unstick` with four queue-stall detection modes so Sonarr/Radarr permanently blocklist the failure types seen on 2026-05-17/18 (stalled-no-peers, metadata-stuck, slow-cluster) in addition to the existing completed-not-imported behaviour.

**Architecture:** Single-file edit. Replace boolean `_is_stuck(item)` with classifier `_classify_stuck(item, by_downloadId) → mode | None`. Per-mode env-tunable thresholds. Bounded action caps (per-run + per-slug) to prevent runaway. Backward-compatible state-file schema additions (`mode`, `sizeleft_history`). One unit-test file driven by three JSON fixtures.

**Tech Stack:** Python 3 stdlib only (urllib, json, time, argparse), pytest, existing `lib.notify` for Discord. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-18-arr-queue-stall-detection.md`

---

## File Map

| Path | Action | Responsibility |
|---|---|---|
| `scripts/maint/arr-housekeeping.py` | modify | Replace `_is_stuck` with `_classify_stuck`; per-mode thresholds; caps; mode-tagged actions |
| `tests/fixtures/arr-queue/peers.json` | create | One stalled-no-peers queue item |
| `tests/fixtures/arr-queue/metadata.json` | create | One metadata-stuck queue item |
| `tests/fixtures/arr-queue/cluster.json` | create | Three queue items sharing one downloadId, 87-day ETA, identical sizeleft over time |
| `tests/fixtures/arr-queue/import.json` | create | One completed-not-imported item (regression coverage for legacy predicate) |
| `tests/unit/test_arr_housekeeping.py` | create | Unit tests against `_classify_stuck` + cap logic |

---

## Task 1: Fixtures + test scaffold

**Files:**
- Create: `tests/fixtures/arr-queue/peers.json`
- Create: `tests/fixtures/arr-queue/metadata.json`
- Create: `tests/fixtures/arr-queue/cluster.json`
- Create: `tests/fixtures/arr-queue/import.json`
- Create: `tests/unit/test_arr_housekeeping.py`

- [ ] **Step 1: Create `peers.json`**

```json
{
  "records": [
    {
      "id": 478413373,
      "title": "www.Torrenting.com - Blue.Mountain.State.S03E08.1080p.WEB.x264-STRiFE",
      "status": "warning",
      "trackedDownloadStatus": "ok",
      "trackedDownloadState": "downloading",
      "errorMessage": "The download is stalled with no connections",
      "downloadId": "A8100391C16FB6A525685D375CE928F19DF1B7B4",
      "size": 1324778490,
      "sizeleft": 1324778490
    }
  ]
}
```

- [ ] **Step 2: Create `metadata.json`**

```json
{
  "records": [
    {
      "id": 326839586,
      "title": "45502c242d89346414cd96f06b97e96da38b09ea",
      "status": "queued",
      "trackedDownloadStatus": "ok",
      "trackedDownloadState": "downloading",
      "errorMessage": "qBittorrent is downloading metadata",
      "downloadId": "45502C242D89346414CD96F06B97E96DA38B09EA",
      "size": 0,
      "sizeleft": 0
    }
  ]
}
```

- [ ] **Step 3: Create `cluster.json`**

```json
{
  "records": [
    {
      "id": 1867372303,
      "title": "NYPD.Blue.S01.1080p.HULU.WEBRip.AAC2.0.x264-AJP69",
      "status": "downloading",
      "trackedDownloadStatus": "ok",
      "trackedDownloadState": "downloading",
      "errorMessage": null,
      "downloadId": "F3CCE7C6141E01ECDED47488E89F2CC5142FED8E",
      "estimatedCompletionTime": "2026-08-13T11:38:13Z",
      "size": 47160373523,
      "sizeleft": 29952491520
    },
    {
      "id": 295571643,
      "title": "NYPD.Blue.S01.1080p.HULU.WEBRip.AAC2.0.x264-AJP69",
      "status": "downloading",
      "trackedDownloadStatus": "ok",
      "trackedDownloadState": "downloading",
      "errorMessage": null,
      "downloadId": "F3CCE7C6141E01ECDED47488E89F2CC5142FED8E",
      "estimatedCompletionTime": "2026-08-13T11:38:13Z",
      "size": 47160373523,
      "sizeleft": 29952491520
    },
    {
      "id": 56165182,
      "title": "NYPD.Blue.S01.1080p.HULU.WEBRip.AAC2.0.x264-AJP69",
      "status": "downloading",
      "trackedDownloadStatus": "ok",
      "trackedDownloadState": "downloading",
      "errorMessage": null,
      "downloadId": "F3CCE7C6141E01ECDED47488E89F2CC5142FED8E",
      "estimatedCompletionTime": "2026-08-13T11:38:13Z",
      "size": 47160373523,
      "sizeleft": 29952491520
    }
  ]
}
```

- [ ] **Step 4: Create `import.json`**

```json
{
  "records": [
    {
      "id": 999111222,
      "title": "Some.Show.S01E01.1080p.WEB-DL",
      "status": "completed",
      "trackedDownloadStatus": "warning",
      "trackedDownloadState": "importPending",
      "errorMessage": null,
      "downloadId": "AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555",
      "size": 1000000000,
      "sizeleft": 0
    }
  ]
}
```

- [ ] **Step 5: Create `tests/unit/test_arr_housekeeping.py` scaffold**

```python
"""Tests for arr-housekeeping classify-stuck logic."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# Load arr-housekeeping.py as a module (it's a script, not a package)
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "maint"))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "arr_housekeeping",
    ROOT / "scripts" / "maint" / "arr-housekeeping.py",
)
arrhk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arrhk)


FIXTURES = ROOT / "tests" / "fixtures" / "arr-queue"


def _load(name: str) -> list[dict]:
    """Return the records list from a fixture."""
    data = json.loads((FIXTURES / name).read_text())
    return data["records"]


def _by_downloadId(records: list[dict]) -> dict[str, list[dict]]:
    """Group queue records by downloadId (uppercased)."""
    out: dict[str, list[dict]] = {}
    for r in records:
        dl = (r.get("downloadId") or "").upper()
        out.setdefault(dl, []).append(r)
    return out
```

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/arr-queue/ tests/unit/test_arr_housekeeping.py
git commit -m "test(arr-housekeeping): add stuck-queue fixtures + test scaffold"
```

---

## Task 2: `_classify_stuck` skeleton + completed-not-imported mode

**Files:**
- Modify: `scripts/maint/arr-housekeeping.py:183-188` (replace `_is_stuck`)
- Modify: `scripts/maint/arr-housekeeping.py:221-223` (update caller in `cmd_unstick`)
- Modify: `tests/unit/test_arr_housekeeping.py`

- [ ] **Step 1: Write failing test for legacy predicate preserved**

Append to `tests/unit/test_arr_housekeeping.py`:

```python
def test_classify_completed_not_imported_legacy_predicate_preserved():
    records = _load("import.json")
    by_dl = _by_downloadId(records)
    mode = arrhk._classify_stuck(records[0], by_dl)
    assert mode == "completed-not-imported"


def test_classify_returns_none_for_healthy_item():
    healthy = {"status": "downloading", "trackedDownloadState": "downloading"}
    assert arrhk._classify_stuck(healthy, {}) is None
```

- [ ] **Step 2: Run failing tests**

```bash
cd G:/Documents/GIT/Ultra.cc/QFlix
pytest tests/unit/test_arr_housekeeping.py -v
```

Expected: `AttributeError: module 'arr_housekeeping' has no attribute '_classify_stuck'`

- [ ] **Step 3: Replace `_is_stuck` with `_classify_stuck` (completed-not-imported branch only)**

Replace the function block in `scripts/maint/arr-housekeeping.py` (originally lines 183-188):

```python
# Modes returned by _classify_stuck. Each mode has its own grace-period
# threshold (see THRESHOLD_HOURS_BY_MODE) and shows up in state file +
# Discord notification body.
MODE_IMPORT = "completed-not-imported"
MODE_PEERS = "stalled-no-peers"
MODE_METADATA = "metadata-stuck"
MODE_CLUSTER = "slow-cluster"

STUCK_IMPORT_STATES = {"importPending", "importBlocked", "importFailed"}


def _classify_stuck(item: dict, by_downloadId: dict[str, list[dict]]) -> str | None:
    """Return the stall mode an item matches, or None if healthy.

    `by_downloadId` is a {downloadId-upper: [records...]} index of the
    full queue, needed by slow-cluster detection only. Pass an empty
    dict if you don't care about cluster mode.
    """
    # Existing predicate: completed but the *arr couldn't import the
    # downloaded file (post-DL failure). Sonarr's blocklist makes it
    # search for a replacement release.
    if (
        item.get("status") == "completed"
        and item.get("trackedDownloadState") in STUCK_IMPORT_STATES
    ):
        return MODE_IMPORT
    return None
```

- [ ] **Step 4: Update caller in `cmd_unstick`**

In `scripts/maint/arr-housekeeping.py` find the loop that calls `_is_stuck`:

```python
        for item in records:
            if not _is_stuck(item):
                continue
            sk = _state_key(slug, item.get("downloadId", ""))
```

Replace with:

```python
        by_downloadId: dict[str, list[dict]] = {}
        for r in records:
            dl = (r.get("downloadId") or "").upper()
            by_downloadId.setdefault(dl, []).append(r)

        for item in records:
            mode = _classify_stuck(item, by_downloadId)
            if mode is None:
                continue
            sk = _state_key(slug, item.get("downloadId", ""))
```

- [ ] **Step 5: Persist `mode` in state record**

In the same function, update the `new_state[sk] = {…}` block (the "First time seeing this stuck item" branch). Add `"mode": mode,` to the dict.

- [ ] **Step 6: Run tests**

```bash
pytest tests/unit/test_arr_housekeeping.py -v
```

Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add scripts/maint/arr-housekeeping.py tests/unit/test_arr_housekeeping.py
git commit -m "refactor(arr-housekeeping): replace _is_stuck with _classify_stuck dispatcher"
```

---

## Task 3: `stalled-no-peers` mode

**Files:**
- Modify: `scripts/maint/arr-housekeeping.py` (add branch to `_classify_stuck`)
- Modify: `tests/unit/test_arr_housekeeping.py`

- [ ] **Step 1: Failing test**

```python
def test_classify_stalled_no_peers():
    records = _load("peers.json")
    mode = arrhk._classify_stuck(records[0], _by_downloadId(records))
    assert mode == "stalled-no-peers"
```

- [ ] **Step 2: Run → FAIL**

```bash
pytest tests/unit/test_arr_housekeeping.py::test_classify_stalled_no_peers -v
```

Expected: assertion mismatch (returns None).

- [ ] **Step 3: Add branch to `_classify_stuck`**

Before `return None`, add:

```python
    # Pre-completion peer starvation. Common when an indexer lists a
    # release whose tracker is gone or the swarm has fully dispersed.
    err = (item.get("errorMessage") or "").lower()
    if item.get("status") == "warning" and "stalled" in err and "no connections" in err:
        return MODE_PEERS
```

- [ ] **Step 4: Run → PASS**

```bash
pytest tests/unit/test_arr_housekeeping.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/maint/arr-housekeeping.py tests/unit/test_arr_housekeeping.py
git commit -m "feat(arr-housekeeping): detect stalled-no-peers queue items"
```

---

## Task 4: `metadata-stuck` mode

**Files:**
- Modify: `scripts/maint/arr-housekeeping.py`
- Modify: `tests/unit/test_arr_housekeeping.py`

- [ ] **Step 1: Failing test**

```python
def test_classify_metadata_stuck():
    records = _load("metadata.json")
    mode = arrhk._classify_stuck(records[0], _by_downloadId(records))
    assert mode == "metadata-stuck"
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Add branch — place after the peers branch, before `return None`**

```python
    # Magnet hash that never resolved to a torrent file. qBit holds it
    # in 'downloading metadata' state indefinitely.
    if item.get("status") == "queued" and "downloading metadata" in err:
        return MODE_METADATA
```

Note: `err` is already defined from Task 3's branch — reuse it.

- [ ] **Step 4: Run → PASS**

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/maint/arr-housekeeping.py tests/unit/test_arr_housekeeping.py
git commit -m "feat(arr-housekeeping): detect metadata-stuck queue items"
```

---

## Task 5: `slow-cluster` mode (most complex)

The cluster detector needs (a) cluster membership ≥3 items, (b) ETA > 30 days from now, (c) sizeleft has not decreased over the last 7 days from state history. Items (a) and (b) are testable from a single fixture frame; (c) needs the state-history field, which is populated by the caller before classify-time.

**Files:**
- Modify: `scripts/maint/arr-housekeeping.py`
- Modify: `tests/unit/test_arr_housekeeping.py`

- [ ] **Step 1: Failing tests — two scenarios**

```python
import datetime as _dt


def test_classify_slow_cluster_eta_only_not_enough():
    """3-item cluster + far-future ETA but no sizeleft history yet — must NOT
    classify as cluster (we need 7d of stable sizeleft first)."""
    records = _load("cluster.json")
    # No prior sizeleft history attached — caller hasn't seen this hash before
    for r in records:
        r["_sizeleft_history"] = []
    mode = arrhk._classify_stuck(records[0], _by_downloadId(records))
    assert mode is None


def test_classify_slow_cluster_stable_sizeleft_7d():
    """Same cluster, but with 7+ days of identical sizeleft → classify."""
    records = _load("cluster.json")
    now = time.time()
    week_ago = now - (7.5 * 86400)
    for r in records:
        # Caller injects this from state file; classifier reads only.
        r["_sizeleft_history"] = [
            {"ts": week_ago, "sizeleft": r["sizeleft"]},
            {"ts": now, "sizeleft": r["sizeleft"]},
        ]
    mode = arrhk._classify_stuck(records[0], _by_downloadId(records))
    assert mode == "slow-cluster"
```

- [ ] **Step 2: Run → both FAIL (returns None or wrong)**

- [ ] **Step 3: Add classifier branch + helper**

Above `_classify_stuck` add the helper:

```python
import datetime

CLUSTER_MIN_ITEMS = 3
CLUSTER_ETA_DAYS = float(os.environ.get("ARR_STUCK_DAYS_CLUSTER_ETA", "30"))
CLUSTER_NOPROGRESS_DAYS = float(os.environ.get("ARR_STUCK_DAYS_CLUSTER_NOPROGRESS", "7"))


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
    """True iff every sample inside `window_days` shows the same sizeleft
    as the oldest sample. Empty/single-sample histories → False (not enough
    data to conclude no-progress)."""
    if len(samples) < 2:
        return False
    now = time.time()
    cutoff = now - (window_days * 86400)
    in_window = [s for s in samples if s.get("ts", 0) >= cutoff]
    if len(in_window) < 2:
        # Need at least 2 datapoints inside the no-progress window.
        return False
    sizes = {s.get("sizeleft") for s in in_window}
    return len(sizes) == 1
```

Then add the cluster branch to `_classify_stuck`. The full updated function:

```python
def _classify_stuck(item: dict, by_downloadId: dict[str, list[dict]]) -> str | None:
    if (
        item.get("status") == "completed"
        and item.get("trackedDownloadState") in STUCK_IMPORT_STATES
    ):
        return MODE_IMPORT

    err = (item.get("errorMessage") or "").lower()
    if item.get("status") == "warning" and "stalled" in err and "no connections" in err:
        return MODE_PEERS
    if item.get("status") == "queued" and "downloading metadata" in err:
        return MODE_METADATA

    # Slow-cluster: ≥CLUSTER_MIN_ITEMS items share this downloadId,
    # ETA pushed past CLUSTER_ETA_DAYS, sizeleft has not decreased over
    # the last CLUSTER_NOPROGRESS_DAYS (history injected by caller as
    # item["_sizeleft_history"]).
    dl = (item.get("downloadId") or "").upper()
    cluster = by_downloadId.get(dl, [])
    if len(cluster) >= CLUSTER_MIN_ITEMS:
        eta = _parse_iso(item.get("estimatedCompletionTime"))
        if eta is not None and eta > time.time() + (CLUSTER_ETA_DAYS * 86400):
            history = item.get("_sizeleft_history") or []
            if _cluster_no_progress(history, CLUSTER_NOPROGRESS_DAYS):
                return MODE_CLUSTER
    return None
```

- [ ] **Step 4: Run → both PASS**

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/maint/arr-housekeeping.py tests/unit/test_arr_housekeeping.py
git commit -m "feat(arr-housekeeping): detect slow-cluster (multi-item, far-ETA, no-progress)"
```

---

## Task 6: Wire sizeleft history into `cmd_unstick` + per-mode thresholds

The classifier now expects `item["_sizeleft_history"]` to be pre-populated by the caller. The caller has to:
1. Read existing `sizeleft_history` from state for this hash.
2. Append the current `sizeleft` sample.
3. Trim entries older than `CLUSTER_NOPROGRESS_DAYS + 1` (no point keeping older).
4. Attach to item before calling `_classify_stuck`.
5. Save back to state.

Per-mode thresholds: use a single dict lookup so adding a 5th mode later is trivial.

**Files:**
- Modify: `scripts/maint/arr-housekeeping.py`
- Modify: `tests/unit/test_arr_housekeeping.py`

- [ ] **Step 1: Add threshold table near the top (after the other env vars at line ~54)**

Replace the existing single-threshold line:

```python
STUCK_HOURS = float(os.environ.get("ARR_STUCK_HOURS", "6"))
```

with:

```python
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
```

This requires the MODE_* constants from Task 2 to be defined first — they already are.

- [ ] **Step 2: Modify the cmd_unstick loop to inject + persist sizeleft history**

Replace the current loop body in `cmd_unstick`:

```python
        for item in records:
            mode = _classify_stuck(item, by_downloadId)
            if mode is None:
                continue
            sk = _state_key(slug, item.get("downloadId", ""))
```

with this version (which injects history before classifying so cluster mode works):

```python
        for item in records:
            sk = _state_key(slug, item.get("downloadId", ""))
            prev = state.get(sk, {})

            # Maintain a rolling sizeleft history (used by slow-cluster).
            # Cap at 14 entries so file stays bounded.
            prior_hist = prev.get("sizeleft_history") or []
            cutoff = now - ((CLUSTER_NOPROGRESS_DAYS + 1) * 86400)
            trimmed = [s for s in prior_hist if s.get("ts", 0) >= cutoff]
            trimmed.append({"ts": now, "sizeleft": item.get("sizeleft", 0)})
            item["_sizeleft_history"] = trimmed[-14:]

            mode = _classify_stuck(item, by_downloadId)
            if mode is None:
                continue
```

Then in the "first time seeing this stuck item" branch, persist `sizeleft_history`:

```python
            if prev.get("first_seen_stuck") is None:
                new_state[sk] = {
                    "title": title,
                    "queue_id": qid,
                    "first_seen_stuck": now,
                    "slug": slug,
                    "mode": mode,
                    "sizeleft_history": item["_sizeleft_history"],
                }
                continue
```

And update the carry-forward + cutoff blocks to use the per-mode threshold instead of the single `cutoff`:

Replace:

```python
            first_seen = float(prev.get("first_seen_stuck", now))
            age_hours = (now - first_seen) / 3600
            if first_seen >= cutoff:
                # Still stuck but hasn't aged out yet — carry forward.
                new_state[sk] = prev
                continue
```

with:

```python
            first_seen = float(prev.get("first_seen_stuck", now))
            age_hours = (now - first_seen) / 3600
            mode_cutoff = now - (THRESHOLD_HOURS_BY_MODE[mode] * 3600)
            if first_seen >= mode_cutoff:
                # Still stuck but hasn't aged out under THIS mode's grace — carry forward.
                prev["sizeleft_history"] = item["_sizeleft_history"]  # keep history fresh
                prev["mode"] = mode  # mode may shift if predicates compete (rare)
                new_state[sk] = prev
                continue
```

Also remove the now-unused module-level `cutoff = now - (STUCK_HOURS * 3600)` line at the top of `cmd_unstick`.

- [ ] **Step 3: Add an integration-style test that exercises `cmd_unstick` end-to-end against a mock HTTP layer**

Append to `tests/unit/test_arr_housekeeping.py`:

```python
from unittest.mock import patch


def test_cmd_unstick_classifies_and_persists_mode(tmp_path, monkeypatch):
    """End-to-end: one peers-stalled item, no prior state, dry-run.
    Expect a state record created with mode='stalled-no-peers' and no DELETE."""
    state_file = tmp_path / "stuck.json"
    monkeypatch.setattr(arrhk, "STATE_DIR", tmp_path)
    monkeypatch.setattr(arrhk, "STUCK_STATE_FILE", state_file)

    def fake_req(method, url, key, **kw):
        if method == "GET" and "/queue" in url and "sonarr/" in url:
            return 200, json.dumps({"records": _load("peers.json")})
        if method == "GET":
            return 200, json.dumps({"records": []})
        return 500, ""

    monkeypatch.setattr(arrhk, "_req", fake_req)
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "fake-key" if slug == "sonarr" else "")
    monkeypatch.setattr(arrhk, "_notify", lambda *a, **kw: None)

    rc = arrhk.cmd_unstick(dry_run=True)
    assert rc == 0

    state = json.loads(state_file.read_text())
    assert len(state) == 1
    key = next(iter(state))
    assert state[key]["mode"] == "stalled-no-peers"
    assert "sizeleft_history" in state[key]
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/unit/test_arr_housekeeping.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/maint/arr-housekeeping.py tests/unit/test_arr_housekeeping.py
git commit -m "feat(arr-housekeeping): wire per-mode thresholds + sizeleft history into cmd_unstick"
```

---

## Task 7: Action caps (per-run + per-slug) + cap-hit notification escalation

**Files:**
- Modify: `scripts/maint/arr-housekeeping.py` (`cmd_unstick`)
- Modify: `tests/unit/test_arr_housekeeping.py`

- [ ] **Step 1: Failing test for per-slug cap**

```python
def test_cmd_unstick_respects_per_slug_cap(tmp_path, monkeypatch):
    """Six stalled items from sonarr, cap=2 per slug. Only 2 actions issued."""
    state_file = tmp_path / "stuck.json"
    monkeypatch.setattr(arrhk, "STATE_DIR", tmp_path)
    monkeypatch.setattr(arrhk, "STUCK_STATE_FILE", state_file)
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "2")
    monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")  # bypass grace period

    # Pre-populate state so every item is "aged out" already.
    base = _load("peers.json")[0]
    six = []
    for i in range(6):
        item = dict(base)
        item["id"] = 1000 + i
        item["downloadId"] = f"{i:040x}"
        six.append(item)

    fake_state = {
        f"sonarr:{(it['downloadId']).lower()}": {
            "title": it["title"], "queue_id": it["id"],
            "first_seen_stuck": time.time() - 86400,
            "slug": "sonarr", "mode": "stalled-no-peers",
            "sizeleft_history": [],
        }
        for it in six
    }
    state_file.write_text(json.dumps(fake_state))

    deletes: list[str] = []
    def fake_req(method, url, key, **kw):
        if method == "GET" and "sonarr/" in url and "/queue" in url:
            return 200, json.dumps({"records": six})
        if method == "GET":
            return 200, json.dumps({"records": []})
        if method == "DELETE":
            deletes.append(url)
            return 200, ""
        return 500, ""

    monkeypatch.setattr(arrhk, "_req", fake_req)
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")
    monkeypatch.setattr(arrhk, "_notify", lambda *a, **kw: None)

    arrhk.cmd_unstick(dry_run=False)
    assert len(deletes) == 2, f"expected 2 DELETEs (cap=2), got {len(deletes)}"
```

Also re-import time at the top of the test file if it isn't already.

- [ ] **Step 2: Run → FAIL (no cap implemented; would issue 6 DELETEs)**

- [ ] **Step 3: Implement caps in `cmd_unstick`**

Add at the top of `cmd_unstick` (after `state = _load_state()`):

```python
    max_per_run  = int(os.environ.get("ARR_MAX_ACTIONS_PER_RUN",  "10"))
    max_per_slug = int(os.environ.get("ARR_MAX_ACTIONS_PER_SLUG", "5"))
    cap_hit = False
    actions_total = 0
    actions_by_slug: dict[str, int] = {}
```

Inside the per-arr loop, BEFORE the per-item loop, reset the per-slug counter:

```python
        actions_by_slug[slug] = 0
```

Replace the existing DELETE block:

```python
            dcode, dbody = _req("DELETE", del_url, key)
            if dcode in (200, 204):
                msg = f"  ✓ {slug}: unstuck id={qid} age={age_hours:.1f}h — {title}"
                print(msg)
                actions.append(f"{slug}: {title} ({age_hours:.1f}h) → blocklisted+research")
```

with this cap-aware version:

```python
            if actions_total >= max_per_run or actions_by_slug[slug] >= max_per_slug:
                cap_hit = True
                print(f"  [cap-hit] {slug}: would-delete id={qid} mode={mode} — "
                      f"skipped (per-run={actions_total}/{max_per_run}, "
                      f"per-slug[{slug}]={actions_by_slug[slug]}/{max_per_slug})")
                new_state[sk] = prev  # keep tracking so we retry next cycle
                continue

            dcode, dbody = _req("DELETE", del_url, key)
            if dcode in (200, 204):
                actions_total += 1
                actions_by_slug[slug] += 1
                msg = f"  ✓ {slug}: unstuck id={qid} age={age_hours:.1f}h mode={mode} — {title}"
                print(msg)
                actions.append(f"{slug}: {title} ({age_hours:.1f}h, {mode}) → blocklisted+research")
```

And update the bottom-of-function notify call to escalate level on cap-hit:

Replace:

```python
    if actions:
        _notify(
            "arr-unstick swept:\n" + "\n".join(actions),
            level="warning" if len(actions) > 0 else "info",
        )
```

with:

```python
    if actions or cap_hit:
        body = "arr-unstick swept:\n" + "\n".join(actions) if actions else "arr-unstick: cap hit with zero successful actions"
        if cap_hit:
            body += f"\n⚠ cap hit (run≥{max_per_run} or slug≥{max_per_slug}) — systemic issue likely"
        _notify(body, level="error" if cap_hit else "warning")
```

- [ ] **Step 4: Run → PASS**

```bash
pytest tests/unit/test_arr_housekeeping.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/maint/arr-housekeeping.py tests/unit/test_arr_housekeeping.py
git commit -m "feat(arr-housekeeping): add per-run + per-slug action caps with error escalation"
```

---

## Task 8: Docstring update + final verification + PR

**Files:**
- Modify: `scripts/maint/arr-housekeeping.py` (module docstring)

- [ ] **Step 1: Update module docstring (lines 1-19) to reflect new modes**

Replace the existing docstring:

```python
"""arr-housekeeping — daily Find-Missing sweep + hourly stuck-queue unstick.

Two modes:
  --missing   Fire MissingSearch command on each *arr. Sched: 04:00 Tue–Sun
              (Monday is the cp.ultra.cc maintenance window; we skip it).
  --unstick   Scan each *arr's queue for items stuck in completed-but-not-
              imported state (importPending / importBlocked / importFailed)
              for >=STUCK_HOURS. For each, DELETE the queue item with
              removeFromClient=true and blocklist=true — Sonarr/Radarr
              auto-search a replacement after the blocklist add. Sched:
              hourly.

State for stuck-tracking is keyed by qBit downloadId (hash) so it's stable
across queue-id renumberings. Stored at ~/.opt/maint/stuck-queue-state.json.

Reads creds from ~/secrets/{arr}.key + ~/secrets/{arr}.urlbase + the shared
htpasswd password. Posts a Discord summary via lib.notify on completion.
"""
```

with:

```python
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
```

- [ ] **Step 2: Run full test suite one last time**

```bash
pytest tests/unit/test_arr_housekeeping.py -v
```

Expected: all green.

- [ ] **Step 3: Lint (informational only — repo doesn't enforce)**

```bash
python -m py_compile scripts/maint/arr-housekeeping.py
```

Expected: exit 0 (no syntax errors).

- [ ] **Step 4: Commit docstring update**

```bash
git add scripts/maint/arr-housekeeping.py
git commit -m "docs(arr-housekeeping): document stall modes + caps in module docstring"
```

- [ ] **Step 5: Push branch + open PR**

```bash
git push -u origin feat/arr-queue-stall-detection-modes
gh pr create --base master --head feat/arr-queue-stall-detection-modes \
  --title "feat(arr-housekeeping): detect peer/metadata/cluster stalls + action caps" \
  --body "$(cat <<'EOF'
## Summary
Extends `scripts/maint/arr-housekeeping.py --unstick` with three new stall detection modes covering the failure types observed in the 2026-05-17/18 audit:

- **stalled-no-peers** (4h grace): `status=warning` + "stalled with no connections" — the BMS S03E08/S03E09 STRiFE case.
- **metadata-stuck** (6h grace): `status=queued` + "downloading metadata" — the magnet-that-never-resolves case.
- **slow-cluster** (immediate on match): ≥3 queue items sharing one downloadId, ETA > 30d, sizeleft stable over 7d — the NYPD Blue cross-attached 22-row case.

Plus the existing **completed-not-imported** mode (6h grace) is preserved.

Also adds **per-run** and **per-slug** action caps (defaults 10 / 5) to prevent runaway when something systemic breaks. Cap-hit escalates the Discord notification level to `error`.

Spec: `docs/superpowers/specs/2026-05-18-arr-queue-stall-detection.md`.

## Test plan
- [ ] `pytest tests/unit/test_arr_housekeeping.py -v` — 8 tests green.
- [ ] Deploy to manitoba, run `arr-housekeeping.py --unstick --dry-run` once → confirm state baseline written, zero actions.
- [ ] Wait one hourly cycle, run live → confirm Discord notification carries new mode tags.
- [ ] Inspect `~/.opt/maint/stuck-queue-state.json` → confirm `mode` and `sizeleft_history` fields populated.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Confirm PR opened**

```bash
gh pr view --json url,number,state
```

Expected: state=OPEN with a PR URL.

---

## Notes for the implementing engineer

- **Existing code style:** the repo uses `from __future__ import annotations` and `dict[str, list[dict]]` style hints. Keep the style consistent — Python 3.9+ on manitoba.
- **No mocking the urllib layer directly:** the existing `_req()` function is the seam. Mock it via `monkeypatch.setattr(arrhk, "_req", fake_req)` as in the test scaffolding.
- **`fake_req` shape:** returns `(status_code, body_string)`. GET queue endpoints return JSON-encoded `{"records": [...]}`. The real *arrs sometimes return a bare list — the existing code already handles both via `payload.get("records") if isinstance(payload, dict) else payload`.
- **State-file is mutable across test runs:** every cmd_unstick test must `monkeypatch.setattr(arrhk, "STUCK_STATE_FILE", tmp_path / "stuck.json")` to avoid clobbering `~/.opt/maint/stuck-queue-state.json` on a dev machine.
- **Don't bother with `--unstick --dry-run` integration tests beyond what's specified** — the existing module had no such tests, and adding them would balloon scope. The unit-level coverage on `_classify_stuck` + one e2e on `cmd_unstick` per Task 6 is sufficient.

## Self-review summary

- ✅ All 4 modes from spec covered (Tasks 2-5)
- ✅ Per-mode thresholds from spec (Task 6)
- ✅ Action caps from spec (Task 7)
- ✅ Backward-compatible state schema (mode + sizeleft_history optional fields) — handled in Task 2's "first time seeing" branch + Task 6's history injection
- ✅ Notification mode-tagging (Task 7 step 3 includes `({mode})` in action line)
- ✅ Test coverage per spec: peers, metadata, cluster + import preserved + integration smoke
- ✅ Reversal plan from spec: every step is a separate commit so any task can be reverted independently
