"""tests/unit/test_health.py — TDD tests for lib/health.py.

Tests are written before implementation (red phase). Each test covers one
probe kind and one failure mode. No real network, no real secrets, no SSH.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch, call

import pytest
import requests

from lib.manifest import App, HealthConfig
from lib import health as health_mod
from lib.health import HealthResult, probe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(
    kind: str,
    *,
    path_template: str = "/{urlbase}/api/v3/system/status",
    auth_header: str | None = "X-Api-Key",
    auth_secret: str | None = "myapp.key",
    port_secret: str | None = "myapp.port",
    port_source: str | None = None,
    urlbase_secret: str | None = "myapp.urlbase",
    expect_status: int | None = None,
    unit: str = "myapp.service",
    venv_python: str = "~/.venv/bin/python",
    module: str = "plexapi",
    defaults: dict | None = None,
) -> App:
    raw: dict = {"kind": kind}
    if path_template is not None:
        raw["path_template"] = path_template
    if auth_header is not None:
        raw["auth_header"] = auth_header
    if auth_secret is not None:
        raw["auth_secret"] = auth_secret
    if port_secret is not None:
        raw["port_secret"] = port_secret
    if port_source is not None:
        raw["port_source"] = port_source
    if urlbase_secret is not None:
        raw["urlbase_secret"] = urlbase_secret
    if expect_status is not None:
        raw["expect_status"] = expect_status
    if unit is not None:
        raw["unit"] = unit
    if venv_python is not None:
        raw["venv_python"] = venv_python
    if module is not None:
        raw["module"] = module

    return App(
        name="myapp",
        class_="ucc",
        kuma_monitor="MyApp",
        health=HealthConfig(kind=kind, raw=raw),
        defaults=defaults or {"health_timeout_s": 5},
        upgrade=None,
        raw={},
    )


def _write_secret(secrets_dir: Path, name: str, value: str) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / name).write_text(value + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# test_http_api_200_with_apikey
# ---------------------------------------------------------------------------

def test_http_api_200_with_apikey(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.key", "APIKEY123")
    _write_secret(secrets_dir, "myapp.port", "17001")
    _write_secret(secrets_dir, "myapp.urlbase", "sonarr")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app("http_api")

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.get", return_value=mock_resp) as mock_get:
        result = probe(app)

    assert result.ok is True
    assert isinstance(result.latency_ms, int)
    assert result.latency_ms >= 0

    call_args = mock_get.call_args
    url = call_args[0][0]
    assert url == "http://127.0.0.1:17001/sonarr/api/v3/system/status"
    headers = call_args[1].get("headers", {})
    assert headers.get("X-Api-Key") == "APIKEY123"


# ---------------------------------------------------------------------------
# test_http_api_substitutes_urlbase_template
# ---------------------------------------------------------------------------

def test_http_api_substitutes_urlbase_template(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.key", "KEY")
    _write_secret(secrets_dir, "myapp.port", "17001")
    _write_secret(secrets_dir, "myapp.urlbase", "sonarr")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app("http_api", path_template="/{urlbase}/api/v3/x")

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.get", return_value=mock_resp) as mock_get:
        probe(app)

    url = mock_get.call_args[0][0]
    assert "/sonarr/api/v3/x" in url


def test_http_api_substitutes_empty_urlbase(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.key", "KEY")
    _write_secret(secrets_dir, "myapp.port", "17001")
    _write_secret(secrets_dir, "myapp.urlbase", "")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app("http_api", path_template="/{urlbase}/api/v3/x")

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.get", return_value=mock_resp) as mock_get:
        probe(app)

    url = mock_get.call_args[0][0]
    assert "api/v3/x" in url
    assert "//api/v3/x" not in url, "double-slash should be collapsed"


# ---------------------------------------------------------------------------
# test_http_api_timeout
# ---------------------------------------------------------------------------

def test_http_api_timeout(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.key", "KEY")
    _write_secret(secrets_dir, "myapp.port", "17001")
    _write_secret(secrets_dir, "myapp.urlbase", "sonarr")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app("http_api")

    with patch("requests.get", side_effect=requests.Timeout("timed out")):
        result = probe(app)

    assert result.ok is False
    assert result.reason == "timeout"


# ---------------------------------------------------------------------------
# test_http_api_non_200
# ---------------------------------------------------------------------------

def test_http_api_non_200(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.key", "KEY")
    _write_secret(secrets_dir, "myapp.port", "17001")
    _write_secret(secrets_dir, "myapp.urlbase", "sonarr")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app("http_api", expect_status=200)

    mock_resp = MagicMock()
    mock_resp.status_code = 502

    with patch("requests.get", return_value=mock_resp):
        result = probe(app)

    assert result.ok is False
    assert "502" in result.reason


# ---------------------------------------------------------------------------
# test_http_api_connection_error
# ---------------------------------------------------------------------------

def test_http_api_connection_error(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.key", "KEY")
    _write_secret(secrets_dir, "myapp.port", "17001")
    _write_secret(secrets_dir, "myapp.urlbase", "sonarr")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app("http_api")

    with patch("requests.get", side_effect=requests.ConnectionError("refused")):
        result = probe(app)

    assert result.ok is False
    assert result.reason.lower().startswith("connection")


# ---------------------------------------------------------------------------
# test_http_api_no_auth_secret (auth_secret absent — request sent without header)
# ---------------------------------------------------------------------------

def test_http_api_no_auth_secret(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.port", "17001")
    _write_secret(secrets_dir, "myapp.urlbase", "app")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app("http_api", auth_header=None, auth_secret=None)

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.get", return_value=mock_resp) as mock_get:
        result = probe(app)

    assert result.ok is True
    headers = mock_get.call_args[1].get("headers", {})
    assert "X-Api-Key" not in headers


# ---------------------------------------------------------------------------
# test_http_root_accepts_200_302_401_default
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status_code", [200, 302, 401])
def test_http_root_accepts_200_302_401_default(status_code, tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.port", "17002")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app(
        "http_root",
        path_template=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = status_code

    with patch("requests.get", return_value=mock_resp):
        result = probe(app)

    assert result.ok is True, f"Expected ok=True for status {status_code}"


# ---------------------------------------------------------------------------
# test_http_root_explicit_expect_status
# ---------------------------------------------------------------------------

def test_http_root_explicit_expect_status(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.port", "17002")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app(
        "http_root",
        path_template=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
        expect_status=200,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 302

    with patch("requests.get", return_value=mock_resp):
        result = probe(app)

    assert result.ok is False
    assert "302" in result.reason


# ---------------------------------------------------------------------------
# test_http_root_port_source_env_file
# ---------------------------------------------------------------------------

def test_http_root_port_source_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "app.env"
    env_file.write_text("PORT=18080\n", encoding="utf-8")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path / "secrets"))

    app = _make_app(
        "http_root",
        port_secret=None,
        port_source=f"env_file:{env_file}:PORT",
        path_template=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.get", return_value=mock_resp) as mock_get:
        result = probe(app)

    url = mock_get.call_args[0][0]
    assert "18080" in url
    assert result.ok is True


# ---------------------------------------------------------------------------
# test_http_root_port_source_json_file
# ---------------------------------------------------------------------------

def test_http_root_port_source_json_file(tmp_path, monkeypatch):
    json_file = tmp_path / "config.json"
    json_file.write_text(json.dumps({"serverPort": 18081}), encoding="utf-8")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path / "secrets"))

    app = _make_app(
        "http_root",
        port_secret=None,
        port_source=f"json_file:{json_file}:serverPort",
        path_template=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.get", return_value=mock_resp) as mock_get:
        result = probe(app)

    url = mock_get.call_args[0][0]
    assert "18081" in url
    assert result.ok is True


# ---------------------------------------------------------------------------
# test_systemd_only_active
# ---------------------------------------------------------------------------

def test_systemd_only_active(monkeypatch):
    app = _make_app(
        "systemd_only",
        port_secret=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
        unit="recyclarr.timer",
    )
    app.health.raw["unit"] = "recyclarr.timer"

    cp = CompletedProcess(args=[], returncode=0, stdout="active\n", stderr="")
    with patch("subprocess.run", return_value=cp):
        result = probe(app)

    assert result.ok is True
    assert result.reason == "active"
    assert result.latency_ms is None


# ---------------------------------------------------------------------------
# test_systemd_only_inactive
# ---------------------------------------------------------------------------

def test_systemd_only_inactive(monkeypatch):
    app = _make_app(
        "systemd_only",
        port_secret=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
        unit="recyclarr.timer",
    )

    cp = CompletedProcess(args=[], returncode=3, stdout="inactive\n", stderr="")
    with patch("subprocess.run", return_value=cp):
        result = probe(app)

    assert result.ok is False
    assert result.reason == "inactive"


# ---------------------------------------------------------------------------
# test_systemd_only_failed
# ---------------------------------------------------------------------------

def test_systemd_only_failed(monkeypatch):
    app = _make_app(
        "systemd_only",
        port_secret=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
        unit="myapp.service",
    )

    cp = CompletedProcess(args=[], returncode=3, stdout="failed\n", stderr="")
    with patch("subprocess.run", return_value=cp):
        result = probe(app)

    assert result.ok is False
    assert result.reason == "failed"


# ---------------------------------------------------------------------------
# test_systemd_oneshot_success — last invocation succeeded
# ---------------------------------------------------------------------------

def test_systemd_oneshot_success():
    app = _make_app(
        "systemd_oneshot",
        port_secret=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
        unit="recyclarr.service",
    )
    app.health.raw["unit"] = "recyclarr.service"

    cp = CompletedProcess(
        args=[],
        returncode=0,
        stdout="Result=success\nActiveState=inactive\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=cp):
        result = probe(app)

    assert result.ok is True
    assert "success" in result.reason


# ---------------------------------------------------------------------------
# test_systemd_oneshot_failed — last invocation exited non-zero
# ---------------------------------------------------------------------------

def test_systemd_oneshot_failed():
    app = _make_app(
        "systemd_oneshot",
        port_secret=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
        unit="buildarr.service",
    )
    app.health.raw["unit"] = "buildarr.service"

    cp = CompletedProcess(
        args=[],
        returncode=0,
        stdout="Result=exit-code\nActiveState=failed\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=cp):
        result = probe(app)

    assert result.ok is False
    assert "exit-code" in result.reason
    assert "failed" in result.reason


# ---------------------------------------------------------------------------
# test_systemd_oneshot_in_flight — currently activating, prior Result stale
# ---------------------------------------------------------------------------

def test_systemd_oneshot_in_flight():
    """During an active run, ActiveState=activating while Result still holds
    the PRIOR invocation's value. Treating this as ok is what prevents the
    recovery loop's 10/30/60s backoff from mis-reading a slow-but-OK run."""
    app = _make_app(
        "systemd_oneshot",
        port_secret=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
        unit="buildarr.service",
    )
    app.health.raw["unit"] = "buildarr.service"

    cp = CompletedProcess(
        args=[],
        returncode=0,
        stdout="Result=exit-code\nActiveState=activating\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=cp):
        result = probe(app)

    assert result.ok is True
    assert "in-flight" in result.reason


# ---------------------------------------------------------------------------
# test_systemd_oneshot_never_ran — fresh install, Result empty
# ---------------------------------------------------------------------------

def test_systemd_oneshot_never_ran():
    """A newly-installed timer-driven service has no Result yet; treat as ok
    so newly-deployed timers don't show red before their first fire."""
    app = _make_app(
        "systemd_oneshot",
        port_secret=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
        unit="brand-new.service",
    )
    app.health.raw["unit"] = "brand-new.service"

    cp = CompletedProcess(
        args=[],
        returncode=0,
        stdout="Result=\nActiveState=inactive\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=cp):
        result = probe(app)

    assert result.ok is True


# ---------------------------------------------------------------------------
# test_systemd_oneshot_no_unit — manifest misconfiguration
# ---------------------------------------------------------------------------

def test_systemd_oneshot_no_unit():
    app = _make_app(
        "systemd_oneshot",
        port_secret=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
        unit=None,
    )

    result = probe(app)
    assert result.ok is False
    assert "no unit" in result.reason


# ---------------------------------------------------------------------------
# test_port_listen_open
# ---------------------------------------------------------------------------

def test_port_listen_open(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.port", "17003")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app(
        "port_listen",
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
    )

    mock_sock = MagicMock()
    with patch("socket.create_connection", return_value=mock_sock) as mock_conn:
        result = probe(app)

    assert result.ok is True
    assert isinstance(result.latency_ms, int)
    mock_sock.close.assert_called_once()


# ---------------------------------------------------------------------------
# test_port_listen_refused
# ---------------------------------------------------------------------------

def test_port_listen_refused(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.port", "17003")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app(
        "port_listen",
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
    )

    with patch("socket.create_connection", side_effect=ConnectionRefusedError()):
        result = probe(app)

    assert result.ok is False
    assert result.reason == "connection refused"


# ---------------------------------------------------------------------------
# test_port_listen_timeout
# ---------------------------------------------------------------------------

def test_port_listen_timeout(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.port", "17003")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app(
        "port_listen",
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
    )

    with patch("socket.create_connection", side_effect=socket.timeout()):
        result = probe(app)

    assert result.ok is False
    assert result.reason == "timeout"


# ---------------------------------------------------------------------------
# test_import_check_success
# ---------------------------------------------------------------------------

def test_import_check_success(monkeypatch):
    app = _make_app(
        "import_check",
        port_secret=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
        venv_python="~/.venv/bin/python",
        module="plexapi",
    )

    cp = CompletedProcess(args=[], returncode=0, stdout="4.18.0\n", stderr="")
    with patch("subprocess.run", return_value=cp):
        result = probe(app)

    assert result.ok is True
    assert result.reason == "4.18.0"
    assert result.latency_ms is None


# ---------------------------------------------------------------------------
# test_import_check_failure
# ---------------------------------------------------------------------------

def test_import_check_failure(monkeypatch):
    app = _make_app(
        "import_check",
        port_secret=None,
        auth_header=None,
        auth_secret=None,
        urlbase_secret=None,
        venv_python="~/.venv/bin/python",
        module="plexapi",
    )

    cp = CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="ModuleNotFoundError: No module named 'plexapi'\n",
    )
    with patch("subprocess.run", return_value=cp):
        result = probe(app)

    assert result.ok is False
    assert "ModuleNotFoundError" in result.reason


# ---------------------------------------------------------------------------
# test_process_pattern_*
# ---------------------------------------------------------------------------

def _make_process_pattern_app(pattern: str | None = "/app/unpackerr") -> App:
    raw: dict = {"kind": "process_pattern"}
    if pattern is not None:
        raw["pattern"] = pattern
    return App(
        name="unpackerr",
        class_="ucc",
        kuma_monitor="Unpackerr",
        health=HealthConfig(kind="process_pattern", raw=raw),
        defaults={"health_timeout_s": 5},
        upgrade=None,
        raw={},
    )


def test_process_pattern_match():
    app = _make_process_pattern_app("/app/unpackerr")
    cp = CompletedProcess(args=[], returncode=0, stdout="12345\n67890\n", stderr="")
    with patch("subprocess.run", return_value=cp) as mock_run:
        result = probe(app)

    assert result.ok is True
    assert result.latency_ms is None
    assert "2" in result.reason  # 2 matches
    called = mock_run.call_args[0][0]
    assert called[0] == "pgrep"
    assert "-f" in called
    assert "/app/unpackerr" in called


def test_process_pattern_no_match():
    app = _make_process_pattern_app("postgres: checkpointer")
    cp = CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    with patch("subprocess.run", return_value=cp):
        result = probe(app)

    assert result.ok is False
    assert "no process" in result.reason.lower()


def test_process_pattern_missing_pattern():
    app = _make_process_pattern_app(pattern=None)
    result = probe(app)

    assert result.ok is False
    assert "pattern" in result.reason.lower()


def test_process_pattern_timeout():
    app = _make_process_pattern_app("/app/unpackerr")
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=5)):
        result = probe(app)

    assert result.ok is False
    assert result.reason == "timeout"


# ---------------------------------------------------------------------------
# test_unknown_kind_raises
# ---------------------------------------------------------------------------

def test_unknown_kind_raises():
    app = _make_app("wat", port_secret=None, auth_header=None, auth_secret=None, urlbase_secret=None)

    with pytest.raises(Exception) as exc_info:
        probe(app)

    assert "wat" in str(exc_info.value).lower() or "kind" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# test_timeout_s_parameter_overrides_default
# ---------------------------------------------------------------------------

def test_timeout_s_parameter_overrides_default(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    _write_secret(secrets_dir, "myapp.key", "KEY")
    _write_secret(secrets_dir, "myapp.port", "17001")
    _write_secret(secrets_dir, "myapp.urlbase", "app")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(secrets_dir))

    app = _make_app("http_api")

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.get", return_value=mock_resp) as mock_get:
        probe(app, timeout_s=30.0)

    kwargs = mock_get.call_args[1]
    assert kwargs["timeout"] == 30.0
