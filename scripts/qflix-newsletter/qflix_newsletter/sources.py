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
    tautulli_thumb_url: Optional[str] = None
    show_title: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    library_name: Optional[str] = None
    tmdb_id: Optional[int] = None
    rating_key: Optional[str] = None
    grandparent_rating_key: Optional[str] = None
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
    rk_raw = row.get("rating_key")
    grk_raw = row.get("grandparent_rating_key")
    return RecentItem(
        media_type=mt,
        title=row.get("title") or "",
        year=year,
        summary=row.get("summary") or "",
        thumb_url=thumb_url,
        tautulli_thumb_url=thumb_url,  # preserved for fallback after TMDB enrich
        added_at=int(row.get("added_at") or 0),
        rating=rating,
        show_title=row.get("grandparent_title") if is_episode else None,
        season=int(season_raw) if season_raw not in (None, "") else None,
        episode=int(episode_raw) if episode_raw not in (None, "") else None,
        library_name=row.get("library_name"),
        tmdb_id=_extract_tmdb_id(row),
        rating_key=str(rk_raw) if rk_raw not in (None, "") else None,
        grandparent_rating_key=str(grk_raw) if grk_raw not in (None, "") else None,
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
    """Pull /calendar?days=N from each configured *arr. A single arr
    failure (e.g. Sonarr2 in a bad state during the Monday maintenance
    window) used to raise out and drop the entire coming-soon section
    plus everything downstream. Now each fetch is wrapped so one bad arr
    degrades that section by 25%, not the whole email."""
    import logging
    log = logging.getLogger(__name__)
    items: list[CalendarItem] = []
    attempts: list[tuple[str, Optional[ArrEndpoint]]] = [
        ("sonarr",       cfg.sonarr),
        ("sonarr_anime", cfg.sonarr_anime),
        ("radarr",       cfg.radarr),
        ("radarr_anime", cfg.radarr_anime),
    ]
    for label, arr in attempts:
        if arr is None:
            continue
        try:
            items.extend(fetch_calendar(arr, days))
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            log.warning(
                "fetch_all_calendars: %s calendar fetch failed (%s) — "
                "section degraded but other arrs continue",
                label, exc,
            )
    items.sort(key=lambda c: c.air_date)
    return items


def fetch_libraries_table(cfg: Config) -> list[dict]:
    data = _tautulli_call(cfg, "get_libraries_table")
    return data.get("data", []) or []


def tmdb_search(read_token: str, kind: str, query: str, year: Optional[int] = None) -> dict:
    """Hit TMDB /search/{movie|tv}; return the first result (or {}).

    Searching TMDB by title (rather than resolving a TMDB id via Tautulli
    `get_metadata` and then hitting /movie/{id} or /tv/{id}) keeps the
    pipeline independent of Tautulli's Plex connection. That was originally
    a workaround for the `plex.direct` DNS bug — Tautulli's
    `get_metadata_details` silently returned None when its outbound call
    to PMS failed (fixed in scripts/configure/50-tautulli-pms-url-fix.sh
    by pinning pms_url to the local IP). The workaround stayed because
    title-search is cheaper, public-CDN-friendly, and resilient to future
    Tautulli/Plex hiccups.
    """
    if not read_token or not query:
        return {}
    url = f"https://api.themoviedb.org/3/search/{kind}"
    headers = {"Authorization": f"Bearer {read_token}"}
    params: dict = {"query": query, "include_adult": "false"}
    if year and kind == "movie":
        params["year"] = year
    if year and kind == "tv":
        params["first_air_date_year"] = year
    try:
        r = requests.get(url, headers=headers, params=params, timeout=DEFAULT_TIMEOUT_S)
        if r.status_code == 429:
            # TMDB rate limit — surface to the operator log so a posterless
            # render isn't silently attributed to "TMDB just doesn't have it".
            import logging
            retry_after = r.headers.get("Retry-After", "?")
            logging.getLogger(__name__).warning(
                "tmdb_search: 429 rate-limited (Retry-After=%s) — falling back"
                " to TMDB-less render for this item", retry_after,
            )
            return {}
        if r.status_code != 200:
            return {}
        results = (r.json() or {}).get("results") or []
        return results[0] if results else {}
    except (requests.RequestException, ValueError):
        return {}


def enrich_with_tmdb(cfg: Config, items: Iterable[RecentItem]) -> list[RecentItem]:
    """Rewrite each item's `thumb_url` to a TMDB image-CDN URL so mail
    clients can render it (the prior Tautulli /pms_image_proxy URL was
    session-cookie gated and never rendered in email), and backfill
    `rating` from TMDB's vote_average.

    Resolution uses TMDB search-by-title:
      * movies → /search/movie?query=<title>&year=<year>
      * episodes → /search/tv?query=<show_title>  (one call per show, deduped)
      * other types (e.g. `season`) → no poster.

    Items that can't be matched have `thumb_url` set to None so the
    template skips the <img> tag gracefully instead of showing a
    broken-image icon (which is what every recipient currently sees).
    """
    items = list(items)
    if not cfg.tmdb_read_token:
        return items

    movie_cache: dict[tuple[str, Optional[int]], dict] = {}
    show_cache: dict[str, dict] = {}

    for item in items:
        if item.media_type == "movie":
            key = (item.title.lower().strip(), item.year)
            result = movie_cache.get(key)
            if result is None:
                result = tmdb_search(cfg.tmdb_read_token, "movie", item.title, item.year)
                movie_cache[key] = result
        elif item.media_type == "episode":
            show = (item.show_title or item.title).strip()
            key2 = show.lower()
            result = show_cache.get(key2)
            if result is None:
                result = tmdb_search(cfg.tmdb_read_token, "tv", show)
                show_cache[key2] = result
        else:
            item.thumb_url = None
            continue

        poster = result.get("poster_path") if result else None
        if poster:
            item.thumb_url = f"https://image.tmdb.org/t/p/w342{poster}"
        else:
            item.thumb_url = None

        if item.rating is None and result:
            v = result.get("vote_average")
            if v not in (None, ""):
                try:
                    item.rating = float(v)
                except (TypeError, ValueError):
                    pass

    return items
