"""QFLX-71: recovery must not blame Kuma for an app that is crash-looping.

2026-10-22 unpackerr panicked ~60s after every start (fake .iso torrent). The
recovery loop restarted it, probed inside the brief up-window, saw "healthy",
read Kuma "down", and paged "healthy locally but Kuma reports down - likely
routing/auth issue" every ~8 minutes for 5 hours. Kuma was right.

Offline: lifecycle, health, kuma, notify, state are all mocked.
"""
from __future__ import annotations

from unittest.mock import patch

import lib.lifecycle as lifecycle_mod
import lib.recovery as recovery_mod
from lib.health import HealthResult
from lib.lifecycle import LifecycleResult
from lib.manifest import App, HealthConfig
from lib.recovery import run

OK_LR = LifecycleResult(ok=True, duration_s=0.1, stdout="", stderr="", reason="ok")
UP = HealthResult(ok=True, latency_ms=5, reason="1 match(es)")
DOWN = HealthResult(ok=False, latency_ms=None, reason="no process matching pattern")


def _app(class_="systemd", unit="qflix-unpackerr.service"):
    raw = {"class": class_}
    if unit:
        raw["unit"] = unit
    return App(name="unpackerr", class_=class_, kuma_monitor="Unpackerr",
               health=HealthConfig(kind="process_pattern", raw={}),
               defaults={"recovery_attempts": 3, "recovery_backoff_s": [1],
                         "kuma_recheck_delay_s": 90, "lifecycle_timeout_s": 5},
               upgrade=None, raw=raw)


class _M:
    def __init__(self, app):
        self._a = app

    def app(self, name):
        if name != self._a.name:
            raise KeyError(name)
        return self._a


def _run(app, *, probes, restarts, kuma="down"):
    with patch("lib.recovery.lifecycle.restart", return_value=OK_LR), \
         patch("lib.recovery.lifecycle.auto_restarts", return_value=restarts), \
         patch("lib.recovery.health.probe", side_effect=probes), \
         patch("lib.recovery.kuma.monitor_status", return_value=kuma), \
         patch("lib.recovery._escalation_page_due", return_value=True), \
         patch("lib.recovery.notify.notify") as n, \
         patch("lib.recovery.state.record"), \
         patch("lib.recovery.time.sleep"):
        result = run(app.name, manifest=_M(app))
    return result, (n.call_args[0][0] if n.call_args else "")


def test_systemd_restart_count_proves_crash_loop():
    result, msg = _run(_app(), probes=[UP, UP], restarts=2)
    assert result["event"] == "crash_looping"
    assert "crash-looping" in msg and "2 auto-restart" in msg
    assert "routing" not in msg
    assert "qflix-unpackerr.service" in msg


def test_failed_reprobe_proves_crash_loop_without_systemd():
    result, msg = _run(_app(class_="ucc", unit=None), probes=[UP, DOWN], restarts=None)
    assert result["event"] == "crash_looping"
    assert "local probe failed again" in msg


def test_genuinely_healthy_still_reports_kuma_side():
    result, msg = _run(_app(), probes=[UP, UP], restarts=0)
    assert result["event"] == "healthy_locally_kuma_down"
    assert "routing" in msg


def test_kuma_up_short_circuits_before_crash_check():
    result, _ = _run(_app(), probes=[UP], restarts=5, kuma="up")
    assert result["event"] == "recovered"


def test_crash_looping_is_a_failure_event_for_reconcile():
    assert "crash_looping" in recovery_mod._FAILURE_EVENTS


# --- lifecycle.auto_restarts --------------------------------------------------

def _lr(stdout, ok=True):
    return LifecycleResult(ok=ok, duration_s=0.0, stdout=stdout, stderr="", reason="ok")


def test_auto_restarts_reads_nrestarts(monkeypatch):
    monkeypatch.delenv("MANITOBA_DRY_RUN", raising=False)
    seen = {}

    def fake(args, t):
        seen["args"] = args
        return _lr("4\n")

    with patch("lib.lifecycle._run", side_effect=fake):
        assert lifecycle_mod.auto_restarts(_app()) == 4
    assert seen["args"][-1] == "qflix-unpackerr.service"
    assert "NRestarts" in seen["args"]


def test_auto_restarts_unknown_is_none(monkeypatch):
    monkeypatch.delenv("MANITOBA_DRY_RUN", raising=False)
    with patch("lib.lifecycle._run", return_value=_lr("", ok=False)):
        assert lifecycle_mod.auto_restarts(_app()) is None
    with patch("lib.lifecycle._run", return_value=_lr("garbage")):
        assert lifecycle_mod.auto_restarts(_app()) is None
    assert lifecycle_mod.auto_restarts(_app(class_="ucc")) is None
    assert lifecycle_mod.auto_restarts(_app(unit=None)) is None
