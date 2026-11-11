"""QFLX-123: the box-side roll-up REA ships for "Not paged this week: N - <names>".

The Python lives inside the bash heredoc that qflix-rea.ps1 sends over its one
SSH hop, so nothing else executes it before the box does. These tests extract
that exact block from the tracked script and run it against:
  * a synthetic ~/.opt/maint/weekly-events.jsonl in lib/paging.py's
    record_weekly() format, and
  * a synthetic kuma.db with Uptime Kuma's monitor / heartbeat / notification /
    monitor_notification tables (the only place non-paging Kuma DOWN edges
    exist: the container cannot reach the auto-heal webhook, so those edges
    never reach the jsonl).
A broken filter fails here, not in Monday's Discord message.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REA_PS1 = REPO_ROOT / "scripts" / "local-llm" / "qflix-rea.ps1"
NOW = datetime.now(timezone.utc)


def _rollup_source() -> str:
    text = REA_PS1.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    start = text.index("<<'PYEOF'")
    body = text[text.index("\n", start) + 1:]
    return body[:body.index("\nPYEOF\n")] + "\n"


def _run(tmp_path: Path, events: Path, db: Path) -> dict:
    script = tmp_path / "rollup.py"
    script.write_text(_rollup_source(), encoding="utf-8")
    out = subprocess.run([sys.executable, "-I", str(script), str(events), str(db)],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def _rec(ts: datetime, kind: str, monitor, state: str, msg: str = "m", origin="x.py") -> str:
    # Same shape as lib/paging.py record_weekly (sort_keys, default separators).
    return json.dumps({"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": kind,
                       "monitor": monitor, "origin": origin, "state": state,
                       "msg": msg}, sort_keys=True) + "\n"


def _kuma_ts(dt: datetime) -> str:
    # Kuma stores heartbeat.time as UTC "YYYY-MM-DD HH:MM:SS.mmm".
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + "123"


def _kuma_db(path: Path, beats) -> Path:
    """beats: (monitor_name, status, important, datetime). Monitor 'Plex' and
    'Seerr' carry the Discord channel (member-facing); the rest do not."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE monitor (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE heartbeat (id INTEGER PRIMARY KEY, monitor_id INTEGER,
                                status INTEGER, important INTEGER, time TEXT, msg TEXT);
        CREATE TABLE notification (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE monitor_notification (id INTEGER PRIMARY KEY,
                                           monitor_id INTEGER, notification_id INTEGER);
        INSERT INTO notification VALUES (1, 'Mission Control - QFlix'),
                                        (2, 'Manitoba auto-heal webhook');
    """)
    ids = {}
    for name, *_ in beats:
        if name not in ids:
            ids[name] = len(ids) + 1
            con.execute("INSERT INTO monitor VALUES (?, ?)", (ids[name], name))
            con.execute("INSERT INTO monitor_notification (monitor_id, notification_id) "
                        "VALUES (?, 2)", (ids[name],))
            if name in ("Plex", "Seerr"):
                con.execute("INSERT INTO monitor_notification (monitor_id, notification_id) "
                            "VALUES (?, 1)", (ids[name],))
    for name, status, important, dt in beats:
        con.execute("INSERT INTO heartbeat (monitor_id, status, important, time, msg) "
                    "VALUES (?, ?, ?, ?, 'SECRET-BEAT-MSG')",
                    (ids[name], status, important, _kuma_ts(dt)))
    con.commit()
    con.close()
    return path


def _events(tmp_path: Path, lines) -> Path:
    f = tmp_path / "weekly-events.jsonl"
    f.write_text("".join(lines), encoding="utf-8")
    return f


def test_both_sources_unreadable_is_not_a_zero(tmp_path):
    """No kuma.db and an unreadable file: 'unknown', never a reassuring 0."""
    d = tmp_path / "events-is-a-dir.jsonl"
    d.mkdir()
    r = _run(tmp_path, d, tmp_path / "no-kuma.db")
    assert r["ok"] is False


def test_jsonl_alone_still_counts_and_names_the_missing_kuma_db(tmp_path):
    f = _events(tmp_path, [
        _rec(NOW - timedelta(days=3), "notify", None, "warning", origin="qflix-reaper.py"),
        _rec(NOW - timedelta(days=9), "notify", "Old", "warning"),              # outside the week
        "not json at all\n",                                                   # skipped, not fatal
        _rec(NOW - timedelta(hours=2), "notify", "Canary Quota", "info",
             msg="SECRET-MSG-TEXT from a janitor"),
    ])
    r = _run(tmp_path, f, tmp_path / "no-kuma.db")
    assert r["ok"] is True and r["n"] == 2
    assert r["partial"] == "kuma.db unreadable"
    assert {e["name"] for e in r["names"]} == {"qflix-reaper.py", "Canary Quota"}
    assert "SECRET-MSG-TEXT" not in json.dumps(r), "msg must never leave the box"


def test_kuma_db_counts_important_downs_on_non_paging_monitors_only(tmp_path):
    db = _kuma_db(tmp_path / "kuma.db", [
        ("Canary Tdarr Scanner", 0, 1, NOW - timedelta(days=1)),   # counted
        ("Canary Tdarr Scanner", 1, 1, NOW - timedelta(days=1)),   # UP edge: no
        ("Canary Tdarr Scanner", 0, 0, NOW - timedelta(days=1)),   # not important: no
        ("Canary Tdarr Scanner", 0, 1, NOW - timedelta(days=2)),   # counted
        ("Canary Tdarr Scanner", 0, 1, NOW - timedelta(days=8)),   # last week: no
        ("Plex", 0, 1, NOW - timedelta(hours=5)),                  # pages (Discord): no
        ("Canary Movie", 0, 1, NOW - timedelta(hours=1)),          # counted
    ])
    r = _run(tmp_path, tmp_path / "absent.jsonl", db)
    assert r["ok"] is True and r["partial"] == ""
    assert r["names"] == [{"name": "Canary Tdarr Scanner", "count": 2},
                          {"name": "Canary Movie", "count": 1}]
    assert r["n"] == 3
    assert "SECRET-BEAT-MSG" not in json.dumps(r)


def test_merge_dedups_by_monitor_name_and_adds_notify_events(tmp_path):
    """If the webhook ever starts recording Kuma edges, the same DOWN must not
    count twice: per monitor, Kuma DOWNs = max(file, kuma.db). notify() events
    for the same name are separate signals and add on top."""
    db = _kuma_db(tmp_path / "kuma.db", [
        ("Canary Quota", 0, 1, NOW - timedelta(days=1)),
        ("Canary Quota", 0, 1, NOW - timedelta(days=2)),
    ])
    f = _events(tmp_path, [
        _rec(NOW - timedelta(days=1), "kuma", "Canary Quota", "down"),   # same edge, file side
        _rec(NOW - timedelta(days=1), "kuma", "Canary Quota", "up"),     # recovery: never counted
        _rec(NOW - timedelta(days=1), "notify", "Canary Quota", "warning"),
    ])
    r = _run(tmp_path, f, db)
    assert r["names"] == [{"name": "Canary Quota", "count": 3}]       # max(1,2) + 1 notify
    assert r["n"] == 3


def test_kuma_db_is_opened_read_only(tmp_path):
    db = _kuma_db(tmp_path / "kuma.db", [("Canary Movie", 0, 1, NOW)])
    before = db.read_bytes()
    _run(tmp_path, tmp_path / "absent.jsonl", db)
    assert db.read_bytes() == before
    src = _rollup_source()
    assert "mode=ro" in src
    assert 'r.get("msg")' not in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
