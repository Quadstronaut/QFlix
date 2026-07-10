"""Listmonk delivery tests — verify request shape; no live API."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qflix_newsletter import delivery
from qflix_newsletter.config import ArrEndpoint, Config


def _cfg() -> Config:
    return Config(
        tautulli_url="http://127.0.0.1:42000/tautulli",
        tautulli_key="t",
        sonarr=ArrEndpoint("http://127.0.0.1:42010/sonarr", "s"),
        sonarr_anime=None,
        radarr=ArrEndpoint("http://127.0.0.1:42011/radarr", "r"),
        radarr_anime=None,
        tmdb_read_token=None,
        github_repo="Quadstronaut/QFlix",
        listmonk_base_url="http://127.0.0.1:42014",
        listmonk_api_user="api-user",
        listmonk_api_token="api-token",
        listmonk_list_id=7,
        listmonk_template_id=42,
        public_host="seedbox.example.com",
        kuma_public_host="kuma.seedbox.example.com",
        poster_cache_dir=Path("/tmp/poster-cache"),
    )


def _mock_resp(body, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = body
    m.raise_for_status = MagicMock()
    return m


def test_create_and_send_campaign_posts_then_starts():
    cfg = _cfg()
    create_resp = _mock_resp({"data": {"id": 99, "uuid": "abc-123"}})
    start_resp = _mock_resp({"data": True})

    with patch.object(delivery.requests, "post", return_value=create_resp) as mock_post, \
         patch.object(delivery.requests, "put", return_value=start_resp) as mock_put:
        result = delivery.create_and_send_campaign(
            cfg, subject="Hello", html_body="<p>body</p>"
        )

    # POST /api/campaigns
    assert mock_post.call_count == 1
    args, kwargs = mock_post.call_args
    assert args[0].endswith("/api/campaigns")
    assert kwargs["auth"] == ("api-user", "api-token")
    payload = kwargs["json"]
    assert payload["subject"] == "Hello"
    assert payload["body"] == "<p>body</p>"
    assert payload["lists"] == [7]
    assert payload["archive"] is True
    assert payload["archive_template_id"] == 42
    assert payload["type"] == "regular"
    assert payload["content_type"] == "html"

    # PUT .../99/status with status=running
    assert mock_put.call_count == 1
    args, kwargs = mock_put.call_args
    assert args[0].endswith("/api/campaigns/99/status")
    assert kwargs["json"] == {"status": "running"}

    assert result.campaign_id == 99
    assert result.status == "running"
    assert result.archive_url == "https://seedbox.example.com/listmonk/campaign/abc-123"


def test_create_and_send_campaign_dry_run_skips_network():
    cfg = _cfg()
    with patch.object(delivery.requests, "post") as mock_post, \
         patch.object(delivery.requests, "put") as mock_put:
        result = delivery.create_and_send_campaign(
            cfg, subject="Hi", html_body="<p>x</p>", dry_run=True
        )
    mock_post.assert_not_called()
    mock_put.assert_not_called()
    assert result.status == "dry-run"


def test_create_and_send_campaign_raises_when_id_missing():
    cfg = _cfg()
    bad = _mock_resp({"data": {}})
    with patch.object(delivery.requests, "post", return_value=bad):
        with pytest.raises(RuntimeError, match="returned no id"):
            delivery.create_and_send_campaign(cfg, subject="x", html_body="y")
