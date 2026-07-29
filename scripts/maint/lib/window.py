"""lib/window.py — maintenance window orchestrator + watchdog.

Responsibilities:
  - Lockfile lifecycle (open / close)
  - Queue drain (queue.jsonl → per-entry lifecycle.upgrade dispatch)
  - Health-only smoke run across every app in the manifest
  - Notifiarr pings at window open and close
  - Standalone watchdog that clears stale lockfiles

State-dir convention (same as state.py / notify.py):
  MANITOBA_STATE_DIR env var  →  fallback  ~/.opt/maint
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from lib import deep_check, health, kuma, lifecycle, listmonk, notify
from lib.lifecycle import LifecycleError
from lib.manifest import Manifest

logger = logging.getLogger(__name__)

_STALE_LOCK_HOURS = 4
_LOG_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Kuma green-poll defaults — hold the lock after smoke until every manifest
# monitor goes green (or the cap is reached). 60 min cap matches the operator's
# tolerance for service-settling; 60 s cadence is gentle on Kuma.
_KUMA_GREEN_MAX_WAIT_S = 3600
_KUMA_GREEN_POLL_INTERVAL_S = 60

# UCC app-upgrade sweep budget when run INSIDE the window (was a standalone
# 3h30m unit at 11:30 UTC; folded into the orchestrator 2026-06-28). Kept to
# 2h30m so sweep + the ≤60m green-poll + overhead all finish well before the
# 15:00 UTC watchdog (window opens 11:00 UTC = 4h budget). Override via env.
_UPGRADE_SWEEP_BUDGET_S = int(
    os.environ.get("MANITOBA_UPGRADE_BUDGET_S", str(2 * 3600 + 30 * 60))
)


# ---------------------------------------------------------------------------
# Public exceptions + types
# ---------------------------------------------------------------------------

class WindowAlreadyOpen(Exception):
    """Raised when open() detects a live lock and force=False."""


@dataclass
class WindowSummary:
    started_at: str
    closed_at: Optional[str]
    queue_processed: int
    queue_succeeded: int
    queue_dropped_unknown: int
    queue_dropped_max_block: int
    queue_deferred_active_cron: int
    smoke_results: dict  # app_name -> bool
    notes: list          # free-text notes
    # Kuma green-poll outcome (populated when wait_for_kuma_green ran)
    kuma_converged: bool = False
    kuma_wait_s: int = 0
    kuma_still_down: list = field(default_factory=list)
    # UCC app-upgrade sweep results (the JSON app-upgrade-all.sh writes to
    # last-upgrade.json; {} when the sweep was skipped/dry-run/failed).
    upgrade_results: dict = field(default_factory=dict)
    # True only when the sweep actually attempted to run but crashed, timed
    # out, or its results file couldn't be read — as opposed to a clean run
    # that legitimately found zero upgrades. See run_upgrade_sweep() (2026-07-29).
    upgrade_sweep_failed: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _state_dir_default() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_lock(lock_path: Path) -> tuple[int, str]:
    """Return (pid, start_iso) from lockfile content."""
    lines = lock_path.read_text(encoding="utf-8").splitlines()
    pid = int(lines[0].strip())
    start_iso = lines[1].strip() if len(lines) > 1 else ""
    return pid, start_iso


def _pid_alive(pid: int) -> bool:
    """Return True if the process is alive (posix kill-0 semantics).

    On Windows this always returns False so any existing lock is treated as
    stale — safe for the test environment.
    """
    if os.name != "posix":
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by another uid — treat as alive.
        return True


def _version_tuple(v: str):
    """Parse a dotted-version string into a comparable tuple of ints.

    Falls back gracefully on non-integer segments.
    """
    try:
        from packaging.version import Version  # type: ignore
        return Version(v)
    except Exception:
        pass
    parts = []
    for seg in v.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(seg)
    return tuple(parts)


def _version_exceeds_max(target: str, max_ver: str) -> bool:
    """Return True if target > max_ver (version comparison)."""
    try:
        return _version_tuple(target) > _version_tuple(max_ver)
    except Exception:
        # Fall back to plain string compare
        return target > max_ver


# ---------------------------------------------------------------------------
# UCC app-upgrade sweep (folded into the window 2026-06-28)
# ---------------------------------------------------------------------------

def _upgrade_script_path() -> Path:
    """Resolve app-upgrade-all.sh. window.py is at scripts/maint/lib/window.py,
    so the script is one directory up — same layout in the repo and on the box
    (~/scripts/maint/app-upgrade-all.sh)."""
    return Path(__file__).resolve().parent.parent / "app-upgrade-all.sh"


def _upgrade_results_path(state_dir: Path) -> Path:
    env = os.environ.get("MANITOBA_UPGRADE_RESULTS")
    return Path(env).expanduser() if env else Path(state_dir) / "last-upgrade.json"


def run_upgrade_sweep(
    *,
    state_dir: Path,
    dry_run: bool = False,
    budget_s: int = _UPGRADE_SWEEP_BUDGET_S,
    runner=subprocess.run,
) -> Optional[dict]:
    """Run the UCC app-upgrade sweep (app-upgrade-all.sh) and return the parsed
    results dict — the same JSON the script writes to last-upgrade.json, which
    the newsletter reads for its "what we tuned" section.

    Best-effort, NEVER raises: a sweep crash/timeout must not fail the window.

    Return contract (fixed 2026-07-29 — a crashed sweep used to be
    indistinguishable from a clean run that found zero upgrades, both
    collapsing to {}, which hid a broken weekly sweep for weeks with only a
    logger.error as a trace):
      - None  → the sweep did NOT produce trustworthy results: script missing,
        the subprocess raised (crash/timeout/OSError), or last-upgrade.json
        was missing/unparseable afterwards. Caller must treat this as a
        failure, not a zero-upgrade run.
      - dict  → the script ran and its results file parsed cleanly. May
        legitimately be {} or have an empty "summary" if the script itself
        wrote that — that's a real (if uninformative) result, not a failure.

    The script's internal budget is passed via env so it bails gracefully
    (and still writes results) before the Python subprocess timeout fires.
    """
    script = _upgrade_script_path()
    results_path = _upgrade_results_path(Path(state_dir))
    if not script.exists():
        logger.warning("upgrade sweep: script missing at %s — skipping", script)
        return None

    cmd = ["bash", str(script)]
    if dry_run:
        cmd.append("--dry-run")
    env = dict(os.environ)
    env["MANITOBA_UPGRADE_RESULTS"] = str(results_path)
    env["MANITOBA_UPGRADE_BUDGET_S"] = str(budget_s)

    try:
        runner(cmd, env=env, timeout=budget_s + 300,
               capture_output=True, text=True)
    except Exception as exc:
        # Timeout / OSError / anything — this is a real failure, not a clean
        # zero-upgrade run, so it must NOT collapse into the same {} below.
        logger.error("upgrade sweep run failed: %s", exc)
        return None

    try:
        return json.loads(results_path.read_text(encoding="utf-8"))
    except Exception as exc:
        # Missing/unreadable/malformed results file — same reasoning: this is
        # a failure to observe the outcome, distinct from an observed zero.
        logger.error("upgrade sweep: results unreadable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# WindowOrchestrator
# ---------------------------------------------------------------------------

class WindowOrchestrator:
    def __init__(
        self,
        *,
        state_dir: Optional[Path] = None,
        manifest: Manifest,
        dry_run: bool = False,
    ) -> None:
        self._state_dir: Path = Path(state_dir) if state_dir is not None else _state_dir_default()
        self._manifest = manifest
        self._dry_run = dry_run

        # Mutable window state (populated during open/drain/smoke)
        self._started_at: Optional[str] = None
        self._queue_processed = 0
        self._queue_succeeded = 0
        self._queue_dropped_unknown = 0
        self._queue_dropped_max_block = 0
        self._queue_deferred_active_cron = 0
        self._smoke_results: dict[str, bool] = {}
        self._notes: list[str] = []
        self._kuma_converged: bool = False
        self._kuma_wait_s: int = 0
        self._kuma_still_down: list[str] = []
        self._queue_depth_at_open: int = 0
        self._upgrade_results: dict = {}
        self._upgrade_sweep_failed: bool = False

    # ------------------------------------------------------------------ paths

    @property
    def _lock_path(self) -> Path:
        return self._state_dir / "lock"

    @property
    def _queue_path(self) -> Path:
        return self._state_dir / "queue.jsonl"

    @property
    def _deferred_queue_path(self) -> Path:
        return self._state_dir / "queue.deferred.jsonl"

    @property
    def _window_events_path(self) -> Path:
        return self._state_dir / "window-events.jsonl"

    def _window_log_path(self) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._state_dir / "window-log" / f"{today}.log"

    # ------------------------------------------------------------------ open

    def open(self, *, force: bool = False) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)

        if self._lock_path.exists():
            try:
                pid, start_iso = _parse_lock(self._lock_path)
            except Exception:
                pid, start_iso = 0, ""

            alive = _pid_alive(pid)

            if alive and not force:
                raise WindowAlreadyOpen(
                    f"window already in progress (PID={pid} started={start_iso}); use force=True to override"
                )
            elif not alive:
                msg = f"stale lock detected (PID={pid} dead), taking over"
                logger.info(msg)
                self._notes.append(msg)
            else:
                # force=True — log and overwrite
                msg = f"force override of existing lock (PID={pid})"
                logger.info(msg)
                self._notes.append(msg)

        self._started_at = _utc_now_iso()
        content = f"{os.getpid()}\n{self._started_at}\n"
        self._lock_path.write_text(content, encoding="utf-8")

    # ------------------------------------------------------------------ drain_queue

    def drain_queue(self) -> None:
        if not self._queue_path.exists():
            return

        raw = self._queue_path.read_text(encoding="utf-8")
        # Atomically truncate the queue immediately so we own all entries
        self._queue_path.write_text("", encoding="utf-8")

        deferred_lines: list[str] = []

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("queue: malformed JSON line skipped: %s", exc)
                self._notes.append(f"malformed queue line: {exc}")
                continue

            app_name = entry.get("app", "")
            target_version = entry.get("target_version", "")

            # 1. Unknown app
            try:
                app = self._manifest.app(app_name)
            except KeyError:
                logger.warning("queue: unknown app '%s', dropping", app_name)
                self._notes.append(f"unknown app dropped: {app_name}")
                self._queue_dropped_unknown += 1
                continue

            # 2. max_version ceiling
            if app.upgrade is not None and app.upgrade.max_version is not None:
                if _version_exceeds_max(target_version, app.upgrade.max_version):
                    reason = (
                        f"queue: {app_name} target {target_version} > max {app.upgrade.max_version}, dropping"
                    )
                    logger.warning(reason)
                    self._notes.append(reason)
                    self._queue_dropped_max_block += 1
                    continue

            # 3. Cron-class active → defer
            if app.class_ == "cron":
                lc_status = lifecycle.status(app)
                if lc_status.ok:
                    logger.info("queue: %s is cron-class and active, deferring", app_name)
                    self._notes.append(f"deferred (active cron): {app_name}")
                    self._queue_deferred_active_cron += 1
                    deferred_lines.append(line)
                    continue

            # 4. Dry-run: count but skip actual upgrade
            if self._dry_run:
                logger.info("queue: dry-run — skipping upgrade for %s@%s", app_name, target_version)
                self._notes.append(f"dry-run skip: {app_name}@{target_version}")
                self._queue_processed += 1
                continue

            # 5. Attempt upgrade
            self._queue_processed += 1
            try:
                lifecycle.upgrade(app, target_version)
                self._queue_succeeded += 1
                logger.info("queue: upgraded %s to %s", app_name, target_version)
            except LifecycleError as exc:
                logger.error("queue: upgrade failed for %s: %s", app_name, exc)
                self._notes.append(f"upgrade error {app_name}: {exc}")

        # Write deferred entries back
        if deferred_lines:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            with self._deferred_queue_path.open("a", encoding="utf-8") as fh:
                for dl in deferred_lines:
                    fh.write(dl + "\n")

    # ------------------------------------------------------------------ upgrade_sweep

    def upgrade_sweep(self) -> None:
        """Run the UCC app-upgrade sweep INSIDE the window (after the queue drain,
        before smoke). Folding it in (was a standalone Mon-11:30 timer) means every
        app restart it triggers happens under the lock — the pusher + canaries
        suppress, the webhook queues — so nothing fights the upgrade. Results land
        in last-upgrade.json (the newsletter's "what we tuned" source) and on the
        summary. Best-effort: a failure is noted, never fatal.
        """
        if self._dry_run:
            self._notes.append("dry-run: upgrade sweep skipped")
            return
        try:
            results = run_upgrade_sweep(state_dir=self._state_dir)
        except Exception as exc:  # run_upgrade_sweep is meant not to raise; belt+braces
            logger.error("upgrade_sweep: unexpected error: %s", exc)
            results = None

        if results is None:
            # 2026-07-29 fix: a crash/timeout/unreadable-results-file used to
            # fall through to {} here, indistinguishable from a sweep that ran
            # cleanly and found zero upgrades — the only trace was a
            # logger.error in journald, invisible until someone went looking.
            # Since this runs weekly, that hid a broken sweep for weeks.
            # Recorded explicitly on the summary so both the window-log line
            # and the Discord window-close message can say so.
            self._upgrade_sweep_failed = True
            self._upgrade_results = {}
            self._notes.append("upgrade sweep: FAILED to run or produce readable results")
            return

        self._upgrade_results = results or {}
        summ = self._upgrade_results.get("summary", {}) if isinstance(self._upgrade_results, dict) else {}
        if summ:
            self._notes.append(
                f"upgrade sweep: upgraded={summ.get('upgraded', 0)} "
                f"failed={summ.get('failed', 0)} bailed={summ.get('bailed', 0)}"
            )
        else:
            self._notes.append("upgrade sweep: no results recorded")

    # ------------------------------------------------------------------ smoke

    def smoke(self) -> None:
        for app in self._manifest.apps():
            result = health.probe(app)
            self._smoke_results[app.name] = result.ok

    # ------------------------------------------------------------------ wait_for_kuma_green

    def wait_for_kuma_green(
        self,
        *,
        max_wait_s: Optional[int] = None,
        poll_interval_s: Optional[int] = None,
        sleep=None,
        now=None,
    ) -> bool:
        """Hold the window open until every manifest monitor reports 'up' in
        Kuma, or `max_wait_s` elapses. Returns True if all monitors converged
        green within the cap; False if the cap was reached.

        External monitors and apps with no kuma_monitor are skipped.
        Monitors stuck at 'unknown' (network blip, pending, maintenance) are
        treated as not-yet-green and keep us polling.

        Resolves the module-level cap / cadence at call time (so tests can
        patch them). sleep/now injectable for deterministic tests.
        """
        if max_wait_s is None:
            max_wait_s = _KUMA_GREEN_MAX_WAIT_S
        if poll_interval_s is None:
            poll_interval_s = _KUMA_GREEN_POLL_INTERVAL_S
        if sleep is None:
            sleep = time.sleep
        if now is None:
            now = time.monotonic
        monitor_names = sorted({
            app.kuma_monitor for app in self._manifest.apps()
            if app.kuma_monitor
        })
        if not monitor_names:
            self._kuma_converged = True
            self._kuma_wait_s = 0
            self._kuma_still_down = []
            return True

        start = now()
        deadline = start + max_wait_s
        last_down: list[str] = list(monitor_names)

        while True:
            statuses = kuma.monitors_status(monitor_names)
            not_green = sorted(n for n, s in statuses.items() if s != "up")
            last_down = not_green

            if not not_green:
                self._kuma_converged = True
                self._kuma_wait_s = int(now() - start)
                self._kuma_still_down = []
                self._notes.append(
                    f"kuma green after {self._kuma_wait_s}s "
                    f"({len(monitor_names)} monitors)"
                )
                return True

            current = now()
            if current >= deadline:
                self._kuma_converged = False
                self._kuma_wait_s = int(current - start)
                self._kuma_still_down = not_green
                self._notes.append(
                    f"kuma green-poll timed out after {self._kuma_wait_s}s; "
                    f"still down: {', '.join(not_green)}"
                )
                return False

            sleep_for = min(poll_interval_s, max(1, int(deadline - current)))
            sleep(sleep_for)

    # ------------------------------------------------------------------ close

    def close(self) -> WindowSummary:
        closed_at = _utc_now_iso()

        # Remove lockfile
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass

        # Write summary to window log
        summary = WindowSummary(
            started_at=self._started_at or "",
            closed_at=closed_at,
            queue_processed=self._queue_processed,
            queue_succeeded=self._queue_succeeded,
            queue_dropped_unknown=self._queue_dropped_unknown,
            queue_dropped_max_block=self._queue_dropped_max_block,
            queue_deferred_active_cron=self._queue_deferred_active_cron,
            smoke_results=dict(self._smoke_results),
            notes=list(self._notes),
            kuma_converged=self._kuma_converged,
            kuma_wait_s=self._kuma_wait_s,
            kuma_still_down=list(self._kuma_still_down),
            upgrade_results=dict(self._upgrade_results),
            upgrade_sweep_failed=self._upgrade_sweep_failed,
        )

        log_path = self._window_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        smoke_pass = sum(1 for v in self._smoke_results.values() if v)
        smoke_total = len(self._smoke_results)
        kuma_seg = (
            f"kuma_green={self._kuma_wait_s}s"
            if self._kuma_converged
            else f"kuma_timeout still_down={len(self._kuma_still_down)}"
        )
        # Upgrade-sweep segment only appears when the sweep actually failed —
        # omitted on the happy path (incl. dry-run/no-results) so the log line
        # format above is unchanged for every case that isn't a new failure.
        # This is the audit-log side of the fix: a suppressed/failed sweep
        # must never be silently dropped (second design law), even though the
        # window-log line previously carried no upgrade info at all.
        upgrade_seg = " upgrade_sweep=FAILED" if self._upgrade_sweep_failed else ""
        summary_line = (
            f"{closed_at} window closed: "
            f"processed={self._queue_processed} "
            f"succeeded={self._queue_succeeded} "
            f"dropped_unknown={self._queue_dropped_unknown} "
            f"dropped_max_block={self._queue_dropped_max_block} "
            f"deferred_cron={self._queue_deferred_active_cron} "
            f"smoke={smoke_pass}/{smoke_total} "
            f"{kuma_seg}{upgrade_seg}\n"
        )
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(summary_line)

        return summary

    # ------------------------------------------------------------------ run

    def run(self, *, force: bool = False) -> WindowSummary:
        """Full window cycle: open → pre-maint notify → drain_queue → smoke →
        wait_for_kuma_green → close → post-maint notify.

        force is forwarded to open() — overrides an existing live lock.
        """
        self.open(force=force)

        # Snapshot queue depth before we drain so the pre-maint ping can
        # tell the operator what's about to be processed.
        try:
            self._queue_depth_at_open = sum(
                1 for ln in self._queue_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            )
        except FileNotFoundError:
            self._queue_depth_at_open = 0

        monitor_count = sum(1 for a in self._manifest.apps() if a.kuma_monitor)
        notify.notify(
            "🔧 maintenance window opened — "
            f"queued={self._queue_depth_at_open}, "
            f"monitors={monitor_count}, "
            f"green-poll cap={_KUMA_GREEN_MAX_WAIT_S // 60}m"
        )

        # Email the subscriber list — they expect a heads-up before things
        # may briefly hiccup. Fire-and-forget: listmonk failures don't block
        # the window (they log to notify-fail.log).
        if not self._dry_run:
            listmonk.fire_template_campaign(
                template_title="Maintenance Window Start",
                subject="QFlix maintenance window starting",
            )

        self.drain_queue()
        # Upgrade sweep runs INSIDE the lock, before smoke, so the post-upgrade
        # health probe + green-poll reflect the upgraded state (and nothing
        # auto-heals an app mid-upgrade). Skipped in dry-run.
        self.upgrade_sweep()
        self.smoke()

        # Hold the lock until Kuma confirms every manifest monitor is up,
        # or the cap fires. During this wait the webhook server queues
        # down events (see lib/kuma.do_POST) so we don't fight ourselves.
        if not self._dry_run:
            self.wait_for_kuma_green()
        else:
            self._kuma_converged = True
            self._notes.append("dry-run: kuma green-poll skipped")

        summary = self.close()

        smoke_pass = sum(1 for v in summary.smoke_results.values() if v)
        smoke_total = len(summary.smoke_results)
        if summary.kuma_converged:
            tail = f"kuma green in {summary.kuma_wait_s // 60}m{summary.kuma_wait_s % 60}s"
            level = "info"
            icon = "✅"
        else:
            still = ", ".join(summary.kuma_still_down[:6])
            if len(summary.kuma_still_down) > 6:
                still += f", +{len(summary.kuma_still_down) - 6} more"
            tail = f"kuma TIMEOUT after {summary.kuma_wait_s // 60}m, still down: {still}"
            level = "warning"
            icon = "⚠"
        if summary.upgrade_sweep_failed:
            # Explicit failure marker — 2026-07-29 fix. Previously a crashed/
            # timed-out/unreadable sweep produced the same {} as a clean
            # zero-upgrade run, so this segment was silently omitted and the
            # only trace was a logger.error, invisible for up to a week
            # between Monday windows. Happy-path format below is unchanged.
            up_seg = " · upgrade sweep FAILED"
        else:
            up_summ = summary.upgrade_results.get("summary", {}) if summary.upgrade_results else {}
            up_seg = f" · upgraded {up_summ.get('upgraded', 0)}" if up_summ else ""
        notify.notify(
            f"{icon} window closed: "
            f"{summary.queue_processed}↑ {summary.queue_succeeded}✓ "
            f"{summary.queue_dropped_unknown + summary.queue_dropped_max_block}⊘ "
            f"smoke={smoke_pass}/{smoke_total}{up_seg} · {tail}",
            level=level,
        )

        # Subscriber-list email — services are settled, give the all-clear.
        if not self._dry_run:
            listmonk.fire_template_campaign(
                template_title="Maintenance Window Complete",
                subject="QFlix maintenance window complete",
            )

        # Post-window deep-check: probe all apps and recover anything still
        # down that was suppressed/queued during the window. Best-effort —
        # a deep-check failure must not fail the window close. Skipped in
        # dry_run (no real recovery should fire during a dry run).
        if not self._dry_run:
            try:
                deep_check.run_deep_check(
                    reason="qflix-window",
                    manifest=self._manifest,
                )
            except Exception:
                pass  # best-effort; window summary already returned

        return summary

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        """Return lock status: present/absent, pid, started_at, alive."""
        if not self._lock_path.exists():
            return {"present": False, "pid": None, "started_at": None, "alive": False}

        try:
            pid, start_iso = _parse_lock(self._lock_path)
        except Exception:
            return {"present": True, "pid": None, "started_at": None, "alive": False}

        alive = _pid_alive(pid)
        return {
            "present": True,
            "pid": pid,
            "started_at": start_iso,
            "alive": alive,
        }


# ---------------------------------------------------------------------------
# Standalone watchdog
# ---------------------------------------------------------------------------

def watchdog_clear_stale_lock(state_dir: Optional[Path] = None) -> bool:
    """Check for a stale lockfile and remove it if stale.

    Returns True if a stale lock was cleared, False otherwise.
    A lock is stale if:
      - The PID is dead (kill -0 raises ProcessLookupError), OR
      - The start timestamp is older than 4 hours.
    """
    sd = Path(state_dir) if state_dir is not None else _state_dir_default()
    lock_path = sd / "lock"

    if not lock_path.exists():
        return False

    try:
        pid, start_iso = _parse_lock(lock_path)
    except Exception as exc:
        logger.warning("watchdog: could not parse lockfile: %s — removing", exc)
        lock_path.unlink(missing_ok=True)
        notify.notify("⚠ window watchdog cleared unreadable lockfile")
        return True

    # Check PID liveness (posix only)
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            # Process alive — fall through to time-based check
        except ProcessLookupError:
            lock_path.unlink(missing_ok=True)
            notify.notify(f"⚠ window watchdog cleared stale lockfile (PID {pid} dead)")
            logger.info("watchdog: cleared stale lock — PID %d dead", pid)
            return True
        except PermissionError:
            pass  # process exists, not ours — treat as alive

    # Time-based staleness check
    if start_iso:
        try:
            # Parse with or without trailing Z
            ts_str = start_iso.rstrip("Z")
            started = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - started
            if age > timedelta(hours=_STALE_LOCK_HOURS):
                lock_path.unlink(missing_ok=True)
                notify.notify(
                    f"⚠ window watchdog cleared stale lockfile (timeout exceeded: {age})"
                )
                logger.info("watchdog: cleared stale lock — age %s > 4h", age)
                return True
        except Exception as exc:
            logger.warning("watchdog: could not parse start time '%s': %s", start_iso, exc)

    logger.info("watchdog: lock active and within 4h, no action (pid=%d)", pid)
    return False
