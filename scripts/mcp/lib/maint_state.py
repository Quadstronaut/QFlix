"""Reads ~/.opt/maint/state.json (populated by manitoba-maint-webhook.service)
to determine whether a given *arr's Kuma monitor is currently red.

Fail-closed: if the state file is unreadable or the monitor isn't named, we
report red (callers refuse to act on potentially-down apps).

Module is named `maint_state` (not `state`) because `scripts/maint/lib/state.py`
already exists. `lib/` is a namespace package — both files coexist on the
shared import root.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Optional

DEFAULT_STATE_FILE = Path(os.environ.get(
    "MANITOBA_MAINT_STATE",
    str(Path.home() / ".opt" / "maint" / "state.json"),
))

# Manifest slug → Kuma monitor name
_MONITOR_NAMES = {
    "sonarr": "Sonarr",
    "sonarr2": "Sonarr Anime",
    "radarr": "Radarr",
    "radarr2": "Radarr 2",
    "prowlarr": "Prowlarr",
    "qbittorrent": "qBittorrent",
    "plex": "Plex",
    "seerr": "Seerr",
}


def is_arr_red(slug: str, *, state_file: Optional[Path] = None) -> bool:
    """Returns True if the given *arr is currently flagged unhealthy.

    Reads ~/.opt/maint/state.json (populated by manitoba-maint-webhook).
    Schema: state["apps"][slug] = {event, final_health, kuma_status, ...}

    Semantics:
      - Unknown slug (not in _MONITOR_NAMES whitelist) → True (defensive)
      - state.json unreadable / missing / corrupt → True (fail-closed: no info)
      - Slug missing from apps block → False (fail-open: never had an event)
      - apps[slug].final_health == "down" → True
      - apps[slug].kuma_status == "down" → True
      - otherwise → False
    """
    if slug not in _MONITOR_NAMES:
        return True
    state_file = state_file or DEFAULT_STATE_FILE
    try:
        state = json.loads(Path(state_file).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return True
    entry = (state.get("apps") or {}).get(slug)
    if entry is None:
        return False
    if entry.get("final_health") == "down":
        return True
    if entry.get("kuma_status") == "down":
        return True
    return False
