#!/usr/bin/env python3
"""Authenticate as the Jellyfin admin user and create (or fetch) an API key
named 'OptimizeManitoba'. Writes it to /tmp/jf.key on the remote.

Run on manitoba; reads ADMIN_PASS, JF_PORT from env.
"""
import json, os, sys, urllib.request, urllib.error

ADMIN_PASS = os.environ["ADMIN_PASS"]
JF_PORT = os.environ.get("JF_PORT", "17002")
URL = f"http://127.0.0.1:{JF_PORT}/jellyfin"

def req(method, path, headers=None, body=None, timeout=30):
    h = {"Content-Type": "application/json"}
    if headers: h.update(headers)
    d = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(URL + path, data=d, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            t = resp.read().decode()
            return resp.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        b = e.read().decode()
        try: return e.code, json.loads(b)
        except: return e.code, b

# 1. Authenticate
auth_hdr = 'MediaBrowser Client="OptimizeManitoba", Device="controller", DeviceId="opt-mb-001", Version="1.0.0"'
code, resp = req("POST", "/Users/AuthenticateByName",
                 headers={"X-Emby-Authorization": auth_hdr},
                 body={"Username": "quadstronaut", "Pw": ADMIN_PASS})
if code != 200:
    print(f"AUTH FAILED: HTTP {code}: {str(resp)[:200]}", file=sys.stderr)
    sys.exit(1)
token = resp["AccessToken"]
user_id = resp["User"]["Id"]
print(f"authenticated user_id={user_id} token={token[:8]}...")

token_hdr = {"Authorization": f'MediaBrowser Token="{token}"'}

# 2. Check for existing OptimizeManitoba key
code, keys = req("GET", "/Auth/Keys", headers=token_hdr)
existing = None
for k in (keys or {}).get("Items", []):
    if k.get("AppName") == "OptimizeManitoba":
        existing = k["AccessToken"]
        break

if existing:
    print(f"existing OptimizeManitoba key: {existing[:8]}...")
    out_key = existing
else:
    code, _ = req("POST", "/Auth/Keys?app=OptimizeManitoba", headers=token_hdr)
    print(f"create key: HTTP {code}")
    code, keys = req("GET", "/Auth/Keys", headers=token_hdr)
    new_key = next((k["AccessToken"] for k in keys["Items"] if k.get("AppName") == "OptimizeManitoba"), None)
    if not new_key:
        print("FAILED to find newly-created key", file=sys.stderr)
        sys.exit(1)
    print(f"new key: {new_key[:8]}...")
    out_key = new_key

with open("/tmp/jf.key", "w") as f:
    f.write(out_key)
print(f"wrote /tmp/jf.key ({len(out_key)} chars)")
