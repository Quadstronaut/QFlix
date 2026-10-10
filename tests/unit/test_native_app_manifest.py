"""Converted (native) apps in the REAL manifest (QFLX-25, plan "App conversions").

Every app ticket carries "the manifest test": a systemd app with native
metadata (a `ucc_slug`, i.e. it used to be a UCC container) must have its
installer, an exact version pin, an upgrade block and a tracked unit. Also
pinned for the pilot:
  * the generated app-upgrade-all skip list carries unpackerr (I-9);
  * ZERO UCC starts of unpackerr once converted: appctl and lifecycle route
    start/restart/status to the native unit, never to the panel tool (O-3);
  * 31-unpackerr.sh restarts and reports through ~/bin/appctl.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
import yaml

from lib import lifecycle
from lib.manifest import load as load_manifest

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "manifest" / "apps.yaml"
APPCTL = REPO / "scripts" / "lib" / "appctl"
LIB = REPO / "scripts" / "maint" / "lib"
UPGRADE_KINDS = {"pip_install", "git_checkout", "tarball_swap", "zip_swap"}


def _apps() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["apps"]


def _converted() -> dict:
    return {k: v for k, v in _apps().items()
            if isinstance(v, dict) and v.get("class") == "systemd" and v.get("ucc_slug")}


def _versions_env() -> dict:
    out = {}
    for line in (REPO / "versions.env").read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_unpackerr_is_converted():
    assert "unpackerr" in _converted()


@pytest.mark.parametrize("name", sorted(_converted()))
def test_converted_app_has_installer_pin_upgrade_and_unit(name):
    a = _converted()[name]
    slug = a["ucc_slug"]
    assert a["unit"] == f"qflix-{slug}.service"
    assert (REPO / "scripts" / "maint" / "systemd" / a["unit"]).is_file()
    installers = list((REPO / "scripts" / "configure").glob(f"3[0-9][0-9]-native-{slug}-install.sh"))
    assert len(installers) == 1, f"no 3NN-native-{slug}-install.sh"
    up = a.get("upgrade") or {}
    assert up.get("kind") in UPGRADE_KINDS
    vp = up.get("version_pin") or {}
    assert vp.get("source") == "versions.env"
    assert vp.get("key") in _versions_env(), vp
    assert a.get("ucc_dormant") is True, "the UCC container must stay dormant (I-8)"


def test_flaresolverr_is_converted_pending_swap():
    a = _converted()["flaresolverr"]
    assert a["swap_state"] == "pending-swap" and a["unit"] == "qflix-flaresolverr.service"
    # The probe still targets the bridge gateway, not loopback (never widen the bind).
    assert a["health"]["hostname_ref"] == "net.app_host"
    # The release wraps everything in flaresolverr/: the upgrade must flatten it.
    assert any("mv .fs-tmp/* ." in step for step in a["upgrade"]["post_steps"])


def test_real_manifest_loads_with_the_flip():
    app = load_manifest(MANIFEST).app("unpackerr")
    assert app.class_ == "systemd" and app.upgrade.kind == "tarball_swap"


def test_generated_skip_list_carries_unpackerr():
    r = subprocess.run([sys.executable, str(LIB / "ucc_skip.py"), "--list",
                        "--manifest", str(MANIFEST)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "unpackerr" in r.stdout.split()
    assert "flaresolverr" in r.stdout.split()


# --- zero UCC starts after the swap (O-3 / F8 dependency) --------------------------

def _post_swap_manifest(tmp_path: Path) -> Path:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    data["apps"]["unpackerr"].pop("swap_state", None)
    p = tmp_path / "apps.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


_STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\nexit 0\n'


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("verb", ["start", "restart", "status", "stop"])
def test_appctl_never_starts_the_ucc_container_after_the_swap(tmp_path, verb):
    man = _post_swap_manifest(tmp_path)
    binp = tmp_path / "bin"
    binp.mkdir()
    for n in ("app-unpackerr", "systemctl"):
        (binp / n).write_text(_STUB, newline="\n")
        (binp / n).chmod(0o755)
    log = tmp_path / "argv.log"
    env = dict(os.environ, HOME=tmp_path.as_posix(), STUB_LOG=log.as_posix(),
               PATH=binp.as_posix() + os.pathsep + os.environ.get("PATH", ""),
               APPCTL_MANIFEST=man.as_posix(), APPCTL_PYTHON=Path(sys.executable).as_posix(),
               APPCTL_LIB=LIB.as_posix())
    r = subprocess.run(["bash", APPCTL.as_posix(), verb, "unpackerr"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    argv = log.read_text() if log.exists() else ""
    assert "app-unpackerr" not in argv
    want = "is-active" if verb == "status" else verb
    assert f"systemctl --user {want} qflix-unpackerr.service" in argv


@pytest.mark.parametrize("fn", [lifecycle.start, lifecycle.restart, lifecycle.status])
def test_lifecycle_never_starts_the_ucc_container_after_the_swap(tmp_path, fn):
    app = load_manifest(_post_swap_manifest(tmp_path)).app("unpackerr")
    with patch("subprocess.run", return_value=CompletedProcess([], 0, "active\n", "")) as run:
        fn(app)
    for call in run.call_args_list:
        assert not call[0][0][0].startswith("app-"), call


def test_31_unpackerr_goes_through_appctl_only():
    text = (REPO / "scripts" / "configure" / "31-unpackerr.sh").read_text(encoding="utf-8")
    code = [l for l in text.splitlines() if not l.strip().startswith("#")]
    assert not any("app-unpackerr" in l for l in code)
    assert "~/bin/appctl restart unpackerr" in text
    assert "~/bin/appctl status unpackerr" in text
    assert "systemctl --user is-active unpackerr" not in text
