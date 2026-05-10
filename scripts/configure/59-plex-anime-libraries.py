#!/usr/bin/env python3
"""Add Anime + Anime Movies libraries to Plex pointing at ~/media/Anime
and ~/media/Anime Movies. Idempotent — skips if a library with the same
name already exists.

After creation, this script does NOT trigger the Maintainerr 60-day rule
re-run — invoke 27b-maintainerr-rules.py separately for that.

Run on the seedbox via the python-plexapi venv:
    ~/.apps/python-plexapi/venv/bin/python 59-plex-anime-libraries.py
"""
from __future__ import annotations

import os
import sys

from plexapi.server import PlexServer  # type: ignore


def secret(name: str) -> str:
    with open(os.path.expanduser(f"~/secrets/{name}")) as f:
        return f.read().strip()


LIBS = [
    {
        "name": "Anime",
        "type": "show",
        "agent": "tv.plex.agents.series",
        "scanner": "Plex TV Series",
        "location": "/home/quadstronaut/media/Anime",
        "language": "en-US",
    },
    {
        "name": "Anime Movies",
        "type": "movie",
        "agent": "tv.plex.agents.movie",
        "scanner": "Plex Movie",
        "location": "/home/quadstronaut/media/Anime Movies",
        "language": "en-US",
    },
]


def main() -> int:
    # Plex runs in Docker at 172.17.1.250:32400 — see CLAUDE memory.
    # The seedbox loopback secret (plex.host=127.0.0.1) triggers an SSL
    # redirect from plexapi's relay logic; use the container IP+port directly.
    host = "172.17.1.250"
    port = 32400
    token = secret("plex.token")
    plex = PlexServer(f"http://{host}:{port}", token)

    existing = {s.title for s in plex.library.sections()}
    created = 0
    for lib in LIBS:
        if lib["name"] in existing:
            print(f"[skip] '{lib['name']}' already exists in Plex")
            continue
        if not os.path.isdir(lib["location"]):
            print(f"[skip] '{lib['name']}': location missing: {lib['location']}",
                  file=sys.stderr)
            continue
        try:
            plex.library.add(**lib)
            print(f"[create] '{lib['name']}' → {lib['location']} "
                  f"({lib['type']})")
            created += 1
        except Exception as exc:
            print(f"[fail] '{lib['name']}': {exc}", file=sys.stderr)
    print(f"\nLibraries created: {created}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
