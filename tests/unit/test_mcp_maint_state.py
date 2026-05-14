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
        "apps": {"sonarr": {"final_health": "down", "kuma_status": "n/a", "event": "failed"}}
    }))
    assert is_arr_red("sonarr", state_file=state_file) is True


def test_green_when_state_says_up(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "apps": {"sonarr": {"final_health": "ok", "kuma_status": "up", "event": "recovered"}}
    }))
    assert is_arr_red("sonarr", state_file=state_file) is False


def test_fail_closed_when_state_missing(tmp_path):
    """Missing state file means we can't verify health → fail closed (red)."""
    assert is_arr_red("sonarr", state_file=tmp_path / "missing.json") is True


def test_fail_closed_when_monitor_unknown(tmp_path):
    """Slug missing from apps block → fail-open (never had an event → presumed ok)."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"apps": {}}))
    assert is_arr_red("sonarr", state_file=state_file) is False


def test_arr_red_when_app_final_health_down(tmp_path):
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({"apps": {"radarr": {"final_health": "down",
                                                   "kuma_status": "n/a",
                                                   "event": "failed"}}}))
    assert is_arr_red("radarr", state_file=sf) is True


def test_arr_red_when_app_kuma_status_down(tmp_path):
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({"apps": {"radarr": {"final_health": "ok",
                                                   "kuma_status": "down"}}}))
    assert is_arr_red("radarr", state_file=sf) is True


def test_arr_not_red_when_app_recovered(tmp_path):
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({"apps": {"radarr": {"final_health": "ok",
                                                   "kuma_status": "up",
                                                   "event": "recovered"}}}))
    assert is_arr_red("radarr", state_file=sf) is False


def test_arr_not_red_when_slug_missing_from_apps(tmp_path):
    """Fail-open: a slug that has never had an event is presumed healthy."""
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({"apps": {"sonarr": {"final_health": "ok"}}}))
    assert is_arr_red("radarr", state_file=sf) is False


def test_arr_red_on_unknown_slug(tmp_path):
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({"apps": {}}))
    assert is_arr_red("bogus-slug", state_file=sf) is True


def test_arr_red_when_state_file_missing(tmp_path):
    """Fail-closed: no information available → don't act."""
    missing = tmp_path / "does-not-exist.json"
    assert is_arr_red("radarr", state_file=missing) is True


def test_arr_red_when_state_file_corrupt(tmp_path):
    sf = tmp_path / "state.json"
    sf.write_text("{not valid json")
    assert is_arr_red("radarr", state_file=sf) is True
