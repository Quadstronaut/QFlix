"""Tests for Kuma push monitor wrapper."""
from __future__ import annotations
import json
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "local" / "qflix-mcp"))

from lib.kuma_push import push_up  # noqa: E402


@patch("urllib.request.urlopen")
def test_push_up_uses_correct_url(mock_open, tmp_path):
    secrets = tmp_path / "secrets"; secrets.mkdir()
    (secrets / "kuma-push-tokens.json").write_text(json.dumps({
        "QFlix Collect (seedbox)": "TOKEN123"
    }))
    (secrets / "uptimekuma.host").write_text("kuma.example.com")
    m = MagicMock(); m.status = 200; m.read.return_value = b'{"ok": true}'
    m.__enter__.return_value = m
    mock_open.return_value = m
    ok = push_up("QFlix Collect (seedbox)", msg="all good",
                 secrets_dir=secrets)
    assert ok is True
    url = mock_open.call_args[0][0]
    # urlopen accepts either a URL string or a Request object
    full_url = url if isinstance(url, str) else url.full_url
    assert "TOKEN123" in full_url
    assert "status=up" in full_url
    assert "kuma.example.com" in full_url


@patch("urllib.request.urlopen")
def test_push_up_returns_false_on_no_token(mock_open, tmp_path):
    secrets = tmp_path / "secrets"; secrets.mkdir()
    (secrets / "kuma-push-tokens.json").write_text("{}")
    ok = push_up("Missing", msg="x", secrets_dir=secrets)
    assert ok is False
    mock_open.assert_not_called()
