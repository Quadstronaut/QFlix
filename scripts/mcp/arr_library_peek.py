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

DEVIATION from brief (verified against scripts/mcp/lib/arr_client.py and
every existing caller — missing.py, unstick.py, quality_fallback.py,
collect.py):
  1. ArrClient's constructor is `ArrClient(slug, version, ...)` — `version`
     is a required positional arg. The brief's `ArrClient(slug)` would raise
     TypeError in production. Every sibling script passes "v3" explicitly.
  2. ArrClient.get(path) returns an (http_code, payload) TUPLE of the
     RECORDS, and `path` is relative to the version root ArrClient already
     builds into the URL (e.g. missing.py never calls "/api/v3/command", it
     calls "/command"; quality_fallback.py calls "/series", not
     "/api/v3/series"). Calling c.get("/api/v3/series") against the real
     client would double the "/api/v3" segment in the URL (404) even before
     accounting for the tuple. The brief's own test fakes (correctly, per
     the brief's OWN stated interface) return a bare list from a path that
     DOES carry "/api/v3" — i.e. the brief's fakes model a client that does
     not exist on this box.
  A thin adapter in _default_client() below reconciles this: it builds the
  real ArrClient with the version, strips the "/api/v3" prefix peek() sends
  (matching the fixed test contract) before delegating, unwraps the
  (code, payload) tuple, and raises on a non-200 so peek()'s existing
  except-and-degrade path handles it the same way it handles a fake's raised
  RuntimeError. client=<fake> (as every test here supplies) bypasses the
  adapter entirely and is used exactly as the brief specified.
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

_ARR_VERSION = "v3"
_API_PREFIX = "/api/" + _ARR_VERSION


class _ArrClientAdapter:
    """Presents the bare get(path) -> records interface peek() (and its
    tests) use, backed by the real ArrClient — see the module docstring's
    DEVIATION note for why this exists rather than calling ArrClient
    directly."""

    def __init__(self, slug: str):
        from lib.arr_client import ArrClient
        self._slug = slug
        self._client = ArrClient(slug, _ARR_VERSION)

    def get(self, path: str, **kw):
        rel = path[len(_API_PREFIX):] if path.startswith(_API_PREFIX) else path
        code, payload = self._client.get(rel, **kw)
        if code != 200:
            raise RuntimeError(
                "%s: HTTP %s from %s" % (self._slug, code, path))
        return payload


def _default_client(slug: str):
    return _ArrClientAdapter(slug)


def peek(slug: str, client=None) -> dict:
    kind = "series" if slug in SERIES_SLUGS else "movie"
    out = {"slug": slug, "kind": kind, "titles": [], "ok": True, "error": ""}
    try:
        c = client if client is not None else _default_client(slug)
        if kind == "series":
            for s in c.get("/api/v3/series"):
                st = s.get("statistics") or {}
                have = int(st.get("episodeFileCount") or 0)
                total = int(st.get("totalEpisodeCount") or 0)
                out["titles"].append({
                    "title": s.get("title", "?"), "have": have, "total": total,
                    "complete": total > 0 and have >= total})
        else:
            for m in c.get("/api/v3/movie"):
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
