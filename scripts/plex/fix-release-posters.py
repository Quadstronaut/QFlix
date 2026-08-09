#!/usr/bin/env python3
"""fix-release-posters — repoint Plex posters off release/piracy artwork.

Some torrent/usenet releases ship their own `poster.jpg` (or embed cover art
inside the video file) carrying the release group's branding. Plex picks that
up as a "local"/"embedded" poster and selects it, so members see abstract
group art instead of the real movie/show poster.

TWO parts (mirrors the operator decision 2026-07-26):

  --set-prefs   PREVENTION: set `useLocalAssets=false` on the movie/show
                libraries so Plex stops using release poster.jpg/folder art for
                FUTURE items. (Note: may not fully suppress art embedded INSIDE
                the video file — the flip below is the catch-all for those.)

  (default)     DRY-RUN the backlog flip: list every item whose SELECTED poster
                is provider local/embedded/screenshot AND has an agent
                (tmdb/gracenote/fanart/...) alternative.
  --apply       Do the flip: select the best agent poster instead.

Run on the seedbox via the python-plexapi venv:
    ~/.apps/python-plexapi/venv/bin/python scripts/plex/fix-release-posters.py [--apply] [--set-prefs]

Read-only by default. Idempotent: an item already on an agent poster is skipped.
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request

from plexapi.server import PlexServer  # type: ignore

# Plex is the Ultra.cc slot port on loopback (NOT 32400; NOT the docker IP,
# which the loopback secret's SSL-redirect logic mishandles - see
# 59-plex-anime-libraries.py). Overridable for dev.
PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:17025")
SECTIONS = ["QFlix - Movies", "QFlix - Anime Movies", "QFlix - TV", "QFlix - Anime"]

# A "real" (agent-supplied) poster provider vs the release-shipped ones.
AGENT_PROVIDERS = {"tmdb", "fanart", "gracenote", "plex", "imdb",
                   "thetvdb", "tvdb", "themoviedb"}
BAD_PROVIDERS = {"local", "embedded", "screenshot"}


def _token() -> str:
    with open(os.path.expanduser("~/secrets/plex.token")) as f:
        return f.read().strip()


def _put_pref(token: str, section_key: str, setting: str, value: str) -> int:
    url = "{}/library/sections/{}/prefs?{}={}&X-Plex-Token={}".format(
        PLEX_URL, section_key, setting, value, token)
    req = urllib.request.Request(url, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except Exception as exc:
        print("  ! prefs PUT failed for section {}: {}".format(section_key, exc))
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="perform the poster flips (default is dry-run)")
    ap.add_argument("--set-prefs", action="store_true",
                    help="set useLocalAssets=false on the managed libraries")
    args = ap.parse_args()

    token = _token()
    plex = PlexServer(PLEX_URL, token)

    if args.set_prefs:
        print("=== prevention: useLocalAssets=false ===")
        for name in SECTIONS:
            try:
                sec = plex.library.section(name)
            except Exception as exc:
                print("  skip {}: {}".format(name, exc))
                continue
            code = _put_pref(token, str(sec.key), "useLocalAssets", "0")
            print("  {} (key={}) -> HTTP {}".format(name, sec.key, code))

    print("=== poster flip ({}) ===".format("APPLY" if args.apply else "dry-run"))
    planned = flipped = errs = 0
    for name in SECTIONS:
        try:
            sec = plex.library.section(name)
        except Exception as exc:
            print("  skip {}: {}".format(name, exc))
            continue
        for item in sec.all():
            try:
                posters = item.posters()
            except Exception:
                continue
            sel = next((p for p in posters if p.selected), None)
            if sel is None:
                continue
            if (sel.provider or "").lower() not in BAD_PROVIDERS:
                continue
            agent = next((p for p in posters
                          if (p.provider or "").lower() in AGENT_PROVIDERS), None)
            if agent is None:
                continue
            planned += 1
            verb = "FLIP" if args.apply else "PLAN"
            print("  {} {:18s} | {:45s} | {} -> {}".format(
                verb, name, (item.title or "?")[:45],
                sel.provider or "(none)", agent.provider))
            if args.apply:
                try:
                    agent.select()
                    flipped += 1
                except Exception as exc:
                    errs += 1
                    print("     ! {}".format(exc))

    print("planned={} flipped={} errs={} apply={}".format(
        planned, flipped, errs, args.apply))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
