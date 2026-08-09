#!/usr/bin/env python3
"""Curated bulk-add + audit for Prowlarr. Run on manitoba; reads PROW_KEY + PROW_URL from env.

Strategy:
1. Delete indexers NOT in the curated list (operator wants exactly these 21).
2. Add missing curated ones, treating HTTP 400 + socket-timeout as "saved-with-warnings"
   (Prowlarr returns 400 from forceSave when the post-save connectivity test fails, but the
   record IS persisted). Verify presence by re-listing after each POST.
3. Tag CF-needy indexers as `cloudflare`.
4. POST /indexer/test on each; reconcile enable=test_passed.
"""
import json, os, socket, time, urllib.request, urllib.error

KEY = os.environ["PROW_KEY"]
URL = os.environ["PROW_URL"]

def req(method, path, body=None, timeout=180):
    h = {"X-Api-Key": KEY, "Content-Type": "application/json"}
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
    except socket.timeout:
        return 0, "timeout"
    except urllib.error.URLError as e:
        return 0, f"urlerror: {e}"

def first_err(resp):
    if isinstance(resp, list) and resp:
        return resp[0].get("errorMessage", str(resp[0]))[:90]
    if isinstance(resp, str): return resp[:90]
    return str(resp)[:90]

manifest = json.load(open("/tmp/prowlarr-curated.json"))
wanted = manifest["names"]
wanted_lc = {n.lower() for n in wanted}
needs_fs = set(manifest["needs_flaresolverr"])

_, profiles = req("GET", "/appprofile")
ap_id = next((p["id"] for p in (profiles or []) if p.get("name") == "Standard"), 1)

print("Fetching schema catalog...", flush=True)
_, schemas = req("GET", "/indexer/schema")
print(f"  {len(schemas)} schemas")
schema_by_name = {s.get("name", "").lower(): s for s in schemas}
schema_by_def  = {s.get("definitionName", "").lower(): s for s in schemas}

# 1. Delete indexers NOT in curated list (clean slate per operator filter)
print()
print("=== Pruning indexers not in curated list...")
_, indexers = req("GET", "/indexer")
to_delete = [i for i in indexers if i["name"].lower() not in wanted_lc]
for ind in to_delete:
    code, _ = req("DELETE", f"/indexer/{ind['id']}", timeout=30)
    print(f"  - DELETE {ind['name']} (id={ind['id']}) → HTTP {code}")

# 2. Add missing from curated list
_, indexers = req("GET", "/indexer")
have = {i["name"].lower() for i in indexers}

_, tags = req("GET", "/tag")
tag_by_label = {t["label"]: t["id"] for t in tags}
def ensure_tag(label):
    if label in tag_by_label: return tag_by_label[label]
    _, t = req("POST", "/tag", {"label": label})
    tag_by_label[label] = t["id"]
    return t["id"]
cf_tag = ensure_tag("cloudflare")

print()
print("=== Adding missing curated indexers...")
for name in wanted:
    if name.lower() in have:
        continue
    schema = schema_by_name.get(name.lower()) or schema_by_def.get(name.lower())
    if not schema:
        print(f"  ? {name}: schema not in Prowlarr catalog (skipping)")
        continue
    body = json.loads(json.dumps(schema))
    body["name"] = name
    body["enable"] = True
    body["appProfileId"] = ap_id
    body["tags"] = [cf_tag] if name in needs_fs else []
    body["indexerProxyId"] = 0
    print(f"  + adding {name} (this may take up to 100s if indexer is unreachable)...", flush=True)
    t0 = time.time()
    code, resp = req("POST", "/indexer?forceSave=true", body, timeout=180)
    dt = time.time() - t0
    # Verify by re-fetching the indexer list (forceSave may have saved despite HTTP 400)
    _, current = req("GET", "/indexer")
    saved = next((i for i in current if i["name"].lower() == name.lower()), None)
    if saved:
        print(f"    ✓ saved (id={saved['id']}, http={code}, {dt:.1f}s)")
    else:
        print(f"    ✗ NOT saved (http={code} {first_err(resp)}, {dt:.1f}s)")

# Tag CF-needy that exist
_, indexers = req("GET", "/indexer")
for ind in indexers:
    if ind["name"] in needs_fs:
        cur = set(ind.get("tags") or [])
        if cf_tag not in cur:
            body = json.loads(json.dumps(ind))
            body["tags"] = list(cur | {cf_tag})
            code, _ = req("PUT", f"/indexer/{ind['id']}?forceSave=true", body, timeout=60)
            if code in (200, 202): print(f"  ~ retagged {ind['name']} cloudflare")

# 3. Audit
print()
print("=== Auditing reachability (POST /indexer/test)...", flush=True)
_, indexers = req("GET", "/indexer")
indexers.sort(key=lambda x: x["name"].lower())
results, t0 = [], time.time()
for i, ind in enumerate(indexers, 1):
    code, body = req("POST", "/indexer/test", body=ind, timeout=120)
    ok = code == 200
    msg = "" if ok else first_err(body)
    results.append((ind, ok, msg))
    if i % 5 == 0 or i == len(indexers):
        passing = sum(1 for r in results if r[1])
        print(f"  ... {i}/{len(indexers)} ({passing} pass, {int(time.time()-t0)}s)", flush=True)

# 4. Reconcile
to_off, to_on = 0, 0
for ind, ok, _ in results:
    if ind.get("enable") == ok: continue
    body = json.loads(json.dumps(ind))
    body["enable"] = ok
    code, _ = req("PUT", f"/indexer/{ind['id']}?forceSave=true", body, timeout=60)
    if code in (200, 202):
        if ok: to_on += 1
        else:  to_off += 1

passed = sum(1 for r in results if r[1])
total = len(results)
print()
print(f"=== Reconciled: +{to_on} enabled, -{to_off} disabled")
print(f"=== FINAL: {passed}/{total} respond ({passed*100/total:.1f}%)")
print()
print("PASSING:")
for ind, ok, _ in results:
    if ok: print(f"  + {ind['name']}")
print()
print("FAILING (now disabled):")
for ind, ok, msg in results:
    if not ok: print(f"  - {ind['name']:<28s} {msg}")
