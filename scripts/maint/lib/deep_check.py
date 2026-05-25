"""lib/deep_check.py — post-window deep-check autoheal sweep.

Probes every manifest app and fires trigger_async for each that is down.
This is the safety net that runs after a QFlix or UCC maintenance window
closes, recovering anything that was suppressed or queued during the window.

Public seam (pinned — sub-project B calls this exact signature):
    run_deep_check(*, reason: str, manifest=None, recover: bool = True) -> dict

Never raises. Best-effort per app. Appends to deep-check.jsonl.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lib import health, notify, recovery
from lib.manifest import Manifest


# ---------------------------------------------------------------------------
# State dir
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_deep_check(
    *,
    reason: str,
    manifest: Optional[Manifest] = None,
    recover: bool = True,
) -> dict:
    """Probe every manifest app; for each that is down, trigger recovery
    (now ungated). Best-effort; never raises.

    Returns {
        "reason": str,
        "ts": str (ISO UTC),
        "checked": int,
        "down": [app_name, ...],
        "recovery_triggered": {app_name: decision, ...},
        "skipped": [app_name, ...],   # probe error or recover=False
    }
    On manifest-load failure the dict also contains an "error" field.
    """
    ts = _utc_now_iso()

    # Load manifest if not provided
    if manifest is None:
        try:
            manifest = recovery._load_default_manifest()
        except Exception as exc:
            result: dict = {
                "reason": reason,
                "ts": ts,
                "checked": 0,
                "down": [],
                "recovery_triggered": {},
                "skipped": [],
                "error": str(exc),
            }
            try:
                notify.notify(
                    f"deep-check manifest load failed ({reason}): {exc}",
                    level="warning",
                )
            except Exception:
                pass
            _append_log(result)
            return result

    down: list[str] = []
    recovery_triggered: dict[str, str] = {}
    skipped: list[str] = []
    checked = 0

    for app in manifest.apps():
        checked += 1
        try:
            hr = health.probe(app)
        except Exception as exc:
            # Per-app probe failure: skip, don't crash the sweep
            skipped.append(app.name)
            continue

        if not hr.ok:
            down.append(app.name)
            if recover:
                try:
                    decision = recovery.trigger_async(app, manifest=manifest)
                except Exception as exc:
                    decision = f"trigger_error:{exc}"
                recovery_triggered[app.name] = decision
            else:
                skipped.append(app.name)

    # Summary notification
    if down:
        msg = (
            f"deep-check ({reason}): {len(down)} app(s) down, "
            f"recovery triggered for {list(recovery_triggered.keys())}"
        )
        level = "warning"
    else:
        msg = f"deep-check ({reason}): all {checked} apps healthy — no recovery needed"
        level = "info"

    try:
        notify.notify(msg, level=level)
    except Exception:
        pass

    result = {
        "reason": reason,
        "ts": ts,
        "checked": checked,
        "down": down,
        "recovery_triggered": recovery_triggered,
        "skipped": skipped,
    }
    _append_log(result)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _append_log(entry: dict) -> None:
    """Append a JSON line to deep-check.jsonl under MANITOBA_STATE_DIR."""
    try:
        sd = _state_dir()
        sd.mkdir(parents=True, exist_ok=True)
        log_path = sd / "deep-check.jsonl"
        line = json.dumps(entry, default=str)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass  # log write is best-effort
