"""Tests for MCP tools (call the underlying functions directly).

The MCP framework registration is exercised in the acceptance test.
"""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "local" / "qflix-mcp"))

import qflix_mcp  # noqa: E402
from lib.cache import Cache, atomic_write_json  # noqa: E402


def _seed_snapshot(root: Path, hour: int, content: dict) -> None:
    snaps = root / "snapshots" / "2026-05-12"
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / f"{hour:02d}.json").write_text(json.dumps(content))


def test_status_with_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    out = qflix_mcp.qflix_status()
    assert out["latest_snapshot"] is None
    assert out["torrent_count"] == 0


def test_status_returns_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    _seed_snapshot(tmp_path, 1, {
        "captured_at": "2026-05-12T01:00:00Z",
        "qbit": {"torrents": [{"hash": "a"}, {"hash": "b"}], "totals": {}},
        "health": {"kuma_red": ["X"], "zombies": []},
    })
    out = qflix_mcp.qflix_status()
    assert out["torrent_count"] == 2
    assert out["kuma_red"] == ["X"]


def test_list_torrents_returns_torrents(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    _seed_snapshot(tmp_path, 1, {
        "captured_at": "2026-05-12T01:00:00Z",
        "qbit": {"torrents": [{"hash": "a", "name": "Foo"}]},
    })
    out = qflix_mcp.qflix_list_torrents()
    assert out[0]["hash"] == "a"


def test_torrent_history_finds_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    for h in (0, 1):
        _seed_snapshot(tmp_path, h, {
            "captured_at": f"2026-05-12T{h:02d}:00:00Z",
            "qbit": {"torrents": [{"hash": "abc", "downloaded_bytes": 100 + h,
                                    "progress": 0.1, "dl_speed_bytes_s": 0,
                                    "state": "stalledDL"}]},
        })
    hist = qflix_mcp.qflix_torrent_history("abc", hours=24)
    assert len(hist) == 2


def test_list_stale_returns_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    state = {"hashes": {"abc": {"candidate_for_unstick": True,
                                 "consecutive_zero_hours": 3,
                                 "rule_matched": "stalledDL"}},
             "updated_at": "2026-05-12T01:00:00Z"}
    (tmp_path / "stale-state.json").write_text(json.dumps(state))
    out = qflix_mcp.qflix_list_stale()
    assert "abc" in {h["hash"] for h in out}


def test_get_logs_returns_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    logs = tmp_path / "logs" / "2026-05-12"
    logs.mkdir(parents=True)
    (logs / "sonarr.log").write_text("line1\nline2\nline3\n")
    out = qflix_mcp.qflix_get_logs(app="sonarr", date="2026-05-12", grep=None, max_lines=10)
    assert len(out) == 3


def test_plex_libraries(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    _seed_snapshot(tmp_path, 1, {
        "captured_at": "x",
        "plex": {"libraries": [{"key": "1", "title": "Movies", "count": 100}]},
    })
    out = qflix_mcp.qflix_plex_libraries()
    assert out["libraries"][0]["title"] == "Movies"


def test_recent_events(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    events = tmp_path / "events"
    events.mkdir()
    (events / "2026-05-12.jsonl").write_text(
        '{"ts":"2026-05-12T01:00:00Z","action":"unstick","slug":"sonarr"}\n'
    )
    out = qflix_mcp.qflix_recent_events(n=10)
    assert out[0]["action"] == "unstick"


def test_arr_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    _seed_snapshot(tmp_path, 1, {
        "captured_at": "x",
        "arrs": {"sonarr": {"queue": [{"id": 1, "title": "X",
                                       "trackedDownloadState": "downloading"}],
                            "missing_count": 5}},
    })
    out = qflix_mcp.qflix_arr_queue("sonarr")
    assert out["queue"][0]["id"] == 1
    assert out["missing_count"] == 5
