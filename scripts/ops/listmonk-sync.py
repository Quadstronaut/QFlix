#!/usr/bin/env python3
"""Nightly reconcile: Plex friends + Seerr users -> Listmonk Subscribers.

Single-list model. Every user lands in the canonical Subscribers list
(id from ~/secrets/listmonk.list_id); the originating source is recorded
on the subscriber via attribs.source ("plex" | "seerr"). Idempotent —
never removes subscribers (they self-unsubscribe via Listmonk link).

Secrets live in ~/secrets/<name> on the seedbox (one file per secret,
gitignored in the source repo). Logs to stderr (cron tees to
~/.apps/listmonk/logs/sync.log).
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
SSL_CTX = ssl.create_default_context()


def s(name):
    with open(os.path.join(SECRETS, name)) as f:
        return f.read().strip()


def _maybe(name, default):
    try:
        return s(name)
    except FileNotFoundError:
        return default


LM_HOST = f"https://{_maybe('seedbox.host', 'quadstronaut.seedbox.example.com')}/listmonk"


def lm_auth():
    return "Basic " + base64.b64encode(f"{s('listmonk.api_user')}:{s('listmonk.api_token')}".encode()).decode()


def lm_req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{LM_HOST}{path}", method=method, data=data,
                                  headers={"Content-Type": "application/json", "Authorization": lm_auth()})
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


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


def upsert(email, name, list_id, source, existing_map):
    body = {
        "email": email,
        "name": name,
        "status": "enabled",
        "lists": [list_id],
        "preconfirm_subscriptions": True,
        "attribs": {"source": source},
    }
    existing = existing_map.get(email.lower())
    if existing:
        sid = int(existing["id"])
        existing_lists = {int(L["id"]) for L in existing.get("lists") or []}
        body["lists"] = sorted(existing_lists | {list_id})
        # Preserve existing attribs but record source if absent or different
        existing_attribs = existing.get("attribs") or {}
        if existing_attribs.get("source") != source:
            body["attribs"] = {**existing_attribs, "source": source}
        else:
            body["attribs"] = existing_attribs
        try:
            lm_req("PUT", f"/api/subscribers/{sid}", body)
            return "updated"
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return "noop"
            raise
    try:
        resp = lm_req("POST", "/api/subscribers", body)
    except urllib.error.HTTPError as e:
        # 409 = subscriber already exists. Happens for a brand-new user who
        # is in BOTH sources (Plex friend + Seerr user): the first source
        # creates them, then the second source — working off the start-of-run
        # snapshot that predates the create — tries to POST again. Benign; the
        # subscriber is already on the list, so treat as a no-op.
        if e.code == 409:
            return "noop"
        raise
    # Record the freshly-created subscriber in the snapshot so the second
    # source takes the update path instead of re-POSTing (which 409s above).
    created = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(created, dict) and created.get("id"):
        existing_map[email.lower()] = created
    return "created"


def fetch_plex_friends():
    """Plex.tv /api/v2/friends — currently shared friends with email when available.
    Home accounts have no email; skip them.
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
    list_id = int(s("listmonk.list_id"))
    existing = fetch_subscribers_by_email()
    print(f"snapshot: {len(existing)} existing subscribers (target list_id={list_id})", file=sys.stderr)

    deltas = {"created": 0, "updated": 0, "noop": 0, "errors": 0}

    sources = [
        ("plex", fetch_plex_friends),
        ("seerr", fetch_seerr_users),
    ]

    for tag, fetcher in sources:
        try:
            users = fetcher()
        except Exception as e:
            print(f"  ! {tag} fetch failed: {e}", file=sys.stderr)
            deltas["errors"] += 1
            continue
        print(f"  {tag}: {len(users)} users with email", file=sys.stderr)
        for email, name in users:
            try:
                action = upsert(email, name, list_id, tag, existing)
                deltas[action] = deltas.get(action, 0) + 1
            except Exception as e:
                print(f"  ! {tag} upsert {email}: {e}", file=sys.stderr)
                deltas["errors"] += 1

    print(f"sync done: {deltas}", file=sys.stderr)
    return 0 if deltas["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
