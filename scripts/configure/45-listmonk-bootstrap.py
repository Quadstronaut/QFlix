#!/usr/bin/env python3
"""Listmonk first-run bootstrap — ensure the canonical Subscribers list exists.

Single-list model: every subscriber (Plex friends, Seerr users, manual adds)
lands in ONE list. Source is recorded on each subscriber via attribs.source.

This script is idempotent — safe to re-run. It:
  1. Finds or creates the list named "Subscribers".
  2. Writes its id to ~/secrets/listmonk.list_id (so qflix-newsletter +
     listmonk-sync.py both target it without name lookup).

Subscriber seeding lives in scripts/ops/listmonk-sync.py (cron 04:00 daily).
"""
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
SECRETS = os.path.join(HOME, "secrets")


def s(name: str) -> str:
    with open(os.path.join(SECRETS, name), "r") as f:
        return f.read().strip()


def _maybe(name, default):
    try:
        return s(name)
    except FileNotFoundError:
        return default


LM_HOST = f"https://{_maybe('seedbox.host', 'quadstronaut.seedbox.example.com')}/listmonk"
AUTH_HEADER = "Basic " + base64.b64encode(f"{s('listmonk.api_user')}:{s('listmonk.api_token')}".encode()).decode()
SSL_CTX = ssl.create_default_context()

LIST_NAME = "Subscribers"


def lm_req(method: str, path: str, body=None):
    url = f"{LM_HOST}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Authorization": AUTH_HEADER}
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def main() -> int:
    print(f"-> looking up list {LIST_NAME!r}...")
    lists = lm_req("GET", "/api/lists?per_page=all")["data"]["results"]
    target = next((L for L in lists if L["name"] == LIST_NAME), None)
    if target is None:
        body = {
            "name": LIST_NAME,
            "type": "private",
            "optin": "single",
            "tags": ["primary"],
            "description": "Single canonical subscribers list. Source distinguished via subscriber attribs.source.",
        }
        target = lm_req("POST", "/api/lists", body)["data"]
        print(f"   created list id={target['id']}")
    else:
        print(f"   found existing list id={target['id']} (subscriber_count={target.get('subscriber_count', '?')})")

    list_id = int(target["id"])
    secret_path = os.path.join(SECRETS, "listmonk.list_id")
    existing = _maybe("listmonk.list_id", "")
    if existing != str(list_id):
        with open(secret_path, "w") as f:
            f.write(str(list_id))
        os.chmod(secret_path, 0o600)
        print(f"   wrote {secret_path} = {list_id}")
    else:
        print(f"   {secret_path} already = {list_id}")

    print(f"[OK] Subscribers list ready (id={list_id}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
