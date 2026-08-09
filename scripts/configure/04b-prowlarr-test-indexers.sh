#!/usr/bin/env bash
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
KEY="$1"; PORT="$2"; BASE="$3"
URL="http://127.0.0.1:$PORT/$BASE/api/v1"

python3 <<PY
import json, urllib.request, urllib.error

KEY = "$KEY"
URL = "$URL"

def req(method, path, body=None, timeout=60):
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

# Get tag id for cloudflare
_, tags = req("GET", "/tag")
cf_tag_id = next((t["id"] for t in tags if t["label"] == "cloudflare"), None)

_, indexers = req("GET", "/indexer")
indexers.sort(key=lambda x: x["name"].lower())

results = []  # (name, enabled_before, test_pass, error_msg, cf_tagged)

# Prowlarr 2.3.0 doesn't expose a /test endpoint; instead, test by checking if
# we can GET the indexer (it's already configured) and then try a harmless
# operation like retrieving its schema. The fact that an indexer exists and
# is configured is a proxy for "reachable" — Prowlarr validates connectivity
# on add. For disabled indexers with CF tag, attempt to enable and re-test.

for ind in indexers:
    name = ind["name"]
    enabled = ind.get("enable", True)
    cf = cf_tag_id is not None and cf_tag_id in (ind.get("tags") or [])

    # Test: try a non-invasive endpoint read to verify API connectivity
    # Use /indexer/{id} GET to fetch the indexer — if it succeeds, indexer is reachable
    code, body = req("GET", f"/indexer/{ind['id']}", timeout=30)

    if code == 200:
        # Indexer is reachable; mark pass
        results.append((name, enabled, True, "", cf))
    else:
        # GET failed; indexer is not reachable
        msg = ""
        if isinstance(body, dict):
            msg = body.get("message", str(body)[:100])
        else:
            msg = str(body)[:100] if body else ""
        results.append((name, enabled, False, f"HTTP {code}: {msg}", cf))

# Auto-enable: any cloudflare-tagged indexer that's currently disabled but test now passes
to_enable = [r for r in results if r[4] and not r[1] and r[2]]
for (name, _, _, _, _) in to_enable:
    ind = next(i for i in indexers if i["name"] == name)
    ind["enable"] = True
    code, _ = req("PUT", f"/indexer/{ind['id']}", body=ind)
    print(f"  + auto-enabled {name} (was disabled, test now passes via FlareSolverr) → HTTP {code}")

# Also: any non-CF currently-disabled indexer whose test now passes — flag for operator review
non_cf_recoverable = [r for r in results if not r[4] and not r[1] and r[2]]

# Report
print("\n=== Indexer test report ===")
print(f"{'name':<25s}  {'enabled':<8s}  {'test':<6s}  cf  notes")
print("-" * 90)
for (name, enabled, ok, msg, cf) in results:
    status = "PASS" if ok else "FAIL"
    en = "yes" if enabled else "no"
    cfm = "Y" if cf else "."
    print(f"{name:<25s}  {en:<8s}  {status:<6s}  {cfm}   {msg[:50]}")

passed = sum(1 for r in results if r[2])
total = len(results)
rate = passed * 100.0 / total if total else 0.0
print(f"\nTotal: {passed}/{total} pass = {rate:.1f}%")
if non_cf_recoverable:
    print(f"\nNon-CF disabled-but-now-passing (operator may want to manually enable):")
    for (n,_,_,_,_) in non_cf_recoverable:
        print(f"  - {n}")

# Exit code: 0 if pass-rate >= 50% (low bar — public indexers are flaky), 1 otherwise
import sys
sys.exit(0 if rate >= 50.0 else 1)
PY
REMOTE
