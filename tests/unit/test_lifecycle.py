"""tests/unit/test_lifecycle.py — TDD tests for lib/lifecycle.py.

Tests are written before implementation (red phase). Each test covers one
dispatch class, one lifecycle verb, and one failure mode. No real subprocess,
no SSH, no network.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from subprocess import CompletedProcess
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

from lib.lifecycle import (
    LifecycleError,
    LifecycleResult,
    downgrade,
    restart,
    start,
    status,
    stop,
    upgrade,
)
from lib.manifest import App, HealthConfig


# ---------------------------------------------------------------------------
# Fixtures — inline App builders (no apps.yaml dependency)
# ---------------------------------------------------------------------------

def _make_app(
    class_: str,
    *,
    name: str = "myapp",
    ucc_slug: str | None = None,
    unit: str | None = None,
    lifecycle_timeout_s: float = 60.0,
) -> App:
    raw: dict = {"class": class_}
    if ucc_slug is not None:
        raw["ucc_slug"] = ucc_slug
    if unit is not None:
        raw["unit"] = unit

    defaults = {"lifecycle_timeout_s": lifecycle_timeout_s}

    app = App(
        name=name,
        class_=class_,
        kuma_monitor=None,
        health=HealthConfig(kind="systemd_only", raw={}),
        defaults=defaults,
        upgrade=None,
        raw=raw,
    )
    return app


def _ucc_app(ucc_slug: str = "sonarr", **kwargs) -> App:
    return _make_app("ucc", ucc_slug=ucc_slug, **kwargs)


def _systemd_app(unit: str = "conjurr.service", **kwargs) -> App:
    return _make_app("systemd", unit=unit, **kwargs)


def _cron_app(unit: str = "recyclarr.timer", **kwargs) -> App:
    return _make_app("cron", unit=unit, **kwargs)


def _library_app(**kwargs) -> App:
    return _make_app("library", **kwargs)


# ---------------------------------------------------------------------------
# UCC class tests
# ---------------------------------------------------------------------------

def test_ucc_start_invokes_app_slug():
    app = _ucc_app(ucc_slug="sonarr")
    cp = CompletedProcess(args=[], returncode=0, stdout="started\n", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        result = start(app)

    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args == ["app-sonarr", "start"]
    assert result.ok is True
    assert result.reason == "ok"


def test_ucc_stop_invokes_app_slug_stop():
    app = _ucc_app(ucc_slug="sonarr")
    cp = CompletedProcess(args=[], returncode=0, stdout="stopped\n", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        result = stop(app)

    args = mock_run.call_args[0][0]
    assert args == ["app-sonarr", "stop"]
    assert result.ok is True
    assert result.reason == "ok"


def test_ucc_restart_invokes_app_slug_restart():
    app = _ucc_app(ucc_slug="sonarr")
    cp = CompletedProcess(args=[], returncode=0, stdout="restarted\n", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        result = restart(app)

    args = mock_run.call_args[0][0]
    assert args == ["app-sonarr", "restart"]
    assert result.ok is True
    assert result.reason == "ok"


def test_ucc_nonzero_exit_returns_failure():
    app = _ucc_app(ucc_slug="sonarr")
    cp = CompletedProcess(args=[], returncode=2, stdout="", stderr="upstream error\n")

    with patch("subprocess.run", return_value=cp):
        result = start(app)

    assert result.ok is False
    assert result.reason.startswith("exit 2")


# ---------------------------------------------------------------------------
# Timeout test (applies to any class)
# ---------------------------------------------------------------------------

def test_lifecycle_timeout_returns_timeout_reason():
    app = _ucc_app(ucc_slug="sonarr", lifecycle_timeout_s=30.0)

    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["app-sonarr", "start"], timeout=30.0),
    ):
        result = start(app)

    assert result.ok is False
    assert result.reason == "timeout"
    assert result.duration_s == 30.0


# ---------------------------------------------------------------------------
# systemd class tests
# ---------------------------------------------------------------------------

def test_systemd_start_invokes_systemctl_user():
    app = _systemd_app(unit="conjurr.service")
    cp = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        result = start(app)

    args = mock_run.call_args[0][0]
    assert args == ["systemctl", "--user", "start", "conjurr.service"]
    assert result.ok is True


def test_systemd_stop_invokes_systemctl_user():
    app = _systemd_app(unit="conjurr.service")
    cp = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        result = stop(app)

    args = mock_run.call_args[0][0]
    assert args == ["systemctl", "--user", "stop", "conjurr.service"]
    assert result.ok is True


def test_systemd_restart_invokes_systemctl_user():
    app = _systemd_app(unit="conjurr.service")
    cp = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        result = restart(app)

    args = mock_run.call_args[0][0]
    assert args == ["systemctl", "--user", "restart", "conjurr.service"]
    assert result.ok is True


def test_systemd_status_returns_is_active():
    app = _systemd_app(unit="conjurr.service")
    cp = CompletedProcess(args=[], returncode=0, stdout="active\n", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        result = status(app)

    args = mock_run.call_args[0][0]
    assert args == ["systemctl", "--user", "is-active", "conjurr.service"]
    assert result.ok is True
    assert result.reason == "active"


# ---------------------------------------------------------------------------
# cron class tests
# ---------------------------------------------------------------------------

def test_cron_start_invokes_systemctl_start_wait_on_service_unit():
    # Manifest convention is `unit: <name>.service` for timer-driven oneshots.
    # The recovery flow needs --wait so the subsequent health probe reads the
    # new Result, not the prior invocation's.
    app = _cron_app(unit="buildarr.service")
    cp = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        result = start(app)

    calls = [c.args[0] for c in mock_run.call_args_list]
    # Two calls: reset-failed (clear prior state) then start --wait.
    assert calls[0] == ["systemctl", "--user", "reset-failed", "buildarr.service"]
    assert calls[1] == ["systemctl", "--user", "start", "--wait", "buildarr.service"]
    assert result.ok is True


def test_cron_start_strips_timer_suffix_if_someone_passed_one():
    # Defensive: legacy manifests may still have `unit: <name>.timer`. The .timer
    # is a scheduler — starting it does not invoke the service. Strip to .service.
    app = _cron_app(unit="recyclarr.timer")
    cp = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        start(app)

    last_args = mock_run.call_args_list[-1].args[0]
    assert last_args == ["systemctl", "--user", "start", "--wait", "recyclarr.service"]


def test_cron_start_without_unit_returns_not_applicable():
    # Pure crontab-driven cron entries (no systemd unit) can't be auto-recovered
    # via systemctl; fall through to not-applicable.
    app = _cron_app(unit=None)

    with patch("subprocess.run") as mock_run:
        result = start(app)

    mock_run.assert_not_called()
    assert result.ok is False
    assert result.reason == "not applicable"


def test_cron_stop_returns_not_applicable():
    # Stopping a oneshot doesn't make conceptual sense — the operator disables
    # the timer instead. Keep stop a no-op for cron.
    app = _cron_app()

    with patch("subprocess.run") as mock_run:
        result = stop(app)

    mock_run.assert_not_called()
    assert result.ok is False
    assert result.reason == "not applicable"


def test_cron_restart_invokes_systemctl_start_wait():
    # restart on cron = same as start (re-fire the oneshot). The recovery loop
    # only calls start, but the public restart() should behave consistently.
    app = _cron_app(unit="kometa.service")
    cp = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        result = restart(app)

    last_args = mock_run.call_args_list[-1].args[0]
    assert last_args == ["systemctl", "--user", "start", "--wait", "kometa.service"]
    assert result.ok is True


def test_cron_status_runs_is_active_on_timer_unit():
    app = _cron_app(unit="recyclarr.timer")
    cp = CompletedProcess(args=[], returncode=0, stdout="active\n", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        result = status(app)

    args = mock_run.call_args[0][0]
    assert "recyclarr.timer" in args
    assert result.ok is True


# ---------------------------------------------------------------------------
# library class tests
# ---------------------------------------------------------------------------

def test_library_lifecycle_all_not_applicable():
    app = _library_app()

    with patch("subprocess.run") as mock_run:
        r_start = start(app)
        r_stop = stop(app)
        r_restart = restart(app)
        r_status = status(app)

    mock_run.assert_not_called()
    for r in (r_start, r_stop, r_restart, r_status):
        assert r.ok is False
        assert r.reason == "not applicable"


# ---------------------------------------------------------------------------
# upgrade / downgrade — Phase 16
# ---------------------------------------------------------------------------

from lib.manifest import UpgradeConfig, VersionPin


def _ok_cp(stdout: str = "") -> CompletedProcess:
    return CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _attach_upgrade(app: App, **kwargs) -> App:
    """Attach an UpgradeConfig to `app` for tests."""
    raw = dict(kwargs)
    vp_raw = raw.pop("version_pin", None)
    vp = VersionPin(**vp_raw) if vp_raw else None
    app.upgrade = UpgradeConfig(
        kind=raw.get("kind", ""),
        version_pin=vp,
        raw=raw,
    )
    return app


# ---- UCC upgrade/downgrade ------------------------------------------------

def test_ucc_upgrade_runs_app_stop_then_update():
    app = _ucc_app(ucc_slug="maintainerr")

    with patch("subprocess.run", return_value=_ok_cp()) as mock_run, \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")):
        result = upgrade(app, target_version="3.4.5")

    assert result.ok is True
    args_list = [c.args[0] for c in mock_run.call_args_list]
    assert ["app-maintainerr", "stop"] in args_list
    assert ["app-maintainerr", "update"] in args_list


def test_ucc_downgrade_returns_not_supported():
    app = _ucc_app(ucc_slug="maintainerr")

    with patch("subprocess.run") as mock_run:
        result = downgrade(app, target_version="3.0.0")

    mock_run.assert_not_called()
    assert result.ok is False
    assert "downgrade" in result.reason.lower()


# ---- systemd git_checkout upgrade -----------------------------------------

def test_systemd_git_checkout_upgrade_runs_fetch_checkout_postSteps_restart():
    app = _systemd_app(unit="conjurr.service", name="conjurr")
    _attach_upgrade(
        app,
        kind="git_checkout",
        repo_path="~/.apps/conjurr/repo",
        post_steps=["cd ~/.apps/conjurr/repo && .venv/bin/pip install -r requirements.txt"],
    )

    with patch("subprocess.run", return_value=_ok_cp()) as mock_run, \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")):
        result = upgrade(app, target_version="v4.1.0")

    assert result.ok is True
    cmds = [c.args[0] for c in mock_run.call_args_list]
    # Must fetch + checkout + run post_steps + daemon-reload + restart
    assert any("fetch" in " ".join(c) for c in cmds), cmds
    assert any("checkout" in " ".join(c) and "v4.1.0" in " ".join(c) for c in cmds), cmds
    assert any(".venv/bin/pip install" in " ".join(c) for c in cmds), cmds
    assert ["systemctl", "--user", "daemon-reload"] in cmds
    assert ["systemctl", "--user", "restart", "conjurr.service"] in cmds


# ---- systemd tarball_swap upgrade -----------------------------------------

def test_systemd_tarball_swap_upgrade_downloads_and_extracts():
    app = _systemd_app(unit="listmonk.service", name="listmonk")
    _attach_upgrade(
        app,
        kind="tarball_swap",
        url_template="https://github.com/knadh/listmonk/releases/download/v{version}/listmonk_v{version}_linux_amd64.tar.gz",
        target_path="~/.apps/listmonk/bin/listmonk",
        post_steps=["~/.apps/listmonk/bin/listmonk --install --idempotent --yes"],
    )

    with patch("subprocess.run", return_value=_ok_cp()) as mock_run, \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")):
        result = upgrade(app, target_version="6.1.0")

    assert result.ok is True
    cmd_strs = [" ".join(c.args[0]) for c in mock_run.call_args_list]
    assert any("curl" in s and "6.1.0" in s for s in cmd_strs), cmd_strs
    assert any("tar" in s for s in cmd_strs), cmd_strs
    # post-step ran
    assert any("listmonk --install" in s for s in cmd_strs), cmd_strs
    # systemd restart
    assert ["systemctl", "--user", "restart", "listmonk.service"] in [c.args[0] for c in mock_run.call_args_list]


# ---- systemd zip_swap upgrade ---------------------------------------------

def test_systemd_zip_swap_upgrade_downloads_and_unzips():
    app = _systemd_app(unit="tdarr-server.service", name="tdarr-server")
    _attach_upgrade(
        app,
        kind="zip_swap",
        url_template="https://storage.tdarr.io/versions/{version}/linux_x64/Tdarr_Server.zip",
        target_dir="~/.apps/tdarr/Tdarr_Server",
    )

    with patch("subprocess.run", return_value=_ok_cp()) as mock_run, \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")):
        result = upgrade(app, target_version="2.17.01")

    assert result.ok is True
    cmd_strs = [" ".join(c.args[0]) for c in mock_run.call_args_list]
    assert any("curl" in s and "2.17.01" in s for s in cmd_strs)
    assert any("unzip" in s for s in cmd_strs)


# ---- cron upgrade (no service restart) ------------------------------------

def test_cron_tarball_swap_upgrade_does_not_restart():
    app = _cron_app(unit="recyclarr.timer", name="recyclarr")
    _attach_upgrade(
        app,
        kind="tarball_swap",
        url_template="https://github.com/recyclarr/recyclarr/releases/download/{version}/recyclarr-linux-x64.tar.xz",
        target_path="~/.apps/recyclarr/bin/recyclarr",
    )

    with patch("subprocess.run", return_value=_ok_cp()) as mock_run, \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")):
        result = upgrade(app, target_version="v8.6.0")

    assert result.ok is True
    cmds = [c.args[0] for c in mock_run.call_args_list]
    # Must NOT restart (cron class has no service)
    assert not any(c[:3] == ["systemctl", "--user", "restart"] for c in cmds if len(c) >= 4)


# ---- library pip_install upgrade ------------------------------------------

def test_library_pip_install_upgrade_runs_venv_pip():
    app = _library_app(name="python-plexapi")
    _attach_upgrade(
        app,
        kind="pip_install",
        venv_python="~/.apps/python-plexapi/venv/bin/python",
        package="plexapi",
    )

    with patch("subprocess.run", return_value=_ok_cp()) as mock_run, \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")):
        result = upgrade(app, target_version="4.18.1")

    assert result.ok is True
    cmds = [c.args[0] for c in mock_run.call_args_list]
    expected = ["~/.apps/python-plexapi/venv/bin/python", "-m", "pip", "install",
                "--upgrade", "plexapi==4.18.1"]
    # Allow the impl to expand ~ — match by suffix
    assert any(c[-3:] == ["install", "--upgrade", "plexapi==4.18.1"] for c in cmds), cmds


def test_library_pip_install_downgrade_runs_venv_pip_with_specific_version():
    app = _library_app(name="python-plexapi")
    _attach_upgrade(
        app,
        kind="pip_install",
        venv_python="~/.apps/python-plexapi/venv/bin/python",
        package="plexapi",
    )

    with patch("subprocess.run", return_value=_ok_cp()) as mock_run, \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")):
        result = downgrade(app, target_version="4.17.0")

    assert result.ok is True
    cmds = [c.args[0] for c in mock_run.call_args_list]
    assert any(c[-3:] == ["install", "--upgrade", "plexapi==4.17.0"] for c in cmds), cmds


# ---- version_pin.max ceiling enforcement ----------------------------------

def test_upgrade_target_above_max_raises_lifecycle_error():
    app = _systemd_app(unit="tdarr-server.service", name="tdarr-server")
    _attach_upgrade(
        app,
        kind="zip_swap",
        url_template="x",
        target_dir="x",
        version_pin={"max": "2.17.01", "max_reason": "GLIBC 2.34 required"},
    )

    with pytest.raises(LifecycleError) as exc_info:
        upgrade(app, target_version="2.71.01")

    msg = str(exc_info.value)
    assert "max" in msg.lower() or "ceiling" in msg.lower()
    assert "GLIBC" in msg or "2.17.01" in msg


def test_upgrade_at_max_is_allowed():
    app = _systemd_app(unit="tdarr-server.service", name="tdarr-server")
    _attach_upgrade(
        app,
        kind="zip_swap",
        url_template="https://x/{version}/Tdarr_Server.zip",
        target_dir="~/x",
        version_pin={"max": "2.17.01"},
    )

    with patch("subprocess.run", return_value=_ok_cp()), \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")):
        result = upgrade(app, target_version="2.17.01")

    assert result.ok is True


# ---- rollback on health failure -------------------------------------------

def test_upgrade_rolls_back_on_post_upgrade_health_failure():
    app = _library_app(name="python-plexapi")
    _attach_upgrade(
        app,
        kind="pip_install",
        venv_python="~/v/bin/python",
        package="plexapi",
    )

    # post_health_probe returns failure on first call (after upgrade),
    # success on second call (after rollback)
    probe_results = iter([(False, "import failed"), (True, "ok")])

    def fake_probe(*a, **kw):
        return next(probe_results)

    with patch("subprocess.run", return_value=_ok_cp()) as mock_run, \
         patch("lib.lifecycle._post_health_probe", side_effect=fake_probe):
        result = upgrade(app, target_version="4.18.1", previous_version="4.17.0")

    assert result.ok is False
    assert "rolled back" in result.reason.lower() or "rollback" in result.reason.lower()
    # Both versions appear in the recorded subprocess calls
    cmd_strs = [" ".join(c.args[0]) for c in mock_run.call_args_list]
    assert any("plexapi==4.18.1" in s for s in cmd_strs)
    assert any("plexapi==4.17.0" in s for s in cmd_strs)


def test_upgrade_no_rollback_when_previous_unknown_returns_failure():
    app = _library_app(name="python-plexapi")
    _attach_upgrade(
        app,
        kind="pip_install",
        venv_python="~/v/bin/python",
        package="plexapi",
    )

    with patch("subprocess.run", return_value=_ok_cp()), \
         patch("lib.lifecycle._post_health_probe", return_value=(False, "import failed")):
        result = upgrade(app, target_version="4.18.1")  # no previous_version given

    assert result.ok is False
    # Should not loop forever — just report failure
    assert "health" in result.reason.lower() or "import failed" in result.reason.lower()


# ---- target_version resolution from versions.env --------------------------

def test_upgrade_resolves_target_from_versions_env(tmp_path, monkeypatch):
    versions_env = tmp_path / "versions.env"
    versions_env.write_text("# header\nPYTHON_PLEXAPI_VERSION=4.18.1\n", encoding="utf-8")
    monkeypatch.setenv("MANITOBA_VERSIONS_ENV_PATH", str(versions_env))

    app = _library_app(name="python-plexapi")
    _attach_upgrade(
        app,
        kind="pip_install",
        venv_python="~/v/bin/python",
        package="plexapi",
        version_pin={"source": "versions.env", "key": "PYTHON_PLEXAPI_VERSION"},
    )

    with patch("subprocess.run", return_value=_ok_cp()) as mock_run, \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")):
        result = upgrade(app)  # no explicit target_version

    assert result.ok is True
    cmd_strs = [" ".join(c.args[0]) for c in mock_run.call_args_list]
    assert any("plexapi==4.18.1" in s for s in cmd_strs), cmd_strs


# ---- state recording ------------------------------------------------------

def test_upgrade_records_state_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))

    app = _library_app(name="python-plexapi")
    _attach_upgrade(app, kind="pip_install", venv_python="~/v/bin/python", package="plexapi")

    with patch("subprocess.run", return_value=_ok_cp()), \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")):
        upgrade(app, target_version="4.18.1")

    state_file = tmp_path / "state.json"
    assert state_file.exists()
    import json as _json
    data = _json.loads(state_file.read_text())
    assert "python-plexapi" in data["apps"]
    entry = data["apps"]["python-plexapi"]
    assert entry["event"] == "upgraded"
    assert entry["version"] == "4.18.1"


# ---------------------------------------------------------------------------
# MANITOBA_DRY_RUN env hook
# ---------------------------------------------------------------------------

def test_dry_run_env_skips_subprocess(monkeypatch):
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    app = _ucc_app(ucc_slug="sonarr")

    with patch("subprocess.run") as mock_run:
        result = start(app)

    mock_run.assert_not_called()
    assert result.ok is True
    assert result.reason == "dry-run"
    assert result.duration_s == 0.0
    assert result.stdout == ""
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# timeout_s uses app default when not specified
# ---------------------------------------------------------------------------

def test_timeout_uses_app_default_when_not_specified():
    app = _ucc_app(ucc_slug="sonarr", lifecycle_timeout_s=30.0)
    cp = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=cp) as mock_run:
        start(app)

    kwargs = mock_run.call_args[1]
    assert kwargs.get("timeout") == 30.0
