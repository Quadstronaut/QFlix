#!/usr/bin/env python3
"""flaresolverr-canary — health probe + auto-restart for FlareSolverr.

WHY this exists: FlareSolverr's bundled Chromium worker pool occasionally
exhausts itself under sustained Cloudflare-challenge pressure. The process
keeps running but the HTTP listener stops responding (connection refused
on the bound socket, or the / endpoint times out). On 2026-05-18 we found
an instance had been in this state for 10 days, silently breaking the
entire Prowlarr indexer fan-out. Restart cleared it; this canary makes
sure we never have to discover that by audit again.

WHAT it does NOT do: it does NOT restart FlareSolverr just because Prowlarr
is logging 500s. The most common 500 carries the payload "Error solving
the challenge. Timeout after 60.0 seconds." — that's FlareSolverr correctly
*refusing* an unsolvable Cloudflare challenge after the configured 60-second
budget. The service is healthy; the indexer is upstream-broken. Restarting
in that scenario does nothing and just adds churn. The canary only restarts
when FlareSolverr's own HTTP listener is unreachable or returns junk.

Probes (both must succeed):
  1. GET  /         → expect HTTP 200 with JSON containing "FlareSolverr is ready!"
  2. POST /v1       → expect HTTP 200 with {"status": "ok"} on sessions.list

If EITHER probe fails (timeout, connection refused, non-200, malformed
body), AND:
  - the FlareSolverr process has been up > FS_MIN_UPTIME_S (default 60s,
    so we don't restart during cold-start startup), AND
  - we've issued fewer than FS_MAX_RESTARTS_PER_HOUR restarts in the last
    rolling 60 minutes (default 3, crash-loop protection)
then: subprocess.run(['app-flaresolverr', 'restart']) and notify via Discord.

State file: ~/.opt/maint/flaresolverr-canary-state.json — JSON list of
restart epoch timestamps. Trimmed to the last hour on every run.

Schedule: systemd timer every 5 minutes
(scripts/maint/systemd/manitoba-maint-flaresolverr-canary.timer).

Reads creds: ~/secrets/flaresolverr.port + the Docker bridge IP
(172.17.0.1 — Ultra.cc default; override via FS_HOST env var).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SECRETS_DIR = Path(os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets")))
STATE_DIR = Path(os.environ.get("MANITOBA_STATE_DIR", str(Path.home() / ".opt" / "maint")))
STATE_FILE = STATE_DIR / "flaresolverr-canary-state.json"

FS_HOST = os.environ.get("FS_HOST", "172.17.0.1")
# Push-suppress registry key. The pusher mutes the "FlareSolverr" Kuma monitor
# under this same key while flaresolverr is knowingly down (awaiting the
# Ultra.cc cap_setuid ticket); this canary honors it too so it stops paging
# while the outage is already acknowledged. Default matches the app name in
# manifest/apps.yaml and the self-destructing unsuppress watcher's APP var.
FS_SUPPRESS_KEY = os.environ.get("FS_SUPPRESS_KEY", "flaresolverr")
FS_TIMEOUT_S = int(os.environ.get("FS_TIMEOUT_S", "10"))
FS_MIN_UPTIME_S = int(os.environ.get("FS_MIN_UPTIME_S", "60"))
FS_MAX_RESTARTS_PER_HOUR = int(os.environ.get("FS_MAX_RESTARTS_PER_HOUR", "3"))
FS_RESTART_CMD = os.environ.get("FS_RESTART_CMD", "app-flaresolverr restart")
# Restart-command subprocess timeout. 60s was too tight: during a host-level
# reboot recovery (load avg ≥30 on the 2026-05-20 incident), `app-flaresolverr
# restart` exceeded 60s and the canary emitted a false-positive "restart
# command timed out" operator alert. 180s gives Ultra.cc's helper room to
# tear down + restart the Docker container even under heavy contention.
FS_RESTART_TIMEOUT_S = int(os.environ.get("FS_RESTART_TIMEOUT_S", "180"))
# After restart, poll the probes for up to this long before declaring failure.
# Chromium subprocess spin-up takes 15-60s on a normal boot, longer under
# post-reboot CPU contention. Old single-shot 15s wait + 10s probe timeout
# was too short to catch a slow but successful recovery.
FS_POST_RESTART_DEADLINE_S = int(os.environ.get("FS_POST_RESTART_DEADLINE_S", "120"))
FS_POST_RESTART_POLL_INTERVAL_S = int(os.environ.get("FS_POST_RESTART_POLL_INTERVAL_S", "5"))


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def _load_state() -> list[float]:
    """Returns list of restart epoch timestamps (newest last)."""
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text())
        return data.get("restarts") or []
    except Exception:
        return []


def _save_state(restarts: list[float]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"restarts": restarts}, indent=2))


def _trim_history(restarts: list[float], now: float) -> list[float]:
    cutoff = now - 3600
    return [t for t in restarts if t >= cutoff]


def _probe_root(url: str) -> tuple[bool, str]:
    """GET / and verify ready-message. Returns (ok, detail)."""
    try:
        with urllib.request.urlopen(url, timeout=FS_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False, f"http={resp.status}"
            body = resp.read().decode("utf-8", errors="replace")
            if "FlareSolverr is ready" in body:
                return True, "ready"
            return False, f"unexpected-body[{body[:80]}]"
    except urllib.error.URLError as exc:
        return False, f"url-error[{exc.reason}]"
    except socket.timeout:
        return False, "timeout"
    except Exception as exc:
        return False, f"unexpected[{type(exc).__name__}:{exc}]"


def _probe_v1(url: str) -> tuple[bool, str]:
    """POST /v1 with sessions.list. Returns (ok, detail)."""
    payload = json.dumps({"cmd": "sessions.list"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=FS_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False, f"http={resp.status}"
            body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return False, f"bad-json[{body[:80]}]"
            if parsed.get("status") == "ok":
                return True, "ok"
            return False, f"status={parsed.get('status')!r}"
    except urllib.error.URLError as exc:
        return False, f"url-error[{exc.reason}]"
    except socket.timeout:
        return False, "timeout"
    except Exception as exc:
        return False, f"unexpected[{type(exc).__name__}:{exc}]"


def _process_uptime_s() -> int | None:
    """Best-effort: find the FlareSolverr python process and return uptime in
    seconds. Returns None if not found (which we treat as 'restart-eligible'
    since the process is missing entirely)."""
    try:
        proc = subprocess.run(
            ["pgrep", "-o", "-f", "/app/flaresolverr.py"],
            capture_output=True, text=True, timeout=5,
        )
        pid = proc.stdout.strip()
        if not pid:
            return None
        # /proc/<pid>/stat field 22 is starttime in clock ticks since boot;
        # comparing to /proc/uptime gets us the process age in seconds.
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read().split()
        start_ticks = float(stat[21])
        clk_tck = os.sysconf("SC_CLK_TCK") or 100
        with open("/proc/uptime") as f:
            boot_uptime = float(f.read().split()[0])
        proc_uptime = boot_uptime - (start_ticks / clk_tck)
        return int(proc_uptime)
    except (subprocess.TimeoutExpired, FileNotFoundError, IndexError,
            ValueError, OSError):
        return None


def _restart() -> tuple[bool, str]:
    """Invoke the configured restart command. Returns (ok, output)."""
    try:
        proc = subprocess.run(
            FS_RESTART_CMD.split(),
            capture_output=True, text=True, timeout=FS_RESTART_TIMEOUT_S,
        )
        ok = proc.returncode == 0
        return ok, (proc.stdout + proc.stderr).strip()[:400]
    except subprocess.TimeoutExpired:
        return False, f"restart command timed out after {FS_RESTART_TIMEOUT_S}s"
    except FileNotFoundError:
        return False, f"restart command not found: {FS_RESTART_CMD!r}"


def _wait_for_healthy(base: str) -> tuple[bool, str, str]:
    """Poll /  and /v1 until both report ok, or until the deadline. Returns
    (ok, root_detail, v1_detail) reflecting the LAST observed state."""
    deadline = time.time() + FS_POST_RESTART_DEADLINE_S
    ok_root = ok_v1 = False
    detail_root = detail_v1 = "not-yet-probed"
    while time.time() < deadline:
        ok_root, detail_root = _probe_root(f"{base}/")
        ok_v1, detail_v1 = _probe_v1(f"{base}/v1")
        if ok_root and ok_v1:
            return True, detail_root, detail_v1
        time.sleep(FS_POST_RESTART_POLL_INTERVAL_S)
    return False, detail_root, detail_v1


def _suppress_reason() -> str | None:
    """Return the push-suppression reason for FlareSolverr if its monitor is
    muted in the push-suppress registry, else None.

    WHY this canary needs its own check: the pusher already pushes the
    "FlareSolverr" Kuma monitor UP-with-[SUPPRESSED] and skips recovery when
    flaresolverr is listed in push-suppress.json (e.g. while it's knowingly down
    awaiting the Ultra.cc cap_setuid ticket). But this canary runs on its OWN
    5-minute systemd timer and notifies Discord *directly* — so without this
    check it keeps paging "restart REFUSED — crash-loop; operator intervention
    needed" even though the operator has already acknowledged the outage and
    muted the monitor. The self-destructing unsuppress watcher removes the
    registry entry once flaresolverr is live, restoring both the pushed monitor
    and this canary in one move.

    Best-effort: returns None on any error (fail toward normal alerting, never
    toward silent suppression) — mirrors lib.suppression.push_suppressed."""
    try:
        here = Path(__file__).resolve().parent
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        from lib.suppression import push_suppressed
        return push_suppressed(FS_SUPPRESS_KEY)
    except Exception as exc:
        print(f"suppress check failed (non-fatal): {exc}", file=sys.stderr)
        return None


def _notify(msg: str, level: str = "info") -> None:
    """Discord notification via lib.notify (best-effort, never raises)."""
    try:
        here = Path(__file__).resolve().parent
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        from lib.notify import notify
        notify(msg, level)
    except Exception as exc:
        print(f"notify failed (non-fatal): {exc}", file=sys.stderr)


def run(dry_run: bool) -> int:
    # Honor push-suppression FIRST: if the operator has muted FlareSolverr
    # (monitor + recovery) in the push-suppress registry, this canary must go
    # fully silent too — no probe, no restart churn, no Discord page. Otherwise
    # a crash-looping flaresolverr keeps paging "restart REFUSED" every cycle
    # despite the outage already being acknowledged. The unsuppress watcher
    # lifts this automatically once flaresolverr is live again.
    suppressed = _suppress_reason()
    if suppressed:
        print(f"SUPPRESSED ({suppressed}) — FlareSolverr is muted in the "
              f"push-suppress registry; skipping probe/restart/notify. The "
              f"unsuppress watcher restores alerting once it's live.")
        return 0

    port = _read(SECRETS_DIR / "flaresolverr.port")
    if not port:
        print("FATAL: ~/secrets/flaresolverr.port missing or empty",
              file=sys.stderr)
        return 2

    base = f"http://{FS_HOST}:{port}"
    print(f"--- flaresolverr-canary ({'DRY-RUN' if dry_run else 'LIVE'}) base={base} ---")

    ok_root, detail_root = _probe_root(f"{base}/")
    ok_v1, detail_v1 = _probe_v1(f"{base}/v1")
    print(f"  probe /        → ok={ok_root} ({detail_root})")
    print(f"  probe /v1      → ok={ok_v1} ({detail_v1})")

    if ok_root and ok_v1:
        print("HEALTHY — no action.")
        return 0

    uptime = _process_uptime_s()
    print(f"  process uptime → {uptime}s")
    if uptime is not None and uptime < FS_MIN_UPTIME_S:
        print(f"  uptime < FS_MIN_UPTIME_S ({FS_MIN_UPTIME_S}s) — likely "
              "still starting; deferring.")
        return 0

    now = time.time()
    history = _trim_history(_load_state(), now)
    if len(history) >= FS_MAX_RESTARTS_PER_HOUR:
        msg = (
            f"flaresolverr-canary: restart REFUSED — "
            f"{len(history)} restarts in last hour ≥ cap {FS_MAX_RESTARTS_PER_HOUR}. "
            f"Probes: root[{detail_root}] v1[{detail_v1}]. "
            f"Probable crash-loop; operator intervention needed."
        )
        print(msg)
        _notify(msg, level="error")
        return 3

    if dry_run:
        print("  DRY-RUN — would restart, would notify. Stopping here.")
        return 0

    ok, out = _restart()
    print(f"  restart action → ok={ok} output={out!r}")

    if ok:
        history.append(now)
        _save_state(history)
        # Poll for recovery instead of a single-shot probe. Chromium subprocess
        # spin-up runs 15-60s on a normal boot, longer when the host is under
        # post-reboot load. The single 15s wait + 10s probe used to flag
        # successful slow recoveries as "restart failed".
        print(f"  waiting up to {FS_POST_RESTART_DEADLINE_S}s for "
              f"FlareSolverr to come back...")
        healthy, d2_root, d2_v1 = _wait_for_healthy(base)
        print(f"  post-restart / → ok={healthy} ({d2_root})")
        print(f"  post-restart /v1 → ok={healthy} ({d2_v1})")
        if healthy:
            msg = (
                f"flaresolverr-canary: ✓ restarted FlareSolverr — "
                f"probes failed (root[{detail_root}] v1[{detail_v1}]); "
                f"recovered after restart #{len(history)} in the last hour."
            )
            _notify(msg, level="warning")
        else:
            msg = (
                f"flaresolverr-canary: ⚠ restart issued but not yet healthy "
                f"after {FS_POST_RESTART_DEADLINE_S}s — "
                f"root[{d2_root}] v1[{d2_v1}]; "
                f"this is restart #{len(history)} in the last hour. "
                f"Next cycle will re-probe; if still down then, operator "
                f"intervention needed."
            )
            _notify(msg, level="warning")
        return 0
    else:
        msg = (
            f"flaresolverr-canary: ✗ restart command FAILED — {out}. "
            f"Probes: root[{detail_root}] v1[{detail_v1}]. "
            f"Operator intervention needed."
        )
        print(msg)
        _notify(msg, level="error")
        return 4


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="probe + decide, but do NOT actually restart or record state")
    args = ap.parse_args()
    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
