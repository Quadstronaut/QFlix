"""lib/ucc.py — UCC upstream-maintenance detection and state management.

Single responsibility: probe the UCC lifecycle gate, classify the result,
maintain ucc-window.json, and emit edge transitions. Does NOT send email,
pin Kuma incidents, or run heals.

Probe
-----
Runs `app-<probe_app> start` via subprocess (timeout 15s). probe_app
resolves from secret `ucc.probe_app` → fallback `kavita`.

Classification
--------------
- ``gated``      — JSON result==false AND message matches /maintenance/i.
- ``clear``      — JSON result==true, OR empty stdout with rc 0 (the
                   silent-success signature of app-manager >=2026.05.22).
- ``probe-error``— timeout, non-JSON, empty stdout with non-zero rc,
                   SSH/host stall, unknown-app / not-installed. No state change.

Debounce
--------
- clear → active : single gated probe (immediate).
- active → clear : requires UCC_CLEAR_DEBOUNCE consecutive clear probes.
- probe-error    : holds last state and increments consecutive_error --
                   UNLESS the hold has lasted UCC_PROBE_ERROR_CAP consecutive
                   errors while active, in which case it fails OPEN (forces
                   active -> False) rather than holding forever. See the
                   FIX comment at UCC_PROBE_ERROR_CAP and in detect() for the
                   2026-07-29 audit finding this closes: consecutive_error
                   had no upper bound, so a dead/unreachable probe could
                   freeze the fleet-wide auto-recovery gate ON indefinitely
                   (suppression.ucc_active() reads this same `active` flag).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Number of consecutive clear probes needed to flip active → False.
UCC_CLEAR_DEBOUNCE: int = 3

# FIX (audit, 2026-07-29): number of consecutive probe-error cycles allowed
# before we stop trusting a frozen `active=True` and fail OPEN.
#
# Before this cap, the probe-error branch held `active` frozen forever with
# NO upper bound on consecutive_error -- a production instance was found at
# consecutive_error == 128 (~10.6h at the 5-min timer cadence, see
# manitoba-maint-ucc-detect.timer) with no code path anywhere in
# scripts/maint or scripts/canaries ever reading consecutive_error to break
# the freeze. suppression.ucc_active() / recovery_suppressed() read that
# same frozen `active` flag on every webhook down-event, so a probe that
# merely can't reach the host (SSH stall, host overload -- NOT necessarily
# a real UCC gate) silently disabled auto-recovery for every ucc-class app,
# fleet-wide, for as long as the probe stayed broken.
#
# 3 cycles = 15 minutes at the 5-min cadence: the same order of magnitude as
# UCC_CLEAR_DEBOUNCE (3 x 5min = 15min) already assumes is enough to rule
# out a single host-load blip, so it does not fire open on the transient
# noise the debounce elsewhere already tolerates -- but it is short enough
# that a genuinely dead probe doesn't leave recovery suppressed for hours.
UCC_PROBE_ERROR_CAP: int = 3

# Probe subprocess timeout in seconds.
_PROBE_TIMEOUT_S: int = 15

# Default probe app when the secret is not set.
_DEFAULT_PROBE_APP: str = "kavita"

# State file name (under MANITOBA_STATE_DIR).
_STATE_FILE = "ucc-window.json"

# Transitions log file name.
_TRANSITIONS_LOG = "ucc-transitions.jsonl"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


def _default_state_path() -> Path:
    return _state_dir() / _STATE_FILE


def _transitions_log_path() -> Path:
    return _state_dir() / _TRANSITIONS_LOG


# ---------------------------------------------------------------------------
# State read / write
# ---------------------------------------------------------------------------

def read_state(path: Path) -> dict:
    """Read ucc-window.json from *path*. Returns {} on missing/corrupt files."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state root is not a JSON object")
        return data
    except Exception as exc:
        print(f"WARNING: ucc state read failed ({path}): {exc}", file=sys.stderr)
        return {}


def write_state(path: Path, data: dict) -> None:
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
# Classification
# ---------------------------------------------------------------------------

def classify(output: str, returncode: int = 0) -> str:
    """Classify raw stdout from `app-<name> start`.

    Returns one of: ``"gated"``, ``"clear"``, ``"probe-error"``.

    NOTE 2026-05-25 (app-manager v2026.05.22): write-ops (start/stop/restart)
    are now SILENT on success — empty stdout, exit 0 — instead of returning
    ``{"result": true}``. The maintenance *rejection* path still returns the
    gated JSON (``result:false`` + "due to maintenance"). So an empty stdout
    with rc 0 means "the lifecycle CLI accepted the command = NOT gated" and
    must classify as ``clear``; without this, the gate sticks ``active``
    forever (probe-error never satisfies the clear debounce).
    """
    output = output.strip()

    # Empty stdout: silent success (rc 0 → clear) vs. genuine failure
    # (rc != 0 → probe-error). Timeouts/OS errors are handled in probe().
    if output == "":
        return "clear" if returncode == 0 else "probe-error"

    # Try to parse as JSON first.
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        # Non-JSON → unknown host/app response → probe-error.
        return "probe-error"

    if not isinstance(parsed, dict):
        return "probe-error"

    result = parsed.get("result")
    if result is False:
        # Only classify as gated when the maintenance message is present.
        msg = ""
        data = parsed.get("data")
        if isinstance(data, dict):
            msg = data.get("message", "") or ""
        if re.search(r"due to maintenance", msg, re.IGNORECASE):
            return "gated"
        # result==false but no maintenance text → unknown error state → probe-error.
        return "probe-error"

    if result is True:
        return "clear"

    # Any other value → probe-error.
    return "probe-error"


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

def probe(*, probe_app: Optional[str] = None) -> tuple[str, str, str]:
    """Run one `app-<probe_app> start` probe.

    Returns ``(classification, probe_op, raw_output)`` where *classification*
    is one of ``"gated"``, ``"clear"``, ``"probe-error"``.
    """
    if probe_app is None:
        # Resolve probe_app: secret → default.
        try:
            from lib.secrets import read_secret
            probe_app = read_secret("ucc.probe_app")
        except (FileNotFoundError, Exception):
            probe_app = _DEFAULT_PROBE_APP

    cmd = ["app-" + probe_app, "start"]
    probe_op = " ".join(cmd)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
        raw = result.stdout or ""
        rc = result.returncode
    except subprocess.TimeoutExpired as exc:
        return "probe-error", probe_op, f"timeout after {exc.timeout}s"
    except OSError as exc:
        return "probe-error", probe_op, f"os error: {exc}"
    except Exception as exc:
        return "probe-error", probe_op, f"unexpected error: {exc}"

    return classify(raw, rc), probe_op, raw


# ---------------------------------------------------------------------------
# Transitions log
# ---------------------------------------------------------------------------

def _append_transition(
    log_path: Path,
    from_state: str,
    to_state: str,
    probe_op: str,
    *,
    detail: Optional[str] = None,
) -> None:
    """Append one JSONL record to the transitions log. Best-effort."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now = _now_iso()
        record = {
            "timestamp": now,
            "from": from_state,
            "to": to_state,
            "probe_op": probe_op,
        }
        if detail is not None:
            record["detail"] = detail
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        print(f"WARNING: could not write transitions log: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _on_edge(
    from_label: str,
    to_label: str,
    probe_op: str,
    *,
    detail: Optional[str] = None,
) -> None:
    """Fire best-effort side-effects on a state-machine edge.

    Both the notify call and the transitions-log write are best-effort;
    neither must abort the state write that follows.
    """
    transitions_path = _transitions_log_path()

    # Transitions log (always first — cheaper than network).
    _append_transition(transitions_path, from_label, to_label, probe_op, detail=detail)

    # Discord notification — best-effort.
    try:
        from lib import notify as notify_mod
        if to_label == "active":
            msg = f"UCC maintenance gate detected (`{probe_op}` returned gated). Window marked active."
            level = "warning"
        elif to_label == "clear-failopen":
            # Distinct from a debounce-confirmed clear: we never SAW a clear
            # probe here, we gave up trusting the frozen `active` flag after
            # too many consecutive probe errors (see UCC_PROBE_ERROR_CAP).
            # Flagged "warning" (not "info") because it's an unconfirmed
            # guess, not a verified all-clear -- the operator should know
            # recovery is being un-suppressed on a probe outage, not on
            # evidence UCC actually lifted the gate.
            msg = (
                f"UCC probe-error cap reached ({detail}) -- failing OPEN: "
                f"`{probe_op}` has been erroring, not confirming clear, so we "
                f"stopped trusting the frozen `active` flag and un-suppressed "
                f"recovery rather than risk holding it forever. If UCC is "
                f"still actually gated, the next successful probe re-flips "
                f"active immediately."
            )
            level = "warning"
        else:
            msg = f"UCC maintenance window cleared (`{probe_op}` confirmed clear x{UCC_CLEAR_DEBOUNCE}). Window deactivated."
            level = "info"
        notify_mod.notify(msg, level=level)
    except Exception as exc:
        print(f"WARNING: notify failed on UCC edge ({from_label}→{to_label}): {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def detect(
    *,
    state_path: Optional[Path] = None,
    probe_app: Optional[str] = None,
) -> dict:
    """Run one probe cycle: probe → classify → update state → emit edge.

    Returns the new state dict. All edge side-effects (notify, transitions
    log) are best-effort and never abort the state write.
    """
    if state_path is None:
        state_path = _default_state_path()

    # Load prior state (corrupt/missing → fresh start).
    prior = read_state(state_path)
    was_active = bool(prior.get("active", False))
    consecutive_clear = int(prior.get("consecutive_clear", 0))
    consecutive_error = int(prior.get("consecutive_error", 0))
    first_detected_at = prior.get("first_detected_at")

    # Run the probe.
    classification, probe_op_str, _raw = probe(probe_app=probe_app)

    now = _now_iso()

    # Build updated state from prior, preserving fields that don't change.
    # Ensure baseline fields are always present even on a fresh start.
    state: dict = {
        "active": was_active,
        "consecutive_clear": consecutive_clear,
        "consecutive_error": consecutive_error,
        **prior,
        "last_probe_at": now,
        "last_probe_result": classification,
        "probe_op": probe_op_str,
    }

    edge_from: Optional[str] = None
    edge_to: Optional[str] = None
    edge_detail: Optional[str] = None

    if classification == "probe-error":
        # Hold last state — increment error counter only; reset nothing.
        new_consecutive_error = consecutive_error + 1
        state["consecutive_error"] = new_consecutive_error
        # (consecutive_clear is intentionally unchanged)

        # FIX (audit, 2026-07-29): cap the hold — see UCC_PROBE_ERROR_CAP.
        # Only relevant while active=True (a frozen `active=False` is
        # already the non-suppressing state; nothing to fail open from).
        # Fail OPEN (force active=False) rather than fail CLOSED (the old,
        # unbounded hold): a probe that can't run tells us nothing about
        # whether the gate is really up, so trusting the stale True is no
        # better a guess than False — and False is the direction that
        # doesn't leave every ucc-class app's recovery suppressed
        # indefinitely. Worst case on a wrong guess here: one harmless
        # recovery attempt against a still-gated host, which the gated
        # lifecycle CLI simply no-ops on (see module docstring).
        if was_active and new_consecutive_error >= UCC_PROBE_ERROR_CAP:
            state["active"] = False
            edge_from = "active"
            edge_to = "clear-failopen"
            edge_detail = (
                f"{new_consecutive_error} consecutive probe errors "
                f">= cap {UCC_PROBE_ERROR_CAP}"
            )

    elif classification == "gated":
        state["consecutive_error"] = 0
        state["consecutive_clear"] = 0  # reset any in-progress clear run

        if not was_active:
            # clear → active (immediate flip on first gated probe).
            state["active"] = True
            state["first_detected_at"] = now
            state["last_confirmed_at"] = now
            edge_from = "clear"
            edge_to = "active"
        else:
            # Already active — update confirmation timestamp.
            state["last_confirmed_at"] = now

    elif classification == "clear":
        state["consecutive_error"] = 0
        new_consecutive_clear = consecutive_clear + 1
        state["consecutive_clear"] = new_consecutive_clear

        if was_active:
            if new_consecutive_clear >= UCC_CLEAR_DEBOUNCE:
                # active → clear (debounce satisfied).
                state["active"] = False
                state["consecutive_clear"] = 0  # reset after flip
                edge_from = "active"
                edge_to = "clear"
            # else: still in debounce — stay active, counter already updated above.
        # If already inactive and clear, nothing changes except the counter
        # (which is harmless to increment and will stay at 0 due to inactive).
        # Reset it to 0 while inactive to avoid spurious accumulation.
        else:
            state["consecutive_clear"] = 0

    # Emit edge side-effects BEFORE writing state so a crash in side-effects
    # doesn't leave a written state with no log/notify. Side-effects are
    # best-effort — if they raise, we catch and continue.
    if edge_from is not None and edge_to is not None:
        try:
            _on_edge(edge_from, edge_to, probe_op_str, detail=edge_detail)
        except Exception as exc:
            print(f"WARNING: edge side-effects failed: {exc}", file=sys.stderr)

    # Atomic state write.
    write_state(state_path, state)

    return state


# ---------------------------------------------------------------------------
# Status (read-only)
# ---------------------------------------------------------------------------

def status(*, state_path: Optional[Path] = None) -> dict:
    """Return the current UCC window state dict without probing."""
    if state_path is None:
        state_path = _default_state_path()
    return read_state(state_path)
