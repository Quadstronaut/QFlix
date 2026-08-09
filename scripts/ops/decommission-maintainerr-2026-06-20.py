#!/usr/bin/env python3
"""One-off (2026-06-20): retire Maintainerr in Uptime Kuma and stand up the
qflix-reaper monitor.

Maintainerr's autodelete was unfixable on Plex (see docs/maintainerr-plex-id-
resolution-bug.md); qflix-reaper replaces it. This deletes the three now-dead
Kuma PUSH monitors ("Maintainerr", "Canary Deletion", "Canary Maintainerr Rule
Sanity") so they don't dead-man-page, and creates the "QFlix Reaper" PUSH
monitor the reaper self-pushes to each run.

Run from the workstation with a tunnel open to Kuma:
    ssh -fN -L 42005:127.0.0.1:42005 quadstronaut@<seedbox-ssh-host>
    tests/.venv/Scripts/python.exe scripts/ops/decommission-maintainerr-2026-06-20.py

Prints PUSHTOKEN=<token> on success; the caller writes it into the seedbox
~/secrets/kuma-push-tokens.json under the key "qflix-reaper" (the key the reaper
reads). Idempotent: deletes are skip-if-absent; the reaper monitor is reused if
it already exists. Modeled on scripts/maint/bootstrap-kuma-monitors.py.
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
KUMA = "http://127.0.0.1:42005"
DELETE = ["Maintainerr", "Canary Deletion", "Canary Maintainerr Rule Sanity"]
CREATE = "QFlix Reaper"
# Reaper runs daily; Kuma flips a PUSH monitor DOWN if no ping inside `interval`.
# 25h gives a daily run a full-day-plus-buffer window.
REAPER_HEARTBEAT_S = 90000


def secret(name):
    return (REPO / "secrets" / name).read_text().strip()


def main():
    try:
        from uptime_kuma_api import UptimeKumaApi, MonitorType
    except ImportError:
        print("ERROR: uptime-kuma-api not installed in this venv", file=sys.stderr)
        return 2

    api = UptimeKumaApi(KUMA)
    logged_in = False
    for pw_name in ("htpasswd.password", "shared-admin.password"):
        try:
            api.login("quadstronaut", secret(pw_name))
            logged_in = True
            print("login ok via secrets/" + pw_name)
            break
        except Exception as _exc:
            sys.stderr.write("decommission-maintainerr-2026-06-20.py: maintainerr login probe failed (best-effort, continuing): "
                             + repr(_exc) + "\n")
    if not logged_in:
        print("FATAL: all logins failed", file=sys.stderr)
        api.disconnect()
        return 3

    monitors = {m["name"]: m for m in api.get_monitors()}

    for name in DELETE:
        m = monitors.get(name)
        if m:
            try:
                api.delete_monitor(m["id"])
                print("deleted monitor: " + name + " (id=" + str(m["id"]) + ")")
            except Exception as exc:
                print("delete FAILED " + name + ": " + str(exc))
        else:
            print("absent (ok): " + name)

    token = ""
    if CREATE in monitors:
        token = monitors[CREATE].get("pushToken", "") or ""
        print("reaper monitor already exists; reusing token")
    else:
        from uptime_kuma_api.api import _convert_monitor_input, _check_arguments_monitor
        from uptime_kuma_api.event import Event
        data = api._build_monitor_data(
            type=MonitorType.PUSH, name=CREATE,
            interval=REAPER_HEARTBEAT_S, maxretries=0,
        )
        data["conditions"] = []          # Kuma 2.3.x requires this; api 1.2.1 omits it
        _convert_monitor_input(data)
        _check_arguments_monitor(data)
        with api.wait_for_event(Event.MONITOR_LIST):
            api._call("add", data)
        time.sleep(2)
        for m in api.get_monitors():
            if m["name"] == CREATE:
                token = m.get("pushToken", "") or ""
                break
        print("created monitor: " + CREATE + " (heartbeat=" + str(REAPER_HEARTBEAT_S) + "s)")

    api.disconnect()
    if not token:
        print("WARN: no push token captured for " + CREATE, file=sys.stderr)
        return 1
    print("PUSHTOKEN=" + token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
