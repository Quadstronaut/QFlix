#!/usr/bin/env python3
"""Phase 20.1 — One-shot Listmonk bootstrap from Ombi cohort.

Idempotent. Re-running won't dupe lists or subscribers.

Creates four lists (All Members, Ombi imports, Plex friends, Jellyseerr requesters)
and seeds 13 Ombi users into All Members + Ombi imports. Plex/Jellyseerr/Jellyfin
reconcile is handled by the nightly cron in scripts/ops/listmonk-sync.py.
"""
import base64
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "..", "secrets"))


def s(name: str) -> str:
    with open(os.path.join(SECRETS_DIR, name), "r") as f:
        return f.read().strip()


LM_HOST = "https://quadstronaut.seedbox.example.com/listmonk"
# Listmonk v6 API endpoints require an api-type user + token. The legacy
# admin_username/admin_password from config.toml authenticates a "config"
# pseudo-user that has no list_role_id, so list operations are denied.
# secrets/listmonk.api_user + listmonk.api_token are minted by the
# installer and bound to the Manitoba List Access list-role (role id=9).
API_USER = s("listmonk.api_user")
API_TOKEN = s("listmonk.api_token")
AUTH_HEADER = "Basic " + base64.b64encode(f"{API_USER}:{API_TOKEN}".encode()).decode()
SSL_CTX = ssl.create_default_context()


def lm_req(method: str, path: str, body=None):
    url = f"{LM_HOST}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Authorization": AUTH_HEADER}
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def get_or_create_list(name: str, tags: list[str]) -> int:
    lists = lm_req("GET", "/api/lists?per_page=all")["data"]["results"]
    for L in lists:
        if L["name"] == name:
            return int(L["id"])
    body = {
        "name": name,
        "type": "private",
        "optin": "single",
        "tags": tags,
        "description": f"Auto-created by Manitoba bootstrap: {name}",
    }
    return int(lm_req("POST", "/api/lists", body)["data"]["id"])


def fetch_subscribers_by_email() -> dict:
    """Walk all subscribers (subscribers:get_all perm) and build email->subscriber map.

    Avoids the subscribers:sql_query path which HTTP Basic auth doesn't seem to
    activate even though the Super Admin role includes that permission.
    """
    out = {}
    page = 1
    while True:
        res = lm_req("GET", f"/api/subscribers?per_page=200&page={page}")["data"]
        for sub in res.get("results", []):
            out[sub["email"].lower()] = sub
        if page * 200 >= int(res.get("total", 0)):
            break
        page += 1
    return out


def upsert_subscriber(existing_map: dict, email: str, name: str, list_ids: list[int], attribs: dict):
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
        return lm_req("PUT", f"/api/subscribers/{sid}", body), "updated"
    return lm_req("POST", "/api/subscribers", body), "created"


def fetch_ombi_users() -> list[tuple[str, str]]:
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "quadstronaut@seedbox.example.com",
        "sqlite3 -separator '|' ~/.apps/ombi/Ombi.db "
        "\"SELECT UserName, Email FROM AspNetUsers "
        "WHERE Email IS NOT NULL AND Email != '' AND UserName != 'Api'\"",
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
    return [tuple(line.split("|", 1)) for line in out.splitlines() if "|" in line]


def main() -> int:
    print("-> creating/finding lists...")
    main_list = get_or_create_list("All Members", ["all"])
    ombi_list = get_or_create_list("Ombi imports (legacy)", ["ombi", "legacy"])
    get_or_create_list("Plex friends", ["plex"])
    get_or_create_list("Jellyseerr requesters", ["jellyseerr"])
    get_or_create_list("Jellyfin users", ["jellyfin"])
    print(f"  All Members id={main_list}, Ombi imports id={ombi_list}")

    print("-> snapshotting existing subscribers...")
    existing_map = fetch_subscribers_by_email()
    print(f"  {len(existing_map)} existing subscriber(s)")

    print("-> pulling Ombi users via SSH+sqlite3...")
    users = fetch_ombi_users()
    print(f"  {len(users)} users")

    created = updated = 0
    for username, email in users:
        try:
            _, action = upsert_subscriber(
                existing_map,
                email,
                username,
                [main_list, ombi_list],
                attribs={"source": "ombi", "ombi_username": username},
            )
            if action == "created":
                created += 1
            else:
                updated += 1
        except urllib.error.HTTPError as e:
            print(f"  ! {email}: HTTP {e.code} {e.reason}", file=sys.stderr)
            print(f"    body: {e.read().decode()[:200]}", file=sys.stderr)
            return 1
    print(f"[OK] bootstrap: {created} created, {updated} updated, {len(users)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
