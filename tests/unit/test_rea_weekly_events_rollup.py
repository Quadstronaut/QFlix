"""QFLX-123: the box-side roll-up REA ships for "Not paged this week: N - <names>".

The Python lives inside the bash heredoc that qflix-rea.ps1 sends over its one
SSH hop, so nothing else executes it before the box does. This test extracts
that exact block from the tracked script and runs it against a synthetic
~/.opt/maint/weekly-events.jsonl written in lib/paging.py's record_weekly()
format, so a broken filter fails here and not in Monday's Discord message.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REA_PS1 = REPO_ROOT / "scripts" / "local-llm" / "qflix-rea.ps1"


def _rollup_source() -> str:
    text = REA_PS1.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    start = text.index("<<'PYEOF'")
    body = text[text.index("\n", start) + 1:]
    return body[:body.index("\nPYEOF\n")] + "\n"


def _run(tmp_path: Path, events_file: Path) -> dict:
    script = tmp_path / "rollup.py"
    script.write_text(_rollup_source(), encoding="utf-8")
    out = subprocess.run([sys.executable, "-I", str(script), str(events_file)],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def _rec(ts: datetime, kind: str, monitor, state: str, msg: str = "m", origin="x.py") -> str:
    # Same shape as lib/paging.py record_weekly (sort_keys, default separators).
    return json.dumps({"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": kind,
                       "monitor": monitor, "origin": origin, "state": state,
                       "msg": msg}, sort_keys=True) + "\n"


def test_absent_file_is_reported_not_zero(tmp_path):
    """No file = #160 not deployed yet: 'unknown', never a reassuring 0."""
    assert _run(tmp_path, tmp_path / "missing.jsonl") == {"ok": False, "why": "absent"}


def test_counts_last_7_days_names_only_and_skips_recoveries(tmp_path):
    now = datetime.now(timezone.utc)
    f = tmp_path / "weekly-events.jsonl"
    f.write_text("".join([
        _rec(now - timedelta(days=1), "kuma", "Canary Tdarr Scanner", "down"),
        _rec(now - timedelta(days=1, hours=-1), "kuma", "Canary Tdarr Scanner", "up"),   # recovery: not counted
        _rec(now - timedelta(days=2), "kuma", "Canary Tdarr Scanner", "down"),
        _rec(now - timedelta(days=3), "notify", None, "warning", origin="qflix-reaper.py"),
        _rec(now - timedelta(days=9), "kuma", "Old Monitor", "down"),                   # outside the week
        "not json at all\n",                                                            # skipped, not fatal
        _rec(now - timedelta(hours=2), "notify", "Canary Quota", "info",
             msg="SECRET-MSG-TEXT from a janitor"),
    ]), encoding="utf-8")
    r = _run(tmp_path, f)
    assert r["ok"] is True
    assert r["n"] == 4
    assert r["names"] == [{"name": "Canary Tdarr Scanner", "count": 2},
                          {"name": "qflix-reaper.py", "count": 1},
                          {"name": "Canary Quota", "count": 1}]
    blob = json.dumps(r)
    assert "SECRET-MSG-TEXT" not in blob, "msg must never leave the box"


def test_rollup_block_is_outside_sources_and_never_reads_msg():
    src = _rollup_source()
    assert 'r.get("msg")' not in src
    assert "timedelta(days=7)" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
