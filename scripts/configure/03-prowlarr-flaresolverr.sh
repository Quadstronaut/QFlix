#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

PROW_KEY="$(secret_read prowlarr.key)"
PROW_PORT="$(secret_read prowlarr.port)"
PROW_BASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"
FS_PORT="$(secret_read flaresolverr.port)"
FS_HOST="172.17.0.1"  # docker0 gateway — see Phase 2 finding
FS_URL="http://$FS_HOST:$FS_PORT/"

# Run all curl calls remotely on manitoba — Prowlarr is bound 127.0.0.1 there, no tunnel needed.
log_info "Adding/verifying FlareSolverr indexer proxy in Prowlarr ($FS_URL)..."

sshm bash -s "$PROW_KEY" "$PROW_PORT" "$PROW_BASE" "$FS_URL" <<'REMOTE'
set -euo pipefail
KEY="$1"; PORT="$2"; BASE="$3"; FS_URL="$4"
URL="http://127.0.0.1:$PORT/$BASE/api/v1"

# Idempotency check
existing="$(curl -sS -H "X-Api-Key: $KEY" "$URL/indexerProxy" | python3 -c 'import sys,json; d=json.load(sys.stdin); ids=[p["id"] for p in d if p.get("implementation")=="FlareSolverr"]; print(ids[0] if ids else "")')"
if [ -n "$existing" ]; then
  echo "FlareSolverr indexer proxy already exists (id=$existing) — skipping"
  exit 0
fi

# Fetch the schema for FlareSolverr to get the exact contract Prowlarr expects
schema="$(curl -sS -H "X-Api-Key: $KEY" "$URL/indexerProxy/schema" | python3 -c 'import sys,json; d=json.load(sys.stdin); fs=[s for s in d if s.get("implementation")=="FlareSolverr"]; import sys; sys.stdout.write(json.dumps(fs[0]) if fs else "")')"
[ -z "$schema" ] && { echo "Could not fetch FlareSolverr schema from Prowlarr"; exit 1; }

# Build the POST body: take the schema, override fields[host] + fields[requestTimeout]
body="$(python3 -c '
import sys, json
schema = json.loads(sys.argv[1])
fs_url = sys.argv[2]
schema["name"] = "FlareSolverr"
schema["tags"] = []
for f in schema["fields"]:
    if f["name"] == "host":
        f["value"] = fs_url
    elif f["name"] == "requestTimeout":
        f["value"] = 60
print(json.dumps(schema))
' "$schema" "$FS_URL")"

resp="$(curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/indexerProxy" -d "$body")"
echo "POST response (preview): $(printf '%s' "$resp" | head -c 300)"
new_id="$(printf '%s' "$resp" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("id",""))' 2>/dev/null || true)"
[ -z "$new_id" ] && { echo "Proxy creation failed; response above"; exit 1; }

# Verify by fetching it back
curl -sS -H "X-Api-Key: $KEY" "$URL/indexerProxy/$new_id" | python3 << 'VERIFYPY'
import sys, json
d = json.load(sys.stdin)
host_val = next((f.get("value", "") for f in d["fields"] if f["name"] == "host"), "?")
print(f"Created proxy id={d['id']} name={d['name']} impl={d['implementation']} host={host_val}")
VERIFYPY
REMOTE

log_info "Test the proxy by calling /test endpoint:"
sshm bash -s "$PROW_KEY" "$PROW_PORT" "$PROW_BASE" <<'REMOTE'
set -euo pipefail
KEY="$1"; PORT="$2"; BASE="$3"
URL="http://127.0.0.1:$PORT/$BASE/api/v1"
proxies="$(curl -sS -H "X-Api-Key: $KEY" "$URL/indexerProxy")"
fs_id="$(printf '%s' "$proxies" | python3 -c 'import sys,json; d=json.load(sys.stdin); ids=[p["id"] for p in d if p.get("implementation")=="FlareSolverr"]; print(ids[0])')"
fs_body="$(printf '%s' "$proxies" | python3 -c 'import sys,json; d=json.load(sys.stdin); fs=[p for p in d if p.get("implementation")=="FlareSolverr"][0]; print(json.dumps(fs))')"
test_resp="$(curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/indexerProxy/test" -d "$fs_body" -w '\n%{http_code}')"
echo "Test response (test endpoint returns either 200 with empty body for OK, or 400 with errors):"
echo "$test_resp"
REMOTE
