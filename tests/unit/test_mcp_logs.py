"""Tests for scripts/mcp/logs.py — log routing logic."""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "mcp"))

import logs  # noqa: E402


def test_route_for_ucc_app():
    plan = logs.route("sonarr")
    assert plan["kind"] == "file"
    assert ".apps" in plan["path"] and "sonarr" in plan["path"] and "sonarr.txt" in plan["path"]


def test_route_for_systemd_app():
    # maint-window really is StandardOutput=journal. listmonk/tdarr-* are NOT
    # (StandardOutput=append:<file>) and were moved to _FILE_LOGS 2026-08-23 —
    # see test_append_stdout_apps_route_to_files below.
    plan = logs.route("maint-window")
    assert plan["kind"] == "journalctl"
    assert plan["unit"] == "manitoba-maint-window.service"


def test_append_stdout_apps_route_to_files():
    """listmonk / tdarr-server / tdarr-node write stdout to files, so journalctl
    only ever returned systemd's own start/stop lines for them. Routing them to
    journalctl reported them permanently `dark` in the collector's log-coverage
    ledger while their real logs sat on disk, uncollected."""
    for app, needle in (("listmonk", "listmonk.log"),
                        ("tdarr-server", "server.log"),
                        ("tdarr-node", "node.log")):
        plan = logs.route(app)
        assert plan["kind"] == "file", app
        assert needle in plan["path"], app


def test_route_for_nginx():
    plan = logs.route("nginx")
    assert plan["kind"] == "file"
    assert "logs" in plan["path"] and "error.log" in plan["path"]


def test_parse_line_iso():
    line = "2026-05-12T07:00:13Z [Info] Sonarr.Some.Class - Doing thing"
    parsed = logs.parse_line(line, source="sonarr.txt")
    assert parsed["ts"].startswith("2026-05-12")
    # _normalize_level upper-cases everything; "Info" → "INFO".
    assert parsed["level"] == "INFO"
    assert "Doing thing" in parsed["message"]


def test_parse_line_tdarr_colourised_file():
    """Tdarr writes raw SGR escapes into its log FILE, not just to a tty. Every
    _TS_PATTERNS entry is ^-anchored, so an unstripped escape left the whole
    file ts=None/level=unknown — and collect_for's carry-forward then hands
    those lines the INGEST clock, resurfacing old content as phantom-recent
    errors. Strip SGR and the existing Kometa bracket-ts pattern matches."""
    line = "\x1b[33m[2026-08-23T20:36:48.935] [WARN] Tdarr_Server - \x1b[39mExit approved."
    parsed = logs.parse_line(line, source="server.log")
    assert parsed["ts"] == "2026-08-23T20:36:48.935"
    assert parsed["level"] == "WARN"
    assert parsed["message"] == "Tdarr_Server - Exit approved."


def test_parse_line_go_stdlib_ts():
    """listmonk logs via Go's stdlib logger: slash-dated, no level token. The
    real timestamp is the point — it is what keeps these lines out of the
    phantom-recency path above. `unknown` level is the honest read; the source
    emits none."""
    line = "2026/08/23 02:00:04.753761 maintenance.go:95: finished VACUUM"
    parsed = logs.parse_line(line, source="listmonk.log")
    assert parsed["ts"] == "2026-08-23T02:00:04.753761"
    assert parsed["level"] == "unknown"
    assert "finished VACUUM" in parsed["message"]


def test_parse_line_unknown_format():
    parsed = logs.parse_line("garbage", source="x")
    assert parsed["level"] == "unknown"
    assert parsed["message"] == "garbage"


def test_collect_for_carries_ts_to_continuation_lines(monkeypatch):
    # A timestamped error followed by untimestamped stack-trace/continuation
    # lines: each continuation must INHERIT the error line's ts (not None), so
    # the vlogs ingester can't restamp re-tailed old blocks with ingest-time
    # and resurrect them as phantom "recent" errors (the 2026-06-25 SAB/Plex
    # false-residual that misled the council).
    raw = [
        "2026-06-23 04:55:11.1|Error|DownloadClientCheck|Unable to connect to SABnzbd",
        "  ---> System.Net.Http.HttpRequestException: Connection refused (127.0.0.1:17007)",
        "   at NzbDrone.Core.Download.Clients.Sabnzbd.SabnzbdProxy.ProcessRequest()",
        "2026-06-23 04:55:12.0|Info|RssSyncService|RSS Sync Completed.",
    ]
    monkeypatch.setattr(logs, "route", lambda app: {"kind": "file", "path": "/x/sonarr.txt"})
    monkeypatch.setattr(logs, "_tail_file", lambda path, n: raw)
    lines = logs.collect_for("sonarr", since="5m", tail=100)["lines"]
    assert len(lines) == 4
    err_ts = lines[0]["ts"]
    assert err_ts and err_ts.startswith("2026-06-23")
    assert lines[1]["ts"] == err_ts          # continuation inherits parent ts
    assert lines[2]["ts"] == err_ts
    assert "127.0.0.1:17007" in lines[1]["message"]
    assert lines[3]["ts"].startswith("2026-06-23") and lines[3]["level"] == "INFO"


LOGS_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mcp" / "logs.py"


def test_journalctl_drops_no_entries_placeholder():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "-- No entries --\n"
    with patch.object(logs.subprocess, "run", return_value=fake):
        out = logs._journalctl("whatever.service", "5m", 100)
    assert out == []


def test_journalctl_keeps_real_lines_when_placeholder_mixed():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = (
        "2026-05-15T10:11:12+0000 seedbox foo: started\n"
        "-- No entries --\n"
        "2026-05-15T10:11:13+0000 seedbox foo: working\n"
    )
    with patch.object(logs.subprocess, "run", return_value=fake):
        out = logs._journalctl("foo.service", "5m", 100)
    assert len(out) == 2
    assert "started" in out[0] and "working" in out[1]


def test_list_apps_returns_route_tables():
    proc = subprocess.run(
        ["python3", str(LOGS_SCRIPT), "--emit-json", "--list-apps"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert "file_apps" in data and "systemd_apps" in data
    assert "sonarr" in data["file_apps"]
    assert "radarr" in data["file_apps"]
    assert "listmonk" in data["file_apps"]
    assert "maint-window" in data["systemd_apps"]
    assert data["file_apps"] == sorted(data["file_apps"])
    assert data["systemd_apps"] == sorted(data["systemd_apps"])


def test_collect_for_file_route_honours_since(monkeypatch, tmp_path):
    """A file-routed app whose log has not been appended to inside the window
    must come back EMPTY, exactly as the journalctl branch does.

    Before this, `--since` reached only the journalctl branch. Anything grading
    an app on `len(lines)` -- qflix-collect.py's log-coverage ledger -- read
    every file-routed app as live for as long as its log file existed and was
    non-empty, which for an append-only file systemd never truncates is
    forever. Moving listmonk/tdarr-server/tdarr-node onto that branch would
    have traded a permanent false DARK for a permanent false LIVE.
    """
    log = tmp_path / "server.log"
    log.write_text("2026-08-23T04:00:00 INFO old line\n", encoding="utf-8")
    import os
    stale = os.path.getmtime(log) - 90000          # ~25h in the past
    os.utime(log, (stale, stale))
    monkeypatch.setattr(logs, "route",
                        lambda app: {"kind": "file", "path": str(log)})
    assert logs.collect_for("tdarr-server", since="24h", tail=50)["lines"] == []
    # Same file, a window wide enough to contain it: the lines come back.
    assert logs.collect_for("tdarr-server", since="7d", tail=50)["lines"]


def test_since_seconds_falls_back_to_24h_on_garbage():
    """A malformed --since must narrow to the default, never widen to
    'everything, forever' -- that would silently disable the dormant gate."""
    assert logs._since_seconds("30m") == 1800
    assert logs._since_seconds("2d") == 172800
    assert logs._since_seconds("") == 86400
    assert logs._since_seconds("banana") == 86400
