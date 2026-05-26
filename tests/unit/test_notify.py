"""tests/unit/test_notify.py — TDD tests for lib/notify.py (Discord webhook only)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import lib.notify as notify_mod  # noqa: F401
from lib.notify import notify


def _write_secret(secrets_dir: Path, name: str, value: str) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / name).write_text(value + "\n", encoding="utf-8")


def test_notify_posts_to_discord_webhook(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "discord-webhook.url",
                  "https://discord.com/api/webhooks/12345/abcDEF")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path / "state"))

    posted_to: list[str] = []
    posted_payload: list[dict] = []

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
    assert posted_payload[0]["username"] == "manitoba-maint"
    embeds = posted_payload[0]["embeds"]
    assert embeds[0]["description"] == "hello via webhook"
    assert embeds[0]["color"] > 0


def test_notify_returns_true_on_success_and_writes_no_fail_log(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "discord-webhook.url",
                  "https://discord.com/api/webhooks/1/ok")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp):
        result = notify("success message")

    assert result is True
    assert not (state_dir / "notify-fail.log").exists()


def test_notify_returns_false_on_500_and_logs(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "discord-webhook.url",
                  "https://discord.com/api/webhooks/2/fail")
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
    log = (state_dir / "notify-fail.log").read_text(encoding="utf-8")
    assert "fail message" in log
    assert "error" in log


def test_notify_returns_false_on_timeout_and_logs(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "discord-webhook.url",
                  "https://discord.com/api/webhooks/3/slow")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    import requests as req_mod
    with patch("requests.post", side_effect=req_mod.Timeout("timed out")):
        result = notify("timeout message", level="warning")

    assert result is False
    log = (state_dir / "notify-fail.log").read_text(encoding="utf-8")
    assert "timeout message" in log


def test_notify_fails_loud_when_webhook_url_missing(tmp_path, monkeypatch):
    """Without discord-webhook.url, notify() must fail-loud (return False
    + log to notify-fail.log) rather than silently fall back to Notifiarr."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()  # exists but empty — no discord-webhook.url, no notifiarr.key
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    with patch("requests.post") as mock_post:
        result = notify("no webhook configured", level="error")

    assert result is False
    assert not mock_post.called, "must not POST anywhere when webhook URL missing"
    log = (state_dir / "notify-fail.log").read_text(encoding="utf-8")
    assert "no webhook" in log


def test_notify_writes_audit_log_on_success(tmp_path, monkeypatch):
    """Every successful send is recorded in notify.log as 'sent' — without
    touching notify-fail.log."""
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "discord-webhook.url",
                  "https://discord.com/api/webhooks/4/audit")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp):
        assert notify("audited success", level="warning") is True

    audit = (state_dir / "notify.log").read_text(encoding="utf-8")
    assert "audited success" in audit
    assert "sent" in audit
    assert "warning" in audit
    assert not (state_dir / "notify-fail.log").exists()


def test_notify_writes_audit_log_on_failure(tmp_path, monkeypatch):
    """A failed send is recorded in BOTH notify.log ('failed: ...') and
    notify-fail.log, with the webhook token redacted from the audit trail."""
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "discord-webhook.url",
                  "https://discord.com/api/webhooks/5/SECRET_xyz")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    import requests as req_mod
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.raise_for_status.side_effect = req_mod.HTTPError(
        "500 for https://discord.com/api/webhooks/5/SECRET_xyz")

    with patch("requests.post", return_value=mock_resp):
        assert notify("audited failure", level="error") is False

    audit = (state_dir / "notify.log").read_text(encoding="utf-8")
    assert "audited failure" in audit
    assert "failed" in audit
    assert "SECRET_xyz" not in audit


def test_redact_url_hides_discord_webhook_token(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "discord-webhook.url",
                  "https://discord.com/api/webhooks/9999/SECRET_TOKEN_xyz")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    import requests as req_mod
    with patch("requests.post",
               side_effect=req_mod.HTTPError(
                   "401 Client Error for url: "
                   "https://discord.com/api/webhooks/9999/SECRET_TOKEN_xyz")):
        notify("redact test", level="error")

    log_text = (state_dir / "notify-fail.log").read_text(encoding="utf-8")
    assert "SECRET_TOKEN_xyz" not in log_text
    assert "<redacted>" in log_text
