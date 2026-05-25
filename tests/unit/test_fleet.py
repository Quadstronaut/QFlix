"""Unit tests for lib/fleet.py — correlated-alert collapse (sub-project C)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Ensure lib/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "maint"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _probe_ok(n_down: int, total: int) -> dict[str, bool]:
    """Build a probe_ok map with n_down False entries and (total-n_down) True."""
    result = {}
    for i in range(total):
        result[f"app-{i}"] = i >= n_down  # first n_down are False
    return result


def _results(probe_ok_map: dict[str, bool]) -> dict[str, str]:
    """Build a matching results dict (pusher format)."""
    return {k: ("ok" if v else "down") for k, v in probe_ok_map.items()}


# ---------------------------------------------------------------------------
# Threshold boundary tests
# ---------------------------------------------------------------------------

class TestThresholdBoundary:
    def test_seven_down_no_storm(self, tmp_path):
        """7 down with default threshold 8 → no storm."""
        from lib import fleet
        state_path = tmp_path / "fleet-window.json"
        probe_ok = _probe_ok(7, 33)
        res = fleet.evaluate(_results(probe_ok), probe_ok=probe_ok, state_path=state_path)
        assert res["down_count"] == 7
        assert res["total"] == 33
        assert res["storm_active"] is False
        assert res["edge"] is None

    def test_eight_down_storm_onset(self, tmp_path):
        """8 down with default threshold 8 → storm onset."""
        from lib import fleet
        state_path = tmp_path / "fleet-window.json"
        probe_ok = _probe_ok(8, 33)
        res = fleet.evaluate(_results(probe_ok), probe_ok=probe_ok, state_path=state_path)
        assert res["down_count"] == 8
        assert res["storm_active"] is True
        assert res["edge"] == "onset"

    def test_threshold_boundary_exact(self, tmp_path, monkeypatch):
        """Threshold is >= not >: exactly threshold triggers storm."""
        from lib import fleet
        monkeypatch.setattr(fleet, "FLEET_STORM_THRESHOLD", 5)
        state_path = tmp_path / "fleet-window.json"
        # 4 down → no storm
        probe_ok = _probe_ok(4, 10)
        res = fleet.evaluate(_results(probe_ok), probe_ok=probe_ok, state_path=state_path)
        assert res["storm_active"] is False
        # 5 down → storm
        probe_ok = _probe_ok(5, 10)
        state_path2 = tmp_path / "fleet-window-2.json"
        res2 = fleet.evaluate(_results(probe_ok), probe_ok=probe_ok, state_path=state_path2)
        assert res2["storm_active"] is True
        assert res2["edge"] == "onset"


# ---------------------------------------------------------------------------
# Edge-detection across cycles
# ---------------------------------------------------------------------------

class TestEdgeDetection:
    def test_onset_fires_once_then_silent(self, tmp_path, monkeypatch):
        """Storm onset fires on first storm cycle; subsequent cycles return edge=None."""
        from lib import fleet
        monkeypatch.setattr(fleet, "FLEET_STORM_THRESHOLD", 3)
        state_path = tmp_path / "fleet-window.json"
        probe_ok = _probe_ok(5, 10)  # 5 down, storm

        # First cycle: onset
        res1 = fleet.evaluate(_results(probe_ok), probe_ok=probe_ok, state_path=state_path)
        assert res1["edge"] == "onset"
        assert res1["storm_active"] is True

        # Second cycle: still storm, no edge re-fire
        res2 = fleet.evaluate(_results(probe_ok), probe_ok=probe_ok, state_path=state_path)
        assert res2["edge"] is None
        assert res2["storm_active"] is True

        # Third cycle: still storm, no edge
        res3 = fleet.evaluate(_results(probe_ok), probe_ok=probe_ok, state_path=state_path)
        assert res3["edge"] is None

    def test_clear_fires_once_then_silent(self, tmp_path, monkeypatch):
        """Storm clear fires exactly once when storm ends; subsequent non-storm cycles no edge."""
        from lib import fleet
        monkeypatch.setattr(fleet, "FLEET_STORM_THRESHOLD", 3)
        state_path = tmp_path / "fleet-window.json"

        # Establish storm
        storm_ok = _probe_ok(5, 10)
        fleet.evaluate(_results(storm_ok), probe_ok=storm_ok, state_path=state_path)

        # Storm clears
        clear_ok = _probe_ok(1, 10)
        res_clear = fleet.evaluate(_results(clear_ok), probe_ok=clear_ok, state_path=state_path)
        assert res_clear["edge"] == "clear"
        assert res_clear["storm_active"] is False

        # Next cycle: no storm, no edge
        res_next = fleet.evaluate(_results(clear_ok), probe_ok=clear_ok, state_path=state_path)
        assert res_next["edge"] is None
        assert res_next["storm_active"] is False

    def test_onset_clear_cycle(self, tmp_path, monkeypatch):
        """Full cycle: no storm → onset → (sustain) → clear → no edge."""
        from lib import fleet
        monkeypatch.setattr(fleet, "FLEET_STORM_THRESHOLD", 3)
        state_path = tmp_path / "fleet-window.json"
        storm = _probe_ok(5, 10)
        calm = _probe_ok(1, 10)

        r1 = fleet.evaluate(_results(calm), probe_ok=calm, state_path=state_path)
        assert r1["edge"] is None  # no storm yet

        r2 = fleet.evaluate(_results(storm), probe_ok=storm, state_path=state_path)
        assert r2["edge"] == "onset"

        r3 = fleet.evaluate(_results(storm), probe_ok=storm, state_path=state_path)
        assert r3["edge"] is None  # still storm, no re-fire

        r4 = fleet.evaluate(_results(calm), probe_ok=calm, state_path=state_path)
        assert r4["edge"] == "clear"

        r5 = fleet.evaluate(_results(calm), probe_ok=calm, state_path=state_path)
        assert r5["edge"] is None  # no re-fire on clear


# ---------------------------------------------------------------------------
# State round-trip and corrupt/missing state
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_state_written_after_evaluate(self, tmp_path, monkeypatch):
        from lib import fleet
        monkeypatch.setattr(fleet, "FLEET_STORM_THRESHOLD", 3)
        state_path = tmp_path / "fleet-window.json"
        probe_ok = _probe_ok(5, 10)
        fleet.evaluate(_results(probe_ok), probe_ok=probe_ok, state_path=state_path)
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert data["storm_active"] is True
        assert data["down_count"] == 5
        assert data["total"] == 10
        assert "since" in data
        assert "last_eval_at" in data

    def test_corrupt_state_fresh_start(self, tmp_path, monkeypatch):
        """Corrupt state file → treat as no prior storm (fresh start)."""
        from lib import fleet
        monkeypatch.setattr(fleet, "FLEET_STORM_THRESHOLD", 3)
        state_path = tmp_path / "fleet-window.json"
        state_path.write_text("NOT VALID JSON{{{{")

        # Should not raise; treats as fresh
        probe_ok = _probe_ok(1, 10)  # below threshold
        res = fleet.evaluate(_results(probe_ok), probe_ok=probe_ok, state_path=state_path)
        assert res["edge"] is None
        assert res["storm_active"] is False

    def test_missing_state_fresh_start(self, tmp_path, monkeypatch):
        """Missing state file → treat as no prior storm."""
        from lib import fleet
        monkeypatch.setattr(fleet, "FLEET_STORM_THRESHOLD", 3)
        state_path = tmp_path / "nonexistent-fleet-window.json"
        assert not state_path.exists()

        probe_ok = _probe_ok(5, 10)
        res = fleet.evaluate(_results(probe_ok), probe_ok=probe_ok, state_path=state_path)
        # First call with no prior state → onset
        assert res["edge"] == "onset"
        assert state_path.exists()

    def test_state_survives_restart(self, tmp_path, monkeypatch):
        """State written in one call is read correctly in the next (simulates restart)."""
        from lib import fleet
        monkeypatch.setattr(fleet, "FLEET_STORM_THRESHOLD", 3)
        state_path = tmp_path / "fleet-window.json"
        storm = _probe_ok(5, 10)

        # Simulated cycle 1
        r1 = fleet.evaluate(_results(storm), probe_ok=storm, state_path=state_path)
        assert r1["edge"] == "onset"

        # "Restart" — reload the module's function (same state file)
        # Cycle 2: should NOT re-fire onset
        r2 = fleet.evaluate(_results(storm), probe_ok=storm, state_path=state_path)
        assert r2["edge"] is None, "onset must not re-fire after restart reads persisted state"


# ---------------------------------------------------------------------------
# down_count / total math
# ---------------------------------------------------------------------------

class TestCountMath:
    def test_down_count_equals_false_entries(self, tmp_path, monkeypatch):
        from lib import fleet
        monkeypatch.setattr(fleet, "FLEET_STORM_THRESHOLD", 100)  # disable storm
        state_path = tmp_path / "fleet-window.json"
        # 3 down out of 7
        probe_ok = {"a": True, "b": False, "c": True, "d": False, "e": True, "f": False, "g": True}
        res = fleet.evaluate({k: ("ok" if v else "down") for k, v in probe_ok.items()},
                             probe_ok=probe_ok, state_path=state_path)
        assert res["down_count"] == 3
        assert res["total"] == 7

    def test_all_up_zero_down(self, tmp_path):
        from lib import fleet
        state_path = tmp_path / "fleet-window.json"
        probe_ok = {"a": True, "b": True, "c": True}
        res = fleet.evaluate({k: "ok" for k in probe_ok},
                             probe_ok=probe_ok, state_path=state_path)
        assert res["down_count"] == 0
        assert res["total"] == 3
        assert res["storm_active"] is False

    def test_all_down_triggers_storm(self, tmp_path, monkeypatch):
        from lib import fleet
        monkeypatch.setattr(fleet, "FLEET_STORM_THRESHOLD", 3)
        state_path = tmp_path / "fleet-window.json"
        probe_ok = {"a": False, "b": False, "c": False, "d": False}
        res = fleet.evaluate({k: "down" for k in probe_ok},
                             probe_ok=probe_ok, state_path=state_path)
        assert res["down_count"] == 4
        assert res["storm_active"] is True

    def test_never_raises(self, tmp_path):
        """evaluate() must never raise regardless of inputs."""
        from lib import fleet
        # Bad state_path parent doesn't exist but evaluate should not raise
        state_path = tmp_path / "deep" / "nonexistent" / "fleet.json"
        try:
            res = fleet.evaluate({}, probe_ok={}, state_path=state_path)
        except Exception as exc:
            pytest.fail(f"fleet.evaluate raised: {exc}")
        assert res["down_count"] == 0
        assert res["total"] == 0
