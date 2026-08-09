"""One-shot audit: query Kuma over the tunnel, dump each monitor's live state
(status, last heartbeat age, msg). Run with the same venv used by
bootstrap-kuma-monitors.py. Tunnel must already be open on 42005."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
USER = "quadstronaut"
KUMA_URL = os.environ.get("KUMA_URL", "http://127.0.0.1:42005")


def main() -> int:
    # Force UTF-8 stdout so msg fields with → ✓ etc. don't blow up cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception as _exc:
        sys.stderr.write("audit-kuma-state.py: stdout encoding setup failed (best-effort, continuing): "
                         + repr(_exc) + "\n")
    from uptime_kuma_api import UptimeKumaApi
    pw = (REPO_ROOT / "secrets" / "htpasswd.password").read_text().strip()

    api = UptimeKumaApi(KUMA_URL)
    api.login(USER, pw)

    monitors = api.get_monitors()
    now = int(time.time())

    print(f"{'STATE':<6} {'AGE':>7} {'NAME':<38} MSG")
    print("-" * 110)

    rows = []
    for m in monitors:
        mid = m["id"]
        name = m.get("name", "?")
        beats = api.get_monitor_beats(mid, 1)
        if not beats:
            rows.append(("?", -1, name, "no-heartbeats-yet"))
            continue
        last = beats[-1]
        status = last.get("status")
        # Kuma status: 0=down 1=up 2=pending 3=maintenance
        status_str = {0: "DOWN", 1: "UP", 2: "PEND", 3: "MAINT"}.get(int(status) if status is not None else -1, f"?{status}")
        ts = last.get("time", "")
        age = -1
        try:
            from datetime import datetime
            # Kuma 2.x uses "YYYY-MM-DD HH:MM:SS.fff" (UTC)
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(ts, fmt)
                    age = now - int(dt.timestamp())
                    break
                except Exception as _exc:
                    sys.stderr.write("audit-kuma-state.py: heartbeat timestamp parse failed (best-effort, continuing): "
                                     + repr(_exc) + "\n")
            if age < 0:
                # Try ISO format too
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age = now - int(dt.timestamp())
        except Exception:
            age = -1
        # Strip newlines / control so the table stays one row per monitor
        msg = (last.get("msg") or "").replace("\n", "  ").replace("\r", "").strip()[:80]
        rows.append((status_str, age, name, msg))

    # Sort: DOWN first, then PEND, then by name
    order = {"DOWN": 0, "PEND": 1, "?": 2, "UP": 3, "MAINT": 4}
    rows.sort(key=lambda r: (order.get(r[0], 9), r[2]))

    for state, age, name, msg in rows:
        age_str = f"{age}s" if age >= 0 else "?"
        print(f"{state:<6} {age_str:>7} {name:<38} {msg}")

    print("-" * 110)
    by_state = {}
    for state, _, _, _ in rows:
        by_state[state] = by_state.get(state, 0) + 1
    print("totals:", {k: by_state[k] for k in sorted(by_state)})
    api.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
