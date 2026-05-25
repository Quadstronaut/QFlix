"""Tests for qflix_newsletter/sync.py — the Listmonk template uploader.

Closes a coverage gap flagged in the 2026-05-15 audit: sync.py is the
deployment tool that pushes HTML into Listmonk's template store. A
regression here (e.g. the `{{ template "content" . }}` injection logic)
would silently corrupt the archive template viewed by every subscriber.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qflix_newsletter import sync
from qflix_newsletter.config import ArrEndpoint, Config


def _config(tmp_path: Path) -> Config:
    return Config(
        tautulli_url="http://127.0.0.1/", tautulli_key="t",
        sonarr=ArrEndpoint("http://127.0.0.1/", "s"), sonarr_anime=None,
        radarr=ArrEndpoint("http://127.0.0.1/", "r"), radarr_anime=None,
        tmdb_read_token=None, gemini_api_key=None,
        listmonk_base_url="http://127.0.0.1:42014",
        listmonk_api_user="u", listmonk_api_token="tok",
        listmonk_list_id=1, listmonk_template_id=None,
        public_host="seedbox.example.com",
        kuma_public_host="kuma.seedbox.example.com",
        poster_cache_dir=tmp_path / "poster-cache",
    )


def _response(json_body: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_body
    m.raise_for_status = MagicMock()
    return m


def test_render_preview_includes_sample_pick_and_no_template_slot():
    """render_preview produces a fully-rendered Jinja preview; the
    `{{ template "content" . }}` slot is added later by upsert_template,
    not by render_preview itself. A future refactor that moves the slot
    injection into render_preview would silently double it."""
    out = sync.render_preview("weekly.html.j2", "example.com", "kuma.example.com")
    assert "Suzume" in out  # sample pick title
    assert "{{ template" not in out
    assert "Sample Movie A" in out


def test_upsert_creates_new_template_when_absent(tmp_path):
    cfg = _config(tmp_path)
    client = sync._ListmonkClient(
        base_url=cfg.listmonk_base_url,
        auth=(cfg.listmonk_api_user, cfg.listmonk_api_token),
    )
    with patch("qflix_newsletter.sync.requests") as req:
        req.post.return_value = _response({"data": {"id": 42}})
        tid = client.upsert_template("Prod · Weekly Digest", "<html>x</html>", None)
    assert tid == 42
    req.post.assert_called_once()
    body = req.post.call_args.kwargs["json"]
    # The Listmonk-required slot is injected exactly once.
    assert body["body"].count('{{ template "content" . }}') == 1
    assert body["type"] == "campaign"
    assert body["name"] == "Prod · Weekly Digest"


def test_upsert_updates_existing_template_when_id_given(tmp_path):
    cfg = _config(tmp_path)
    client = sync._ListmonkClient(
        base_url=cfg.listmonk_base_url,
        auth=(cfg.listmonk_api_user, cfg.listmonk_api_token),
    )
    with patch("qflix_newsletter.sync.requests") as req:
        req.put.return_value = _response({"data": {"id": 7}})
        tid = client.upsert_template("Prod · Weekly Digest", "<html>x</html>", 7)
    assert tid == 7
    # PUT to /api/templates/<id>, never POST.
    assert req.put.called and not req.post.called
    assert req.put.call_args.args[0].endswith("/api/templates/7")


def test_upsert_propagates_listmonk_4xx(tmp_path):
    """A duplicate-name conflict or auth failure must raise — not
    silently leave the template store stale."""
    cfg = _config(tmp_path)
    client = sync._ListmonkClient(
        base_url=cfg.listmonk_base_url,
        auth=(cfg.listmonk_api_user, cfg.listmonk_api_token),
    )
    with patch("qflix_newsletter.sync.requests") as req:
        import requests as _r
        bad = MagicMock()
        bad.status_code = 422
        bad.json.return_value = {"message": "duplicate"}
        bad.raise_for_status.side_effect = _r.HTTPError("422 dup", response=bad)
        req.post.return_value = bad
        with pytest.raises(_r.HTTPError):
            client.upsert_template("Prod · Weekly Digest", "<html>x</html>", None)


def test_sync_rejects_unknown_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path / "secrets"))
    with pytest.raises(ValueError) as exc_info:
        sync.sync("test")
    assert "prod" in str(exc_info.value) and "stage" in str(exc_info.value)


def test_list_templates_returns_name_to_id_map(tmp_path):
    cfg = _config(tmp_path)
    client = sync._ListmonkClient(
        base_url=cfg.listmonk_base_url,
        auth=(cfg.listmonk_api_user, cfg.listmonk_api_token),
    )
    with patch("qflix_newsletter.sync.requests") as req:
        req.get.return_value = _response({"data": [
            {"id": 1, "name": "Prod · Weekly Digest"},
            {"id": 2, "name": "Stage · Weekly Digest"},
        ]})
        out = client.list_templates()
    assert out == {"Prod · Weekly Digest": 1, "Stage · Weekly Digest": 2}


# ---------------------------------------------------------------------------
# B3: upstream-maint-* template render tests
# ---------------------------------------------------------------------------

def test_upstream_maint_start_renders_without_error():
    """upstream-maint-start.html.j2 renders against the preview context
    without raising and produces non-empty HTML."""
    out = sync.render_preview("upstream-maint-start.html.j2",
                              "example.com", "kuma.example.com")
    assert out.strip()
    assert "{{ template" not in out
    assert "upstream" in out.lower() or "maintenance" in out.lower()


def test_upstream_maint_complete_renders_without_error():
    """upstream-maint-complete.html.j2 renders against the preview context
    without raising and produces non-empty HTML."""
    out = sync.render_preview("upstream-maint-complete.html.j2",
                              "example.com", "kuma.example.com")
    assert out.strip()
    assert "{{ template" not in out
    assert "complete" in out.lower() or "maintenance" in out.lower()


def test_template_titles_includes_upstream_maint():
    """TEMPLATE_TITLES must contain both upstream-maint entries so sync()
    will upsert them to Listmonk."""
    assert "upstream-maint-start.html.j2" in sync.TEMPLATE_TITLES
    assert sync.TEMPLATE_TITLES["upstream-maint-start.html.j2"] == "Upstream Maintenance Start"
    assert "upstream-maint-complete.html.j2" in sync.TEMPLATE_TITLES
    assert sync.TEMPLATE_TITLES["upstream-maint-complete.html.j2"] == "Upstream Maintenance Complete"
