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


# --- QFLX-48: a drained Discord bucket must not silently drop the page --------


def _resp(status, headers=None, body=None):
    import requests as req_mod
    m = MagicMock()
    m.status_code = status
    m.headers = headers or {}
    m.json.return_value = body or {}
    if status >= 400:
        m.raise_for_status.side_effect = req_mod.HTTPError(f"{status} Client Error")
    else:
        m.raise_for_status.return_value = None
    return m


def _wire(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "discord-webhook.url",
                  "https://discord.com/api/webhooks/12345/abcDEF")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path / "state"))
    slept = []
    monkeypatch.setattr(notify_mod.time, "sleep", lambda s: slept.append(s))
    return slept


def test_short_429_is_waited_out_once_and_the_page_is_sent(tmp_path, monkeypatch):
    """2026-10-11 04:32:30: "Fleet storm cleared" failed 429 because Kuma's
    per-monitor posts had drained the webhook bucket. A short Retry-After is
    now honoured once."""
    slept = _wire(tmp_path, monkeypatch)
    seq = [_resp(429, headers={"Retry-After": "1.5"}), _resp(204)]
    with patch("requests.post", side_effect=seq) as post:
        assert notify("Fleet storm cleared (0/31 down now)", level="info") is True
    assert post.call_count == 2
    assert slept == [1.5]
    log = (tmp_path / "state" / "notify.log").read_text(encoding="utf-8")
    assert "\tsent\t" in log


def test_retry_after_from_json_body_is_honoured(tmp_path, monkeypatch):
    slept = _wire(tmp_path, monkeypatch)
    seq = [_resp(429, body={"retry_after": 0.4}), _resp(204)]
    with patch("requests.post", side_effect=seq):
        assert notify("x", level="info") is True
    assert slept == [0.4]


def test_long_429_is_not_waited_and_fails_loud(tmp_path, monkeypatch):
    """The wait runs inline in the pusher cycle; a long one could cost a Kuma
    heartbeat, so it fails exactly as before (notify-fail.log)."""
    slept = _wire(tmp_path, monkeypatch)
    with patch("requests.post", side_effect=[_resp(429, headers={"Retry-After": "30"})]) as post:
        assert notify("x", level="info") is False
    assert post.call_count == 1 and slept == []
    assert "429" in (tmp_path / "state" / "notify-fail.log").read_text(encoding="utf-8")


def test_429_retries_at_most_once(tmp_path, monkeypatch):
    slept = _wire(tmp_path, monkeypatch)
    seq = [_resp(429, headers={"Retry-After": "1"}), _resp(429, headers={"Retry-After": "1"})]
    with patch("requests.post", side_effect=seq) as post:
        assert notify("x", level="info") is False
    assert post.call_count == 2 and slept == [1.0]
