"""Tautulli + arr source tests — no live API, mock with monkeypatch."""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qflix_newsletter import sources
from qflix_newsletter.config import ArrEndpoint, Config

FIXTURES = Path(__file__).parent.parent / "fixtures" / "qflix_newsletter"


def _config_stub(tmp_path) -> Config:
    return Config(
        tautulli_url="http://127.0.0.1:42000/tautulli",
        tautulli_key="t-key",
        sonarr=ArrEndpoint("http://127.0.0.1:42010/sonarr", "s-key"),
        sonarr_anime=None,
        radarr=ArrEndpoint("http://127.0.0.1:42011/radarr", "r-key"),
        radarr_anime=None,
        tmdb_read_token=None,
        gemini_api_key=None,
        listmonk_base_url="http://127.0.0.1:42014",
        listmonk_api_user="u",
        listmonk_api_token="tok",
        listmonk_list_id=1,
        listmonk_template_id=None,
        public_host="seedbox.example.com",
    )


def _mock_response(body: dict, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = body
    m.raise_for_status = MagicMock()
    return m


def test_fetch_recently_added_normalizes_movie_and_episode(tmp_path):
    cfg = _config_stub(tmp_path)
    fixture = json.loads((FIXTURES / "recent.json").read_text())
    with patch.object(sources.requests, "get", return_value=_mock_response(fixture)):
        items = sources.fetch_recently_added(cfg, count=10)

    assert len(items) == 6
    movie = next(i for i in items if i.title == "Dune: Part Two")
    assert movie.media_type == "movie"
    assert movie.year == 2024
    assert movie.rating == pytest.approx(8.7)
    assert movie.tmdb_id == 693134
    assert movie.thumb_url and movie.thumb_url.startswith("http://127.0.0.1:42000/tautulli/pms_image_proxy")

    ep = next(i for i in items if i.show_title == "Severance" and i.episode == 8)
    assert ep.media_type == "episode"
    assert ep.season == 2
    assert ep.library_name == "TV Shows"


def test_fetch_recently_added_propagates_tautulli_failure(tmp_path):
    cfg = _config_stub(tmp_path)
    bad = {"response": {"result": "error", "message": "broken"}}
    with patch.object(sources.requests, "get", return_value=_mock_response(bad)):
        with pytest.raises(RuntimeError, match="tautulli get_recently_added failed"):
            sources.fetch_recently_added(cfg)


def test_fetch_calendar_normalizes_sonarr_and_radarr_rows():
    arr = ArrEndpoint("http://127.0.0.1:42010/sonarr", "s-key")
    sonarr_rows = [
        {
            "seriesId": 5,
            "title": "Episode One",
            "airDateUtc": "2026-05-15T20:00:00Z",
            "seasonNumber": 2,
            "episodeNumber": 1,
            "overview": "blah",
            "series": {"title": "Andor", "network": "Disney+"},
        }
    ]
    with patch.object(sources.requests, "get", return_value=_mock_response(sonarr_rows)):
        rows = sources.fetch_calendar(arr, days=14, now=_dt.datetime(2026, 5, 10))
    assert len(rows) == 1
    assert rows[0].media_type == "tv"
    assert rows[0].show_title == "Andor"
    assert rows[0].season == 2 and rows[0].episode == 1
    assert rows[0].air_date == _dt.date(2026, 5, 15)

    radarr_rows = [{"title": "Future Movie", "digitalRelease": "2026-05-20", "overview": "soon"}]
    with patch.object(sources.requests, "get", return_value=_mock_response(radarr_rows)):
        rows = sources.fetch_calendar(arr, days=14, now=_dt.datetime(2026, 5, 10))
    assert rows[0].media_type == "movie"
    assert rows[0].title == "Future Movie"
    assert rows[0].air_date == _dt.date(2026, 5, 20)


def test_extract_tmdb_id_handles_string_and_dict_guids():
    assert sources._extract_tmdb_id({"guids": [{"id": "tmdb://12345"}]}) == 12345
    assert sources._extract_tmdb_id({"guids": ["tmdb://9876"]}) == 9876
    assert sources._extract_tmdb_id({"guids": [{"id": "imdb://tt000001"}]}) is None
    assert sources._extract_tmdb_id({}) is None
