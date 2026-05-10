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
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from lib import health, lifecycle, notify
from lib.lifecycle import LifecycleError
from lib.manifest import Manifest

logger = logging.getLogger(__name__)

_STALE_LOCK_HOURS = 4
_LOG_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"


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

    # ------------------------------------------------------------------ smoke

    def smoke(self) -> None:
        for app in self._manifest.apps():
            result = health.probe(app)
            self._smoke_results[app.name] = result.ok

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
        )

        log_path = self._window_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        smoke_pass = sum(1 for v in self._smoke_results.values() if v)
        smoke_total = len(self._smoke_results)
        summary_line = (
            f"{closed_at} window closed: "
            f"processed={self._queue_processed} "
            f"succeeded={self._queue_succeeded} "
            f"dropped_unknown={self._queue_dropped_unknown} "
            f"dropped_max_block={self._queue_dropped_max_block} "
            f"deferred_cron={self._queue_deferred_active_cron} "
            f"smoke={smoke_pass}/{smoke_total}\n"
        )
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(summary_line)

        return summary

    # ------------------------------------------------------------------ run

    def run(self, *, force: bool = False) -> WindowSummary:
        """Full window cycle: open → drain_queue → smoke → close + notify at open/close.

        force is forwarded to open() — overrides an existing live lock.
        """
        self.open(force=force)
        notify.notify("🔧 maintenance window opened")

        self.drain_queue()
        self.smoke()

        summary = self.close()

        smoke_pass = sum(1 for v in summary.smoke_results.values() if v)
        smoke_total = len(summary.smoke_results)
        notify.notify(
            f"✅ window closed: "
            f"{summary.queue_processed}↑ {summary.queue_succeeded}✓ "
            f"{summary.queue_dropped_unknown + summary.queue_dropped_max_block}⊘ "
            f"smoke={smoke_pass}/{smoke_total}"
        )
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
