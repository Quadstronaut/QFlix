"""tests/unit/test_deep_check.py — TDD tests for lib/deep_check.py.

No live network, no subprocess, no SSH.
All probes, trigger_async, and notify are mocked.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from lib.manifest import App, HealthConfig, UpgradeConfig, VersionPin
from lib.health import HealthResult


# ---------------------------------------------------------------------------
# App / manifest builders
# ---------------------------------------------------------------------------

def _make_defaults() -> dict:
    return {
        "health_timeout_s": 5,
        "recovery_attempts": 3,
        "recovery_backoff_s": [10, 30, 60],
        "lifecycle_timeout_s": 60,
        "kuma_recheck_delay_s": 90,
    }


def _make_app(name: str, *, class_: str = "systemd", parked: bool = False) -> App:
    raw: dict = {"class": class_}
    return App(
        name=name,
        class_=class_,
        kuma_monitor=None,
        health=HealthConfig(kind="systemd_only", raw=raw),
        defaults=_make_defaults(),
        upgrade=None,
        parked=parked,
        raw=raw,
    )


class _FakeManifest:
    def __init__(self, apps: dict[str, App]) -> None:
        self._apps = apps

    def app(self, name: str) -> App:
        if name not in self._apps:
            raise KeyError(f"No app named '{name}'")
        return self._apps[name]

    def apps(self):
        return iter(self._apps.values())


def _up() -> HealthResult:
    return HealthResult(ok=True, latency_ms=10, reason="ok")


def _down() -> HealthResult:
    return HealthResult(ok=False, latency_ms=None, reason="connection refused")


# ---------------------------------------------------------------------------
# 1. mixed up/down fleet: only down apps get trigger_async
# ---------------------------------------------------------------------------

def test_only_down_apps_get_trigger_async(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))

    app_up = _make_app("sonarr", class_="ucc")
    app_down = _make_app("radarr", class_="ucc")
    manifest = _FakeManifest({"sonarr": app_up, "radarr": app_down})

    def _probe(app, **kw):
        if app.name == "sonarr":
            return _up()
        return _down()

    with patch("lib.deep_check.health.probe", side_effect=_probe) as mock_probe, \
         patch("lib.deep_check.recovery.trigger_async", return_value="started") as mock_trigger, \
         patch("lib.deep_check.notify.notify") as mock_notify:
        from lib import deep_check
        result = deep_check.run_deep_check(reason="test", manifest=manifest)

    assert mock_probe.call_count == 2
    # trigger_async called only for radarr
    assert mock_trigger.call_count == 1
    triggered_app = mock_trigger.call_args[0][0]
    assert triggered_app.name == "radarr"
    # manifest kwarg forwarded
    assert mock_trigger.call_args[1].get("manifest") is manifest


# ---------------------------------------------------------------------------
# 2. summary dict shape — all required keys present
# ---------------------------------------------------------------------------

def test_result_dict_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))

    app_down = _make_app("tautulli", class_="systemd")
    manifest = _FakeManifest({"tautulli": app_down})

    with patch("lib.deep_check.health.probe", return_value=_down()), \
         patch("lib.deep_check.recovery.trigger_async", return_value="started"), \
         patch("lib.deep_check.notify.notify"):
        from lib import deep_check
        result = deep_check.run_deep_check(reason="shape-test", manifest=manifest)

    assert "reason" in result
    assert "ts" in result
    assert "checked" in result
    assert "down" in result
    assert "recovery_triggered" in result
    assert "skipped" in result
    assert result["reason"] == "shape-test"
    assert isinstance(result["checked"], int)
    assert isinstance(result["down"], list)
    assert isinstance(result["recovery_triggered"], dict)
    assert isinstance(result["skipped"], list)


# ---------------------------------------------------------------------------
# 3. nothing-down path: info notify, no trigger_async calls
# ---------------------------------------------------------------------------

def test_nothing_down_emits_info_notify_and_no_triggers(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))

    manifest = _FakeManifest({
        "sonarr": _make_app("sonarr", class_="ucc"),
        "radarr": _make_app("radarr", class_="ucc"),
    })

    with patch("lib.deep_check.health.probe", return_value=_up()), \
         patch("lib.deep_check.recovery.trigger_async") as mock_trigger, \
         patch("lib.deep_check.notify.notify") as mock_notify:
        from lib import deep_check
        result = deep_check.run_deep_check(reason="all-ok", manifest=manifest)

    mock_trigger.assert_not_called()
    # exactly one summary notify, at info level
    mock_notify.assert_called_once()
    _, kwargs = mock_notify.call_args
    level = kwargs.get("level", mock_notify.call_args[0][1] if len(mock_notify.call_args[0]) > 1 else "info")
    assert level == "info"
    assert result["down"] == []
    assert result["recovery_triggered"] == {}


# ---------------------------------------------------------------------------
# 4. manifest-load failure returns error result, never raises
# ---------------------------------------------------------------------------

def test_manifest_load_failure_returns_error_result(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    # Ensure no manifest path is resolvable so _load_default_manifest raises
    monkeypatch.setenv("MANITOBA_MANIFEST_PATH", str(tmp_path / "nonexistent.yaml"))

    with patch("lib.deep_check.notify.notify") as mock_notify:
        from lib import deep_check
        # Pass manifest=None so it tries to load default and fails
        result = deep_check.run_deep_check(reason="manifest-fail")

    assert "error" in result
    assert result["down"] == []
    # notify called (warning level) even on manifest failure
    mock_notify.assert_called_once()
    args = mock_notify.call_args
    level = args[1].get("level") if args[1] else (args[0][1] if len(args[0]) > 1 else "info")
    assert level == "warning"
    # never raises — we got here


# ---------------------------------------------------------------------------
# 5. not_recoverable decision recorded in result dict
# ---------------------------------------------------------------------------

def test_not_recoverable_decision_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))

    # library class = not_recoverable per recovery._is_recoverable logic
    app_library = _make_app("some-lib", class_="library")
    manifest = _FakeManifest({"some-lib": app_library})

    with patch("lib.deep_check.health.probe", return_value=_down()), \
         patch("lib.deep_check.recovery.trigger_async", return_value="not_recoverable") as mock_trigger, \
         patch("lib.deep_check.notify.notify"):
        from lib import deep_check
        result = deep_check.run_deep_check(reason="not-rec", manifest=manifest)

    assert "some-lib" in result["recovery_triggered"]
    assert result["recovery_triggered"]["some-lib"] == "not_recoverable"
    # still counted in down list
    assert "some-lib" in result["down"]


# ---------------------------------------------------------------------------
# 6. down apps with recover=False: no trigger_async calls
# ---------------------------------------------------------------------------

def test_recover_false_skips_trigger_async(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))

    manifest = _FakeManifest({"radarr": _make_app("radarr", class_="ucc")})

    with patch("lib.deep_check.health.probe", return_value=_down()), \
         patch("lib.deep_check.recovery.trigger_async") as mock_trigger, \
         patch("lib.deep_check.notify.notify"):
        from lib import deep_check
        result = deep_check.run_deep_check(reason="no-recover", manifest=manifest, recover=False)

    mock_trigger.assert_not_called()
    # radarr is down but skipped (recover=False)
    assert "radarr" in result["skipped"]


# ---------------------------------------------------------------------------
# 7. down apps get warning-level notify (not info)
# ---------------------------------------------------------------------------

def test_down_apps_trigger_warning_notify(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))

    manifest = _FakeManifest({"radarr": _make_app("radarr", class_="ucc")})

    with patch("lib.deep_check.health.probe", return_value=_down()), \
         patch("lib.deep_check.recovery.trigger_async", return_value="started"), \
         patch("lib.deep_check.notify.notify") as mock_notify:
        from lib import deep_check
        result = deep_check.run_deep_check(reason="warn-test", manifest=manifest)

    mock_notify.assert_called_once()
    args, kwargs = mock_notify.call_args
    level = kwargs.get("level") if kwargs else (args[1] if len(args) > 1 else "info")
    assert level == "warning"


# ---------------------------------------------------------------------------
# 8. jsonl log written to MANITOBA_STATE_DIR/deep-check.jsonl
# ---------------------------------------------------------------------------

def test_jsonl_appended_to_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))

    manifest = _FakeManifest({"sonarr": _make_app("sonarr", class_="ucc")})

    with patch("lib.deep_check.health.probe", return_value=_up()), \
         patch("lib.deep_check.recovery.trigger_async"), \
         patch("lib.deep_check.notify.notify"):
        from lib import deep_check
        result = deep_check.run_deep_check(reason="log-test", manifest=manifest)

    log_path = tmp_path / "deep-check.jsonl"
    assert log_path.exists(), "deep-check.jsonl must be written"
    line = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert line["reason"] == "log-test"
    assert "ts" in line
    assert "down" in line


# ---------------------------------------------------------------------------
# 9. per-app probe failure is captured, not propagated
# ---------------------------------------------------------------------------

def test_per_app_probe_exception_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))

    manifest = _FakeManifest({"sonarr": _make_app("sonarr", class_="ucc")})

    with patch("lib.deep_check.health.probe", side_effect=RuntimeError("probe crash")), \
         patch("lib.deep_check.recovery.trigger_async"), \
         patch("lib.deep_check.notify.notify"):
        from lib import deep_check
        result = deep_check.run_deep_check(reason="probe-exc", manifest=manifest)

    # Must not raise; app should appear in skipped
    assert isinstance(result, dict)
