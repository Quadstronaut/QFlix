#!/usr/bin/env python3
"""Phase 9.3: Configure Maintainerr — Plex, Seerr, 4 *arrs.

Run on manitoba; reads MT_KEY, HTPW_USER, HTPW_PASS, MT_URL plus secret values from env.
Maintainerr's API is gated by both htpasswd (Ultra.cc nginx) and X-Api-Key.

Idempotent.
"""
import base64, json, os, socket, sys, urllib.request, urllib.error

MT_URL = os.environ["MT_URL"]
MT_KEY = os.environ["MT_KEY"]
HTPW_USER = os.environ["HTPW_USER"]
HTPW_PASS = os.environ["HTPW_PASS"]

basic = base64.b64encode(f"{HTPW_USER}:{HTPW_PASS}".encode()).decode()

def req(path, method="GET", body=None, timeout=60):
    h = {
        "Content-Type": "application/json",
        "X-Api-Key": MT_KEY,
        "Authorization": f"Basic {basic}",
    }
    d = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(MT_URL + path, data=d, method=method, headers=h)
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=ctx) as resp:
            t = resp.read().decode()
            return resp.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        b = e.read().decode()
        try: parsed = json.loads(b)
        except Exception: parsed = b
        return e.code, parsed
    except (socket.timeout, urllib.error.URLError) as e:
        return 0, str(e)

# 1. POST /api/settings with the full shape (Plex + Seerr)
print("=== 1. Main settings (Plex + Seerr) ===")
code, current = req("/api/settings")
if code != 200:
    print(f"  ! Cannot GET /api/settings: HTTP {code} {current}")
    sys.exit(1)

current["plex_name"] = "manitoba"
current["plex_hostname"] = os.environ["PLEX_HOST"]
current["plex_port"] = int(os.environ["PLEX_PORT"])
current["plex_ssl"] = 0
current["plex_auth_token"] = os.environ["PLEX_TOKEN"]
current["media_server_type"] = 1  # 1 = Plex
current["seerr_url"] = f"http://172.17.0.1:{os.environ['JS_PORT']}"
current["seerr_api_key"] = os.environ["JS_KEY"]

code, resp = req("/api/settings", method="POST", body=current)
print(f"  POST /api/settings ->HTTP {code}" + ("" if code in (200, 201, 204) else f" {str(resp)[:200]}"))

# 2. Sonarr instances (Sonarr + Sonarr2)
print("\n=== 2. Sonarr instances (general + anime) ===")
def upsert_sonarr(label, port, base, key):
    body = {
        "serverName": label,
        "url": f"http://172.17.0.1:{port}/{base}",
        "apiKey": key,
    }
    code, existing = req("/api/settings/sonarr")
    found = next((x for x in (existing or []) if x.get("serverName") == label), None)
    if found:
        body["id"] = found["id"]
        code, resp = req(f"/api/settings/sonarr/{found['id']}", method="PUT", body=body)
        verb = "updated"
    else:
        code, resp = req("/api/settings/sonarr", method="POST", body=body)
        verb = "added"
    if code in (200, 201, 204):
        print(f"  {verb} {label}")
    else:
        print(f"  ! {label} failed: HTTP {code} {str(resp)[:200]}")

upsert_sonarr("Sonarr",  os.environ["SONARR_PORT"],  os.environ.get("SONARR_BASE", "sonarr"),  os.environ["SONARR_KEY"])
upsert_sonarr("Sonarr2", os.environ["SONARR2_PORT"], os.environ.get("SONARR2_BASE", "sonarr2"), os.environ["SONARR2_KEY"])

# 3. Radarr instances
print("\n=== 3. Radarr instances ===")
def upsert_radarr(label, port, base, key):
    body = {"serverName": label, "url": f"http://172.17.0.1:{port}/{base}", "apiKey": key}
    code, existing = req("/api/settings/radarr")
    found = next((x for x in (existing or []) if x.get("serverName") == label), None)
    if found:
        body["id"] = found["id"]
        code, resp = req(f"/api/settings/radarr/{found['id']}", method="PUT", body=body)
        verb = "updated"
    else:
        code, resp = req("/api/settings/radarr", method="POST", body=body)
        verb = "added"
    if code in (200, 201, 204):
        print(f"  {verb} {label}")
    else:
        print(f"  ! {label} failed: HTTP {code} {str(resp)[:200]}")

upsert_radarr("Radarr",  os.environ["RADARR_PORT"],  os.environ.get("RADARR_BASE", "radarr"),  os.environ["RADARR_KEY"])
upsert_radarr("Radarr2", os.environ["RADARR2_PORT"], os.environ.get("RADARR2_BASE", "radarr2"), os.environ["RADARR2_KEY"])

# 4. Tests
print("\n=== 4. Connection tests ===")
for kind in ("plex", "overseerr"):
    code, resp = req(f"/api/settings/test/{kind}", method="POST", body={})
    status = (resp or {}).get("status") if isinstance(resp, dict) else "?"
    msg = (resp or {}).get("message", "") if isinstance(resp, dict) else ""
    print(f"  {kind:10s} ->HTTP {code} status={status} {msg[:80]}")

# Final: list configured instances
print("\n=== Final: configured instances ===")
for path, label in [("/api/settings/sonarr", "sonarr"), ("/api/settings/radarr", "radarr")]:
    code, items = req(path)
    if isinstance(items, list):
        for i in items:
            print(f"  {label}: id={i.get('id')} serverName={i.get('serverName')} url={i.get('url','?')}")
