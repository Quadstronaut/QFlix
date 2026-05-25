#!/usr/bin/env python3
"""qflix_mcp.py — MCP stdio server for QFlix farm inspection.

Read tools operate on B:\\QFlix\\data\\ (zero seedbox traffic) where possible.
qflix_get_logs proxies SSH to the seedbox.
qflix_query_logs hits the seedbox-resident VictoriaLogs index via SSH-exec'd
curl — no workstation tunnel required.
Write tools proxy SSH to seedbox ~/scripts/mcp/.
"""
from __future__ import annotations

import json
import os
import shlex
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

def _snapshot_age_minutes(captured_at: Optional[str]) -> Optional[int]:
    """Returns minutes elapsed since the snapshot was written. None if the
    timestamp is missing or unparseable."""
    if not captured_at:
        return None
    import datetime as _dt
    try:
        ts = _dt.datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = _dt.datetime.now(_dt.timezone.utc)
    return int((now - ts).total_seconds() // 60)


# After this many minutes with no new snapshot, every read tool annotates
# its response with stale_warning=True. Threshold matches the workstation
# Kuma dead-man (90 min after hourly collector misses).
_STALE_SNAPSHOT_MINUTES = 90


def qflix_status() -> dict:
    """Returns: latest snapshot timestamp, torrent count, Kuma red monitors,
    last-collect file age, recent action count. Includes snapshot age and a
    `stale_warning` flag so callers can detect a suspended/offline collector
    without manually parsing captured_at.

    Use when: you want a one-shot health summary of the farm.
    """
    c = _cache()
    snap = c.latest_snapshot()
    if snap is None:
        return {"latest_snapshot": None, "torrent_count": 0, "kuma_red": [],
                "recent_actions_24h": 0, "stale_warning": True,
                "stale_reason": "no snapshot has ever been written"}
    last_collect_file = DATA_ROOT / "last-collect.json"
    last_collect = None
    if last_collect_file.exists():
        try:
            last_collect = json.loads(last_collect_file.read_text())
        except json.JSONDecodeError:
            pass
    events = c.recent_events(n=200)
    captured_at = snap.get("captured_at")
    age = _snapshot_age_minutes(captured_at)
    stale = age is not None and age > _STALE_SNAPSHOT_MINUTES
    out = {
        "latest_snapshot": captured_at,
        "snapshot_age_minutes": age,
        "stale_warning": stale,
        "torrent_count": len((snap.get("qbit", {}).get("torrents") or [])),
        "kuma_red": (snap.get("health", {}).get("kuma_red") or []),
        "zombies": (snap.get("health", {}).get("zombies") or []),
        "last_collect": last_collect,
        "recent_actions_24h": len(events),
        # Caller-discoverability — these are the only slugs qflix_arr_queue +
        # qflix_unstick_torrent accept. Avoids the "guess the slug" pattern.
        "valid_arr_slugs": ["sonarr", "sonarr2", "radarr", "radarr2"],
    }
    if last_collect and last_collect.get("exit_code") not in (None, 0):
        out["last_collect_warning"] = (
            f"last collect exited {last_collect.get('exit_code')} — torrent "
            f"list and stale state may be from a partial collect"
        )
    return out


def qflix_list_torrents(state: Optional[str] = None,
                        category: Optional[str] = None,
                        stale_only: bool = False) -> list:
    """Returns: torrent list with enriched fields (qBit + *arr + Seerr).

    Use when: you need to inspect specific torrents (DL speed, progress,
    requester, *arr queue state). On large farms (>50 torrents) the
    unfiltered return floods context — prefer `stale_only=True` for
    debugging stuck downloads, or `state=` / `category=` filters.

    Parameters:
      state:      filter by qBit state ('stalledDL', 'downloading',
                  'pausedDL', 'queuedDL', 'metaDL', 'uploading', ...)
      category:   filter by qBit category ('tv-sonarr', 'radarr', ...)
      stale_only: when True, only include torrents currently flagged as
                  candidate_for_unstick by the collector heuristic.
    """
    snap = _cache().latest_snapshot()
    if snap is None:
        return []
    torrents = list(snap.get("qbit", {}).get("torrents") or [])

    if state:
        torrents = [t for t in torrents if (t.get("state") or "") == state]
    if category:
        torrents = [t for t in torrents
                    if (t.get("category") or "") == category]
    if stale_only:
        stale = _cache().load_stale_state().get("hashes") or {}
        candidates = {h for h, v in stale.items() if v.get("candidate_for_unstick")}
        torrents = [t for t in torrents
                    if (t.get("hash") or "").lower() in candidates]
    return torrents


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
    # shlex.quote every caller-supplied value so a hostile string (e.g.
    # `radarr; rm -rf ~`) can't break out of the SSH command line. tail is
    # cast to int upstream so it can't carry shell metacharacters.
    cmd = (f"python3 ~/scripts/mcp/logs.py --emit-json "
           f"--app {shlex.quote(app)} --since {shlex.quote(since)} "
           f"--tail {int(tail)}")
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
    if grep and isinstance(result, dict) and "lines" in result:
        gl = grep.lower()
        result["lines"] = [
            ln for ln in result["lines"]
            if gl in (ln.get("message") or "").lower()
        ]
    return result


def qflix_query_logs(query: str, start: str = "1h",
                     limit: int = 200) -> dict:
    """Returns: matching log entries from the seedbox-resident VictoriaLogs index.

    Use when: investigating across multiple apps or hours where qflix_get_logs
    (one-shot SSH pull) is too narrow. Hits the persistent 90-day index that
    qflix-vlogs-ingest.timer feeds every 5 min on the seedbox. SSH-exec'd
    curl, so no workstation tunnel required.

    `query` is LogsQL. Examples:
      - "level:ERROR"
      - "app:radarr AND _msg:timeout"
      - "_stream:{app=qbittorrent}"
    `start` is a duration string ("1h", "24h", "7d") — passed through to
    VictoriaLogs' `start` parameter, which accepts relative offsets.

    Returns {"entries": [...], "count": N, "query": ...} on success,
    {"status": "vlogs-unreachable", ...} when the seedbox server is down,
    or {"status": "ssh-timeout"|"ssh-failed", ...} on SSH-layer failure.
    """
    # Build the remote curl. shlex.quote each user-supplied value so a malicious
    # query string can't break out of the curl args. The port is read on the
    # seedbox side from ~/secrets/vlogs.port — no workstation-side config drift.
    q = shlex.quote(query)
    s = shlex.quote(start)
    n = shlex.quote(str(int(limit)))
    remote = (
        "PORT=$(cat ~/secrets/vlogs.port 2>/dev/null); "
        "[ -n \"$PORT\" ] || { echo 'vlogs-port-missing' >&2; exit 3; } && "
        f"curl -sf -m 10 --get "
        f"--data-urlencode query={q} "
        f"--data-urlencode start={s} "
        f"--data-urlencode limit={n} "
        "\"http://127.0.0.1:$PORT/select/logsql/query\""
    )
    proc = ssh_call(remote, timeout=15)
    if proc.returncode == 124:
        return {**_parse_ssh_timeout(proc.stderr, default=15), "query": query}
    if proc.returncode == 3:
        return {"status": "vlogs-unreachable",
                "error": "secrets/vlogs.port not set on seedbox",
                "query": query}
    if proc.returncode != 0:
        # curl exits 22 on HTTP >=400, 7 on connection refused, etc.
        return {"status": "vlogs-unreachable",
                "error": (proc.stderr or f"curl exit {proc.returncode}")[:200],
                "query": query}
    entries = []
    for line in proc.stdout.splitlines():
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
    """Returns: one *arr's full queue + missing_count, annotated with snapshot
    freshness (captured_at, snapshot_age_minutes, stale_warning).

    The queue is served from the latest cached collect snapshot, which can be
    up to an hour old — or staler if the collector is suspended. Without the
    freshness fields a just-acted-on item shows its pre-action state, which
    reads as a misleading 'already-removed' (the 2026-05 confusion). Callers
    seeing stale_warning=True should qflix_refresh_collect() before trusting
    queue contents.

    Use when: inspecting downloads in flight for a specific *arr.
    """
    snap = _cache().latest_snapshot()
    if snap is None:
        return {"queue": [], "missing_count": 0,
                "captured_at": None, "snapshot_age_minutes": None,
                "stale_warning": True,
                "stale_reason": "no snapshot has ever been written"}
    captured_at = snap.get("captured_at")
    age = _snapshot_age_minutes(captured_at)
    base = dict(snap.get("arrs", {}).get(slug) or {"queue": [], "missing_count": 0})
    base["captured_at"] = captured_at
    base["snapshot_age_minutes"] = age
    base["stale_warning"] = age is not None and age > _STALE_SNAPSHOT_MINUTES
    return base


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
    # shlex.quote every caller-supplied string so embedded shell metacharacters
    # (quotes, semicolons, backticks) can't break out of the SSH command line.
    args = [f"--slug {shlex.quote(slug)}", f"--reason {shlex.quote(reason)}"]
    if queue_id is not None:
        args.append(f"--queue-id {int(queue_id)}")
    if hash_ is not None:
        args.append(f"--hash {shlex.quote(hash_)}")
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
           f"--slug {shlex.quote(slug)} --hash {shlex.quote(hash_)}")
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
    args = "" if slug is None else f"--slug {shlex.quote(slug)}"
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
