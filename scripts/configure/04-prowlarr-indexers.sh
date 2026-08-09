#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

PROW_KEY="$(secret_read prowlarr.key)"
PROW_PORT="$(secret_read prowlarr.port)"
PROW_BASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"
MANIFEST="$HERE/data/prowlarr-indexers.json"

log_info "Pushing indexer manifest to manitoba..."
scpm_to "$MANIFEST" "/tmp/prowlarr-indexers.json"

log_info "Adding Prowlarr indexers (port=$PROW_PORT base=$PROW_BASE)..."

sshm bash -s "$PROW_KEY" "$PROW_PORT" "$PROW_BASE" <<'REMOTE'
set -euo pipefail
KEY="$1"; PORT="$2"; BASE="$3"
URL="http://127.0.0.1:$PORT/$BASE/api/v1"

python3 <<PY
import json, sys, urllib.request, urllib.error

KEY = "$KEY"
URL = "$URL"

def req(method, path, body=None):
    headers = {"X-Api-Key": KEY, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            txt = resp.read().decode()
            return resp.status, json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

with open("/tmp/prowlarr-indexers.json") as f:
    manifest = json.load(f)

general = manifest["general"]
anime = manifest["anime"]
needs_fs = set(manifest["needs_flaresolverr"])

# 1. Ensure tags exist (idempotent)
def ensure_tag(label):
    code, tags = req("GET", "/tag")
    for t in tags:
        if t["label"] == label:
            return t["id"]
    code, t = req("POST", "/tag", {"label": label})
    print(f"  + created tag {label!r} (id={t['id']})")
    return t["id"]

tag_anime = ensure_tag("anime")
tag_cloudflare = ensure_tag("cloudflare")
print(f"Tags: anime={tag_anime}  cloudflare={tag_cloudflare}")

# 2. Find FlareSolverr proxy id
code, proxies = req("GET", "/indexerProxy")
fs_proxy = next((p for p in proxies if p.get("implementation") == "FlareSolverr"), None)
fs_id = fs_proxy["id"] if fs_proxy is not None else 0
print(f"FlareSolverr proxy id = {fs_id}")
if fs_id == 0:
    print("  ! WARNING: FlareSolverr proxy not found — cloudflare indexers will be added without proxy")

# Prowlarr routes via tag matching: proxy must carry tag_cloudflare to auto-apply to tagged indexers.
if fs_proxy is not None and tag_cloudflare not in fs_proxy.get("tags", []):
    fs_proxy["tags"] = list(set(fs_proxy.get("tags", []) + [tag_cloudflare]))
    code2, _ = req("PUT", "/indexerProxy/" + str(fs_id), fs_proxy)
    print(f"  + assigned cloudflare tag to FlareSolverr proxy (HTTP {code2})")

# 3. Fetch all available schemas
code, schemas = req("GET", "/indexer/schema")
if not isinstance(schemas, list):
    print(f"FATAL: /indexer/schema returned HTTP {code}: {str(schemas)[:300]}")
    sys.exit(1)

schema_by_name = {}
for s in schemas:
    for k in ("name", "definitionName"):
        val = (s.get(k) or "").strip()
        if val:
            schema_by_name[val.lower()] = s
print(f"Loaded {len(schemas)} indexer schemas")

# 4. Fetch existing indexers for idempotency check
code, existing = req("GET", "/indexer")
existing_names = {ind["name"].strip().lower() for ind in existing}
print(f"Existing indexers: {len(existing_names)}")

added = []
skipped = []
failed = []
missing_schema = []

def add_indexer(name, kind):
    key = name.strip().lower()
    if key in existing_names:
        skipped.append(f"{name} (exists)")
        return

    schema = schema_by_name.get(key)
    if not schema:
        # Fuzzy match: strip spaces, dots, hyphens for comparison
        def normalize(s):
            return s.lower().replace(" ", "").replace(".", "").replace("-", "").replace("_", "")
        norm_name = normalize(name)
        candidates = [
            s for s in schemas
            if norm_name in normalize(s.get("name", "") + " " + s.get("definitionName", ""))
        ]
        if candidates:
            schema = candidates[0]
            matched = schema.get("name") or schema.get("definitionName", "?")
            print(f"  ~ fuzzy-matched {name!r} -> {matched!r}")
        else:
            missing_schema.append(name)
            print(f"  ? schema not found: {name!r}")
            return

    body = json.loads(json.dumps(schema))
    body["name"] = name
    body["enable"] = True

    tags = []
    proxy_id = 0
    if kind == "anime":
        tags.append(tag_anime)
    if name in needs_fs:
        tags.append(tag_cloudflare)
        proxy_id = fs_id

    body["appProfileId"] = 1  # "Standard" profile (id=1, always present)
    body["tags"] = tags
    body["indexerProxyId"] = proxy_id

    def is_connectivity_error(resp_body):
        if isinstance(resp_body, list):
            return any(
                "unable to connect" in (e.get("errorMessage","") or "").lower() or
                "blocked by cloudflare" in (e.get("errorMessage","") or "").lower() or
                "unexpected response" in (e.get("errorMessage","") or "").lower() or
                "redirected to" in (e.get("errorMessage","") or "").lower()
                for e in resp_body
            )
        if isinstance(resp_body, str):
            lo = resp_body.lower()
            return "unable to connect" in lo or "blocked by cloudflare" in lo or "redirected to" in lo
        return False

    code, resp = req("POST", "/indexer", body)
    if code in (200, 201):
        added.append(f"{name} (id={resp['id']}, tags={tags}, proxy={proxy_id})")
    elif code == 400 and is_connectivity_error(resp if isinstance(resp, list) else (json.loads(resp) if isinstance(resp, str) and resp.strip().startswith("[") else resp)):
        # Prowlarr tests connectivity on add; retry disabled so it skips the live check
        print(f"  ~ {name}: connectivity check failed, retrying with enable=False")
        body["enable"] = False
        code2, resp2 = req("POST", "/indexer", body)
        if code2 in (200, 201):
            added.append(f"{name} (id={resp2['id']}, tags={tags}, proxy={proxy_id}, enabled=False)")
        else:
            msg2 = str(resp2)[:200] if not isinstance(resp2, str) else resp2[:200]
            failed.append(f"{name}: HTTP {code2}: {msg2}")
            print(f"  ! FAILED {name}: HTTP {code2}: {msg2}")
    else:
        msg = str(resp)[:300] if not isinstance(resp, str) else resp[:300]
        failed.append(f"{name}: HTTP {code}: {msg}")
        print(f"  ! FAILED {name}: HTTP {code}: {msg}")

for n in general:
    add_indexer(n, "general")
for n in anime:
    add_indexer(n, "anime")

print()
print(f"=== Summary: {len(added)} added, {len(skipped)} skipped, {len(missing_schema)} schema-missing, {len(failed)} failed")
for a in added:    print(f"  + {a}")
for s in skipped:  print(f"  - {s}")
for m in missing_schema: print(f"  ? {m}")
for f in failed:   print(f"  ! {f}")

if len(added) == 0 and len(skipped) == 0:
    print("\nFATAL: 0 added and 0 skipped — Prowlarr API may be rejecting all schemas.")
    sys.exit(2)

# 5. Final list
code, final = req("GET", "/indexer")
print(f"\n=== Total indexers in Prowlarr now: {len(final)}")
for ind in sorted(final, key=lambda x: x["name"].lower()):
    print(f"  {ind['id']:3d}  {ind['name']:30s}  tags={ind.get('tags',[])}  proxy={ind.get('indexerProxyId',0)}")
PY
REMOTE
