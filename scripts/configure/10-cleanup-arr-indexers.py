#!/usr/bin/env python3
"""Cleanup pass: make Prowlarr the single source of indexer truth.

1. Delete Sonarr/Radarr indexers that are NOT named "X (Prowlarr)" (i.e. manual stale entries).
2. Tag every non-anime indexer in Prowlarr with `general`.
3. Update Sonarr/Radarr apps in Prowlarr to require `general` tag (so anime-only indexers don't leak in).
4. Trigger a re-sync via Prowlarr's app/sync endpoint.
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
    return str(resp)[:120]

PROW_KEY = os.environ["PROW_KEY"]
PROW_URL = f"http://127.0.0.1:{os.environ['PROW_PORT']}/{os.environ['PROW_BASE']}/api/v1"

# 1. Delete non-Prowlarr indexers from Sonarr+Radarr (Sonarr2/Radarr2 are clean — they were freshly installed)
for app in ("sonarr", "radarr"):
    api = f"http://127.0.0.1:{os.environ[f'{app.upper()}_PORT']}/{os.environ[f'{app.upper()}_BASE']}/api/v3"
    key = os.environ[f"{app.upper()}_KEY"]
    print(f"=== Cleaning {app} indexers ({api})")
    code, inds = req(f"{api}/indexer", api_key=key)
    deleted, kept = [], []
    for ind in inds or []:
        name = ind.get("name", "")
        if "(Prowlarr)" in name:
            kept.append(name)
        else:
            code, _ = req(f"{api}/indexer/{ind['id']}", method="DELETE", api_key=key, timeout=30)
            deleted.append(name)
    print(f"  deleted {len(deleted)} stale, kept {len(kept)} (Prowlarr)")

# 2. Tag every non-anime indexer in Prowlarr `general`
print("\n=== Tagging non-anime indexers in Prowlarr `general` ===")
_, tags = req(f"{PROW_URL}/tag", api_key=PROW_KEY)
tag_by_label = {t["label"]: t["id"] for t in tags}
def ensure_tag(label):
    if label in tag_by_label: return tag_by_label[label]
    _, t = req(f"{PROW_URL}/tag", method="POST", api_key=PROW_KEY, body={"label": label})
    tag_by_label[label] = t["id"]
    return t["id"]

anime_tag = ensure_tag("anime")
general_tag = ensure_tag("general")

_, indexers = req(f"{PROW_URL}/indexer", api_key=PROW_KEY)
for ind in indexers:
    is_anime = ind["name"] in ANIME_INDEXERS or anime_tag in (ind.get("tags") or [])
    cur_tags = set(ind.get("tags") or [])
    want_tags = set(cur_tags)
    if is_anime:
        # Make sure anime tag is set, ensure general is NOT (anime stays anime-only)
        want_tags.add(anime_tag)
        want_tags.discard(general_tag)
    else:
        # Non-anime indexer: tag general, leave any cloudflare tag alone
        want_tags.add(general_tag)
        want_tags.discard(anime_tag)
    if want_tags != cur_tags:
        body = json.loads(json.dumps(ind))
        body["tags"] = list(want_tags)
        code, _ = req(f"{PROW_URL}/indexer/{ind['id']}?forceSave=true", method="PUT", api_key=PROW_KEY, body=body)
        cat = "anime" if is_anime else "general"
        print(f"  ~ {ind['name']:<30s} tags → {sorted(want_tags)} ({cat})")

# 3. Update Sonarr/Radarr apps in Prowlarr to require general tag
print("\n=== Updating Sonarr/Radarr apps to require `general` tag ===")
_, apps = req(f"{PROW_URL}/applications", api_key=PROW_KEY)
for a in apps:
    name = a.get("name", "")
    cur_tags = sorted(a.get("tags") or [])
    if name in ("Sonarr", "Radarr"):
        want_tags = sorted([general_tag])
    elif name in ("Sonarr2 (Anime)", "Radarr2 (Anime)"):
        want_tags = sorted([anime_tag])
    else:
        continue
    if cur_tags == want_tags:
        print(f"  = {name} tags already {want_tags}")
        continue
    a["tags"] = want_tags
    code, _ = req(f"{PROW_URL}/applications/{a['id']}?forceSave=true", method="PUT", api_key=PROW_KEY, body=a)
    print(f"  ~ {name} tags → {want_tags} (HTTP {code})")

# 4. Trigger re-sync via Prowlarr's command endpoint
print("\n=== Triggering Prowlarr Apps re-sync ===")
code, resp = req(f"{PROW_URL}/command", method="POST", api_key=PROW_KEY, body={"name": "ApplicationIndexerSync", "forceSync": True})
if code in (200, 201):
    print(f"  + Re-sync queued (commandId={resp.get('id') if isinstance(resp, dict) else '?'})")
else:
    print(f"  ! Re-sync failed: HTTP {code} {first_err(resp)}")

# 5. Wait briefly for sync to propagate, then count indexers per *arr
print("\n=== Waiting 10s for sync...")
time.sleep(10)
for app in ("sonarr", "radarr", "sonarr2", "radarr2"):
    api = f"http://127.0.0.1:{os.environ[f'{app.upper()}_PORT']}/{os.environ[f'{app.upper()}_BASE']}/api/v3"
    key = os.environ[f"{app.upper()}_KEY"]
    code, inds = req(f"{api}/indexer", api_key=key)
    names = sorted(i["name"] for i in inds or [])
    print(f"  {app}: {len(names)} indexers — {names}")
