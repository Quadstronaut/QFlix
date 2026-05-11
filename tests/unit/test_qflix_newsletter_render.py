"""Render layer tests for qflix-newsletter — pure data, no live API."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from qflix_newsletter.ai import AiPick
from qflix_newsletter.render import (
    build_email_context,
    build_subject,
    group_episodes_by_show,
    render_html,
    select_pick_of_week,
    split_by_library,
)
from qflix_newsletter.sources import CalendarItem, RecentItem


def _movie(title: str, *, rating=None, year=2024, library="Movies", thumb="/x/thumb") -> RecentItem:
    return RecentItem(
        media_type="movie",
        title=title,
        year=year,
        summary="lorem ipsum dolor sit amet " * 20,
        thumb_url=thumb,
        added_at=1715212800,
        rating=rating,
        library_name=library,
    )


def _episode(show: str, season: int, ep: int, *, library="TV Shows", thumb="/x/thumb") -> RecentItem:
    return RecentItem(
        media_type="episode",
        title=f"{show} S{season:02d}E{ep:02d}",
        year=2025,
        summary="episode summary",
        thumb_url=thumb,
        added_at=1715212900,
        rating=None,
        show_title=show,
        season=season,
        episode=ep,
        library_name=library,
    )


def test_split_by_library_partitions_anime():
    items = [
        _movie("A", library="Movies"),
        _movie("B", library="Anime Movies"),
        _episode("S1", 1, 1, library="TV Shows"),
        _episode("S2", 1, 1, library="Anime"),
    ]
    regular, anime = split_by_library(items)
    assert {i.title for i in regular} == {"A", "S1 S01E01"}
    assert {i.title for i in anime} == {"B", "S2 S01E01"}


def test_group_episodes_by_show_collapses_by_show_title():
    eps = [
        _episode("Severance", 2, 8),
        _episode("Severance", 2, 9),
        _episode("Severance", 2, 10),
        _episode("Andor", 1, 5),
    ]
    groups = group_episodes_by_show(eps)
    assert [g.show_title for g in groups] == ["Severance", "Andor"]
    assert groups[0].episode_count == 3
    assert groups[0].season_episode_label.startswith("S02")
    assert groups[1].episode_count == 1


def test_group_episodes_skips_non_episodes():
    items = [_movie("X"), _episode("S", 1, 1)]
    groups = group_episodes_by_show(items)
    assert len(groups) == 1
    assert groups[0].show_title == "S"


def test_select_pick_of_week_prefers_highest_rating():
    items = [_movie("Low", rating=6.5), _movie("High", rating=9.0), _movie("Mid", rating=7.5)]
    pick = select_pick_of_week(items)
    assert pick is not None and pick.title == "High"


def test_select_pick_of_week_handles_unrated_fallback():
    items = [_movie("A"), _movie("B")]
    assert select_pick_of_week(items) is not None


def test_select_pick_of_week_empty_returns_none():
    assert select_pick_of_week([]) is None


def test_build_subject_includes_marquee_and_count():
    pick = _movie("Dune: Part Two", rating=8.7)
    s = build_subject(pick, 12)
    assert "Dune: Part Two" in s
    assert "12 other things" in s
    assert s.startswith("Qflix · this week:")


def test_build_subject_no_pick_uses_placeholder():
    s = build_subject(None, 5)
    assert "fresh additions" in s


def test_build_email_context_smoke():
    items = [
        _movie("Dune: Part Two", rating=8.7),
        _movie("The Wild Robot", rating=8.3),
        _movie("Spirited Away", rating=9.2, library="Anime Movies"),
        _episode("Severance", 2, 8),
        _episode("Severance", 2, 9),
        _episode("Frieren", 1, 3, library="Anime"),
    ]
    coming = [
        CalendarItem(media_type="movie", title="Future Movie", air_date=_dt.date(2026, 5, 15)),
        CalendarItem(
            media_type="tv",
            title="Some Episode",
            air_date=_dt.date(2026, 5, 17),
            show_title="Andor",
            season=2,
            episode=1,
        ),
    ]
    ai = [AiPick(if_you_liked="Dune", try_this="Foundation", blurb="Sweeping sci-fi.")]

    ctx = build_email_context(
        recent=items,
        coming=coming,
        ai_picks=ai,
        library_stats={"total_items": 5000, "sections": [{"name": "Movies", "count": 3000}]},
        public_host="seedbox.example.com",
        now=_dt.datetime(2026, 5, 10, 12, 0, 0),
    )

    assert ctx.pick is not None and ctx.pick.title == "Dune: Part Two"
    assert all(m.title != "Dune: Part Two" for m in ctx.movies)  # pick excluded from grid
    assert {s.show_title for s in ctx.shows} == {"Severance"}
    assert {m.title for m in ctx.anime_movies} == {"Spirited Away"}
    assert {s.show_title for s in ctx.anime_shows} == {"Frieren"}
    assert len(ctx.coming_soon) == 2
    assert len(ctx.ai_picks) == 1
    assert ctx.subject.startswith("Qflix · this week: Dune: Part Two")
    assert ctx.week_label == "May 10, 2026"


def test_render_html_produces_a_full_email():
    ctx = build_email_context(
        recent=[_movie("Dune: Part Two", rating=8.7)],
        coming=[],
        ai_picks=[],
        library_stats={"total_items": 1, "sections": []},
        public_host="seedbox.example.com",
        now=_dt.datetime(2026, 5, 10),
    )
    html = render_html(ctx)
    assert "<!DOCTYPE html>" in html
    assert "QFlix" in html
    assert "Dune: Part Two" in html
    # Listmonk template tokens left intact for server-side substitution
    assert "{{ UnsubscribeURL }}" in html
    assert "{{ MessageURL }}" in html


def test_render_handles_empty_inputs_without_crashing():
    ctx = build_email_context(
        recent=[],
        coming=[],
        ai_picks=[],
        library_stats={"total_items": 0, "sections": []},
        public_host="seedbox.example.com",
    )
    html = render_html(ctx)
    assert "QFlix" in html
    assert "fresh additions" in ctx.subject
