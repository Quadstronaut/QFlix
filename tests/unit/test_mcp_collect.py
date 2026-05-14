"""Tests for scripts/mcp/collect.py — JSON shape + classification logic."""
from __future__ import annotations
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "mcp"))

import collect  # noqa: E402


def _qbit_torrent(**overrides):
    base = {
        "hash": "abc", "name": "Foo", "added_on": 1715000000,
        "size": 1_000_000_000, "downloaded": 500_000_000, "progress": 0.5,
        "dlspeed": 0, "upspeed": 0, "state": "stalledDL", "category": "sonarr",
        "tags": "tv-sonarr", "ratio": 0.0, "eta": 0, "num_seeds": 0,
        "num_leechs": 0, "last_activity": 1715000000,
    }
    base.update(overrides)
    return base


def test_normalize_qbit_renames_fields():
    """qBit's API uses dlspeed/upspeed/num_seeds; spec wants
    dl_speed_bytes_s/up_speed_bytes_s/seeds. The normalizer remaps."""
    out = collect.normalize_qbit_torrent(_qbit_torrent())
    assert out["dl_speed_bytes_s"] == 0
    assert out["up_speed_bytes_s"] == 0
    assert out["seeds"] == 0
    assert out["downloaded_bytes"] == 500_000_000
    assert out["size_bytes"] == 1_000_000_000


def test_classify_stalled_dl_24h():
    """state=stalledDL counts toward rule 1 once the workstation-side 3-hour
    rule has confirmed no movement. The classifier flags eligibility, the
    workstation enforces the 3-hour-zero-movement window."""
    t = collect.normalize_qbit_torrent(_qbit_torrent(state="stalledDL"))
    assert collect.matches_stale_rule(t) == "stalledDL"


def test_classify_dead_slow_dl():
    """state=downloading + speed<10 kB/s and progress<1.0 → dead-slow eligible."""
    t = collect.normalize_qbit_torrent(_qbit_torrent(state="downloading",
                                                     dlspeed=5000, progress=0.3))
    assert collect.matches_stale_rule(t) == "dead-slow"


def test_classify_healthy_returns_none():
    t = collect.normalize_qbit_torrent(_qbit_torrent(state="downloading",
                                                     dlspeed=2_000_000, progress=0.3))
    assert collect.matches_stale_rule(t) is None


def test_classify_completed_returns_none():
    t = collect.normalize_qbit_torrent(_qbit_torrent(state="uploading",
                                                     progress=1.0))
    assert collect.matches_stale_rule(t) is None


def test_suspicious_size_movie():
    """Single-video movie <100MB → suspicious."""
    t = collect.normalize_qbit_torrent(_qbit_torrent(category="radarr",
                                                     size=50_000_000, progress=1.0))
    assert collect.is_suspicious_size(t) is True


def test_suspicious_size_episode():
    t = collect.normalize_qbit_torrent(_qbit_torrent(category="sonarr",
                                                     size=30_000_000, progress=1.0))
    assert collect.is_suspicious_size(t) is True


def test_orphan_detection():
    """qBit category=sonarr but no matching queue entry → orphan."""
    t = collect.normalize_qbit_torrent(_qbit_torrent(category="sonarr"))
    arr_queues = {"sonarr": [{"downloadId": "DIFFERENT_HASH"}]}
    assert collect.is_orphan(t, arr_queues) is True


def test_zombie_detection():
    """*arr queue references a hash that doesn't exist in qBit → zombie."""
    qbit_hashes = {"abc"}
    arr_queues = {"sonarr": [{"id": 1, "downloadId": "ZOMBIE_HASH",
                              "title": "X"}]}
    zombies = collect.find_zombies(qbit_hashes, arr_queues)
    assert zombies == [{"slug": "sonarr", "queue_id": 1, "hash": "ZOMBIE_HASH",
                        "title": "X"}]


def test_find_stuck_imports():
    """Rule 4: queue items in importPending/Blocked/Failed are surfaced
    in health for visibility (no autonomous action)."""
    arr_queues = {
        "sonarr": [
            {"id": 1, "title": "X", "downloadId": "h1",
             "trackedDownloadState": "downloading", "statusMessages": []},
            {"id": 2, "title": "Y", "downloadId": "h2",
             "trackedDownloadState": "importPending",
             "statusMessages": [{"title": "msg"}]},
            {"id": 3, "title": "Z", "downloadId": "h3",
             "trackedDownloadState": "importBlocked", "statusMessages": []},
        ],
    }
    stuck = collect.find_stuck_imports(arr_queues)
    titles = {s["title"] for s in stuck}
    assert titles == {"Y", "Z"}  # X is fine


def test_compute_bad_grab_signals_suspicious_size():
    t = collect.normalize_qbit_torrent({
        "hash": "h", "name": "n", "added_on": 0, "size": 50_000_000,
        "downloaded": 50_000_000, "progress": 1.0, "dlspeed": 0, "upspeed": 0,
        "state": "uploading", "category": "radarr", "tags": "",
        "ratio": 0.5, "eta": 0, "num_seeds": 1, "num_leechs": 0,
        "last_activity": 0,
    })
    t["arr"] = {"cf_score": 0}
    sig = collect.compute_bad_grab_signals(t)
    assert sig["suspicious_size"] is True
    assert sig["negative_cf"] is False
    assert sig["any"] is True


def test_compute_bad_grab_signals_negative_cf():
    t = collect.normalize_qbit_torrent({
        "hash": "h", "name": "n", "added_on": 0, "size": 2_000_000_000,
        "downloaded": 2_000_000_000, "progress": 1.0, "dlspeed": 0, "upspeed": 0,
        "state": "uploading", "category": "radarr", "tags": "",
        "ratio": 0.5, "eta": 0, "num_seeds": 1, "num_leechs": 0,
        "last_activity": 0,
    })
    t["arr"] = {"cf_score": -50}
    sig = collect.compute_bad_grab_signals(t)
    assert sig["suspicious_size"] is False
    assert sig["negative_cf"] is True
    assert sig["any"] is True


def test_compute_bad_grab_signals_clean():
    t = collect.normalize_qbit_torrent({
        "hash": "h", "name": "n", "added_on": 0, "size": 2_000_000_000,
        "downloaded": 2_000_000_000, "progress": 1.0, "dlspeed": 0, "upspeed": 0,
        "state": "uploading", "category": "radarr", "tags": "",
        "ratio": 0.5, "eta": 0, "num_seeds": 1, "num_leechs": 0,
        "last_activity": 0,
    })
    t["arr"] = {"cf_score": 100}
    sig = collect.compute_bad_grab_signals(t)
    assert sig["any"] is False
