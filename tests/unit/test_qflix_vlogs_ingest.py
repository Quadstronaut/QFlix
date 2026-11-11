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


def test_line_ids_distinguish_repeated_text_and_follow_tail(tmp_path):
    """QFLX-51: identical text at two offsets gets two ids; the ids stay
    parallel to the returned lines when --tail trims the backlog, and reach the
    parsed records (blank lines are skipped but do not shift the ids)."""
    mod = _load_ingest()
    f = tmp_path / "radarr.txt"
    f.write_text("seed\n")
    _, cur = mod._read_new_lines(str(f), None, tail=50)
    with open(f, "a") as fh:
        fh.write("2026-10-10 06:44:28.2|Info|X|same\n\n2026-10-10 06:44:28.2|Info|X|same\n")
    ids: list = []
    raw, cur = mod._read_new_lines(str(f), cur, tail=50, ids=ids)
    assert len(raw) == len(ids) == 3
    assert len(set(ids)) == 3
    recs, _ = mod._parse_with_carry(raw, source=str(f), last_ts=None, ids=ids)
    assert [r["line_id"] for r in recs] == [ids[0], ids[2]]
    # tail trims both lists the same way
    with open(f, "a") as fh:
        fh.write("a\nb\nc\n")
    ids2: list = []
    raw2, _ = mod._read_new_lines(str(f), cur, tail=2, ids=ids2)
    assert raw2 == ["b", "c"] and len(ids2) == 2
    assert ids2[1].split(":")[1] == str(f.read_bytes().rindex(b"c"))


# ── QFLX-116: a retry after an ambiguous POST failure must not re-ship ─────────

def _rows(*ids, src="/l/sonarr.txt"):
    return [{"message": f"m{i}", "ts": "2026-11-11T00:03:48.4Z", "level": "info",
             "source_file": src, "line_id": i} for i in ids]


def test_retry_dedup_drops_only_stored_ids():
    ingest = _load_ingest()
    seen = []

    def lookup(port, app, src, ids, stamps):
        seen.append((app, src, list(ids), list(stamps)))
        return {"1:10"}

    keep, dropped = ingest._drop_already_stored(9, "sonarr", _rows("1:10", "1:85"),
                                                lookup=lookup)
    assert [r["line_id"] for r in keep] == ["1:85"]
    assert dropped == 1
    assert seen == [("sonarr", "/l/sonarr.txt", ["1:10", "1:85"],
                     ["2026-11-11T00:03:48.4Z"] * 2)]


def test_retry_dedup_lookup_failure_sends_everything():
    ingest = _load_ingest()
    rows = _rows("1:10", "1:85")
    keep, dropped = ingest._drop_already_stored(9, "sonarr", rows,
                                                lookup=lambda *a: None)
    assert keep == rows and dropped is None


def test_retry_dedup_rows_without_line_id_are_kept():
    ingest = _load_ingest()
    rows = [{"message": "x", "source_file": "journalctl:u"}]
    keep, dropped = ingest._drop_already_stored(9, "maint", rows,
                                                lookup=lambda *a: set())
    assert keep == rows and dropped == 0


def test_already_stored_queries_app_file_and_ids(monkeypatch):
    import io
    import urllib.parse
    ingest = _load_ingest()
    captured = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        form = urllib.parse.parse_qs(req.data.decode())
        captured.update(query=form["query"][0], start=form["start"][0], end=form["end"][0])
        return _Resp(b'{"line_id":"1:10"}\n')

    monkeypatch.setattr(ingest.urllib.request, "urlopen", fake_urlopen)
    got = ingest._already_stored(9, "sonarr", "/l/sonarr.txt", ["1:10", "1:85"],
                                 ["2026-11-11T00:03:48.4Z", "2026-11-11T00:05:01Z"])
    assert got == {"1:10"}
    # Time-bounded: an unbounded query scans the whole index and times out.
    assert (captured["start"], captured["end"]) == ("2026-11-11T00:01:48Z",
                                                    "2026-11-11T00:07:01Z")
    q = captured["query"]
    assert q.startswith('{app="sonarr"} source_file:="/l/sonarr.txt" line_id:in("1:10","1:85")')


def test_already_stored_unreachable_is_none(monkeypatch):
    ingest = _load_ingest()

    def boom(req, timeout=None):
        raise ingest.urllib.error.URLError("down")

    monkeypatch.setattr(ingest.urllib.request, "urlopen", boom)
    assert ingest._already_stored(9, "sonarr", "/l/s.txt", ["1:1"]) is None


def test_failed_post_flags_cursor_and_retry_dedups(tmp_path, monkeypatch):
    """End to end through main(): POST times out after vlogs stored the batch;
    the next cycle sends nothing twice and clears the flag."""
    ingest = _load_ingest()
    log = tmp_path / "sonarr.txt"
    log.write_bytes(b"")
    st = os.stat(log)
    state = tmp_path / "state"
    monkeypatch.setattr(ingest, "STATE_DIR", state)
    ingest._save_cursors(state / "cursors.json", {"sonarr": {
        "inode": st.st_ino, "offset": 0, "last_ts": None, "path": str(log)}})
    log.write_bytes(b"2026-11-11 01:03:48.4|Info|A|one\n2026-11-11 01:03:48.4|Info|A|two\n")
    monkeypatch.setattr(ingest, "_read_port", lambda: 9)
    monkeypatch.setattr(ingest.logs_mod, "_FILE_LOGS", {"sonarr": str(log)})
    monkeypatch.setattr(ingest.logs_mod, "_GLOB_LOGS", {}, raising=False)
    monkeypatch.setattr(ingest.logs_mod, "_SYSTEMD_LOGS", {})
    monkeypatch.setattr(ingest.logs_mod, "route",
                        lambda app: {"kind": "file", "path": str(log)})
    store: list[str] = []
    calls = {"n": 0}

    def fake_post(port, app, lines):
        calls["n"] += 1
        store.extend(ln["line_id"] for ln in lines)
        if calls["n"] == 1:
            return False, "timed out"      # stored, but the answer was late
        return True, f"{len(lines)} lines"

    monkeypatch.setattr(ingest, "_post_jsonline", fake_post)
    monkeypatch.setattr(ingest, "_already_stored",
                        lambda port, app, src, ids, stamps: {i for i in ids if i in store})
    import sys
    monkeypatch.setattr(sys, "argv", ["ingest"])
    ingest.main()
    cur = ingest._load_cursors(state / "cursors.json")["sonarr"]
    assert cur["unconfirmed"] is True and cur["offset"] == 0
    ingest.main()
    cur = ingest._load_cursors(state / "cursors.json")["sonarr"]
    assert "unconfirmed" not in cur and cur["offset"] == log.stat().st_size
    assert len(store) == len(set(store)) == 2
