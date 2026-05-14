#!/usr/bin/env python3
"""scripts/configure/72-mcp-workstation-kuma-monitor.py

Adds ONE Kuma push monitor — "QFlix Collect (workstation)" — that doesn't
belong in manifest/apps.yaml (because it's a workstation-side dead-man, not
a seedbox-managed app).

Runs the same socket.io flow as bootstrap-kuma-monitors.py but for one
monitor name only. Idempotent: skips if already present. Updates
secrets/kuma-push-tokens.json in place (merges with existing keys).

Threshold = 5400s (90 min) — workstation pushes hourly so one missed run
is tolerated, two consecutive misses = red.

Prereq: SSH tunnel to Kuma admin port (42005) must be open.

Usage:
  PYTHONPATH=scripts/maint tests/.venv/Scripts/python.exe \\
      scripts/configure/72-mcp-workstation-kuma-monitor.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "maint"))

KUMA_URL = os.environ.get("KUMA_URL", "http://127.0.0.1:42005")
USER = "quadstronaut"
MONITOR_NAME = "QFlix Collect (workstation)"
INTERVAL_S = 5400  # 90 min: tolerate one missed hourly run, red on two


def _read_secret(name: str) -> str:
    return (REPO_ROOT / "secrets" / name).read_text().strip()


def _login(api, candidates):
    last_err = None
    for pw_name, pw in candidates:
        try:
            api.login(USER, pw)
            return pw_name
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"all logins failed; last error: {last_err}")


def _add_push_monitor(api, name: str, interval: int) -> str:
    """Create a Kuma PUSH monitor — same body bootstrap-kuma-monitors.py uses,
    so we know it works against the live Kuma 2.3.x version."""
    from uptime_kuma_api.api import _convert_monitor_input, _check_arguments_monitor
    from uptime_kuma_api.event import Event
    from uptime_kuma_api import MonitorType

    data = api._build_monitor_data(
        type=MonitorType.PUSH,
        name=name,
        interval=interval,
        maxretries=0,
    )
    data["conditions"] = []
    _convert_monitor_input(data)
    _check_arguments_monitor(data)
    with api.wait_for_event(Event.MONITOR_LIST):
        api._call("add", data)
    for m in api.get_monitors():
        if m["name"] == name and str(m.get("type", "")).lower().endswith("push"):
            return m.get("pushToken", "")
    return ""


def main() -> int:
    try:
        from uptime_kuma_api import UptimeKumaApi
    except ImportError:
        print("ERROR: uptime-kuma-api not installed. pip install uptime-kuma-api",
              file=sys.stderr)
        return 2

    candidates = []
    for pw_name in ("htpasswd.password", "shared-admin.password"):
        try:
            candidates.append((pw_name, _read_secret(pw_name)))
        except FileNotFoundError:
            pass
    if not candidates:
        print("ERROR: no candidate passwords", file=sys.stderr)
        return 2

    api = UptimeKumaApi(KUMA_URL)
    try:
        used = _login(api, candidates)
        print(f"login ok: user={USER} via secrets/{used}")
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        api.disconnect()
        return 3

    existing = {m["name"]: m for m in api.get_monitors()}
    if MONITOR_NAME in existing:
        m = existing[MONITOR_NAME]
        token = m.get("pushToken", "")
        print(f"OK: {MONITOR_NAME} already exists (id={m.get('id')}, "
              f"token={'present' if token else 'MISSING'})")
    else:
        token = _add_push_monitor(api, MONITOR_NAME, INTERVAL_S)
        print(f"ADDED: {MONITOR_NAME} (interval={INTERVAL_S}s)")

    api.disconnect()

    # Merge token into existing kuma-push-tokens.json
    if not token:
        print(f"warn: no pushToken yet; re-run after a few seconds for token capture",
              file=sys.stderr)
        return 1
    tokens_file = REPO_ROOT / "secrets" / "kuma-push-tokens.json"
    try:
        tokens = json.loads(tokens_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        tokens = {}
    tokens[MONITOR_NAME] = token
    tokens_file.write_text(json.dumps(tokens, indent=2, sort_keys=True))
    try:
        os.chmod(tokens_file, 0o600)
    except OSError:
        pass
    print(f"wrote token to secrets/kuma-push-tokens.json (now {len(tokens)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
