"""Tests for MCP tools (call the underlying functions directly).

The MCP framework registration is exercised in the acceptance test.
"""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "local" / "qflix-mcp"))

import qflix_mcp  # noqa: E402
from lib.cache import Cache, atomic_write_json  # noqa: E402


def _seed_snapshot(root: Path, hour: int, content: dict) -> None:
    snaps = root / "snapshots" / "2026-05-12"
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / f"{hour:02d}.json").write_text(json.dumps(content))


def test_status_with_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    out = qflix_mcp.qflix_status()
    assert out["latest_snapshot"] is None
    assert out["torrent_count"] == 0


def test_status_returns_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    _seed_snapshot(tmp_path, 1, {
        "captured_at": "2026-05-12T01:00:00Z",
        "qbit": {"torrents": [{"hash": "a"}, {"hash": "b"}], "totals": {}},
        "health": {"kuma_red": ["X"], "zombies": []},
    })
    out = qflix_mcp.qflix_status()
    assert out["torrent_count"] == 2
    assert out["kuma_red"] == ["X"]


def test_list_torrents_returns_torrents(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    _seed_snapshot(tmp_path, 1, {
        "captured_at": "2026-05-12T01:00:00Z",
        "qbit": {"torrents": [{"hash": "a", "name": "Foo"}]},
    })
    out = qflix_mcp.qflix_list_torrents()
    assert out[0]["hash"] == "a"


def test_torrent_history_finds_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    for h in (0, 1):
        _seed_snapshot(tmp_path, h, {
            "captured_at": f"2026-05-12T{h:02d}:00:00Z",
            "qbit": {"torrents": [{"hash": "abc", "downloaded_bytes": 100 + h,
                                    "progress": 0.1, "dl_speed_bytes_s": 0,
                                    "state": "stalledDL"}]},
        })
    hist = qflix_mcp.qflix_torrent_history("abc", hours=24)
    assert len(hist) == 2


def test_list_stale_returns_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    state = {"hashes": {"abc": {"candidate_for_unstick": True,
                                 "consecutive_zero_hours": 3,
                                 "rule_matched": "stalledDL"}},
             "updated_at": "2026-05-12T01:00:00Z"}
    (tmp_path / "stale-state.json").write_text(json.dumps(state))
    out = qflix_mcp.qflix_list_stale()
    assert "abc" in {h["hash"] for h in out}


@patch("qflix_mcp.ssh_call")
def test_get_logs_returns_parsed_lines(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "app": "sonarr",
        "source": "/home/q/.apps/sonarr/logs/sonarr.txt",
        "lines": [
            {"ts": "2026-05-13T10:00:00Z", "level": "Info",
             "message": "hello", "source_file": "sonarr.txt"},
            {"ts": "2026-05-13T10:01:00Z", "level": "Error",
             "message": "boom", "source_file": "sonarr.txt"},
        ],
    })
    fake.stderr = ""
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_get_logs(app="sonarr", since="6h", tail=50)
    assert out["app"] == "sonarr"
    assert len(out["lines"]) == 2
    cmd = mock_ssh.call_args[0][0]
    assert "logs.py" in cmd and "--app sonarr" in cmd
    assert "--since 6h" in cmd and "--tail 50" in cmd


@patch("qflix_mcp.ssh_call")
def test_get_logs_applies_grep_client_side(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "app": "sonarr", "source": "x",
        "lines": [
            {"ts": "t", "level": "Info", "message": "hello world",
             "source_file": "x"},
            {"ts": "t", "level": "Error", "message": "boom",
             "source_file": "x"},
        ],
    })
    fake.stderr = ""
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_get_logs(app="sonarr", grep="boom")
    assert len(out["lines"]) == 1
    assert out["lines"][0]["message"] == "boom"


@patch("qflix_mcp.ssh_call")
def test_get_logs_handles_unsupported_app(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({"app": "bogus", "error": "unsupported", "lines": []})
    fake.stderr = ""
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_get_logs(app="bogus")
    assert out.get("error") == "unsupported"
    assert out["lines"] == []


def test_plex_libraries(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    _seed_snapshot(tmp_path, 1, {
        "captured_at": "x",
        "plex": {"libraries": [{"key": "1", "title": "Movies", "count": 100}]},
    })
    out = qflix_mcp.qflix_plex_libraries()
    assert out["libraries"][0]["title"] == "Movies"


def test_recent_events(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    events = tmp_path / "events"
    events.mkdir()
    (events / "2026-05-12.jsonl").write_text(
        '{"ts":"2026-05-12T01:00:00Z","action":"unstick","slug":"sonarr"}\n'
    )
    out = qflix_mcp.qflix_recent_events(n=10)
    assert out[0]["action"] == "unstick"


def test_arr_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(qflix_mcp, "DATA_ROOT", tmp_path)
    _seed_snapshot(tmp_path, 1, {
        "captured_at": "x",
        "arrs": {"sonarr": {"queue": [{"id": 1, "title": "X",
                                       "trackedDownloadState": "downloading"}],
                            "missing_count": 5}},
    })
    out = qflix_mcp.qflix_arr_queue("sonarr")
    assert out["queue"][0]["id"] == 1
    assert out["missing_count"] == 5


@patch("qflix_mcp.ssh_call")
def test_list_log_apps_returns_routes(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = '{"file_apps": ["sonarr", "radarr"], "systemd_apps": ["listmonk"]}'
    fake.stderr = ""
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_list_log_apps()
    assert out == {"file_apps": ["sonarr", "radarr"], "systemd_apps": ["listmonk"]}
    cmd = mock_ssh.call_args[0][0]
    assert "logs.py" in cmd and "--list-apps" in cmd


@patch("qflix_mcp.ssh_call")
def test_list_log_apps_ssh_timeout(mock_ssh):
    fake = MagicMock()
    fake.returncode = 124
    fake.stdout = ""
    fake.stderr = "ssh-timeout after 30s"
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_list_log_apps()
    assert out["status"] == "ssh-timeout"
    assert out["timeout_s"] == 30


@patch("qflix_mcp.ssh_call")
def test_diagnose_unstick_returns_timings(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "status": "diagnose", "slug": "radarr", "hash": "abc",
        "phases": {"state_read_ms": 0.5, "queue_lookup_paged_ms": 47000.0,
                    "queue_lookup_default_ms": 800.0, "hash_match_ms": 850.0},
        "queue_size_paged": 50, "queue_size_default": 50,
    })
    fake.stderr = ""
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_diagnose_unstick(slug="radarr", hash_="abc")
    assert out["status"] == "diagnose"
    assert out["phases"]["queue_lookup_paged_ms"] == 47000.0
    cmd = mock_ssh.call_args[0][0]
    assert "--diagnose" in cmd and "--slug radarr" in cmd and "--hash abc" in cmd
    # 180s timeout passed
    assert mock_ssh.call_args[1]["timeout"] == 180


@patch("qflix_mcp.ssh_call")
def test_unstick_torrent_returns_ssh_timeout_struct(mock_ssh):
    fake = MagicMock()
    fake.returncode = 124
    fake.stdout = ""
    fake.stderr = "ssh-timeout after 120s"
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_unstick_torrent(slug="radarr", hash_="abc")
    assert out["status"] == "ssh-timeout"
    assert out["timeout_s"] == 120


@patch("qflix_mcp.ssh_call")
def test_unstick_torrent_accepts_custom_timeout(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = '{"status": "deleted+blocklisted"}'
    fake.stderr = ""
    mock_ssh.return_value = fake
    qflix_mcp.qflix_unstick_torrent(slug="radarr", hash_="abc", timeout=200)
    assert mock_ssh.call_args[1]["timeout"] == 200


@patch("qflix_mcp.ssh_call")
def test_refresh_collect_returns_ssh_timeout_struct(mock_ssh):
    fake = MagicMock()
    fake.returncode = 124
    fake.stdout = ""
    fake.stderr = "ssh-timeout after 90s"
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_refresh_collect()
    assert out["status"] == "ssh-timeout"
    assert out["timeout_s"] == 90


# ===== VictoriaLogs integration =========================================
# vlogs now lives on the seedbox; MCP reaches it via SSH-exec'd curl.

@patch("qflix_mcp.ssh_call")
def test_query_logs_returns_parsed_entries(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = (
        '{"_time":"2026-05-13T10:00:00Z","_msg":"hello","app":"sonarr"}\n'
        '{"_time":"2026-05-13T10:01:00Z","_msg":"boom","app":"sonarr"}\n'
    )
    fake.stderr = ""
    mock_ssh.return_value = fake

    out = qflix_mcp.qflix_query_logs(query="app:sonarr", start="1h", limit=50)
    assert out["count"] == 2
    assert out["entries"][1]["_msg"] == "boom"
    # ssh_call was invoked with a curl command that targets the LogsQL endpoint
    # and reads the port from the seedbox-side secrets file.
    cmd = mock_ssh.call_args[0][0]
    assert "/select/logsql/query" in cmd
    assert "~/secrets/vlogs.port" in cmd
    assert "query=app:sonarr" in cmd
    assert "start=1h" in cmd
    assert "limit=50" in cmd


@patch("qflix_mcp.ssh_call")
def test_query_logs_handles_vlogs_unreachable(mock_ssh):
    # curl exits 7 on connection refused — surface as vlogs-unreachable.
    fake = MagicMock()
    fake.returncode = 7
    fake.stdout = ""
    fake.stderr = "curl: (7) Failed to connect to 127.0.0.1 port"
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_query_logs(query="*")
    assert out["status"] == "vlogs-unreachable"
    assert "Failed to connect" in out["error"]


@patch("qflix_mcp.ssh_call")
def test_query_logs_handles_missing_port_secret(mock_ssh):
    # Remote stub exits 3 when ~/secrets/vlogs.port is missing.
    fake = MagicMock()
    fake.returncode = 3
    fake.stdout = ""
    fake.stderr = "vlogs-port-missing"
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_query_logs(query="*")
    assert out["status"] == "vlogs-unreachable"
    assert "vlogs.port" in out["error"]


@patch("qflix_mcp.ssh_call")
def test_query_logs_handles_ssh_timeout(mock_ssh):
    fake = MagicMock()
    fake.returncode = 124
    fake.stdout = ""
    fake.stderr = "ssh-timeout after 15s"
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_query_logs(query="*")
    assert out["status"] == "ssh-timeout"
    assert out["timeout_s"] == 15
