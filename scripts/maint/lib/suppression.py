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
from pathlib import Path
from typing import Optional

# Manual push-suppression registry (under MANITOBA_STATE_DIR). Maps app.name →
# {"reason": str, "since": iso}. When an app is listed here the pusher pushes
# it UP (with a [SUPPRESSED] note) and skips probe/recovery — used to mute a
# monitor for an app awaiting an upstream fix, without touching Kuma's admin
# API (operator-only). A self-destructing watcher removes the entry once the
# app is live again.
_PUSH_SUPPRESS_FILE = "push-suppress.json"


def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    return Path(env) if env else Path.home() / ".opt" / "maint"


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
        if getattr(app, "class_", None) != "ucc":
            return False
        return ucc_active()
    except Exception as exc:
        print(f"WARNING: suppression.recovery_suppressed: unexpected error: {exc}",
              file=sys.stderr)
        return False
