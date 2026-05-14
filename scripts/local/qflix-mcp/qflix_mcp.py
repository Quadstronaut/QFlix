#!/usr/bin/env python3
"""qflix_mcp.py — MCP stdio server for QFlix farm inspection.

Read tools operate on B:\\QFlix\\data\\ (zero seedbox traffic).
Write tools proxy SSH to seedbox ~/scripts/mcp/.

11 tools: 8 read + 3 write.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

# Make `lib/` resolvable when the MCP runtime invokes us with arbitrary cwd.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

# Local lib (cache, ssh, discord, kuma_push)
from lib.cache import Cache  # noqa: E402
from lib.ssh import ssh_call  # noqa: E402

DATA_ROOT = Path(os.environ.get("QFLIX_DATA_ROOT", r"B:\QFlix\data"))


def _parse_ssh_timeout(stderr: str, default: int) -> dict:
    """Extract the timeout integer from 'ssh-timeout after Ns' stderr."""
    import re
    m = re.search(r"ssh-timeout after (\d+)s", stderr or "")
    return {"status": "ssh-timeout",
            "timeout_s": int(m.group(1)) if m else default}


def _cache() -> Cache:
    return Cache(DATA_ROOT)


# ===== READ TOOLS (cache-only) ===========================================

def qflix_status() -> dict:
    """Returns: latest snapshot timestamp, torrent count, Kuma red monitors,
    last-collect file age, recent action count.

    Use when: you want a one-shot health summary of the farm.
    """
    c = _cache()
    snap = c.latest_snapshot()
    if snap is None:
        return {"latest_snapshot": None, "torrent_count": 0, "kuma_red": [],
                "recent_actions_24h": 0}
    last_collect_file = DATA_ROOT / "last-collect.json"
    last_collect = None
    if last_collect_file.exists():
        try:
            last_collect = json.loads(last_collect_file.read_text())
        except json.JSONDecodeError:
            pass
    events = c.recent_events(n=200)
    return {
        "latest_snapshot": snap.get("captured_at"),
        "torrent_count": len((snap.get("qbit", {}).get("torrents") or [])),
        "kuma_red": (snap.get("health", {}).get("kuma_red") or []),
        "zombies": (snap.get("health", {}).get("zombies") or []),
        "last_collect": last_collect,
        "recent_actions_24h": len(events),
    }


def qflix_list_torrents() -> list:
    """Returns: full torrent list with enriched fields (qBit + *arr + Seerr).

    Use when: you need to inspect specific torrents (DL speed, progress,
    requester, *arr queue state).
    """
    snap = _cache().latest_snapshot()
    if snap is None:
        return []
    return list(snap.get("qbit", {}).get("torrents") or [])


def qflix_torrent_history(hash_: str, hours: int = 24) -> list:
    """Returns: per-hour samples of one torrent's progress/speed/state.

    Use when: investigating whether a torrent is genuinely stalled, recovering,
    or oscillating. Default last 24h, max 720h (30d).
    """
    hours = max(1, min(720, int(hours)))
    return _cache().history_for_hash(hash_, hours=hours)


def qflix_list_stale() -> list:
    """Returns: torrents currently flagged as 'candidate_for_unstick'
    (3+ consecutive hours of zero-movement matching a stale rule).

    Use when: deciding what to act on or audit before scheduled action.
    """
    state = _cache().load_stale_state()
    out = []
    for h, data in (state.get("hashes") or {}).items():
        if data.get("candidate_for_unstick"):
            out.append({
                "hash": h,
                "consecutive_zero_hours": data.get("consecutive_zero_hours"),
                "rule_matched": data.get("rule_matched"),
                "first_zero_movement_at": data.get("first_zero_movement_at"),
                "acted_on_at": data.get("acted_on_at"),
            })
    return out


def qflix_get_logs(app: str, date: Optional[str] = None,
                   grep: Optional[str] = None, max_lines: int = 500) -> list:
    """Returns: structured log lines for one app on one date.

    Use when: investigating recent app behavior. Default = today (UTC).
    `grep` filters lines containing the substring (case-insensitive).
    """
    import datetime as dt
    if date is None:
        date = dt.date.today().isoformat()
    log_file = DATA_ROOT / "logs" / date / f"{app}.log"
    if not log_file.exists():
        return []
    lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    if grep:
        gl = grep.lower()
        lines = [ln for ln in lines if gl in ln.lower()]
    return lines[-max_lines:]


def qflix_plex_libraries() -> dict:
    """Returns: Plex library list with counts + recently_added_24h + sessions.

    Use when: checking library health, recent additions, active streams.
    """
    snap = _cache().latest_snapshot()
    if snap is None:
        return {"libraries": [], "active_sessions": 0}
    return snap.get("plex", {})


def qflix_recent_events(n: int = 20) -> list:
    """Returns: last N action events (unstick/blocklist) newest-first.

    Use when: auditing what the autonomous collector has done.
    """
    return _cache().recent_events(n=n)


def qflix_arr_queue(slug: str) -> dict:
    """Returns: one *arr's full queue + missing_count.

    Use when: inspecting downloads in flight for a specific *arr.
    """
    snap = _cache().latest_snapshot()
    if snap is None:
        return {"queue": [], "missing_count": 0}
    return (snap.get("arrs", {}).get(slug) or {"queue": [], "missing_count": 0})


def qflix_list_log_apps() -> dict:
    """Returns: known log slugs from the host's logs.py routing tables.

    Use when: you want to know which `app` values qflix_get_logs accepts.
    Returns {"file_apps": [...], "systemd_apps": [...]} or
    {"status": "ssh-timeout", "timeout_s": N} on SSH timeout.
    """
    proc = ssh_call("python3 ~/scripts/mcp/logs.py --emit-json --list-apps",
                    timeout=30)
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=30)
    if proc.returncode != 0:
        return {"status": "ssh-failed", "code": proc.returncode,
                "stderr": proc.stderr[:300]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "bad-json", "stdout": proc.stdout[:300]}


# ===== WRITE TOOLS (proxy SSH to seedbox) ================================

def qflix_unstick_torrent(slug: str, queue_id: Optional[int] = None,
                          hash_: Optional[str] = None,
                          reason: str = "manual-via-mcp",
                          dry_run: bool = False) -> dict:
    """Manually unstick one *arr queue item (DELETE+blocklist+research).

    Use when: a specific torrent is wedged and you want to act now without
    waiting for the 3-hour rule.
    """
    args = [f"--slug {slug}", f'--reason "{reason}"']
    if queue_id is not None:
        args.append(f"--queue-id {int(queue_id)}")
    if hash_ is not None:
        args.append(f'--hash {hash_}')
    if dry_run:
        args.append("--dry-run")
    cmd = "python3 ~/scripts/mcp/unstick.py --emit-json " + " ".join(args)
    proc = ssh_call(cmd, timeout=60)
    if proc.returncode != 0:
        return {"status": "ssh-failed", "code": proc.returncode,
                "stderr": proc.stderr[:300]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "bad-json", "stdout": proc.stdout[:300]}


def qflix_trigger_missing_search(slug: Optional[str] = None) -> dict:
    """Fire MissingSearch on one *arr (or all if slug omitted).

    Use when: you suspect a recent indexer change unlocked grabs.
    The daily 07:00 UTC timer also calls this; manual invocation is harmless.
    """
    args = "" if slug is None else f"--slug {slug}"
    proc = ssh_call(f"python3 ~/scripts/mcp/missing.py --emit-json {args}",
                    timeout=120)
    if proc.returncode != 0:
        return {"status": "ssh-failed", "code": proc.returncode,
                "stderr": proc.stderr[:300]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "bad-json", "stdout": proc.stdout[:300]}


def qflix_refresh_collect() -> dict:
    """Force an out-of-band collect right now (don't wait for next hour).

    Use when: you've just fixed something and want a fresh snapshot
    immediately rather than waiting up to 60 minutes.
    """
    import datetime as dt
    proc = ssh_call("python3 ~/scripts/mcp/collect.py --emit-json", timeout=90)
    if proc.returncode != 0:
        return {"status": "ssh-failed", "code": proc.returncode,
                "stderr": proc.stderr[:300]}
    try:
        snap = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "bad-json", "stdout": proc.stdout[:300]}
    now = dt.datetime.utcnow()
    path = _cache().write_snapshot(now, snap)
    return {"status": "ok", "snapshot_path": str(path),
            "captured_at": snap.get("captured_at")}


# ===== MCP FRAMEWORK WIRING (lazy import) =================================

def _build_server():
    """Construct the MCP stdio server with all 11 tools registered."""
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("qflix-mcp")
    # Read tools
    server.tool()(qflix_status)
    server.tool()(qflix_list_torrents)
    server.tool()(qflix_torrent_history)
    server.tool()(qflix_list_stale)
    server.tool()(qflix_get_logs)
    server.tool()(qflix_plex_libraries)
    server.tool()(qflix_recent_events)
    server.tool()(qflix_arr_queue)
    server.tool()(qflix_list_log_apps)
    # Write tools
    server.tool()(qflix_unstick_torrent)
    server.tool()(qflix_trigger_missing_search)
    server.tool()(qflix_refresh_collect)
    return server


def main() -> int:
    server = _build_server()
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
