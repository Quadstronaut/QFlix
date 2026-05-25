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

import sys
from pathlib import Path
from typing import Optional


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
        if getattr(app, "class_", None) != "ucc":
            return False
        return ucc_active()
    except Exception as exc:
        print(f"WARNING: suppression.recovery_suppressed: unexpected error: {exc}",
              file=sys.stderr)
        return False
