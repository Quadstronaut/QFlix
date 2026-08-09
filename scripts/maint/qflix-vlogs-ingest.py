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
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# logs.py lives at scripts/mcp/logs.py on the seedbox (~/scripts/mcp/logs.py).
# This script lives at scripts/maint/qflix-vlogs-ingest.py (~/scripts/maint/).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "mcp"))

import logs as logs_mod  # noqa: E402


_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DEFAULT_WINDOW_S = 360  # 6 minutes; matches --window default


def _parse_window_seconds(window: str) -> int:
    """Convert a journalctl-style duration ('6m', '2h', '30s', '1d') to
    seconds. Falls back to _DEFAULT_WINDOW_S on garbage so a malformed
    arg never disables the dormant-file skip."""
    m = _DURATION_RE.match((window or "").strip())
    if not m:
        return _DEFAULT_WINDOW_S
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _file_is_dormant(path: str, *, max_age_s: int) -> bool:
    """True iff the file exists and hasn't been modified within max_age_s.

    Used to skip append-only logs (recyclarr weekly, kometa daily) whose
    last entries are days old. Without this, every 5-min ingest re-tails
    the last 5000 lines and re-publishes the same stale errors forever.
    Non-existent files return False — logs.collect_for handles them.
    """
    if not os.path.exists(path):
        return False
    try:
        return (time.time() - os.path.getmtime(path)) > max_age_s
    except OSError:
        return False


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

    apps = (list(logs_mod._FILE_LOGS)
            + list(getattr(logs_mod, "_GLOB_LOGS", {}))
            + list(logs_mod._SYSTEMD_LOGS))
    window_s = _parse_window_seconds(args.window)
    total_lines = 0
    failures = 0
    skipped_dormant = 0

    for app in apps:
        # For file-routed apps, skip re-tailing logs that haven't been
        # written to within the ingest window. journalctl already does
        # the equivalent via --since, so systemd apps don't need this.
        plan = logs_mod.route(app)
        if plan.get("kind") == "file":
            if _file_is_dormant(plan["path"], max_age_s=window_s):
                skipped_dormant += 1
                continue
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

    print(f"summary: apps={len(apps)} lines_indexed={total_lines} "
          f"failures={failures} skipped_dormant={skipped_dormant}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
