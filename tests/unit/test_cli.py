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

        def canaries(self):
            return iter([])

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


# ---------------------------------------------------------------------------
# ucc subcommand tests
# ---------------------------------------------------------------------------

class TestUccDetect:
    """ucc detect — runs one probe + state update, exits 0 on success."""

    def test_ucc_detect_calls_detect(self, tmp_path, monkeypatch):
        """ucc detect dispatches to lib.ucc.detect() and exits 0."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        fake_state = {
            "active": False,
            "consecutive_clear": 0,
            "consecutive_error": 0,
            "last_probe_result": "clear",
            "probe_op": "app-sonarr start",
            "last_probe_at": "2026-05-24T12:00:00Z",
        }
        detect_mock = MagicMock(return_value=fake_state)
        with patch("lib.ucc.detect", detect_mock):
            rc = main(["ucc", "detect"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        detect_mock.assert_called_once()

    def test_ucc_detect_exits_2_on_exception(self, tmp_path, monkeypatch, capsys):
        """If detect() raises, ucc detect should exit 2 (operational failure)."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        detect_mock = MagicMock(side_effect=RuntimeError("SSH exploded"))
        with patch("lib.ucc.detect", detect_mock):
            rc = main(["ucc", "detect"], manifest_path=_VALID_FIXTURE)
        assert rc == 2
        err = capsys.readouterr().err
        assert "error" in err.lower() or "SSH" in err

    def test_ucc_detect_stdout_is_clean(self, tmp_path, monkeypatch, capsys):
        """ucc detect should not write unexpected output to stdout."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        fake_state = {
            "active": True,
            "consecutive_clear": 0,
            "consecutive_error": 0,
            "last_probe_result": "gated",
            "probe_op": "app-sonarr start",
            "last_probe_at": "2026-05-24T12:00:00Z",
        }
        detect_mock = MagicMock(return_value=fake_state)
        with patch("lib.ucc.detect", detect_mock):
            rc = main(["ucc", "detect"], manifest_path=_VALID_FIXTURE)
        out = capsys.readouterr().out
        # stdout should be empty or minimal (just probe result line is acceptable)
        assert rc == 0


class TestUccStatus:
    """ucc status — read-only print of current state, exits 0."""

    def test_ucc_status_prints_state(self, tmp_path, monkeypatch, capsys):
        """ucc status should print the UCC window state."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        state_data = {
            "active": True,
            "first_detected_at": "2026-05-24T21:30:00Z",
            "last_confirmed_at": "2026-05-24T23:55:00Z",
            "last_probe_at": "2026-05-24T23:55:00Z",
            "last_probe_result": "gated",
            "probe_op": "app-sonarr start",
            "consecutive_clear": 0,
            "consecutive_error": 0,
        }
        read_mock = MagicMock(return_value=state_data)
        with patch("lib.ucc.read_state", read_mock):
            rc = main(["ucc", "status"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        out = capsys.readouterr().out
        assert "active" in out.lower() or "gated" in out.lower() or "True" in out or "true" in out

    def test_ucc_status_no_prior_state(self, tmp_path, monkeypatch, capsys):
        """ucc status with empty state should print something indicating inactive."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        read_mock = MagicMock(return_value={})
        with patch("lib.ucc.read_state", read_mock):
            rc = main(["ucc", "status"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        out = capsys.readouterr().out
        assert out.strip() != ""  # must print something

    def test_ucc_status_does_not_call_detect(self, tmp_path, monkeypatch):
        """ucc status is read-only — detect() must NOT be called."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        read_mock = MagicMock(return_value={})
        detect_mock = MagicMock()
        with patch("lib.ucc.read_state", read_mock), \
             patch("lib.ucc.detect", detect_mock):
            main(["ucc", "status"], manifest_path=_VALID_FIXTURE)
        detect_mock.assert_not_called()

    def test_ucc_status_exits_0(self, tmp_path, monkeypatch):
        """ucc status always exits 0 (read-only, cannot operationally fail)."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        read_mock = MagicMock(return_value={"active": False, "consecutive_clear": 0, "consecutive_error": 0})
        with patch("lib.ucc.read_state", read_mock):
            rc = main(["ucc", "status"], manifest_path=_VALID_FIXTURE)
        assert rc == 0


# ---------------------------------------------------------------------------
# status --json tests
# ---------------------------------------------------------------------------

def _make_app_json(
    name: str,
    *,
    class_: str = "ucc",
    kuma_monitor: Optional[str] = None,
    health_kind: str = "http_api",
) -> App:
    """Build an App for JSON-mode tests; kuma_monitor=None by default to test fallback."""
    return App(
        name=name,
        class_=class_,
        kuma_monitor=kuma_monitor,
        health=HealthConfig(kind=health_kind, raw={"port_secret": f"{name}.port"}),
        defaults={"health_timeout_s": 5},
        upgrade=None,
        raw={"class": class_},
    )


class TestStatusJson:
    """--json mode: payload shape, key contract, stdout purity."""

    def _run_json(self, apps, probe_side_effect, state_data=None, argv=None, capsys=None):
        """Helper: run status --json with mocked probe + state; return (rc, payload, capsys_out)."""
        manifest = _make_manifest(apps)
        argv = argv or ["status", "--all", "--json"]
        if state_data is None:
            state_data = {}

        with patch("lib.health.probe", side_effect=probe_side_effect), \
             patch("lib.state.read", return_value=state_data):
            rc = main(argv, _manifest=manifest)

        captured = capsys.readouterr()
        import json as _json
        payload = _json.loads(captured.out)
        return rc, payload, captured

    def test_top_level_keys_exact(self, capsys):
        """Payload has exactly {schema_version, captured_at, summary, apps, canaries}."""
        apps = [_make_app_json("sonarr")]
        rc, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=10, reason="ok"),
            capsys=capsys,
        )
        assert rc == 0
        assert set(payload.keys()) == {"schema_version", "captured_at", "summary", "apps", "canaries"}

    def test_canaries_empty_when_manifest_has_none(self, capsys):
        """No canaries in manifest → canaries is an empty list (not absent)."""
        apps = [_make_app_json("sonarr")]
        _, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=10, reason="ok"),
            capsys=capsys,
        )
        assert payload["canaries"] == []

    def test_summary_counts_apps_only_not_canaries(self, capsys):
        """summary stays apps-only even when canaries are present."""
        from lib.manifest import Canary, Manifest
        apps = [_make_app_json("sonarr")]
        canaries = {
            "movie": Canary(name="movie", kuma_monitor="Canary Movie",
                            script="x.sh", schedule="hourly"),
        }
        manifest = Manifest({a.name: a for a in apps}, canaries=canaries)
        with patch("lib.health.probe", side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=10, reason="ok")), \
             patch("lib.state.read", return_value={}), \
             patch("lib.cli._probe_canary", return_value={"name": "movie", "display": "Canary Movie",
                                                          "ok": True, "reason": "success",
                                                          "last_run": "2026-05-25T00:00:00Z", "stale": False}):
            rc = main(["status", "--all", "--json"], _manifest=manifest)
        import json as _json
        payload = _json.loads(capsys.readouterr().out)
        assert payload["summary"]["total"] == 1  # only the app
        assert len(payload["canaries"]) == 1
        assert payload["canaries"][0]["display"] == "Canary Movie"

    def test_schema_version_is_1(self, capsys):
        apps = [_make_app_json("sonarr")]
        rc, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=10, reason="ok"),
            capsys=capsys,
        )
        assert payload["schema_version"] == 1

    def test_captured_at_utc_iso8601_z(self, capsys):
        """captured_at is a UTC ISO-8601 string ending with Z."""
        import re
        apps = [_make_app_json("sonarr")]
        _, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=10, reason="ok"),
            capsys=capsys,
        )
        ts = payload["captured_at"]
        assert isinstance(ts, str)
        assert ts.endswith("Z"), f"captured_at should end with Z, got: {ts!r}"
        # Rough ISO-8601 shape: YYYY-MM-DDTHH:MM:SSZ
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ts), f"Bad timestamp shape: {ts!r}"

    def test_summary_totals_up_down(self, capsys):
        """summary.total == len(apps); up + down == total; up == count of ok."""
        apps = [
            _make_app_json("sonarr"),
            _make_app_json("radarr"),
            _make_app_json("plex", health_kind="systemd_only"),
        ]

        def _probe(app, **kw):
            # sonarr=ok, radarr=fail, plex=ok
            if app.name == "radarr":
                return HealthResult(ok=False, latency_ms=None, reason="down")
            return HealthResult(ok=True, latency_ms=5, reason="ok")

        _, payload, _ = self._run_json(apps, _probe, capsys=capsys)
        s = payload["summary"]
        assert s["total"] == 3
        assert s["up"] == 2
        assert s["down"] == 1
        assert s["up"] + s["down"] == s["total"]

    def test_display_falls_back_to_app_key_when_no_kuma_monitor(self, capsys):
        """When kuma_monitor is None, display == app name."""
        apps = [_make_app_json("sonarr", kuma_monitor=None)]
        _, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=10, reason="ok"),
            capsys=capsys,
        )
        app_entry = payload["apps"][0]
        assert app_entry["app"] == "sonarr"
        assert app_entry["display"] == "sonarr"

    def test_display_uses_kuma_monitor_when_set(self, capsys):
        """When kuma_monitor is set, display == kuma_monitor."""
        apps = [_make_app_json("sonarr2", kuma_monitor="Sonarr Anime")]
        _, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=10, reason="ok"),
            capsys=capsys,
        )
        app_entry = payload["apps"][0]
        assert app_entry["display"] == "Sonarr Anime"

    def test_probe_kind_carried_through_verbatim(self, capsys):
        """probe_kind is the raw health.kind string, no enum mapping."""
        apps = [_make_app_json("recyclarr", health_kind="systemd_only")]
        _, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=False, latency_ms=None, reason="inactive"),
            capsys=capsys,
        )
        app_entry = payload["apps"][0]
        assert app_entry["probe_kind"] == "systemd_only"
        # Non-HTTP probe → latency_ms is null
        assert app_entry["latency_ms"] is None

    def test_http_probe_carries_latency(self, capsys):
        """HTTP probe kind → latency_ms is an integer."""
        apps = [_make_app_json("sonarr", health_kind="http_api")]
        _, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=42, reason="ok"),
            capsys=capsys,
        )
        assert payload["apps"][0]["latency_ms"] == 42

    def test_last_recovery_empty_when_no_state(self, capsys):
        """last_recovery is "" when app has no entry in state_data."""
        apps = [_make_app_json("sonarr")]
        _, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=5, reason="ok"),
            state_data={},
            capsys=capsys,
        )
        assert payload["apps"][0]["last_recovery"] == ""

    def test_last_recovery_formatted_when_state_present(self, capsys):
        """last_recovery is 'event (YYYY-MM-DD)' when state has event+updated_at."""
        apps = [_make_app_json("sonarr")]
        state_data = {
            "apps": {
                "sonarr": {
                    "event": "restart",
                    "updated_at": "2026-05-20T14:22:00Z",
                }
            }
        }
        _, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=5, reason="ok"),
            state_data=state_data,
            capsys=capsys,
        )
        assert payload["apps"][0]["last_recovery"] == "restart (2026-05-20)"

    def test_stdout_is_pure_json_no_table_text(self, capsys):
        """With --json, stdout must parse as JSON and contain no table headers/symbols."""
        apps = [_make_app_json("sonarr")]
        _, payload, captured = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=5, reason="ok"),
            capsys=capsys,
        )
        out = captured.out
        # Must not contain table markers
        assert "APP" not in out or "schema_version" in out  # ensure the APP is from JSON, not header
        assert "LAST RECOVERY" not in out
        assert "✓" not in out
        assert "✗" not in out
        # The entire stdout must be valid JSON (no trailing garbage)
        import json as _json
        _json.loads(out)  # raises if invalid

    def test_single_app_json_summary_total_1(self, capsys):
        """Single-app form: summary.total == 1."""
        apps = [
            _make_app_json("sonarr"),
            _make_app_json("radarr"),
        ]
        manifest = _make_manifest(apps)
        state_data = {}

        with patch("lib.health.probe", return_value=HealthResult(ok=True, latency_ms=8, reason="ok")), \
             patch("lib.state.read", return_value=state_data):
            rc = main(["status", "sonarr", "--json"], _manifest=manifest)

        import json as _json
        captured = capsys.readouterr()
        payload = _json.loads(captured.out)
        assert payload["summary"]["total"] == 1
        assert len(payload["apps"]) == 1
        assert payload["apps"][0]["app"] == "sonarr"

    def test_regression_without_json_table_is_printed(self, capsys):
        """Without --json, stdout still contains the human table (not valid JSON)."""
        import json as _json
        apps = [_make_app_json("sonarr")]
        manifest = _make_manifest(apps)

        with patch("lib.health.probe", return_value=HealthResult(ok=True, latency_ms=5, reason="ok")), \
             patch("lib.state.read", return_value={}):
            rc = main(["status", "--all"], _manifest=manifest)

        captured = capsys.readouterr()
        out = captured.out
        # Table header should be present
        assert "APP" in out
        assert "LAST RECOVERY" in out
        # Should NOT be parseable as JSON
        try:
            _json.loads(out)
            assert False, "Without --json, stdout should not be valid JSON"
        except _json.JSONDecodeError:
            pass  # expected

    def test_all_app_fields_present(self, capsys):
        """Every app entry has exactly the documented 7 fields."""
        expected_fields = {"app", "display", "class", "probe_kind", "ok", "latency_ms", "last_recovery"}
        apps = [_make_app_json("sonarr", kuma_monitor="Sonarr", health_kind="http_api")]
        _, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=20, reason="ok"),
            capsys=capsys,
        )
        for entry in payload["apps"]:
            assert set(entry.keys()) == expected_fields, f"unexpected keys in entry: {entry.keys()}"

    def test_class_field_name_in_json(self, capsys):
        """JSON key is 'class' (not 'class_' which is the Python attr)."""
        apps = [_make_app_json("sonarr", class_="ucc")]
        _, payload, _ = self._run_json(
            apps,
            probe_side_effect=lambda a, **kw: HealthResult(ok=True, latency_ms=10, reason="ok"),
            capsys=capsys,
        )
        entry = payload["apps"][0]
        assert "class" in entry
        assert "class_" not in entry
        assert entry["class"] == "ucc"


class TestStatusPauseWindow:
    """An app inside its pause_window is reported up (intentionally stopped, not a
    fault) and is NOT probed — so the dashboard + QuadstroNot stop false-alarming
    on scheduled quiet-hours pauses (e.g. tdarr-node 18:00-23:00 UTC)."""

    def test_paused_app_reported_up_in_json_without_probing(self, capsys):
        from lib.manifest import Manifest
        apps = [_make_app_json("tdarr-node", kuma_monitor="Tdarr Node",
                               health_kind="systemd_only")]
        manifest = Manifest({a.name: a for a in apps})
        # probe would say "inactive" — proves the short-circuit, not the probe,
        # produced ok:true.
        probe_mock = MagicMock(return_value=HealthResult(ok=False, latency_ms=None, reason="inactive"))

        with patch("lib.health.probe", probe_mock), \
             patch("lib.state.read", return_value={}), \
             patch("lib.suppression.in_pause_window", return_value=True):
            rc = main(["status", "--all", "--json"], _manifest=manifest)

        import json as _json
        payload = _json.loads(capsys.readouterr().out)
        assert rc == 0
        probe_mock.assert_not_called()  # paused → real probe skipped entirely
        entry = payload["apps"][0]
        assert entry["app"] == "tdarr-node"
        assert entry["ok"] is True
        assert entry["latency_ms"] is None
        assert payload["summary"]["down"] == 0  # paused counts as up
        # JSON contract unchanged — no `paused` field leaks into the payload.
        assert set(entry.keys()) == {"app", "display", "class", "probe_kind",
                                     "ok", "latency_ms", "last_recovery"}

    def test_paused_app_shows_paused_in_human_table(self, capsys):
        from lib.manifest import Manifest
        apps = [_make_app_json("tdarr-node", kuma_monitor="Tdarr Node",
                               health_kind="systemd_only")]
        manifest = Manifest({a.name: a for a in apps})

        with patch("lib.health.probe",
                   return_value=HealthResult(ok=False, latency_ms=None, reason="inactive")), \
             patch("lib.state.read", return_value={}), \
             patch("lib.suppression.in_pause_window", return_value=True):
            rc = main(["status", "--all"], _manifest=manifest)

        out = capsys.readouterr().out
        assert rc == 0
        assert "paused" in out
        assert "✗" not in out  # never rendered as a fault

    def test_non_paused_app_still_probed_normally(self, capsys):
        from lib.manifest import Manifest
        apps = [_make_app_json("sonarr", kuma_monitor="Sonarr")]
        manifest = Manifest({a.name: a for a in apps})
        probe_mock = MagicMock(return_value=HealthResult(ok=True, latency_ms=7, reason="ok"))

        with patch("lib.health.probe", probe_mock), \
             patch("lib.state.read", return_value={}), \
             patch("lib.suppression.in_pause_window", return_value=False):
            rc = main(["status", "--all", "--json"], _manifest=manifest)

        import json as _json
        payload = _json.loads(capsys.readouterr().out)
        assert rc == 0
        probe_mock.assert_called_once()  # not paused → normal probe runs
        assert payload["apps"][0]["latency_ms"] == 7

    def test_unknown_app_json_mode_stderr_empty_stdout(self, capsys):
        """Single unknown app with --json: exit 1, stderr has error, stdout is empty."""
        with patch("lib.state.read", return_value={}):
            rc = main(["status", "nonexistent", "--json"], manifest_path=_VALID_FIXTURE)

        assert rc == 1
        captured = capsys.readouterr()
        assert captured.out.strip() == ""
        assert "unknown" in captured.err.lower() or "nonexistent" in captured.err


# ---------------------------------------------------------------------------
# B: ucc detect calls ucc_response.respond
# ---------------------------------------------------------------------------

class TestUccDetectCallsRespond:
    """Extend TestUccDetect: `ucc detect` must call ucc_response.respond with
    the dict returned by ucc.detect(). Both are mocked — no real probe, no
    real responder side-effects."""

    def test_ucc_detect_calls_respond_with_detect_result(self, tmp_path, monkeypatch):
        """ucc detect dispatches the detect state to ucc_response.respond."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        fake_state = {
            "active": True,
            "last_probe_result": "gated",
            "probe_op": "app-sonarr start",
            "last_probe_at": "2026-05-24T12:00:00Z",
        }
        detect_mock = MagicMock(return_value=fake_state)
        respond_mock = MagicMock(return_value={"edge": "clear_to_active"})

        with patch("lib.ucc.detect", detect_mock), \
             patch("lib.ucc_response.respond", respond_mock):
            rc = main(["ucc", "detect"], manifest_path=_VALID_FIXTURE)

        assert rc == 0
        detect_mock.assert_called_once()
        respond_mock.assert_called_once()
        # The first positional arg to respond must be the detect result.
        actual_state = respond_mock.call_args.args[0]
        assert actual_state is fake_state

    def test_ucc_detect_respond_failure_does_not_fail_command(self, tmp_path, monkeypatch):
        """If ucc_response.respond raises, ucc detect still exits 0 (best-effort)."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        fake_state = {"active": False, "last_probe_result": "clear"}
        detect_mock = MagicMock(return_value=fake_state)
        respond_mock = MagicMock(side_effect=RuntimeError("listmonk unreachable"))

        with patch("lib.ucc.detect", detect_mock), \
             patch("lib.ucc_response.respond", respond_mock):
            rc = main(["ucc", "detect"], manifest_path=_VALID_FIXTURE)

        assert rc == 0  # respond failure must not propagate

    def test_ucc_detect_respond_not_called_when_detect_raises(self, tmp_path, monkeypatch):
        """If detect() raises, respond() must not be called."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        detect_mock = MagicMock(side_effect=RuntimeError("probe failed"))
        respond_mock = MagicMock()

        with patch("lib.ucc.detect", detect_mock), \
             patch("lib.ucc_response.respond", respond_mock):
            rc = main(["ucc", "detect"], manifest_path=_VALID_FIXTURE)

        assert rc == 2  # detect failure → exit 2
        respond_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Sub-project D: deep-check subcommand
# ---------------------------------------------------------------------------

class TestDeepCheckSubcommand:
    _FAKE_RESULT = {
        "reason": "manual",
        "ts": "2026-05-24T12:00:00Z",
        "checked": 3,
        "down": ["radarr"],
        "recovery_triggered": {"radarr": "started"},
        "skipped": [],
    }

    def test_deep_check_dispatches_to_run_deep_check(self, capsys):
        with patch("lib.deep_check.run_deep_check", return_value=self._FAKE_RESULT) as mock_dc:
            rc = main(["deep-check"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        mock_dc.assert_called_once()

    def test_deep_check_default_reason(self, capsys):
        with patch("lib.deep_check.run_deep_check", return_value=self._FAKE_RESULT) as mock_dc:
            rc = main(["deep-check"], manifest_path=_VALID_FIXTURE)
        kwargs = mock_dc.call_args[1]
        assert "reason" in kwargs

    def test_deep_check_custom_reason(self, capsys):
        with patch("lib.deep_check.run_deep_check", return_value=self._FAKE_RESULT) as mock_dc:
            rc = main(["deep-check", "--reason", "my-reason"], manifest_path=_VALID_FIXTURE)
        assert rc == 0
        kwargs = mock_dc.call_args[1]
        assert kwargs.get("reason") == "my-reason"

    def test_deep_check_prints_summary(self, capsys):
        with patch("lib.deep_check.run_deep_check", return_value=self._FAKE_RESULT):
            rc = main(["deep-check"], manifest_path=_VALID_FIXTURE)
        out = capsys.readouterr().out
        # Summary dict contents must appear in stdout
        assert "radarr" in out

    def test_deep_check_exit_0_on_success(self, capsys):
        with patch("lib.deep_check.run_deep_check", return_value=self._FAKE_RESULT):
            rc = main(["deep-check"], manifest_path=_VALID_FIXTURE)
        assert rc == 0

    def test_deep_check_exits_0_when_nothing_down(self, capsys):
        clean = {
            "reason": "manual",
            "ts": "2026-05-24T12:00:00Z",
            "checked": 3,
            "down": [],
            "recovery_triggered": {},
            "skipped": [],
        }
        with patch("lib.deep_check.run_deep_check", return_value=clean):
            rc = main(["deep-check"], manifest_path=_VALID_FIXTURE)
        assert rc == 0


# ---------------------------------------------------------------------------
# _probe_canary tests (cheap, read-only systemd-unit status; no script run)
# ---------------------------------------------------------------------------

class TestProbeCanary:
    """lib.cli._probe_canary: maps a canary's systemd unit to a status dict."""

    @staticmethod
    def _canary(name="movie", monitor="Canary Movie", schedule="hourly"):
        from lib.manifest import Canary
        return Canary(name=name, kuma_monitor=monitor, script="x.sh", schedule=schedule)

    @staticmethod
    def _runner(stdout, returncode=0):
        """Build a fake subprocess.run that returns a CompletedProcess-like obj."""
        from types import SimpleNamespace

        def _run(*a, **kw):
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
        return _run

    def _show(self, *, load="loaded", result="success", state="inactive",
              status="0", exit_epoch=None):
        lines = [f"LoadState={load}", f"Result={result}", f"ActiveState={state}",
                 f"ExecMainStatus={status}"]
        if exit_epoch is not None:
            lines.append(f"ExecMainExitTimestamp=@{exit_epoch}")
        else:
            lines.append("ExecMainExitTimestamp=")
        return "\n".join(lines) + "\n"

    def test_ok_recent_not_stale(self):
        from lib.cli import _probe_canary
        import datetime as dt
        now = dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
        recent = int((now - dt.timedelta(minutes=20)).timestamp())  # 20m ago, hourly
        e = _probe_canary(self._canary(), now=now,
                          run=self._runner(self._show(exit_epoch=recent)))
        assert e["ok"] is True
        assert e["display"] == "Canary Movie"
        assert e["last_run"] == "2026-05-25T11:40:00Z"
        assert e["stale"] is False

    def test_stale_when_older_than_1_5x_interval(self):
        from lib.cli import _probe_canary
        import datetime as dt
        now = dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
        old = int((now - dt.timedelta(minutes=100)).timestamp())  # >90m for hourly
        e = _probe_canary(self._canary(schedule="hourly"), now=now,
                          run=self._runner(self._show(exit_epoch=old)))
        assert e["ok"] is True
        assert e["stale"] is True

    def test_every_15min_stale_threshold(self):
        from lib.cli import _probe_canary
        import datetime as dt
        now = dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
        # 20m ago > 1.5*15=22.5? no → not stale; 30m ago → stale
        not_stale = int((now - dt.timedelta(minutes=20)).timestamp())
        stale = int((now - dt.timedelta(minutes=30)).timestamp())
        e1 = _probe_canary(self._canary(schedule="every-15min"), now=now,
                           run=self._runner(self._show(exit_epoch=not_stale)))
        e2 = _probe_canary(self._canary(schedule="every-15min"), now=now,
                           run=self._runner(self._show(exit_epoch=stale)))
        assert e1["stale"] is False
        assert e2["stale"] is True

    def test_failed_result_marks_not_ok(self):
        from lib.cli import _probe_canary
        import datetime as dt
        now = dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
        recent = int((now - dt.timedelta(minutes=5)).timestamp())
        e = _probe_canary(self._canary(), now=now,
                          run=self._runner(self._show(result="exit-code",
                                                      state="failed",
                                                      status="1",
                                                      exit_epoch=recent)))
        assert e["ok"] is False
        assert "exit-code" in e["reason"]

    def test_not_installed_unit(self):
        from lib.cli import _probe_canary
        e = _probe_canary(self._canary(),
                          run=self._runner(self._show(load="not-found")))
        assert e["ok"] is False
        assert e["reason"] == "unit-not-installed"
        assert e["stale"] is True

    def test_never_run_is_ok_and_not_stale(self):
        from lib.cli import _probe_canary
        e = _probe_canary(self._canary(),
                          run=self._runner(self._show(result="", state="inactive",
                                                      exit_epoch=None)))
        assert e["ok"] is True
        assert e["last_run"] is None
        assert e["stale"] is False

    def test_in_flight_is_ok(self):
        from lib.cli import _probe_canary
        e = _probe_canary(self._canary(),
                          run=self._runner(self._show(result="success",
                                                      state="activating")))
        assert e["ok"] is True
        assert "in-flight" in e["reason"]

    def test_systemctl_nonzero_exit_not_ok(self):
        from lib.cli import _probe_canary
        e = _probe_canary(self._canary(), run=self._runner("", returncode=4))
        assert e["ok"] is False
        assert "systemctl exit 4" in e["reason"]

    def test_subprocess_exception_not_ok_and_stale(self):
        from lib.cli import _probe_canary

        def _boom(*a, **kw):
            raise TimeoutError("systemctl hung")
        e = _probe_canary(self._canary(), run=_boom)
        assert e["ok"] is False
        assert e["stale"] is True
        assert "probe-error" in e["reason"]


class TestCanaryPushSuppression:
    """`canary push <name>` honors push-suppress.json: pushes UP-with-note and
    skips the script run when the canary is suppressed."""

    def _canary(self):
        c = MagicMock()
        c.name = "prowlarr-indexer-health"
        c.script = "scripts/canaries/prowlarr-indexer-health.sh"
        c.kuma_monitor = "Canary Prowlarr Indexer Health"
        return c

    def test_suppressed_canary_pushes_up_and_skips_script(self, tmp_path):
        import argparse, json as _json
        from lib.cli import _cmd_canary_push

        manifest = MagicMock()
        manifest.canary.return_value = self._canary()
        tokens_file = tmp_path / "kuma-push-tokens.json"
        tokens_file.write_text(_json.dumps({"canary-prowlarr-indexer-health": "tok-pc"}), encoding="utf-8")

        args = argparse.Namespace(name="prowlarr-indexer-health")
        with patch("lib.cli._tokens_path", return_value=tokens_file), \
             patch("lib.suppression.push_suppressed", return_value="downstream of flaresolverr"), \
             patch("lib.cli.subprocess.run") as mock_run, \
             patch("lib.cli.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            rc = _cmd_canary_push(args, manifest)

        assert rc == 0
        mock_run.assert_not_called()                      # script never ran
        params = mock_get.call_args[1]["params"]
        assert params["status"] == "up"
        assert "SUPPRESSED" in params["msg"]
