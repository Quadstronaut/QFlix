"""lib/recovery.py — 3-attempt recovery loop with backoff + escalation.

Per-app threading.Lock prevents two concurrent recoveries for the same app.
Never raises — all failure modes collapse into the result dict + a notify.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from lib import health, kuma, lifecycle, notify, state
from lib.manifest import App, Manifest

# ---------------------------------------------------------------------------
# State path
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


_STATE_PATH: Path = _state_dir() / "state.json"

# ---------------------------------------------------------------------------
# Per-app lock registry
# ---------------------------------------------------------------------------

_locks: dict[str, threading.Lock] = {}
_locks_mutex: threading.Lock = threading.Lock()

_LOCK_ACQUIRE_TIMEOUT_S: float = 120.0


def _get_lock(app_name: str) -> threading.Lock:
    with _locks_mutex:
        if app_name not in _locks:
            _locks[app_name] = threading.Lock()
        return _locks[app_name]


# ---------------------------------------------------------------------------
# Async fire-and-forget trigger (called from pusher when health probe fails)
# ---------------------------------------------------------------------------

# Cap parallel recoveries: at most 5 apps recovering at once.
_RECOVERY_SEMAPHORE = threading.BoundedSemaphore(5)

# Tracks app_names currently in a recovery thread, so the pusher doesn't
# fire a second recovery while the first is still running its 3-attempt loop.
_in_flight: set[str] = set()
_in_flight_mutex = threading.Lock()


def _is_recoverable(app: App) -> bool:
    """Library / cron apps don't have a service to start — skip them."""
    return app.class_ in {"ucc", "systemd"}


def trigger_async(app: App, *, manifest: Optional[Manifest] = None) -> str:
    """Fire-and-forget recovery for `app` if not already running.

    Returns one of:
      - "started"           — recovery thread spawned
      - "already_running"   — recovery for this app is in flight
      - "cap_exceeded"      — global semaphore full; skipped
      - "not_recoverable"   — class can't be auto-recovered (library/cron)
      - "parked"            — manifest marks app as intentionally stopped

    Never blocks. Safe to call on every push cycle.
    """
    if getattr(app, "parked", False):
        return "parked"
    if not _is_recoverable(app):
        return "not_recoverable"

    with _in_flight_mutex:
        if app.name in _in_flight:
            return "already_running"
        if not _RECOVERY_SEMAPHORE.acquire(blocking=False):
            return "cap_exceeded"
        _in_flight.add(app.name)

    def _worker():
        try:
            run(app.name, manifest=manifest)
        except Exception as exc:
            sys.stderr.write(f"recovery thread crashed for {app.name}: {exc}\n")
        finally:
            with _in_flight_mutex:
                _in_flight.discard(app.name)
            _RECOVERY_SEMAPHORE.release()

    threading.Thread(target=_worker, daemon=True).start()
    return "started"


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------

def _load_default_manifest() -> Manifest:
    from lib import manifest as manifest_mod
    manifest_path = os.environ.get(
        "MANITOBA_MANIFEST_PATH",
        str(Path.home() / ".opt" / "maint" / "apps.yaml"),
    )
    return manifest_mod.load(manifest_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(app_name: str, *, manifest: Optional[Manifest] = None) -> dict:
    """Execute the 3-attempt recovery flow for `app_name`.

    Returns a dict:
      {"app": app_name, "event": str, "attempts": int,
       "final_health": str, "kuma_status": str}

    Always writes outcome to state via state.record().
    Always notifies via notify.notify().
    Never raises.
    """
    if manifest is None:
        try:
            manifest = _load_default_manifest()
        except Exception as exc:
            _emit("unknown_app", app_name, 0, "unknown", "unknown",
                  f"manifest load failed: {exc}", "warning")
            return _result(app_name, "unknown_app", 0, "unknown", "unknown")

    # Resolve app
    try:
        app = manifest.app(app_name)
    except KeyError:
        msg = f"Recovery requested for unknown app '{app_name}' — not in manifest"
        _emit("unknown_app", app_name, 0, "unknown", "unknown", msg, "warning")
        return _result(app_name, "unknown_app", 0, "unknown", "unknown")

    # Per-app lock
    lock = _get_lock(app_name)
    acquired = lock.acquire(timeout=_LOCK_ACQUIRE_TIMEOUT_S)
    if not acquired:
        msg = f"Recovery for '{app_name}' already in progress — dropping duplicate"
        notify.notify(msg, "warning")
        return _result(app_name, "duplicate_recovery_dropped", 0, "unknown", "unknown")

    try:
        return _recovery_loop(app)
    finally:
        lock.release()


def _recovery_loop(app: App) -> dict:
    app_name = app.name
    attempts_max = int(app.defaults.get("recovery_attempts", 3))
    backoff = list(app.defaults.get("recovery_backoff_s", [10, 30, 60]))
    recheck_delay = int(app.defaults.get("kuma_recheck_delay_s", 90))

    for attempt in range(1, attempts_max + 1):
        lifecycle.start(app)

        # Backoff before probing
        sleep_s = backoff[attempt - 1] if attempt - 1 < len(backoff) else backoff[-1]
        time.sleep(sleep_s)

        hr = health.probe(app)
        if hr.ok:
            # Local health OK
            if app.kuma_monitor is None:
                msg = f"✓ {app_name} recovered after {attempt} attempt(s)"
                _emit("recovered", app_name, attempt, "ok", "n/a", msg, "info")
                return _result(app_name, "recovered", attempt, "ok", "n/a")

            # Wait for Kuma to re-probe
            time.sleep(recheck_delay)
            kstatus = kuma.monitor_status(app.kuma_monitor)

            if kstatus == "up":
                msg = f"✓ {app_name} recovered after {attempt} attempt(s)"
                _emit("recovered", app_name, attempt, "ok", kstatus, msg, "info")
                return _result(app_name, "recovered", attempt, "ok", kstatus)
            else:
                msg = (
                    f"⚠ {app_name} healthy locally but Kuma reports {kstatus} "
                    f"— likely routing/auth issue"
                )
                _emit("healthy_locally_kuma_down", app_name, attempt, "ok",
                      kstatus, msg, "warning")
                return _result(app_name, "healthy_locally_kuma_down", attempt, "ok", kstatus)

    # All attempts exhausted — try auto-downgrade once before escalating
    auto_down = _attempt_auto_downgrade(app, attempts_max)
    if auto_down is not None:
        return auto_down

    msg = (
        f"✗ {app_name} could not be started after {attempts_max} attempts"
        f" — operator needed"
    )
    _emit("failed", app_name, attempts_max, "down", "n/a", msg, "error")
    return _result(app_name, "failed", attempts_max, "down", "n/a")


def _attempt_auto_downgrade(app: App, attempts: int) -> Optional[dict]:
    """Try one auto-downgrade to state.previous_version.

    Returns a result dict if a downgrade was attempted (success or failure),
    None if no previous_version was recorded (recovery falls through to the
    normal "operator needed" notification).
    """
    try:
        data = state.read(_STATE_PATH)
        entry = data.get("apps", {}).get(app.name, {})
        previous_version = entry.get("previous_version")
    except Exception:
        previous_version = None

    if not previous_version or not isinstance(previous_version, str):
        return None

    # Library/cron/systemd can downgrade. UCC class returns "not supported"
    # from lifecycle.downgrade — fall through to operator-needed in that case.
    if app.class_ == "ucc":
        return None

    try:
        result = lifecycle.downgrade(app, previous_version)
    except Exception as exc:
        msg = (
            f"✗ {app.name} auto-downgrade to {previous_version} failed "
            f"with exception: {exc} — operator needed"
        )
        _emit("auto_downgrade_failed", app.name, attempts, "down", "n/a",
              msg, "error")
        return _result(app.name, "auto_downgrade_failed", attempts, "down", "n/a")

    if result.ok:
        msg = (
            f"⚠ {app.name} auto-downgraded to {previous_version} after "
            f"{attempts} restart failures — operator review recommended"
        )
        _emit("auto_downgraded", app.name, attempts, "ok", "n/a",
              msg, "warning")
        return _result(app.name, "auto_downgraded", attempts, "ok", "n/a")

    msg = (
        f"✗ {app.name} auto-downgrade to {previous_version} failed: "
        f"{result.reason} — operator needed"
    )
    _emit("auto_downgrade_failed", app.name, attempts, "down", "n/a",
          msg, "error")
    return _result(app.name, "auto_downgrade_failed", attempts, "down", "n/a")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _result(app_name: str, event: str, attempts: int,
            final_health: str, kuma_status: str) -> dict:
    return {
        "app": app_name,
        "event": event,
        "attempts": attempts,
        "final_health": final_health,
        "kuma_status": kuma_status,
    }


def _emit(event: str, app_name: str, attempts: int,
          final_health: str, kuma_status: str,
          message: str, level: str) -> None:
    try:
        state.record(
            _STATE_PATH,
            app_name,
            event=event,
            attempts=attempts,
            final_health=final_health,
            kuma_status=kuma_status,
        )
    except Exception:
        pass
    try:
        notify.notify(message, level)
    except Exception:
        pass
