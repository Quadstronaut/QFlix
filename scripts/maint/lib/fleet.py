"""lib/fleet.py — Correlated-alert collapse (fleet dead-man, sub-project C).

Detects mass-down "storm" events: when too many pushed-app monitors are failing
simultaneously, collapse them into one aggregate signal ("QFlix Fleet") instead
of paging the operator N times for a single correlated event.

Threshold: FLEET_STORM_THRESHOLD (env MANITOBA_FLEET_STORM_THRESHOLD, default 8).
Fleet is ~33 pushed app monitors; 8 simultaneously-down (~25%) distinguishes a
correlated storm from a handful of independent outages.

State file: ~/.opt/maint/fleet-window.json
  {storm_active, down_count, total, since, last_eval_at}
Atomic write (same idiom as ucc.write_state). Corrupt/missing → fresh start.
evaluate() never raises.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------

FLEET_STORM_THRESHOLD: int = int(os.environ.get("MANITOBA_FLEET_STORM_THRESHOLD", "8"))


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


def _default_state_path() -> Path:
    return _state_dir() / "fleet-window.json"


# ---------------------------------------------------------------------------
# State I/O (mirrors ucc.write_state idiom)
# ---------------------------------------------------------------------------

def _read_state(path: Path) -> dict:
    """Read fleet-window.json. Returns {} on missing/corrupt."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state root is not a JSON object")
        return data
    except Exception as exc:
        print(f"WARNING: fleet state read failed ({path}): {exc}", file=sys.stderr)
        return {}


def _write_state(path: Path, data: dict) -> None:
    """Write *data* to *path* atomically (temp-file + os.replace)."""
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
# Core API
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def evaluate(
    results: dict[str, str],
    *,
    probe_ok: dict[str, bool],
    state_path: Optional[Path] = None,
) -> dict:
    """Given this cycle's per-app health, update fleet-window.json and return:
      {"down_count", "total", "storm_active", "edge"}
      where edge in {None, "onset", "clear"}.

    Storm is active when down_count >= FLEET_STORM_THRESHOLD.
    Edge fires only on state transition (non-storm→storm = 'onset',
    storm→non-storm = 'clear'). Edge never repeats mid-storm/mid-calm.

    Never raises; state read/write is best-effort.
    """
    if state_path is None:
        state_path = _default_state_path()

    # Count using probe_ok (authoritative bool map); fall back to results if needed
    if probe_ok:
        down_count = sum(1 for ok in probe_ok.values() if not ok)
        total = len(probe_ok)
    else:
        down_count = 0
        total = 0

    # Load prior state (corrupt/missing → fresh start = no prior storm)
    try:
        prior = _read_state(state_path)
    except Exception:
        prior = {}

    was_storm = bool(prior.get("storm_active", False))
    now = _now_iso()

    storm_active = down_count >= FLEET_STORM_THRESHOLD

    # Determine edge (only fires on transitions)
    edge: Optional[str] = None
    if storm_active and not was_storm:
        edge = "onset"
    elif not storm_active and was_storm:
        edge = "clear"

    # Build new state
    new_state: dict = {
        "storm_active": storm_active,
        "down_count": down_count,
        "total": total,
        "last_eval_at": now,
    }
    # Preserve "since" timestamp: set on onset, clear on clear, keep during sustain
    if edge == "onset":
        new_state["since"] = now
    elif edge == "clear":
        new_state["since"] = None
    else:
        new_state["since"] = prior.get("since")

    # Persist (best-effort)
    try:
        _write_state(state_path, new_state)
    except Exception as exc:
        print(f"WARNING: fleet state write failed ({state_path}): {exc}", file=sys.stderr)

    return {
        "down_count": down_count,
        "total": total,
        "storm_active": storm_active,
        "edge": edge,
    }
