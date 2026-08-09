#!/usr/bin/env python3
"""Disable every 2160p quality entry on every quality profile across the
*arr stack (sonarr, sonarr2, radarr, radarr2). Idempotent — safe to re-run.

Closes the recyclarr-no-4k smoke gate that flags the three factory-default
profiles Recyclarr does not manage:
  - sonarr2  Ultra-HD (id=5) — 2 UHD entries
  - radarr2  Any      (id=1) — 3 UHD entries
  - radarr2  Ultra-HD (id=5) — 3 UHD entries

After disabling 2160p, fixes up:
  - profile.cutoff (if it pointed at a now-disabled quality, reset to the
    highest-id allowed item — promoting Ultra-HD profiles to 1080p)
  - profile must have >=1 allowed quality (enables the highest 1080p
    quality if every quality was disabled)

Run on the seedbox (reads ~/secrets/<arr>.{key,port,urlbase}). Or pipe via
SSH:  sshm "python3 -" < scripts/configure/57-no-4k-enforce.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


ARRS = {
    "sonarr":  "v3",
    "sonarr2": "v3",
    "radarr":  "v3",
    "radarr2": "v3",
}


def secret(name: str) -> str:
    with open(os.path.expanduser(f"~/secrets/{name}")) as f:
        return f.read().strip()


def secret_or(name: str, fallback: str) -> str:
    try:
        return secret(name)
    except FileNotFoundError:
        return fallback


def is_group(item: dict) -> bool:
    """A group has its own id+name and a nested `items` list."""
    return ("id" in item and "name" in item and isinstance(item.get("items"), list))


def disable_2160(items: list) -> int:
    """Walk items recursively, set allowed=false on any quality whose
    quality.name contains '2160'. Returns count toggled."""
    toggled = 0
    for item in items or []:
        if is_group(item):
            toggled += disable_2160(item.get("items") or [])
            # If any sub-quality is allowed, group remains allowed; else disable
            any_allowed = any(
                bool(s.get("allowed"))
                for s in (item.get("items") or [])
            )
            if not any_allowed and item.get("allowed"):
                item["allowed"] = False
        else:
            q = item.get("quality") or {}
            name = q.get("name") if isinstance(q, dict) else None
            if name and "2160" in name and item.get("allowed"):
                item["allowed"] = False
                toggled += 1
    return toggled


def collect_allowed_ids(items: list) -> set:
    """Return ids of all currently-allowed items (quality ids or group ids)."""
    out: set = set()
    for item in items or []:
        if is_group(item):
            if item.get("allowed"):
                out.add(item.get("id"))
            out |= collect_allowed_ids(item.get("items") or [])
        else:
            if item.get("allowed"):
                q = item.get("quality") or {}
                qid = q.get("id") if isinstance(q, dict) else None
                if qid is not None:
                    out.add(qid)
    return out


def enable_first_1080p(items: list) -> int | None:
    """Find the first quality whose name contains '1080', enable it,
    return its id. Returns None if no 1080p found."""
    for item in items or []:
        if is_group(item):
            r = enable_first_1080p(item.get("items") or [])
            if r is not None:
                # enabling sub re-enables the group too (kept implicit)
                if not item.get("allowed"):
                    item["allowed"] = True
                return r
        else:
            q = item.get("quality") or {}
            name = q.get("name") if isinstance(q, dict) else ""
            if name and "1080" in name:
                item["allowed"] = True
                return q.get("id") if isinstance(q, dict) else None
    return None


def fix_cutoff(profile: dict) -> None:
    items = profile.get("items") or []
    allowed = collect_allowed_ids(items)
    cutoff = profile.get("cutoff")
    if cutoff in allowed:
        return
    if allowed:
        profile["cutoff"] = max(allowed)
        return
    # No allowed items — enable a 1080p one
    new_id = enable_first_1080p(items)
    if new_id is not None:
        profile["cutoff"] = new_id


def main() -> int:
    grand_total = 0
    profiles_changed = 0
    for arr, api_v in ARRS.items():
        try:
            key = secret(f"{arr}.key")
            port = secret(f"{arr}.port")
        except FileNotFoundError as exc:
            print(f"[{arr}] skipped: {exc}")
            continue
        base = secret_or(f"{arr}.urlbase", arr).strip("/")
        url_base = f"http://127.0.0.1:{port}/{base}/api/{api_v}/qualityprofile"

        try:
            req = urllib.request.Request(url_base, headers={"X-Api-Key": key})
            profiles = json.loads(urllib.request.urlopen(req, timeout=15).read())
        except Exception as exc:
            print(f"[{arr}] GET qualityprofile failed: {exc}", file=sys.stderr)
            continue

        for p in profiles:
            toggled = disable_2160(p.get("items") or [])
            if toggled == 0:
                continue
            fix_cutoff(p)
            pid = p.get("id")
            pname = p.get("name", "?")
            try:
                req = urllib.request.Request(
                    f"{url_base}/{pid}",
                    data=json.dumps(p).encode("utf-8"),
                    headers={
                        "X-Api-Key": key,
                        "Content-Type": "application/json",
                    },
                    method="PUT",
                )
                urllib.request.urlopen(req, timeout=15).read()
                print(f"[{arr}] disabled {toggled} 2160p entries on "
                      f"profile '{pname}' (id={pid}); cutoff={p.get('cutoff')}")
                grand_total += toggled
                profiles_changed += 1
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:200]
                print(f"[{arr}] PUT profile '{pname}' failed: "
                      f"{exc.code} {body}", file=sys.stderr)
            except Exception as exc:
                print(f"[{arr}] PUT profile '{pname}' failed: {exc}",
                      file=sys.stderr)

    print()
    print(f"Profiles modified: {profiles_changed}")
    print(f"Total 2160p entries disabled: {grand_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
