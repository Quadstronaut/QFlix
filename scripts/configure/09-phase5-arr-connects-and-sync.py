#!/usr/bin/env python3
"""Phase 5: Sonarr/Radarr Plex+Notifiarr Connects, Prowlarr Apps Sync, anime indexer tagging.

Run on manitoba; reads PROW_KEY, SONARR_*, RADARR_*, SONARR2_*, RADARR2_*, PLEX_HOST,
PLEX_PORT, PLEX_TOKEN, NOTIFIARR_KEY from env.

Idempotent.
"""
import json, os, socket, time, urllib.request, urllib.error

ANIME_INDEXERS = {"Bangumi Moe", "Nyaa.si", "nekoBT", "subsplease"}

def req(url, method="GET", api_key=None, body=None, timeout=60, key_header="X-Api-Key"):
    h = {"Content-Type": "application/json"}
    if api_key: h[key_header] = api_key
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
    if isinstance(resp, str): return resp[:120]
    return str(resp)[:120]

# ===== 1. Tag anime indexers in Prowlarr =====
PROW_KEY = os.environ["PROW_KEY"]
PROW_URL = f"http://127.0.0.1:{os.environ['PROW_PORT']}/{os.environ['PROW_BASE']}/api/v1"

print("=== 1. Tagging anime indexers in Prowlarr ===")
_, tags = req(f"{PROW_URL}/tag", api_key=PROW_KEY)
tag_map = {t["label"]: t["id"] for t in tags}
def ensure_tag(label):
    if label in tag_map: return tag_map[label]
    _, t = req(f"{PROW_URL}/tag", method="POST", api_key=PROW_KEY, body={"label": label})
    tag_map[label] = t["id"]
    return t["id"]
anime_tag = ensure_tag("anime")
print(f"  anime tag id = {anime_tag}")

_, indexers = req(f"{PROW_URL}/indexer", api_key=PROW_KEY)
for ind in indexers:
    if ind["name"] in ANIME_INDEXERS:
        cur = set(ind.get("tags") or [])
        if anime_tag in cur:
            print(f"  = {ind['name']} already anime-tagged")
            continue
        body = json.loads(json.dumps(ind))
        body["tags"] = list(cur | {anime_tag})
        code, _ = req(f"{PROW_URL}/indexer/{ind['id']}?forceSave=true", method="PUT", api_key=PROW_KEY, body=body)
        print(f"  ~ {ind['name']} → anime (HTTP {code})")

# ===== 2. Plex + Notifiarr Connects on Sonarr + Radarr (existing v3 *arrs) =====
PLEX_HOST = os.environ["PLEX_HOST"]
PLEX_PORT = int(os.environ["PLEX_PORT"])
PLEX_TOKEN = os.environ["PLEX_TOKEN"]
NOTIFIARR_KEY = os.environ["NOTIFIARR_KEY"]

def build_notif_from_schema(api, key, impl, on_events, field_overrides):
    """Fetch the notification schema for impl and produce a POST body. Field
    names differ between Sonarr/Radarr (e.g. apiKey vs aPIKey) — schema-first
    avoids guessing."""
    code, schemas = req(f"{api}/notification/schema", api_key=key)
    s = next((x for x in (schemas or []) if x.get("implementation") == impl), None)
    if not s: return None
    body = json.loads(json.dumps(s))
    for k, v in on_events.items():
        if k in body: body[k] = v
    for f in body.get("fields", []):
        # Match by case-insensitive key against overrides
        for k, v in field_overrides.items():
            if f["name"].lower() == k.lower():
                f["value"] = v
                break
    body["tags"] = []
    return body

def configure_arr_connects(label, port, base, key):
    api = f"http://127.0.0.1:{port}/{base}/api/v3"
    print(f"\n=== 2. {label} ({api}) — Plex + Notifiarr Connects ===")

    code, notifs = req(f"{api}/notification", api_key=key)
    have_plex = any(n.get("implementation") == "PlexServer" for n in (notifs or []))
    have_notif = any(n.get("implementation") == "Notifiarr" for n in (notifs or []))

    # Plex Connect
    if have_plex:
        # Update host/token if stale (existing Sonarr from prior config)
        plex = next(n for n in notifs if n.get("implementation") == "PlexServer")
        cur_host = next((f.get("value") for f in plex.get("fields", []) if f["name"] == "host"), "")
        if cur_host != PLEX_HOST:
            print(f"  ~ updating Plex Connect host: {cur_host} → {PLEX_HOST}")
            updated = json.loads(json.dumps(plex))
            for f in updated.get("fields", []):
                if f["name"] == "host":      f["value"] = PLEX_HOST
                elif f["name"] == "port":    f["value"] = PLEX_PORT
                elif f["name"] == "useSsl":  f["value"] = False
                elif f["name"] == "authToken": f["value"] = PLEX_TOKEN
            code, _ = req(f"{api}/notification/{plex['id']}?forceSave=true", method="PUT", api_key=key, body=updated)
            if code in (200, 202): print(f"    ✓ Plex Connect updated")
            else: print(f"    ! Plex Connect update failed: HTTP {code}")
        else:
            print(f"  = Plex Connect already current (host={cur_host})")
    else:
        body = build_notif_from_schema(api, key, "PlexServer",
            {"onDownload": True, "onUpgrade": True, "onRename": True, "onGrab": False,
             "onHealthIssue": False, "onApplicationUpdate": False},
            {"host": PLEX_HOST, "port": PLEX_PORT, "useSsl": False,
             "authToken": PLEX_TOKEN, "updateLibrary": True})
        if body:
            body["name"] = "Plex"
            code, resp = req(f"{api}/notification?forceSave=true", method="POST", api_key=key, body=body)
            if code in (200, 201):
                print(f"  + Plex Connect added (id={resp.get('id')})")
            else:
                print(f"  ! Plex Connect failed: HTTP {code} {first_err(resp)}")
        else:
            print(f"  ! Plex schema not found")

    # Notifiarr Connect (schema-first to handle Sonarr.apiKey vs Radarr.aPIKey)
    if have_notif:
        print(f"  = Notifiarr Connect already exists")
    else:
        body = build_notif_from_schema(api, key, "Notifiarr",
            {"onGrab": True, "onDownload": True, "onUpgrade": True, "onHealthIssue": True},
            {"apiKey": NOTIFIARR_KEY})  # build_notif_from_schema does case-insensitive match → covers aPIKey too
        if body:
            body["name"] = "Notifiarr"
            code, resp = req(f"{api}/notification?forceSave=true", method="POST", api_key=key, body=body)
            if code in (200, 201):
                print(f"  + Notifiarr Connect added (id={resp.get('id')})")
            else:
                print(f"  ! Notifiarr Connect failed: HTTP {code} {first_err(resp)}")
        else:
            print(f"  ! Notifiarr schema not found")

    # Test (POST /notification/test, no id, body=full notif)
    code, notifs = req(f"{api}/notification", api_key=key)
    for n in notifs or []:
        if n.get("implementation") in ("PlexServer", "Notifiarr"):
            test_code, test_body = req(f"{api}/notification/test", method="POST", api_key=key, body=n)
            label_n = n.get("name")
            if test_code in (200, 202):
                print(f"  ✓ {label_n} test OK")
            else:
                print(f"  ✗ {label_n} test failed: HTTP {test_code} {first_err(test_body)}")

configure_arr_connects("Sonarr",  os.environ["SONARR_PORT"],  os.environ.get("SONARR_BASE", "sonarr"),  os.environ["SONARR_KEY"])
configure_arr_connects("Radarr",  os.environ["RADARR_PORT"],  os.environ.get("RADARR_BASE", "radarr"),  os.environ["RADARR_KEY"])
configure_arr_connects("Sonarr2", os.environ["SONARR2_PORT"], os.environ.get("SONARR2_BASE", "sonarr2"), os.environ["SONARR2_KEY"])
configure_arr_connects("Radarr2", os.environ["RADARR2_PORT"], os.environ.get("RADARR2_BASE", "radarr2"), os.environ["RADARR2_KEY"])

# ===== 3. Prowlarr Apps Sync =====
print("\n=== 3. Prowlarr Apps Sync — register all 4 *arrs ===")

# anime tag id for Sonarr2/Radarr2 filtering
anime_tag_id = anime_tag

# *arr container reaches Prowlarr container via... within docker0 they should be able to use 172.17.0.1:17024
# (Prowlarr is bound to 127.0.0.1 from host's perspective; from another container, must use docker0 gateway)
PROW_URL_FOR_ARR = f"http://172.17.0.1:{os.environ['PROW_PORT']}/{os.environ['PROW_BASE']}"

def register_app(name, impl, port, base, key, tags):
    api_url = f"http://172.17.0.1:{port}/{base}"
    code, apps = req(f"{PROW_URL}/applications", api_key=PROW_KEY)
    existing = next((a for a in apps or [] if a.get("name") == name), None)
    if existing:
        # Update tags if they differ
        cur_tags = sorted(existing.get("tags") or [])
        if cur_tags == sorted(tags):
            print(f"  = {name} already registered (tags={tags})")
            return
        existing["tags"] = tags
        code, _ = req(f"{PROW_URL}/applications/{existing['id']}?forceSave=true", method="PUT", api_key=PROW_KEY, body=existing)
        print(f"  ~ {name} tags updated → {tags} (HTTP {code})")
        return

    # Fetch the schema for this implementation
    code, schemas = req(f"{PROW_URL}/applications/schema", api_key=PROW_KEY)
    schema = next((s for s in schemas if s.get("implementation") == impl), None)
    if not schema:
        print(f"  ! No schema for impl={impl}")
        return

    body = json.loads(json.dumps(schema))
    body["name"] = name
    body["syncLevel"] = "fullSync"
    body["tags"] = tags
    for f in body["fields"]:
        if f["name"] == "prowlarrUrl": f["value"] = PROW_URL_FOR_ARR
        elif f["name"] == "baseUrl":   f["value"] = api_url
        elif f["name"] == "apiKey":    f["value"] = key
    code, resp = req(f"{PROW_URL}/applications?forceSave=true", method="POST", api_key=PROW_KEY, body=body)
    if code in (200, 201):
        print(f"  + {name} registered (id={resp.get('id')}, tags={tags})")
    else:
        print(f"  ! {name} registration failed: HTTP {code} {first_err(resp)}")

# Sonarr (general TV) — no tag filter, gets all non-anime indexers
register_app(
    "Sonarr", "Sonarr",
    os.environ["SONARR_PORT"], os.environ.get("SONARR_BASE", "sonarr"), os.environ["SONARR_KEY"],
    [],
)
# Radarr (general movies) — no tag filter
register_app(
    "Radarr", "Radarr",
    os.environ["RADARR_PORT"], os.environ.get("RADARR_BASE", "radarr"), os.environ["RADARR_KEY"],
    [],
)
# Sonarr2 (anime TV) — anime tag filter
register_app(
    "Sonarr2 (Anime)", "Sonarr",
    os.environ["SONARR2_PORT"], os.environ.get("SONARR2_BASE", "sonarr2"), os.environ["SONARR2_KEY"],
    [anime_tag_id],
)
# Radarr2 (anime movies) — anime tag filter
register_app(
    "Radarr2 (Anime)", "Radarr",
    os.environ["RADARR2_PORT"], os.environ.get("RADARR2_BASE", "radarr2"), os.environ["RADARR2_KEY"],
    [anime_tag_id],
)

print("\n=== Final apps:")
_, apps = req(f"{PROW_URL}/applications", api_key=PROW_KEY)
for a in apps or []:
    print(f"  {a.get('name'):<20s}  syncLevel={a.get('syncLevel'):<10s}  tags={a.get('tags')}")
