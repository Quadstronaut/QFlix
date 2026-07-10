#!/usr/bin/env python3
"""scripts/maint/qflix-collect.py — seedbox-side hourly farm collector.

Always-on Python port of the workstation orchestrator scripts/local/
qflix-collect.ps1. Migrated off the operator PC ("devil") to the qflix box
on 2026-07-09 because the workstation-resident job left the Kuma monitor
"QFlix Collect (workstation)" red — a CUSTOMER-VISIBLE false failure on the
public status page — whenever the PC was off, and silently stopped the
autonomous unstick loop. Same reasoning that moved VLogs ingest to the box
on 2026-05-14 (the autonomy mandate: no autonomy-critical job may depend on
the operator's PC being on).

Runs each hour under systemd-user timer `qflix-collect.timer`. Since it runs
ON the box, the SSH hops the PowerShell version made collapse into local
subprocess calls to ~/scripts/mcp/{collect,logs,unstick}.py.

Flow (mirrors the PS script's box-relevant steps):
  1. flock single-instance lock.
  2. collect.py --emit-json  -> snapshots/<date>/HH.json
  3. logs.py   --emit-json  -> logs/<date>/<app>.log (append)
  4. Walk last 3 snapshots -> stale-state.json; select unstick candidates.
  5. unstick.py per candidate (cap 10/day) -> events/<date>.jsonl.
  6. Discord summary + Kuma push (dead-man heartbeat).
  7. last-collect.json; prune retention.

Data root defaults to ~/.opt/qflix-collect (override QFLIX_COLLECT_DATA).
Exit 0 on success, 1 on fatal error (a down push to Kuma precedes it).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# --- Config ---------------------------------------------------------------
DATA_ROOT = Path(os.environ.get(
    "QFLIX_COLLECT_DATA", str(Path.home() / ".opt" / "qflix-collect")))
MCP_DIR = Path(os.environ.get(
    "QFLIX_MCP_DIR", str(Path.home() / "scripts" / "mcp")))
KUMA_BASE = os.environ.get("KUMA_BASE", "http://127.0.0.1:42005")
# The monitor was created workstation-side under this exact name; the box's
# ~/secrets/kuma-push-tokens.json already carries the token under this key,
# so the box feeds the SAME monitor — no Kuma re-creation needed.
KUMA_PUSH_KEY = os.environ.get("QFLIX_COLLECT_KUMA_KEY", "QFlix Collect (workstation)")
MAX_ACTIONS_PER_DAY = int(os.environ.get("QFLIX_COLLECT_MAX_ACTIONS", "10"))

DEAD_SLOW_BYTES = 10000        # dl_speed below this on a downloading torrent = dead-slow
ZERO_MOVEMENT_HOURS = 3        # snapshots of zero downloaded-delta before acting
META_STUCK_AGE_S = 86400       # metaDL + size 0 must be >=24h old to act


# --- Logging (systemd routes stdout/stderr to journald) -------------------
def log(msg: str) -> None:
    print("[qflix-collect] " + msg, flush=True)


def warn(msg: str) -> None:
    print("[qflix-collect] WARNING: " + msg, file=sys.stderr, flush=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat().replace("+00:00", "Z")


# --- Best-effort notify + Kuma (never raise into main flow) ---------------
def _notify(msg: str, level: str = "info") -> None:
    """Discord via lib.notify (matches qflix-reaper). Degrades to a logged
    no-op if the dep/webhook is missing. Never raises."""
    try:
        if str(_HERE) not in sys.path:
            sys.path.insert(0, str(_HERE))
        from lib.notify import notify
        notify(msg, level)
    except ImportError as exc:
        warn("notify unavailable (missing dep), continuing: " + str(exc))
    except Exception as exc:
        warn("notify failed (non-fatal): " + str(exc))


def _read_kuma_token() -> str:
    env = os.environ.get("QFLIX_COLLECT_KUMA_TOKEN")
    if env:
        return env
    try:
        path = Path.home() / "secrets" / "kuma-push-tokens.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get(KUMA_PUSH_KEY, "") or ""
    except Exception:
        return ""


def _push_kuma(status: str, msg: str) -> None:
    """Push a heartbeat to Kuma (stdlib urllib GET). status 'up'|'down'.
    Best-effort; swallows all errors."""
    token = _read_kuma_token()
    if not token:
        warn("no Kuma push token for '" + KUMA_PUSH_KEY + "' — skipping push")
        return
    qs = urllib.parse.urlencode({"status": status, "msg": msg[:200], "ping": 0})
    url = KUMA_BASE + "/api/push/" + token + "?" + qs
    try:
        urllib.request.urlopen(url, timeout=8).read()
    except Exception as exc:
        warn("Kuma push failed (non-fatal): " + str(exc))


# --- Run-lock (flock; auto-releases on process exit) ----------------------
_LOCK_PATH = os.environ.get("QFLIX_COLLECT_LOCK", "/tmp/qflix-collect.lock")


def _acquire_run_lock():
    try:
        import fcntl
    except ImportError:
        return True
    try:
        fh = open(_LOCK_PATH, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (OSError, IOError):
        return None


def _release_run_lock(handle) -> None:
    if handle is None or handle is True:
        return
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


# --- MCP subprocess helper ------------------------------------------------
def _run_mcp(script: str, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Invoke ~/scripts/mcp/<script> with args. The PowerShell version SSH'd
    these; on the box they are local subprocesses."""
    cmd = ["python3", str(MCP_DIR / script), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# --- Atomic JSON write ----------------------------------------------------
def _write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)   # atomic same-filesystem rename


# --- Step 2: snapshot -----------------------------------------------------
def collect_snapshot() -> Path:
    r = _run_mcp("collect.py",
                 ["--emit-json", "--include", "qbit,arrs,seerr,plex"], timeout=90)
    if r.returncode != 0:
        raise RuntimeError(f"collect.py exit={r.returncode}: {r.stderr.strip()[:300]}")
    now = utc_now()
    d = DATA_ROOT / "snapshots" / now.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    path = d / (now.strftime("%H") + ".json")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(r.stdout, encoding="utf-8")
    os.replace(tmp, path)
    return path


# --- Step 3: logs ---------------------------------------------------------
def collect_logs() -> bool:
    try:
        r = _run_mcp("logs.py",
                     ["--app", "all", "--since", "1h", "--tail", "2000", "--emit-json"],
                     timeout=60)
    except subprocess.TimeoutExpired:
        return False
    if r.returncode != 0:
        return False
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False
    today = utc_now().strftime("%Y-%m-%d")
    logs_dir = DATA_ROOT / "logs" / today
    logs_dir.mkdir(parents=True, exist_ok=True)
    for app_name, entry in payload.items():
        lines = (entry or {}).get("lines")
        if not lines:
            continue
        with open(logs_dir / (app_name + ".log"), "a", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(json.dumps(ln) + "\n")
    return True


# --- Step 4: stale-state --------------------------------------------------
def _load_snapshots(last_n: int = 3) -> list[dict]:
    snap_root = DATA_ROOT / "snapshots"
    if not snap_root.is_dir():
        return []
    files = sorted(str(p) for p in snap_root.rglob("*.json"))
    out = []
    for fp in files[-last_n:]:
        try:
            out.append(json.loads(Path(fp).read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def update_stale_state() -> list[str]:
    """Port of Update-StaleState. State is a plain dict persisted to
    stale-state.json. Returns hashes that are fresh unstick candidates."""
    state_file = DATA_ROOT / "stale-state.json"
    hashes: dict = {}
    if state_file.exists():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            hashes = dict(loaded.get("hashes", {}))
        except Exception:
            hashes = {}

    snaps = _load_snapshots(3)
    if len(snaps) < 3:
        _write_json_atomic(state_file, {"hashes": hashes, "updated_at": iso()})
        return []

    # hash -> [samples] across the 3 snapshots
    samples: dict[str, list[dict]] = {}
    for s in snaps:
        for t in (s.get("qbit", {}) or {}).get("torrents", []) or []:
            samples.setdefault(t.get("hash"), []).append({
                "downloaded": t.get("downloaded_bytes"),
                "state": t.get("state"),
                "progress": t.get("progress"),
                "dlspeed": t.get("dl_speed_bytes_s"),
            })

    candidates: list[str] = []
    for h, sm in list(samples.items()):
        if len(sm) < 3:
            continue
        try:
            delta = (sm[-1]["downloaded"] or 0) - (sm[0]["downloaded"] or 0)
        except TypeError:
            continue
        if delta != 0:
            hashes.pop(h, None)   # made progress — no longer stale
            continue
        latest = sm[-1]
        if (latest.get("progress") or 0) >= 1.0:
            continue
        state = latest.get("state")
        if state == "stalledDL":
            rule = "stalledDL"
        elif state == "downloading" and (latest.get("dlspeed") or 0) < DEAD_SLOW_BYTES:
            rule = "dead-slow"
        elif state in ("stoppedDL", "pausedDL"):
            rule = "stopped-incomplete"
        else:
            continue

        if h not in hashes:
            hashes[h] = {
                "first_zero_movement_at": iso(),
                "consecutive_zero_hours": ZERO_MOVEMENT_HOURS,
                "last_progress": latest.get("progress"),
                "rule_matched": rule,
                "candidate_for_unstick": True,
                "acted_on_at": None,
            }
        else:
            prev = int(hashes[h].get("consecutive_zero_hours") or 0)
            if prev < ZERO_MOVEMENT_HOURS:
                prev = ZERO_MOVEMENT_HOURS
            hashes[h]["consecutive_zero_hours"] = prev + 1
            hashes[h]["rule_matched"] = rule
            hashes[h]["candidate_for_unstick"] = True
            hashes[h]["last_progress"] = latest.get("progress")
        if not hashes[h].get("acted_on_at"):
            candidates.append(h)

    latest_snap = snaps[-1]
    latest_torrents = (latest_snap.get("qbit", {}) or {}).get("torrents", []) or []

    # Rule 3 (bad grab): completed torrent flagged bad — act now, no 3h wait.
    for t in latest_torrents:
        bg = t.get("bad_grab_signals") or {}
        if not bg.get("any"):
            continue
        h = t.get("hash")
        if h in hashes and hashes[h].get("acted_on_at"):
            continue
        if h not in hashes:
            rule = "bad-grab-size" if bg.get("suspicious_size") else "bad-grab-cf"
            hashes[h] = {
                "first_zero_movement_at": iso(),
                "consecutive_zero_hours": 0,
                "last_progress": t.get("progress"),
                "rule_matched": rule,
                "candidate_for_unstick": True,
                "acted_on_at": None,
            }
            candidates.append(h)

    # Rule 5 (meta-stuck): metaDL + size 0 + added >=24h ago.
    now_epoch = int(utc_now().timestamp())
    for t in latest_torrents:
        if t.get("state") != "metaDL":
            continue
        if t.get("size_bytes") != 0:   # PS: metaDL whose metadata never resolved
            continue
        added = t.get("added_on")
        if not added:
            continue
        age = now_epoch - int(added)
        if age < META_STUCK_AGE_S:
            continue
        h = t.get("hash")
        if h in hashes and hashes[h].get("acted_on_at"):
            continue
        if h in hashes:
            continue
        hashes[h] = {
            "first_zero_movement_at": iso(),
            "consecutive_zero_hours": int(age / 3600),
            "last_progress": t.get("progress"),
            "rule_matched": "meta-stuck",
            "candidate_for_unstick": True,
            "acted_on_at": None,
        }
        candidates.append(h)

    _write_json_atomic(state_file, {"hashes": hashes, "updated_at": iso()})
    return candidates


# --- Step 5: act ----------------------------------------------------------
_EFFECTIVE_RESULTS = ("deleted+blocklisted", "qbit-orphan-removed")
_TERMINAL_STATUSES = ("deleted+blocklisted", "qbit-orphan-removed", "already-fully-removed")


def count_todays_actions() -> int:
    """Only EFFECTIVE actions consume a daily slot; refusals stay in the
    audit log but must not gate the next attempt (same fix as unstick.py)."""
    today = utc_now().strftime("%Y-%m-%d")
    f = DATA_ROOT / "events" / (today + ".jsonl")
    if not f.exists():
        return 0
    n = 0
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("result") in _EFFECTIVE_RESULTS:
            n += 1
    return n


def stamp_acted_on(h: str) -> None:
    state_file = DATA_ROOT / "stale-state.json"
    if not state_file.exists():
        return
    try:
        loaded = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return
    if h not in loaded.get("hashes", {}):
        return
    loaded["hashes"][h]["acted_on_at"] = iso()
    _write_json_atomic(state_file, loaded)


def act_on_candidates(candidates: list[str]) -> list[str]:
    acted: list[str] = []
    count = count_todays_actions()
    events_dir = DATA_ROOT / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    for h in candidates:
        if count >= MAX_ACTIONS_PER_DAY:
            break
        try:
            r = _run_mcp("unstick.py",
                         ["--emit-json", "--hash", h, "--reason", "3h-zero-movement"],
                         timeout=60)
        except subprocess.TimeoutExpired:
            warn("unstick timeout for " + h)
            continue
        if r.returncode != 0:
            warn("unstick failed for " + h + ": " + r.stderr.strip()[:160])
            continue
        try:
            result = json.loads(r.stdout)
        except json.JSONDecodeError:
            continue
        today = utc_now().strftime("%Y-%m-%d")
        line = {
            "ts": iso(), "action": "unstick", "hash": h,
            "result": result.get("status"), "via": "qflix-collect.py",
        }
        with open(events_dir / (today + ".jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
        if result.get("status") in _TERMINAL_STATUSES:
            stamp_acted_on(h)
        acted.append(h)
        count += 1
    return acted


# --- Step 7: retention ----------------------------------------------------
def _prune_dir(sub: str, days: int, files: bool = False) -> None:
    root = DATA_ROOT / sub
    if not root.is_dir():
        return
    cutoff = utc_now().timestamp() - days * 86400
    for p in root.iterdir():
        try:
            if p.stat().st_mtime >= cutoff:
                continue
            if files and p.is_file():
                p.unlink()
            elif not files and p.is_dir():
                import shutil
                shutil.rmtree(p)
        except Exception:
            continue


def prune_retention() -> None:
    _prune_dir("snapshots", 30)
    _prune_dir("logs", 7)
    _prune_dir("events", 365, files=True)
    _prune_dir("runs", 7)


# --- Main -----------------------------------------------------------------
def main() -> int:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    lock = _acquire_run_lock()
    if lock is None:
        log("prior collect still running — exiting")
        return 0
    started = utc_now()
    try:
        snap_path = collect_snapshot()
        collect_logs()
        candidates = update_stale_state()
        acted = act_on_candidates(candidates) if candidates else []
        prune_retention()

        try:
            snap = json.loads(Path(snap_path).read_text(encoding="utf-8"))
            tcount = len((snap.get("qbit", {}) or {}).get("torrents", []) or [])
        except Exception:
            tcount = -1
        dur = round((utc_now() - started).total_seconds(), 2)
        msg = (f"Snapshot {started.strftime('%H')}.json: {tcount} torrents, "
               f"{len(candidates)} stale candidates, {len(acted)} actions")
        log(msg + f" ({dur}s)")
        _notify(msg, "info")
        _push_kuma("up", msg)

        _write_json_atomic(DATA_ROOT / "last-collect.json", {
            "ts": iso(started), "exit_code": 0, "duration_s": dur,
            "snapshot_path": str(snap_path), "torrent_count": tcount,
            "candidates": len(candidates), "actions": len(acted),
        })
        return 0
    except Exception as exc:
        err = str(exc)
        warn("collect failed: " + err)
        _notify("Collect failed: " + err, "error")
        _push_kuma("down", "collect failed: " + err[:160])
        _write_json_atomic(DATA_ROOT / "last-collect.json", {
            "ts": iso(started), "exit_code": 1, "error": err,
        })
        return 1
    finally:
        _release_run_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
