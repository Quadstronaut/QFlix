#!/usr/bin/env python3
"""Add a Tautulli Webhook notification agent that posts to Notifiarr's
generic-passthrough endpoint, triggering on Recently Added + Watched.

Newer Tautulli versions removed Notifiarr from the native agent list, so
we use the Webhook agent (id=22 in Tautulli's agent registry).

Idempotent: if a Notifiarr-passthrough Webhook agent already exists,
this script updates its triggers/URL rather than creating a duplicate.

Run on the seedbox:  python3 58-tautulli-notifiarr-webhook.py
Or pipe via SSH:    sshm "python3 -" < 58-tautulli-notifiarr-webhook.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


WEBHOOK_AGENT_ID = 22  # Tautulli internal agent_id for Webhook

# Triggers (from Tautulli's notify_actions.json):
#   on_play=1, on_stop=2, on_pause=3, on_resume=4, on_buffer=5,
#   on_change=14, on_watched=6, on_intdown=7, on_extdown=8, on_intup=9,
#   on_extup=10, on_pmsupdate=11, on_concurrent=12, on_newdevice=13,
#   on_created=15  (Recently Added)
DESIRED_TRIGGERS = {
    "on_created": 1,   # Recently Added
    "on_watched": 1,   # Watched
}


def secret(name: str) -> str:
    with open(os.path.expanduser(f"~/secrets/{name}")) as f:
        return f.read().strip()


def secret_or(name: str, fallback: str) -> str:
    try:
        return secret(name)
    except FileNotFoundError:
        return fallback


def tautulli_get(cmd: str, **params) -> dict:
    key = secret("tautulli.key")
    port = secret("tautulli.port")
    base = secret_or("tautulli.urlbase", "tautulli").strip("/")
    qs = {"apikey": key, "cmd": cmd, **params}
    url = f"http://127.0.0.1:{port}/{base}/api/v2?{urllib.parse.urlencode(qs)}"
    return json.loads(urllib.request.urlopen(url, timeout=15).read())


def tautulli_post(cmd: str, params: dict) -> dict:
    key = secret("tautulli.key")
    port = secret("tautulli.port")
    base = secret_or("tautulli.urlbase", "tautulli").strip("/")
    body = {"apikey": key, "cmd": cmd, **params}
    data = urllib.parse.urlencode(body).encode("utf-8")
    url = f"http://127.0.0.1:{port}/{base}/api/v2"
    return json.loads(urllib.request.urlopen(url, data=data, timeout=15).read())


def find_existing_notifier() -> int | None:
    notifiers = tautulli_get("get_notifiers").get("response", {}).get("data", [])
    for n in notifiers:
        if n.get("agent_id") == WEBHOOK_AGENT_ID and \
           "notifiarr" in (n.get("friendly_name") or "").lower():
            return n.get("id")
    return None


def configure_notifier(notifier_id: int, webhook_url: str) -> None:
    # set_notifier_config keys are flat. Triggers are integer 0/1.
    config = {
        "notifier_id": notifier_id,
        "agent_id": WEBHOOK_AGENT_ID,
        "friendly_name": "Notifiarr passthrough",
        "webhook_hook": webhook_url,
        "webhook_method": "POST",
        # Triggers
        **{k: v for k, v in DESIRED_TRIGGERS.items()},
    }
    r = tautulli_post("set_notifier_config", config)
    if r.get("response", {}).get("result") != "success":
        raise SystemExit(f"set_notifier_config failed: {r}")


def main() -> int:
    notifiarr_key = secret("notifiarr.key")
    webhook_url = (
        f"https://notifiarr.com/api/v1/notification/passthrough/{notifiarr_key}"
    )

    nid = find_existing_notifier()
    if nid is None:
        # Create empty Webhook notifier — POST add_notifier_config
        r = tautulli_post("add_notifier_config", {"agent_id": WEBHOOK_AGENT_ID})
        if r.get("response", {}).get("result") != "success":
            print(f"add_notifier_config failed: {r}", file=sys.stderr)
            return 1
        nid_after = find_existing_notifier()
        # find_existing_notifier matches by friendly_name=notifiarr — won't
        # match yet; pick the newest webhook agent without a friendly_name.
        if nid_after is None:
            notifiers = tautulli_get("get_notifiers").get(
                "response", {}).get("data", [])
            webhooks = [
                n for n in notifiers
                if n.get("agent_id") == WEBHOOK_AGENT_ID
            ]
            if not webhooks:
                print("could not find newly-created webhook notifier",
                      file=sys.stderr)
                return 1
            nid = max(n.get("id") for n in webhooks)
        else:
            nid = nid_after
        action = "created"
    else:
        action = "updated"

    configure_notifier(nid, webhook_url)
    print(f"Tautulli notifier {action}: id={nid} → "
          f"Notifiarr passthrough (Recently Added + Watched)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
