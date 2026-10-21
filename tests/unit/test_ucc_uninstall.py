"""scripts/ops/ucc-uninstall.sh -- uninstall a dormant UCC app, keep its data (QFLX-60).

Drives the real script with STUB binaries on PATH (the test_appctl.py
pattern): app-<slug>, systemctl and a ~/bin/appctl wrapper append their argv to
a log, so each test asserts exactly which commands ran. `appctl lookup` is
forwarded to the REAL scripts/lib/appctl so resolution is the production code.

What is pinned:
  * dry-run touches nothing (no uninstall argv, no tarball, no log file);
  * refusals (exit 2, no uninstall): class ucc, not dormant, pending-swap,
    native unit inactive, seerr;
  * the execute path calls `app-<slug> uninstall --keep-config` and nothing
    else on app-<slug> -- plain uninstall deletes the live data dir;
  * post-checks: a vanished data file is non-zero; a vanished proxy conf is
    restored and `appctl proxy-reload` is called; a lingering container
    process is non-zero;
  * backup verification holds for listings past the 64 KiB pipe buffer;
  * key files outside ~/.apps/<slug>: ~/ ones are copied + cmp-verified, and
    the GONE message names the copy that really holds the file (or says none).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ops" / "ucc-uninstall.sh"
APPCTL = REPO / "scripts" / "lib" / "appctl"
LIB = REPO / "scripts" / "maint" / "lib"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

MANIFEST = """\
apps:
  unpackerr:
    class: systemd
    unit: qflix-unpackerr.service
    ucc_slug: unpackerr
    ucc_dormant: true
    health:
      kind: process_pattern
      pattern: "/unpackerr"
  sonarr:
    class: systemd
    unit: qflix-sonarr.service
    ucc_slug: sonarr
    ucc_dormant: true
  uccapp:
    class: ucc
    ucc_slug: unpackerr
  awake:
    class: systemd
    unit: qflix-unpackerr.service
    ucc_slug: unpackerr
  pending:
    class: systemd
    unit: qflix-unpackerr.service
    ucc_slug: unpackerr
    ucc_dormant: true
    swap_state: pending-swap
  seerr:
    class: systemd
    unit: qflix-seerr.service
    ucc_slug: seerr
    ucc_dormant: true
  qbittorrent:
    class: systemd
    unit: qflix-qbittorrent.service
    ucc_slug: qbittorrent
    ucc_dormant: true
  postgres:
    class: systemd
    unit: qflix-postgres.service
    ucc_slug: postgres
    ucc_dormant: true
    native_dir: pg-native
"""

# $UNIT_STATE drives is-active; $UNINSTALL_EFFECT lets the app-* stub simulate
# the panel removing things it should not.
_STUB = r"""#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
case "$(basename "$0")" in
  systemctl)
    case "$2" in
      is-active) echo "${UNIT_STATE:-active}"; [ "${UNIT_STATE:-active}" = active ] ;;
      show) echo 4242 ;;
    esac ;;
  app-*)
    if [ "$1" = uninstall ]; then
      case "$UNINSTALL_EFFECT" in
        rm-data) rm -f "$HOME/.apps/unpackerr/unpackerr.conf" ;;
        rm-proxy) rm -f "$HOME/.apps/nginx/proxy.d/unpackerr.conf" ;;
        rm-qbit) rm -f "$HOME/.config/qBittorrent/qBittorrent.conf" ;;
        rm-pg) rm -f "$HOME/.apps/pg-native/data/PG_VERSION" ;;
      esac
    fi ;;
esac
exit 0
"""

# ~/bin/appctl: log the argv; `lookup` is answered by the real appctl.
_APPCTL_WRAP = """#!/bin/sh
echo "appctl $*" >> "$STUB_LOG"
[ "$1" = lookup ] && exec bash "$REAL_APPCTL" "$@"
exit 0
"""


def _env(tmp_path: Path, manifest: str = MANIFEST, **extra) -> dict:
    home = tmp_path / "home"
    (home / ".opt" / "maint").mkdir(parents=True, exist_ok=True)
    (home / ".opt" / "maint" / "apps.yaml").write_text(manifest, newline="\n")
    (home / "bin").mkdir(exist_ok=True)
    app = home / ".apps" / "unpackerr"
    (app / "bin").mkdir(parents=True, exist_ok=True)
    (app / "unpackerr.conf").write_text("[[sonarr]]\n")
    (app / "bin" / "unpackerr").write_text("binary\n")
    (home / ".apps" / "sonarr").mkdir(parents=True, exist_ok=True)
    # qbittorrent: its key file is under ~/.config, OUTSIDE ~/.apps/<slug>.
    (home / ".apps" / "qbittorrent").mkdir(parents=True, exist_ok=True)
    (home / ".apps" / "qbittorrent" / "state.dat").write_text("x\n")
    qb = home / ".config" / "qBittorrent"
    qb.mkdir(parents=True, exist_ok=True)
    (qb / "qBittorrent.conf").write_text("[Preferences]\nWebUI\\Port=1\n")
    # postgres: native_dir pg-native, outside ~/.apps/postgres.
    (home / ".apps" / "postgres").mkdir(parents=True, exist_ok=True)
    (home / ".apps" / "postgres" / "postgresql.conf").write_text("x\n")
    (home / ".apps" / "pg-native" / "data").mkdir(parents=True, exist_ok=True)
    (home / ".apps" / "pg-native" / "data" / "PG_VERSION").write_text("16\n")
    proxy_d = home / ".apps" / "nginx" / "proxy.d"
    proxy_d.mkdir(parents=True, exist_ok=True)
    (proxy_d / "unpackerr.conf").write_text("location /unpackerr {}\n")
    (tmp_path / "proc").mkdir(exist_ok=True)  # empty /proc: no container pids
    # hostpolicy decides the window from host.profile; pin it, never inherit
    # the workstation's secrets dir.
    (home / "secrets").mkdir(exist_ok=True)
    (home / "secrets" / "host.profile").write_text("ultra\n")
    binp = tmp_path / "stubbin"
    binp.mkdir(exist_ok=True)
    for name in ("systemctl", "app-unpackerr", "app-seerr", "app-sonarr",
                 "app-qbittorrent", "app-postgres"):
        p = binp / name
        p.write_text(_STUB, newline="\n")
        p.chmod(0o755)
    w = home / "bin" / "appctl"
    w.write_text(_APPCTL_WRAP, newline="\n")
    w.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "HOME": home.as_posix(),
        "PATH": binp.as_posix() + os.pathsep + env.get("PATH", ""),
        "STUB_LOG": (tmp_path / "argv.log").as_posix(),
        "REAL_APPCTL": APPCTL.as_posix(),
        "APPCTL_PYTHON": Path(sys.executable).as_posix(),
        "APPCTL_LIB": LIB.as_posix(),
        "MANITOBA_SECRETS_DIR": (home / "secrets").as_posix(),
        "UCC_UNINSTALL_PYTHON": Path(sys.executable).as_posix(),
        "UCC_UNINSTALL_LIB": LIB.as_posix(),
        "UCC_UNINSTALL_PROC": (tmp_path / "proc").as_posix(),
        "UCC_UNINSTALL_UID": "1000",
        "UCC_UNINSTALL_SETTLE_S": "0",
        "QFLIX_NOW": "2026-10-21T12:00:00+00:00",  # a Wednesday: outside the window
    })
    env.pop("APPCTL_MANIFEST", None)
    env.update(extra)
    return env


def _run(tmp_path, *args, **extra):
    env = _env(tmp_path, **extra)
    r = subprocess.run(["bash", SCRIPT.as_posix(), *args], env=env,
                       capture_output=True, text=True, timeout=120)
    log = tmp_path / "argv.log"
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls, Path(env["HOME"])


def _uninstalls(calls):
    return [c for c in calls if c.startswith("app-") and " uninstall" in c]


# --- dry-run -------------------------------------------------------------------

def test_dry_run_touches_nothing(tmp_path):
    r, calls, home = _run(tmp_path, "unpackerr")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _uninstalls(calls) == []
    assert "would run: app-unpackerr uninstall --keep-config" in r.stdout
    assert not (home / ".opt" / "maint" / "ucc-uninstall").exists()
    assert not (home / ".opt" / "maint" / "ucc-uninstall.log").exists()
    assert (home / ".apps" / "unpackerr" / "unpackerr.conf").exists()
    assert not any(c.startswith("appctl proxy-reload") for c in calls)


# --- refusals --------------------------------------------------------------------

@pytest.mark.parametrize("app,needle", [
    ("uccapp", "not systemd"),
    ("awake", "ucc_dormant"),
    ("pending", "pending-swap"),
    ("seerr", "QFLX-56"),
])
@pytest.mark.parametrize("mode", [[], ["--execute"]])
def test_refusals_exit_2_and_never_uninstall(tmp_path, app, needle, mode):
    r, calls, _ = _run(tmp_path, app, *mode)
    assert r.returncode == 2, r.stdout + r.stderr
    assert needle in r.stderr
    assert _uninstalls(calls) == []


def test_refuses_when_native_unit_inactive(tmp_path):
    r, calls, _ = _run(tmp_path, "unpackerr", "--execute", UNIT_STATE="inactive")
    assert r.returncode == 2
    assert "not active" in r.stderr
    assert _uninstalls(calls) == []


def test_refuses_unknown_app(tmp_path):
    r, calls, _ = _run(tmp_path, "nosuch", "--execute")
    assert r.returncode == 2
    assert _uninstalls(calls) == []


def test_refuses_app_without_key_file_proof(tmp_path):
    """sonarr's dir has no config.xml/sonarr.db here: parity unproven."""
    r, calls, _ = _run(tmp_path, "sonarr", "--execute")
    assert r.returncode == 2
    assert "key file" in r.stderr
    assert _uninstalls(calls) == []


# --- execute ---------------------------------------------------------------------

def test_execute_uninstalls_with_keep_config_only(tmp_path):
    r, calls, home = _run(tmp_path, "unpackerr", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    # The ONLY app-<slug> call, with the exact argv.
    assert [c for c in calls if c.startswith("app-")] == ["app-unpackerr uninstall --keep-config"]
    tgz = list((home / ".opt" / "maint" / "ucc-uninstall").glob("unpackerr-*.tgz"))
    assert len(tgz) == 1
    with tarfile.open(tgz[0]) as t:
        names = t.getnames()
    assert "unpackerr/unpackerr.conf" in names
    assert not any(n.startswith("unpackerr/bin") for n in names)  # binaries excluded
    assert list((home / ".opt" / "maint" / "ucc-uninstall").glob("unpackerr-*.proxy.conf"))
    log = (home / ".opt" / "maint" / "ucc-uninstall.log").read_text()
    assert "done:" in log and "backup verified" in log


def test_keep_config_is_structurally_the_only_uninstall_form():
    """One code path: every `uninstall` token in the script is followed by
    --keep-config, and it is a literal (no variable can drop it)."""
    src = SCRIPT.read_text(encoding="utf-8")
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    calls = [ln for ln in code if '"app-$SLUG" uninstall' in ln]
    assert calls == ['  act "app-$SLUG" uninstall --keep-config </dev/null']


def test_post_check_data_file_missing_is_nonzero(tmp_path):
    r, calls, _ = _run(tmp_path, "unpackerr", "--execute", UNINSTALL_EFFECT="rm-data")
    assert r.returncode == 1
    assert "unpackerr.conf is GONE" in r.stdout
    # Never reinstall automatically.
    assert [c for c in calls if c.startswith("app-")] == ["app-unpackerr uninstall --keep-config"]


def test_post_check_proxy_vanished_is_restored_and_reloaded(tmp_path):
    r, calls, home = _run(tmp_path, "unpackerr", "--execute", UNINSTALL_EFFECT="rm-proxy")
    assert r.returncode == 1  # still a failed post-check: the operator verifies the route
    conf = home / ".apps" / "nginx" / "proxy.d" / "unpackerr.conf"
    assert conf.read_text() == "location /unpackerr {}\n"
    assert "appctl proxy-reload" in calls
    assert "restored from snapshot" in r.stdout


def test_post_check_lingering_container_is_nonzero(tmp_path):
    env_proc = tmp_path / "proc" / "777"
    env_proc.mkdir(parents=True)
    (env_proc / "status").write_text("Name:\tunpackerr\nUid:\t1000\t1000\t1000\t1000\n")
    (env_proc / "cgroup").write_text("0::/docker/abc123\n")
    (env_proc / "cmdline").write_text("/unpackerr\0-c\0/config/unpackerr.conf\0")
    r, _, _ = _run(tmp_path, "unpackerr", "--execute")
    assert r.returncode == 1
    assert "dormant container woken: unpackerr pid(s) 777" in r.stdout


def test_bash_syntax():
    assert subprocess.run(["bash", "-n", SCRIPT.as_posix()]).returncode == 0


def test_execute_refused_inside_the_maintenance_window(tmp_path):
    """Monday 12:00 UTC: hostpolicy says in-window -> refuse before any action."""
    r, calls, _ = _run(tmp_path, "unpackerr", "--execute", QFLIX_NOW="2026-10-19T12:00:00+00:00")
    assert r.returncode == 2
    assert "maintenance window" in r.stderr
    assert _uninstalls(calls) == []


# --- backup verification at scale (pipefail/SIGPIPE regression) ---------------

def test_backup_verify_survives_listing_over_pipe_buffer(tmp_path):
    """`printf "$listing" | grep -q` under pipefail returned 141 once the
    listing passed the 64 KiB pipe buffer (grep -q quits on its first match,
    printf takes SIGPIPE) -> a good backup was failed. ~300 KiB of names here."""
    env = _env(tmp_path)
    big = Path(env["HOME"]) / ".apps" / "unpackerr" / "many"
    big.mkdir()
    for i in range(3000):
        (big / f"{i:05d}-{'x' * 90}.dat").write_bytes(b"")
    r = subprocess.run(["bash", SCRIPT.as_posix(), "unpackerr", "--execute"], env=env,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "backup verified" in r.stdout


def test_no_pipe_into_grep_q_under_pipefail():
    """Structural guard for the same bug: no `| grep -q` anywhere in code."""
    code = [ln for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "| grep -q" in ln]


# --- key files outside ~/.apps/<slug> --------------------------------------------

def test_qbittorrent_home_key_file_is_backed_up_and_verified(tmp_path):
    r, calls, home = _run(tmp_path, "qbittorrent", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [c for c in calls if c.startswith("app-")] == ["app-qbittorrent uninstall --keep-config"]
    bdir = home / ".opt" / "maint" / "ucc-uninstall"
    extra = list(bdir.glob("qbittorrent-*.extra"))
    assert len(extra) == 1
    copy = extra[0] / ".config" / "qBittorrent" / "qBittorrent.conf"
    assert copy.read_text() == (home / ".config" / "qBittorrent" / "qBittorrent.conf").read_text()
    assert "extra backup verified" in r.stdout


def test_qbittorrent_dry_run_copies_nothing(tmp_path):
    r, _, home = _run(tmp_path, "qbittorrent")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "would run: cp -p" in r.stdout
    assert not (home / ".opt" / "maint" / "ucc-uninstall").exists()


def test_qbittorrent_gone_message_names_the_real_copy(tmp_path):
    r, _, home = _run(tmp_path, "qbittorrent", "--execute", UNINSTALL_EFFECT="rm-qbit")
    assert r.returncode == 1
    line = next(ln for ln in r.stdout.splitlines() if "is GONE" in ln)
    assert ".extra/.config/qBittorrent/qBittorrent.conf" in line
    assert ".tgz" not in line  # the tarball does not hold it


def test_postgres_gone_message_says_no_backup(tmp_path):
    """pg-native is outside ~/.apps/postgres: never claim the tarball has it."""
    r, _, _ = _run(tmp_path, "postgres", "--execute", UNINSTALL_EFFECT="rm-pg")
    assert r.returncode == 1
    line = next(ln for ln in r.stdout.splitlines() if "is GONE" in ln)
    assert "NO automated backup" in line
    assert "restore from" not in line


def test_in_slug_gone_message_names_the_tarball(tmp_path):
    r, _, _ = _run(tmp_path, "unpackerr", "--execute", UNINSTALL_EFFECT="rm-data")
    line = next(ln for ln in r.stdout.splitlines() if "is GONE" in ln)
    assert ".tgz (unpackerr/unpackerr.conf)" in line
