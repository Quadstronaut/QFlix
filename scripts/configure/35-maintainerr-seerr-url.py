#!/usr/bin/env python3
"""Pin Maintainerr's seerr_url to the docker0-gateway:42011 host loopback.

Run on the seedbox. Edits ~/.apps/maintainerr/maintainerr.sqlite directly so
the value sticks across restarts. Idempotent.

Why this exists: Maintainerr lives in a Docker container on the default
bridge network. To reach Seerr (also Dockerized) it must hit the host's
docker0 gateway 172.17.0.1, on Seerr's actual port 42011 — not the old
Jellyseerr port 17013 that the value drifted to before the 2026-05-11
Jellyseerr→Seerr migration. See [[ucc-docker-host-loopback]] and
inventory.md for the broader 127.0.0.1 vs 172.17.0.1 fix class.

Backs up the sqlite to .bak-<UTC-stamp> on every run.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DESIRED_SEERR_URL = "http://172.17.0.1:42011"

DB = Path(os.path.expanduser("~/.apps/maintainerr/maintainerr.sqlite"))


def main() -> int:
    if not DB.exists():
        print(f"NO-OP: {DB} not present (Maintainerr not installed?)")
        return 0

    with sqlite3.connect(str(DB)) as cx:
        row = cx.execute("SELECT id, seerr_url FROM settings LIMIT 1").fetchone()
        if row is None:
            print("NO-OP: settings table empty")
            return 0
        sid, current = row
        if current == DESIRED_SEERR_URL:
            print(f"OK: settings.id={sid} seerr_url already {DESIRED_SEERR_URL!r}")
            return 0

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = DB.with_suffix(DB.suffix + f".bak-{stamp}")
        shutil.copy2(DB, backup)
        print(f"backed up to {backup.name}")

        cx.execute(
            "UPDATE settings SET seerr_url=? WHERE id=?",
            (DESIRED_SEERR_URL, sid),
        )
        cx.commit()
        print(
            f"UPDATED: settings.id={sid} seerr_url {current!r} -> {DESIRED_SEERR_URL!r}"
        )

    print("restarting Maintainerr so the new value is read...")
    subprocess.run(["app-maintainerr", "restart"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
