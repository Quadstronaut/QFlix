"""lifecycle + the `pending-swap` manifest state (QFLX-25, spec 5.9 step 7).

The A1 manifest flip (class systemd, ucc_dormant, unit) merges BEFORE the swap
runs, carrying `swap_state: pending-swap`. For that window the UCC container is
still the live runtime. appctl (QFLX-20) already dispatches such an app as the
UCC app it still is; lifecycle must agree, or pusher recovery would run
`systemctl --user restart qflix-<slug>.service` and start the native unit next
to the live container (two runtimes, I-6).

Also pinned: native upgrades are refused while pending-swap (version parity,
I-10), and tarball_swap substitutes `{version}` into target_dir / target_path /
post_steps so a native app can land each version in bin/<ver> (spec 5.1).
"""
from __future__ import annotations

from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from lib.lifecycle import restart, start, status, stop, upgrade
from lib.manifest import App, HealthConfig, UpgradeConfig


def _app(*, pending=True, dormant=True, upgrade_raw=None) -> App:
    raw = {"class": "systemd", "ucc_slug": "unpackerr", "unit": "qflix-unpackerr.service"}
    if dormant:
        raw["ucc_dormant"] = True
    if pending:
        raw["swap_state"] = "pending-swap"
    up = UpgradeConfig(kind=upgrade_raw["kind"], raw=upgrade_raw) if upgrade_raw else None
    return App(
        name="unpackerr", class_="systemd", kuma_monitor="Unpackerr",
        health=HealthConfig(kind="process_pattern", raw={"pattern": "/unpackerr"}),
        defaults={"lifecycle_timeout_s": 5.0}, upgrade=up, raw=raw,
    )


def _cp(out="", rc=0):
    return CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


@pytest.mark.parametrize("fn,verb", [(start, "start"), (stop, "stop"), (restart, "restart")])
def test_pending_swap_routes_verbs_to_ucc_not_the_native_unit(fn, verb):
    with patch("subprocess.run", return_value=_cp()) as run:
        r = fn(_app())
    assert r.ok is True, r.reason
    argv = run.call_args[0][0]
    assert argv == ["app-unpackerr", verb]
    assert "systemctl" not in argv


def test_pending_swap_status_is_the_ucc_status():
    panel = '{"data": {"version": "0.16.1"}, "result": true}\n'
    with patch("subprocess.run", return_value=_cp(out=panel)) as run:
        r = status(_app())
    assert r.ok is True
    assert run.call_args[0][0] == ["app-unpackerr", "version"]


def test_converted_app_without_pending_swap_uses_the_unit():
    with patch("subprocess.run", return_value=_cp()) as run:
        restart(_app(pending=False))
    assert run.call_args[0][0] == ["systemctl", "--user", "restart", "qflix-unpackerr.service"]


def test_pending_swap_upgrade_is_refused():
    up = {"kind": "tarball_swap", "url_template": "https://x/{version}.tar.gz",
          "target_dir": "~/.apps/unpackerr/bin/{version}"}
    with patch("subprocess.run") as run, patch("lib.lifecycle._record_state"):
        r = upgrade(_app(upgrade_raw=up), target_version="0.16.2")
    assert r.ok is False
    assert "pending-swap" in r.reason
    run.assert_not_called()


def test_tarball_swap_substitutes_version_into_dir_and_post_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("QFLIX_SWAP_DIR", str(tmp_path / "swap"))
    up = {"kind": "tarball_swap",
          "url_template": "https://example.invalid/v{version}/u_{version}.tar.gz",
          "target_dir": "~/.apps/unpackerr/bin/{version}",
          "post_steps": ["ln -sfn {version} ~/.apps/unpackerr/bin/current"]}
    calls = []

    def fake(args, **kw):
        calls.append(args)
        return _cp()

    with patch("subprocess.run", side_effect=fake), \
         patch("lib.lifecycle._post_health_probe", return_value=(True, "ok")), \
         patch("lib.lifecycle._record_state"):
        r = upgrade(_app(pending=False, upgrade_raw=up), target_version="0.16.2")
    assert r.ok is True, r.reason
    shell = [c[2] for c in calls if c[:2] == ["bash", "-c"]]
    assert any("/.apps/unpackerr/bin/0.16.2'" in s and "tar " in s for s in shell), shell
    assert "ln -sfn 0.16.2 ~/.apps/unpackerr/bin/current" in shell
    assert not any("{version}" in s for s in shell)
