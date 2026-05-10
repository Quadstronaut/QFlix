#!/usr/bin/env python3
"""Emit current Plex stream state to ~/.apps/stream-stats/state.json.

Designed for cron @ 1-min cadence. Idempotent — overwrites state.json each run
via temp-file + rename so readers never see a half-written file.

Output JSON shape (stable contract for the future phone APK / dashboard):

  {
    "ts": 1715000000,
    "active_streams": 3,
    "by_user": {"alice": 1, "bob": 2},
    "streams": [
      {"user": "alice", "title": "...", "state": "playing", "transcode": false, "media_type": "movie"}
    ]
  }
"""
from __future__ import annotations
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from plexapi.server import PlexServer

STATE_FILE = Path.home() / ".apps" / "stream-stats" / "state.json"


def main() -> int:
    plex = PlexServer(os.environ["PLEX_URL"], os.environ["PLEX_TOKEN"], timeout=10)
    sessions = plex.sessions()

    streams = []
    user_counter: Counter[str] = Counter()
    for s in sessions:
        user = (s.usernames[0] if s.usernames else "unknown").lower()
        user_counter[user] += 1
        streams.append({
            "user": user,
            "title": str(s),
            "state": getattr(s.player, "state", "unknown") if s.player else "unknown",
            "transcode": bool(getattr(s, "transcodeSession", None)),
            "media_type": getattr(s, "type", None),
        })

    payload = {
        "ts": int(time.time()),
        "active_streams": len(sessions),
        "by_user": dict(user_counter),
        "streams": streams,
    }

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(STATE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
