#!/usr/bin/env python3
"""Phase 24 — Daily Listmonk -> Newsletterr recipient sync.

Idempotent. Runs on the seedbox via cron at 04:15.

Pulls all enabled subscribers from Listmonk's "All Members" list and writes
them as a comma-separated string into Newsletterr's email_lists table under
the name "Manitoba (auto)". Newsletterr's UI shows this list as a regular
selectable recipient list — operators just pick it when scheduling sends.

Removal flow: when a user clicks Listmonk's unsubscribe link, they're
removed from "All Members". Next 04:15 run rewrites the comma-separated
string without them — single unsubscribe path, both products honor it.

Schema-critical guard: if email_lists table is missing or its schema
diverges, exit non-zero. Manual schema migration required.
"""
import base64
import json
import os
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
SECRETS = os.path.join(HOME, "secrets")
LM_HOST = "https://quadstronaut.seedbox.example.com/listmonk"
NL_DB = os.path.join(HOME, ".apps/newsletterr/repo/database/data.db")
LIST_NAME_NL = "Manitoba (auto)"
LIST_NAME_LM = "All Members"
SSL_CTX = ssl.create_default_context()


def s(name):
    with open(os.path.join(SECRETS, name)) as f:
        return f.read().strip()


def lm_auth():
    return "Basic " + base64.b64encode(f"{s('listmonk.api_user')}:{s('listmonk.api_token')}".encode()).decode()


def lm_get(path):
    req = urllib.request.Request(f"{LM_HOST}{path}", headers={"Authorization": lm_auth()})
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=20) as r:
        return json.loads(r.read())


def get_all_members_id():
    res = lm_get("/api/lists?per_page=all")["data"]["results"]
    for L in res:
        if L["name"] == LIST_NAME_LM:
            return int(L["id"])
    raise SystemExit(f"FATAL: Listmonk list {LIST_NAME_LM!r} not found")


def fetch_emails(list_id):
    out = []
    page = 1
    while True:
        d = lm_get(f"/api/subscribers?list_id={list_id}&per_page=200&page={page}")["data"]
        for sub in d.get("results", []):
            if sub.get("status") == "enabled":
                out.append(sub["email"])
        total = int(d.get("total", 0))
        if page * 200 >= total or not d.get("results"):
            break
        page += 1
    return out


def upsert_newsletterr_list(emails):
    if not os.path.exists(NL_DB):
        print(f"FATAL: Newsletterr DB not found at {NL_DB}", file=sys.stderr)
        sys.exit(2)
    con = sqlite3.connect(NL_DB)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='email_lists'")
    if not cur.fetchone():
        print("FATAL: Newsletterr schema changed — email_lists table missing", file=sys.stderr)
        sys.exit(2)
    csv = ", ".join(sorted(set(e.strip() for e in emails if e and "@" in e)))
    cur.execute(
        "INSERT INTO email_lists (name, emails) VALUES (?, ?) "
        "ON CONFLICT (name) DO UPDATE SET emails = excluded.emails",
        (LIST_NAME_NL, csv),
    )
    con.commit()
    con.close()
    return len(csv.split(", ")) if csv else 0


def main() -> int:
    list_id = get_all_members_id()
    emails = fetch_emails(list_id)
    n = upsert_newsletterr_list(emails)
    print(f"sync: {n} emails -> Newsletterr list {LIST_NAME_NL!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
