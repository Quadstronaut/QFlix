"""tests/unit/test_cli.py — TDD tests for lib/cli.py (manitoba-maint CLI).

All external I/O (health.probe, lifecycle.*, recovery.run, window.*,
watchdog) is mocked. No network, no subprocess, no SSH.

Testing strategy: load lib.cli.main via importlib so the entrypoint
script path is irrelevant. conftest.py already puts scripts/maint on
sys.path so `from lib.cli import main` works directly.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

from lib.cli import main
from lib.manifest import App, HealthConfig, ManifestError
from lib.health import HealthResult
from lib.lifecycle import LifecycleResult, LifecycleError

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "manifests"
_VALID_FIXTURE = _FIXTURES / "valid.yaml"
_BAD_CLASS_FIXTURE = _FIXTURES / "bad-class.yaml"
_REAL_MANIFEST = _REPO_ROOT / "manifest" / "apps.yaml"
_ENTRYPOINT = _REPO_ROOT / "scripts" / "maint" / "manitoba-maint"


# ---------------------------------------------------------------------------
# App / manifest builders
# ---------------------------------------------------------------------------

def _make_app(
    name: str = "sonarr",
    *,
    class_: str = "ucc",
    kuma_monitor: Optional[str] = "Sonarr",
) -> App:
    return App(
        name=name,
        class_=class_,
        kuma_monitor=kuma_monitor,
        health=HealthConfig(kind="http_api", raw={"port_secret": f"{name}.port"}),
        defaults={
            "health_timeout_s": 5,
            "recovery_attempts": 3,
            "recovery_backoff_s": [10, 30, 60],
            "lifecycle_timeout_s": 60,
            "kuma_recheck_delay_s": 90,
        },
        upgrade=None,
        raw={"class": class_, "ucc_slug": name},
    )


def _make_manifest(apps: list[App]):
    """Build a minimal Manifest stub from a list of App objects."""
    from lib.manifest import Manifest

    app_dict = {a.name: a for a in apps}

    class _Manifest:
        def app(self, name):
            if name not in app_dict:
                raise KeyError(name)
            return app_dict[name]

        def apps(self):
            return iter(app_dict.values())

        def resolve_kuma_monitor(self, mon):
            for a in app_dict.values():
                if a.kuma_monitor == mon:
                    return a.name
            return None

    return _Manifest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_result(**kw) -> LifecycleResult:
    return LifecycleResult(ok=True, duration_s=0.1, stdout="", stderr="", reason="ok", **kw)


def _fail_result(**kw) -> LifecycleResult:
    return LifecycleResult(ok=False, duration_s=0.1, stdout="", stderr="", reason="fail", **kw)


def _health_ok() -> HealthResult:
    return HealthResult(ok=True, latency_ms=12, reason="ok")


def _health_fail() -> HealthResult:
    return HealthResult(ok=False, latency_ms=None, reason="connection refused")


# ---------------------------------------------------------------------------
# status tests
# ---------------------------------------------------------------------------

class TestStatusAllApps:
    def test_status_calls_health_probe_for_each_app(self, capsys, monkeypatch):
        # valid.yaml fixture has sonarr, listmonk, recyclarr (3 apps)
        probe_mock = MagicMock(return_value=_health_ok())

        with patch("lib.health.probe", probe_mock), \
             patch("lib.state.read", return_value={}):
            rc = main(["status", "--all"], manifest_path=_VALID_FIXTURE)

        assert probe_mock.call_count == 3
        out = capsys.readouterr().out
        assert "sonarr" in out
        assert "listmonk" in out
        assert "recyclarr" in out
        assert rc == 0

    def test_status_renders_ok_and_fail(self, capsys, monkeypatch):
        apps = [_make_app("sonarr"), _make_app("radarr")]
        manifest = _make_manifest(apps)

        def _probe(app, **kw):
            if app.name == "sonarr":
                return _health_ok()
            return _health_fail()

        with patch("lib.health.probe", side_effect=_probe), \
             patch("lib.state.read", return_value={}):
            rc = main(["status", "--all"], manifest_path=_VALID_FIXTURE)

        out = capsys.readouterr().out
        assert "✓" in out
        assert "✗" in out
        assert rc == 0


class TestStatusSingleApp:
    def test_status_single_app(self, capsys):
        probe_mock = MagicMock(return_value=_health_ok())

        with patch("lib.health.probe", probe_mock), \
             patch("lib.state.read", return_value={}):
            rc = main(["status", "sonarr"], manifest_path=_VALID_FIXTURE)

        assert probe_mock.call_count == 1
        out = capsys.readouterr().out
        assert "sonarr" in out
        assert rc == 0

    def test_status_unknown_app_exits_1(self, capsys):
        with patch("lib.state.read", return_value={}):
            rc = main(["status", "nonexistent"], manifest_path=_VALID_FIXTURE)

        assert rc == 1
        err = capsys.readouterr().err
        assert "unknown" in err.lower() or "nonexistent" in err

    def test_status_no_arg_shows_all(self, capsys):
        probe_mock = MagicMock(return_value=_health_ok())

        with patch("lib.health.probe", probe_mock), \
             patch("lib.state.read", return_value={}):
            rc = main(["status"], manifest_path=_VALID_FIXTURE)

        # valid.yaml has 3 apps
        assert probe_mock.call_count == 3
        assert rc == 0


class TestStatusColumnOrder:
    def test_status_renders_columns_in_order(self, capsys):
        probe_mock = MagicMock(return_value=_health_ok())

        with patch("lib.health.probe", probe_mock), \
             patch("lib.state.read", return_value={}):
            rc = main(["status", "--all"], manifest_path=_VALID_FIXTURE)

        out = capsys.readouterr().out
        header = out.splitlines()[0]
        assert rc == 0
        # All columns present in order
        for col in ("APP", "CLASS", "STATUS", "LATENCY", "LAST RECOVERY"):
            assert col in header
        # Check positional order
        positions = {col: header.index(col) for col in ("APP", "CLASS", "STATUS", "LATENCY", "LAST RECOVERY")}
        assert positions["APP"] < positions["CLASS"] < positions["STATUS"] < positions["LATENCY"] < positions["LAST RECOVERY"]


# ---------------------------------------------------------------------------
# lifecycle: start / stop / restart
# ---------------------------------------------------------------------------

class TestLifecycleStart:
    def test_cli_start_invokes_lifecycle_start(self, capsys):
        with patch("lib.lifecycle.start", return_value=_ok_result()) as mock_start:
            rc = main(["start", "sonarr"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        mock_start.assert_called_once()

    def test_cli_start_failure_exits_2(self, capsys):
        with patch("lib.lifecycle.start", return_value=_fail_result()):
            rc = main(["start", "sonarr"], manifest_path=_VALID_FIXTURE)
        assert rc == 2

    def test_cli_start_unknown_app_exits_1(self, capsys):
        rc = main(["start", "nonexistent"], manifest_path=_VALID_FIXTURE)
        assert rc == 1
        assert "unknown" in capsys.readouterr().err.lower() or "nonexistent" in capsys.readouterr().err


class TestLifecycleStop:
    def test_cli_stop_invokes_lifecycle_stop(self):
        with patch("lib.lifecycle.stop", return_value=_ok_result()) as mock_stop:
            rc = main(["stop", "sonarr"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        mock_stop.assert_called_once()

    def test_cli_stop_failure_exits_2(self):
        with patch("lib.lifecycle.stop", return_value=_fail_result()):
            rc = main(["stop", "sonarr"], manifest_path=_VALID_FIXTURE)
        assert rc == 2


class TestLifecycleRestart:
    def test_cli_restart_invokes_lifecycle_restart(self):
        with patch("lib.lifecycle.restart", return_value=_ok_result()) as mock_restart:
            rc = main(["restart", "sonarr"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        mock_restart.assert_called_once()


# ---------------------------------------------------------------------------
# upgrade / downgrade (not implemented)
# ---------------------------------------------------------------------------

class TestUpgradeDowngrade:
    def test_cli_upgrade_invokes_lifecycle_upgrade(self, capsys):
        from subprocess import CompletedProcess
        cp = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=cp), \
             patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")):
            rc = main(["upgrade", "sonarr", "--to", "4.0.0"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        out = capsys.readouterr().out
        assert "upgrade sonarr" in out

    def test_cli_downgrade_ucc_returns_2_unsupported(self, capsys):
        # UCC downgrade is not supported by Ultra.cc tooling
        rc = main(["downgrade", "sonarr", "--to", "3.0.0"], manifest_path=_VALID_FIXTURE)
        assert rc == 2
        out = capsys.readouterr().out
        assert "ucc" in out.lower() or "not supported" in out.lower()

    def test_cli_upgrade_unknown_app_exits_1(self, capsys):
        rc = main(["upgrade", "nonexistent"], manifest_path=_VALID_FIXTURE)
        assert rc == 1


# ---------------------------------------------------------------------------
# recover
# ---------------------------------------------------------------------------

class TestRecover:
    def test_cli_recover_invokes_recovery_run(self, capsys):
        result = {"app": "sonarr", "event": "recovered", "attempts": 1,
                  "final_health": "ok", "kuma_status": "up"}
        with patch("lib.recovery.run", return_value=result) as mock_run:
            rc = main(["recover", "sonarr"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        mock_run.assert_called_once()
        assert mock_run.call_args[1].get("manifest") is not None or mock_run.call_args[0]

    def test_cli_recover_healthy_locally_kuma_down_exits_0(self, capsys):
        result = {"app": "sonarr", "event": "healthy_locally_kuma_down", "attempts": 1,
                  "final_health": "ok", "kuma_status": "down"}
        with patch("lib.recovery.run", return_value=result):
            rc = main(["recover", "sonarr"], manifest_path=_VALID_FIXTURE)
        assert rc == 0

    def test_cli_recover_failed_exits_2(self, capsys):
        result = {"app": "sonarr", "event": "failed", "attempts": 3,
                  "final_health": "down", "kuma_status": "n/a"}
        with patch("lib.recovery.run", return_value=result):
            rc = main(["recover", "sonarr"], manifest_path=_VALID_FIXTURE)
        assert rc == 2

    def test_cli_recover_unknown_app_exits_1(self, capsys):
        rc = main(["recover", "nonexistent"], manifest_path=_VALID_FIXTURE)
        assert rc == 1


# ---------------------------------------------------------------------------
# window run
# ---------------------------------------------------------------------------

class TestWindowRun:
    def _make_summary(self):
        from lib.window import WindowSummary
        return WindowSummary(
            started_at="2026-05-09T04:00:00Z",
            closed_at="2026-05-09T05:00:00Z",
            queue_processed=0,
            queue_succeeded=0,
            queue_dropped_unknown=0,
            queue_dropped_max_block=0,
            queue_deferred_active_cron=0,
            smoke_results={"sonarr": True},
            notes=[],
        )

    def test_cli_window_run_invokes_orchestrator(self):
        summary = self._make_summary()
        mock_orch = MagicMock()
        mock_orch.return_value.run.return_value = summary

        with patch("lib.window.WindowOrchestrator", mock_orch):
            rc = main(["window", "run"], manifest_path=_VALID_FIXTURE)

        assert rc == 0
        mock_orch.return_value.run.assert_called_once()

    def test_cli_window_run_dry_run_passes_flag(self):
        summary = self._make_summary()
        mock_orch = MagicMock()
        mock_orch.return_value.run.return_value = summary

        with patch("lib.window.WindowOrchestrator", mock_orch):
            rc = main(["window", "run", "--dry-run"], manifest_path=_VALID_FIXTURE)

        assert rc == 0
        ctor_kwargs = mock_orch.call_args[1]
        assert ctor_kwargs.get("dry_run") is True

    def test_cli_window_run_force_passes_flag(self):
        summary = self._make_summary()
        mock_orch = MagicMock()
        mock_orch.return_value.run.return_value = summary

        with patch("lib.window.WindowOrchestrator", mock_orch):
            rc = main(["window", "run", "--force"], manifest_path=_VALID_FIXTURE)

        assert rc == 0
        run_kwargs = mock_orch.return_value.run.call_args
        # force=True should be passed to run()
        assert run_kwargs == call(force=True)


# ---------------------------------------------------------------------------
# window status
# ---------------------------------------------------------------------------

class TestWindowStatus:
    def test_cli_window_status_no_lock(self, capsys):
        mock_orch = MagicMock()
        mock_orch.return_value.status.return_value = {
            "present": False, "pid": None, "started_at": None, "alive": False
        }

        with patch("lib.window.WindowOrchestrator", mock_orch):
            rc = main(["window", "status"], manifest_path=_VALID_FIXTURE)

        assert rc == 0
        mock_orch.return_value.status.assert_called_once()
        out = capsys.readouterr().out
        assert "no lock" in out.lower()

    def test_cli_window_status_with_lock(self, capsys):
        mock_orch = MagicMock()
        mock_orch.return_value.status.return_value = {
            "present": True,
            "pid": 12345,
            "started_at": "2026-05-09T04:00:00Z",
            "alive": True,
        }

        with patch("lib.window.WindowOrchestrator", mock_orch):
            rc = main(["window", "status"], manifest_path=_VALID_FIXTURE)

        assert rc == 0
        out = capsys.readouterr().out
        assert "12345" in out or "lock" in out.lower()


# ---------------------------------------------------------------------------
# window watchdog
# ---------------------------------------------------------------------------

class TestWindowWatchdog:
    def test_cli_window_watchdog_cleared(self, capsys):
        with patch("lib.window.watchdog_clear_stale_lock", return_value=True) as mock_wdog:
            rc = main(["window", "watchdog"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        mock_wdog.assert_called_once()
        assert "cleared" in capsys.readouterr().out.lower()

    def test_cli_window_watchdog_no_action(self, capsys):
        with patch("lib.window.watchdog_clear_stale_lock", return_value=False):
            rc = main(["window", "watchdog"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        assert "no action" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# webhook CLI wiring (Phase 8)
# ---------------------------------------------------------------------------

class TestWebhook:
    def test_cli_webhook_reads_port_from_env_and_starts_server(self, monkeypatch, capsys):
        """webhook verb should call kuma.serve() with the port from MANITOBA_WEBHOOK_PORT."""
        import socket as _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
        s.close()

        monkeypatch.setenv("MANITOBA_WEBHOOK_PORT", str(free_port))
        monkeypatch.setenv("MANITOBA_MANIFEST", str(_VALID_FIXTURE))

        import threading as _threading
        import lib.cli as cli_mod

        result = {}

        def _fake_serve(port, *, host="127.0.0.1", manifest=None):
            result["port"] = port
            result["host"] = host

        monkeypatch.setattr("lib.kuma.serve", _fake_serve)

        rc = main(["webhook"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        assert result["port"] == free_port
        assert result["host"] == "127.0.0.1"


# ---------------------------------------------------------------------------
# manifest validate
# ---------------------------------------------------------------------------

class TestManifestValidate:
    def test_cli_manifest_validate_exits_0_on_real_manifest(self, capsys):
        if not _REAL_MANIFEST.exists():
            pytest.skip("manifest/apps.yaml not found in repo")
        rc = main(["manifest", "validate"], manifest_path=_REAL_MANIFEST)
        assert rc == 0
        out = capsys.readouterr().out
        assert "ok" in out.lower()

    def test_cli_manifest_validate_exits_2_on_bad_fixture(self, capsys):
        rc = main(["manifest", "validate"], manifest_path=_BAD_CLASS_FIXTURE)
        assert rc == 2
        err = capsys.readouterr().err
        assert "bogus" in err or "unknown class" in err.lower() or "manifest" in err.lower()


# ---------------------------------------------------------------------------
# pusher CLI tests
# ---------------------------------------------------------------------------

class TestPusherCli:
    def _tokens_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "tokens.json"
        p.write_text('{"sonarr": "tok-abc"}')
        return p

    def test_cli_pusher_invokes_serve(self, tmp_path, monkeypatch):
        """pusher verb should call pusher.serve()."""
        tokens_file = self._tokens_file(tmp_path)
        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", str(tokens_file))

        serve_mock = MagicMock()
        with patch("lib.pusher.serve", serve_mock):
            rc = main(["pusher"], manifest_path=_VALID_FIXTURE)

        assert rc == 0
        serve_mock.assert_called_once()

    def test_cli_pusher_once_passes_run_once_true(self, tmp_path, monkeypatch):
        """pusher --once should pass run_once=True to pusher.serve()."""
        tokens_file = self._tokens_file(tmp_path)
        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", str(tokens_file))

        serve_mock = MagicMock()
        with patch("lib.pusher.serve", serve_mock):
            rc = main(["pusher", "--once"], manifest_path=_VALID_FIXTURE)

        assert rc == 0
        call_kwargs = serve_mock.call_args[1]
        assert call_kwargs.get("run_once") is True

    def test_cli_pusher_missing_tokens_exits_1(self, capsys, monkeypatch):
        """pusher with no tokens file should exit 1."""
        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", "/nonexistent/tokens.json")
        rc = main(["pusher"], manifest_path=_VALID_FIXTURE)
        assert rc == 1
        err = capsys.readouterr().err
        assert "error" in err.lower()

    def test_cli_pusher_uses_kuma_url_env(self, tmp_path, monkeypatch):
        """MANITOBA_KUMA_URL env var should be forwarded to pusher.serve()."""
        tokens_file = self._tokens_file(tmp_path)
        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", str(tokens_file))
        monkeypatch.setenv("MANITOBA_KUMA_URL", "http://127.0.0.1:19999")

        serve_mock = MagicMock()
        with patch("lib.pusher.serve", serve_mock):
            rc = main(["pusher", "--once"], manifest_path=_VALID_FIXTURE)

        assert rc == 0
        call_kwargs = serve_mock.call_args[1]
        assert call_kwargs.get("kuma_url") == "http://127.0.0.1:19999"


# ---------------------------------------------------------------------------
# Entrypoint executable check (POSIX only)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="executable bit is POSIX-only")
def test_entrypoint_is_executable():
    assert _ENTRYPOINT.exists(), f"entrypoint not found: {_ENTRYPOINT}"
    assert os.access(_ENTRYPOINT, os.X_OK), f"entrypoint not executable: {_ENTRYPOINT}"
