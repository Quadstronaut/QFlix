"""sonarr2 in the REAL manifest after the QFLX-30 flip (UCC divorce A6).

The generic converted-app checks live in test_native_app_manifest.py; this file
pins what is specific to sonarr2:
  * the flip is `pending-swap` (the container stays the live runtime until the
    swap runs) and the UCC container is dormant;
  * the version pin in versions.env is the FOUR-part build, the installer and
    the manifest upgrade block agree on it, and the tarball URL carries it;
  * the generated app-upgrade-all skip list carries sonarr2 (I-9);
  * while pending-swap appctl still routes to the panel tool, and after the
    swap (swap_state dropped) it routes to the native unit and never to the
    panel tool (O-3);
  * the primary sonarr stays a UCC container (A8 is its own ticket).
"""
from __future__ import annotations

import os
import re
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
INSTALLER = REPO / "scripts" / "configure" / "305-native-sonarr2-install.sh"


def _app() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["apps"]["sonarr2"]


def _pin() -> str:
    for line in (REPO / "versions.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("SONARR2_VERSION="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("SONARR2_VERSION missing from versions.env")


def test_sonarr2_is_converted_but_pending_swap_and_dormant():
    a = _app()
    assert a["class"] == "systemd" and a["unit"] == "qflix-sonarr2.service"
    assert a["ucc_slug"] == "sonarr2" and a["ucc_dormant"] is True
    assert a["swap_state"] == "pending-swap"
    assert a["health"]["require_unit_active"] is True
    # the health probe (Kuma "Sonarr Anime") is unchanged: same secrets, same path
    assert a["kuma_monitor"] == "Sonarr Anime"
    assert a["health"]["port_secret"] == "sonarr2.port"
    assert a["health"]["path_template"] == "/{urlbase}/api/v3/system/status"


def test_primary_sonarr_is_its_own_conversion_never_sonarr2s_unit():
    """The primary sonarr converts on its own ticket (QFLX-32): separate unit,
    separate slug, so neither flip can drive the other's runtime."""
    prim = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["apps"]["sonarr"]
    assert prim["unit"] == "qflix-sonarr.service" and prim["ucc_slug"] == "sonarr"
    assert prim["unit"] != _app()["unit"]


def test_pin_is_the_four_part_build_and_matches_installer_and_manifest():
    pin = _pin()
    assert pin.count(".") == 3
    text = INSTALLER.read_text(encoding="utf-8")
    assert f'VERSION="{pin}"' in text
    up = _app()["upgrade"]
    assert up["version_pin"] == {"source": "versions.env", "key": "SONARR2_VERSION"}
    assert up["kind"] == "tarball_swap"
    # manifest URL template and installer URL are the same release asset
    url = re.search(r'^URL="([^"]+)"', text, re.M).group(1).replace("${VERSION}", "{version}")
    assert up["url_template"] == url


def test_manifest_loads_with_the_flip():
    app = load_manifest(MANIFEST).app("sonarr2")
    assert app.class_ == "systemd" and app.upgrade.kind == "tarball_swap"


def test_generated_skip_list_carries_sonarr2_and_the_primary_sonarr():
    r = subprocess.run([sys.executable, str(LIB / "ucc_skip.py"), "--list",
                        "--manifest", str(MANIFEST)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    names = r.stdout.split()
    assert "sonarr2" in names
    assert "sonarr" in names            # converted on its own ticket (QFLX-32)


def test_post_swap_post_steps_hoist_the_tarball_dir_and_flip_current():
    steps = _app()["upgrade"]["post_steps"]
    assert len(steps) == 2
    assert "mv Sonarr .pkg" in steps[0] and "rm -rf Sonarr.Update" in steps[0]
    assert "bin/current" in steps[1] and "mv -Tf" in steps[1]


# --- appctl / lifecycle routing -----------------------------------------------------

def _manifest_copy(tmp_path: Path, *, swapped: bool) -> Path:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    if swapped:
        data["apps"]["sonarr2"].pop("swap_state", None)
    p = tmp_path / "apps.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


_STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\nexit 0\n'


def _run_appctl(tmp_path: Path, verb: str, *, swapped: bool) -> str:
    man = _manifest_copy(tmp_path, swapped=swapped)
    binp = tmp_path / "bin"
    binp.mkdir()
    for n in ("app-sonarr2", "systemctl"):
        (binp / n).write_text(_STUB, newline="\n")
        (binp / n).chmod(0o755)
    log = tmp_path / "argv.log"
    env = dict(os.environ, HOME=tmp_path.as_posix(), STUB_LOG=log.as_posix(),
               PATH=binp.as_posix() + os.pathsep + os.environ.get("PATH", ""),
               APPCTL_MANIFEST=man.as_posix(), APPCTL_PYTHON=Path(sys.executable).as_posix(),
               APPCTL_LIB=LIB.as_posix())
    r = subprocess.run(["bash", APPCTL.as_posix(), verb, "sonarr2"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return log.read_text() if log.exists() else ""


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("verb", ["start", "restart", "status", "stop"])
def test_appctl_never_starts_the_ucc_container_after_the_swap(tmp_path, verb):
    argv = _run_appctl(tmp_path, verb, swapped=True)
    assert "app-sonarr2" not in argv
    want = "is-active" if verb == "status" else verb
    assert f"systemctl --user {want} qflix-sonarr2.service" in argv


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("verb", ["start", "restart"])
def test_appctl_keeps_the_container_live_while_pending_swap(tmp_path, verb):
    argv = _run_appctl(tmp_path, verb, swapped=False)
    assert f"app-sonarr2 {verb}" in argv
    assert "qflix-sonarr2.service" not in argv


@pytest.mark.parametrize("fn", [lifecycle.start, lifecycle.restart, lifecycle.status])
def test_lifecycle_never_starts_the_ucc_container_after_the_swap(tmp_path, fn):
    app = load_manifest(_manifest_copy(tmp_path, swapped=True)).app("sonarr2")
    with patch("subprocess.run", return_value=CompletedProcess([], 0, "active\n", "")) as run:
        fn(app)
    for call in run.call_args_list:
        assert not call[0][0][0].startswith("app-"), call
