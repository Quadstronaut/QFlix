"""Build email context + render Jinja2 template."""
from __future__ import annotations

import datetime as _dt
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .changelog import BehindScenes
from .sources import CalendarItem, RecentItem

TEMPLATE_DIR = Path(__file__).parent / "templates"


@dataclass
class ShowGroup:
    show_title: str
    episodes: list[RecentItem] = field(default_factory=list)
    thumb_url: Optional[str] = None
    summary: str = ""

    @property
    def episode_count(self) -> int:
        return len(self.episodes)

    @property
    def season_episode_label(self) -> str:
        seasons = sorted({e.season for e in self.episodes if e.season is not None})
        if not seasons:
            return f"{self.episode_count} new episodes"
        if len(seasons) == 1:
            return f"S{seasons[0]:02d} · {self.episode_count} new episodes"
        return f"Seasons {seasons[0]}–{seasons[-1]} · {self.episode_count} new episodes"


@dataclass
class EmailContext:
    week_label: str
    pick: Optional[RecentItem]
    movies: list[RecentItem]
    shows: list[ShowGroup]
    anime_movies: list[RecentItem]
    anime_shows: list[ShowGroup]
    coming_soon: list[CalendarItem]
    behind_scenes: Optional[BehindScenes]
    nerd_corner: dict
    subject: str
    public_host: str
    kuma_public_host: str


def group_episodes_by_show(items: Sequence[RecentItem]) -> list[ShowGroup]:
    groups: dict[str, ShowGroup] = {}
    for item in items:
        if item.media_type != "episode":
            continue
        key = item.show_title or item.title
        g = groups.setdefault(key, ShowGroup(show_title=key))
        g.episodes.append(item)
        if g.thumb_url is None and item.thumb_url is not None:
            g.thumb_url = item.thumb_url
        if not g.summary and item.summary:
            g.summary = item.summary
    return sorted(groups.values(), key=lambda g: -g.episode_count)


def split_by_library(items: Sequence[RecentItem]) -> tuple[list[RecentItem], list[RecentItem]]:
    """Return (regular_items, anime_items) using library_name heuristics."""
    regular: list[RecentItem] = []
    anime: list[RecentItem] = []
    for item in items:
        lib = (item.library_name or "").lower()
        if "anime" in lib:
            anime.append(item)
        else:
            regular.append(item)
    return regular, anime


def select_pick_of_week(items: Sequence[RecentItem]) -> Optional[RecentItem]:
    rated = [i for i in items if i.rating is not None and i.media_type in {"movie", "show"}]
    if not rated:
        rated = [i for i in items if i.media_type in {"movie", "show"}]
    if not rated:
        return None
    return max(rated, key=lambda i: (i.rating or 0.0, i.added_at))


def build_subject(pick: Optional[RecentItem], total_other: int) -> str:
    marquee = (pick.title if pick else "fresh additions")
    return f"Qflix · this week: {marquee} + {total_other} other things"


def build_email_context(
    *,
    recent: Sequence[RecentItem],
    coming: Sequence[CalendarItem],
    behind_scenes: Optional[BehindScenes],
    library_stats: dict,
    public_host: str,
    kuma_public_host: str,
    now: Optional[_dt.datetime] = None,
) -> EmailContext:
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)

    regular, anime = split_by_library(recent)

    movies = [r for r in regular if r.media_type == "movie"]
    episodes = [r for r in regular if r.media_type == "episode"]
    anime_movies = [r for r in anime if r.media_type == "movie"]
    anime_episodes = [r for r in anime if r.media_type == "episode"]

    movies.sort(key=lambda r: -(r.rating or 0.0))
    anime_movies.sort(key=lambda r: -(r.rating or 0.0))

    shows = group_episodes_by_show(episodes)
    anime_shows = group_episodes_by_show(anime_episodes)

    pick = select_pick_of_week(regular)
    other_count = len(movies) + len(shows) + len(anime_movies) + len(anime_shows) - (1 if pick else 0)
    if other_count < 0:
        other_count = 0
    subject = build_subject(pick, other_count)

    return EmailContext(
        week_label=now.strftime("%B %d, %Y"),
        pick=pick,
        movies=[m for m in movies if m is not pick],
        shows=shows,
        anime_movies=anime_movies,
        anime_shows=anime_shows,
        coming_soon=list(coming),
        behind_scenes=behind_scenes,
        nerd_corner=library_stats,
        subject=subject,
        public_host=public_host,
        kuma_public_host=kuma_public_host,
    )


def render_html(ctx: EmailContext, *, template_dir: Optional[Path] = None) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir or TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tmpl = env.get_template("weekly.html.j2")
    return tmpl.render(ctx=ctx)
