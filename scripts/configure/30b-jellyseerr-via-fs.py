#!/usr/bin/env python3
"""Phase 10 (alt): Configure Jellyseerr by patching settings.json directly + restarting.

Jellyseerr's REST API treats key fields (Plex.name, libraries) as read-only.
The on-disk settings.json is the source of truth — patching it before restart
is the cleanest path for headless config.

Run on manitoba; reads PLEX_HOST/PORT/TOKEN, JF_KEY/PORT, *arr secrets from env.
"""
import json, os, sys

SETTINGS_PATH = os.environ.get("JS_SETTINGS", "/home/quadstronaut/.apps/jellyseerr/settings.json")

with open(SETTINGS_PATH) as f:
    s = json.load(f)

# Plex
s.setdefault("plex", {}).update({
    "name": "manitoba",
    "ip": os.environ["PLEX_HOST"],
    "port": int(os.environ["PLEX_PORT"]),
    "useSsl": False,
})

# Jellyfin (Jellyseerr stores it as a single object, not array)
s.setdefault("jellyfin", {}).update({
    "name": "manitoba-jellyfin",
    "ip": "172.17.0.1",
    "port": int(os.environ["JF_PORT"]),
    "useSsl": False,
    "urlBase": "/jellyfin",
    "apiKey": os.environ["JF_KEY"],
    "libraries": s.get("jellyfin", {}).get("libraries", []),
    "serverId": s.get("jellyfin", {}).get("serverId", ""),
})

# Sonarr/Radarr arrays
def make_arr(name, port, base, key, root, is_anime):
    return {
        "name": name,
        "hostname": "172.17.0.1",
        "port": int(port),
        "useSsl": False,
        "apiKey": key,
        "baseUrl": f"/{base}",
        "activeProfileId": 1,
        "activeProfileName": "Any",
        "activeRootFolder": root,
        "activeAnimeProfileId": None,
        "activeAnimeRootFolder": None,
        "activeLanguageProfileId": None,
        "activeAnimeLanguageProfileId": None,
        "is4k": False,
        "isDefault": not is_anime,  # Sonarr+Radarr default for non-anime; Sonarr2/Radarr2 are non-default
        "syncEnabled": True,
        "preventSearch": False,
        "tagRequests": False,
    }

# Replace sonarr/radarr arrays entirely (idempotent — re-running gives same result)
sonarrs = [
    make_arr("Sonarr",  os.environ["SONARR_PORT"],  os.environ.get("SONARR_BASE", "sonarr"),  os.environ["SONARR_KEY"],  "/home/quadstronaut/media/TV Shows", False),
    make_arr("Sonarr2", os.environ["SONARR2_PORT"], os.environ.get("SONARR2_BASE", "sonarr2"), os.environ["SONARR2_KEY"], "/home/quadstronaut/media/Anime",    True),
]
radarrs = [
    make_arr("Radarr",  os.environ["RADARR_PORT"],  os.environ.get("RADARR_BASE", "radarr"),  os.environ["RADARR_KEY"],  "/home/quadstronaut/media/Movies",       False),
    make_arr("Radarr2", os.environ["RADARR2_PORT"], os.environ.get("RADARR2_BASE", "radarr2"), os.environ["RADARR2_KEY"], "/home/quadstronaut/media/Anime Movies", True),
]
# Preserve existing IDs if present (so Jellyseerr doesn't duplicate)
prev_sonarrs = {a.get("name"): a.get("id") for a in s.get("sonarr", [])}
prev_radarrs = {a.get("name"): a.get("id") for a in s.get("radarr", [])}
for i, a in enumerate(sonarrs, start=1):
    a["id"] = prev_sonarrs.get(a["name"], i)
for i, a in enumerate(radarrs, start=1):
    a["id"] = prev_radarrs.get(a["name"], i)

s["sonarr"] = sonarrs
s["radarr"] = radarrs

# Persist
with open(SETTINGS_PATH, "w") as f:
    json.dump(s, f, indent=4)
print(f"Patched {SETTINGS_PATH}")
print(f"  plex.ip={s['plex']['ip']}:{s['plex']['port']}")
print(f"  jellyfin.ip={s['jellyfin']['ip']}:{s['jellyfin']['port']} urlBase={s['jellyfin']['urlBase']}")
print(f"  sonarrs: {[a['name'] for a in s['sonarr']]}")
print(f"  radarrs: {[a['name'] for a in s['radarr']]}")
