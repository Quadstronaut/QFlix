#!/usr/bin/env python3
"""Wire post-import rescan callbacks:
  Mylar3 (config.ini extra_scripts) -> library-rescan.sh komga,kavita
  Readarr (CustomScript Connect)   -> library-rescan.sh audiobookshelf
"""
import json, os, socket, urllib.request, urllib.error, subprocess

READARR_KEY = os.environ["READARR_KEY"]
READARR_URL = f"http://127.0.0.1:{os.environ['READARR_PORT']}/{os.environ.get('READARR_BASE','readarr')}/api/v1"
HELPER_PATH = "/home/quadstronaut/scripts/post-import/library-rescan.sh"

def req(url, method="GET", api_key=None, body=None, timeout=60):
    h = {"Content-Type": "application/json"}
    if api_key: h["X-Api-Key"] = api_key
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
    except Exception as e:
        return 0, str(e)

# --- 1. Readarr: add CustomScript Connects for audiobookshelf + calibre-web rescan ---
print("=== Readarr: CustomScript Connects ===")
code, schemas = req(f"{READARR_URL}/notification/schema", api_key=READARR_KEY)
cs_schema = next((s for s in schemas if s.get("implementation") == "CustomScript"), None)

code, notifs = req(f"{READARR_URL}/notification", api_key=READARR_KEY)

def upsert_custom_script(name, script_path):
    found = next((n for n in (notifs or []) if n.get("name") == name), None)
    body = json.loads(json.dumps(cs_schema))
    body["name"] = name
    body["onGrab"] = False
    body["onReleaseImport"] = True
    body["onUpgrade"] = True
    body["onRename"] = False
    body["onHealthIssue"] = False
    body["onApplicationUpdate"] = False
    body["tags"] = []
    for f in body.get("fields", []):
        if f["name"].lower() == "path":
            f["value"] = script_path
    if found:
        body["id"] = found["id"]
        code, resp = req(f"{READARR_URL}/notification/{found['id']}?forceSave=true", method="PUT", api_key=READARR_KEY, body=body)
        verb = "updated"
    else:
        code, resp = req(f"{READARR_URL}/notification?forceSave=true", method="POST", api_key=READARR_KEY, body=body)
        verb = "added"
    if code in (200, 201, 204):
        print(f"  {verb} CustomScript '{name}' (path={script_path})")
    else:
        print(f"  ! {name} failed: HTTP {code} {str(resp)[:200]}")

upsert_custom_script("Rescan Audiobookshelf", "/home/quadstronaut/scripts/post-import/library-rescan-audiobookshelf.sh")
upsert_custom_script("Rescan Calibre-Web",    "/home/quadstronaut/scripts/post-import/library-rescan-calibre-web.sh")

# Mylar3 patching is done by the calling shell wrapper (24-wire-rescan-callbacks.sh).
print("\n(Mylar3 extra_scripts patching handled by 24-wire-rescan-callbacks.sh shell wrapper.)")
