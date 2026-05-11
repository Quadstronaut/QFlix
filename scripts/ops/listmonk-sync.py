#!/usr/bin/env python3
"""Phase 20.2 — Nightly reconcile: Plex friends + Seerr -> Listmonk.

Runs on the seedbox via cron. Idempotent: never removes subscribers (operators
self-unsubscribe via Listmonk link). Adds new users to the appropriate source
list and All Members.

Secrets live in ~/secrets/<name> on the seedbox (one file per secret, gitignored
in the source repo and operator-deployed via the install script).

Logs to stderr (cron will tee to ~/.apps/listmonk/logs/sync.log).
"""
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

HOME = os.path.expanduser("~")
SECRETS = os.path.join(HOME, "secrets")
LM_HOST = "https://quadstronaut.seedbox.example.com/listmonk"
SSL_CTX = ssl.create_default_context()


def s(name):
    with open(os.path.join(SECRETS, name)) as f:
        return f.read().strip()


def lm_auth():
    return "Basic " + base64.b64encode(f"{s('listmonk.api_user')}:{s('listmonk.api_token')}".encode()).decode()


def lm_req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{LM_HOST}{path}", method=method, data=data,
                                  headers={"Content-Type": "application/json", "Authorization": lm_auth()})
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def get_list_id_by_name(name):
    res = lm_req("GET", "/api/lists?per_page=all")["data"]["results"]
    for L in res:
        if L["name"] == name:
            return int(L["id"])
    raise RuntimeError(f"list not found: {name!r}")


def fetch_subscribers_by_email():
    out = {}
    page = 1
    while True:
        res = lm_req("GET", f"/api/subscribers?per_page=200&page={page}")["data"]
        for sub in res.get("results", []):
            out[sub["email"].lower()] = sub
        total = int(res.get("total", 0))
        if page * 200 >= total or not res.get("results"):
            break
        page += 1
    return out


def upsert(email, name, list_ids, attribs, existing_map):
    body = {
        "email": email,
        "name": name,
        "status": "enabled",
        "lists": list_ids,
        "preconfirm_subscriptions": True,
        "attribs": attribs,
    }
    existing = existing_map.get(email.lower())
    if existing:
        sid = int(existing["id"])
        existing_lists = {int(L["id"]) for L in existing.get("lists") or []}
        body["lists"] = sorted(existing_lists | set(list_ids))
        try:
            lm_req("PUT", f"/api/subscribers/{sid}", body)
            return "updated"
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return "noop"
            raise
    lm_req("POST", "/api/subscribers", body)
    return "created"


def fetch_plex_friends():
    """Plex.tv /api/v2/friends — returns currently shared friends with email
    when available. Some friends ('home' accounts) have no email; skip them.
    """
    token = s("plex.token")
    req = urllib.request.Request(
        f"https://plex.tv/api/v2/friends?X-Plex-Token={token}",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"  ! plex.tv friends fetch failed: HTTP {e.code}", file=sys.stderr)
        return []
    out = []
    for f in data:
        email = (f.get("email") or "").strip()
        title = f.get("title") or f.get("username") or ""
        if email:
            out.append((email, title))
    return out


def fetch_seerr_users():
    key = s("seerr.key")
    port = s("seerr.port")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/user?take=1000",
        headers={"X-Api-Key": key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    out = []
    for u in data.get("results", []):
        email = (u.get("email") or "").strip()
        name = u.get("displayName") or u.get("username") or ""
        if email:
            out.append((email, name))
    return out


def main() -> int:
    main_id = get_list_id_by_name("All Members")
    plex_id = get_list_id_by_name("Plex friends")
    seerr_id = get_list_id_by_name("Seerr requesters")

    existing = fetch_subscribers_by_email()
    print(f"snapshot: {len(existing)} existing subscribers", file=sys.stderr)

    deltas = {"created": 0, "updated": 0, "noop": 0, "errors": 0}

    sources = [
        ("plex", fetch_plex_friends, plex_id),
        ("seerr", fetch_seerr_users, seerr_id),
    ]

    for tag, fetcher, list_id in sources:
        try:
            users = fetcher()
        except Exception as e:
            print(f"  ! {tag} fetch failed: {e}", file=sys.stderr)
            deltas["errors"] += 1
            continue
        print(f"  {tag}: {len(users)} users with email", file=sys.stderr)
        for email, name in users:
            try:
                action = upsert(email, name, [main_id, list_id], {"source": tag}, existing)
                deltas[action] = deltas.get(action, 0) + 1
            except Exception as e:
                print(f"  ! {tag} upsert {email}: {e}", file=sys.stderr)
                deltas["errors"] += 1

    print(f"sync done: {deltas}", file=sys.stderr)
    return 0 if deltas["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
