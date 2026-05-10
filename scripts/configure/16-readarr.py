#!/usr/bin/env python3
"""Configure Readarr: root folders + qBittorrent + Notifiarr Connect.
Run on manitoba; reads READARR_*, QBIT_*, NOTIFIARR_KEY, SLOT_HOST from env.
Idempotent.

Note: Readarr's API is v1 (not v3 like Sonarr/Radarr), and its qBit field is
`musicCategory` for some reason (its book-handling reuses Lidarr's contract).
"""
import json, os, socket, sys, urllib.request, urllib.error

KEY = os.environ["READARR_KEY"]
PORT = os.environ["READARR_PORT"]
BASE = os.environ.get("READARR_BASE", "readarr")
URL = f"http://127.0.0.1:{PORT}/{BASE}/api/v1"
QBIT_USER = os.environ["QBIT_USER"]
QBIT_PASS = os.environ["QBIT_PASS"]
SLOT_HOST = os.environ.get("SLOT_HOST", "quadstronaut.seedbox.example.com")
NOTIFIARR_KEY = os.environ["NOTIFIARR_KEY"]

def req(path, method="GET", body=None, timeout=60):
    h = {"Content-Type": "application/json", "X-Api-Key": KEY}
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
    if isinstance(resp, list) and resp:
        return resp[0].get("errorMessage", str(resp[0]))[:120]
    if isinstance(resp, dict): return str(resp)[:120]
    return str(resp)[:120]

# 1. Root folders. Readarr requires metadataProfileId on rootfolder.
print("=== 1. Root folders")
code, mps = req("/metadataprofile")
default_mp = (mps or [{"id": 1}])[0]["id"]
print(f"  metadata profile id = {default_mp}")

code, qps = req("/qualityprofile")
default_qp = (qps or [{"id": 1}])[0]["id"]
print(f"  quality profile id = {default_qp}")

for path in ("/home/quadstronaut/media/Books", "/home/quadstronaut/media/Audiobooks"):
    code, roots = req("/rootfolder")
    if any(r.get("path") == path for r in (roots or [])):
        print(f"  = {path} already present")
        continue
    body = {
        "name": os.path.basename(path),
        "path": path,
        "defaultMetadataProfileId": default_mp,
        "defaultQualityProfileId": default_qp,
        "isCalibreLibrary": False,
        "outputProfile": "default",
    }
    code, resp = req("/rootfolder?forceSave=true", method="POST", body=body)
    if code in (200, 201):
        print(f"  + root added: {path}")
    else:
        print(f"  ! root failed: {path} HTTP {code} {first_err(resp)}")

# 2. qBittorrent download client (matching existing Sonarr/Radarr public-HTTPS pattern)
print("\n=== 2. qBittorrent download client")
code, dcs = req("/downloadclient")
if any(d.get("name") == "qBittorrent" for d in (dcs or [])):
    print("  = qBittorrent already present")
else:
    code, schemas = req("/downloadclient/schema")
    qb_schema = next((s for s in schemas if s.get("implementation") == "QBittorrent"), None)
    if not qb_schema:
        print("  ! QBittorrent schema not found")
    else:
        body = json.loads(json.dumps(qb_schema))
        body["name"] = "qBittorrent"
        body["enable"] = True
        body["protocol"] = "torrent"
        body["priority"] = 1
        body["removeCompletedDownloads"] = True
        body["removeFailedDownloads"] = True
        body["tags"] = []
        overrides = {
            "host": SLOT_HOST, "port": 443, "useSsl": True, "urlBase": "/qbittorrent",
            "username": QBIT_USER, "password": QBIT_PASS,
            "musicCategory": "readarr", "category": "readarr",  # try both names
            "bookCategory": "readarr",
        }
        for f in body.get("fields", []):
            for k, v in overrides.items():
                if f["name"].lower() == k.lower():
                    f["value"] = v
                    break
        code, resp = req("/downloadclient?forceSave=true", method="POST", body=body)
        if code in (200, 201):
            print(f"  + qBittorrent added (id={resp.get('id')})")
        else:
            print(f"  ! qBittorrent failed: HTTP {code} {first_err(resp)}")

# 3. Notifiarr Connect
print("\n=== 3. Notifiarr Connect")
code, notifs = req("/notification")
if any(n.get("implementation") == "Notifiarr" for n in (notifs or [])):
    print("  = Notifiarr already present")
else:
    code, schemas = req("/notification/schema")
    notif_schema = next((s for s in schemas if s.get("implementation") == "Notifiarr"), None)
    if not notif_schema:
        print("  ! Notifiarr schema not found")
    else:
        body = json.loads(json.dumps(notif_schema))
        body["name"] = "Notifiarr"
        body["onGrab"] = True
        body["onReleaseImport"] = True
        body["onUpgrade"] = True
        body["onHealthIssue"] = True
        body["tags"] = []
        for f in body.get("fields", []):
            if f["name"].lower() in ("apikey", "apikey"):
                f["value"] = NOTIFIARR_KEY
        code, resp = req("/notification?forceSave=true", method="POST", body=body)
        if code in (200, 201):
            print(f"  + Notifiarr added (id={resp.get('id')})")
        else:
            print(f"  ! Notifiarr failed: HTTP {code} {first_err(resp)}")

# 4. Test
print("\n=== 4. Tests")
code, dcs = req("/downloadclient")
qb = next((d for d in dcs if d.get("name") == "qBittorrent"), None)
if qb:
    code, body = req("/downloadclient/test", method="POST", body=qb)
    print(f"  qBittorrent test: HTTP {code} {'OK' if code in (200,202) else first_err(body)}")

code, notifs = req("/notification")
nt = next((n for n in notifs if n.get("implementation") == "Notifiarr"), None)
if nt:
    code, body = req("/notification/test", method="POST", body=nt)
    print(f"  Notifiarr test: HTTP {code} {'OK' if code in (200,202) else first_err(body)}")
