"""Sync rendered Jinja templates into Listmonk as named templates.

Each .j2 template under ``templates/`` is rendered against a representative
preview context, then POSTed (new) or PUT (existing) to Listmonk's
``/api/templates`` endpoint. The template's display name in Listmonk is
``"<Env> · <Title>"`` where ``Env=Prod|Stage`` and ``Title`` is the
operator-visible channel name.

The branch determines the env:
  master  → --env prod  → "Prod · Weekly Digest" etc.
  staging → --env stage → "Stage · Weekly Digest" etc.

Run: ``python -m qflix_newsletter.sync --env prod``

Idempotent — re-running just upserts the latest rendered HTML against
the matching name. Templates that exist but aren't in TEMPLATE_TITLES
are left alone (Listmonk's two default templates stay untouched).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .ai import AiPick
from .config import Config
from .render import EmailContext, ShowGroup
from .sources import CalendarItem, RecentItem

logger = logging.getLogger(__name__)
TEMPLATE_DIR = Path(__file__).parent / "templates"

TEMPLATE_TITLES = {
    "weekly.html.j2": "Weekly Digest",
    "maint-start.html.j2": "Maintenance Window Start",
    "maint-complete.html.j2": "Maintenance Window Complete",
}


def _preview_context(public_host: str, kuma_public_host: str) -> EmailContext:
    """Representative sample data so the rendered preview shows the design
    with realistic content (not an empty shell). Mirrors tests/conftest.py
    but lives in the package so sync.py has no test-code dependency."""
    pick = RecentItem(
        media_type="movie", title="Suzume", year=2022,
        summary="Sample preview content. Real digests render against live "
                "Tautulli + Sonarr/Radarr data.",
        thumb_url=f"https://image.tmdb.org/t/p/w300/sample.jpg",
        added_at=1700000000, rating=10.0, library_name="Movies",
    )
    movies = [
        RecentItem(media_type="movie", title="Sample Movie A", year=2025,
                   summary="", thumb_url=None, added_at=0, rating=8.5,
                   library_name="Movies"),
        RecentItem(media_type="movie", title="Sample Movie B", year=2024,
                   summary="", thumb_url=None, added_at=0, rating=7.2,
                   library_name="Movies"),
    ]
    shows = [ShowGroup(
        show_title="Sample Show",
        episodes=[
            RecentItem(media_type="episode", title="Ep 1", year=2026,
                       summary="", thumb_url=None, added_at=0, rating=None,
                       show_title="Sample Show", season=1, episode=1,
                       library_name="TV Shows"),
        ],
    )]
    coming = [CalendarItem(
        media_type="tv", title="Pilot",
        air_date=_dt.date.today() + _dt.timedelta(days=7),
        show_title="Upcoming Show", season=1, episode=1,
    )]
    ai_picks = [AiPick(
        if_you_liked="Spirited Away",
        try_this="The Tale of the Princess Kaguya",
        blurb="Same emotional register, different visual language.",
    )]
    return EmailContext(
        week_label=_dt.datetime.utcnow().strftime("%B %d, %Y"),
        pick=pick, movies=movies, shows=shows,
        anime_movies=[], anime_shows=[], coming_soon=coming,
        ai_picks=ai_picks,
        nerd_corner={"total_items": 12345,
                     "sections": [{"name": "Movies", "count": 1234}]},
        subject="QFlix preview",
        public_host=public_host,
        kuma_public_host=kuma_public_host,
    )


def render_preview(template_filename: str, public_host: str, kuma_public_host: str) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True, lstrip_blocks=True,
    )
    tmpl = env.get_template(template_filename)
    return tmpl.render(ctx=_preview_context(public_host, kuma_public_host))


@dataclass
class _ListmonkClient:
    base_url: str
    auth: tuple[str, str]

    def list_templates(self) -> dict[str, int]:
        r = requests.get(f"{self.base_url}/api/templates",
                         auth=self.auth, timeout=30)
        r.raise_for_status()
        return {t["name"]: t["id"] for t in r.json()["data"]}

    def upsert_template(self, name: str, body: str,
                        existing_id: Optional[int]) -> int:
        # Listmonk requires `{{ template "content" . }}` exactly once in any
        # type=campaign template body. Our previews are self-contained, so
        # we tack the slot on as an HTML comment at the very end where it
        # won't render visibly.
        wrapped = body + '\n<!--{{ template "content" . }}-->\n'
        payload = {"name": name, "type": "campaign", "body": wrapped,
                   "subject": ""}
        if existing_id is None:
            r = requests.post(f"{self.base_url}/api/templates",
                              json=payload, auth=self.auth, timeout=30)
            r.raise_for_status()
            return int(r.json()["data"]["id"])
        r = requests.put(f"{self.base_url}/api/templates/{existing_id}",
                         json=payload, auth=self.auth, timeout=30)
        r.raise_for_status()
        return existing_id


def sync(env: str, *, secrets_dir: Optional[Path] = None) -> int:
    if env not in {"prod", "stage"}:
        raise ValueError(f"env must be 'prod' or 'stage', got {env!r}")
    prefix = "Prod" if env == "prod" else "Stage"

    cfg = Config.from_env(secrets_dir=secrets_dir)
    client = _ListmonkClient(
        base_url=cfg.listmonk_base_url,
        auth=(cfg.listmonk_api_user, cfg.listmonk_api_token),
    )
    existing = client.list_templates()

    for filename, title in TEMPLATE_TITLES.items():
        full_name = f"{prefix} · {title}"
        body = render_preview(filename, cfg.public_host, cfg.kuma_public_host)
        tid = client.upsert_template(full_name, body,
                                     existing.get(full_name))
        verb = "updated" if full_name in existing else "created"
        logger.info("%s template id=%d name=%r (%d bytes)",
                    verb, tid, full_name, len(body))

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, choices=("prod", "stage"))
    parser.add_argument("--secrets-dir", type=Path, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return sync(args.env, secrets_dir=args.secrets_dir)


if __name__ == "__main__":
    sys.exit(main())
