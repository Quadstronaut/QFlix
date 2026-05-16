"""Tests for scripts/maint/qflix-vlogs-ingest.py helpers.

Loaded via importlib because the script filename has a dash and isn't
importable as a normal module.
"""
from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

_INGEST_PATH = (Path(__file__).resolve().parents[2]
                / "scripts" / "maint" / "qflix-vlogs-ingest.py")


def _load_ingest():
    spec = importlib.util.spec_from_file_location("qflix_vlogs_ingest",
                                                   _INGEST_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_window_seconds_supported_units():
    mod = _load_ingest()
    assert mod._parse_window_seconds("30s") == 30
    assert mod._parse_window_seconds("6m") == 360
    assert mod._parse_window_seconds("2h") == 7200
    assert mod._parse_window_seconds("1d") == 86400


def test_parse_window_seconds_unknown_falls_back():
    mod = _load_ingest()
    # Garbage input shouldn't crash — fall back to a sane default.
    assert mod._parse_window_seconds("garbage") > 0
    assert mod._parse_window_seconds("") > 0


def test_file_is_dormant_when_old(tmp_path):
    mod = _load_ingest()
    f = tmp_path / "old.log"
    f.write_text("stale\n")
    # Backdate to 10 minutes ago.
    old = time.time() - 600
    os.utime(f, (old, old))
    assert mod._file_is_dormant(str(f), max_age_s=360) is True


def test_file_is_not_dormant_when_fresh(tmp_path):
    mod = _load_ingest()
    f = tmp_path / "fresh.log"
    f.write_text("hot\n")
    assert mod._file_is_dormant(str(f), max_age_s=360) is False


def test_file_is_dormant_missing_file_is_not_dormant(tmp_path):
    """Non-existent files are handled by logs.collect_for (returns empty);
    the dormant check must not short-circuit those."""
    mod = _load_ingest()
    assert mod._file_is_dormant(str(tmp_path / "nope.log"), max_age_s=60) is False
