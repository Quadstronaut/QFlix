"""Tests for qflix-collect.py update_stale_state — ghost pruning.

Regression (2026-07-19): acted-on unstick candidates whose torrents were
long gone from qBit lingered in stale-state.json forever — nothing ever
removed a hash that stopped appearing in snapshots (the delta!=0 prune
needs 3 samples, which a gone torrent never produces). The heartbeat app
surfaced them as 5 phantom "stuck" downloads vs 0 real.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# Load qflix-collect.py as a module (it's a script, not a package).
ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "qflix_collect",
    ROOT / "scripts" / "maint" / "qflix-collect.py",
)
qc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qc)


GHOST = "45e79f57" + "0" * 32   # acted-on, torrent long gone from qBit
LIVE = "c4962b76" + "0" * 32    # healthy, downloading with progress


def _snapshot(torrents: list[dict], *, qbit_error: str | None = None) -> dict:
    q: dict = {"torrents": torrents, "totals": {}}
    if qbit_error:
        q["error"] = qbit_error
    return {"qbit": q}


def _torrent(h: str, downloaded: int, state: str = "downloading") -> dict:
    return {"hash": h, "downloaded_bytes": downloaded, "state": state,
            "progress": 0.5, "dl_speed_bytes_s": 500_000}


def _seed(data_root: Path, snapshots: list[dict], stale_hashes: dict) -> None:
    snap_dir = data_root / "snapshots" / "2026-07-19"
    snap_dir.mkdir(parents=True)
    for i, snap in enumerate(snapshots):
        (snap_dir / f"{i:02d}.json").write_text(json.dumps(snap), encoding="utf-8")
    (data_root / "stale-state.json").write_text(
        json.dumps({"hashes": stale_hashes, "updated_at": "2026-07-19T00:00:00Z"}),
        encoding="utf-8")


def _stale_entry(acted: bool = True) -> dict:
    return {
        "first_zero_movement_at": "2026-07-15T16:00:00Z",
        "consecutive_zero_hours": 7,
        "last_progress": 0.62,
        "rule_matched": "stalledDL",
        "candidate_for_unstick": True,
        "acted_on_at": "2026-07-15T19:00:39Z" if acted else None,
    }


def _run(monkeypatch, tmp_path: Path) -> tuple[list[str], dict]:
    monkeypatch.setattr(qc, "DATA_ROOT", tmp_path)
    candidates = qc.update_stale_state()
    state = json.loads((tmp_path / "stale-state.json").read_text(encoding="utf-8"))
    return candidates, state["hashes"]


def test_ghost_hash_pruned(monkeypatch, tmp_path):
    """Hash absent from the latest snapshot's torrent list is dropped."""
    snaps = [_snapshot([_torrent(LIVE, d)]) for d in (100, 200, 300)]
    _seed(tmp_path, snaps, {GHOST: _stale_entry()})
    _, hashes = _run(monkeypatch, tmp_path)
    assert GHOST not in hashes


def test_qbit_error_snapshot_keeps_state(monkeypatch, tmp_path):
    """A failed qBit collect (error key, empty torrent list) must NOT
    mass-prune legitimate tracked state."""
    snaps = [_snapshot([_torrent(LIVE, d)]) for d in (100, 200)]
    snaps.append(_snapshot([], qbit_error="login_failed"))
    _seed(tmp_path, snaps, {GHOST: _stale_entry()})
    _, hashes = _run(monkeypatch, tmp_path)
    assert GHOST in hashes


def test_live_stalled_candidate_survives_prune(monkeypatch, tmp_path):
    """A genuinely stalled torrent (still in qBit, zero movement across
    3 snapshots) is tracked and survives the ghost prune."""
    snaps = [_snapshot([_torrent(LIVE, 100, state="stalledDL")]) for _ in range(3)]
    _seed(tmp_path, snaps, {})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert LIVE in hashes
    assert candidates == [LIVE]


def test_fewer_than_three_snapshots_no_prune(monkeypatch, tmp_path):
    """<3 snapshots = early return; state is rewritten untouched."""
    _seed(tmp_path, [_snapshot([])], {GHOST: _stale_entry()})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert candidates == []
    assert GHOST in hashes
