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
# Paths verified against seedbox (manitoba) state — keep in sync when apps move
# or rotate. qbittorrent + homarr have no file logs in this layout and are
# intentionally absent.
_FILE_LOGS = {
    "sonarr":          str(HOME / ".apps/sonarr/logs/sonarr.txt"),
    "sonarr2":         str(HOME / ".apps/sonarr2/logs/sonarr.txt"),
    "radarr":          str(HOME / ".apps/radarr/logs/radarr.txt"),
    "radarr2":         str(HOME / ".apps/radarr2/logs/radarr.txt"),
    "prowlarr":        str(HOME / ".apps/prowlarr/logs/prowlarr.txt"),
    "bazarr":          str(HOME / ".apps/bazarr/log/bazarr.log"),
    "bazarr2":         str(HOME / ".apps/bazarr2/logs/bazarr2.log"),
    "tautulli":        str(HOME / ".apps/tautulli/logs/tautulli.log"),
    "seerr":           str(HOME / ".apps/seerr/logs/seerr.log"),
    "kometa":          str(HOME / ".apps/kometa/config/logs/meta.log"),
    "buildarr":        str(HOME / ".apps/buildarr/logs/buildarr.log"),
    "recyclarr":       str(HOME / ".apps/recyclarr/logs/recyclarr.log"),
    "nginx":           str(HOME / ".apps/nginx/logs/error.log"),
}

# Apps with date-rotated logs (no stable filename): resolve at scan time to the
# newest matching file. Glob is relative to HOME.
_GLOB_LOGS = {
    "maintainerr": ".apps/maintainerr/logs/maintainerr-*.log",
}

_SYSTEMD_LOGS = {
    "listmonk":      "listmonk.service",
    "tdarr-server":  "tdarr-server.service",
    "tdarr-node":    "tdarr-node.service",
    "qbittorrent":   "qbittorrent.service",
    "maint-pusher":  "manitoba-maint-pusher.service",
    "maint-webhook": "manitoba-maint-webhook.service",
    "maint-window":  "manitoba-maint-window.service",
}

# Order matters: most-specific patterns first. Each pattern must define named
# groups `ts` (optional), `lvl` (optional), and `msg` (rest of line).
_TS_PATTERNS = [
    # .NET *arr / bazarr pipe form. Two variants:
    #   2026-05-15 09:30:09.7|Info|RssSyncService|message       (no padding)
    #   2026-05-15 09:34:33|INFO    |root            |message|  (bazarr padded)
    re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
        r"\|\s*(?P<lvl>[A-Za-z]+)\s*\|[^|]*\|(?P<msg>.*)$"
    ),
    # Tautulli dash form:   2026-05-15 03:36:18 - INFO    :: thread :: message
    re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
        r"\s+-\s+(?P<lvl>[A-Z]+)\s+(?P<msg>.*)$"
    ),
    # Maintainerr DD/MM/YYYY pipe form: [maintainerr]  |  15/05/2026 04:45:00  [INFO] [Comp] msg
    re.compile(
        r"^\[\w+\]\s*\|\s*(?P<ts>\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})"
        r"\s+\[(?P<lvl>[A-Z]+)\]\s+(?P<msg>.*)$"
    ),
    # Kometa bracket-ts form: [2026-05-15 03:33:12,510] [kometa.py:480] [INFO] msg
    re.compile(
        r"^\[(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d+)\]"
        r"\s+.{0,80}?\[(?P<lvl>[A-Z]+)\]\s*(?P<msg>.*)$"
    ),
    # Python-logging form:  2026-05-15 04:30:13,104 mod:pid logger [INFO] message
    re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d+)"
        r"\s+.{0,80}?\[(?P<lvl>[A-Z]+)\]\s*(?P<msg>.*)$"
    ),
    # Bracket-no-ts form (recyclarr): [INF] anime: All quality profiles ...
    re.compile(r"^\[(?P<lvl>[A-Z]{2,8})\]\s*(?P<msg>.*)$"),
    # Fallback ts-only forms (kept for journalctl short-iso lines that the
    # bracket variants above already handle):
    re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[Z+\-:0-9]*)"
        r"\s*\[?(?P<lvl>[A-Z][a-zA-Z]+)?\]?\s*(?P<msg>.*)$"
    ),
]

# Map captured level → canonical uppercase tag. Anything not listed passes
# through .upper() unchanged, so unknown future levels still get a real value
# rather than collapsing to 'unknown'.
_LEVEL_NORMALIZE = {
    "INFO": "INFO", "INF": "INFO",
    "WARN": "WARN", "WARNING": "WARN", "WRN": "WARN",
    "ERROR": "ERROR", "ERR": "ERROR",
    "DEBUG": "DEBUG", "DBG": "DEBUG", "TRACE": "TRACE", "TRC": "TRACE",
    "FATAL": "FATAL", "CRITICAL": "FATAL", "CRIT": "FATAL",
}


def _resolve_glob(pattern: str) -> str | None:
    """Return the newest file matching the HOME-relative glob, or None."""
    import glob
    matches = glob.glob(str(HOME / pattern))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def route(app: str) -> dict:
    if app in _FILE_LOGS:
        return {"kind": "file", "path": _FILE_LOGS[app]}
    if app in _GLOB_LOGS:
        resolved = _resolve_glob(_GLOB_LOGS[app])
        return {"kind": "file", "path": resolved or str(HOME / _GLOB_LOGS[app])}
    if app in _SYSTEMD_LOGS:
        return {"kind": "journalctl", "unit": _SYSTEMD_LOGS[app]}
    return {"kind": "unsupported", "app": app}


def _normalize_level(raw: str | None) -> str:
    if not raw:
        return "unknown"
    up = raw.upper()
    return _LEVEL_NORMALIZE.get(up, up)


def _normalize_ts(raw: str | None) -> str | None:
    """Coerce assorted log timestamp shapes to ISO 8601 the vlogs ingester can
    parse. Handles space-as-T, comma-millis, and DD/MM/YYYY (maintainerr).
    Leaves already-ISO strings alone."""
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})[T ](\d{2}:\d{2}:\d{2})(.*)$", s)
    if m:
        dd, mm, yyyy, hms, rest = m.groups()
        s = f"{yyyy}-{mm}-{dd}T{hms}{rest}"
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if "," in s:
        s = s.replace(",", ".", 1)
    return s


def parse_line(line: str, *, source: str) -> dict:
    line = line.rstrip("\n")
    for pat in _TS_PATTERNS:
        m = pat.match(line)
        if m:
            gd = m.groupdict()
            return {
                "ts": _normalize_ts(gd.get("ts")),
                "level": _normalize_level(gd.get("lvl")),
                "message": gd.get("msg") or "",
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


_SELF_TEST_CASES = [
    # (label, raw_line, expected_level)
    ("arr-pipe",
     "2026-05-15 09:30:09.7|Info|RssSyncService|RSS Sync Completed.",
     "INFO"),
    ("arr-pipe-warn",
     "2026-05-15 09:29:59.2|Warn|Cardigann|Request failed.",
     "WARN"),
    ("arr-pipe-error",
     "2026-05-15 01:23:45.0|Error|ImportFailedCommand|Import failed",
     "ERROR"),
    ("bazarr-pipe-padded",
     "2026-05-15 09:34:33|INFO    |root                            |Using tvsubtitles|",
     "INFO"),
    ("bazarr-pipe-padded-error",
     "2026-05-15 09:34:33|ERROR   |root                            |trailing trace|",
     "ERROR"),
    ("kometa-bracket-ts",
     "[2026-05-15 03:33:12,510] [kometa.py:480]             [INFO]     | Version 2.3.1.4 |",
     "INFO"),
    ("maintainerr-ddmmyyyy",
     "[maintainerr]  |  15/05/2026 04:45:00  [INFO] [OverlayProcessorService] Started",
     "INFO"),
    ("maintainerr-error",
     "[maintainerr]  |  15/05/2026 04:45:00  [ERROR] [SomeService] Boom",
     "ERROR"),
    ("tautulli-dash",
     "2026-05-15 03:36:18 - INFO    :: ThreadPoolExecutor-2_2 : Tautulli Config :: Writing",
     "INFO"),
    ("tautulli-warning",
     "2026-05-15 03:36:18 - WARNING :: Worker :: Slow query",
     "WARN"),
    ("buildarr-bracket",
     "2026-05-15 04:30:13,104 buildarr:2651442 buildarr.cli.run [INFO] <sonarr> done",
     "INFO"),
    ("recyclarr-no-ts",
     "[INF] anime: All quality profiles are up to date!",
     "INFO"),
    ("recyclarr-warn",
     "[WRN] partial sync skipped",
     "WARN"),
    ("journalctl-iso",
     "2026-05-15T10:11:12+0000 some-host process: Started",
     "unknown"),
]


def _self_test() -> int:
    failures = []
    for label, line, expected in _SELF_TEST_CASES:
        parsed = parse_line(line, source="<test>")
        if parsed["level"] != expected:
            failures.append(
                f"  FAIL {label}: got level={parsed['level']!r} expected={expected!r} "
                f"line={line[:80]!r}"
            )
        else:
            print(f"  OK   {label}: level={parsed['level']}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\nFAIL: {len(failures)}/{len(_SELF_TEST_CASES)} cases", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(_SELF_TEST_CASES)}/{len(_SELF_TEST_CASES)} cases")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--cron", action="store_true")
    g.add_argument("--self-test", action="store_true",
                   help="run inline regex coverage tests and exit")
    ap.add_argument("--list-apps", action="store_true",
                    help="print routing tables and exit")
    ap.add_argument("--app", help="slug or 'all'")
    ap.add_argument("--since", default="24h")
    ap.add_argument("--tail", type=int, default=5000)
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    if args.list_apps:
        out = {
            "file_apps": sorted(_FILE_LOGS.keys()),
            "glob_apps": sorted(_GLOB_LOGS.keys()),
            "systemd_apps": sorted(_SYSTEMD_LOGS.keys()),
        }
        if args.emit_json:
            json.dump(out, sys.stdout, default=str)
            sys.stdout.write("\n")
        return 0
    if not args.app:
        ap.error("--app required (unless --list-apps)")

    if args.app == "all":
        apps = list(_FILE_LOGS) + list(_GLOB_LOGS) + list(_SYSTEMD_LOGS)
        result = {a: collect_for(a, since=args.since, tail=args.tail) for a in apps}
    else:
        result = collect_for(args.app, since=args.since, tail=args.tail)

    if args.emit_json:
        json.dump(result, sys.stdout, default=str)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
