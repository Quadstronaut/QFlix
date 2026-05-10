"""tests/unit/test_state.py — TDD tests for lib/state.py."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from lib.state import read, record, write


# ---------------------------------------------------------------------------
# read()
# ---------------------------------------------------------------------------

def test_state_read_missing_file_returns_default(tmp_path):
    result = read(tmp_path / "nonexistent.json")
    assert result == {}


def test_state_read_corrupt_file_returns_default_logs(tmp_path, capsys):
    bad = tmp_path / "corrupt.json"
    bad.write_text("this is not json {{{{", encoding="utf-8")
    result = read(bad)
    assert result == {}
    captured = capsys.readouterr()
    assert captured.err != "", "expected a warning on stderr for corrupt file"


# ---------------------------------------------------------------------------
# write()
# ---------------------------------------------------------------------------

def test_state_write_atomic_replace(tmp_path):
    path = tmp_path / "state.json"
    data = {"apps": {"sonarr": {"event": "recovered"}}}
    write(path, data)
    assert read(path) == data
    # No leftover .tmp files
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"leftover temp files: {tmp_files}"


def test_state_write_creates_parent_dirs(tmp_path):
    path = tmp_path / "a" / "b" / "state.json"
    data = {"apps": {}}
    write(path, data)
    assert read(path) == data


def test_state_write_sets_mode_0600(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX file modes are advisory on non-POSIX systems")
    path = tmp_path / "state.json"
    write(path, {"x": 1})
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------

def test_state_record_app_event(tmp_path):
    path = tmp_path / "state.json"
    record(path, "sonarr", event="recovered", attempts=1)
    data = read(path)
    app = data["apps"]["sonarr"]
    assert app["event"] == "recovered"
    assert app["attempts"] == 1
    # updated_at must be a parseable ISO timestamp
    updated_at = app["updated_at"]
    datetime.fromisoformat(updated_at.rstrip("Z"))


def test_state_record_preserves_other_apps(tmp_path):
    path = tmp_path / "state.json"
    record(path, "sonarr", event="recovered", attempts=1)
    record(path, "radarr", event="failed", attempts=3)
    data = read(path)
    assert "sonarr" in data["apps"]
    assert "radarr" in data["apps"]
    assert data["apps"]["sonarr"]["event"] == "recovered"
    assert data["apps"]["radarr"]["event"] == "failed"
