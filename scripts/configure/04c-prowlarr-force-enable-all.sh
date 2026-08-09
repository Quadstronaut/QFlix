#!/usr/bin/env bash
# Operator decision (Phase 3.3 follow-up): enable all indexers regardless of
# Prowlarr's pre-save test, then run a real POST /api/v1/indexer/{id}/test on
# each so we can see which actually return live results.
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
import json, os, urllib.request, urllib.error
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

# Discover ?forceSave=true mode (Prowlarr supports skipping the pre-save test)
_, indexers = req("GET", "/indexer")
indexers.sort(key=lambda x: x["name"].lower())

enabled_results = []
for ind in indexers:
    if ind.get("enable"):
        enabled_results.append((ind["name"], "already-enabled", 200))
        continue
    body = json.loads(json.dumps(ind))
    body["enable"] = True
    code, resp = req("PUT", f"/indexer/{ind['id']}?forceSave=true", body)
    if code in (200, 202):
        enabled_results.append((ind["name"], "force-enabled", code))
    else:
        # Try without ?forceSave (older Prowlarr) — and parse error if any
        code2, resp2 = req("PUT", f"/indexer/{ind['id']}", body)
        if code2 in (200, 202):
            enabled_results.append((ind["name"], "enabled (no-force)", code2))
        else:
            err = ""
            if isinstance(resp, list) and resp:
                err = resp[0].get("errorMessage", str(resp[0]))[:80]
            elif isinstance(resp, str):
                err = resp[:80]
            enabled_results.append((ind["name"], f"FAIL: HTTP {code}: {err}", code))

print("=== Enable results ===")
for name, status, code in enabled_results:
    print(f"  {name:<25s}  {status}")

# Now POST /api/v1/indexer/{id}/test on every (final) indexer
_, indexers = req("GET", "/indexer")
indexers.sort(key=lambda x: x["name"].lower())

print("\n=== Real reachability test (POST /indexer/{id}/test) ===")
print(f"{'name':<25s}  {'enabled':<7s}  {'test':<6s}  cf  notes")
print("-" * 100)
_, tags = req("GET", "/tag")
cf_tag_id = next((t["id"] for t in tags if t["label"] == "cloudflare"), None)
passed = 0
total = len(indexers)
test_results = []
for ind in indexers:
    code, body = req("POST", "/indexer/test", body=ind, timeout=120)
    if code in (200, 202):
        ok = True
        msg = ""
    else:
        ok = False
        if isinstance(body, list) and body:
            msg = body[0].get("errorMessage", str(body[0]))[:60]
        elif isinstance(body, dict):
            msg = body.get("message", str(body)[:60])
        else:
            msg = str(body)[:60]
    if ok: passed += 1
    en = "yes" if ind.get("enable") else "no"
    cf = "Y" if (cf_tag_id is not None and cf_tag_id in (ind.get("tags") or [])) else "."
    test_results.append((ind["name"], en, ok, cf, msg))
    print(f"  {ind['name']:<25s}  {en:<7s}  {('PASS' if ok else 'FAIL'):<6s}  {cf}   {msg}")

rate = passed * 100.0 / total if total else 0.0
print(f"\nReachability: {passed}/{total} pass = {rate:.1f}%")
print(f"Operator threshold: 50%   |   {'OK' if rate >= 50 else 'BELOW THRESHOLD'}")

import sys
sys.exit(0 if rate >= 50 else 0)  # never fail this script — it's informational
PY
REMOTE
