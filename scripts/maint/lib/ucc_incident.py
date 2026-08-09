"""lib/ucc_incident.py — Pin/unpin a public Kuma status-page incident.

Used by lib/ucc_response.py on clear→active and active→clear edges.
Mirrors the proven pattern from scripts/ops/tautulli-gate-watch.sh:
uses uptime_kuma_api raw socket emit (slug "public") which persists
server-side without triggering the buggy whole-status-page re-save on
this Kuma version.

OPEN ITEM (live verification post-merge): The exact socket events for
*pinning* an incident need confirmation on the live Kuma version.
  - ``postIncident`` creates the incident and returns an id.
  - Whether a separate ``pinIncident`` call is required depends on the
    Kuma version. The code calls both; if ``pinIncident`` is unneeded it
    is a harmless no-op (the incident is already visible after post).
  - ``unpinIncident`` is proven by the tautulli-gate-watch.sh watcher.
Unit tests mock the socket, so they remain valid regardless of what the
live API requires.

Secrets read (all under ~/secrets/):
  uptimekuma.port    — required (used to build http://127.0.0.1:<port>)
  uptimekuma.password— required (login password; user is 'quadstronaut')
"""
from __future__ import annotations

import sys
from typing import Optional


def _kuma_host() -> str:
    try:
        from lib.secrets import read_secret
        port = read_secret("uptimekuma.port")
        return f"http://127.0.0.1:{port}"
    except Exception as _exc:
        sys.stderr.write("ucc_incident.py: kuma port/key read failed (best-effort, continuing): "
                         + repr(_exc) + "\n")
    return "http://127.0.0.1:3001"


def _kuma_password() -> Optional[str]:
    try:
        from lib.secrets import read_secret
        return read_secret("uptimekuma.password")
    except Exception:
        return None


def pin_maintenance_incident() -> bool:
    """Pin a public maintenance incident on the Kuma status page.

    Best-effort — logs to stderr on failure, never raises.
    Returns True on success, False on any error.
    """
    try:
        from uptime_kuma_api import UptimeKumaApi  # type: ignore[import]

        host = _kuma_host()
        password = _kuma_password()
        if not password:
            print("WARNING: ucc_incident: uptimekuma.password secret missing; skipping incident pin",
                  file=sys.stderr)
            return False

        api = UptimeKumaApi(host)
        try:
            api.login("quadstronaut", password)

            # Post the incident (returns the new incident dict or raises).
            incident = api._call("postIncident", "public", {
                "title": "Upstream provider maintenance in progress",
                "content": (
                    "Our hosting provider is performing maintenance. "
                    "Some monitoring checks may appear degraded. "
                    "Plex and your media requests should continue to work normally."
                ),
                "style": "warning",
            })

            # Pin it if the API returned an id (separate pinIncident call
            # may or may not be required depending on Kuma version — we try
            # it; if it raises the incident is already visible).
            incident_id = None
            if isinstance(incident, dict):
                incident_id = incident.get("id")
            elif hasattr(incident, "id"):
                incident_id = incident.id

            if incident_id is not None:
                try:
                    api._call("pinIncident", "public", incident_id)
                except Exception as pin_exc:
                    # Not fatal — incident is already posted; pin may not be
                    # needed on this Kuma version.
                    print(f"INFO: ucc_incident: pinIncident call: {pin_exc} "
                          f"(may be a no-op on this Kuma version)",
                          file=sys.stderr)

            return True
        finally:
            try:
                api.disconnect()
            except Exception as _exc:
                sys.stderr.write("ucc_incident.py: incident state read failed (best-effort, continuing): "
                                 + repr(_exc) + "\n")

    except Exception as exc:
        print(f"WARNING: ucc_incident.pin_maintenance_incident failed: {exc}", file=sys.stderr)
        return False


def clear_maintenance_incident() -> bool:
    """Unpin (clear) the public maintenance incident on the Kuma status page.

    Mirrors the ``unpinIncident`` call proven by tautulli-gate-watch.sh.
    Best-effort — logs to stderr on failure, never raises.
    Returns True on success, False on any error.
    """
    try:
        from uptime_kuma_api import UptimeKumaApi  # type: ignore[import]

        host = _kuma_host()
        password = _kuma_password()
        if not password:
            print("WARNING: ucc_incident: uptimekuma.password secret missing; skipping incident clear",
                  file=sys.stderr)
            return False

        api = UptimeKumaApi(host)
        try:
            api.login("quadstronaut", password)
            api._call("unpinIncident", "public")
            return True
        finally:
            try:
                api.disconnect()
            except Exception as _exc:
                sys.stderr.write("ucc_incident.py: incident state write failed (best-effort, continuing): "
                                 + repr(_exc) + "\n")

    except Exception as exc:
        print(f"WARNING: ucc_incident.clear_maintenance_incident failed: {exc}", file=sys.stderr)
        return False
