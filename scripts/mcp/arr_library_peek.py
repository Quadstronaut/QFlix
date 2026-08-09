#!/usr/bin/env python3
"""scripts/mcp/arr_library_peek.py — coarse content-presence peek for one *arr.

Answers "do we have it", never "did anyone watch it" — see the privacy
constraint in docs/superpowers/specs/2026-08-03-qflix-admin-android-design.md.
Prior tasks in this plan leaked member/consumption data twice (app_status.py's
top5, logs.py reaching listmonk/tautulli/seerr/plex); this script takes ONLY
title + file-presence counts off the *arr record and drops everything else —
no path, no quality, no added/download timestamps, nothing that traces back
to who requested or watched anything.

Series  -> have/total episode counts per show.
Movies  -> present/absent per film (have/total are 1/1 or 0/1 so one shape
           serves both and the phone renders a single row type).

Deliberately coarse: the operator asked for a peek, not library statistics.

DEVIATION from brief (fix round 1, verified against scripts/mcp/lib/arr_client.py
and every existing caller — missing.py, unstick.py, quality_fallback.py,
collect.py):
  1. ArrClient's constructor is `ArrClient(slug, version, ...)` — `version`
     is a required positional arg. `_default_client` passes "v3" explicitly,
     matching every sibling script.
  2. ArrClient.get(path) returns an (http_code, payload) TUPLE, and `path` is
     relative to the version root ArrClient already builds into the URL
     (missing.py calls "/command", not "/api/v3/command"; quality_fallback.py
     calls "/series", not "/api/v3/series"). `_rows()` below unwraps that
     tuple and raises on a non-200 or non-list payload; `peek()` calls
     "/series" / "/movie" through it. The test fakes mirror this real shape
     (tuple return, version-relative path) so the production path
     (client=None -> _default_client -> the real ArrClient) is exercised by
     the same contract the tests check, not a fake built to a different API.
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
MOVIE_SLUGS = ("radarr", "radarr2")


def _default_client(slug: str):
    # ArrClient(slug, version) - version is a REQUIRED positional, and every
    # real caller passes "v3" (see missing.py, unstick.py).
    from lib.arr_client import ArrClient
    return ArrClient(slug, "v3")


def _rows(result):
    """ArrClient.get() returns (http_code, payload). Anything non-200, or a
    payload that is not a list, is an error the caller must see rather than a
    quietly empty library."""
    code, payload = result
    if code != 200:
        raise RuntimeError("arr returned HTTP %s" % code)
    if not isinstance(payload, list):
        raise RuntimeError("arr returned %s, expected a list" % type(payload).__name__)
    return payload


def peek(slug: str, client=None) -> dict:
    kind = "series" if slug in SERIES_SLUGS else "movie"
    out = {"slug": slug, "kind": kind, "titles": [], "ok": True, "error": ""}
    try:
        c = client if client is not None else _default_client(slug)
        if kind == "series":
            for s in _rows(c.get("/series")):
                st = s.get("statistics") or {}
                have = int(st.get("episodeFileCount") or 0)
                total = int(st.get("totalEpisodeCount") or 0)
                out["titles"].append({
                    "title": s.get("title", "?"), "have": have, "total": total,
                    "complete": total > 0 and have >= total})
        else:
            for m in _rows(c.get("/movie")):
                has = bool(m.get("hasFile"))
                out["titles"].append({
                    "title": m.get("title", "?"), "have": 1 if has else 0,
                    "total": 1, "complete": has})
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)
        out["titles"] = []
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit-json", action="store_true", required=True)
    ap.add_argument("--slug", required=True)
    args = ap.parse_args()
    # See Global Constraints: --emit-json always exits 0, failure detail
    # rides in the JSON body's ok/error fields (peek() never raises).
    json.dump(peek(args.slug), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
