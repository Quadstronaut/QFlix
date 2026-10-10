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


# ── Cursors ──────────────────────────────────────────────────────────────────
# WHY: every cycle used to re-tail the last --tail lines of each live file
# (and journalctl --since 6m on a 5-min timer). Anything still inside that tail
# was re-POSTed every 5 minutes, so one log line landed in vlogs ~18x
# (measured 2026-10-09: listmonk 1224 rows / 68 unique in a 2-min slice). That
# inflated hit counts and pushed plain `app:x` queries past the 30s limit.
# Now each file keeps a byte offset (+ inode, to spot rotation) and each
# journald unit keeps a journalctl cursor, so a line is shipped exactly once.
STATE_DIR = Path(os.environ.get("QFLIX_VLOGS_STATE",
                                "~/.local/state/qflix-vlogs")).expanduser()


def _load_cursors(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_cursors(path: Path, cursors: dict) -> None:
    # Write-then-rename so a crash mid-write can't leave a truncated file,
    # which would reset every cursor and re-ship the whole tail once.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursors, indent=1, sort_keys=True))
    os.replace(tmp, path)


def _read_new_lines(path: str, cur: dict | None, *, tail: int) -> tuple[list[str] | None, dict]:
    """Lines appended to `path` since `cur`, plus the advanced cursor.

    Returns (None, cur) when there is no cursor yet: the caller falls back to
    the old window/tail read once, then the cursor starts at EOF.
    Only complete lines are consumed; a half-written last line stays for the
    next cycle. If the file was rotated (new inode) or truncated (size below
    the offset), reading restarts at byte 0 of the new file.
    """
    try:
        st = os.stat(path)
    except OSError:
        return [], cur or {}
    if not cur:
        return None, {"inode": st.st_ino, "offset": st.st_size, "last_ts": None}
    offset = cur.get("offset", 0)
    if cur.get("inode") != st.st_ino or st.st_size < offset:
        offset = 0
    new = {**cur, "inode": st.st_ino, "offset": offset}
    if st.st_size == offset:
        return [], new
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], new
    new["offset"] = offset + end + 1
    lines = [ln.rstrip("\r") for ln in
             chunk[:end].decode("utf-8", errors="ignore").split("\n")]
    # A backlog bigger than --tail (ingest down for hours) keeps the newest
    # lines, same cap the old tail read had.
    return lines[-tail:], new


def _parse_with_carry(lines: list[str], *, source: str, last_ts: str | None) -> tuple[list[dict], str | None]:
    """logs.parse_line + the same ts carry-forward logs.collect_for does,
    seeded from the cursor so a continuation line at the top of this batch
    inherits the previous batch's time instead of the ingest clock."""
    # A cursor written before QFLX-44 holds a ZONE-LESS last_ts. Re-read it in
    # the source's zone so a continuation line cannot inherit a stamp that
    # vlogs would take as UTC (2h early/late for a LOCAL source).
    if last_ts and not last_ts.endswith("Z"):
        last_ts = logs_mod._normalize_ts(
            last_ts, logs_mod._tz_for_policy(logs_mod.zone_policy_for(source)))
    out = []
    for line in lines:
        if not line.strip():
            continue
        rec = logs_mod.parse_line(line, source=source)
        if rec["ts"] is None:
            rec["ts"] = last_ts
        else:
            last_ts = rec["ts"]
        out.append(rec)
    return out, last_ts


def _journal_new_lines(unit: str, cursor_file: Path, *, window: str, tail: int) -> list[str]:
    """journalctl --cursor-file reads after the saved cursor and rewrites it.

    No cursor file yet → bootstrap from the --since window once. journalctl
    (systemd 257) refuses --since together with --cursor-file, so the bootstrap
    uses --show-cursor and writes the trailing "-- cursor: X" line itself.
    Raises on a journalctl failure, so the caller counts it, not "0 lines".
    """
    import subprocess
    base = ["journalctl", "--user", "-u", unit, "-n", str(tail),
            "--output", "short-iso", "--no-pager"]
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    bootstrap = not cursor_file.exists()
    cmd = base + (["--since", f"{window} ago", "--show-cursor"] if bootstrap
                  else [f"--cursor-file={cursor_file}"])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"journalctl rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    out = []
    for ln in proc.stdout.splitlines():
        if ln.startswith("-- cursor: "):
            cursor_file.write_text(ln[len("-- cursor: "):].strip())
        elif ln.strip() != "-- No entries --":
            out.append(ln)
    return out


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
                    help="first-run bootstrap window, before an app has a cursor (default 6m)")
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
    cursors_path = STATE_DIR / "cursors.json"
    cursors = _load_cursors(cursors_path)

    for app in apps:
        plan = logs_mod.route(app)
        new_cur = None          # file cursor to commit once the POST lands
        jcur_file = jcur_prev = None
        try:
            if plan.get("kind") == "file":
                path = plan["path"]
                cur = cursors.get(app)
                if cur and cur.get("path") not in (None, path):
                    # Route moved (glob resolved to a newer file): the old
                    # offset means nothing in the new file.
                    cur = None
                raw, new_cur = _read_new_lines(path, cur, tail=args.tail)
                new_cur["path"] = path
                if raw is None:
                    # First sight of this app: one last window/tail read so the
                    # switch-over drops nothing, then the cursor (at EOF) rules.
                    if _file_is_dormant(path, max_age_s=window_s):
                        lines = []
                    else:
                        lines = logs_mod.collect_for(app, since=args.window,
                                                     tail=args.tail).get("lines") or []
                    new_cur["last_ts"] = next((ln["ts"] for ln in reversed(lines)
                                               if ln.get("ts")), None)
                else:
                    if not raw:
                        skipped_dormant += 1
                    lines, new_cur["last_ts"] = _parse_with_carry(
                        raw, source=path, last_ts=new_cur.get("last_ts"))
            elif plan.get("kind") == "journalctl":
                jcur_file = STATE_DIR / f"{app}.journal-cursor"
                jcur_prev = jcur_file.read_text() if jcur_file.exists() else None
                raw = _journal_new_lines(plan["unit"], jcur_file,
                                         window=args.window, tail=args.tail)
                lines, _ = _parse_with_carry(raw, source=f"journalctl:{plan['unit']}",
                                             last_ts=None)
            else:
                continue
        except Exception as exc:
            print(f"{app}: collect-failed {exc}")
            failures += 1
            continue
        ok, detail = _post_jsonline(port, app, lines)
        if not ok:
            print(f"{app}: post-failed {detail}")
            failures += 1
            # Un-advance: journalctl already rewrote its cursor file, so put
            # the old one back and these lines are retried next cycle.
            if jcur_file is not None:
                if jcur_prev is None:
                    jcur_file.unlink(missing_ok=True)
                else:
                    jcur_file.write_text(jcur_prev)
            continue
        if new_cur is not None:
            cursors[app] = new_cur
            _save_cursors(cursors_path, cursors)
        n = int(detail.split()[0]) if detail and detail.split()[0].isdigit() else 0
        total_lines += n
        if n > 0:
            print(f"{app}: {detail}")

    print(f"summary: apps={len(apps)} lines_indexed={total_lines} "
          f"failures={failures} skipped_dormant={skipped_dormant}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
