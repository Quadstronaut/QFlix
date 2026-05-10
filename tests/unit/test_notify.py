"""tests/unit/test_notify.py — TDD tests for lib/notify.py."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import lib.notify as notify_mod
from lib.notify import notify


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_secret(secrets_dir: Path, name: str, value: str) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / name).write_text(value + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# test_notify_posts_to_notifiarr_url_with_key
# ---------------------------------------------------------------------------

def test_notify_posts_to_notifiarr_url_with_key(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "notifiarr.key", "TESTKEY123")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path / "state"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp) as mock_post:
        result = notify("hello world", level="info")

    assert result is True
    assert mock_post.called
    url = mock_post.call_args[0][0]
    assert "TESTKEY123" in url
    assert "notifiarr.com" in url
    payload = mock_post.call_args[1]["json"]
    assert "hello world" in str(payload)


# ---------------------------------------------------------------------------
# test_notify_returns_true_on_200
# ---------------------------------------------------------------------------

def test_notify_returns_true_on_200(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "notifiarr.key", "KEY200")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp):
        result = notify("success message")

    assert result is True
    fail_log = state_dir / "notify-fail.log"
    assert not fail_log.exists(), "no fail-log should be written on success"


# ---------------------------------------------------------------------------
# test_notify_returns_false_on_500
# ---------------------------------------------------------------------------

def test_notify_returns_false_on_500(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "notifiarr.key", "KEY500")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    import requests as req_mod

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.raise_for_status.side_effect = req_mod.HTTPError("500 Server Error")

    with patch("requests.post", return_value=mock_resp):
        result = notify("fail message", level="error")

    assert result is False
    fail_log = state_dir / "notify-fail.log"
    assert fail_log.exists(), "fail-log must be written on 500"
    content = fail_log.read_text(encoding="utf-8")
    assert "fail message" in content
    assert "error" in content


# ---------------------------------------------------------------------------
# test_notify_returns_false_on_timeout
# ---------------------------------------------------------------------------

def test_notify_returns_false_on_timeout(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "notifiarr.key", "KEYTIMEOUT")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    import requests as req_mod

    with patch("requests.post", side_effect=req_mod.Timeout("timed out")):
        result = notify("timeout message", level="warning")

    assert result is False
    fail_log = state_dir / "notify-fail.log"
    assert fail_log.exists()
    content = fail_log.read_text(encoding="utf-8")
    assert "timeout message" in content


# ---------------------------------------------------------------------------
# test_notify_failure_log_path_under_opt_maint
# ---------------------------------------------------------------------------

def test_notify_failure_log_path_under_opt_maint(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "notifiarr.key", "KEYLOGPATH")
    state_dir = tmp_path / "maint-state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    import requests as req_mod

    with patch("requests.post", side_effect=req_mod.ConnectionError("refused")):
        notify("log-path test", level="info")

    fail_log = state_dir / "notify-fail.log"
    assert fail_log.exists(), f"expected fail-log at {fail_log}"


# ---------------------------------------------------------------------------
# Discord-webhook-direct path (post-Notifiarr-purge)
# ---------------------------------------------------------------------------

def test_notify_uses_discord_webhook_when_present(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "discord-webhook.url",
                   "https://discord.com/api/webhooks/12345/abcDEF")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    posted_to = []
    posted_payload = []

    def fake_post(url, json=None, timeout=None):
        posted_to.append(url)
        posted_payload.append(json)
        m = MagicMock()
        m.status_code = 204
        m.raise_for_status.return_value = None
        return m

    with patch("requests.post", side_effect=fake_post):
        result = notify("hello via webhook", level="warning")

    assert result is True
    assert posted_to == ["https://discord.com/api/webhooks/12345/abcDEF"]
    # Discord webhook payload: embed with title + description + color
    assert posted_payload[0]["username"] == "manitoba-maint"
    embeds = posted_payload[0]["embeds"]
    assert embeds[0]["description"] == "hello via webhook"
    assert embeds[0]["color"] > 0  # warning color is non-zero


def test_notify_falls_back_to_notifiarr_when_webhook_absent(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    # No discord-webhook.url file. Only notifiarr.key.
    _write_secret(secrets_dir, "notifiarr.key", "LEGACY_KEY")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    posted_to = []
    def fake_post(url, json=None, timeout=None):
        posted_to.append(url)
        m = MagicMock()
        m.status_code = 200
        m.raise_for_status.return_value = None
        return m

    with patch("requests.post", side_effect=fake_post):
        result = notify("legacy fallback msg", level="info")

    assert result is True
    assert "notifiarr.com" in posted_to[0]
    assert "LEGACY_KEY" in posted_to[0]


def test_redact_url_hides_discord_webhook_token(tmp_path, monkeypatch):
    """Failure-log entries must not contain the webhook URL's secret token."""
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "discord-webhook.url",
                   "https://discord.com/api/webhooks/9999/SECRET_TOKEN_xyz")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    import requests as req_mod
    with patch("requests.post",
                side_effect=req_mod.HTTPError(
                    "401 Client Error for url: https://discord.com/api/webhooks/9999/SECRET_TOKEN_xyz")):
        notify("redact test", level="error")

    log_text = (state_dir / "notify-fail.log").read_text(encoding="utf-8")
    assert "SECRET_TOKEN_xyz" not in log_text
    assert "<redacted>" in log_text
