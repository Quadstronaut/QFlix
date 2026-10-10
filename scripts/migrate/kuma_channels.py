#!/usr/bin/env python3
"""kuma_channels.py -- mute or un-mute ONE box's Kuma human channels (I-1).

Invariant I-1: exactly one side pages a human at any moment. On each box, a
Kuma monitor pages a human through every DEFAULT notification channel except
the auto-heal webhook (which POSTs to that box's own maint daemon, not to a
person, so it stays attached on a muted side and auto-heal keeps working).

Runs ON the box, fed over ssh stdin, so nothing has to be deployed first:

    ssh <box> "python3 - status" < scripts/migrate/kuma_channels.py
    ssh <box> "python3 - mute"   < scripts/migrate/kuma_channels.py
    ssh <box> "python3 - loud"   < scripts/migrate/kuma_channels.py

status prints one JSON object: reachable, monitors, human_ids,
monitors_with_human, monitors_without_human. mute/loud are idempotent and end
with the same JSON (re-read after the edit, never trusted from the write).

Kuma on the box: port from ~/secrets/uptimekuma.port, login quadstronaut with
the first readable of ~/secrets/{htpasswd,shared-admin}.password (the same
pair bootstrap-kuma-monitors.py tries). Exit 0 ok, 1 edit failed, 2 Kuma
unreachable / client library missing (could-not-assert).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable, List, Set

AUTOHEAL = "Manitoba auto-heal webhook"
USER = "quadstronaut"


def human_ids(notifications: Iterable[dict]) -> Set[int]:
    """Default channels that reach a person: every isDefault one but auto-heal."""
    return {int(n["id"]) for n in notifications
            if n.get("isDefault") and n.get("name") != AUTOHEAL}


def attached(monitor: dict) -> Set[int]:
    raw = monitor.get("notificationIDList") or {}
    if isinstance(raw, dict):
        return {int(k) for k, v in raw.items() if v}
    return {int(x) for x in raw}


def plan(monitors: Iterable[dict], humans: Set[int], action: str) -> List[tuple]:
    """[(monitor_id, new_id_set)] for monitors that need an edit."""
    out = []
    for m in monitors:
        cur = attached(m)
        new = (cur - humans) if action == "mute" else (cur | humans)
        if new != cur:
            out.append((m["id"], new))
    return out


def summary(monitors: List[dict], humans: Set[int]) -> dict:
    with_h = sum(1 for m in monitors if attached(m) & humans)
    return {"reachable": True, "monitors": len(monitors), "human_ids": sorted(humans),
            "monitors_with_human": with_h, "monitors_without_human": len(monitors) - with_h}


def _secret(name: str) -> str:
    return (Path.home() / "secrets" / name).read_text(encoding="utf-8").strip()


def main(argv: List[str]) -> int:
    action = argv[0] if argv else "status"
    if action not in ("status", "mute", "loud"):
        sys.stderr.write("STAGE=usage msg=kuma_channels-action-must-be-status|mute|loud\n")
        return 2
    try:
        from uptime_kuma_api import UptimeKumaApi
    except ImportError:
        print(json.dumps({"reachable": False, "error": "uptime_kuma_api-missing"}))
        return 2
    try:
        port = _secret("uptimekuma.port")
    except OSError:
        print(json.dumps({"reachable": False, "error": "no-uptimekuma.port-secret"}))
        return 2
    api = None
    try:
        api = UptimeKumaApi("http://127.0.0.1:%s" % port)
        logged = False
        for pw in ("htpasswd.password", "shared-admin.password"):
            try:
                api.login(USER, _secret(pw))
                logged = True
                break
            except Exception as exc:  # wrong/missing password: try the next one
                sys.stderr.write("kuma login with %s failed: %s\n" % (pw, type(exc).__name__))
        if not logged:
            print(json.dumps({"reachable": False, "error": "login-failed"}))
            return 2
        humans = human_ids(api.get_notifications())
        if action != "status":
            for mid, ids in plan(api.get_monitors(), humans, action):
                api.edit_monitor(mid, notificationIDList={str(i): True for i in sorted(ids)})
        print(json.dumps(summary(api.get_monitors(), humans)))
        return 0
    except Exception as exc:
        print(json.dumps({"reachable": False, "error": "%s:%s" % (type(exc).__name__, exc)}))
        return 1 if action != "status" else 2
    finally:
        if api is not None:
            try:
                api.disconnect()
            except Exception as exc:
                sys.stderr.write("kuma disconnect: %s\n" % exc)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
