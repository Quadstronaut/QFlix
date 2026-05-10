#!/usr/bin/env python3
"""Phase 7.x: Add Jellyfin libraries (Movies/TV/Anime/Anime Movies) + Jellyfin Connect on 4 *arrs.

Run on manitoba; reads JF_KEY, JF_PORT, plus *arr secrets from env.
Idempotent.
"""
import json, os, socket, sys, time, urllib.request, urllib.error

JF_KEY = os.environ["JF_KEY"]
JF_PORT = os.environ["JF_PORT"]
JF_URL = f"http://127.0.0.1:{JF_PORT}/jellyfin"

def req(url, method="GET", headers=None, body=None, timeout=60):
    h = {"Content-Type": "application/json"}
    if headers: h.update(headers)
    d = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=d, method=method, headers=h)
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
    if isinstance(resp, list) and resp:
        return resp[0].get("errorMessage", str(resp[0]))[:120]
    if isinstance(resp, dict): return resp.get("Message", str(resp))[:120]
    return str(resp)[:120]

# ===== 1. Add Jellyfin libraries =====
JF_HDR = {"X-Emby-Token": JF_KEY}

print("=== 1. Jellyfin libraries ===")
code, vf = req(f"{JF_URL}/Library/VirtualFolders", headers=JF_HDR)
existing = {v.get("Name", "") for v in (vf or [])}
print(f"  existing libraries: {sorted(existing)}")

LIBS = [
    ("Movies", "movies", ["/home/quadstronaut/media/Movies"]),
    ("TV Shows", "tvshows", ["/home/quadstronaut/media/TV Shows"]),
    ("Anime", "tvshows", ["/home/quadstronaut/media/Anime"]),
    ("Anime Movies", "movies", ["/home/quadstronaut/media/Anime Movies"]),
]
for name, ctype, paths in LIBS:
    if name in existing:
        print(f"  = {name} already present")
        continue
    # POST /Library/VirtualFolders?name=...&collectionType=...&paths[]=...
    paths_q = "&".join(f"paths={urllib.parse.quote(p)}" for p in paths)
    qs = f"?name={urllib.parse.quote(name)}&collectionType={ctype}&{paths_q}&refreshLibrary=true"
    code, resp = req(f"{JF_URL}/Library/VirtualFolders{qs}", method="POST", headers=JF_HDR)
    print(f"  + {name} ({ctype}, {paths}) → HTTP {code}")
    time.sleep(2)

# ===== 2. Jellyfin Connect on each *arr =====
import urllib.parse  # noqa  (used above)

def configure_jellyfin_connect(label, port, base, key):
    api = f"http://127.0.0.1:{port}/{base}/api/v3"
    print(f"\n=== 2. {label} ({api}) — Jellyfin Connect ===")

    code, notifs = req(f"{api}/notification", headers={"X-Api-Key": key})
    have = next((n for n in (notifs or []) if n.get("name") == "Jellyfin"), None)

    code, schemas = req(f"{api}/notification/schema", headers={"X-Api-Key": key})
    schema = next((s for s in (schemas or []) if s.get("implementation") == "MediaBrowser"), None)
    if not schema:
        print("  ! MediaBrowser (Emby/Jellyfin) schema not found")
        return

    body = json.loads(json.dumps(schema))
    body["name"] = "Jellyfin"
    for k, v in {"onDownload": True, "onUpgrade": True, "onRename": True}.items():
        if k in body: body[k] = v
    # Field overrides (case-insensitive name match)
    overrides = {
        "host": "172.17.0.1",      # docker0 gateway (Jellyfin's bind-address from arr container)
        "port": int(JF_PORT),
        "useSsl": False,
        "urlBase": "/jellyfin",
        "apiKey": JF_KEY,
        "updateLibrary": True,
    }
    for f in body.get("fields", []):
        for k, v in overrides.items():
            if f["name"].lower() == k.lower():
                f["value"] = v
                break
    body["tags"] = []

    if have:
        # update fields if stale
        body["id"] = have["id"]
        code, _ = req(f"{api}/notification/{have['id']}?forceSave=true", method="PUT", headers={"X-Api-Key": key}, body=body)
        print(f"  ~ Jellyfin Connect updated (HTTP {code})")
    else:
        code, resp = req(f"{api}/notification?forceSave=true", method="POST", headers={"X-Api-Key": key}, body=body)
        if code in (200, 201):
            print(f"  + Jellyfin Connect added (id={resp.get('id')})")
        else:
            print(f"  ! Jellyfin Connect failed: HTTP {code} {first_err(resp)}")

    # Test
    code, notifs = req(f"{api}/notification", headers={"X-Api-Key": key})
    jf = next((n for n in notifs if n.get("name") == "Jellyfin"), None)
    if jf:
        test_code, test_body = req(f"{api}/notification/test", method="POST", headers={"X-Api-Key": key}, body=jf)
        if test_code in (200, 202):
            print(f"  ✓ Jellyfin test OK")
        else:
            print(f"  ✗ Jellyfin test failed: HTTP {test_code} {first_err(test_body)}")

for app in ("sonarr", "radarr", "sonarr2", "radarr2"):
    configure_jellyfin_connect(
        app.title(),
        os.environ[f"{app.upper()}_PORT"],
        os.environ.get(f"{app.upper()}_BASE", app),
        os.environ[f"{app.upper()}_KEY"],
    )
