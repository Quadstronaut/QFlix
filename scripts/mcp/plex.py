#!/usr/bin/env python3
"""scripts/mcp/plex.py — Plex library + sessions snapshot.

Must run inside the python-plexapi venv:
  ~/.apps/python-plexapi/venv/bin/python ~/scripts/mcp/plex.py --emit-json

Modes: --emit-json | --cron
Args:
  --include libraries,sessions,recent  (default all)
  --recent-hours 24
  --recent-max-per-library 20
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

SECRETS_DIR = Path(os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets")))


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except FileNotFoundError:
        return ""


def collect(include: set, recent_hours: int, recent_max: int) -> dict:
    try:
        from plexapi.server import PlexServer  # type: ignore
    except ImportError:
        return {"error": "plexapi-not-installed"}
    base = _read(SECRETS_DIR / "plex.host") or "127.0.0.1"
    port = _read(SECRETS_DIR / "plex.port") or "32400"
    token = _read(SECRETS_DIR / "plex.token")
    if not token:
        return {"error": "no-plex-token"}
    try:
        plex = PlexServer(f"http://{base}:{port}", token, timeout=10)
    except Exception as e:
        return {"error": f"plex-connect-failed: {e}"[:200]}

    out: dict = {}
    if "libraries" in include:
        libs = []
        cutoff = (dt.datetime.utcnow() - dt.timedelta(hours=recent_hours))
        for s in plex.library.sections():
            try:
                total = s.totalSize
            except Exception:
                total = 0
            recent = []
            try:
                recent = s.recentlyAdded(maxresults=recent_max)
            except Exception:
                recent = []
            recent_24h = sum(
                1 for r in recent
                if getattr(r, "addedAt", None) and r.addedAt >= cutoff
            )
            unanalyzed = 0
            # Show-type libraries iterate Show wrappers, not video files —
            # `.media` is always empty on a Show, so this check would 100%
            # false-positive every show. Movies only.
            if s.type == "movie":
                try:
                    sample = s.search(sort="addedAt:desc", limit=200)
                    unanalyzed = sum(
                        1 for it in sample
                        if not getattr(it, "media", None)
                        or not getattr(it.media[0], "videoCodec", None)
                    )
                except Exception:
                    unanalyzed = 0
            libs.append({
                "key": s.key,
                "title": s.title,
                "type": s.type,
                "count": total,
                "recently_added_24h": recent_24h,
                "unanalyzed_count": unanalyzed,
            })
        out["libraries"] = libs

    if "sessions" in include:
        try:
            out["active_sessions"] = len(plex.sessions())
        except Exception:
            out["active_sessions"] = 0

    out["last_scan"] = dt.datetime.utcnow().isoformat() + "Z"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--cron", action="store_true")
    ap.add_argument("--include", default="libraries,sessions")
    ap.add_argument("--recent-hours", type=int, default=24)
    ap.add_argument("--recent-max-per-library", type=int, default=20)
    args = ap.parse_args()
    include = {x.strip() for x in args.include.split(",") if x.strip()}
    res = collect(include, args.recent_hours, args.recent_max_per_library)
    if args.emit_json:
        json.dump(res, sys.stdout, default=str)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
