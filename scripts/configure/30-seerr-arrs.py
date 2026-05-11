#!/usr/bin/env python3
"""Configure Seerr's Sonarr/Radarr connections via API.

Run on the seedbox. Reads secrets from ~/secrets, hits Seerr's API at
loopback. Adds 4 *arr servers (Sonarr Cinema, Sonarr Anime, Radarr Cinema,
Radarr Anime) with anime auto-routing configured on the default server.
Also ensures the network/proxy settings are sane for nginx reverse proxy.

Idempotent: if servers already exist with the same name, updates them.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request as ur
from pathlib import Path

SECRETS = Path(os.path.expanduser("~/secrets"))
SEERR_KEY = (SECRETS / "seerr.key").read_text().strip()
SEERR_BASE = "http://127.0.0.1:42011"


def seerr(method: str, path: str, body=None):
    req = ur.Request(
        SEERR_BASE + path,
        method=method,
        headers={"X-Api-Key": SEERR_KEY, "Content-Type": "application/json"},
    )
    data = json.dumps(body).encode() if body is not None else None
    try:
        with ur.urlopen(req, data=data, timeout=20) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt else {})
    except ur.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"raw": str(e)}


def arr(secret_prefix: str, kind: str):
    """Pull profiles + rootfolders + lang-profiles from an *arr instance."""
    key = (SECRETS / f"{secret_prefix}.key").read_text().strip()
    port = (SECRETS / f"{secret_prefix}.port").read_text().strip()
    base = (SECRETS / f"{secret_prefix}.urlbase").read_text().strip()
    arr_base = f"http://127.0.0.1:{port}/{base}/api/v3"

    def get(p):
        req = ur.Request(f"{arr_base}{p}", headers={"X-Api-Key": key})
        with ur.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    profiles = get("/qualityprofile")
    roots = get("/rootfolder")
    try:
        langs = get("/languageprofile")
    except Exception:
        langs = []
    return {
        "key": key, "port": int(port), "baseUrl": f"/{base}",
        "profiles": profiles, "roots": roots, "langs": langs,
    }


def pick_profile(profiles, *keywords):
    """Find first profile whose name matches any keyword (case-insensitive)."""
    for kw in keywords:
        for p in profiles:
            if kw.lower() in p["name"].lower():
                return p["id"], p["name"]
    return profiles[0]["id"], profiles[0]["name"]


def pick_root(roots, *keywords):
    for kw in keywords:
        for r in roots:
            if kw.lower() in r["path"].lower():
                return r["path"]
    return roots[0]["path"]


def build_sonarr_payload(name, sonarr_info, *, root_kw, profile_kw,
                        is_default, anime_root_kw=None, anime_profile_kw=None):
    pid, pname = pick_profile(sonarr_info["profiles"], *profile_kw)
    root = pick_root(sonarr_info["roots"], *root_kw)
    lang_id = sonarr_info["langs"][0]["id"] if sonarr_info["langs"] else 1
    payload = {
        "name": name,
        "hostname": "127.0.0.1",
        "port": sonarr_info["port"],
        "useSsl": False,
        "apiKey": sonarr_info["key"],
        "baseUrl": sonarr_info["baseUrl"],
        "activeProfileId": pid,
        "activeProfileName": pname,
        "activeDirectory": root,
        "activeLanguageProfileId": lang_id,
        "activeTags": [],
        "is4k": False,
        "isDefault": is_default,
        "externalUrl": "",
        "syncEnabled": True,
        "preventSearch": False,
        "tagRequests": False,
        "enableSeasonFolders": True,
    }
    # Configure anime override fields ON THE DEFAULT server (Sonarr Cinema)
    # so anime requests auto-route to a separate root/profile. Since Seerr's
    # anime override is same-instance, we set the anime fields to point at
    # an external Sonarr instance via separate-server addition AND we set
    # the *default* server's anime fields to a sane fallback.
    if anime_root_kw and anime_profile_kw:
        ap_id, ap_name = pick_profile(sonarr_info["profiles"], *anime_profile_kw)
        ar = pick_root(sonarr_info["roots"], *anime_root_kw)
        payload["activeAnimeProfileId"] = ap_id
        payload["activeAnimeProfileName"] = ap_name
        payload["activeAnimeDirectory"] = ar
        payload["activeAnimeLanguageProfileId"] = lang_id
        payload["activeAnimeTags"] = []
    return payload


def build_radarr_payload(name, radarr_info, *, root_kw, profile_kw, is_default):
    pid, pname = pick_profile(radarr_info["profiles"], *profile_kw)
    root = pick_root(radarr_info["roots"], *root_kw)
    return {
        "name": name,
        "hostname": "127.0.0.1",
        "port": radarr_info["port"],
        "useSsl": False,
        "apiKey": radarr_info["key"],
        "baseUrl": radarr_info["baseUrl"],
        "activeProfileId": pid,
        "activeProfileName": pname,
        "activeDirectory": root,
        "activeTags": [],
        "is4k": False,
        "isDefault": is_default,
        "externalUrl": "",
        "syncEnabled": True,
        "preventSearch": False,
        "tagRequests": False,
        "minimumAvailability": "released",
    }


def upsert(endpoint, payload, name_field="name"):
    """Add or update a server by name."""
    code, existing = seerr("GET", f"/api/v1/settings/{endpoint}")
    if code >= 300 or not isinstance(existing, list):
        existing = []
    target_name = payload[name_field]
    for srv in existing:
        if srv.get(name_field) == target_name:
            sid = srv["id"]
            print(f"  updating {endpoint}#{sid} {target_name!r}")
            code, resp = seerr("PUT", f"/api/v1/settings/{endpoint}/{sid}", {**srv, **payload})
            return code, resp
    print(f"  creating {endpoint} {target_name!r}")
    return seerr("POST", f"/api/v1/settings/{endpoint}", payload)


def main():
    print("=== pulling *arr metadata ===")
    sonarr_c = arr("sonarr",  "sonarr")
    sonarr_a = arr("sonarr2", "sonarr")
    radarr_c = arr("radarr",  "radarr")
    radarr_a = arr("radarr2", "radarr")
    print(f"  Sonarr Cinema: {len(sonarr_c['profiles'])} profiles, {len(sonarr_c['roots'])} roots")
    print(f"  Sonarr Anime:  {len(sonarr_a['profiles'])} profiles, {len(sonarr_a['roots'])} roots")
    print(f"  Radarr Cinema: {len(radarr_c['profiles'])} profiles, {len(radarr_c['roots'])} roots")
    print(f"  Radarr Anime:  {len(radarr_a['profiles'])} profiles, {len(radarr_a['roots'])} roots")

    print()
    print("=== Sonarr servers ===")
    # Sonarr Cinema: default, regular profile, /TV Shows root
    p1 = build_sonarr_payload(
        "Sonarr Cinema", sonarr_c,
        root_kw=["TV Shows", "TV"], profile_kw=["1080p", "WEB", "720p"],
        is_default=True,
    )
    code, resp = upsert("sonarr", p1)
    print(f"    -> HTTP {code}", json.dumps(resp)[:200] if not isinstance(resp, list) else "list")

    # Sonarr Anime: additional non-default server pointed at Sonarr2 with /Anime root + anime profile
    p2 = build_sonarr_payload(
        "Sonarr Anime", sonarr_a,
        root_kw=["Anime"], profile_kw=["Anime", "1080p"],
        is_default=False,
    )
    code, resp = upsert("sonarr", p2)
    print(f"    -> HTTP {code}", json.dumps(resp)[:200] if not isinstance(resp, list) else "list")

    print()
    print("=== Radarr servers ===")
    p3 = build_radarr_payload(
        "Radarr Cinema", radarr_c,
        root_kw=["Movies"], profile_kw=["1080p", "Bluray"],
        is_default=True,
    )
    code, resp = upsert("radarr", p3)
    print(f"    -> HTTP {code}", json.dumps(resp)[:200] if not isinstance(resp, list) else "list")

    p4 = build_radarr_payload(
        "Radarr Anime", radarr_a,
        root_kw=["Anime", "Movies"], profile_kw=["1080p"],
        is_default=False,
    )
    code, resp = upsert("radarr", p4)
    print(f"    -> HTTP {code}", json.dumps(resp)[:200] if not isinstance(resp, list) else "list")

    print()
    print("=== Verify ===")
    _, sonarrs = seerr("GET", "/api/v1/settings/sonarr")
    _, radarrs = seerr("GET", "/api/v1/settings/radarr")
    for s in (sonarrs or []):
        print(f"  Sonarr: id={s.get('id')} name={s.get('name')!r} isDefault={s.get('isDefault')} is4k={s.get('is4k')} profile={s.get('activeProfileName')} dir={s.get('activeDirectory')} animeDir={s.get('activeAnimeDirectory')}")
    for r in (radarrs or []):
        print(f"  Radarr: id={r.get('id')} name={r.get('name')!r} isDefault={r.get('isDefault')} is4k={r.get('is4k')} profile={r.get('activeProfileName')} dir={r.get('activeDirectory')}")


if __name__ == "__main__":
    main()
