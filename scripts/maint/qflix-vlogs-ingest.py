#!/usr/bin/env python3
"""qflix-vlogs-ingest.py — pull all managed app logs into local VictoriaLogs.

Runs on the seedbox as a systemd-user oneshot fired by qflix-vlogs-ingest.timer
every 5 minutes. Imports scripts/mcp/logs.py directly (in-process) and POSTs
to 127.0.0.1:<vlogs.port>/insert/jsonline.

Output: one stdout line per app with line count, or "skip" if no new content.
Exit 0 always (ingest is best-effort; per-app errors are logged, not fatal).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# logs.py lives at scripts/mcp/logs.py on the seedbox (~/scripts/mcp/logs.py).
# This script lives at scripts/maint/qflix-vlogs-ingest.py (~/scripts/maint/).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "mcp"))

import logs as logs_mod  # noqa: E402


def _read_port() -> int:
    port_file = Path("~/secrets/vlogs.port").expanduser()
    return int(port_file.read_text().strip())


def _post_jsonline(port: int, app: str, lines: list[dict]) -> tuple[bool, str]:
    """POST JSON-line batch to vlogs. Returns (ok, detail)."""
    if not lines:
        return True, "0 lines"

    payload_lines = []
    for ln in lines:
        msg = ln.get("message")
        if not msg:
            continue
        payload_lines.append(json.dumps({
            "_msg":        msg,
            "_time":       ln.get("ts") or "",
            "level":       ln.get("level") or "unknown",
            "app":         app,
            "source_file": ln.get("source_file") or "",
            "host":        "seedbox",
        }))
    if not payload_lines:
        return True, "0 non-empty"

    body = ("\n".join(payload_lines)).encode("utf-8")
    qs = urllib.parse.urlencode({
        "_stream_fields": "host,app",
        "_time_field":    "_time",
        "_msg_field":     "_msg",
    })
    url = f"http://127.0.0.1:{port}/insert/jsonline?{qs}"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/stream+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return True, f"{len(payload_lines)} lines"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", default="6m",
                    help="journalctl-style 'since' (default 6m — overlaps 5min timer)")
    ap.add_argument("--tail", type=int, default=5000,
                    help="max lines per app per cycle")
    args = ap.parse_args()

    try:
        port = _read_port()
    except (FileNotFoundError, ValueError) as exc:
        print(f"FATAL: cannot read vlogs port: {exc}", file=sys.stderr)
        return 0  # don't fail the timer

    apps = list(logs_mod._FILE_LOGS) + list(logs_mod._SYSTEMD_LOGS)
    total_lines = 0
    failures = 0

    for app in apps:
        try:
            result = logs_mod.collect_for(app, since=args.window, tail=args.tail)
        except Exception as exc:
            print(f"{app}: collect-failed {exc}")
            failures += 1
            continue
        lines = result.get("lines") or []
        ok, detail = _post_jsonline(port, app, lines)
        if not ok:
            print(f"{app}: post-failed {detail}")
            failures += 1
            continue
        n = int(detail.split()[0]) if detail and detail.split()[0].isdigit() else 0
        total_lines += n
        if n > 0:
            print(f"{app}: {detail}")

    print(f"summary: apps={len(apps)} lines_indexed={total_lines} failures={failures}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
