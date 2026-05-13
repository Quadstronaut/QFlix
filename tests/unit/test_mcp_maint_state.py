"""Tests for scripts/mcp/lib/maint_state.py — Kuma-red gating reader."""
from __future__ import annotations
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "mcp"))

from lib.maint_state import is_arr_red  # noqa: E402


def test_red_when_state_says_down(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "monitors": {"Sonarr": {"status": "down", "ts": "now"}}
    }))
    assert is_arr_red("sonarr", state_file=state_file) is True


def test_green_when_state_says_up(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "monitors": {"Sonarr": {"status": "up", "ts": "now"}}
    }))
    assert is_arr_red("sonarr", state_file=state_file) is False


def test_fail_closed_when_state_missing(tmp_path):
    """Missing state file means we can't verify health → fail closed (red)."""
    assert is_arr_red("sonarr", state_file=tmp_path / "missing.json") is True


def test_fail_closed_when_monitor_unknown(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"monitors": {}}))
    assert is_arr_red("sonarr", state_file=state_file) is True
