#!/usr/bin/env python3
"""arr-audit-fixes — apply A/B/C from docs/arr-audit-actions-2026-05-09.md.

A) Register Readarr with Prowlarr + sync app to populate book indexers
B) Delete stale disabled download clients (rTorrent, Transmission)
C) Delete 3 underperforming Prowlarr indexers (Internet Archive, Magnet Cat, 1337x)

Read-only dry_run available via --dry-run.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRETS = REPO_ROOT / "secrets"
HOST = "https://quadstronaut.seedbox.example.com"
HTPW = (SECRETS / "htpasswd.password").read_text().strip()
PROW_KEY = (SECRETS / "prowlarr.key").read_text().strip()
READ_KEY = (SECRETS / "readarr.key").read_text().strip()


def _basic() -> str:
    return "Basic " + base64.b64encode(f"quadstronaut:{HTPW}".encode()).decode()


def _hdr(api_key: str, *, json_body: bool = False) -> dict:
    h = {"X-Api-Key": api_key, "Authorization": _basic(), "Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _req(method: str, url: str, api_key: str, body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers=_hdr(api_key, json_body=body is not None))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode(errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:600]


# ----- A: register Readarr with Prowlarr ---------------------------------

def fix_a_register_readarr(dry_run: bool) -> None:
    print("\n--- A. Register Readarr with Prowlarr ---")
    code, body = _req("GET", f"{HOST}/prowlarr/api/v1/applications", PROW_KEY)
    if code != 200:
        print(f"  ! GET /applications failed: HTTP {code}")
        return
    apps = json.loads(body)
    if any(a.get("implementation") == "Readarr" for a in apps):
        print("  ✓ Readarr app already registered, skipping")
        return

    payload = {
        "syncLevel": "fullSync",
        "enable": True,
        "name": "Readarr",
        "implementationName": "Readarr",
        "implementation": "Readarr",
        "configContract": "ReadarrSettings",
        "infoLink": "https://wiki.servarr.com/prowlarr/supported#readarr",
        "tags": [],
        "fields": [
            {"name": "prowlarrUrl", "value": "http://172.17.0.1:17024/prowlarr"},
            {"name": "baseUrl", "value": "http://172.17.0.1:17042/readarr"},
            {"name": "apiKey", "value": READ_KEY},
            {"name": "syncCategories",
             "value": [3030, 7000, 7010, 7020, 7030, 7040, 7050, 7060]},
        ],
    }

    print("  validating via POST /applications/test ...")
    code, body = _req("POST", f"{HOST}/prowlarr/api/v1/applications/test",
                      PROW_KEY, payload)
    if code != 200:
        print(f"  ! test failed HTTP {code}: {body[:200]}")
        return
    print(f"  ✓ test ok ({code})")

    if dry_run:
        print("  [dry-run] would POST /applications to register Readarr")
        return

    print("  POST /applications ...")
    code, body = _req("POST", f"{HOST}/prowlarr/api/v1/applications",
                      PROW_KEY, payload)
    if code not in (200, 201):
        print(f"  ! POST failed HTTP {code}: {body[:300]}")
        return
    new = json.loads(body)
    print(f"  ✓ Readarr registered as Prowlarr app id={new.get('id')}")

    # Trigger a sync command (blocks until indexers propagate)
    cmd_payload = {"name": "ApplicationIndexerSync", "applicationId": new.get("id")}
    code, body = _req("POST", f"{HOST}/prowlarr/api/v1/command", PROW_KEY, cmd_payload)
    if code in (200, 201):
        print(f"  ✓ ApplicationIndexerSync queued ({code})")
    else:
        print(f"  ! sync command HTTP {code}: {body[:200]}")


# ----- B: delete stale download clients ---------------------------------

STALE_DC_IMPLS = {"RTorrent", "Transmission"}

def fix_b_prune_download_clients(dry_run: bool) -> None:
    print("\n--- B. Prune stale disabled download clients ---")
    arrs = [
        ("sonarr", "v3"),
        ("sonarr2", "v3"),
        ("radarr", "v3"),
        ("radarr2", "v3"),
        ("readarr", "v1"),
    ]
    for name, ver in arrs:
        api_key = (SECRETS / f"{name}.key").read_text().strip()
        urlbase = (SECRETS / f"{name}.urlbase").read_text().strip()
        code, body = _req("GET", f"{HOST}/{urlbase}/api/{ver}/downloadclient", api_key)
        if code != 200:
            print(f"  ! {name}: GET downloadclient HTTP {code}")
            continue
        clients = json.loads(body)
        for c in clients:
            impl = c.get("implementation", "")
            cid = c.get("id")
            cname = c.get("name", "?")
            enabled = c.get("enable", False)
            if impl in STALE_DC_IMPLS:
                # Defensive: only delete DISABLED ones — never wipe an active client
                if enabled:
                    print(f"  ⚠ {name}: {cname} ({impl}) is ENABLED — refusing to delete (operator review)")
                    continue
                if dry_run:
                    print(f"  [dry-run] would DELETE {name}/downloadclient/{cid} ({cname})")
                    continue
                code, body = _req("DELETE",
                                   f"{HOST}/{urlbase}/api/{ver}/downloadclient/{cid}",
                                   api_key)
                if code in (200, 204):
                    print(f"  ✓ {name}: deleted {cname} ({impl}, id={cid})")
                else:
                    print(f"  ! {name}: DELETE {cid} HTTP {code}: {body[:200]}")


# ----- C: delete underperforming Prowlarr indexers --------------------

UNDERPERFORMERS = {"Internet Archive", "Magnet Cat", "1337x"}

def fix_c_remove_dead_indexers(dry_run: bool) -> None:
    print("\n--- C. Remove underperforming Prowlarr indexers ---")
    code, body = _req("GET", f"{HOST}/prowlarr/api/v1/indexer", PROW_KEY)
    if code != 200:
        print(f"  ! GET /indexer HTTP {code}")
        return
    idxs = json.loads(body)
    targets = [i for i in idxs if i.get("name") in UNDERPERFORMERS]
    if not targets:
        print("  (none of the named indexers exist — already removed?)")
        return
    for idx in targets:
        iid = idx.get("id")
        name = idx.get("name", "?")
        if dry_run:
            print(f"  [dry-run] would DELETE indexer/{iid} ({name})")
            continue
        code, body = _req("DELETE", f"{HOST}/prowlarr/api/v1/indexer/{iid}", PROW_KEY)
        if code in (200, 204):
            print(f"  ✓ deleted {name} (id={iid})")
        else:
            print(f"  ! DELETE {iid} HTTP {code}: {body[:200]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="show planned actions without executing")
    args = parser.parse_args()
    print(f"arr-audit-fixes — {'DRY-RUN' if args.dry_run else 'LIVE'} mode")

    fix_a_register_readarr(args.dry_run)
    fix_b_prune_download_clients(args.dry_run)
    fix_c_remove_dead_indexers(args.dry_run)
    print("\ndone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
