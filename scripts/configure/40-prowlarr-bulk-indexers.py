#!/usr/bin/env python3
"""Disable dead indexers + bulk-add curated free English film/TV/anime indexers.

Idempotent. Excludes adult and warez per operator policy.

Run on seedbox: python3 40-prowlarr-bulk-indexers.py [--dry-run]
"""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request

KEY = open(os.path.expanduser("~/secrets/prowlarr.key")).read().strip()
PORT = open(os.path.expanduser("~/secrets/prowlarr.port")).read().strip()
BASE = "http://127.0.0.1:%s/prowlarr/api/v1" % PORT

# Tag IDs (verified live 2026-05-20)
TAG_ANIME = 1
TAG_CLOUDFLARE = 2
TAG_GENERAL = 3

# Indexers to disable (chronic site-down per WARN.md)
DISABLE = {"kickasstorrents.ws", "Torrent[CORE]"}

# Known Cloudflare-protected — auto-tag with cloudflare so FlareSolverr handles
# (live testing showed FlareSolverr can't crack 1337x/ExtraTorrent currently —
# left here for re-attempt once FlareSolverr health is fixed; see A2 followup)
CLOUDFLARE_NEEDED = {"1337x", "ExtraTorrent.st"}

# Curated allow-list — names match Prowlarr schema "name" field exactly.
# General film/TV (tag=general)
# Excluded after live test (operator can re-attempt later):
#   Demonoid Clone (SSL/dead), TorrentGalaxyClone (URL redirect issue),
#   1337x + ExtraTorrent.st (Cloudflare 403 — FlareSolverr can't crack currently),
#   Internet Archive (POST hangs >90s on add — too slow for sync workflow)
GENERAL = [
    "EZTV",
    "YTS",
    "TorrentProject2",
    "TorrentsCSV",
    "MagnetDownload",
    "BTdirectory",
    "0Magnet",
    "Postman",
    "TorrentDownload",
]
# LinuxTracker intentionally excluded — Linux ISOs only, no film/TV value

# Anime-primary (tag=anime)
ANIME = [
    "AniSource",
    "Anidex",
    "Nipponsei",
    "Shana Project",
    "Tokyo Toshokan",
]


def http(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"X-Api-Key": KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            txt = r.read().decode()
            return r.status, json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (socket.timeout, urllib.error.URLError) as e:
        return 0, "network: %s" % e


def get_indexers() -> list:
    code, data = http("GET", "/indexer")
    if code != 200:
        sys.exit("GET /indexer failed: %s %s" % (code, data))
    return data


def get_schema() -> list:
    code, data = http("GET", "/indexer/schema")
    if code != 200:
        sys.exit("GET /indexer/schema failed: %s %s" % (code, data))
    return data


def disable_indexer(idx: dict, dry: bool) -> bool:
    if not idx.get("enable", False):
        return False
    if dry:
        print("[dry] disable: %s (id=%s)" % (idx["name"], idx["id"]))
        return True
    idx["enable"] = False
    code, resp = http("PUT", "/indexer/%s?forceSave=true" % idx["id"], idx)
    if code in (200, 202):
        print("[disable] %s" % idx["name"])
        return True
    print("[fail] disable %s: %s %s" % (idx["name"], code, str(resp)[:200]), file=sys.stderr)
    return False


def add_indexer(schema_def: dict, tag_id: int, dry: bool) -> bool:
    payload = dict(schema_def)
    tags = [tag_id]
    if schema_def["name"] in CLOUDFLARE_NEEDED:
        tags.append(TAG_CLOUDFLARE)
    payload["tags"] = tags
    payload["enable"] = True
    payload["appProfileId"] = payload.get("appProfileId") or 1
    payload["priority"] = payload.get("priority") or 25
    payload.pop("id", None)
    if dry:
        print("[dry] add: %s (tag=%s)" % (schema_def["name"], tag_id))
        return True
    code, resp = http("POST", "/indexer?forceSave=true", payload)
    if code in (200, 201):
        print("[add] %s (tag=%s)" % (schema_def["name"], tag_id))
        return True
    print("[fail] add %s: %s %s" % (schema_def["name"], code, str(resp)[:300]), file=sys.stderr)
    return False


def main() -> int:
    dry = "--dry-run" in sys.argv

    existing = {i["name"]: i for i in get_indexers()}
    schema = {d["name"]: d for d in get_schema()}

    print("=== current: %d indexers ===" % len(existing))

    # Disable dead ones
    print("\n--- disable dead indexers ---")
    disabled = 0
    for name in DISABLE:
        if name in existing:
            if disable_indexer(existing[name], dry):
                disabled += 1
        else:
            print("[skip] %s not present" % name)

    # Add general
    print("\n--- add general film/TV indexers ---")
    added_g = 0
    for name in GENERAL:
        if name in existing:
            print("[skip] %s already configured" % name)
            continue
        if name not in schema:
            print("[miss] %s not in schema (catalog drift?)" % name, file=sys.stderr)
            continue
        if add_indexer(schema[name], TAG_GENERAL, dry):
            added_g += 1

    # Add anime
    print("\n--- add anime indexers ---")
    added_a = 0
    for name in ANIME:
        if name in existing:
            print("[skip] %s already configured" % name)
            continue
        if name not in schema:
            print("[miss] %s not in schema" % name, file=sys.stderr)
            continue
        if add_indexer(schema[name], TAG_ANIME, dry):
            added_a += 1

    print("\n=== summary ===")
    print("disabled: %d" % disabled)
    print("added general: %d" % added_g)
    print("added anime:   %d" % added_a)
    if not dry:
        post = get_indexers()
        print("final indexer count: %d (was %d)" % (len(post), len(existing)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
