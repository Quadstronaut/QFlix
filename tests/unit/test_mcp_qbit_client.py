"""Tests for scripts/mcp/lib/qbit_client.py."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "mcp"))

from lib.qbit_client import QbitClient  # noqa: E402


def _resp(body, status=200):
    m = MagicMock()
    m.status = status
    m.read.return_value = (body if isinstance(body, (bytes,)) else
                           (body if isinstance(body, str) else json.dumps(body)).encode())
    m.headers.get.return_value = "SID=abc; path=/"
    m.__enter__.return_value = m
    return m


def _write_secrets(tmp: Path) -> Path:
    s = tmp / "secrets"
    s.mkdir()
    (s / "qbittorrent.user").write_text("user")
    (s / "qbittorrent.password").write_text("pw")
    (s / "qbittorrent.port").write_text("17041")
    return s


@patch("urllib.request.urlopen")
def test_login_success(mock_open, tmp_path):
    mock_open.return_value = _resp("Ok.")
    c = QbitClient(secrets_dir=_write_secrets(tmp_path))
    assert c.login() is True
    assert c._sid == "abc"


@patch("urllib.request.urlopen")
def test_login_bad_pw(mock_open, tmp_path):
    mock_open.return_value = _resp("Fails.")
    c = QbitClient(secrets_dir=_write_secrets(tmp_path))
    assert c.login() is False


@patch("urllib.request.urlopen")
def test_list_torrents_returns_list(mock_open, tmp_path):
    payload = [{"hash": "abc", "name": "Foo", "added_on": 1, "size": 2,
                "downloaded": 1, "progress": 0.5, "dlspeed": 0, "upspeed": 0,
                "state": "stalledDL", "category": "sonarr", "tags": "tv-sonarr",
                "ratio": 0.0, "eta": 0, "num_seeds": 0, "num_leechs": 0,
                "last_activity": 1}]
    mock_open.side_effect = [_resp("Ok."), _resp(payload)]
    c = QbitClient(secrets_dir=_write_secrets(tmp_path))
    c.login()
    items = c.list_torrents()
    assert isinstance(items, list) and items[0]["hash"] == "abc"
