"""lib/dormant_stop.py - put a woken dormant UCC container back to sleep (QFLX-55).

WHY THIS EXISTS: a converted app keeps its old UCC container installed and
stopped. On 2026-10-16 the box rebooted and Ultra.cc started the sonarr and
unpackerr containers again; two runtimes shared one config dir (one SQLite DB)
for four days. runtime_parity saw it (Kuma red from 10-19) but the pusher was
built to wait for the operator, and nobody came for two days.

Operator ruling 2026-10-21: migrated UCC apps get UNINSTALLED once parity is
verified (Unpackerr pilot first). Until each one is gone, this is the interim
remedy: on a "dormant container woken" violation the pusher calls
`appctl stop-dormant <app>` (UCC's `app-<slug> stop`, container only, never the
native unit), confirms the container is gone, and sends ONE Discord note.

Scope: only the woken-container predicate. process-trees and port-owner
violations still need the operator; stopping a container cannot fix those.

Never raises. Notes are deduped per app on a wall-clock ledger file (a pusher
restart must not re-send them) and FAIL OPEN: a broken ledger sends the note.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from lib import notify as notify_mod
from lib import runtime_parity as parity_mod

log = logging.getLogger(__name__)

WOKEN_TELL = "dormant container woken"
_STOP_TIMEOUT_S = 180.0       # app-<slug> stop has taken ~60s on the slot
_GONE_TIMEOUT_S = 90.0        # docker exit is asynchronous; state decides
_POLL_S = 5.0
_NOTE_COOLDOWN_S = float(os.environ.get("MANITOBA_DORMANT_STOP_COOLDOWN_S", str(24 * 3600)))

_in_flight: set[str] = set()
_mutex = threading.Lock()


def _appctl() -> str:
    return os.environ.get("MANITOBA_APPCTL", str(Path.home() / "bin" / "appctl"))


def _ledger_path() -> Path:
    # Lazy, not at import: tests isolate MANITOBA_STATE_DIR after import.
    base = os.environ.get("MANITOBA_STATE_DIR")
    return (Path(base) if base else Path.home() / ".opt" / "maint") / "dormant-stops.json"


def is_woken(violations: list[str]) -> bool:
    return any(v.startswith(WOKEN_TELL) for v in violations)


def _note_due(key: str) -> bool:
    """True (and stamps the ledger) if `key` has not been noted within the
    cooldown. Fails open: any error returns True."""
    path = _ledger_path()
    try:
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(ledger, dict):
                ledger = {}
        except FileNotFoundError:
            ledger = {}
        now = time.time()
        last = ledger.get(key)
        if isinstance(last, (int, float)) and 0 < now - last < _NOTE_COOLDOWN_S:
            return False
        ledger[key] = now
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log.warning("dormant-stop ledger unusable, noting anyway: %r", exc)
        return True


def _still_woken(app) -> bool:
    try:
        return bool(parity_mod.woken_container(parity_mod.Host(), app, os.getuid()))
    except Exception as exc:          # cannot look -> assume still woken (page)
        log.warning("dormant-stop %s: re-check failed: %r", app.name, exc)
        return True


def _worker(app) -> None:
    slug = app.raw.get("ucc_slug") or app.name
    unit = app.raw.get("unit") or "?"
    try:
        try:
            cp = subprocess.run([_appctl(), "stop-dormant", app.name], capture_output=True,
                                text=True, timeout=_STOP_TIMEOUT_S)
            rc, tail = cp.returncode, (cp.stdout + cp.stderr).strip()[-300:]
        except Exception as exc:
            rc, tail = -1, repr(exc)
        deadline = time.monotonic() + _GONE_TIMEOUT_S
        woken = _still_woken(app)
        while woken and time.monotonic() < deadline:
            time.sleep(_POLL_S)
            woken = _still_woken(app)
        if not woken:
            log.warning("dormant-stop %s: woken %s container stopped (appctl rc=%s)", app.name, slug, rc)
            if _note_due(app.name):
                notify_mod.notify(
                    f"QFlix: Ultra.cc woke the retired `{slug}` container, so two copies of "
                    f"{app.name} were running on one config. Stopped it automatically; the native "
                    f"`{unit}` was not touched. (Interim until {app.name} is uninstalled.)",
                    level="info")
        else:
            log.error("dormant-stop %s: container still running after stop (appctl rc=%s: %s)",
                      app.name, rc, tail)
            if _note_due(f"{app.name}:failed"):
                notify_mod.notify(
                    f"QFlix: automatic stop of the woken `{slug}` container FAILED "
                    f"(appctl stop-dormant rc={rc}: {tail or 'no output'}). Two copies of "
                    f"{app.name} may be sharing one config. Operator needed: "
                    f"`app-{slug} stop` (NOT `appctl stop`, which stops the native unit).",
                    level="error")
    except Exception as exc:
        log.error("dormant-stop %s: worker crashed: %r", app.name, exc)
    finally:
        with _mutex:
            _in_flight.discard(app.name)


def trigger_async(app) -> str:
    """Start one stop for `app` in the background. Returns "started" or
    "already_running" (one in flight per app). Never raises."""
    with _mutex:
        if app.name in _in_flight:
            return "already_running"
        _in_flight.add(app.name)
    try:
        threading.Thread(target=_worker, args=(app,), daemon=True,
                         name=f"dormant-stop-{app.name}").start()
        return "started"
    except Exception as exc:
        with _mutex:
            _in_flight.discard(app.name)
        log.error("dormant-stop %s: could not start worker: %r", app.name, exc)
        return "error"
