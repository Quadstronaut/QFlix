#!/usr/bin/env python3
"""Phase 25 — Listmonk cutover campaign.

Idempotent: creates the cutover template + campaign in DRAFT state.
The actual mass-send is gated behind --send so that a stray script
re-run cannot accidentally re-spam 13 subscribers.

Body diverges from the original Task-25.2 plan in one place: the
"alerts" paragraph (#2) is removed because ntfy/alerts was dropped
on 2026-05-08 (Ultra.cc edge constraints). The remaining message
covers (1) dashboard + (3) weekly email.

Usage:
    python3 60-listmonk-cutover.py            # create draft
    python3 60-listmonk-cutover.py --send     # create draft + fire it
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


CAMPAIGN_NAME = "Cutover 2026-05"
CAMPAIGN_SUBJECT = "Manitoba Media — small update"
TEMPLATE_NAME = "Cutover 2026-05 — body"
ALL_MEMBERS_LIST_ID = 3

BODY = """Hi {{ .Subscriber.FirstName }},

We're tidying up the way you hear about new movies, shows, and books on Manitoba.
Two small things:

1. The dashboard you already use will keep showing what's new at the top:
   https://quadstronaut.seedbox.example.com/

2. You'll keep getting this kind of email about once a week. Nothing changes
   there. There's an Unsubscribe link at the bottom of every one if you'd
   rather not.

That's it. No action needed. Thanks for reading.

— Manitoba Media
"""


def secret(name: str) -> str:
    with open(os.path.expanduser(f"~/secrets/{name}")) as f:
        return f.read().strip()


def lm_url() -> str:
    port = secret("listmonk.port")
    return f"http://127.0.0.1:{port}/api"


def lm_auth() -> tuple[str, str]:
    return secret("listmonk.api_user"), secret("listmonk.api_token")


def _basic(user: str, pw: str) -> str:
    import base64
    return base64.b64encode(f"{user}:{pw}".encode()).decode()


def lm_req(path: str, method: str = "GET", body: dict | None = None) -> tuple[int, object]:
    user, pw = lm_auth()
    headers = {"Authorization": f"Basic {_basic(user, pw)}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        lm_url() + path, data=data, method=method, headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, body


def find_template_id() -> int | None:
    code, resp = lm_req("/templates")
    if code != 200:
        raise SystemExit(f"templates GET failed: {code}: {resp!r}")
    for t in resp.get("data", []) or []:
        if t.get("name") == TEMPLATE_NAME:
            return t.get("id")
    return None


def find_campaign_id() -> int | None:
    code, resp = lm_req(f"/campaigns?query={urllib.parse.quote(CAMPAIGN_NAME)}")
    if code != 200:
        raise SystemExit(f"campaigns GET failed: {code}: {resp!r}")
    for c in resp.get("data", {}).get("results", []) or []:
        if c.get("name") == CAMPAIGN_NAME:
            return c.get("id")
    return None


def ensure_template() -> int:
    tid = find_template_id()
    if tid is not None:
        print(f"[skip] template '{TEMPLATE_NAME}' already exists (id={tid})")
        return tid
    code, resp = lm_req("/templates", method="POST", body={
        "name": TEMPLATE_NAME,
        "type": "campaign",
        "subject": "",
        "body": "<html><body>{{ template \"content\" . }}</body></html>",
    })
    if code != 200:
        raise SystemExit(f"template create failed: {code}: {resp!r}")
    tid = resp.get("data", {}).get("id") or resp.get("data", {}).get("ID")
    if not tid:
        raise SystemExit(f"template create returned no id: {resp!r}")
    print(f"[create] template '{TEMPLATE_NAME}' id={tid}")
    return tid


def ensure_campaign(template_id: int) -> int:
    cid = find_campaign_id()
    if cid is not None:
        print(f"[skip] campaign '{CAMPAIGN_NAME}' already exists (id={cid})")
        return cid
    code, resp = lm_req("/campaigns", method="POST", body={
        "name": CAMPAIGN_NAME,
        "subject": CAMPAIGN_SUBJECT,
        "lists": [ALL_MEMBERS_LIST_ID],
        "from_email": "Manitoba Media <operator@example.com>",
        "content_type": "plain",
        "messenger": "email",
        "type": "regular",
        "template_id": template_id,
        "body": BODY,
    })
    if code != 200:
        raise SystemExit(f"campaign create failed: {code}: {resp!r}")
    cid = resp.get("data", {}).get("id") or resp.get("data", {}).get("ID")
    if not cid:
        raise SystemExit(f"campaign create returned no id: {resp!r}")
    print(f"[create] campaign '{CAMPAIGN_NAME}' id={cid} (status: draft)")
    return cid


def fire_campaign(cid: int) -> None:
    code, resp = lm_req(f"/campaigns/{cid}/status", method="PUT",
                        body={"status": "running"})
    if code != 200:
        raise SystemExit(f"campaign fire failed: {code}: {resp!r}")
    print(f"[send] campaign id={cid} → status=running")
    # Poll until finished or 60s
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        code, resp = lm_req(f"/campaigns/{cid}")
        if code == 200:
            status = resp.get("data", {}).get("status")
            print(f"  status: {status}")
            if status == "finished":
                print(f"[done] campaign delivered")
                return
        time.sleep(3)
    print(f"[warn] campaign still running after 60s — check Listmonk UI")


def main() -> int:
    send_mode = "--send" in sys.argv
    template_id = ensure_template()
    campaign_id = ensure_campaign(template_id)
    if send_mode:
        fire_campaign(campaign_id)
    else:
        print()
        print("Campaign created in DRAFT. To actually send to all 13 "
              "subscribers, re-run with --send.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
