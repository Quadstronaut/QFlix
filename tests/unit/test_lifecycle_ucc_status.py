"""lifecycle._ucc_status + the dormant guard (QFLX-18, spec F-3 / I-9).

`app-<slug> status` is not a UCC subcommand, so the old _ucc_status always
"failed" with a usage error. Status is now `app-<slug> version` plus a TCP
probe of the app's port (health.port_secret, health.hostname or 127.0.0.1).

The dormant guard mirrors scripts/lib/appctl: a ucc-class app carrying
`ucc_dormant: true` must never be started, restarted or updated through UCC;
only `stop` may reach it.
"""
from __future__ import annotations

import socket
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from lib import lifecycle
from lib.lifecycle import restart, start, status, stop, upgrade
from lib.manifest import App, HealthConfig


def _app(*, health_raw=None, dormant=False, name="sonarr") -> App:
    raw = {"class": "ucc", "ucc_slug": name}
    if dormant:
        raw["ucc_dormant"] = True
    return App(
        name=name, class_="ucc", kuma_monitor=None,
        health=HealthConfig(kind="http_api", raw=health_raw or {}),
        defaults={"lifecycle_timeout_s": 5.0}, upgrade=None, raw=raw,
    )


PANEL = '{"data": {"version": "4.0.20"}, "result": true}\n'


def _cp(out=PANEL, rc=0):
    return CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


@pytest.fixture
def listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    yield s.getsockname()[1]
    s.close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_ucc_status_never_runs_app_status(tmp_path, monkeypatch, listener):
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path))
    (tmp_path / "sonarr.port").write_text(str(listener))
    with patch("subprocess.run", return_value=_cp()) as run:
        r = status(_app(health_raw={"port_secret": "sonarr.port"}))
    argvs = [c.args[0] for c in run.call_args_list]
    assert ["app-sonarr", "version"] in argvs
    assert ["app-sonarr", "status"] not in argvs
    assert r.ok is True
    assert "version 4.0.20;" in r.reason and str(listener) in r.reason


def test_ucc_status_port_closed_is_not_ok(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path))
    (tmp_path / "sonarr.port").write_text(str(port))
    with patch("subprocess.run", return_value=_cp()):
        r = status(_app(health_raw={"port_secret": "sonarr.port"}))
    assert r.ok is False
    assert "not listening" in r.reason


def test_ucc_status_version_failure_is_not_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path))
    with patch("subprocess.run", return_value=_cp(out="", rc=1)):
        r = status(_app(health_raw={"port_secret": "sonarr.port"}))
    assert r.ok is False


def test_ucc_status_without_port_is_version_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path))
    with patch("subprocess.run", return_value=_cp(out=PANEL.replace("4.0.20", "0.16.1"))) as run:
        r = status(_app(name="unpackerr"))
    assert [c.args[0] for c in run.call_args_list] == [["app-unpackerr", "version"]]
    assert r.ok is True and "0.16.1" in r.reason


def test_ucc_status_no_version_subcommand_no_port_is_unknown(tmp_path, monkeypatch):
    # app-postgres has no `version` subcommand (box, 2026-10-10).
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path))
    cp = CompletedProcess(args=[], returncode=1, stdout="", stderr="Unknown command: version\n")
    with patch("subprocess.run", return_value=cp):
        r = status(_app(name="postgres"))
    assert r.ok is False and "unknown" in r.reason


def test_ucc_status_no_version_subcommand_falls_back_to_port(tmp_path, monkeypatch, listener):
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path))
    (tmp_path / "pg.port").write_text(str(listener))
    cp = CompletedProcess(args=[], returncode=1, stdout="Unknown command: version\n", stderr="")
    with patch("subprocess.run", return_value=cp):
        r = status(_app(name="postgres", health_raw={"port_secret": "pg.port"}))
    assert r.ok is True and "version n/a" in r.reason


def test_ucc_status_missing_port_secret_is_not_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path))
    with patch("subprocess.run", return_value=_cp()):
        r = status(_app(health_raw={"port_secret": "absent.port"}))
    assert r.ok is False
    assert "port" in r.reason


# --- dormant guard ------------------------------------------------------------

@pytest.mark.parametrize("fn", [start, restart, status])
def test_dormant_ucc_refuses_non_stop(fn):
    with patch("subprocess.run") as run:
        r = fn(_app(dormant=True))
    assert r.ok is False
    assert "dormant" in r.reason
    run.assert_not_called()


def test_dormant_ucc_stop_allowed():
    with patch("subprocess.run", return_value=_cp(out="")) as run:
        r = stop(_app(dormant=True))
    assert r.ok is True
    assert run.call_args[0][0] == ["app-sonarr", "stop"]


def test_dormant_ucc_update_refused():
    with patch("subprocess.run") as run, \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")), \
         patch("lib.lifecycle._record_state"):
        r = upgrade(_app(dormant=True), target_version="1.0")
    assert r.ok is False
    assert "dormant" in r.reason
    run.assert_not_called()


def test_dry_run_status_skips_probe(monkeypatch):
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    with patch("subprocess.run") as run:
        r = status(_app(health_raw={"port_secret": "sonarr.port"}))
    assert r.ok is True
    run.assert_not_called()
