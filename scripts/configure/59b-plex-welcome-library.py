#!/usr/bin/env python3
"""59b-plex-welcome-library.py -- create the `QFlix - Welcome` Plex section.

Idempotent. Run it as many times as you like; it creates the folder and the
section only if they are absent and otherwise reports and exits 0.

WHY THIS SECTION EXISTS
-----------------------
It is the FLOOR of the entitlement gate: the one library a person sees when
they have accepted an invite but are not entitled, and the one library they are
returned to if entitlement is withdrawn. Both stages are the same on purpose --
a revoked member is put back in front of the pitch, not evicted from the
building.

It holds the operator's community-invite video. An EMPTY section is fine and is
the expected state on first install; the share works, Plex just shows nothing
until a file is dropped in and scanned.

THE SECTION MUST EXIST BEFORE THE GATE IS ARMED
-----------------------------------------------
`lib/plexshare.minimum_access_ids()` raises when it cannot find this title,
precisely so the gate refuses to run rather than computing an empty section
list -- which plex.tv reads as "unshare this server" and which would delete the
share and evict the person instead of restricting them.

A SIDE EFFECT WORTH KNOWING
---------------------------
Every pre-existing share on this server carries `allLibraries="1"`. Creating a
new section therefore hands it to all existing members immediately, with no
action from anyone. That is harmless (it is a welcome video) and is why this
script is safe to run before the gate exists.

Run on the box, inside the plexapi venv:
    ~/.apps/python-plexapi/venv/bin/python ~/scripts/configure/59b-plex-welcome-library.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SECRETS = Path(os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets")))
DEFAULT_TITLE = "QFlix - Welcome"
DEFAULT_DIR = str(Path.home() / "media" / "Welcome")

# Matched to how the existing QFlix movie libraries are configured. The modern
# agent ('tv.plex.agents.movie') with the 'Plex Movie' scanner is what Plex
# creates for a new movie library today; the legacy com.plexapp.agents.* pair
# still works but is deprecated and produces a library Plex nags about.
AGENT = "tv.plex.agents.movie"
SCANNER = "Plex Movie"


def _read(name: str) -> str:
    try:
        return (SECRETS / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default=DEFAULT_TITLE)
    ap.add_argument("--path", default=DEFAULT_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        from plexapi.server import PlexServer
    except ImportError:
        print("plexapi is not importable. Run this with the venv interpreter:\n"
              "  ~/.apps/python-plexapi/venv/bin/python %s" % sys.argv[0],
              file=sys.stderr)
        return 2

    token = _read("plex.token")
    port = _read("plex.port") or "32400"
    host = _read("plex.host") or "127.0.0.1"
    if not token:
        print("no plex.token in %s" % SECRETS, file=sys.stderr)
        return 2

    plex = PlexServer("http://%s:%s" % (host, port), token, timeout=30)

    existing = {s.title.strip().lower(): s for s in plex.library.sections()}
    if args.title.strip().lower() in existing:
        sec = existing[args.title.strip().lower()]
        print("section %r already exists (key=%s, type=%s, locations=%s) - nothing to do"
              % (sec.title, sec.key, sec.type, list(sec.locations)))
        return 0

    folder = Path(args.path)
    if args.dry_run:
        print("DRY RUN: would mkdir %s and create section %r (%s / %s)"
              % (folder, args.title, AGENT, SCANNER))
        return 0

    # The folder must exist first: Plex validates the location and refuses to
    # create a library pointed at a path it cannot see.
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".plexignore").touch(exist_ok=True)
    print("folder ready: %s" % folder)

    plex.library.add(
        name=args.title,
        type="movie",
        agent=AGENT,
        scanner=SCANNER,
        language="en-US",
        location=str(folder),
    )

    # Read back rather than trusting the create call. A section that did not
    # actually appear would make the gate refuse to run at the worst moment.
    plex.library.reload()
    made = next((s for s in plex.library.sections()
                 if s.title.strip().lower() == args.title.strip().lower()), None)
    if made is None:
        print("section %r was not created - Plex accepted the call but the "
              "library is absent" % args.title, file=sys.stderr)
        return 1

    print("created section %r (key=%s, type=%s, locations=%s)"
          % (made.title, made.key, made.type, list(made.locations)))
    print("\nNEXT: drop the community-invite video into %s and it will scan in."
          % folder)
    print("Every existing share has allLibraries=1, so they all received this "
          "section already.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
