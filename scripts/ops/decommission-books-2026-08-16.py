#!/usr/bin/env python3
"""One-off (2026-08-16): retire the books stack in Uptime Kuma.

Audiobookshelf, Kavita, Komga and Calibre-Web were installed 2026-05-08 by
scripts/install/15-bulk-books-comics.sh and never used — every backing media
directory was still empty at retirement. The operator ordered all four purged.

This deletes their four now-dead Kuma monitors so they don't dead-man-page once
the apps are uninstalled. Nothing replaces them, so unlike the maintainerr
decommission there is no monitor to create and no push token to hand back.

ORDER MATTERS. Run this only AFTER the four apps have been dropped from
manifest/apps.yaml and the deployed copy pushed + `manitoba-maint-pusher`
restarted. The pusher loads the manifest once at startup, so a still-running
pusher would keep pushing "up" into monitors this script just deleted. Doing it
the other way round — uninstall first, monitors last — is what produced the
31-alert Homarr storm on 2026-07-13.

Run from the workstation with a tunnel open to Kuma:
    ssh -fN -L 42005:127.0.0.1:42005 quadstronaut@<seedbox-ssh-host>
    tests/.venv/Scripts/python.exe scripts/ops/decommission-books-2026-08-16.py

Idempotent: every delete is skip-if-absent, so a re-run on an already-clean Kuma
exits 0 and reports each name as "absent (ok)". Modeled on
scripts/ops/decommission-maintainerr-2026-06-20.py.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
KUMA = "http://127.0.0.1:42005"
DELETE = ["Audiobookshelf", "Kavita", "Komga", "Calibre-Web"]


def secret(name):
    return (REPO / "secrets" / name).read_text().strip()


def main():
    try:
        from uptime_kuma_api import UptimeKumaApi
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
            sys.stderr.write(
                "decommission-books-2026-08-16.py: login probe failed "
                "(best-effort, continuing): " + repr(_exc) + "\n")
    if not logged_in:
        print("FATAL: all logins failed", file=sys.stderr)
        api.disconnect()
        return 3

    monitors = {m["name"]: m for m in api.get_monitors()}

    failed = 0
    for name in DELETE:
        m = monitors.get(name)
        if m:
            try:
                api.delete_monitor(m["id"])
                print("deleted monitor: " + name + " (id=" + str(m["id"]) + ")")
            except Exception as exc:
                print("delete FAILED " + name + ": " + str(exc))
                failed += 1
        else:
            print("absent (ok): " + name)

    api.disconnect()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
