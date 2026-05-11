"""tests/unit/test_recovery.py — TDD tests for lib/recovery.py.

All external calls (lifecycle, health, kuma, notify, state) are mocked.
No network, no SSH, no subprocess.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

import lib.recovery as recovery_mod
from lib.recovery import run
from lib.manifest import App, HealthConfig
from lib.health import HealthResult
from lib.lifecycle import LifecycleResult


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------

def _make_app(
    name: str = "sonarr",
    *,
    class_: str = "ucc",
    kuma_monitor: Optional[str] = "Sonarr",
    recovery_attempts: int = 3,
    recovery_backoff_s: list | None = None,
    kuma_recheck_delay_s: int = 90,
) -> App:
    if recovery_backoff_s is None:
        recovery_backoff_s = [10, 30, 60]
    return App(
        name=name,
        class_=class_,
        kuma_monitor=kuma_monitor,
        health=HealthConfig(kind="http_api", raw={}),
        defaults={
            "recovery_attempts": recovery_attempts,
            "recovery_backoff_s": recovery_backoff_s,
            "kuma_recheck_delay_s": kuma_recheck_delay_s,
            "lifecycle_timeout_s": 60,
        },
        upgrade=None,
        raw={"class": class_},
    )


def _ok_lifecycle() -> LifecycleResult:
    return LifecycleResult(ok=True, duration_s=0.1, stdout="", stderr="", reason="ok")


def _fail_lifecycle() -> LifecycleResult:
    return LifecycleResult(ok=False, duration_s=0.1, stdout="", stderr="", reason="exit 1")


def _ok_health() -> HealthResult:
    return HealthResult(ok=True, latency_ms=10, reason="ok")


def _fail_health() -> HealthResult:
    return HealthResult(ok=False, latency_ms=None, reason="connection refused")


# ---------------------------------------------------------------------------
# Fixture manifest stub
# ---------------------------------------------------------------------------

class _FakeManifest:
    def __init__(self, apps: dict):
        self._apps = apps

    def app(self, name: str) -> App:
        if name not in self._apps:
            raise KeyError(f"No app named '{name}' in manifest")
        return self._apps[name]


# ---------------------------------------------------------------------------
# State path — use a temp dir
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_state_path(tmp_path):
    state_file = tmp_path / "state.json"
    with patch.object(recovery_mod, "_STATE_PATH", state_file):
        yield state_file


# ---------------------------------------------------------------------------
# Tests: basic recovery flows
# ---------------------------------------------------------------------------

class TestRecoverySuccess:
    def test_recovery_succeeds_first_attempt_kuma_up(self, tmp_path):
        app = _make_app(kuma_monitor="Sonarr")
        manifest = _FakeManifest({"sonarr": app})

        with patch("lib.recovery.lifecycle.start", return_value=_ok_lifecycle()) as mock_start, \
             patch("lib.recovery.health.probe", return_value=_ok_health()) as mock_probe, \
             patch("lib.recovery.kuma.monitor_status", return_value="up") as mock_kuma, \
             patch("lib.recovery.notify.notify") as mock_notify, \
             patch("lib.recovery.state.record") as mock_state, \
             patch("lib.recovery.time.sleep"):
            result = run("sonarr", manifest=manifest)

        assert result["event"] == "recovered"
        assert result["attempts"] == 1
        assert result["kuma_status"] == "up"
        mock_start.assert_called_once()
        mock_notify.assert_called_once()
        assert "recovered" in mock_notify.call_args[0][0].lower() or \
               "✓" in mock_notify.call_args[0][0]
        mock_state.assert_called_once()

    def test_recovery_succeeds_no_kuma_monitor(self, tmp_path):
        app = _make_app(kuma_monitor=None)
        manifest = _FakeManifest({"sonarr": app})

        with patch("lib.recovery.lifecycle.start", return_value=_ok_lifecycle()), \
             patch("lib.recovery.health.probe", return_value=_ok_health()), \
             patch("lib.recovery.kuma.monitor_status") as mock_kuma, \
             patch("lib.recovery.notify.notify"), \
             patch("lib.recovery.state.record"), \
             patch("lib.recovery.time.sleep"):
            result = run("sonarr", manifest=manifest)

        assert result["event"] == "recovered"
        mock_kuma.assert_not_called()

    def test_recovery_lifecycle_failure_still_continues_to_probe(self, tmp_path):
        app = _make_app(kuma_monitor=None)
        manifest = _FakeManifest({"sonarr": app})

        with patch("lib.recovery.lifecycle.start", return_value=_fail_lifecycle()), \
             patch("lib.recovery.health.probe", return_value=_ok_health()), \
             patch("lib.recovery.kuma.monitor_status") as mock_kuma, \
             patch("lib.recovery.notify.notify"), \
             patch("lib.recovery.state.record"), \
             patch("lib.recovery.time.sleep"):
            result = run("sonarr", manifest=manifest)

        assert result["event"] == "recovered"
        mock_kuma.assert_not_called()


class TestRecoveryFailure:
    def test_recovery_three_failures_escalate(self, tmp_path):
        app = _make_app(kuma_monitor="Sonarr")
        manifest = _FakeManifest({"sonarr": app})

        with patch("lib.recovery.lifecycle.start", return_value=_ok_lifecycle()) as mock_start, \
             patch("lib.recovery.health.probe", return_value=_fail_health()) as mock_probe, \
             patch("lib.recovery.kuma.monitor_status") as mock_kuma, \
             patch("lib.recovery.notify.notify") as mock_notify, \
             patch("lib.recovery.state.record") as mock_state, \
             patch("lib.recovery.time.sleep"):
            result = run("sonarr", manifest=manifest)

        assert result["event"] == "failed"
        assert result["attempts"] == 3
        assert mock_start.call_count == 3
        mock_kuma.assert_not_called()
        notify_msg = mock_notify.call_args[0][0]
        assert "operator" in notify_msg.lower() or "✗" in notify_msg or "3" in notify_msg

    def test_recovery_healthy_locally_kuma_still_down(self, tmp_path):
        app = _make_app(kuma_monitor="Sonarr")
        manifest = _FakeManifest({"sonarr": app})

        with patch("lib.recovery.lifecycle.start", return_value=_ok_lifecycle()), \
             patch("lib.recovery.health.probe", return_value=_ok_health()), \
             patch("lib.recovery.kuma.monitor_status", return_value="down"), \
             patch("lib.recovery.notify.notify") as mock_notify, \
             patch("lib.recovery.state.record") as mock_state, \
             patch("lib.recovery.time.sleep"):
            result = run("sonarr", manifest=manifest)

        assert result["event"] == "healthy_locally_kuma_down"
        assert result["kuma_status"] == "down"
        notify_msg = mock_notify.call_args[0][0]
        assert "routing" in notify_msg.lower() or "⚠" in notify_msg or "kuma" in notify_msg.lower()

    def test_recovery_healthy_locally_kuma_unknown(self, tmp_path):
        app = _make_app(kuma_monitor="Sonarr")
        manifest = _FakeManifest({"sonarr": app})

        with patch("lib.recovery.lifecycle.start", return_value=_ok_lifecycle()), \
             patch("lib.recovery.health.probe", return_value=_ok_health()), \
             patch("lib.recovery.kuma.monitor_status", return_value="unknown"), \
             patch("lib.recovery.notify.notify") as mock_notify, \
             patch("lib.recovery.state.record"), \
             patch("lib.recovery.time.sleep"):
            result = run("sonarr", manifest=manifest)

        assert result["event"] == "healthy_locally_kuma_down"
        assert result["kuma_status"] == "unknown"


class TestRecoveryBackoff:
    def test_recovery_backoff_calls_time_sleep_with_correct_values(self, tmp_path):
        app = _make_app(kuma_monitor=None, recovery_backoff_s=[10, 30, 60])
        manifest = _FakeManifest({"sonarr": app})

        sleep_calls = []

        def _fake_sleep(secs):
            sleep_calls.append(secs)

        with patch("lib.recovery.lifecycle.start", return_value=_ok_lifecycle()), \
             patch("lib.recovery.health.probe", return_value=_fail_health()), \
             patch("lib.recovery.notify.notify"), \
             patch("lib.recovery.state.record"), \
             patch("lib.recovery.time.sleep", side_effect=_fake_sleep):
            result = run("sonarr", manifest=manifest)

        assert result["event"] == "failed"
        assert sleep_calls == [10, 30, 60]

    def test_recovery_backoff_includes_recheck_delay_on_success(self, tmp_path):
        app = _make_app(kuma_monitor="Sonarr", recovery_backoff_s=[10, 30, 60], kuma_recheck_delay_s=90)
        manifest = _FakeManifest({"sonarr": app})

        sleep_calls = []

        def _fake_sleep(secs):
            sleep_calls.append(secs)

        with patch("lib.recovery.lifecycle.start", return_value=_ok_lifecycle()), \
             patch("lib.recovery.health.probe", return_value=_ok_health()), \
             patch("lib.recovery.kuma.monitor_status", return_value="up"), \
             patch("lib.recovery.notify.notify"), \
             patch("lib.recovery.state.record"), \
             patch("lib.recovery.time.sleep", side_effect=_fake_sleep):
            run("sonarr", manifest=manifest)

        assert 10 in sleep_calls
        assert 90 in sleep_calls


# ---------------------------------------------------------------------------
# Tests: auto-downgrade after attempt-cap
# ---------------------------------------------------------------------------

class TestRecoveryAutoDowngrade:
    def test_auto_downgrade_fires_when_previous_version_recorded(self, tmp_path):
        # Seed state.json with a previous_version
        import json
        state_file = tmp_path / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({
            "apps": {
                "conjurr": {
                    "updated_at": "2026-05-09T00:00:00Z",
                    "event": "upgraded",
                    "version": "v4.1.0",
                    "previous_version": "v4.0.0",
                    "reason": "ok",
                }
            }
        }))

        app = _make_app(name="conjurr", class_="systemd", kuma_monitor="Conjurr")
        manifest = _FakeManifest({"conjurr": app})

        ok_result = LifecycleResult(ok=True, duration_s=0.1, stdout="", stderr="", reason="rolled back")
        with patch.object(recovery_mod, "_STATE_PATH", state_file), \
             patch("lib.recovery.lifecycle.start", return_value=_ok_lifecycle()), \
             patch("lib.recovery.health.probe", return_value=_fail_health()), \
             patch("lib.recovery.lifecycle.downgrade", return_value=ok_result) as mock_dg, \
             patch("lib.recovery.notify.notify") as mock_notify, \
             patch("lib.recovery.time.sleep"):
            result = run("conjurr", manifest=manifest)

        assert result["event"] == "auto_downgraded"
        assert result["final_health"] == "ok"
        mock_dg.assert_called_once()
        # downgrade target was the recorded previous_version
        assert mock_dg.call_args[0][1] == "v4.0.0"
        # notify mentions auto-downgrade
        msgs = " ".join(c.args[0] for c in mock_notify.call_args_list)
        assert "auto-downgrade" in msgs.lower() or "downgraded" in msgs.lower()

    def test_no_auto_downgrade_when_previous_version_absent(self, tmp_path):
        state_file = tmp_path / "state.json"

        app = _make_app(name="conjurr", class_="systemd", kuma_monitor="Conjurr")
        manifest = _FakeManifest({"conjurr": app})

        with patch.object(recovery_mod, "_STATE_PATH", state_file), \
             patch("lib.recovery.lifecycle.start", return_value=_ok_lifecycle()), \
             patch("lib.recovery.health.probe", return_value=_fail_health()), \
             patch("lib.recovery.lifecycle.downgrade") as mock_dg, \
             patch("lib.recovery.notify.notify"), \
             patch("lib.recovery.time.sleep"):
            result = run("conjurr", manifest=manifest)

        assert result["event"] == "failed"
        mock_dg.assert_not_called()

    def test_no_auto_downgrade_for_ucc_class(self, tmp_path):
        import json
        state_file = tmp_path / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({
            "apps": {
                "sonarr": {
                    "version": "4.1.0",
                    "previous_version": "4.0.0",
                    "event": "upgraded",
                    "reason": "ok",
                }
            }
        }))

        app = _make_app(name="sonarr", class_="ucc", kuma_monitor="Sonarr")
        manifest = _FakeManifest({"sonarr": app})

        with patch.object(recovery_mod, "_STATE_PATH", state_file), \
             patch("lib.recovery.lifecycle.start", return_value=_ok_lifecycle()), \
             patch("lib.recovery.health.probe", return_value=_fail_health()), \
             patch("lib.recovery.lifecycle.downgrade") as mock_dg, \
             patch("lib.recovery.notify.notify"), \
             patch("lib.recovery.time.sleep"):
            result = run("sonarr", manifest=manifest)

        # UCC class falls through to "operator needed" — auto-downgrade is skipped
        assert result["event"] == "failed"
        mock_dg.assert_not_called()

    def test_auto_downgrade_failure_records_auto_downgrade_failed(self, tmp_path):
        import json
        state_file = tmp_path / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({
            "apps": {
                "conjurr": {
                    "version": "v4.1.0",
                    "previous_version": "v4.0.0",
                    "event": "upgraded",
                    "reason": "ok",
                }
            }
        }))

        app = _make_app(name="conjurr", class_="systemd", kuma_monitor="Conjurr")
        manifest = _FakeManifest({"conjurr": app})

        fail_result = LifecycleResult(ok=False, duration_s=0.1, stdout="", stderr="", reason="git checkout failed")
        with patch.object(recovery_mod, "_STATE_PATH", state_file), \
             patch("lib.recovery.lifecycle.start", return_value=_ok_lifecycle()), \
             patch("lib.recovery.health.probe", return_value=_fail_health()), \
             patch("lib.recovery.lifecycle.downgrade", return_value=fail_result) as mock_dg, \
             patch("lib.recovery.notify.notify") as mock_notify, \
             patch("lib.recovery.time.sleep"):
            result = run("conjurr", manifest=manifest)

        assert result["event"] == "auto_downgrade_failed"
        mock_dg.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: unknown app
# ---------------------------------------------------------------------------

class TestRecoveryUnknownApp:
    def test_recovery_unknown_app_records_and_returns(self, tmp_path):
        manifest = _FakeManifest({})

        with patch("lib.recovery.lifecycle.start") as mock_start, \
             patch("lib.recovery.notify.notify") as mock_notify, \
             patch("lib.recovery.state.record") as mock_state:
            result = run("nonexistent", manifest=manifest)

        assert result["event"] == "unknown_app"
        mock_start.assert_not_called()
        mock_notify.assert_called_once()
        mock_state.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: per-app concurrency lock
# ---------------------------------------------------------------------------

class TestRecoveryLocking:
    def test_recovery_per_app_lock_prevents_concurrent(self, tmp_path):
        app = _make_app(kuma_monitor=None)
        manifest = _FakeManifest({"sonarr": app})

        barrier = threading.Barrier(2)
        first_started = threading.Event()
        second_result = {}

        def _slow_lifecycle(a, **kw):
            first_started.set()
            barrier.wait(timeout=5)
            return _ok_lifecycle()

        def _run_second():
            # Use tiny lock timeout so test completes fast
            orig = recovery_mod._LOCK_ACQUIRE_TIMEOUT_S
            recovery_mod._LOCK_ACQUIRE_TIMEOUT_S = 0.2
            try:
                with patch("lib.recovery.lifecycle.start", side_effect=_slow_lifecycle), \
                     patch("lib.recovery.health.probe", return_value=_ok_health()), \
                     patch("lib.recovery.notify.notify"), \
                     patch("lib.recovery.state.record"), \
                     patch("lib.recovery.time.sleep"):
                    second_result["r"] = run("sonarr", manifest=manifest)
            finally:
                recovery_mod._LOCK_ACQUIRE_TIMEOUT_S = orig

        # Start first recovery in a thread; it will hold the lock at barrier
        with patch("lib.recovery.lifecycle.start", side_effect=_slow_lifecycle), \
             patch("lib.recovery.health.probe", return_value=_ok_health()), \
             patch("lib.recovery.notify.notify"), \
             patch("lib.recovery.state.record"), \
             patch("lib.recovery.time.sleep"):

            t1 = threading.Thread(target=lambda: run("sonarr", manifest=manifest))
            t2 = threading.Thread(target=_run_second)
            t1.start()
            first_started.wait(timeout=5)
            t2.start()
            t2.join(timeout=5)
            barrier.wait(timeout=5)
            t1.join(timeout=5)

        assert second_result["r"]["event"] == "duplicate_recovery_dropped"

    def test_recovery_independent_apps_dont_block(self, tmp_path):
        sonarr = _make_app("sonarr", kuma_monitor=None)
        radarr = _make_app("radarr", kuma_monitor=None)
        manifest = _FakeManifest({"sonarr": sonarr, "radarr": radarr})

        sonarr_started = threading.Event()
        radarr_started = threading.Event()

        def _sonarr_lifecycle(a, **kw):
            sonarr_started.set()
            radarr_started.wait(timeout=3)
            return _ok_lifecycle()

        def _radarr_lifecycle(a, **kw):
            radarr_started.set()
            return _ok_lifecycle()

        results = {}

        def _run_sonarr():
            with patch("lib.recovery.lifecycle.start", side_effect=_sonarr_lifecycle), \
                 patch("lib.recovery.health.probe", return_value=_ok_health()), \
                 patch("lib.recovery.notify.notify"), \
                 patch("lib.recovery.state.record"), \
                 patch("lib.recovery.time.sleep"):
                results["sonarr"] = run("sonarr", manifest=manifest)

        def _run_radarr():
            sonarr_started.wait(timeout=3)
            with patch("lib.recovery.lifecycle.start", side_effect=_radarr_lifecycle), \
                 patch("lib.recovery.health.probe", return_value=_ok_health()), \
                 patch("lib.recovery.notify.notify"), \
                 patch("lib.recovery.state.record"), \
                 patch("lib.recovery.time.sleep"):
                results["radarr"] = run("radarr", manifest=manifest)

        t1 = threading.Thread(target=_run_sonarr)
        t2 = threading.Thread(target=_run_radarr)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert results["sonarr"]["event"] == "recovered"
        assert results["radarr"]["event"] == "recovered"


# ---------------------------------------------------------------------------
# Recoverability — class gating for trigger_async
# ---------------------------------------------------------------------------

class TestIsRecoverable:
    """`_is_recoverable` decides which classes flow into trigger_async's
    3-attempt loop. The historical answer was {ucc, systemd}; cron-class
    timer-driven oneshots (buildarr, recyclarr, ...) joined the recoverable
    set once lifecycle._cron_start_service let us re-fire their .service."""

    def test_ucc_is_recoverable(self):
        app = _make_app("sonarr", class_="ucc")
        assert recovery_mod._is_recoverable(app) is True

    def test_systemd_is_recoverable(self):
        app = _make_app("listmonk", class_="systemd")
        assert recovery_mod._is_recoverable(app) is True

    def test_cron_with_unit_is_recoverable(self):
        # The five timer-driven oneshots (buildarr/recyclarr/kometa/
        # qflix-newsletter/upgradinatorr) declare a `unit:` field. After
        # the systemd_oneshot probe lands, those failures need to flow
        # through the 3-attempt recovery and pings Discord on exhaustion.
        app = _make_app("buildarr", class_="cron")
        app.raw["unit"] = "buildarr.service"
        assert recovery_mod._is_recoverable(app) is True

    def test_cron_without_unit_is_not_recoverable(self):
        # Pure crontab one-liners (no systemd unit) can't be re-fired via
        # systemctl, so they stay opted out.
        app = _make_app("nounit", class_="cron")
        # raw has no "unit"
        assert recovery_mod._is_recoverable(app) is False

    def test_library_is_not_recoverable(self):
        app = _make_app("python-plexapi", class_="library")
        assert recovery_mod._is_recoverable(app) is False
