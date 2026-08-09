#!/usr/bin/env python3
"""Configure Sonarr2 + Radarr2: root folders + qBittorrent download client.

Run on manitoba; reads ENV: PROW_KEY isn't needed here, just per-arr KEY/PORT/BASE/QBIT_*.
Uses the public-HTTPS qBit pattern that existing Sonarr/Radarr use (Ultra.cc convention).
Idempotent: skip if root folder or download client already present.
"""
import json, os, sys, urllib.request, urllib.error

QBIT_USER = os.environ["QBIT_USER"]
QBIT_PASS = os.environ["QBIT_PASS"]
SLOT_HOST = os.environ.get("SLOT_HOST", "quadstronaut.seedbox.example.com")

def req(url, method="GET", api_key=None, body=None, timeout=30):
    h = {"Content-Type": "application/json"}
    if api_key: h["X-Api-Key"] = api_key
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

def configure_arr(label, port, base, key, root, qbit_cat, kind):
    """kind: 'tv' for Sonarr (tvCategory), 'movie' for Radarr (movieCategory)."""
    api = f"http://127.0.0.1:{port}/{base}/api/v3"
    print(f"=== {label} ({api})")

    # 1. Root folder
    code, roots = req(f"{api}/rootfolder", api_key=key)
    if not any(r.get("path") == root for r in (roots or [])):
        code, resp = req(f"{api}/rootfolder", method="POST", api_key=key, body={"path": root})
        if code in (200, 201): print(f"  + root folder: {root}")
        else: print(f"  ! root folder failed: HTTP {code} {str(resp)[:120]}")
    else:
        print(f"  = root folder already present: {root}")

    # 2. qBit download client (mimic existing Sonarr/Radarr's public-HTTPS pattern)
    code, dcs = req(f"{api}/downloadclient", api_key=key)
    if any(d.get("name") == "qBittorrent" for d in (dcs or [])):
        print(f"  = qBittorrent download client already present")
        return

    cat_field = {"tv": "tvCategory", "movie": "movieCategory"}[kind]
    body = {
        "enable": True,
        "protocol": "torrent",
        "priority": 1,
        "name": "qBittorrent",
        "implementation": "QBittorrent",
        "implementationName": "qBittorrent",
        "configContract": "QBittorrentSettings",
        "removeCompletedDownloads": True,
        "removeFailedDownloads": True,
        "fields": [
            {"name": "host", "value": SLOT_HOST},
            {"name": "port", "value": 443},
            {"name": "useSsl", "value": True},
            {"name": "urlBase", "value": "/qbittorrent"},
            {"name": "username", "value": QBIT_USER},
            {"name": "password", "value": QBIT_PASS},
            {"name": cat_field, "value": qbit_cat},
            {"name": "recentTvPriority" if kind == "tv" else "recentMoviePriority", "value": 0},
            {"name": "olderTvPriority"  if kind == "tv" else "olderMoviePriority",  "value": 0},
            {"name": "initialState", "value": 0},
            {"name": "sequentialOrder", "value": False},
            {"name": "firstAndLast", "value": False},
        ],
    }
    code, resp = req(f"{api}/downloadclient?forceSave=true", method="POST", api_key=key, body=body)
    if code in (200, 201):
        print(f"  + qBittorrent download client (cat={qbit_cat})")
    else:
        msg = ""
        if isinstance(resp, list) and resp:
            msg = resp[0].get("errorMessage", str(resp[0]))[:120]
        else:
            msg = str(resp)[:120]
        print(f"  ! qBittorrent client failed: HTTP {code} {msg}")

# Sonarr2 → Anime / sonarr-anime
configure_arr(
    "Sonarr2",
    os.environ["SONARR2_PORT"],
    os.environ.get("SONARR2_BASE", "sonarr2"),
    os.environ["SONARR2_KEY"],
    "/home/quadstronaut/media/Anime",
    "sonarr-anime",
    "tv",
)

# Radarr2 → Anime Movies / radarr-anime
configure_arr(
    "Radarr2",
    os.environ["RADARR2_PORT"],
    os.environ.get("RADARR2_BASE", "radarr2"),
    os.environ["RADARR2_KEY"],
    "/home/quadstronaut/media/Anime Movies",
    "radarr-anime",
    "movie",
)
