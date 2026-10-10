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


def test_bazarr_is_converted():
    """QFLX-27 (A3)."""
    assert "bazarr" in _converted()
    assert _converted()["bazarr"]["upgrade"]["kind"] == "zip_swap"


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


def test_sabnzbd_is_converted_pending_swap():
    a = _converted()["sabnzbd"]
    assert a["swap_state"] == "pending-swap" and a["unit"] == "qflix-sabnzbd.service"
    # The probe stays on loopback (the forwarder) under the urlbase.
    assert a["health"]["path_override"] == "/sabnzbd/" and "hostname_ref" not in a["health"]
    steps = a["upgrade"]["post_steps"]
    assert a["upgrade"]["version_pin"]["key"] == "SABNZBD_VERSION"
    # The upgrade must carry the wrapper, the forwarder and the helpers forward,
    # build the venv, and never delete an existing bin/<ver>.
    assert any("current/qflix-tcpfwd.py current/par2 current/unrar current/7zz" in s for s in steps)
    assert any("-m pip install" in s and "requirements.txt" in s for s in steps)
    assert not any("rm -rf" in s for s in steps)


def test_tautulli_is_converted_and_swapped():
    a = _converted()["tautulli"]
    assert a["unit"] == "qflix-tautulli.service" and "swap_state" not in a   # swapped 2026-10-10
    assert a["health"]["require_unit_active"] is True
    # Same port secret and probe kind: the native app answers on the recorded port.
    assert a["health"]["kind"] == "http_root" and a["health"]["port_secret"] == "tautulli.port"
    steps = a["upgrade"]["post_steps"]
    # The tag archive wraps everything in Tautulli-<ver>/; the upgrade flattens it,
    # builds the venv from the release requirements, then flips `current`.
    assert any("cp -a Tautulli-{version}/. ." in s for s in steps)
    assert any("-m venv venv" in s and "-r requirements.txt" in s for s in steps)
    assert steps[-1].startswith("ln -sfn {version}")
    assert a["upgrade"]["kind"] == "tarball_swap"


def test_real_manifest_loads_with_the_flip():
    app = load_manifest(MANIFEST).app("unpackerr")
    assert app.class_ == "systemd" and app.upgrade.kind == "tarball_swap"
    bz = load_manifest(MANIFEST).app("bazarr")
    assert bz.class_ == "systemd" and bz.upgrade.kind == "zip_swap"


def test_bazarr2_is_untouched_by_the_bazarr_flip():
    """bazarr2 stays the bare-python systemd app it was; it is not a UCC slug."""
    a = _apps()["bazarr2"]
    assert a["class"] == "systemd" and a["unit"] == "bazarr2.service" and not a.get("ucc_slug")


def test_generated_skip_list_carries_unpackerr():
    r = subprocess.run([sys.executable, str(LIB / "ucc_skip.py"), "--list",
                        "--manifest", str(MANIFEST)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "unpackerr" in r.stdout.split()
    assert "flaresolverr" in r.stdout.split()
    assert "bazarr" in r.stdout.split()
    assert "sabnzbd" in r.stdout.split()
    assert "tautulli" in r.stdout.split()
    assert "qbittorrent" in r.stdout.split()


# --- zero UCC starts after the swap (O-3 / F8 dependency) --------------------------

def _post_swap_manifest(tmp_path: Path, slug: str = "unpackerr") -> Path:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    data["apps"][slug].pop("swap_state", None)
    p = tmp_path / "apps.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


_STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\nexit 0\n'


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("slug", ["unpackerr", "bazarr"])
@pytest.mark.parametrize("verb", ["start", "restart", "status", "stop"])
def test_appctl_never_starts_the_ucc_container_after_the_swap(tmp_path, verb, slug):
    man = _post_swap_manifest(tmp_path, slug)
    binp = tmp_path / "bin"
    binp.mkdir()
    for n in (f"app-{slug}", "systemctl"):
        (binp / n).write_text(_STUB, newline="\n")
        (binp / n).chmod(0o755)
    log = tmp_path / "argv.log"
    env = dict(os.environ, HOME=tmp_path.as_posix(), STUB_LOG=log.as_posix(),
               PATH=binp.as_posix() + os.pathsep + os.environ.get("PATH", ""),
               APPCTL_MANIFEST=man.as_posix(), APPCTL_PYTHON=Path(sys.executable).as_posix(),
               APPCTL_LIB=LIB.as_posix())
    r = subprocess.run(["bash", APPCTL.as_posix(), verb, slug], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    argv = log.read_text() if log.exists() else ""
    assert f"app-{slug}" not in argv
    want = "is-active" if verb == "status" else verb
    assert f"systemctl --user {want} qflix-{slug}.service" in argv


@pytest.mark.parametrize("slug", ["unpackerr", "bazarr"])
@pytest.mark.parametrize("fn", [lifecycle.start, lifecycle.restart, lifecycle.status])
def test_lifecycle_never_starts_the_ucc_container_after_the_swap(tmp_path, fn, slug):
    app = load_manifest(_post_swap_manifest(tmp_path, slug)).app(slug)
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


# --- prowlarr (QFLX-28, A4) ---------------------------------------------------------

def test_prowlarr_is_converted_with_a_full_build_pin():
    a = _converted()["prowlarr"]
    assert a["unit"] == "qflix-prowlarr.service" and "swap_state" not in a   # swapped 2026-10-10
    assert a["health"]["kind"] == "http_api" and a["health"]["require_unit_active"] is True
    assert a["upgrade"]["kind"] == "tarball_swap"
    assert _versions_env()["PROWLARR_VERSION"].count(".") == 3        # 2.6.5.5623, not the panel's 2.6.5
    assert "linux-core-x64" in a["upgrade"]["url_template"]


def test_prowlarr_upgrade_hoists_the_tarball_top_dir_into_bin_ver():
    steps = " && ".join(_converted()["prowlarr"]["upgrade"]["post_steps"])
    assert "mv Prowlarr .pkg" in steps and "mv .pkg/* ." in steps    # dir and apphost share a name
    assert "rm -rf Prowlarr.Update" in steps
    assert "bin/current" in steps


def test_prowlarr_flip_keeps_the_probe_secrets_the_whole_stack_reads():
    h = _converted()["prowlarr"]["health"]
    assert (h["port_secret"], h["auth_secret"], h["urlbase_secret"]) == (
        "prowlarr.port", "prowlarr.key", "prowlarr.urlbase")


def test_generated_skip_list_carries_prowlarr():
    r = subprocess.run([sys.executable, str(LIB / "ucc_skip.py"), "--list",
                        "--manifest", str(MANIFEST)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "prowlarr" in r.stdout.split()


def _post_swap_manifest_for(tmp_path: Path, name: str) -> Path:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    data["apps"][name].pop("swap_state", None)
    p = tmp_path / "apps.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("verb", ["start", "restart", "status", "stop"])
def test_appctl_never_starts_the_prowlarr_container_after_the_swap(tmp_path, verb):
    man = _post_swap_manifest_for(tmp_path, "prowlarr")
    binp = tmp_path / "bin"
    binp.mkdir()
    for n in ("app-prowlarr", "systemctl"):
        (binp / n).write_text(_STUB, newline="\n")
        (binp / n).chmod(0o755)
    log = tmp_path / "argv.log"
    env = dict(os.environ, HOME=tmp_path.as_posix(), STUB_LOG=log.as_posix(),
               PATH=binp.as_posix() + os.pathsep + os.environ.get("PATH", ""),
               APPCTL_MANIFEST=man.as_posix(), APPCTL_PYTHON=Path(sys.executable).as_posix(),
               APPCTL_LIB=LIB.as_posix())
    r = subprocess.run(["bash", APPCTL.as_posix(), verb, "prowlarr"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    argv = log.read_text() if log.exists() else ""
    assert "app-prowlarr" not in argv
    want = "is-active" if verb == "status" else verb
    assert f"systemctl --user {want} qflix-prowlarr.service" in argv


@pytest.mark.parametrize("fn", [lifecycle.start, lifecycle.restart, lifecycle.status])
def test_lifecycle_never_starts_the_prowlarr_container_after_the_swap(tmp_path, fn):
    app = load_manifest(_post_swap_manifest_for(tmp_path, "prowlarr")).app("prowlarr")
    with patch("subprocess.run", return_value=CompletedProcess([], 0, "active\n", "")) as run:
        fn(app)
    for call in run.call_args_list:
        assert not call[0][0][0].startswith("app-"), call


@pytest.mark.parametrize("fn", [lifecycle.start, lifecycle.restart])
def test_swapped_prowlarr_is_lifecycled_as_the_native_unit(tmp_path, fn):
    """Swapped 2026-10-10 (pending-swap dropped): the native unit is the live
    runtime, so pusher recovery acts on it and never wakes the dormant
    container through the panel tool (I-6)."""
    app = load_manifest(MANIFEST).app("prowlarr")
    with patch("subprocess.run", return_value=CompletedProcess([], 0, "", "")) as run:
        fn(app)
    argv = [c[0][0] for c in run.call_args_list]
    assert argv and not any(a[0] == "app-prowlarr" for a in argv), argv
    assert any(a[0] == "systemctl" and "qflix-prowlarr.service" in a for a in argv), argv


# --- radarr2 (QFLX-29, A5) ----------------------------------------------------------

def _radarr2_post_swap_manifest(tmp_path: Path, name: str = "radarr2") -> Path:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    data["apps"][name].pop("swap_state", None)
    p = tmp_path / "apps.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_radarr2_is_converted_with_a_full_build_pin():
    a = _converted()["radarr2"]
    assert a["unit"] == "qflix-radarr2.service" and "swap_state" not in a    # swapped 2026-10-10
    assert a["health"]["kind"] == "http_api" and a["health"]["require_unit_active"] is True
    assert a["upgrade"]["kind"] == "tarball_swap"
    assert _versions_env()["RADARR2_VERSION"].count(".") == 3        # 6.4.4.10685, not the panel's 6.4.4
    assert "linux-core-x64" in a["upgrade"]["url_template"]


def test_radarr2_upgrade_hoists_the_tarball_top_dir_into_bin_ver():
    steps = " && ".join(_converted()["radarr2"]["upgrade"]["post_steps"])
    assert "mv Radarr .pkg" in steps and "mv .pkg/* ." in steps      # dir and apphost share a name
    assert "rm -rf Radarr.Update" in steps
    assert "bin/current" in steps
    assert "~/.apps/radarr2/bin/" in steps and "~/.apps/radarr/bin" not in steps


def test_radarr2_flip_keeps_the_probe_secrets_the_whole_stack_reads():
    h = _converted()["radarr2"]["health"]
    assert (h["port_secret"], h["auth_secret"], h["urlbase_secret"]) == (
        "radarr2.port", "radarr2.key", "radarr2.urlbase")


def test_generated_skip_list_carries_radarr2():
    r = subprocess.run([sys.executable, str(LIB / "ucc_skip.py"), "--list",
                        "--manifest", str(MANIFEST)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "radarr2" in r.stdout.split()           # radarr (A7) is pinned below


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("verb", ["start", "restart", "status", "stop"])
def test_appctl_never_starts_the_radarr2_container_after_the_swap(tmp_path, verb):
    man = _radarr2_post_swap_manifest(tmp_path, "radarr2")
    binp = tmp_path / "bin"
    binp.mkdir()
    for n in ("app-radarr2", "systemctl"):
        (binp / n).write_text(_STUB, newline="\n")
        (binp / n).chmod(0o755)
    log = tmp_path / "argv.log"
    env = dict(os.environ, HOME=tmp_path.as_posix(), STUB_LOG=log.as_posix(),
               PATH=binp.as_posix() + os.pathsep + os.environ.get("PATH", ""),
               APPCTL_MANIFEST=man.as_posix(), APPCTL_PYTHON=Path(sys.executable).as_posix(),
               APPCTL_LIB=LIB.as_posix())
    r = subprocess.run(["bash", APPCTL.as_posix(), verb, "radarr2"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    argv = log.read_text() if log.exists() else ""
    assert "app-radarr2" not in argv
    want = "is-active" if verb == "status" else verb
    assert f"systemctl --user {want} qflix-radarr2.service" in argv


@pytest.mark.parametrize("fn", [lifecycle.start, lifecycle.restart, lifecycle.status])
def test_lifecycle_never_starts_the_radarr2_container_after_the_swap(tmp_path, fn):
    app = load_manifest(_radarr2_post_swap_manifest(tmp_path, "radarr2")).app("radarr2")
    with patch("subprocess.run", return_value=CompletedProcess([], 0, "active\n", "")) as run:
        fn(app)
    for call in run.call_args_list:
        assert not call[0][0][0].startswith("app-"), call


@pytest.mark.parametrize("fn", [lifecycle.start, lifecycle.restart])
def test_swapped_radarr2_is_lifecycled_as_the_native_unit(tmp_path, fn):
    """Swapped 2026-10-10 (pending-swap dropped): the native unit is the live
    runtime, so pusher recovery acts on it and never wakes the dormant
    container through the panel tool (I-6)."""
    app = load_manifest(MANIFEST).app("radarr2")
    with patch("subprocess.run", return_value=CompletedProcess([], 0, "", "")) as run:
        fn(app)
    argv = [c[0][0] for c in run.call_args_list]
    assert argv and not any(a[0] == "app-radarr2" for a in argv), argv
    assert any(a[0] == "systemctl" and "qflix-radarr2.service" in a for a in argv), argv


# --- radarr (QFLX-31, A7) ---------------------------------------------------------

def test_radarr_is_converted_with_a_full_build_pin():
    a = _converted()["radarr"]
    assert a["unit"] == "qflix-radarr.service" and "swap_state" not in a     # swapped 2026-10-10
    assert a["health"]["kind"] == "http_api" and a["health"]["require_unit_active"] is True
    assert a["upgrade"]["kind"] == "tarball_swap"
    assert _versions_env()["RADARR_VERSION"].count(".") == 3        # 6.4.4.10685, not the panel's 6.4.4
    assert "linux-core-x64" in a["upgrade"]["url_template"]


def test_radarr_upgrade_hoists_the_tarball_top_dir_into_bin_ver():
    steps = " && ".join(_converted()["radarr"]["upgrade"]["post_steps"])
    assert "mv Radarr .pkg" in steps and "mv .pkg/* ." in steps    # dir and apphost share a name
    assert "rm -rf Radarr.Update" in steps
    assert "bin/current" in steps


def test_radarr_flip_keeps_the_probe_secrets_the_whole_stack_reads():
    h = _converted()["radarr"]["health"]
    assert (h["port_secret"], h["auth_secret"], h["urlbase_secret"]) == (
        "radarr.port", "radarr.key", "radarr.urlbase")


def test_generated_skip_list_carries_radarr():
    r = subprocess.run([sys.executable, str(LIB / "ucc_skip.py"), "--list",
                        "--manifest", str(MANIFEST)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "radarr" in r.stdout.split()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("verb", ["start", "restart", "status", "stop"])
def test_appctl_never_starts_the_radarr_container_after_the_swap(tmp_path, verb):
    man = _post_swap_manifest_for(tmp_path, "radarr")
    binp = tmp_path / "bin"
    binp.mkdir()
    for n in ("app-radarr", "systemctl"):
        (binp / n).write_text(_STUB, newline="\n")
        (binp / n).chmod(0o755)
    log = tmp_path / "argv.log"
    env = dict(os.environ, HOME=tmp_path.as_posix(), STUB_LOG=log.as_posix(),
               PATH=binp.as_posix() + os.pathsep + os.environ.get("PATH", ""),
               APPCTL_MANIFEST=man.as_posix(), APPCTL_PYTHON=Path(sys.executable).as_posix(),
               APPCTL_LIB=LIB.as_posix())
    r = subprocess.run(["bash", APPCTL.as_posix(), verb, "radarr"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    argv = log.read_text() if log.exists() else ""
    assert "app-radarr" not in argv
    want = "is-active" if verb == "status" else verb
    assert f"systemctl --user {want} qflix-radarr.service" in argv


@pytest.mark.parametrize("fn", [lifecycle.start, lifecycle.restart, lifecycle.status])
def test_lifecycle_never_starts_the_radarr_container_after_the_swap(tmp_path, fn):
    app = load_manifest(_post_swap_manifest_for(tmp_path, "radarr")).app("radarr")
    with patch("subprocess.run", return_value=CompletedProcess([], 0, "active\n", "")) as run:
        fn(app)
    for call in run.call_args_list:
        assert not call[0][0][0].startswith("app-"), call


@pytest.mark.parametrize("fn", [lifecycle.start, lifecycle.restart])
def test_swapped_radarr_is_lifecycled_as_the_native_unit(tmp_path, fn):
    """Swapped 2026-10-10 (pending-swap dropped): the native unit is the live
    runtime, so pusher recovery acts on it and never wakes the dormant
    container through the panel tool (I-6)."""
    app = load_manifest(MANIFEST).app("radarr")
    with patch("subprocess.run", return_value=CompletedProcess([], 0, "", "")) as run:
        fn(app)
    argv = [c[0][0] for c in run.call_args_list]
    assert argv and not any(a[0] == "app-radarr" for a in argv), argv
    assert any(a[0] == "systemctl" and "qflix-radarr.service" in a for a in argv), argv


# --- sonarr (QFLX-32, A8) --------------------------------------------------------------


def test_sonarr_is_converted_with_a_full_build_pin():
    a = _converted()["sonarr"]
    assert a["unit"] == "qflix-sonarr.service" and "swap_state" not in a     # swapped 2026-10-10
    assert a["health"]["kind"] == "http_api" and a["health"]["require_unit_active"] is True
    assert a["upgrade"]["kind"] == "tarball_swap"
    assert _versions_env()["SONARR_VERSION"].count(".") == 3          # 4.0.20.3014, not the panel's 4.0.20
    assert "linux-x64" in a["upgrade"]["url_template"] and "Sonarr.main." in a["upgrade"]["url_template"]


def test_sonarr_upgrade_hoists_the_tarball_top_dir_into_bin_ver():
    steps = " && ".join(_converted()["sonarr"]["upgrade"]["post_steps"])
    assert "mv Sonarr .pkg" in steps and "mv .pkg/* ." in steps       # dir and apphost share a name
    assert "rm -rf Sonarr.Update" in steps
    assert "bin/current" in steps


def test_sonarr_flip_keeps_the_probe_secrets_the_whole_stack_reads():
    h = _converted()["sonarr"]["health"]
    assert (h["port_secret"], h["auth_secret"], h["urlbase_secret"]) == (
        "sonarr.port", "sonarr.key", "sonarr.urlbase")


def test_generated_skip_list_carries_sonarr():
    r = subprocess.run([sys.executable, str(LIB / "ucc_skip.py"), "--list",
                        "--manifest", str(MANIFEST)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "sonarr" in r.stdout.split()


def test_gate_probe_can_never_name_sonarr_once_converted():
    """QFLX-32 test line: the UCC gate probe never names a converted slug. The
    guard reads the deployed manifest, so after the flip `sonarr` is refused
    even when the secret still says it."""
    from lib import ucc_skip
    assert ucc_skip.probe_allowed("sonarr", MANIFEST) is False
    assert ucc_skip.probe_allowed("plex", MANIFEST) is True


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("verb", ["start", "restart", "status", "stop"])
def test_appctl_never_starts_the_sonarr_container_after_the_swap(tmp_path, verb):
    man = _post_swap_manifest_for(tmp_path, "sonarr")
    binp = tmp_path / "bin"
    binp.mkdir()
    for n in ("app-sonarr", "systemctl"):
        (binp / n).write_text(_STUB, newline="\n")
        (binp / n).chmod(0o755)
    log = tmp_path / "argv.log"
    env = dict(os.environ, HOME=tmp_path.as_posix(), STUB_LOG=log.as_posix(),
               PATH=binp.as_posix() + os.pathsep + os.environ.get("PATH", ""),
               APPCTL_MANIFEST=man.as_posix(), APPCTL_PYTHON=Path(sys.executable).as_posix(),
               APPCTL_LIB=LIB.as_posix())
    r = subprocess.run(["bash", APPCTL.as_posix(), verb, "sonarr"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    argv = log.read_text() if log.exists() else ""
    assert "app-sonarr" not in argv
    want = "is-active" if verb == "status" else verb
    assert f"systemctl --user {want} qflix-sonarr.service" in argv


@pytest.mark.parametrize("fn", [lifecycle.start, lifecycle.restart, lifecycle.status])
def test_lifecycle_never_starts_the_sonarr_container_after_the_swap(tmp_path, fn):
    app = load_manifest(_post_swap_manifest_for(tmp_path, "sonarr")).app("sonarr")
    with patch("subprocess.run", return_value=CompletedProcess([], 0, "active\n", "")) as run:
        fn(app)
    for call in run.call_args_list:
        assert not call[0][0][0].startswith("app-"), call


@pytest.mark.parametrize("fn", [lifecycle.start, lifecycle.restart])
def test_swapped_sonarr_is_lifecycled_as_the_native_unit(tmp_path, fn):
    """Swapped 2026-10-10 (pending-swap dropped): the native unit is the live
    runtime, so pusher recovery acts on it and never wakes the dormant
    container through the panel tool (I-6)."""
    app = load_manifest(MANIFEST).app("sonarr")
    with patch("subprocess.run", return_value=CompletedProcess([], 0, "", "")) as run:
        fn(app)
    argv = [c[0][0] for c in run.call_args_list]
    assert argv and not any(a[0] == "app-sonarr" for a in argv), argv
    assert any(a[0] == "systemctl" and "qflix-sonarr.service" in a for a in argv), argv
