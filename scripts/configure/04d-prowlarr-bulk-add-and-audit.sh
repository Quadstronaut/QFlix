#!/usr/bin/env bash
# Bulk-add every Prowlarr indexer matching the operator's filter, then audit
# (POST /indexer/test) and disable any that don't respond.
#
# Filter (from operator):
#   protocol == "torrent"
#   language in {"en-US", "en-GB"}
#   privacy in {"public", "private"}
#   capabilities.categories includes Movies (id 2000) or TV (id 5000)
#
# This is destructive only on Prowlarr (it adds many indexers). It does NOT
# delete existing ones. Re-runnable: skip-by-name + idempotent enable/disable.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

PROW_KEY="$(secret_read prowlarr.key)"
PROW_PORT="$(secret_read prowlarr.port)"
PROW_BASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"

sshm bash -s "$PROW_KEY" "$PROW_PORT" "$PROW_BASE" <<'REMOTE'
set -euo pipefail
export PROW_KEY="$1"; export PROW_PORT="$2"; export PROW_BASE="$3"
export PROW_URL="http://127.0.0.1:$PROW_PORT/$PROW_BASE/api/v1"

python3 <<'PY'
import json, os, sys, time, urllib.request, urllib.error
KEY = os.environ["PROW_KEY"]
URL = os.environ["PROW_URL"]

def req(method, path, body=None, timeout=120):
    headers = {"X-Api-Key": KEY, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            txt = resp.read().decode()
            return resp.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try: parsed = json.loads(body)
        except Exception: parsed = body
        return e.code, parsed
    except Exception as e:
        return 0, str(e)

# 1. Pull schemas + existing indexers + the default AppProfile
_, schemas = req("GET", "/indexer/schema")
_, existing = req("GET", "/indexer")
existing_by_name = {ind["name"].lower(): ind for ind in existing}
_, profiles = req("GET", "/appprofile")
default_profile_id = next((p["id"] for p in (profiles or []) if p.get("name") == "Standard"), None) \
                  or (profiles[0]["id"] if profiles else 1)
print(f"=== Using AppProfile id={default_profile_id} ('Standard')")

# 2. Filter
def matches(s):
    if s.get("protocol") != "torrent": return False
    if s.get("language") not in ("en-US", "en-GB"): return False
    if s.get("privacy") not in ("public", "private"): return False
    cats = (s.get("capabilities") or {}).get("categories") or []
    return any(c.get("id") in (2000, 5000) or c.get("name") in ("Movies","TV") for c in cats)

candidates = [s for s in schemas if matches(s)]
candidates.sort(key=lambda x: x.get("name") or "")
print(f"=== {len(candidates)} candidates match filter (of {len(schemas)} schemas)")
print(f"=== {len(existing)} indexers already exist in Prowlarr")

# 3. Add each (skip if exists by name)
added, add_failed, already = [], [], []
for s in candidates:
    name = s.get("name")
    if name and name.lower() in existing_by_name:
        already.append(name)
        continue
    body = json.loads(json.dumps(s))
    body["name"] = name
    body["enable"] = True
    body["tags"] = []
    body["indexerProxyId"] = 0
    body["appProfileId"] = default_profile_id
    code, resp = req("POST", "/indexer?forceSave=true", body, timeout=30)
    if code in (200, 201):
        added.append((name, resp.get("id") if isinstance(resp, dict) else "?"))
    else:
        msg = ""
        if isinstance(resp, list) and resp:
            msg = resp[0].get("errorMessage", str(resp[0]))[:80]
        elif isinstance(resp, str):
            msg = resp[:80]
        else:
            msg = str(resp)[:80]
        add_failed.append((name, code, msg))

print()
print(f"=== Add results: {len(added)} added, {len(already)} already-present, {len(add_failed)} failed")
for n, code, msg in add_failed[:20]:
    print(f"  ! {n}: HTTP {code} {msg}")
if len(add_failed) > 20:
    print(f"  ... and {len(add_failed)-20} more failures")

# 4. Audit: POST /indexer/test for every (now-saved) indexer; disable any that fails.
print()
print("=== Auditing reachability (this can take 5+ minutes for 100+ indexers)...")
_, indexers = req("GET", "/indexer")
indexers.sort(key=lambda x: x.get("name") or "")
total = len(indexers)
print(f"=== Testing {total} indexers...")

results = []  # (name, ok, msg, was_enabled)
t0 = time.time()
for i, ind in enumerate(indexers, 1):
    name = ind.get("name")
    was = ind.get("enable", False)
    code, body = req("POST", "/indexer/test", body=ind, timeout=60)
    if code == 200:
        ok, msg = True, ""
    else:
        ok = False
        if isinstance(body, list) and body:
            msg = body[0].get("errorMessage", str(body[0]))[:70]
        elif isinstance(body, dict):
            msg = body.get("message", str(body)[:70])
        else:
            msg = str(body)[:70]
    results.append((name, ok, msg, was, ind))
    if i % 25 == 0 or i == total:
        elapsed = int(time.time() - t0)
        passed_so_far = sum(1 for r in results if r[1])
        print(f"  ... {i}/{total} done ({passed_so_far} pass, elapsed {elapsed}s)")

# 5. Toggle enable=ok for any indexer where state differs
print()
print("=== Reconciling enable state to test results...")
toggled_on, toggled_off, no_change = 0, 0, 0
for name, ok, msg, was, ind in results:
    if was == ok:
        no_change += 1
        continue
    body = json.loads(json.dumps(ind))
    body["enable"] = ok
    code, _ = req("PUT", f"/indexer/{ind['id']}?forceSave=true", body, timeout=30)
    if code in (200, 202):
        if ok: toggled_on += 1
        else: toggled_off += 1
    else:
        print(f"  ! could not toggle {name}: HTTP {code}")

print(f"  {toggled_on} enabled, {toggled_off} disabled, {no_change} unchanged")

# 6. Final summary
passed = sum(1 for r in results if r[1])
print()
print(f"=== FINAL: {passed}/{total} indexers respond (= {passed*100/total:.1f}%)")
print()
print("PASSING:")
for name, ok, msg, was, ind in results:
    if ok:
        cf = "Y" if any(t for t in (ind.get("tags") or [])) else "."
        print(f"  + {name}")
print()
print("FAILING (now disabled):")
for name, ok, msg, was, ind in results:
    if not ok:
        print(f"  - {name:<35s} {msg}")
PY
REMOTE
