"""Shared pytest fixtures for newsletter render tests."""
from __future__ import annotations

import datetime as _dt

import pytest

from qflix_newsletter.ai import AiPick
from qflix_newsletter.render import EmailContext, ShowGroup
from qflix_newsletter.sources import CalendarItem, RecentItem


def _movie(title: str, year: int, rating: float = 7.5) -> RecentItem:
    return RecentItem(
        media_type="movie",
        title=title,
        year=year,
        summary=f"Summary for {title}.",
        thumb_url=f"https://image.tmdb.org/t/p/w300/{title}.jpg",
        added_at=1700000000,
        rating=rating,
        library_name="Movies",
    )


def _episode(show: str, season: int, ep: int) -> RecentItem:
    return RecentItem(
        media_type="episode",
        title=f"Episode {ep}",
        year=2026,
        summary="",
        thumb_url=None,
        added_at=1700000000,
        rating=None,
        show_title=show,
        season=season,
        episode=ep,
        library_name="TV Shows",
    )


@pytest.fixture
def sample_ctx() -> EmailContext:
    pick = _movie("Suzume", 2022, rating=10.0)
    movies = [_movie("Inception", 2010, 9.0), _movie("Arrival", 2016, 8.5)]
    show_a = ShowGroup(
        show_title="Severance",
        episodes=[_episode("Severance", 2, 1), _episode("Severance", 2, 2)],
        thumb_url="https://image.tmdb.org/t/p/w300/sev.jpg",
    )
    coming = [
        CalendarItem(
            media_type="tv",
            title="Pilot",
            air_date=_dt.date(2026, 5, 20),
            show_title="New Show",
            season=1,
            episode=1,
        )
    ]
    ai_picks = [
        AiPick(
            if_you_liked="Spirited Away",
            try_this="The Tale of the Princess Kaguya",
            blurb="Same Ghibli emotional gut-punch, different visual register.",
        )
    ]
    return EmailContext(
        week_label="May 11, 2026",
        pick=pick,
        movies=movies,
        shows=[show_a],
        anime_movies=[],
        anime_shows=[],
        coming_soon=coming,
        ai_picks=ai_picks,
        nerd_corner={
            "total_items": 12345,
            "sections": [{"name": "Movies", "count": 1234}, {"name": "TV Shows", "count": 11111}],
        },
        subject="Test subject",
        public_host="quadstronaut.seedbox.example.com",
    )
