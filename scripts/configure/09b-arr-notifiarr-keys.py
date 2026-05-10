#!/usr/bin/env python3
"""Idempotently ensure each *arr has a working Notifiarr Connect with the
correct aPIKey. Without this, *arr -> Notifiarr -> Discord is silent.

Discovered 2026-05-09: sonarr/radarr/readarr had Notifiarr Connect rows
with `aPIKey = ''` (UI redacts as ********, but DB Settings JSON had empty
string). sonarr2/radarr2 had no Notifiarr row at all.

Run on the seedbox; reads ~/secrets/{<arr>.{key,port,urlbase},notifiarr.key}.
"""
import json
import os
import sys
import urllib.error
import urllib.request


def secret(name: str) -> str:
    with open(os.path.expanduser(f"~/secrets/{name}")) as f:
        return f.read().strip()


ARRS = [
    {"name": "sonarr",   "api": "v3", "is_sonarr": True},
    {"name": "sonarr2",  "api": "v3", "is_sonarr": True},
    {"name": "radarr",   "api": "v3", "is_sonarr": False},
    {"name": "radarr2",  "api": "v3", "is_sonarr": False},
    {"name": "readarr",  "api": "v1", "is_sonarr": False},
]


def base_url(arr) -> str:
    port = secret(f"{arr['name']}.port")
    urlbase = secret(f"{arr['name']}.urlbase")
    return f"http://127.0.0.1:{port}/{urlbase}/api/{arr['api']}"


def http(arr, path, method="GET", body=None):
    url = f"{base_url(arr)}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "X-Api-Key": secret(f"{arr['name']}.key"),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        return urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        msg = e.read().decode()[:300]
        print(f"    HTTP {e.code} {method} {path}: {msg}")
        return e


def make_body(arr, notifiarr_key):
    body = {
        "name": "Notifiarr",
        "implementation": "Notifiarr",
        "implementationName": "Notifiarr",
        "configContract": "NotifiarrSettings",
        "tags": [],
        "fields": [{"name": "aPIKey", "value": notifiarr_key}],
        "onGrab": True,
        "onDownload": True,
        "onUpgrade": True,
        "onRename": False,
        "onHealthIssue": False,
        "includeHealthWarnings": False,
        "onApplicationUpdate": False,
        "supportsOnGrab": True,
        "supportsOnDownload": True,
        "supportsOnUpgrade": True,
        "supportsOnRename": False,
        "supportsOnHealthIssue": True,
        "supportsOnApplicationUpdate": False,
    }
    if arr["is_sonarr"]:
        body.update({
            "onEpisodeFileDelete": False,
            "onEpisodeFileDeleteForUpgrade": False,
            "onSeriesAdd": False,
            "onSeriesDelete": False,
            "supportsOnEpisodeFileDelete": True,
            "supportsOnSeriesAdd": True,
            "supportsOnSeriesDelete": True,
        })
    else:
        body.update({
            "onMovieAdded": False,
            "onMovieDelete": False,
            "onMovieFileDelete": False,
            "onMovieFileDeleteForUpgrade": False,
            "supportsOnMovieAdded": True,
            "supportsOnMovieDelete": True,
            "supportsOnMovieFileDelete": True,
        })
    return body


def fix_arr(arr, notifiarr_key):
    name = arr["name"]
    resp = http(arr, "/notification")
    if not hasattr(resp, "read"):
        return False
    notifications = json.loads(resp.read())
    existing = next(
        (n for n in notifications if n.get("implementation") == "Notifiarr"),
        None,
    )
    if existing is None:
        body = make_body(arr, notifiarr_key)
        r = http(arr, "/notification", method="POST", body=body)
        ok = hasattr(r, "read") and r.status in (200, 201)
        print(f"  create  {name}: rc={getattr(r,'status','err')}")
        return ok

    nid = existing["id"]
    apikey_field = next(
        (f for f in existing.get("fields", []) if f.get("name") == "aPIKey"),
        None,
    )
    if apikey_field is None:
        existing.setdefault("fields", []).append(
            {"name": "aPIKey", "value": notifiarr_key}
        )
    elif apikey_field.get("value") == notifiarr_key:
        print(f"  ok      {name}: aPIKey already correct (id={nid})")
        return True
    else:
        apikey_field["value"] = notifiarr_key

    r = http(arr, f"/notification/{nid}", method="PUT", body=existing)
    ok = hasattr(r, "read") and r.status in (200, 202)
    print(f"  patch   {name}: id={nid} rc={getattr(r,'status','err')}")
    return ok


def main() -> int:
    nk = secret("notifiarr.key")
    fails = 0
    for arr in ARRS:
        if not fix_arr(arr, nk):
            fails += 1
    print(f"\n[{'OK' if fails == 0 else 'FAIL'}] {len(ARRS)-fails}/{len(ARRS)} *arrs configured")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
