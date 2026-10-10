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


# ── Cursor (QFLX-13): each line shipped once, not ~18x ────────────────────────

def test_read_new_lines_first_sight_starts_at_eof(tmp_path):
    mod = _load_ingest()
    f = tmp_path / "a.log"
    f.write_text("old1\nold2\n")
    raw, cur = mod._read_new_lines(str(f), None, tail=100)
    assert raw is None                      # caller does the one-off bootstrap read
    assert cur["offset"] == f.stat().st_size


def test_read_new_lines_only_returns_appended(tmp_path):
    mod = _load_ingest()
    f = tmp_path / "a.log"
    f.write_text("old\n")
    _, cur = mod._read_new_lines(str(f), None, tail=100)
    with f.open("a") as fh:
        fh.write("new1\nnew2\n")
    raw, cur = mod._read_new_lines(str(f), cur, tail=100)
    assert raw == ["new1", "new2"]
    raw, cur = mod._read_new_lines(str(f), cur, tail=100)
    assert raw == []                        # second cycle re-ships nothing


def test_read_new_lines_holds_partial_line(tmp_path):
    mod = _load_ingest()
    f = tmp_path / "a.log"
    f.write_text("")
    _, cur = mod._read_new_lines(str(f), None, tail=100)
    with f.open("a") as fh:
        fh.write("done\nhalf")
    raw, cur = mod._read_new_lines(str(f), cur, tail=100)
    assert raw == ["done"]
    with f.open("a") as fh:
        fh.write("-written\n")
    raw, _ = mod._read_new_lines(str(f), cur, tail=100)
    assert raw == ["half-written"]


def test_read_new_lines_truncation_restarts_at_zero(tmp_path):
    mod = _load_ingest()
    f = tmp_path / "a.log"
    f.write_text("a" * 50 + "\n")
    _, cur = mod._read_new_lines(str(f), None, tail=100)
    f.write_text("fresh\n")                 # copytruncate-style rotation
    raw, _ = mod._read_new_lines(str(f), cur, tail=100)
    assert raw == ["fresh"]


def test_read_new_lines_rotation_new_inode(tmp_path):
    mod = _load_ingest()
    f = tmp_path / "a.log"
    f.write_text("x\n")
    _, cur = mod._read_new_lines(str(f), None, tail=100)
    cur = {**cur, "inode": -1}              # simulate a rename+recreate
    raw, _ = mod._read_new_lines(str(f), cur, tail=100)
    assert raw == ["x"]


def test_read_new_lines_backlog_capped_to_tail(tmp_path):
    mod = _load_ingest()
    f = tmp_path / "a.log"
    f.write_text("")
    _, cur = mod._read_new_lines(str(f), None, tail=3)
    f.write_text("".join(f"l{i}\n" for i in range(10)))
    raw, _ = mod._read_new_lines(str(f), cur, tail=3)
    assert raw == ["l7", "l8", "l9"]


def _sync_log_path():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "mcp"))
    import logs
    return logs._FILE_LOGS["listmonk-sync"]


def test_parse_with_carry_seeds_continuation_from_cursor():
    mod = _load_ingest()
    recs, last = mod._parse_with_carry(
        ["  File \"x.py\", line 1", "2026-10-09 02:00:06,858 listmonk-sync [ERROR] boom"],
        source=_sync_log_path(), last_ts="2026-10-09T02:00:05.000")
    # Cursor written before QFLX-44 holds a zone-less stamp: sync.log is a
    # ZONED/UTC source, so it is re-read as UTC, never as the ingest clock.
    assert recs[0]["ts"] == "2026-10-09T02:00:05.000Z"
    assert recs[1]["ts"] == "2026-10-09T02:00:06.858Z"
    assert recs[1]["level"] == "ERROR"
    assert last == "2026-10-09T02:00:06.858Z"


def test_cursors_roundtrip_and_corrupt_file(tmp_path):
    mod = _load_ingest()
    p = tmp_path / "st" / "cursors.json"
    assert mod._load_cursors(p) == {}
    mod._save_cursors(p, {"a": {"offset": 3}})
    assert mod._load_cursors(p) == {"a": {"offset": 3}}
    p.write_text("{not json")
    assert mod._load_cursors(p) == {}


def test_journal_bootstrap_uses_show_cursor_not_cursor_file(tmp_path, monkeypatch):
    """systemd 257 rejects --since with --cursor-file (rc=1); the first run must
    use --since + --show-cursor and persist the cursor itself."""
    import subprocess
    mod = _load_ingest()
    calls = []

    class P:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        p = P()
        if any(a.startswith("--cursor-file=") for a in cmd):
            assert "--since" not in cmd
            p.stdout = "2026-10-09T02:00:01+0200 host x[1]: second\n"
        else:
            p.stdout = "2026-10-09T02:00:00+0200 host x[1]: first\n-- cursor: s=abc\n"
        return p

    monkeypatch.setattr(subprocess, "run", fake_run)
    cf = tmp_path / "u.journal-cursor"
    assert mod._journal_new_lines("u", cf, window="6m", tail=10) == [
        "2026-10-09T02:00:00+0200 host x[1]: first"]
    assert cf.read_text() == "s=abc"
    assert mod._journal_new_lines("u", cf, window="6m", tail=10) == [
        "2026-10-09T02:00:01+0200 host x[1]: second"]


# --- QFLX-13 (d): zone-bearing sync.log stamps -------------------------------

def _load_listmonk_sync():
    spec = importlib.util.spec_from_file_location(
        "listmonk_sync", Path(__file__).resolve().parents[2] / "scripts" / "ops" / "listmonk-sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_listmonk_sync_log_has_explicit_utc_offset(capsys):
    import re
    mod = _load_listmonk_sync()
    mod.log("hello", "WARN")
    err = capsys.readouterr().err.strip()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00 listmonk-sync \[WARN\] hello$", err)


def test_listmonk_sync_log_roundtrips_through_parse_line_as_utc(capsys):
    from datetime import datetime, timezone
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "mcp"))
    import logs
    mod = _load_listmonk_sync()
    before = datetime.now(timezone.utc)
    mod.log("rt")
    rec = logs.parse_line(capsys.readouterr().err, source="sync.log")
    ts = datetime.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    assert abs((ts - before).total_seconds()) < 5
    assert rec["level"] == "INFO" and rec["message"] == "rt"


def test_format_change_does_not_reship_old_lines(tmp_path):
    """Cursor is inode+offset, not content: old-format lines already shipped stay
    shipped when new-format lines are appended after a format change."""
    mod = _load_ingest()
    f = tmp_path / "sync.log"
    f.write_text("2026-10-10 00:11:43,312 listmonk-sync [INFO] old format\n")
    raw, cur = mod._read_new_lines(str(f), None, tail=50)
    assert raw is None                         # bootstrap: cursor starts at EOF
    with open(f, "a") as fh:
        fh.write("2026-10-10T00:12:00.000+00:00 listmonk-sync [INFO] new format\n")
    raw, cur = mod._read_new_lines(str(f), cur, tail=50)
    assert raw == ["2026-10-10T00:12:00.000+00:00 listmonk-sync [INFO] new format"]
    raw, cur = mod._read_new_lines(str(f), cur, tail=50)
    assert raw == []                           # nothing re-sent
    with open(f, "a") as fh:
        fh.write("2026-10-10T00:13:00.000+00:00 listmonk-sync [INFO] next\n")
    raw, _ = mod._read_new_lines(str(f), cur, tail=50)
    assert raw == ["2026-10-10T00:13:00.000+00:00 listmonk-sync [INFO] next"]
