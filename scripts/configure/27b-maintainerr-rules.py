#!/usr/bin/env python3
"""Create Maintainerr 60-day deletion rules for Plex libraries.

Spec §7.1/§7.2: 60 days from add, 14-day warning, daily run, delete via *arr.
After the 2026-05-11 Plex/Jellyfin merge, every library (Movies / TV / Anime /
Anime Movies) lives in Plex; this script applies the canonical 60-day rule to
each, routing Anime libraries to the Sonarr2/Radarr2 anime branch.
"""
import json, os, ssl, urllib.request, urllib.error
from pathlib import Path


def _seedbox_host() -> str:
    # Allow env override (for tests / dry-runs from a workstation); otherwise
    # read the real FQDN from ~/secrets/seedbox.host — same pattern as
    # scripts/canaries/deletion.sh. The sanitized placeholder is never used
    # at runtime; it only appears in committed text.
    env = os.environ.get("PUBLIC_HOST")
    if env:
        return env
    return Path("~/secrets/seedbox.host").expanduser().read_text(encoding="utf-8").strip()


_HOST = _seedbox_host()
_USERPART, _DOMAIN = _HOST.split(".", 1)
BASE = f"https://maintainerr-{_USERPART}.{_DOMAIN}"
HTPW = os.environ["HTPW"]
MTKEY = os.environ["MTKEY"]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

import base64
basic = base64.b64encode(f"quadstronaut:{HTPW}".encode()).decode()


def req(path, method="GET", body=None):
    h = {"X-Api-Key": MTKEY, "Authorization": f"Basic {basic}"}
    data = None
    if body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    r = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, context=ctx, timeout=30) as resp:
            t = resp.read().decode()
            return resp.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        b = e.read().decode()
        try:
            return e.code, json.loads(b)
        except Exception:
            return e.code, b


# Verify libraries first
code, libs = req("/api/plex/libraries")
print(f"Libraries fetch: HTTP {code}")
if code != 200:
    raise SystemExit(f"can't proceed — libraries={libs!r}")
print(f"  {len(libs)} libraries: {[(l['key'], l['title'], l['type']) for l in libs]}")

# Existing rule groups (idempotency)
code, existing = req("/api/rules")
existing_names = {g.get("name") for g in (existing or [])}
print(f"Existing rule groups: {existing_names or '(none)'}")

# Build per-library rule definitions
# - 60 days total retention: enter collection at day 46 (rule trigger: addDate older than 46d)
# - deleteAfterDays=14 → 14 days after entering, removed → day 60
# - daily cron schedule
DAYS_THRESHOLD = 46   # warning starts here
DAYS_IN_COLLECTION = 14
THRESHOLD_SECONDS = DAYS_THRESHOLD * 86400

# rule: Plex.addDate BEFORE (now - 46d)
# firstVal = [Application.PLEX=0, addDate prop id=0]
# action = RulePossibility.BEFORE = 5
# customVal = { ruleTypeId: NUMBER (key '0'), value: '<seconds>' }
RULE = {
    "operator": None,        # first rule has no logical operator
    "action": 5,             # BEFORE
    "section": 0,
    "firstVal": [0, 0],      # PLEX, addDate
    "customVal": {"ruleTypeId": 0, "value": str(THRESHOLD_SECONDS)},
}

# Map Plex library type to default *arr instance + dataType
TARGETS = {
    "movie": {"radarrSettingsId": 1, "sonarrSettingsId": None, "dataType": "movie"},
    "show":  {"radarrSettingsId": None, "sonarrSettingsId": 1, "dataType": "show"},
}

# Per-library-title overrides: anime libraries route to the Sonarr2/Radarr2
# instances (anime branch). Maintainerr Settings ids: 1 = primary, 2 = anime.
# Keys are CANONICAL SHORT NAMES (Plex title with the "QFlix - " prefix
# stripped) so this dict survives Plex library renames as long as the
# short identifier (Anime / Anime Movies / Movies / TV) stays stable.
NAME_OVERRIDES = {
    "Anime":        {"radarrSettingsId": None, "sonarrSettingsId": 2, "dataType": "show"},
    "Anime Movies": {"radarrSettingsId": 2,    "sonarrSettingsId": None, "dataType": "movie"},
}


def canonical_short_name(plex_title: str) -> str:
    """Strip the 'QFlix - ' prefix that the operator added during the
    2026-05 Plex rename. Falls back to the literal title if it doesn't
    start with the prefix, so pre-rename installs still work."""
    prefix = "QFlix - "
    return plex_title[len(prefix):] if plex_title.startswith(prefix) else plex_title


for lib in libs:
    lib_type = lib["type"]   # 'movie' or 'show'
    lib_key = str(lib["key"])
    title = lib["title"]
    short = canonical_short_name(title)
    name = f"QFlix {short}-60d"

    if name in existing_names:
        print(f"  [skip] '{name}' already exists")
        continue

    target = NAME_OVERRIDES.get(short) or TARGETS.get(lib_type)
    if not target:
        print(f"  [skip] '{title}' unknown type {lib_type}")
        continue

    body = {
        "libraryId": lib_key,
        "name": name,
        "description": f"Auto-delete items added more than 60 days ago (warns at day {DAYS_THRESHOLD})",
        "isActive": True,
        "arrAction": 0,                       # DELETE
        "useRules": True,
        "ruleHandlerCronSchedule": "0 4 * * *",  # daily 04:00
        "collection": {
            "visibleOnRecommended": False,
            "visibleOnHome": False,
            "deleteAfterDays": DAYS_IN_COLLECTION,
            "manualCollection": False,
            "manualCollectionName": "",
            "keepLogsForMonths": 6,
            "sortTitle": None,
            "overlayEnabled": False,
            "overlayTemplateId": None,
        },
        "listExclusions": False,
        "forceSeerr": False,                  # mark as deleted in Seerr → re-requestable
        "rules": [RULE],
        "dataType": target["dataType"],
        "tautulliWatchedPercentOverride": None,
        "notifications": [],
        "radarrSettingsId": target["radarrSettingsId"],
        "sonarrSettingsId": target["sonarrSettingsId"],
        "radarrQualityProfileId": None,
        "sonarrQualityProfileId": None,
    }

    code, resp = req("/api/rules", method="POST", body=body)
    if code in (200, 201) and (resp is None or resp.get("code") == 1):
        print(f"  [ok] '{name}' created — lib={lib_key}, action=DELETE, schedule=daily-04:00")
    else:
        print(f"  [FAIL] '{name}' HTTP {code}: {str(resp)[:200]}")

# Final state
code, final = req("/api/rules")
print(f"\nRule groups total: {len(final or [])}")
for g in (final or []):
    print(f"  - {g.get('name')} (id={g.get('id')}, lib={g.get('libraryId')}, active={g.get('isActive')})")
