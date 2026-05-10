#!/usr/bin/env python3
"""Kill the OLDEST Plex stream when a single user has > MAX_STREAMS active.

Designed for cron @ 1-min cadence. Idempotent — if no kill needed, exits 0 silently.
Dry-run mode: print what would be killed without sending the terminate API call.

Adapted from JBOPS (https://github.com/blacktwin/JBOPS) under MIT.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from plexapi.server import PlexServer

DEFAULT_MAX_STREAMS_PER_USER = int(os.environ.get("KS_MAX_STREAMS_PER_USER", "2"))
KILL_MESSAGE = os.environ.get(
    "KS_MESSAGE",
    "Too many concurrent streams from this account. The oldest was stopped.",
)
STATE_FILE = Path.home() / ".apps" / "stream-stats" / "kill-history.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print decisions, do not terminate")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_STREAMS_PER_USER)
    args = parser.parse_args()

    plex_url = os.environ["PLEX_URL"]
    plex_token = os.environ["PLEX_TOKEN"]

    plex = PlexServer(plex_url, plex_token, timeout=10)
    sessions = plex.sessions()

    by_user: dict[str, list] = defaultdict(list)
    for s in sessions:
        user = (s.usernames[0] if s.usernames else "unknown").lower()
        by_user[user].append(s)

    decisions = []
    for user, streams in by_user.items():
        if len(streams) <= args.max:
            continue
        # Sort oldest first by viewOffset (closer to start = more recently begun is HIGHER offset
        # in a single-pass; opposite of intuition. Sort ascending so position [0] is the stream
        # that has *least progressed*, meaning *most-recently-started* — kill those first to be
        # least disruptive to people deep into a movie/show).
        streams.sort(key=lambda s: getattr(s, "viewOffset", 0) or 0)
        to_kill = streams[: len(streams) - args.max]
        for s in to_kill:
            decisions.append({
                "user": user,
                "session_id": s.sessionKey,
                "title": str(s),
                "action": "kill" if not args.dry_run else "would-kill",
            })
            if not args.dry_run:
                try:
                    s.stop(reason=KILL_MESSAGE)
                except Exception as e:
                    decisions[-1]["error"] = str(e)
                    decisions[-1]["action"] = "kill-failed"

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    history = []
    if STATE_FILE.exists():
        try:
            history = json.loads(STATE_FILE.read_text())[-99:]
        except (json.JSONDecodeError, OSError):
            history = []
    history.append({"ts": int(time.time()), "decisions": decisions})
    STATE_FILE.write_text(json.dumps(history, indent=2))

    if decisions:
        print(json.dumps(decisions, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
