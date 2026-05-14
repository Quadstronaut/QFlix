"""Tests for scripts/mcp/lib/arr_client.py."""
from __future__ import annotations
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "mcp"))

from lib.arr_client import ArrClient  # noqa: E402


def _resp(body, status=200):
    m = MagicMock()
    m.status = status
    m.read.return_value = (body if isinstance(body, str) else json.dumps(body)).encode()
    m.__enter__.return_value = m
    return m


def _secrets(tmp: Path) -> Path:
    s = tmp / "secrets"
    s.mkdir()
    (s / "sonarr.key").write_text("KEY")
    (s / "sonarr.port").write_text("17026")
    (s / "sonarr.urlbase").write_text("sonarr")
    return s


@patch("urllib.request.urlopen")
def test_get_queue(mock_open, tmp_path):
    mock_open.return_value = _resp({"records": [{"id": 1, "title": "X"}]})
    c = ArrClient("sonarr", "v3", secrets_dir=_secrets(tmp_path))
    code, payload = c.get("/queue", query="pageSize=500")
    assert code == 200
    assert payload["records"][0]["id"] == 1


@patch("urllib.request.urlopen")
def test_post_command(mock_open, tmp_path):
    mock_open.return_value = _resp({"id": 99, "name": "MissingEpisodeSearch"}, status=201)
    c = ArrClient("sonarr", "v3", secrets_dir=_secrets(tmp_path))
    code, payload = c.post("/command", body={"name": "MissingEpisodeSearch"})
    assert code == 201
    assert payload["id"] == 99


@patch("urllib.request.urlopen")
def test_delete_with_query(mock_open, tmp_path):
    mock_open.return_value = _resp("", status=200)
    c = ArrClient("sonarr", "v3", secrets_dir=_secrets(tmp_path))
    code, _ = c.delete("/queue/42", query="removeFromClient=true&blocklist=true")
    assert code == 200
    sent = mock_open.call_args[0][0]
    assert sent.full_url.endswith("/sonarr/api/v3/queue/42?removeFromClient=true&blocklist=true")
    assert sent.method == "DELETE"


def _setup_secrets(tmp_path: Path) -> Path:
    s = tmp_path / "secrets"
    s.mkdir()
    (s / "sonarr.key").write_text("K")
    (s / "sonarr.port").write_text("17026")
    (s / "sonarr.urlbase").write_text("sonarr")
    return s


@patch("urllib.request.urlopen")
def test_arr_client_passes_explicit_timeout(mock_open, tmp_path):
    resp = _resp({"records": []})
    mock_open.return_value = resp
    c = ArrClient("sonarr", "v3", secrets_dir=_setup_secrets(tmp_path), timeout=7)
    c.get("/queue")
    _, kwargs = mock_open.call_args
    assert kwargs.get("timeout") == 7


@patch("urllib.request.urlopen")
def test_arr_client_default_timeout_unchanged(mock_open, tmp_path):
    resp = _resp({})
    mock_open.return_value = resp
    c = ArrClient("sonarr", "v3", secrets_dir=_setup_secrets(tmp_path))
    c.get("/queue")
    _, kwargs = mock_open.call_args
    assert kwargs.get("timeout") == 20


@patch("lib.arr_client.urllib.request.urlopen")
def test_arr_client_instance_default_used_when_no_explicit_arg(mock_open, tmp_path):
    resp = _resp({})
    mock_open.return_value = resp
    c = ArrClient("sonarr", "v3",
                   secrets_dir=_setup_secrets(tmp_path), timeout=15)
    c.get("/queue")
    _, kwargs = mock_open.call_args
    assert kwargs.get("timeout") == 15
