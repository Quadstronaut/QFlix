"""QFLX-126: the weekly "Not paged this week" roll-up, run for real.

The roll-up is a python program embedded in the bash heredoc that
scripts/local-llm/qflix-rea.ps1 sends to the box. Nothing executed it in CI:
the pwsh suite only asserts that certain strings are present. This test lifts
the program out of the ps1 byte-for-byte and runs it against a synthetic
kuma.db and weekly-events.jsonl.

What it pins:
  * a non-paging monitor that went DOWN more than 7 days ago and is STILL down
    has no edge in the window (n == 0) but is named in `down_now`;
  * a monitor that recovered, a paused monitor and a monitor that pages Discord
    are not in `down_now`;
  * info-level notify records are counted in `routine`, not named.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PS1 = ROOT / "scripts" / "local-llm" / "qflix-rea.ps1"


def _rollup_source() -> str:
    text = PS1.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("<<'PYEOF' 2>/dev/null | base64 -w0\n") + len("<<'PYEOF' 2>/dev/null | base64 -w0\n")
    end = text.index("\nPYEOF\n", start)
    return text[start:end] + "\n"


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _make_db(path: Path, now: datetime) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE monitor (id INTEGER PRIMARY KEY, name TEXT, active INTEGER);"
        "CREATE TABLE heartbeat (id INTEGER PRIMARY KEY, monitor_id INTEGER, status INTEGER,"
        " important INTEGER, time TEXT);"
        "CREATE TABLE notification (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE monitor_notification (monitor_id INTEGER, notification_id INTEGER);"
    )
    con.execute("INSERT INTO notification VALUES (1, 'Mission Control - QFlix')")
    mons = [
        (1, "Canary SAB Stall", 1),       # DOWN 10 days ago, still DOWN, not paged
        (2, "Canary Timer Liveness", 1),  # DOWN 3 days ago, recovered
        (3, "Plex", 1),                   # DOWN now, but it pages Discord
        (4, "Old Thing", 0),              # DOWN, but paused
        (5, "Sonarr", 1),                 # always up
    ]
    con.executemany("INSERT INTO monitor VALUES (?, ?, ?)", mons)
    con.execute("INSERT INTO monitor_notification VALUES (3, 1)")
    d = lambda **kw: _fmt(now - timedelta(**kw))  # noqa: E731
    beats = [
        # (monitor_id, status, important, time)
        (1, 1, 0, d(days=11)),
        (1, 0, 1, d(days=10)),          # the one DOWN edge, outside the 7-day window
        (1, 0, 0, d(days=5)),           # still down: ordinary beats are not "important"
        (1, 0, 0, d(minutes=5)),
        (2, 0, 1, d(days=3)),
        (2, 1, 1, d(days=3, hours=-1)),
        (2, 1, 0, d(minutes=5)),
        (3, 0, 1, d(hours=2)),
        (3, 0, 0, d(minutes=5)),
        (4, 0, 1, d(days=9)),
        (5, 1, 0, d(minutes=5)),
    ]
    con.executemany(
        "INSERT INTO heartbeat (monitor_id, status, important, time) VALUES (?, ?, ?, ?)", beats)
    con.commit()
    con.close()


def _run(tmp_path: Path, events: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    db = tmp_path / "kuma.db"
    _make_db(db, now)
    ev = tmp_path / "weekly-events.jsonl"
    ev.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    prog = tmp_path / "rollup.py"
    prog.write_text(_rollup_source(), encoding="utf-8")
    out = subprocess.run([sys.executable, str(prog), str(ev), str(db)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _ts(days: float = 1.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_embedded_program_is_found_and_is_python():
    src = _rollup_source()
    compile(src, "rollup", "exec")
    assert "down_now" in src


def test_a_monitor_red_for_more_than_a_week_is_named_even_with_zero_edges(tmp_path):
    r = _run(tmp_path, [])
    assert r["ok"] is True
    names = {e["name"]: e["count"] for e in r["names"]}
    # Edge count: only the 3-day-old flap is inside the window.
    assert names == {"Canary Timer Liveness": 1}, names
    assert "Canary SAB Stall" not in names, "the 10-day-old edge is outside the window"
    # State: the still-red monitor is reported, with the time it went down.
    down = {e["name"]: e["since"] for e in r["down_now"]}
    assert list(down) == ["Canary SAB Stall"], down
    expected = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    assert down["Canary SAB Stall"].startswith(expected), down


def test_recovered_paused_and_paging_monitors_are_not_listed_as_down(tmp_path):
    down = {e["name"] for e in _run(tmp_path, [])["down_now"]}
    assert "Canary Timer Liveness" not in down      # recovered
    assert "Old Thing" not in down                  # paused (active = 0)
    assert "Plex" not in down                       # pages Discord already
    assert "Sonarr" not in down


def test_info_level_notices_are_counted_apart_from_named_events(tmp_path):
    events = (
        [{"ts": _ts(), "kind": "notify", "origin": "qflix-reaper.py", "state": "info", "msg": "x"}] * 7
        + [{"ts": _ts(), "kind": "notify", "origin": "quality_fallback.py", "state": "warning", "msg": "x"}] * 2
        + [{"ts": _ts(), "kind": "notify", "origin": "arr-housekeeping.py", "state": "error", "msg": "x"}]
        + [{"ts": _ts(9), "kind": "notify", "origin": "old.py", "state": "warning", "msg": "x"}]
    )
    r = _run(tmp_path, events)
    names = {e["name"]: e["count"] for e in r["names"]}
    assert r["routine"] == 7
    assert "qflix-reaper.py" not in names
    assert names["quality_fallback.py"] == 2 and names["arr-housekeeping.py"] == 1
    assert "old.py" not in names
    assert r["n"] == sum(names.values())


def test_unreadable_kuma_db_is_named_partial_and_reports_nobody_down(tmp_path):
    ev = tmp_path / "weekly-events.jsonl"
    ev.write_text("", encoding="utf-8")
    prog = tmp_path / "rollup.py"
    prog.write_text(_rollup_source(), encoding="utf-8")
    out = subprocess.run([sys.executable, str(prog), str(ev), str(tmp_path / "absent.db")],
                         capture_output=True, text=True, timeout=60)
    r = json.loads(out.stdout)
    assert r["ok"] is True and "kuma.db unreadable" in r["partial"]
    assert r["down_now"] == []


def test_output_survives_the_base64_hop_the_heredoc_uses(tmp_path):
    r = _run(tmp_path, [])
    again = json.loads(base64.b64decode(base64.b64encode(json.dumps(r).encode())).decode())
    assert again == r
