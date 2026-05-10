#!/usr/bin/env python3
"""Idempotently set Jellyseerr's *arr server records' activeDirectory /
activeProfileId / activeLanguageProfileId so requests can actually push to
the *arr's library. Without these, requests hang at media.status=3 forever.

Run from the seedbox (loopback) or via SSH tunnel; reads ~/secrets/*.

Routing:
  Radarr  (default movies)   -> /home/quadstronaut/media/Movies
  Radarr2 (anime movies)     -> /home/quadstronaut/media/Anime Movies
  Sonarr  (default TV)       -> /home/quadstronaut/media/TV Shows
  Sonarr2 (anime TV)         -> /home/quadstronaut/media/Anime
All language profiles -> English (id=1).
All quality profiles  -> id=1 (whatever the *arr's first profile is).
"""
import json
import os
import sys
import urllib.error
import urllib.request


def secret(name: str) -> str:
    with open(os.path.expanduser(f"~/secrets/{name}")) as f:
        return f.read().strip()


def js_get(path: str) -> object:
    req = urllib.request.Request(
        f"http://127.0.0.1:{secret('jellyseerr.port')}/api/v1{path}",
        headers={"X-Api-Key": secret("jellyseerr.key")},
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def js_put(path: str, body: dict) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{secret('jellyseerr.port')}/api/v1{path}",
        data=json.dumps(body).encode(),
        headers={
            "X-Api-Key": secret("jellyseerr.key"),
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    try:
        return urllib.request.urlopen(req, timeout=15).getcode()
    except urllib.error.HTTPError as e:
        print(f"    HTTP {e.code}: {e.read().decode()[:400]}")
        return e.code


TARGETS = {
    # name -> (kind, root_dir, quality_profile_id)
    # 1080p caps per "no 4K" policy:
    # Radarr  id=7 "HD Bluray + WEB"      (Recyclarr 1080p movies)
    # Radarr2 id=7 "HD Bluray + WEB"      (avoid id=5 Ultra-HD)
    # Sonarr  id=7 "WEB-1080p"            (Recyclarr 1080p TV)
    # Sonarr2 id=7 "[Anime] Remux-1080p"  (anime)
    "Radarr":  ("radarr",  "/home/quadstronaut/media/Movies",       7),
    "Radarr2": ("radarr2", "/home/quadstronaut/media/Anime Movies", 7),
    "Sonarr":  ("sonarr",  "/home/quadstronaut/media/TV Shows",     7),
    "Sonarr2": ("sonarr2", "/home/quadstronaut/media/Anime",        7),
}


def patch_kind(kind: str) -> int:
    fixes = 0
    for srv in js_get(f"/settings/{kind}"):
        name = srv.get("name")
        if name not in TARGETS:
            print(f"  skip {kind}/{name} (not in routing table)")
            continue
        _, want_dir, want_profile = TARGETS[name]
        changed = False
        if srv.get("activeDirectory") != want_dir:
            srv["activeDirectory"] = want_dir
            changed = True
        if srv.get("activeProfileId") != want_profile:
            srv["activeProfileId"] = want_profile
            changed = True
        if kind == "sonarr":
            if srv.get("activeLanguageProfileId") != 1:
                srv["activeLanguageProfileId"] = 1
                changed = True
            if name == "Sonarr2":
                if srv.get("activeAnimeProfileId") != want_profile:
                    srv["activeAnimeProfileId"] = want_profile
                    changed = True
                if srv.get("activeAnimeDirectory") != want_dir:
                    srv["activeAnimeDirectory"] = want_dir
                    changed = True
                if srv.get("activeAnimeLanguageProfileId") != 1:
                    srv["activeAnimeLanguageProfileId"] = 1
                    changed = True
        if not changed:
            print(f"  ok   {kind}/{name} (already configured)")
            continue
        sid = srv.pop("id")
        if kind == "radarr" and not srv.get("minimumAvailability"):
            srv["minimumAvailability"] = "released"
        if kind == "sonarr" and "enableSeasonFolders" not in srv:
            srv["enableSeasonFolders"] = True
        rc = js_put(f"/settings/{kind}/{sid}", srv)
        print(f"  fix  {kind}/{name} -> dir={want_dir!r} rc={rc}")
        fixes += 1
    return fixes


def main() -> int:
    total = patch_kind("radarr") + patch_kind("sonarr")
    print(f"\n[OK] {total} server record(s) patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
