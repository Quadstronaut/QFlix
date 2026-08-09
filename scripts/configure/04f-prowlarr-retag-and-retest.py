#!/usr/bin/env python3
"""Tag the failing CF-blocked indexers with `cloudflare` (route through FlareSolverr),
re-test, and reconcile enable state. Idempotent."""
import json, os, socket, time, urllib.request, urllib.error

KEY = os.environ["PROW_KEY"]; URL = os.environ["PROW_URL"]
RETAG = {"Magnet Cat", "Torrent[CORE]", "Internet Archive"}

def req(method, path, body=None, timeout=120):
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
    except (socket.timeout, urllib.error.URLError) as e:
        return 0, str(e)

def first_err(resp):
    if isinstance(resp, list) and resp:
        return resp[0].get("errorMessage", str(resp[0]))[:90]
    if isinstance(resp, str): return resp[:90]
    return str(resp)[:90]

_, tags = req("GET", "/tag")
cf_tag = next((t["id"] for t in tags if t["label"] == "cloudflare"), None)
print(f"cloudflare tag id = {cf_tag}")

_, indexers = req("GET", "/indexer")
for ind in indexers:
    if ind["name"] in RETAG:
        cur = set(ind.get("tags") or [])
        if cf_tag in cur:
            print(f"  = {ind['name']} already cloudflare-tagged")
            continue
        body = json.loads(json.dumps(ind))
        body["tags"] = list(cur | {cf_tag})
        code, _ = req("PUT", f"/indexer/{ind['id']}?forceSave=true", body, timeout=60)
        print(f"  ~ {ind['name']} → cloudflare (HTTP {code})")

print()
print("Retesting all indexers...")
_, indexers = req("GET", "/indexer")
indexers.sort(key=lambda x: x["name"].lower())
results = []
for ind in indexers:
    code, body = req("POST", "/indexer/test", body=ind, timeout=120)
    ok = code == 200
    msg = "" if ok else first_err(body)
    results.append((ind, ok, msg))

# Reconcile
toggles = 0
for ind, ok, _ in results:
    if ind.get("enable") != ok:
        body = json.loads(json.dumps(ind))
        body["enable"] = ok
        req("PUT", f"/indexer/{ind['id']}?forceSave=true", body, timeout=60)
        toggles += 1

passed = sum(1 for r in results if r[1])
total = len(results)
print()
print(f"=== {passed}/{total} respond ({passed*100/total:.1f}%), {toggles} toggled")
print()
print("PASSING:")
for ind, ok, _ in results:
    if ok: print(f"  + {ind['name']}")
print()
print("FAILING:")
for ind, ok, msg in results:
    if not ok: print(f"  - {ind['name']:<25s}  {msg}")
