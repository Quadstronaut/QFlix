#!/usr/bin/env python3
"""Phase 10: Configure Jellyseerr — Plex, Jellyfin, 4 *arrs with anime routing.

Run on manitoba; reads JS_KEY, JS_PORT, PLEX_*, JF_*, *arr secrets, NOTIFIARR_KEY from env.

Idempotent. Per the spec:
- Anime TV (default profile + tag detected by Jellyseerr): routed to Sonarr2 (root /home/quadstronaut/media/Anime)
- Non-anime TV: routed to Sonarr (root /home/quadstronaut/media/TV Shows)
- Anime movies: Radarr2 (root /home/quadstronaut/media/Anime Movies)
- Non-anime movies: Radarr (root /home/quadstronaut/media/Movies)
"""
import json, os, socket, sys, urllib.request, urllib.error

JS_KEY = os.environ["JS_KEY"]
JS_PORT = os.environ["JS_PORT"]
URL = f"http://127.0.0.1:{JS_PORT}/api/v1"

def req(path, method="GET", body=None, timeout=60):
    h = {"Content-Type": "application/json", "X-Api-Key": JS_KEY}
    d = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(URL + path, data=d, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            t = resp.read().decode()
            return resp.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        b = e.read().decode()
        try: parsed = json.loads(b)
        except Exception: parsed = b
        return e.code, parsed
    except (socket.timeout, urllib.error.URLError) as e:
        return 0, str(e)

def first_err(resp):
    if isinstance(resp, dict): return str(resp)[:120]
    return str(resp)[:120]

# 1. Plex
print("=== 1. Plex")
plex_body = {
    "name": "manitoba",
    "machineId": None,
    "ip": os.environ["PLEX_HOST"],
    "port": int(os.environ["PLEX_PORT"]),
    "useSsl": False,
    "libraries": [],
    "webAppUrl": "",
}
# POST /api/v1/settings/plex
code, resp = req("/settings/plex", method="POST", body=plex_body)
print(f"  POST /settings/plex → HTTP {code} {'OK' if code in (200,201,204) else first_err(resp)}")

# Sync libraries (Jellyseerr fetches them from Plex)
code, libs = req("/settings/plex/library?sync=true", method="GET", timeout=60)
print(f"  GET /settings/plex/library?sync=true → HTTP {code} (libraries={len(libs) if isinstance(libs, list) else '?'})")

# 2. Jellyfin
print("\n=== 2. Jellyfin")
jf_body = {
    "name": "manitoba-jellyfin",
    "ip": "172.17.0.1",
    "port": int(os.environ["JF_PORT"]),
    "useSsl": False,
    "urlBase": "/jellyfin",
    "apiKey": os.environ["JF_KEY"],
    "libraries": [],
}
code, resp = req("/settings/jellyfin", method="POST", body=jf_body)
print(f"  POST /settings/jellyfin → HTTP {code}")

# 3. Sonarr (general TV) + Sonarr2 (anime TV)
def add_arr(kind, name, port, base, key, root, is_anime):
    """kind: 'sonarr' or 'radarr'."""
    body = {
        "name": name,
        "hostname": "172.17.0.1",
        "port": int(port),
        "useSsl": False,
        "apiKey": key,
        "baseUrl": f"/{base}",
        "activeAnimeProfileId": None,
        "activeAnimeRootFolder": None,
        "activeProfileId": 1,
        "activeProfileName": "Any",
        "activeRootFolder": root,
        "activeLanguageProfileId": None,
        "activeAnimeLanguageProfileId": None,
        "is4k": False,
        "isDefault": not is_anime,
        "syncEnabled": True,
        "preventSearch": False,
        "tagRequests": False,
    }
    code, existing = req(f"/settings/{kind}")
    found = next((x for x in (existing or []) if x.get("name") == name), None)
    if found:
        body["id"] = found["id"]
        code, resp = req(f"/settings/{kind}/{found['id']}", method="PUT", body=body)
        print(f"  ~ {name} updated (HTTP {code})")
    else:
        code, resp = req(f"/settings/{kind}", method="POST", body=body)
        print(f"  + {name} added (HTTP {code})")
    if code not in (200, 201, 204): print(f"    err: {first_err(resp)}")

print("\n=== 3. Sonarr / Sonarr2")
add_arr("sonarr", "Sonarr",  os.environ["SONARR_PORT"],  os.environ.get("SONARR_BASE", "sonarr"),  os.environ["SONARR_KEY"],  "/home/quadstronaut/media/TV Shows",   is_anime=False)
add_arr("sonarr", "Sonarr2", os.environ["SONARR2_PORT"], os.environ.get("SONARR2_BASE", "sonarr2"), os.environ["SONARR2_KEY"], "/home/quadstronaut/media/Anime",      is_anime=True)

print("\n=== 4. Radarr / Radarr2")
add_arr("radarr", "Radarr",  os.environ["RADARR_PORT"],  os.environ.get("RADARR_BASE", "radarr"),  os.environ["RADARR_KEY"],  "/home/quadstronaut/media/Movies",         is_anime=False)
add_arr("radarr", "Radarr2", os.environ["RADARR2_PORT"], os.environ.get("RADARR2_BASE", "radarr2"), os.environ["RADARR2_KEY"], "/home/quadstronaut/media/Anime Movies",   is_anime=True)

# 5. Test
print("\n=== 5. Tests")
code, status = req("/status")
print(f"  Jellyseerr status: HTTP {code}, version={status.get('version') if isinstance(status, dict) else '?'}")
code, sonarrs = req("/settings/sonarr")
code, radarrs = req("/settings/radarr")
print(f"  configured: {len(sonarrs) if isinstance(sonarrs, list) else '?'} sonarr instances, {len(radarrs) if isinstance(radarrs, list) else '?'} radarr instances")
