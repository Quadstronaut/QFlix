"""lib/suppression.py — UCC-maintenance recovery suppression predicates.

Provides a shared predicate both recovery entry points (pusher + kuma webhook)
consult to decide whether to skip triggering recovery while UCC is in
maintenance.

Design rationale (from spec):
- systemd/cron apps use `systemctl --user` (not the gated `app-*` wrapper),
  so their recovery still works during UCC maintenance. Suppressing them would
  needlessly delay legitimate heals.
- ucc-class apps CAN'T be started while the gate is up (`app-* start` is
  gated), so recovery would only churn to permanently-failed and page the
  operator. D's deep-check is the safety net for these once the gate lifts.
- Suppression predicate returns False on any read error (fail toward normal
  recovery, not toward silent suppression).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Manual push-suppression registry (under MANITOBA_STATE_DIR). Maps app.name →
# {"reason": str, "since": iso}. When an app is listed here the pusher pushes
# it UP (with a [SUPPRESSED] note) and skips probe/recovery — used to mute a
# monitor for an app awaiting an upstream fix, without touching Kuma's admin
# API (operator-only). A self-destructing watcher removes the entry once the
# app is live again.
_PUSH_SUPPRESS_FILE = "push-suppress.json"

# Lockfile written by the window orchestrator (lib/window.py) for the duration
# of the Monday maintenance window — which overlaps the 11:30 UTC cp-upgrade
# sweep that stops/upgrades/restarts apps on purpose.
_WINDOW_LOCK_FILE = "lock"


def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    return Path(env) if env else Path.home() / ".opt" / "maint"


def in_maintenance_window(*, state_dir: Optional[Path] = None) -> bool:
    """True iff the weekly maintenance-window lockfile is present.

    The window orchestrator holds ``$STATE_DIR/lock`` for the whole Monday
    window, which overlaps the cp-upgrade sweep (apps are intentionally cycled).
    BOTH recovery entry points must consult this so neither restarts an app the
    window is mid-upgrade on. The Kuma webhook (lib/kuma.do_POST) already does;
    the pusher — the operative auto-heal path — must too.

    Simple existence check, mirroring kuma.do_POST: the standalone
    window-watchdog clears a stale lock at 15:00 UTC, so a lingering lock can't
    silence alerting indefinitely. Fail-open: any error → False (recover as
    normal), never silence a real outage on a path glitch.
    """
    try:
        sd = state_dir if state_dir is not None else _state_dir()
        return (sd / _WINDOW_LOCK_FILE).exists()
    except Exception as exc:
        print(f"WARNING: suppression.in_maintenance_window: {exc}", file=sys.stderr)
        return False


def push_suppressed(app_name: str) -> Optional[str]:
    """Return the suppression reason if *app_name* is in the push-suppress
    registry, else None. Best-effort; None on any error (fail toward normal
    alerting, never toward silent suppression)."""
    try:
        path = _state_dir() / _PUSH_SUPPRESS_FILE
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get(app_name)
        if not entry:
            return None
        if isinstance(entry, dict):
            return entry.get("reason") or "suppressed"
        return str(entry)
    except Exception as exc:
        print(f"WARNING: suppression.push_suppressed({app_name}): {exc}",
              file=sys.stderr)
        return None


def in_pause_window(app, *, now: Optional[datetime] = None) -> bool:
    """True iff *app* declares a pause_window and the current UTC hour is inside
    it — i.e. the unit is INTENTIONALLY stopped right now (e.g. tdarr-node during
    fair-use quiet hours). The canonical predicate consulted by BOTH recovery
    entry points (pusher auto-heal + recovery.trigger_async, which also covers
    deep_check and the Kuma webhook) so no path revives a deliberately-paused app.

    `now` is for test injection. A naive datetime is read as UTC (never the host's
    local time) so behavior is identical on the CEST seedbox and a UTC-N CI host.
    Fail-open: any error → False (probe/recover as normal), never silence a real
    outage on a parse glitch.
    """
    pw = getattr(app, "pause_window", None)
    if pw is None:
        return False
    try:
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return bool(pw.contains(now.astimezone(timezone.utc).hour))
    except Exception as exc:
        print(f"WARNING: suppression.in_pause_window: {exc}", file=sys.stderr)
        return False


def ucc_active(*, state_path: Optional[Path] = None) -> bool:
    """True iff A's ucc-window.json says ``active``. Best-effort; False on any error."""
    try:
        from lib import ucc as ucc_mod
        s = ucc_mod.status(state_path=state_path)
        return bool(s.get("active", False))
    except Exception as exc:
        print(f"WARNING: suppression.ucc_active: could not read UCC state: {exc}",
              file=sys.stderr)
        return False


def recovery_suppressed(app) -> bool:
    """True iff recovery for *app* should be skipped right now.

    Currently: ``app.class_ == 'ucc'`` AND ``ucc_active()``.

    Returns False on any error — fail toward normal recovery, never toward
    silent suppression.
    """
    try:
        # Manual push-suppression mutes recovery too (the app is knowingly
        # down, e.g. awaiting an upstream fix) — defensive belt-and-braces so
        # recovery is skipped even if some path probes the app directly.
        if push_suppressed(getattr(app, "name", "")):
            return True
        # A pending-swap app (QFLX-25, spec 5.9 step 7) is still served by its
        # UCC container, so the UCC gate blocks its recovery exactly as before.
        raw = getattr(app, "raw", None) or {}
        pending = isinstance(raw, dict) and raw.get("swap_state") == "pending-swap"
        if getattr(app, "class_", None) != "ucc" and not pending:
            return False
        return ucc_active()
    except Exception as exc:
        print(f"WARNING: suppression.recovery_suppressed: unexpected error: {exc}",
              file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Writer + CLI (QFLX-25, spec 5.9 steps 4 / 9 and rollback step 0)
# ---------------------------------------------------------------------------
# A UCC->native swap mutes the app's Kuma monitor AND its behavioural canaries
# (canary keys are `canary-<name>`, cli.py) for the planned outage, then lifts
# them together. The registry is shared, so the whole read-modify-write runs
# under an exclusive flock and lands via mkstemp + os.replace (a reader never
# sees a half-written file). Contention past the timeout or an unparseable
# registry is a REFUSAL (exit 2): merging onto {} would silently drop someone
# else's suppression, and a swap must not proceed unsuppressed (pusher
# recovery would otherwise restart the app mid-swap). Lock INFRASTRUCTURE
# missing (no fcntl on a workstation) fails open, as in swapstate.py.
#
#   python3 suppression.py add NAME... --reason TEXT    exit 0 ok, 2 refused
#   python3 suppression.py remove NAME...               exit 0 ok, 2 refused
#   python3 suppression.py has NAME                     exit 0 listed, 1 not

_WRITE_LOCK_TIMEOUT_S = 10.0


class SuppressionWriteError(RuntimeError):
    """Registry unreadable, lock contended, or the write failed."""


def _locked_rmw(mutate) -> dict:
    import tempfile
    import time
    try:
        import fcntl
    except ImportError:            # non-POSIX workstation: no lock (fail open)
        fcntl = None               # type: ignore[assignment]
    sd = _state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    path = sd / _PUSH_SUPPRESS_FILE
    lock = open(sd / (_PUSH_SUPPRESS_FILE + ".lock"), "a+")
    try:
        if fcntl is not None:
            deadline = time.monotonic() + _WRITE_LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise SuppressionWriteError(f"{path}: lock contended")
                    time.sleep(0.1)
        try:
            cur = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError) as exc:
            raise SuppressionWriteError(f"{path} unreadable ({exc}); refusing to overwrite")
        if not isinstance(cur, dict):
            raise SuppressionWriteError(f"{path} is not a JSON object; refusing to overwrite")
        new = mutate(dict(cur))
        fd, tmp = tempfile.mkstemp(dir=str(sd), prefix=".push-suppress.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(new, indent=2, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return new
    finally:
        lock.close()


def add_push_suppress(names, reason: str) -> dict:
    """Mute *names*. Idempotent: an existing entry keeps its original `since`."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def m(cur):
        for n in names:
            if not isinstance(cur.get(n), dict):
                cur[n] = {"reason": reason, "since": now}
        return cur
    return _locked_rmw(m)


def remove_push_suppress(names) -> dict:
    def m(cur):
        for n in names:
            cur.pop(n, None)
        return cur
    return _locked_rmw(m)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="push-suppress.json writer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("names", nargs="+")
    a.add_argument("--reason", required=True)
    r = sub.add_parser("remove")
    r.add_argument("names", nargs="+")
    h = sub.add_parser("has")
    h.add_argument("name")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "add":
            add_push_suppress(args.names, args.reason)
            print("suppressed: " + " ".join(args.names))
            return 0
        if args.cmd == "remove":
            remove_push_suppress(args.names)
            print("unsuppressed: " + " ".join(args.names))
            return 0
        return 0 if push_suppressed(args.name) else 1
    except (SuppressionWriteError, OSError) as exc:
        print(f"suppression: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
