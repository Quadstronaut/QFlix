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


def _with_future_eta(records: list[dict], days: float = 45) -> list[dict]:
    """cluster.json's hardcoded ETA (2026-08-13) rotted on 2026-07-15 when
    now+CLUSTER_ETA_DAYS crossed it; inject a now-relative far-future ETA so
    the slow-cluster predicate stays testable regardless of wall-clock date."""
    eta = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))
    for r in records:
        r["estimatedCompletionTime"] = eta
    return records


def _by_downloadId(records: list[dict]) -> dict[str, list[dict]]:
    """Group queue records by downloadId (uppercased)."""
    out: dict[str, list[dict]] = {}
    for r in records:
        dl = (r.get("downloadId") or "").upper()
        out.setdefault(dl, []).append(r)
    return out


def test_classify_completed_not_imported_legacy_predicate_preserved():
    records = _load("import.json")
    by_dl = _by_downloadId(records)
    mode = arrhk._classify_stuck(records[0], by_dl)
    assert mode == "completed-not-imported"


def test_classify_returns_none_for_healthy_item():
    healthy = {"status": "downloading", "trackedDownloadState": "downloading"}
    assert arrhk._classify_stuck(healthy, {}) is None


def test_classify_stalled_no_peers():
    records = _load("peers.json")
    mode = arrhk._classify_stuck(records[0], _by_downloadId(records))
    assert mode == "stalled-no-peers"


def test_classify_metadata_stuck():
    records = _load("metadata.json")
    mode = arrhk._classify_stuck(records[0], _by_downloadId(records))
    assert mode == "metadata-stuck"


def test_classify_slow_cluster_eta_only_not_enough():
    """3-item cluster + far-future ETA but no sizeleft history yet — must NOT
    classify as cluster (we need 7d of stable sizeleft first)."""
    records = _with_future_eta(_load("cluster.json"))
    # No prior sizeleft history attached — caller hasn't seen this hash before
    for r in records:
        r["_sizeleft_history"] = []
    mode = arrhk._classify_stuck(records[0], _by_downloadId(records))
    assert mode is None


def test_classify_slow_cluster_stable_sizeleft_7d():
    """Same cluster, but with 7+ days of identical sizeleft → classify."""
    records = _with_future_eta(_load("cluster.json"))
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


def test_cmd_unstick_classifies_and_persists_mode(tmp_path, monkeypatch):
    """End-to-end: one peers-stalled item, no prior state, dry-run.
    Expect a state record created with mode='stalled-no-peers' and no DELETE."""
    state_file = tmp_path / "stuck.json"
    monkeypatch.setattr(arrhk, "STATE_DIR", tmp_path)
    monkeypatch.setattr(arrhk, "STUCK_STATE_FILE", state_file)

    def fake_req(method, url, key, **kw):
        if method == "GET" and "/queue" in url and "sonarr/" in url and "sonarr2" not in url:
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
    assert len(state) == 1, f"expected exactly one state record, got {state}"
    key = next(iter(state))
    assert state[key]["mode"] == "stalled-no-peers"
    assert "sizeleft_history" in state[key]


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
        item["downloadId"] = f"{i:040x}".upper()
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
        if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
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


def test_cmd_unstick_history_retains_full_no_progress_window(tmp_path, monkeypatch):
    """Regression: the trim-and-cap path in cmd_unstick must retain enough
    history that the oldest sample stays older than CLUSTER_NOPROGRESS_DAYS.
    Pre-fix [-14:] truncated to ~13 hours of samples → cluster never fired."""
    state_file = tmp_path / "stuck.json"
    monkeypatch.setattr(arrhk, "STATE_DIR", tmp_path)
    monkeypatch.setattr(arrhk, "STUCK_STATE_FILE", state_file)

    cluster_records = _with_future_eta(_load("cluster.json"))

    # Pre-populate state with 9 days of hourly samples (216 entries) at the
    # same sizeleft. After cmd_unstick's trim+append, the oldest retained
    # sample must still be ≥ CLUSTER_NOPROGRESS_DAYS old.
    now = time.time()
    nine_days_s = 9 * 86400
    sizeleft = cluster_records[0]["sizeleft"]
    history = []
    for hours_ago in range(0, 9 * 24):
        history.append({"ts": now - hours_ago * 3600, "sizeleft": sizeleft})
    history.append({"ts": now - nine_days_s, "sizeleft": sizeleft})  # 9d-old anchor

    fake_state = {
        f"sonarr:{cluster_records[0]['downloadId'].lower()}": {
            "title": cluster_records[0]["title"],
            "queue_id": cluster_records[0]["id"],
            "first_seen_stuck": now - nine_days_s,
            "slug": "sonarr",
            "mode": "slow-cluster",
            "sizeleft_history": history,
        }
    }
    state_file.write_text(json.dumps(fake_state))

    def fake_req(method, url, key, **kw):
        if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
            return 200, json.dumps({"records": cluster_records})
        if method == "GET":
            return 200, json.dumps({"records": []})
        if method == "DELETE":
            return 200, ""
        return 500, ""

    monkeypatch.setattr(arrhk, "_req", fake_req)
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")
    monkeypatch.setattr(arrhk, "_notify", lambda *a, **kw: None)

    arrhk.cmd_unstick(dry_run=True)

    state = json.loads(state_file.read_text())
    sk = next(iter(state))
    retained = state[sk]["sizeleft_history"]
    # Oldest retained sample must still be at least CLUSTER_NOPROGRESS_DAYS old.
    oldest_ts = min(s["ts"] for s in retained)
    age_days = (now - oldest_ts) / 86400
    assert age_days >= arrhk.CLUSTER_NOPROGRESS_DAYS, (
        f"oldest retained sample is only {age_days:.2f}d old "
        f"(< CLUSTER_NOPROGRESS_DAYS={arrhk.CLUSTER_NOPROGRESS_DAYS}d) — "
        f"slow-cluster predicate cannot fire in production"
    )
    # Sanity: history was actually trimmed (not unbounded growth)
    assert len(retained) <= 250, f"history is unbounded: {len(retained)} entries"


def test_cmd_unstick_respects_per_run_cap(tmp_path, monkeypatch):
    """Three stalled items on each of two arrs, per-run cap=4. Total DELETEs ≤ 4."""
    state_file = tmp_path / "stuck.json"
    monkeypatch.setattr(arrhk, "STATE_DIR", tmp_path)
    monkeypatch.setattr(arrhk, "STUCK_STATE_FILE", state_file)
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "4")
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "10")  # high enough to not bind first
    monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")  # bypass grace period

    base = _load("peers.json")[0]
    def _records(prefix):
        out = []
        for i in range(3):
            item = dict(base)
            item["id"] = i + (1000 if prefix == "s" else 2000)
            item["downloadId"] = f"{prefix}{i:039x}".upper()
            out.append(item)
        return out

    sonarr_recs = _records("s")
    radarr_recs = _records("r")

    fake_state = {}
    for slug, recs in [("sonarr", sonarr_recs), ("radarr", radarr_recs)]:
        for it in recs:
            fake_state[f"{slug}:{it['downloadId'].lower()}"] = {
                "title": it["title"], "queue_id": it["id"],
                "first_seen_stuck": time.time() - 86400,
                "slug": slug, "mode": "stalled-no-peers",
                "sizeleft_history": [],
            }
    state_file.write_text(json.dumps(fake_state))

    deletes: list[str] = []
    def fake_req(method, url, key, **kw):
        if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
            return 200, json.dumps({"records": sonarr_recs})
        if method == "GET" and "radarr/" in url and "/queue" in url and "radarr2" not in url:
            return 200, json.dumps({"records": radarr_recs})
        if method == "GET":
            return 200, json.dumps({"records": []})
        if method == "DELETE":
            deletes.append(url)
            return 200, ""
        return 500, ""

    monkeypatch.setattr(arrhk, "_req", fake_req)
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug in {"sonarr", "radarr"} else "")
    monkeypatch.setattr(arrhk, "_notify", lambda *a, **kw: None)

    arrhk.cmd_unstick(dry_run=False)
    assert len(deletes) == 4, f"expected 4 DELETEs (per-run cap=4), got {len(deletes)}"
