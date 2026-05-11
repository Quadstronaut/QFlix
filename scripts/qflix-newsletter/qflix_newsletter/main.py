"""Top-level entry point: gather → render → deliver."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .ai import fetch_ai_picks
from .config import Config
from .delivery import create_and_send_campaign
from .posters import mirror_posters
from .render import build_email_context, render_html
from .sources import (
    enrich_with_tmdb,
    fetch_all_calendars,
    fetch_libraries_table,
    fetch_recently_added,
)

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DEFAULT_RECENT_COUNT = 50
DEFAULT_CALENDAR_DAYS = 14


def _build_library_stats(cfg: Config) -> dict:
    rows = fetch_libraries_table(cfg)
    total_count = 0
    by_section: list[dict] = []
    for r in rows:
        try:
            count = int(r.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        total_count += count
        by_section.append({"name": r.get("section_name", "?"), "count": count})
    return {"total_items": total_count, "sections": by_section}


def run(
    *,
    dry_run: bool = False,
    out_html: Optional[Path] = None,
    secrets_dir: Optional[Path] = None,
) -> int:
    log = logging.getLogger("qflix-newsletter")
    cfg = Config.from_env(secrets_dir=secrets_dir)

    recent = fetch_recently_added(cfg, count=DEFAULT_RECENT_COUNT)
    recent = enrich_with_tmdb(cfg, recent)
    recent = mirror_posters(
        recent,
        cache_dir=cfg.poster_cache_dir,
        public_base=f"https://{cfg.public_host}",
    )
    coming = fetch_all_calendars(cfg, days=DEFAULT_CALENDAR_DAYS)

    library_stats = _build_library_stats(cfg)

    library_titles = [r.title for r in recent if r.media_type in {"movie", "show"}][:25]
    recent_titles = [r.title for r in recent if r.media_type in {"movie", "episode"}][:10]
    ai_picks = fetch_ai_picks(cfg.gemini_api_key, library_titles, recent_titles)

    ctx = build_email_context(
        recent=recent,
        coming=coming,
        ai_picks=ai_picks,
        library_stats=library_stats,
        public_host=cfg.public_host,
    )
    html = render_html(ctx)

    if out_html:
        out_html.write_text(html, encoding="utf-8")
        log.info("wrote rendered HTML to %s (%d bytes)", out_html, len(html))

    if dry_run:
        log.info("dry-run: subject=%r body_bytes=%d", ctx.subject, len(html))
        return 0

    result = create_and_send_campaign(cfg, subject=ctx.subject, html_body=html)
    log.info(
        "campaign sent: id=%d status=%s archive=%s",
        result.campaign_id,
        result.status,
        result.archive_url,
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="qflix-newsletter", description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="render but don't send")
    parser.add_argument("--out-html", type=Path, default=None, help="write rendered HTML to this path")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--secrets-dir", type=Path, default=None, help="override ~/secrets")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
    )
    return run(dry_run=args.dry_run, out_html=args.out_html, secrets_dir=args.secrets_dir)


if __name__ == "__main__":
    sys.exit(main())
