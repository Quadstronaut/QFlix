#!/usr/bin/env python3
"""QFLX-42 - push the Plex re-pin campaign to live Listmonk as a DRAFT.

DRY-RUN BY DEFAULT: prints what it would create and touches nothing.
`--execute` creates the campaign in DRAFT. This script can never send:
it has no status-change call. Sending is an operator click in the Listmonk UI
(preview + test-send to the operator first).

Content comes from scripts/qflix-newsletter/campaigns/plex-repin.{html,txt}
(HTML body + plain-text alt body, Listmonk {{ .Subscriber.FirstName }} tags).
Idempotent: an existing campaign with the same name is left alone.
Rollback: delete the draft campaign in the Listmonk UI.

Usage (workstation via the Listmonk tunnel, or on the box):
    python3 62-listmonk-repin-campaign.py                  # dry run
    python3 62-listmonk-repin-campaign.py --execute        # create the draft
Env: LISTMONK_URL (default http://127.0.0.1:<~/secrets/listmonk.port>/api),
     creds in ~/secrets/listmonk.api_user + listmonk.api_token.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CAMPAIGN_NAME = "Plex re-pin 2026-10"
CAMPAIGN_SUBJECT = "QFlix is moving - a quick re-pin in Plex"
FROM_EMAIL = "QFlix <operator@example.com>"
DEFAULT_LIST_ID = 3  # "all members", same as 60-listmonk-cutover.py
CAMPAIGN_DIR = Path(__file__).resolve().parent.parent / "qflix-newsletter" / "campaigns"


def secret(name: str) -> str:
    return Path(os.path.expanduser(f"~/secrets/{name}")).read_text().strip()


def lm_url() -> str:
    return os.environ.get("LISTMONK_URL") or f"http://127.0.0.1:{secret('listmonk.port')}/api"


def lm_req(path: str, method: str = "GET", body: dict | None = None):
    tok = base64.b64encode(
        f"{secret('listmonk.api_user')}:{secret('listmonk.api_token')}".encode()).decode()
    headers = {"Authorization": f"Basic {tok}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(lm_url() + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read().decode()
            return resp.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def build_payload(list_id: int = DEFAULT_LIST_ID) -> dict:
    """Campaign create body. type=regular, never scheduled: stays a draft."""
    return {
        "name": CAMPAIGN_NAME,
        "subject": CAMPAIGN_SUBJECT,
        "lists": [list_id],
        "from_email": FROM_EMAIL,
        "content_type": "html",
        "messenger": "email",
        "type": "regular",
        "body": (CAMPAIGN_DIR / "plex-repin.html").read_text(encoding="utf-8"),
        "altbody": (CAMPAIGN_DIR / "plex-repin.txt").read_text(encoding="utf-8"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--execute", action="store_true",
                    help="actually create the DRAFT campaign (default: dry run)")
    ap.add_argument("--list-id", type=int, default=DEFAULT_LIST_ID)
    args = ap.parse_args(argv)
    payload = build_payload(args.list_id)

    if not args.execute:
        print(f"[dry-run] would create DRAFT campaign '{CAMPAIGN_NAME}' "
              f"(list {args.list_id}, html {len(payload['body'])}B, "
              f"text {len(payload['altbody'])}B). Re-run with --execute.")
        return 0

    code, resp = lm_req(f"/campaigns?query={urllib.parse.quote(CAMPAIGN_NAME)}")
    if code != 200:
        raise SystemExit(f"campaigns GET failed: {code}: {resp!r}")
    for c in (resp.get("data", {}) or {}).get("results", []) or []:
        if c.get("name") == CAMPAIGN_NAME:
            print(f"[skip] campaign already exists (id={c.get('id')})")
            return 0
    code, resp = lm_req("/campaigns", "POST", payload)
    if code != 200:
        raise SystemExit(f"campaign create failed: {code}: {resp!r}")
    cid = (resp.get("data") or {}).get("id")
    print(f"[create] DRAFT campaign '{CAMPAIGN_NAME}' id={cid}. Preview + test-send "
          "to the operator in the Listmonk UI; sending is a manual click.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
