"""tests/unit/test_recovery_escalation_page_cooldown.py

The 2026-09-02 Plex outage produced 33 identical Discord pages in ten hours.

Root cause: the permanent-failure latch (`_PERMANENT_FAILURE_REARM_S`, 900s)
auto-re-arms so a transient root cause can still recover without an operator.
That re-arm is correct and stays. What was wrong is that every re-arm re-ran
the 3-attempt loop and emitted a FULL `error`-level operator page at the end
of it — 900s latch + ~180s loop = one identical "operator needed" ping every
~18 minutes, forever, for a single unchanged fault.

Retrying is not the bug. Re-PAGING is. These tests pin the split: the loop
keeps running on its own cadence, the operator hears about it once per
cooldown, and a genuinely new outage pages immediately because recovery
clears the stamp.
"""
from __future__ import annotations

from typing import Optional
from unittest.mock import patch

import pytest

import lib.recovery as recovery_mod
from lib.recovery import run
from lib.manifest import App, HealthConfig
from lib.health import HealthResult
from lib.lifecycle import LifecycleResult


def _make_app(name: str = "plex", *, class_: str = "ucc",
              kuma_monitor: Optional[str] = "Plex") -> App:
    return App(
        name=name,
        class_=class_,
        kuma_monitor=kuma_monitor,
        health=HealthConfig(kind="http_api", raw={}),
        defaults={
            "recovery_attempts": 3,
            "recovery_backoff_s": [10, 30, 60],
            "kuma_recheck_delay_s": 90,
            "lifecycle_timeout_s": 60,
        },
        upgrade=None,
        raw={"class": class_},
    )


class _FakeManifest:
    def __init__(self, apps: dict):
        self._apps = apps

    def app(self, name: str) -> App:
        if name not in self._apps:
            raise KeyError(name)
        return self._apps[name]


def _ok_lifecycle() -> LifecycleResult:
    return LifecycleResult(ok=True, duration_s=0.1, stdout="", stderr="", reason="ok")


def _fail_health() -> HealthResult:
    return HealthResult(ok=False, latency_ms=None, reason="connection refused")


def _ok_health() -> HealthResult:
    return HealthResult(ok=True, latency_ms=10, reason="ok")


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """Point the escalation ledger and state file at tmp_path, and start
    every test from an empty ledger (the module caches nothing, but the
    real ~/.opt/maint path must never be touched from a unit test)."""
    monkeypatch.setattr(recovery_mod, "_ESCALATION_PAGE_LEDGER",
                        tmp_path / "escalation-pages.json")
    monkeypatch.setattr(recovery_mod, "_STATE_PATH", tmp_path / "state.json")
    recovery_mod.clear_permanent_failure("plex")
    yield


def _run_failed_recovery(manifest, app_name="plex"):
    with patch("lib.recovery.lifecycle.restart", return_value=_ok_lifecycle()), \
         patch("lib.recovery.health.probe", return_value=_fail_health()), \
         patch("lib.recovery.kuma.monitor_status", return_value="down"), \
         patch("lib.recovery.notify.notify") as mock_notify, \
         patch("lib.recovery.time.sleep"):
        result = run(app_name, manifest=manifest)
    return result, mock_notify


def _operator_pages(mock_notify):
    """Only the error-level 'operator needed' escalation counts as a page."""
    return [c for c in mock_notify.call_args_list
            if "operator needed" in str(c.args[0])]


class TestEscalationPageCooldown:

    def test_first_terminal_failure_pages(self):
        manifest = _FakeManifest({"plex": _make_app()})
        result, mock_notify = _run_failed_recovery(manifest)

        assert result["event"] == "failed"
        assert len(_operator_pages(mock_notify)) == 1

    def test_second_terminal_failure_inside_cooldown_does_not_page(self):
        """The exact 2026-09-02 shape: same app, same unchanged fault, the
        loop runs again after the latch re-arms. State is still recorded;
        the operator is not pinged a second time."""
        manifest = _FakeManifest({"plex": _make_app()})

        _, first = _run_failed_recovery(manifest)
        assert len(_operator_pages(first)) == 1

        recovery_mod.clear_permanent_failure("plex")  # latch auto-re-arm
        result, second = _run_failed_recovery(manifest)

        assert result["event"] == "failed", "the loop must still run and still fail"
        assert _operator_pages(second) == [], "duplicate page inside cooldown"

    def test_ten_hours_of_re_arms_page_once(self, monkeypatch):
        """33 loops (the measured 2026-09-02 count) => exactly 1 page."""
        manifest = _FakeManifest({"plex": _make_app()})
        for _ in range(33):
            recovery_mod.clear_permanent_failure("plex")
            _, mock_notify = _run_failed_recovery(manifest)
            pages = _operator_pages(mock_notify)
            total = getattr(self, "_total", 0) + len(pages)
            self._total = total
        assert self._total == 1

    def test_page_returns_after_cooldown_elapses(self, monkeypatch):
        """A fault that is STILL broken a day later must be re-surfaced —
        silence forever would be the opposite failure."""
        manifest = _FakeManifest({"plex": _make_app()})
        _, first = _run_failed_recovery(manifest)
        assert len(_operator_pages(first)) == 1

        monkeypatch.setattr(recovery_mod, "_ESCALATION_PAGE_COOLDOWN_S", 0.0)
        recovery_mod.clear_permanent_failure("plex")
        _, second = _run_failed_recovery(manifest)
        assert len(_operator_pages(second)) == 1

    def test_recovery_clears_the_stamp_so_a_new_outage_pages_immediately(self):
        """Cooldown is per-OUTAGE, not per-wall-clock-day. Once the app comes
        back, the next failure is news again."""
        manifest = _FakeManifest({"plex": _make_app()})
        _, first = _run_failed_recovery(manifest)
        assert len(_operator_pages(first)) == 1

        recovery_mod.clear_escalation_page("plex")  # what the pusher calls on probe OK

        recovery_mod.clear_permanent_failure("plex")
        _, second = _run_failed_recovery(manifest)
        assert len(_operator_pages(second)) == 1

    def test_cooldown_is_per_app(self):
        """A Plex page must not mute a Sonarr page."""
        manifest = _FakeManifest({"plex": _make_app(),
                                  "sonarr": _make_app("sonarr", kuma_monitor="Sonarr")})
        _, p = _run_failed_recovery(manifest, "plex")
        assert len(_operator_pages(p)) == 1

        _, s = _run_failed_recovery(manifest, "sonarr")
        assert len(_operator_pages(s)) == 1

    def test_suppressed_page_still_records_state(self):
        """Suppressing the ping must not suppress the RECORD — state.json is
        the durable audit trail the runbook reads."""
        manifest = _FakeManifest({"plex": _make_app()})
        _run_failed_recovery(manifest)

        recovery_mod.clear_permanent_failure("plex")
        with patch("lib.recovery.lifecycle.restart", return_value=_ok_lifecycle()), \
             patch("lib.recovery.health.probe", return_value=_fail_health()), \
             patch("lib.recovery.kuma.monitor_status", return_value="down"), \
             patch("lib.recovery.notify.notify"), \
             patch("lib.recovery.state.record") as mock_state, \
             patch("lib.recovery.time.sleep"):
            run("plex", manifest=manifest)

        events = [c.kwargs.get("event") for c in mock_state.call_args_list]
        assert "failed" in events

    def test_ledger_survives_process_restart(self, tmp_path):
        """The maint daemon restarts; the cooldown must not reset with it.
        Wall-clock in a file, not monotonic in memory."""
        manifest = _FakeManifest({"plex": _make_app()})
        _, first = _run_failed_recovery(manifest)
        assert len(_operator_pages(first)) == 1

        # Simulate a fresh process: nothing in memory, ledger file on disk.
        assert recovery_mod._ESCALATION_PAGE_LEDGER.exists()
        recovery_mod.clear_permanent_failure("plex")
        _, second = _run_failed_recovery(manifest)
        assert _operator_pages(second) == []

    def test_corrupt_ledger_fails_open_and_pages(self):
        """A garbage ledger must never swallow an operator page."""
        recovery_mod._ESCALATION_PAGE_LEDGER.write_text("{not json", encoding="utf-8")
        manifest = _FakeManifest({"plex": _make_app()})
        _, mock_notify = _run_failed_recovery(manifest)
        assert len(_operator_pages(mock_notify)) == 1
