"""Data fetchers: Tautulli, Sonarr/Radarr calendar, TMDB ratings."""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Iterable, Optional

import requests

from .config import ArrEndpoint, Config

DEFAULT_TIMEOUT_S = 15


@dataclass
class RecentItem:
    """One Tautulli `recently_added` row, normalized."""

    media_type: str
    title: str
    year: Optional[int]
    summary: str
    thumb_url: Optional[str]
    added_at: int
    rating: Optional[float]
    show_title: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    library_name: Optional[str] = None
    tmdb_id: Optional[int] = None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class CalendarItem:
    """Sonarr or Radarr calendar row, normalized."""

    media_type: str  # "movie" | "tv"
    title: str
    air_date: _dt.date
    show_title: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    overview: Optional[str] = None
    network: Optional[str] = None


def _tautulli_call(cfg: Config, cmd: str, **params) -> dict:
    p = {"apikey": cfg.tautulli_key, "cmd": cmd, **params}
    r = requests.get(f"{cfg.tautulli_url}/api/v2", params=p, timeout=DEFAULT_TIMEOUT_S)
    r.raise_for_status()
    body = r.json()
    if body.get("response", {}).get("result") != "success":
        raise RuntimeError(f"tautulli {cmd} failed: {body}")
    return body["response"].get("data") or {}


def fetch_recently_added(cfg: Config, count: int = 50) -> list[RecentItem]:
    data = _tautulli_call(cfg, "get_recently_added", count=count)
    raw_rows = data.get("recently_added", []) or []
    # Use the public Tautulli URL for thumb proxy — emails are read by Gmail/etc.
    # which can't reach the seedbox loopback. The internal cfg.tautulli_url
    # stays loopback for API calls (lower latency, no nginx round-trip).
    public_tautulli = f"https://{cfg.public_host}/tautulli"
    out: list[RecentItem] = []
    for row in raw_rows:
        out.append(_recent_from_tautulli(row, public_tautulli))
    return out


def _recent_from_tautulli(row: dict, tautulli_base: str) -> RecentItem:
    mt = row.get("media_type", "")
    is_episode = mt == "episode"
    thumb = row.get("thumb") or row.get("art")
    thumb_url = (
        f"{tautulli_base}/pms_image_proxy?img={thumb}&width=300&height=450&fallback=poster"
        if thumb
        else None
    )
    rating_raw = row.get("rating") or row.get("audience_rating")
    try:
        rating = float(rating_raw) if rating_raw not in (None, "") else None
    except (TypeError, ValueError):
        rating = None
    year_raw = row.get("year")
    try:
        year = int(year_raw) if year_raw not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    season_raw = row.get("parent_media_index")
    episode_raw = row.get("media_index")
    return RecentItem(
        media_type=mt,
        title=row.get("title") or "",
        year=year,
        summary=row.get("summary") or "",
        thumb_url=thumb_url,
        added_at=int(row.get("added_at") or 0),
        rating=rating,
        show_title=row.get("grandparent_title") if is_episode else None,
        season=int(season_raw) if season_raw not in (None, "") else None,
        episode=int(episode_raw) if episode_raw not in (None, "") else None,
        library_name=row.get("library_name"),
        tmdb_id=_extract_tmdb_id(row),
        raw=row,
    )


def _extract_tmdb_id(row: dict) -> Optional[int]:
    guids = row.get("guids") or row.get("Guid") or []
    for g in guids:
        if isinstance(g, dict):
            v = g.get("id") or g.get("value", "")
        else:
            v = str(g)
        if isinstance(v, str) and v.startswith("tmdb://"):
            try:
                return int(v.removeprefix("tmdb://").split("?")[0])
            except ValueError:
                continue
    return None


def fetch_calendar(arr: ArrEndpoint, days: int, now: Optional[_dt.datetime] = None) -> list[CalendarItem]:
    if now is None:
        now = _dt.datetime.utcnow()
    end = now + _dt.timedelta(days=days)
    params = {
        "start": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "includeSeries": "true",
    }
    headers = {"X-Api-Key": arr.api_key}
    r = requests.get(f"{arr.base_url}/api/v3/calendar", params=params, headers=headers, timeout=DEFAULT_TIMEOUT_S)
    r.raise_for_status()
    rows = r.json() or []
    out: list[CalendarItem] = []
    for row in rows:
        out.append(_calendar_from_arr(row))
    return out


def _calendar_from_arr(row: dict) -> CalendarItem:
    if "seriesId" in row or "series" in row:
        air = row.get("airDateUtc") or row.get("airDate") or ""
        air_date = _parse_date(air)
        series = row.get("series") or {}
        return CalendarItem(
            media_type="tv",
            title=row.get("title") or "",
            air_date=air_date,
            show_title=series.get("title") or row.get("seriesTitle"),
            season=row.get("seasonNumber"),
            episode=row.get("episodeNumber"),
            overview=row.get("overview"),
            network=series.get("network"),
        )
    air = row.get("digitalRelease") or row.get("physicalRelease") or row.get("inCinemas") or ""
    return CalendarItem(
        media_type="movie",
        title=row.get("title") or "",
        air_date=_parse_date(air),
        overview=row.get("overview"),
    )


def _parse_date(s: str) -> _dt.date:
    if not s:
        return _dt.date.today()
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return _dt.datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return _dt.date.today()


def fetch_all_calendars(cfg: Config, days: int) -> list[CalendarItem]:
    items: list[CalendarItem] = []
    items.extend(fetch_calendar(cfg.sonarr, days))
    if cfg.sonarr_anime is not None:
        items.extend(fetch_calendar(cfg.sonarr_anime, days))
    items.extend(fetch_calendar(cfg.radarr, days))
    if cfg.radarr_anime is not None:
        items.extend(fetch_calendar(cfg.radarr_anime, days))
    items.sort(key=lambda c: c.air_date)
    return items


def fetch_libraries_table(cfg: Config) -> list[dict]:
    data = _tautulli_call(cfg, "get_libraries_table")
    return data.get("data", []) or []


def fetch_tmdb_rating(read_token: str, tmdb_id: int, kind: str) -> Optional[float]:
    """Return TMDB vote_average for a given tmdb_id. `kind` is 'movie' or 'tv'."""
    if not read_token or not tmdb_id:
        return None
    url = f"https://api.themoviedb.org/3/{kind}/{tmdb_id}"
    headers = {"Authorization": f"Bearer {read_token}"}
    try:
        r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT_S)
        if r.status_code != 200:
            return None
        v = r.json().get("vote_average")
        return float(v) if v is not None else None
    except (requests.RequestException, ValueError):
        return None


def enrich_with_tmdb(cfg: Config, items: Iterable[RecentItem]) -> list[RecentItem]:
    if not cfg.tmdb_read_token:
        return list(items)
    out: list[RecentItem] = []
    for item in items:
        if item.rating is not None or not item.tmdb_id:
            out.append(item)
            continue
        kind = "movie" if item.media_type == "movie" else "tv"
        rating = fetch_tmdb_rating(cfg.tmdb_read_token, item.tmdb_id, kind)
        if rating is not None:
            item.rating = rating
        out.append(item)
    return out
