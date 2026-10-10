"""QFLX-24 generic-host smoke: native.sh render_unit -> real `systemctl --user` start.

Runs in the `generic-host` CI job as a linger-enabled test user on ubuntu (the
generic host profile: no Ultra.cc, no appctl). Outside that job there is no user
manager, so the systemd test SKIPS rather than fails.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "scripts" / "lib" / "native.sh"
SLUG = "ci-smoke"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _user_manager_up() -> bool:
    if shutil.which("systemctl") is None or os.name != "posix":
        return False
    r = subprocess.run(["systemctl", "--user", "is-system-running"],
                       capture_output=True, text=True)
    return r.stdout.strip() in {"running", "degraded"}


def _render() -> str:
    r = subprocess.run(
        ["bash", "-c", f'source "{NATIVE.as_posix()}"; native_render_unit {SLUG} go smoke.sh "--serve"'],
        capture_output=True, text=True, check=True)
    return r.stdout


@needs_bash
def test_generic_render_has_no_ucc_artifacts():
    unit = _render()
    assert "172.17.0.1" not in unit
    assert "app-" not in unit
    assert "ExecStart=%h/.apps/ci-smoke/bin/current/smoke.sh --serve" in unit


@needs_bash
@pytest.mark.skipif(not _user_manager_up(), reason="no systemd --user manager (CI generic-host job only)")
def test_rendered_unit_starts_under_systemd_user():
    home = Path.home()
    bindir = home / ".apps" / SLUG / "bin" / "v1"
    bindir.mkdir(parents=True, exist_ok=True)
    exe = bindir / "smoke.sh"
    exe.write_text("#!/bin/sh\nexec sleep 300\n", newline="\n")
    exe.chmod(0o755)
    cur = bindir.parent / "current"
    if cur.is_symlink() or cur.exists():
        cur.unlink()
    cur.symlink_to("v1")
    envdir = home / ".config" / "qflix"
    envdir.mkdir(parents=True, exist_ok=True)
    (envdir / f"{SLUG}.env").write_text("GOMAXPROCS=4\n")
    udir = home / ".config" / "systemd" / "user"
    udir.mkdir(parents=True, exist_ok=True)
    name = f"qflix-{SLUG}.service"
    (udir / name).write_text(_render())

    def sc(*a):
        return subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True)

    try:
        assert sc("daemon-reload").returncode == 0
        r = sc("start", name)
        assert r.returncode == 0, r.stderr
        for _ in range(20):
            if sc("is-active", name).stdout.strip() == "active":
                break
            time.sleep(0.5)
        assert sc("is-active", name).stdout.strip() == "active"
    finally:
        sc("stop", name)
        (udir / name).unlink(missing_ok=True)
        sc("daemon-reload")
