#!/usr/bin/env python3
"""scripts/mcp/missing.py — fire MissingSearch on every *arr.

Modes: --emit-json | --cron
Args:  --slug <name>   (default: all *arrs)

Replaces arr-housekeeping.py's cmd_missing. Daily 07:00 UTC = 00:00 Phoenix.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maint"))

from lib.arr_client import ArrClient  # noqa: E402

ARRS = [
    ("sonarr", "v3", "MissingEpisodeSearch"),
    ("sonarr2", "v3", "MissingEpisodeSearch"),
    ("radarr", "v3", "MissingMoviesSearch"),
    ("radarr2", "v3", "MissingMoviesSearch"),
]


def run(*, slug: Optional[str] = None) -> dict:
    targets = [(s, v, c) for (s, v, c) in ARRS if slug is None or s == slug]
    out = {"per_arr": {}}
    for s, ver, cmd in targets:
        c = ArrClient(s, ver)
        code, payload = c.post("/command", body={"name": cmd})
        if code in (200, 201):
            out["per_arr"][s] = {
                "status": "queued",
                "command": cmd,
                "command_id": (payload or {}).get("id") if isinstance(payload, dict) else None,
            }
        else:
            out["per_arr"][s] = {"status": "failed", "code": code}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--cron", action="store_true")
    ap.add_argument("--slug")
    args = ap.parse_args()
    res = run(slug=args.slug)

    if args.cron:
        # Discord notification on any failure
        any_fail = any(r["status"] != "queued" for r in res["per_arr"].values())
        if any_fail:
            try:
                from lib.notify import notify  # type: ignore
                notify(f"missing.py: failures = {[s for s, r in res['per_arr'].items() if r['status'] != 'queued']}", "error")
            except Exception:
                pass

    if args.emit_json:
        # With --emit-json, the caller (MCP server) needs the structured
        # per_arr dict to surface individual failures. Returning exit 1
        # caused MCP to discard the body as ssh-failed, masking which arr
        # actually broke. Always return 0 in JSON mode — the JSON body
        # carries the failure detail.
        json.dump(res, sys.stdout, default=str)
        sys.stdout.write("\n")
        return 0
    return 0 if all(r["status"] == "queued" for r in res["per_arr"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
