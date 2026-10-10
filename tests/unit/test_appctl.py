"""scripts/lib/appctl -- the one lifecycle shim (QFLX-18, spec 5.2).

Subprocess tests with a STUB PATH: every command appctl may reach
(app-<slug>, systemctl, ss, app-ports, app-nginx, manitoba-maint) is a tiny
shell stub that appends its argv to a log file. The tests then assert on
exactly which argv were produced, which is the only thing that matters for a
dispatcher: "did it call the right tool with the right words, and nothing
else".

What is pinned:
  * the class x verb matrix (ucc -> app-<slug>, systemd -> systemctl --user,
    anything else -> exit 2 "no lifecycle");
  * the dormant refusal (I-9): a slug whose UCC runtime is dormant refuses
    every verb except `stop` with exit 3, and emits NO argv at all;
  * `status` on a ucc app is `app-<slug> version` + a port probe and NEVER
    `app-<slug> status` (that subcommand does not exist; F-3);
  * fail closed: a missing deployed manifest is exit 2, not a silent success.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
APPCTL = REPO / "scripts" / "lib" / "appctl"
LIB = REPO / "scripts" / "maint" / "lib"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

MANIFEST = """\
apps:
  sonarr:
    class: ucc
    ucc_slug: sonarr
    health:
      kind: http_api
      port_secret: sonarr.port
  unpackerr:
    class: ucc
    ucc_slug: unpackerr
    ucc_dormant: true
    health:
      kind: process_pattern
  bazarr2:
    class: systemd
    unit: bazarr2.service
    health:
      kind: http_api
      port_secret: bazarr2.port
  flipped:
    class: systemd
    ucc_slug: flipped
    unit: qflix-flipped.service
    ucc_dormant: true
  kometa:
    class: cron
    unit: kometa.service
  ghost:
    class: ucc
    ucc_slug: ghost
  postgres:
    class: ucc
    ucc_slug: postgres
    health:
      kind: process_pattern
"""

# Every stub records "<name> <args...>" to $STUB_LOG, then behaves per name.
_STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
case "$(basename "$0")" in
  app-postgres)
    # Verbatim box behaviour 2026-10-10: app-postgres has no `version`.
    if [ "$1" = version ]; then echo "Unknown command: version"; exit 1; fi ;;
  app-*)
    # Verbatim shape of `app-sonarr version` on the box (2026-10-10).
    if [ "$1" = version ]; then echo '{"data": {"version": "4.0.20"}, "result": true}'; fi ;;
  ss)
    # Pretend every port in $SS_LISTEN is bound.
    for p in $SS_LISTEN; do
      echo "LISTEN 0 4096 127.0.0.1:$p 0.0.0.0:*"
    done ;;
  app-ports)
    printf '42001\\n42002\\n42003\\n' ;;
  systemctl)
    if [ "$2" = is-active ]; then echo active; fi ;;
esac
exit 0
"""

_STUBS = ("app-sonarr", "app-unpackerr", "app-postgres", "app-flipped", "app-nginx", "app-ports",
          "systemctl", "ss", "manitoba-maint")


def _env(tmp_path: Path, *, manifest: str | None = MANIFEST, ss_listen: str = "",
         profile: str | None = None, stubs=_STUBS) -> dict:
    home = tmp_path / "home"
    (home / ".opt" / "maint").mkdir(parents=True, exist_ok=True)
    (home / "bin").mkdir(parents=True, exist_ok=True)
    secrets = home / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "sonarr.port").write_text("42050\n")
    (secrets / "bazarr2.port").write_text("42060\n")
    (secrets / "used.port").write_text("42001\n")
    if profile is not None:
        (secrets / "host.profile").write_text(profile + "\n")
    if manifest is not None:
        (home / ".opt" / "maint" / "apps.yaml").write_text(manifest)
    binp = tmp_path / "stubbin"
    binp.mkdir(exist_ok=True)
    for name in stubs:
        p = binp / name
        p.write_text(_STUB, newline="\n")
        p.chmod(0o755)
    # manitoba-maint lives in ~/bin on the box; appctl calls it absolutely.
    mm = home / "bin" / "manitoba-maint"
    mm.write_text(_STUB, newline="\n")
    mm.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "HOME": home.as_posix(),
        "PATH": binp.as_posix() + os.pathsep + env.get("PATH", ""),
        "STUB_LOG": (tmp_path / "argv.log").as_posix(),
        "SS_LISTEN": ss_listen,
        "APPCTL_PYTHON": Path(sys.executable).as_posix(),
        "APPCTL_LIB": LIB.as_posix(),
        "APPCTL_SECRETS_DIR": secrets.as_posix(),
        "MANITOBA_SECRETS_DIR": secrets.as_posix(),
    })
    env.pop("APPCTL_MANIFEST", None)
    return env


def _run(tmp_path, *args, **kw):
    env = _env(tmp_path, **kw)
    r = subprocess.run(["bash", APPCTL.as_posix(), *args], env=env,
                       capture_output=True, text=True, timeout=60)
    log = tmp_path / "argv.log"
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


# --- class x verb matrix ------------------------------------------------------

@pytest.mark.parametrize("verb", ["start", "stop", "restart", "upgrade", "version"])
def test_ucc_verbs_dispatch_to_app_slug(tmp_path, verb):
    r, calls = _run(tmp_path, verb, "sonarr")
    assert r.returncode == 0, r.stderr
    assert calls == ["app-sonarr " + verb]


def test_ucc_extra_args_pass_through(tmp_path):
    r, calls = _run(tmp_path, "upgrade", "sonarr", "-p", "pw")
    assert r.returncode == 0, r.stderr
    assert calls == ["app-sonarr upgrade -p pw"]


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_systemd_verbs_dispatch_to_systemctl_user(tmp_path, verb):
    r, calls = _run(tmp_path, verb, "bazarr2")
    assert r.returncode == 0, r.stderr
    assert calls == ["systemctl --user %s bazarr2.service" % verb]


def test_systemd_status_is_is_active(tmp_path):
    r, calls = _run(tmp_path, "status", "bazarr2")
    assert r.returncode == 0, r.stderr
    assert calls == ["systemctl --user is-active bazarr2.service"]


def test_systemd_version_reads_bin_current(tmp_path):
    env = _env(tmp_path)
    bindir = Path(env["HOME"]) / ".apps" / "bazarr2" / "bin"
    (bindir / "1.5.3").mkdir(parents=True)
    (bindir / "current").write_text("1.5.3\n")  # plain-file form (no symlink on Windows)
    r = subprocess.run(["bash", APPCTL.as_posix(), "version", "bazarr2"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "1.5.3"
    assert not (tmp_path / "argv.log").exists()


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
def test_systemd_version_reads_bin_current_symlink(tmp_path):
    env = _env(tmp_path)
    bindir = Path(env["HOME"]) / ".apps" / "bazarr2" / "bin"
    (bindir / "1.5.4").mkdir(parents=True)
    os.symlink("1.5.4", bindir / "current")
    r = subprocess.run(["bash", APPCTL.as_posix(), "version", "bazarr2"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "1.5.4"


def test_systemd_version_without_bin_current_fails(tmp_path):
    r, calls = _run(tmp_path, "version", "bazarr2")
    assert r.returncode == 1
    assert calls == []


def test_systemd_upgrade_goes_through_manitoba_maint(tmp_path):
    r, calls = _run(tmp_path, "upgrade", "bazarr2")
    assert r.returncode == 0, r.stderr
    assert calls == ["manitoba-maint upgrade bazarr2"]


# --- status never emits `app-x status` (F-3) ----------------------------------

def test_ucc_status_is_version_plus_port_probe_up(tmp_path):
    r, calls = _run(tmp_path, "status", "sonarr", ss_listen="42050")
    assert r.returncode == 0, r.stderr
    assert "app-sonarr version" in calls
    assert not any(c.startswith("app-sonarr status") for c in calls)
    assert any(c.startswith("ss ") for c in calls)
    assert "version=4.0.20 " in r.stdout and "42050" in r.stdout


def test_ucc_version_passes_panel_output_through(tmp_path):
    r, calls = _run(tmp_path, "version", "sonarr")
    assert r.returncode == 0
    assert r.stdout.strip() == '{"data": {"version": "4.0.20"}, "result": true}'


def test_ucc_status_without_version_subcommand_is_unknown_not_up(tmp_path):
    # app-postgres has no `version` and postgres has no port secret: appctl
    # must say "unknown" (nonzero), never claim it is up.
    r, calls = _run(tmp_path, "status", "postgres")
    assert r.returncode == 1
    assert "unknown" in r.stdout
    assert calls == ["app-postgres version"]


def test_ucc_status_without_version_subcommand_uses_port(tmp_path):
    env_manifest = MANIFEST + """  pgport:
    class: ucc
    ucc_slug: postgres
    health:
      port_secret: sonarr.port
"""
    r, calls = _run(tmp_path, "status", "pgport", manifest=env_manifest, ss_listen="42050")
    assert r.returncode == 0, r.stdout
    assert "version=n/a" in r.stdout and "listening" in r.stdout


def test_ucc_status_port_not_listening_is_down(tmp_path):
    r, calls = _run(tmp_path, "status", "sonarr", ss_listen="1")
    assert r.returncode == 1
    assert not any(" status" in c and c.startswith("app-") for c in calls)


# --- dormant refusal (I-9) ----------------------------------------------------

@pytest.mark.parametrize("verb", ["start", "restart", "status", "version", "upgrade"])
def test_dormant_ucc_refuses_every_verb_but_stop(tmp_path, verb):
    r, calls = _run(tmp_path, verb, "unpackerr")
    assert r.returncode == 3
    assert "dormant" in r.stderr
    assert calls == []          # nothing at all reached UCC


def test_dormant_ucc_stop_is_allowed(tmp_path):
    r, calls = _run(tmp_path, "stop", "unpackerr")
    assert r.returncode == 0, r.stderr
    assert calls == ["app-unpackerr stop"]


def test_flipped_app_starts_native_never_ucc(tmp_path):
    r, calls = _run(tmp_path, "start", "flipped")
    assert r.returncode == 0, r.stderr
    assert calls == ["systemctl --user start qflix-flipped.service"]


@pytest.mark.parametrize("slug,rc", [("flipped", 0), ("bazarr2", 0), ("unpackerr", 0),
                                     ("sonarr", 1), ("kometa", 2), ("nosuch", 2)])
def test_is_native(tmp_path, slug, rc):
    r, calls = _run(tmp_path, "is-native", slug)
    assert r.returncode == rc, (r.stdout, r.stderr)
    assert calls == []


# --- no lifecycle -> exit 2 ----------------------------------------------------

@pytest.mark.parametrize("slug", ["kometa", "nosuch", "ghost"])
def test_no_lifecycle_exits_2(tmp_path, slug):
    r, calls = _run(tmp_path, "start", slug)
    assert r.returncode == 2
    assert "no lifecycle" in r.stderr
    assert calls == []


def test_missing_manifest_fails_closed(tmp_path):
    r, calls = _run(tmp_path, "start", "sonarr", manifest=None)
    assert r.returncode == 2
    assert calls == []


def test_usage_errors(tmp_path):
    r, _ = _run(tmp_path)
    assert r.returncode == 64
    r, _ = _run(tmp_path, "start")
    assert r.returncode == 64
    r, _ = _run(tmp_path, "frobnicate", "sonarr")
    assert r.returncode == 64


# --- host verbs ------------------------------------------------------------------

# The ultra profile's detect() cross-check is shutil.which("app-ports"), which
# on Windows ignores an extension-less stub (PATHEXT). CI (Linux) runs these.
posix_only = pytest.mark.skipif(os.name != "posix", reason="ultra detect() needs POSIX which()")


@posix_only
def test_proxy_reload_ultra_keeps_app_nginx_restart(tmp_path):
    r, calls = _run(tmp_path, "proxy-reload", profile="ultra")
    assert r.returncode == 0, r.stderr
    assert calls == ["app-nginx restart"]


def test_proxy_reload_without_profile_fails_closed(tmp_path):
    r, calls = _run(tmp_path, "proxy-reload")
    assert r.returncode == 2
    assert not any(c.startswith("app-nginx") for c in calls)


def test_proxy_reload_generic_has_no_proxy_yet(tmp_path):
    r, calls = _run(tmp_path, "proxy-reload", profile="generic",
                    stubs=tuple(s for s in _STUBS if s != "app-ports"))
    assert r.returncode == 2
    assert calls == []


@posix_only
def test_ports_free_is_policy_candidates_minus_claimed_minus_bound(tmp_path):
    # candidates 42001..42003 (app-ports stub); 42001 is claimed by
    # secrets/used.port; 42002 is bound per ss.
    r, calls = _run(tmp_path, "ports-free", profile="ultra", ss_listen="42002")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["42003"]


def test_ports_free_without_profile_fails_closed(tmp_path):
    r, _ = _run(tmp_path, "ports-free")
    assert r.returncode == 2
    assert r.stdout.strip() == ""
