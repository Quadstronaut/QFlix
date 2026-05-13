#!/usr/bin/env python3
"""scripts/mcp/logs.py — tail named app logs in structured form.

Modes: --emit-json | --cron
Args:  --app <slug>|all  --since <duration>  --tail <n>

Routes per app class:
  - UCC apps with ~/.apps/<slug>/logs/  → tail canonical log file
  - systemd-class apps                  → journalctl --user -u <unit>
  - Docker UCC apps                     → ~/.apps/<slug>/logs/ if present
  - nginx                               → ~/.apps/nginx/logs/{access,error}.log
  - Maint pipeline                      → journalctl --user -u manitoba-maint-*

Output: list of {ts, level, message, source_file}.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()

# slug → routing plan
_FILE_LOGS = {
    "sonarr":          str(HOME / ".apps/sonarr/logs/sonarr.txt"),
    "sonarr2":         str(HOME / ".apps/sonarr2/logs/sonarr.txt"),
    "radarr":          str(HOME / ".apps/radarr/logs/radarr.txt"),
    "radarr2":         str(HOME / ".apps/radarr2/logs/radarr.txt"),
    "prowlarr":        str(HOME / ".apps/prowlarr/logs/prowlarr.txt"),
    "bazarr":          str(HOME / ".apps/bazarr/data/log/bazarr.log"),
    "bazarr2":         str(HOME / ".apps/bazarr2/data/log/bazarr.log"),
    "tautulli":        str(HOME / ".apps/tautulli/logs/tautulli.log"),
    "maintainerr":     str(HOME / ".apps/maintainerr/logs/main.log"),
    "seerr":           str(HOME / ".apps/seerr/logs/overseerr.log"),
    "qbittorrent":     str(HOME / ".apps/qbittorrent/data/qBittorrent/logs/qbittorrent.log"),
    "homarr":          str(HOME / ".apps/homarr/logs/homarr.log"),
    "kometa":          str(HOME / ".apps/kometa/logs/meta.log"),
    "buildarr":        str(HOME / ".apps/buildarr/logs/buildarr.log"),
    "recyclarr":       str(HOME / ".apps/recyclarr/logs/recyclarr.log"),
    "nginx":           str(HOME / ".apps/nginx/logs/error.log"),
}

_SYSTEMD_LOGS = {
    "listmonk":      "listmonk.service",
    "tdarr-server":  "tdarr-server.service",
    "tdarr-node":    "tdarr-node.service",
    "maint-pusher":  "manitoba-maint-pusher.service",
    "maint-webhook": "manitoba-maint-webhook.service",
    "maint-window":  "manitoba-maint-window.service",
}

_TS_PATTERNS = [
    re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[Z+\-:0-9]*)\s*\[?(?P<lvl>[A-Z][a-zA-Z]+)?\]?\s*(?P<msg>.*)$"),
    re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[,\.]\d+)\s+(?P<lvl>[A-Z]+)\s+(?P<msg>.*)$"),
]


def route(app: str) -> dict:
    if app in _FILE_LOGS:
        return {"kind": "file", "path": _FILE_LOGS[app]}
    if app in _SYSTEMD_LOGS:
        return {"kind": "journalctl", "unit": _SYSTEMD_LOGS[app]}
    return {"kind": "unsupported", "app": app}


def parse_line(line: str, *, source: str) -> dict:
    line = line.rstrip("\n")
    for pat in _TS_PATTERNS:
        m = pat.match(line)
        if m:
            return {
                "ts": m.group("ts"),
                "level": (m.group("lvl") or "unknown"),
                "message": m.group("msg"),
                "source_file": source,
            }
    return {"ts": None, "level": "unknown", "message": line, "source_file": source}


def _tail_file(path: str, n: int) -> list[str]:
    if not os.path.exists(path):
        return []
    from collections import deque
    with open(path, encoding="utf-8", errors="ignore") as f:
        return list(deque(f, maxlen=n))


def _journalctl(unit: str, since: str, n: int) -> list[str]:
    cmd = ["journalctl", "--user", "-u", unit, "--since", f"{since} ago",
           "-n", str(n), "--output", "short-iso", "--no-pager"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return []
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def collect_for(app: str, *, since: str, tail: int) -> dict:
    plan = route(app)
    if plan["kind"] == "file":
        lines = _tail_file(plan["path"], tail)
        source = plan["path"]
    elif plan["kind"] == "journalctl":
        lines = _journalctl(plan["unit"], since, tail)
        source = f"journalctl:{plan['unit']}"
    else:
        return {"app": app, "error": "unsupported", "lines": []}
    return {
        "app": app,
        "source": source,
        "lines": [parse_line(line, source=source) for line in lines if line.strip()],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--cron", action="store_true")
    ap.add_argument("--app", required=True, help="slug or 'all'")
    ap.add_argument("--since", default="24h")
    ap.add_argument("--tail", type=int, default=5000)
    args = ap.parse_args()

    if args.app == "all":
        apps = list(_FILE_LOGS) + list(_SYSTEMD_LOGS)
        result = {a: collect_for(a, since=args.since, tail=args.tail) for a in apps}
    else:
        result = collect_for(args.app, since=args.since, tail=args.tail)

    if args.emit_json:
        json.dump(result, sys.stdout, default=str)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
