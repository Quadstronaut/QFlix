#!/usr/bin/env python3
"""scripts/mcp/arr_disk_usage.py — bytes on disk managed by one *arr.

Sums the *arr's own sizeOnDisk rather than walking the filesystem: the box is
a shared seedbox with hardlinked seeding copies, so `du` would double-count
what the *arr considers one file.

PRIVACY: reports a byte total and a title COUNT only — never a title, path,
or any other identifying field off the raw *arr record. This plan has
produced three real member-data leaks already (see MEMORY.md); this script
does not pass *arr records through.

DEVIATION from brief (same correction Task 6 landed for arr_library_peek.py,
verified against scripts/mcp/lib/arr_client.py and every existing caller —
missing.py, unstick.py, quality_fallback.py, collect.py):
  1. ArrClient's constructor is `ArrClient(slug, version, ...)` — `version`
     is a required positional arg. `_default_client` passes "v3" explicitly,
     matching every sibling script.
  2. ArrClient.get(path) returns an (http_code, payload) TUPLE, and `path` is
     relative to the version root ArrClient already builds into the URL
     (e.g. "/series", not "/api/v3/series"). `_rows()` below unwraps that
     tuple and raises on a non-200 or non-list payload, so a dead/misbehaving
     *arr surfaces as an error rather than as "0 bytes" — which on the phone
     would read as catastrophic data loss, not a transport blip.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maint"))

SERIES_SLUGS = ("sonarr", "sonarr2")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def _default_client(slug: str):
    # ArrClient(slug, version) - version is a REQUIRED positional, and every
    # real caller passes "v3" (see missing.py, unstick.py, arr_library_peek.py).
    from lib.arr_client import ArrClient
    return ArrClient(slug, "v3")


def _rows(result):
    """ArrClient.get() returns (http_code, payload). Anything non-200, or a
    payload that is not a list, is an error the caller must see rather than a
    quietly-zero disk total."""
    code, payload = result
    if code != 200:
        raise RuntimeError("arr returned HTTP %s" % code)
    if not isinstance(payload, list):
        raise RuntimeError("arr returned %s, expected a list" % type(payload).__name__)
    return payload


def usage(slug: str, client=None) -> dict:
    out = {"slug": slug, "bytes": 0, "human": "0.0 B",
           "title_count": 0, "ok": True, "error": ""}
    try:
        c = client if client is not None else _default_client(slug)
        total = 0
        if slug in SERIES_SLUGS:
            rows = _rows(c.get("/series"))
            for s in rows:
                total += int((s.get("statistics") or {}).get("sizeOnDisk") or 0)
        else:
            rows = _rows(c.get("/movie"))
            for m in rows:
                total += int(m.get("sizeOnDisk") or 0)
        out["bytes"] = total
        out["human"] = human(total)
        out["title_count"] = len(rows)
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit-json", action="store_true", required=True)
    ap.add_argument("--slug", required=True)
    args = ap.parse_args()
    # See Global Constraints: --emit-json always exits 0, failure detail
    # rides in the JSON body's ok/error fields (usage() never raises).
    json.dump(usage(args.slug), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
