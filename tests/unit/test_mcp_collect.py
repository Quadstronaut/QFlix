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


def test_classify_stopped_dl_incomplete():
    """qBit 5.x stoppedDL at incomplete progress → stopped-incomplete eligible
    (the 'Unforgettable' ratio-auto-paused dead download)."""
    t = collect.normalize_qbit_torrent(_qbit_torrent(state="stoppedDL",
                                                     progress=0.35))
    assert collect.matches_stale_rule(t) == "stopped-incomplete"


def test_classify_paused_dl_incomplete_legacy():
    """qBit 4.x pausedDL (legacy name) at incomplete progress also matches."""
    t = collect.normalize_qbit_torrent(_qbit_torrent(state="pausedDL",
                                                     progress=0.5))
    assert collect.matches_stale_rule(t) == "stopped-incomplete"


def test_classify_stopped_up_completed_returns_none():
    """stoppedUP is a completed/seeding torrent — progress>=1.0 excludes it;
    must NOT be flagged (no destructive unstick of finished content)."""
    t = collect.normalize_qbit_torrent(_qbit_torrent(state="stoppedUP",
                                                     progress=1.0))
    assert collect.matches_stale_rule(t) is None


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


# --- SAB (Usenet) normalize + classify --------------------------------------
# 2026-07-19 sab-stuck-parity spec, C2/C9. SAB emits mb/mbleft as STRINGS
# ("4801.69") — this is the real shape returned by mode=queue, not a
# simplification for the test.

def _sab_slot(**overrides):
    base = {
        "nzo_id": "SABnzbd_nzo_abc123",
        "filename": "Some.Show.S01E01.mkv",
        "cat": "sonarr",
        "status": "Downloading",
        "mb": "4801.69",
        "mbleft": "4203.90",
    }
    base.update(overrides)
    return base


def test_normalize_sab_slot_renames_fields_and_converts_mib():
    """mb/mbleft are MiB strings; size_bytes/downloaded_bytes are raw byte
    ints derived from them (mb*MiB, (mb-mbleft)*MiB)."""
    out = collect.normalize_sab_slot(_sab_slot(), kbpersec=500.0)
    assert out["id"] == "SABnzbd_nzo_abc123"
    assert out["name"] == "Some.Show.S01E01.mkv"
    assert out["cat"] == "sonarr"
    assert out["state"] == "Downloading"
    assert out["size_bytes"] == round(4801.69 * 1024 * 1024)
    assert out["downloaded_bytes"] == round((4801.69 - 4203.90) * 1024 * 1024)
    assert out["progress"] == round(1 - (4203.90 / 4801.69), 4)


def test_normalize_sab_slot_dl_speed_only_when_downloading():
    """dl_speed_bytes_s is the QUEUE kbpersec*1024 attributed to the active
    Downloading slot; every other state gets 0 regardless of queue speed."""
    downloading = collect.normalize_sab_slot(_sab_slot(status="Downloading"),
                                              kbpersec=500.0)
    assert downloading["dl_speed_bytes_s"] == round(500.0 * 1024)

    queued = collect.normalize_sab_slot(_sab_slot(status="Queued"),
                                         kbpersec=500.0)
    assert queued["dl_speed_bytes_s"] == 0


def test_normalize_sab_slot_zero_mb_no_division_by_zero():
    """mb == 0 (a slot that hasn't started sizing yet) -> progress 0, not a
    ZeroDivisionError crashing the hourly collector."""
    out = collect.normalize_sab_slot(_sab_slot(mb="0.0", mbleft="0.0"))
    assert out["progress"] == 0
    assert out["size_bytes"] == 0
    assert out["downloaded_bytes"] == 0


def test_normalize_sab_slot_coerces_malformed_numeric_field():
    """A garbage/missing mb or mbleft must fall back to 0.0, never raise."""
    out = collect.normalize_sab_slot(_sab_slot(mb="", mbleft=None))
    assert out["size_bytes"] == 0
    assert out["downloaded_bytes"] == 0
    assert out["progress"] == 0


def test_matches_stale_sab_rule_paused_pinned_when_queue_running():
    """Paused slot + queue NOT paused == the object.py force-pause wedge."""
    assert collect.matches_stale_sab_rule("Paused", False) == "sab-paused-pinned"


def test_matches_stale_sab_rule_paused_while_queue_paused_does_not_match():
    """Paused slot + queue ALSO paused == an operator pausing the whole
    queue, not a stuck job. Must NOT match any rule."""
    assert collect.matches_stale_sab_rule("Paused", True) is None


def test_matches_stale_sab_rule_zero_movement_states():
    for state in ("Downloading", "Queued", "Grabbing", "Fetching", "Propagating"):
        assert collect.matches_stale_sab_rule(state, False) == "sab-zero-movement"


def test_matches_stale_sab_rule_pp_hung_states():
    for state in ("Verifying", "Repairing", "Extracting", "Moving", "Running",
                  "QuickCheck", "Checking"):
        assert collect.matches_stale_sab_rule(state, False) == "sab-pp-hung"


def test_matches_stale_sab_rule_healthy_state_returns_none():
    assert collect.matches_stale_sab_rule("Completed", False) is None


class _FakeSabNoSecrets:
    """Stand-in for SabClient() when ~/secrets/sabnzbd.{port,key} are absent."""
    def __init__(self):
        self.host = ""
        self.apikey = ""


def test_collect_sab_error_shape_no_secrets(monkeypatch):
    """Missing secrets -> qbit-parity error shape, never a live call attempt."""
    monkeypatch.setattr(collect, "SabClient", _FakeSabNoSecrets)
    out = collect._collect_sab()
    assert out == {"error": "no_secrets", "slots": [], "queue": {}, "totals": {}}


class _FakeSabTransportError:
    """SabClient methods raise on transport error (per lib/sab_client.py's
    contract) — _collect_sab is the one place that must catch it."""
    def __init__(self):
        self.host = "http://127.0.0.1:8080/api"
        self.apikey = "deadbeef"

    def queue_meta(self):
        raise OSError("connection reset by peer")

    def list_slots(self):  # pragma: no cover - queue_meta raises first
        return []


def test_collect_sab_error_shape_transport_failure(monkeypatch):
    monkeypatch.setattr(collect, "SabClient", _FakeSabTransportError)
    out = collect._collect_sab()
    assert out["slots"] == []
    assert out["queue"] == {}
    assert out["totals"] == {}
    assert "connection reset" in out["error"]


class _FakeSabHealthy:
    """A queue with one active Downloading slot and one Queued slot."""
    def __init__(self):
        self.host = "http://127.0.0.1:8080/api"
        self.apikey = "deadbeef"

    def queue_meta(self):
        return {"paused": False, "kbpersec": 500.0, "status": "Downloading"}

    def list_slots(self):
        return [
            _sab_slot(nzo_id="a", status="Downloading"),
            _sab_slot(nzo_id="b", status="Queued", mb="1000.0", mbleft="1000.0"),
        ]


def test_collect_sab_happy_path_assembles_qbit_parity_shape(monkeypatch):
    monkeypatch.setattr(collect, "SabClient", _FakeSabHealthy)
    out = collect._collect_sab()
    assert "error" not in out
    assert out["totals"] == {"count": 2}
    assert out["queue"] == {"paused": False, "kbpersec": 500.0, "status": "Downloading"}
    by_id = {s["id"]: s for s in out["slots"]}
    assert by_id["a"]["dl_speed_bytes_s"] == round(500.0 * 1024)  # active slot
    assert by_id["b"]["dl_speed_bytes_s"] == 0  # queued slot, not attributed


def test_run_omits_sab_key_when_not_included(monkeypatch):
    """Regression (2026-07-20 review crit): run() must NOT emit a healthy-
    empty sab section when --include lacks sab — qflix-collect.py's ghost
    prune reads a MISSING sab key as "no evidence, keep sab entries"; an
    always-present empty shape would mass-prune every tracked SAB entry on
    any snapshot collected without sab."""
    monkeypatch.setattr(collect, "_collect_qbit", lambda: {"torrents": [], "totals": {}})
    monkeypatch.setattr(collect, "_kuma_red_list", lambda: [])
    out = collect.run(include={"qbit"}, recent_hours=1)
    assert "sab" not in out

    monkeypatch.setattr(collect, "_collect_sab",
                        lambda: {"slots": [], "queue": {"paused": False}, "totals": {"count": 0}})
    out = collect.run(include={"qbit", "sab"}, recent_hours=1)
    assert out["sab"]["totals"] == {"count": 0}
