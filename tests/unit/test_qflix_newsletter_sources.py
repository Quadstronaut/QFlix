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
        kuma_public_host="kuma.seedbox.example.com",
        poster_cache_dir=tmp_path / "poster-cache",
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
    # Pre-enrichment thumb_url points at the seedbox's public Tautulli proxy.
    # enrich_with_tmdb later rewrites this to image.tmdb.org (see the
    # dedicated test below).
    assert movie.thumb_url and movie.thumb_url.startswith(
        "https://seedbox.example.com/tautulli/pms_image_proxy"
    )

    ep = next(i for i in items if i.show_title == "Severance" and i.episode == 8)
    assert ep.media_type == "episode"
    assert ep.season == 2
    assert ep.library_name == "TV Shows"


def test_fetch_recently_added_preserves_tautulli_thumb_url(tmp_path):
    """tautulli_thumb_url survives enrich_with_tmdb so it can be a fallback source."""
    cfg = _config_stub(tmp_path)
    fixture = json.loads((FIXTURES / "recent.json").read_text())
    with patch.object(sources.requests, "get", return_value=_mock_response(fixture)):
        items = sources.fetch_recently_added(cfg, count=10)

    movie = next(i for i in items if i.title == "Dune: Part Two")
    assert movie.tautulli_thumb_url is not None
    assert movie.tautulli_thumb_url.startswith(
        "https://seedbox.example.com/tautulli/pms_image_proxy"
    )
    # Equal to thumb_url at this stage; enrich_with_tmdb will diverge them.
    assert movie.tautulli_thumb_url == movie.thumb_url


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


def test_enrich_with_tmdb_rewrites_thumb_to_image_cdn(tmp_path):
    cfg = _config_stub(tmp_path)
    cfg.tmdb_read_token = "tmdb-test-token"

    movie = sources.RecentItem(
        media_type="movie",
        title="Dune: Part Two",
        year=2024,
        summary="",
        thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/metadata/12345/thumb",
        tautulli_thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/metadata/12345/thumb",
        added_at=1715212800,
        rating=None,
    )
    ep1 = sources.RecentItem(
        media_type="episode",
        title="Pure Gold",
        year=None,
        summary="",
        thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/metadata/6492/thumb",
        tautulli_thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/metadata/6492/thumb",
        added_at=1715299200,
        rating=None,
        show_title="The Curse of Oak Island",
        season=13,
        episode=1,
    )
    # Second episode of the same show — must hit the show-search cache, no second TMDB call.
    ep2 = sources.RecentItem(
        media_type="episode",
        title="Dust in the Wind",
        year=None,
        summary="",
        thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/metadata/6491/thumb",
        tautulli_thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/metadata/6491/thumb",
        added_at=1715212900,
        rating=None,
        show_title="The Curse of Oak Island",
        season=13,
        episode=2,
    )
    # Movie that TMDB has no result for → thumb dropped.
    orphan = sources.RecentItem(
        media_type="movie",
        title="Forgotten Title That TMDB Does Not Know",
        year=None,
        summary="",
        thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/metadata/9999/thumb",
        tautulli_thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/metadata/9999/thumb",
        added_at=1715000000,
        rating=None,
    )
    # Season rows have no useful poster source — should be cleared, no TMDB call.
    season = sources.RecentItem(
        media_type="season",
        title="Season 24",
        year=None,
        summary="",
        thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/metadata/5766/thumb",
        tautulli_thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/metadata/5766/thumb",
        added_at=1715200000,
        rating=None,
    )

    call_count = {"movie": 0, "tv": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        if "api.themoviedb.org/3/search/movie" in url:
            call_count["movie"] += 1
            q = (params or {}).get("query", "")
            if q.startswith("Dune"):
                return _mock_response({"results": [{"poster_path": "/dune2.jpg", "vote_average": 8.7}]})
            return _mock_response({"results": []})
        if "api.themoviedb.org/3/search/tv" in url:
            call_count["tv"] += 1
            q = (params or {}).get("query", "")
            if "Oak Island" in q:
                return _mock_response({"results": [{"poster_path": "/oakisland.jpg", "vote_average": 8.4}]})
            return _mock_response({"results": []})
        raise AssertionError(f"unexpected GET {url} params={params}")

    with patch.object(sources.requests, "get", side_effect=fake_get):
        out = sources.enrich_with_tmdb(cfg, [movie, ep1, ep2, orphan, season])

    assert out[0].thumb_url == "https://image.tmdb.org/t/p/w342/dune2.jpg"
    assert out[0].tautulli_thumb_url.startswith("https://seedbox.example.com/tautulli/pms_image_proxy")
    assert out[0].tautulli_thumb_url != out[0].thumb_url
    assert out[0].rating == pytest.approx(8.7)
    assert out[1].thumb_url == "https://image.tmdb.org/t/p/w342/oakisland.jpg"
    assert out[2].thumb_url == "https://image.tmdb.org/t/p/w342/oakisland.jpg"  # cache hit
    assert out[3].thumb_url is None
    assert out[4].thumb_url is None
    # Show search was called once despite two episodes from the same show.
    assert call_count["tv"] == 1
    # Two distinct movie titles → two movie searches.
    assert call_count["movie"] == 2


def test_enrich_with_tmdb_passthrough_without_token(tmp_path):
    cfg = _config_stub(tmp_path)
    assert cfg.tmdb_read_token is None
    item = sources.RecentItem(
        media_type="movie",
        title="X",
        year=None,
        summary="",
        thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/foo",
        tautulli_thumb_url="https://seedbox.example.com/tautulli/pms_image_proxy?img=/foo",
        added_at=0,
        rating=None,
    )
    out = sources.enrich_with_tmdb(cfg, [item])
    assert out[0].thumb_url == item.thumb_url


# ---------------------------------------------------------------------------
# Resilience: per-arr fetch failure must not kill the whole section
# ---------------------------------------------------------------------------

def test_fetch_all_calendars_survives_per_arr_failure(tmp_path):
    """A single arr fetch raising (e.g. Sonarr2 in a bad state during the
    Monday maintenance window) used to propagate out of run() and drop the
    entire Coming Soon section — and everything downstream in the email."""
    import requests as _req
    cfg = _config_stub(tmp_path)
    # Add an anime arr so we have ≥2 calls; first one will raise.
    cfg.sonarr_anime = ArrEndpoint("http://127.0.0.1:42013/sonarr2", "s2-key")

    call_log = []
    def _flaky_fetch(arr, days, now=None):
        call_log.append(arr.base_url)
        if "42013" in arr.base_url:
            raise _req.ConnectionError("simulated sonarr2 down")
        # Healthy arrs return one item.
        return [sources.CalendarItem(
            media_type="tv" if "sonarr" in arr.base_url else "movie",
            title=f"From {arr.base_url}",
            air_date=_dt.date.today(),
        )]

    with patch.object(sources, "fetch_calendar", side_effect=_flaky_fetch):
        items = sources.fetch_all_calendars(cfg, days=14)

    # Three healthy arrs (sonarr, radarr — radarr_anime is None), one failed.
    # Output should still contain items from the survivors, not raise.
    assert len(items) == 2
    assert len(call_log) == 3  # one per non-None arr
