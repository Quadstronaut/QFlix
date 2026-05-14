"""Tests for scripts/local/qflix-mcp/lib/cache.py."""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "local" / "qflix-mcp"))

from lib.cache import Cache, atomic_write_json  # noqa: E402


def test_atomic_write_json(tmp_path):
    p = tmp_path / "sub" / "file.json"
    atomic_write_json(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}


def test_latest_snapshot(tmp_path):
    snaps = tmp_path / "snapshots" / "2026-05-12"
    snaps.mkdir(parents=True)
    (snaps / "00.json").write_text(json.dumps({"captured_at": "2026-05-12T00:00:00Z"}))
    (snaps / "01.json").write_text(json.dumps({"captured_at": "2026-05-12T01:00:00Z"}))
    c = Cache(tmp_path)
    latest = c.latest_snapshot()
    assert latest["captured_at"] == "2026-05-12T01:00:00Z"


def test_previous_snapshots_returns_n(tmp_path):
    snaps = tmp_path / "snapshots" / "2026-05-12"
    snaps.mkdir(parents=True)
    for h in (0, 1, 2):
        (snaps / f"{h:02d}.json").write_text(json.dumps({"captured_at": f"2026-05-12T{h:02d}:00:00Z"}))
    c = Cache(tmp_path)
    prev = c.previous_snapshots(2)  # not including latest
    assert len(prev) == 2
    assert prev[0]["captured_at"] == "2026-05-12T01:00:00Z"
    assert prev[1]["captured_at"] == "2026-05-12T00:00:00Z"


def test_history_for_hash(tmp_path):
    snaps = tmp_path / "snapshots" / "2026-05-12"
    snaps.mkdir(parents=True)
    for h in (0, 1, 2):
        (snaps / f"{h:02d}.json").write_text(json.dumps({
            "captured_at": f"2026-05-12T{h:02d}:00:00Z",
            "qbit": {"torrents": [{"hash": "abc", "downloaded_bytes": 100 + h,
                                    "progress": 0.1 * h}]},
        }))
    c = Cache(tmp_path)
    hist = c.history_for_hash("abc", hours=24)
    assert len(hist) == 3
    assert hist[0]["downloaded_bytes"] == 100
