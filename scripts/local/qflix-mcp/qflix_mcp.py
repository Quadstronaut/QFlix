#!/usr/bin/env python3
"""qflix_mcp.py — MCP stdio server for QFlix farm inspection.

Read tools operate on B:\\QFlix\\data\\ (zero seedbox traffic) where possible.
qflix_get_logs proxies SSH and opportunistically write-throughs to VictoriaLogs
so on-demand pulls also enrich the persistent index.
Write tools proxy SSH to seedbox ~/scripts/mcp/.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# Make `lib/` resolvable when the MCP runtime invokes us with arbitrary cwd.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

# Local lib (cache, ssh, discord, kuma_push)
from lib.cache import Cache  # noqa: E402
from lib.ssh import ssh_call  # noqa: E402

DATA_ROOT = Path(os.environ.get("QFLIX_DATA_ROOT", r"B:\QFlix\data"))
VLOGS_URL = os.environ.get("QFLIX_VLOGS_URL", "http://127.0.0.1:9428")
VLOGS_TIMEOUT_S = float(os.environ.get("QFLIX_VLOGS_TIMEOUT_S", "3"))


def _ship_to_vlogs(app: str, result: dict) -> None:
    """Opportunistic write-through. Silent on every error path —
    log capture is best-effort enrichment, never block the read.

    Per QFlix autonomy mandate: every log line pulled by hand should
    also reach the persistent index so future questions answer locally
    without another SSH hop.
    """
    if not isinstance(result, dict):
        return
    lines = result.get("lines") or []
    if not lines:
        return
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
        return
    body = ("\n".join(payload_lines)).encode("utf-8")
    qs = urllib.parse.urlencode({
        "_stream_fields": "host,app",
        "_time_field":    "_time",
        "_msg_field":     "_msg",
    })
    url = f"{VLOGS_URL}/insert/jsonline?{qs}"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/stream+json"},
    )
    try:
        urllib.request.urlopen(req, timeout=VLOGS_TIMEOUT_S).read()
    except (urllib.error.URLError, TimeoutError, OSError):
        # VictoriaLogs may be down, not yet started, or unreachable.
        # Read path stays successful — the periodic shipper covers gaps.
        return


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


def qflix_get_logs(app: str, since: str = "24h", tail: int = 500,
                   grep: Optional[str] = None) -> dict:
    """Returns: structured log lines for one app via the host's logs.py.

    Use when: investigating recent app behavior.
    `since` accepts journalctl-style durations ("6h", "30m", "2d").
    `grep` filters lines whose `message` contains the substring (case-insensitive).
    Use qflix_list_log_apps() to discover valid `app` slugs.
    """
    cmd = (f"python3 ~/scripts/mcp/logs.py --emit-json "
           f"--app {app} --since {since} --tail {int(tail)}")
    proc = ssh_call(cmd, timeout=60)
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=60)
    if proc.returncode != 0:
        return {"status": "ssh-failed", "code": proc.returncode,
                "stderr": proc.stderr[:300]}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "bad-json", "stdout": proc.stdout[:300]}
    # Write-through to VictoriaLogs BEFORE grep so the index sees everything,
    # not only the filtered subset the caller asked about.
    _ship_to_vlogs(app, result)
    if grep and isinstance(result, dict) and "lines" in result:
        gl = grep.lower()
        result["lines"] = [
            ln for ln in result["lines"]
            if gl in (ln.get("message") or "").lower()
        ]
    return result


def qflix_query_logs(query: str, start: str = "1h",
                     limit: int = 200) -> dict:
    """Returns: matching log entries from the workstation-local VictoriaLogs index.

    Use when: investigating across multiple apps or hours where qflix_get_logs
    (one-shot SSH pull) is too narrow. Unlike qflix_get_logs, this hits a
    persisted index — useful for "what was sonarr doing 3 hours ago" or
    "show every error across the *arr stack in the last day". No SSH hop.

    `query` is LogsQL. Examples:
      - "level:ERROR"
      - "app:radarr AND _msg:timeout"
      - "_stream:{app=qbittorrent}"
    `start` is a duration string ("1h", "24h", "7d") — passed through to
    VictoriaLogs' `start` parameter, which accepts relative offsets.

    Returns {"entries": [...], "count": N, "query": ...} on success or
    {"status": "vlogs-unreachable", "error": ...} when the local server is down.
    """
    params = urllib.parse.urlencode({
        "query": query,
        "start": start,
        "limit": int(limit),
    })
    url = f"{VLOGS_URL}/select/logsql/query?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"status": "vlogs-unreachable", "error": str(e),
                "query": query}
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"entries": entries, "count": len(entries), "query": query}


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
                          dry_run: bool = False,
                          timeout: int = 120) -> dict:
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
    proc = ssh_call(cmd, timeout=timeout)
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=timeout)
    if proc.returncode != 0:
        return {"status": "ssh-failed", "code": proc.returncode,
                "stderr": proc.stderr[:300]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "bad-json", "stdout": proc.stdout[:300]}


def qflix_diagnose_unstick(slug: str, hash_: str) -> dict:
    """Time each phase of unstick.py's pre-flight path. No DELETE.

    Use when: unstick is hanging and you want to know which step is slow.
    Returns {status: "diagnose", phases: {state_read_ms, queue_lookup_paged_ms,
    queue_lookup_default_ms, hash_match_ms}, queue_size_*}.
    """
    cmd = (f"python3 ~/scripts/mcp/unstick.py --emit-json --diagnose "
           f"--slug {slug} --hash {hash_}")
    proc = ssh_call(cmd, timeout=180)
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=180)
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
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=120)
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
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=90)
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
    server.tool()(qflix_query_logs)
    server.tool()(qflix_plex_libraries)
    server.tool()(qflix_recent_events)
    server.tool()(qflix_arr_queue)
    server.tool()(qflix_list_log_apps)
    # Write tools
    server.tool()(qflix_unstick_torrent)
    server.tool()(qflix_diagnose_unstick)
    server.tool()(qflix_trigger_missing_search)
    server.tool()(qflix_refresh_collect)
    return server


def main() -> int:
    server = _build_server()
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
