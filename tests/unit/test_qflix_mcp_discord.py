"""Tests for discord webhook poster."""
from __future__ import annotations
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "local" / "qflix-mcp"))

from lib.discord import post  # noqa: E402


@patch("urllib.request.urlopen")
def test_post_includes_no_ping(mock_open, tmp_path):
    secrets = tmp_path / "secrets"; secrets.mkdir()
    (secrets / "discord-webhook.url").write_text("https://discord.example/hook")
    m = MagicMock(); m.status = 204; m.read.return_value = b""
    m.__enter__.return_value = m
    mock_open.return_value = m
    ok = post("test message", secrets_dir=secrets)
    assert ok is True
    sent = mock_open.call_args[0][0]
    body = sent.data.decode()
    assert "@" not in body or "<@" not in body  # no role/user pings


@patch("urllib.request.urlopen")
def test_post_no_webhook_returns_false(mock_open, tmp_path):
    ok = post("test message", secrets_dir=tmp_path)
    assert ok is False
    mock_open.assert_not_called()
