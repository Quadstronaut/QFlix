"""tests/unit/test_listmonk.py — TDD tests for lib/listmonk.py."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from lib.listmonk import fire_template_campaign


def _write_secret(secrets_dir: Path, name: str, value: str) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / name).write_text(value + "\n", encoding="utf-8")


def _common_secrets(tmp_path: Path) -> Path:
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "listmonk.port", "9000")
    _write_secret(secrets_dir, "listmonk.api_user", "admin")
    _write_secret(secrets_dir, "listmonk.api_token", "tok123")
    _write_secret(secrets_dir, "listmonk.list_id", "4")
    return secrets_dir


def _templates_payload() -> dict:
    return {"data": [
        {"id": 1, "name": "Default campaign template"},
        {"id": 10, "name": "Prod · Maintenance Window Start"},
        {"id": 11, "name": "Prod · Maintenance Window Complete"},
        {"id": 13, "name": "Stage · Maintenance Window Start"},
    ]}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_fire_campaign_posts_and_starts_with_correct_template_id(tmp_path, monkeypatch):
    secrets_dir = _common_secrets(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path / "state"))

    list_templates_resp = MagicMock(status_code=200)
    list_templates_resp.json.return_value = _templates_payload()
    list_templates_resp.raise_for_status = MagicMock()

    create_resp = MagicMock(status_code=200)
    create_resp.json.return_value = {"data": {"id": 99, "uuid": "abc"}}
    create_resp.raise_for_status = MagicMock()

    status_resp = MagicMock(status_code=200)
    status_resp.raise_for_status = MagicMock()

    with patch("lib.listmonk.requests.get", return_value=list_templates_resp), \
         patch("lib.listmonk.requests.post", return_value=create_resp) as mock_post, \
         patch("lib.listmonk.requests.put", return_value=status_resp) as mock_put:
        result = fire_template_campaign(
            template_title="Maintenance Window Start",
            subject="QFlix maintenance window starting",
        )

    assert result is True

    # Campaign POST used template id 10 (Prod · Maintenance Window Start)
    post_payload = mock_post.call_args.kwargs["json"]
    assert post_payload["template_id"] == 10
    assert post_payload["subject"] == "QFlix maintenance window starting"
    assert post_payload["lists"] == [4]
    assert post_payload["type"] == "regular"
    assert post_payload["content_type"] == "html"

    # Status PUT targeted the new campaign id and asked for 'running'
    put_url = mock_put.call_args.args[0]
    assert "/api/campaigns/99/status" in put_url
    assert mock_put.call_args.kwargs["json"] == {"status": "running"}


def test_fire_campaign_honors_env_prefix_override(tmp_path, monkeypatch):
    secrets_dir = _common_secrets(tmp_path)
    _write_secret(secrets_dir, "listmonk.maint_env", "Stage")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path / "state"))

    list_resp = MagicMock(status_code=200)
    list_resp.json.return_value = _templates_payload()
    list_resp.raise_for_status = MagicMock()
    create_resp = MagicMock(status_code=200)
    create_resp.json.return_value = {"data": {"id": 50}}
    create_resp.raise_for_status = MagicMock()
    status_resp = MagicMock(status_code=200)
    status_resp.raise_for_status = MagicMock()

    with patch("lib.listmonk.requests.get", return_value=list_resp), \
         patch("lib.listmonk.requests.post", return_value=create_resp) as mock_post, \
         patch("lib.listmonk.requests.put", return_value=status_resp):
        result = fire_template_campaign(
            template_title="Maintenance Window Start",
            subject="test",
        )

    assert result is True
    # Stage · Maintenance Window Start = id 13
    assert mock_post.call_args.kwargs["json"]["template_id"] == 13


def test_fire_campaign_explicit_list_id_overrides_secret(tmp_path, monkeypatch):
    secrets_dir = _common_secrets(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path / "state"))

    list_resp = MagicMock(status_code=200)
    list_resp.json.return_value = _templates_payload()
    list_resp.raise_for_status = MagicMock()
    create_resp = MagicMock(status_code=200)
    create_resp.json.return_value = {"data": {"id": 7}}
    create_resp.raise_for_status = MagicMock()
    status_resp = MagicMock(status_code=200)
    status_resp.raise_for_status = MagicMock()

    with patch("lib.listmonk.requests.get", return_value=list_resp), \
         patch("lib.listmonk.requests.post", return_value=create_resp) as mock_post, \
         patch("lib.listmonk.requests.put", return_value=status_resp):
        fire_template_campaign(
            template_title="Maintenance Window Complete",
            subject="test",
            list_id=19,
        )

    assert mock_post.call_args.kwargs["json"]["lists"] == [19]


# ---------------------------------------------------------------------------
# Failure paths — all return False and log to notify-fail.log
# ---------------------------------------------------------------------------

def test_fire_campaign_template_not_found_returns_false_and_logs(tmp_path, monkeypatch):
    secrets_dir = _common_secrets(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    list_resp = MagicMock(status_code=200)
    list_resp.json.return_value = {"data": [
        {"id": 1, "name": "Default campaign template"},
    ]}
    list_resp.raise_for_status = MagicMock()

    with patch("lib.listmonk.requests.get", return_value=list_resp), \
         patch("lib.listmonk.requests.post") as mock_post:
        result = fire_template_campaign(
            template_title="Maintenance Window Start",
            subject="x",
        )

    assert result is False
    mock_post.assert_not_called(), "must not POST a campaign when template missing"
    log = (state_dir / "notify-fail.log").read_text(encoding="utf-8")
    assert "template not found" in log
    assert "Maintenance Window Start" in log


def test_fire_campaign_missing_creds_returns_false(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()  # empty — no listmonk.port etc.
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    with patch("lib.listmonk.requests.get") as mock_get, \
         patch("lib.listmonk.requests.post") as mock_post:
        result = fire_template_campaign(
            template_title="Maintenance Window Start",
            subject="x",
        )

    assert result is False
    mock_get.assert_not_called()
    mock_post.assert_not_called()
    log = (state_dir / "notify-fail.log").read_text(encoding="utf-8")
    assert "creds missing" in log


def test_fire_campaign_missing_list_id_returns_false(tmp_path, monkeypatch):
    secrets_dir = _common_secrets(tmp_path)
    (secrets_dir / "listmonk.list_id").unlink()  # remove just the list_id
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    list_resp = MagicMock(status_code=200)
    list_resp.json.return_value = _templates_payload()
    list_resp.raise_for_status = MagicMock()

    with patch("lib.listmonk.requests.get", return_value=list_resp), \
         patch("lib.listmonk.requests.post") as mock_post:
        result = fire_template_campaign(
            template_title="Maintenance Window Start",
            subject="x",
        )

    assert result is False
    mock_post.assert_not_called()
    log = (state_dir / "notify-fail.log").read_text(encoding="utf-8")
    assert "list_id missing" in log


def test_fire_campaign_listmonk_down_returns_false(tmp_path, monkeypatch):
    secrets_dir = _common_secrets(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    with patch("lib.listmonk.requests.get", side_effect=requests.ConnectionError("refused")):
        result = fire_template_campaign(
            template_title="Maintenance Window Start",
            subject="x",
        )

    assert result is False
    log = (state_dir / "notify-fail.log").read_text(encoding="utf-8")
    assert "refused" in log


def test_fire_campaign_status_put_failure_returns_false(tmp_path, monkeypatch):
    """Even if the campaign was created, failure to flip status=running
    means the email never sends. Return False so the operator can retry."""
    secrets_dir = _common_secrets(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))

    list_resp = MagicMock(status_code=200)
    list_resp.json.return_value = _templates_payload()
    list_resp.raise_for_status = MagicMock()
    create_resp = MagicMock(status_code=200)
    create_resp.json.return_value = {"data": {"id": 99}}
    create_resp.raise_for_status = MagicMock()
    bad_status_resp = MagicMock(status_code=500)
    bad_status_resp.raise_for_status.side_effect = requests.HTTPError("500")

    with patch("lib.listmonk.requests.get", return_value=list_resp), \
         patch("lib.listmonk.requests.post", return_value=create_resp), \
         patch("lib.listmonk.requests.put", return_value=bad_status_resp):
        result = fire_template_campaign(
            template_title="Maintenance Window Start",
            subject="x",
        )

    assert result is False
    log = (state_dir / "notify-fail.log").read_text(encoding="utf-8")
    assert "500" in log
