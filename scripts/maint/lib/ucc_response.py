"""lib/ucc_response.py — Respond to UCC maintenance state transitions.

Called by `manitoba-maint ucc detect` after ucc.detect() returns. Keeps its
own cursor file (``ucc-response-state.json``) to detect edges:

  cursor False/absent → state active True  → clear→active edge:
    - pin Kuma status-page incident (B2)
    - fire "Upstream Maintenance Start" email (B3)
    - Discord notify

  cursor True → state active False  → active→clear edge:
    - unpin Kuma status-page incident (B2)
    - fire "Upstream Maintenance Complete" email (B3)
    - Discord notify
    - trigger deep-check (B→D seam)

  No change → no-op (idempotent — safe to call every cycle).

Every side-effect is best-effort; a failing effect never blocks others or the
cursor write. The cursor is written only after attempting all effects, so a
transient failure will retry on the next cycle.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Cursor file path helpers (mirrors ucc.py idiom)
# ---------------------------------------------------------------------------

_CURSOR_FILE = "ucc-response-state.json"


def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


def _default_cursor_path() -> Path:
    return _state_dir() / _CURSOR_FILE


def _read_cursor(path: Path) -> dict:
    """Read the cursor file. Returns {} on missing/corrupt (→ treat as no prior action)."""
    import json
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as exc:
        print(f"WARNING: ucc_response: cursor read failed ({path}): {exc}", file=sys.stderr)
        return {}


def _write_cursor(path: Path, data: dict) -> None:
    """Write cursor atomically (temp-file + os.replace), copying ucc.write_state idiom."""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        suffix=".tmp",
        prefix=path.name + ".",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        if os.name == "posix":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# B→D seam: trigger deep-check on the active→clear edge
# ---------------------------------------------------------------------------

def _trigger_deep_check(reason: str) -> None:
    """Invoke D's deep_check.run_deep_check on the clear edge.

    D's module does not exist on this branch (sub-project D builds it; it
    merges later). ImportError is expected and silently tolerated.
    """
    try:
        from lib import deep_check  # noqa: PLC0415 — lazy import by design
        deep_check.run_deep_check(reason=reason)
    except Exception as exc:
        # ImportError (D not merged yet) or any runtime error from D.
        # Log but never abort the cursor write or other side-effects.
        print(f"INFO: ucc_response: deep_check not available or raised: {exc}",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def respond(
    state: dict,
    *,
    response_state_path: Optional[Path] = None,
) -> dict:
    """Compare *state* to the cursor and fire side-effects on a detected edge.

    Parameters
    ----------
    state:
        The dict returned by ``ucc.detect()`` — must contain ``"active"``
        (bool). If missing, treated as False.
    response_state_path:
        Override for the cursor file path (for testing). Defaults to
        ``MANITOBA_STATE_DIR/ucc-response-state.json``.

    Returns
    -------
    dict with keys:
        ``edge``  — ``"clear_to_active"``, ``"active_to_clear"``, or ``"none"``
        ``cursor_was_active`` — bool, prior cursor value
        ``state_active``     — bool, current state value
    """
    if response_state_path is None:
        response_state_path = _default_cursor_path()

    cursor = _read_cursor(response_state_path)
    cursor_active = bool(cursor.get("active", False))
    state_active = bool(state.get("active", False))

    edge = "none"

    if not cursor_active and state_active:
        # clear → active
        edge = "clear_to_active"
        try:
            _fire_clear_to_active()
        except Exception as exc:
            print(f"WARNING: ucc_response: _fire_clear_to_active raised: {exc}", file=sys.stderr)

    elif cursor_active and not state_active:
        # active → clear
        edge = "active_to_clear"
        try:
            _fire_active_to_clear()
        except Exception as exc:
            print(f"WARNING: ucc_response: _fire_active_to_clear raised: {exc}", file=sys.stderr)

    # Write cursor after attempting all effects so a transient failure retries.
    try:
        _write_cursor(response_state_path, {"active": state_active})
    except Exception as exc:
        print(f"WARNING: ucc_response: cursor write failed: {exc}", file=sys.stderr)

    return {
        "edge": edge,
        "cursor_was_active": cursor_active,
        "state_active": state_active,
    }


def _fire_clear_to_active() -> None:
    """Side-effects for the clear→active edge. All best-effort."""
    # 1. Pin Kuma status-page incident.
    try:
        from lib import ucc_incident  # noqa: PLC0415
        ucc_incident.pin_maintenance_incident()
    except Exception as exc:
        print(f"WARNING: ucc_response: pin_maintenance_incident raised: {exc}", file=sys.stderr)

    # 2. Fire "Upstream Maintenance Start" email.
    try:
        from lib import listmonk  # noqa: PLC0415
        listmonk.fire_template_campaign(
            template_title="Upstream Maintenance Start",
            subject="QFlix — upstream provider maintenance in progress",
        )
    except Exception as exc:
        print(f"WARNING: ucc_response: listmonk start email raised: {exc}", file=sys.stderr)

    # 3. Discord notify.
    try:
        from lib import notify  # noqa: PLC0415
        notify.notify(
            "UCC upstream maintenance detected. Kuma incident pinned; subscriber email queued.",
            level="warning",
        )
    except Exception as exc:
        print(f"WARNING: ucc_response: notify (clear→active) raised: {exc}", file=sys.stderr)


def _fire_active_to_clear() -> None:
    """Side-effects for the active→clear edge. All best-effort."""
    # 1. Unpin Kuma status-page incident.
    try:
        from lib import ucc_incident  # noqa: PLC0415
        ucc_incident.clear_maintenance_incident()
    except Exception as exc:
        print(f"WARNING: ucc_response: clear_maintenance_incident raised: {exc}", file=sys.stderr)

    # 2. Fire "Upstream Maintenance Complete" email.
    try:
        from lib import listmonk  # noqa: PLC0415
        listmonk.fire_template_campaign(
            template_title="Upstream Maintenance Complete",
            subject="QFlix — upstream provider maintenance complete",
        )
    except Exception as exc:
        print(f"WARNING: ucc_response: listmonk complete email raised: {exc}", file=sys.stderr)

    # 3. Discord notify.
    try:
        from lib import notify  # noqa: PLC0415
        notify.notify(
            "UCC upstream maintenance cleared. Kuma incident unpinned; subscriber email queued.",
            level="info",
        )
    except Exception as exc:
        print(f"WARNING: ucc_response: notify (active→clear) raised: {exc}", file=sys.stderr)

    # 4. Trigger D's deep-check (B→D seam). Always last so other effects run even if D is absent.
    _trigger_deep_check("ucc-clear")
