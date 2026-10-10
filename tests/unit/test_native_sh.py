"""scripts/lib/native.sh - native installer lib (QFLX-21, spec 5.3/5.7).

Subprocess tests: each case sources native.sh in bash with HOME / the swap dir
pointed at tmp_path and a STUB appctl / curl / ss, then calls one function.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "scripts" / "lib" / "native.sh"
GOLDEN = REPO / "tests" / "fixtures" / "native_units"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _sh(tmp_path, body, *, ucc_version="4.0.20", extra_env=None):
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    appctl = stub / "appctl"
    appctl.write_text(
        '#!/bin/sh\n[ "$1" = version ] || exit 2\n'
        f"echo '{{\"data\": {{\"version\": \"{ucc_version}\"}}, \"result\": true}}'\n",
        newline="\n")
    ss = stub / "ss"
    ss.write_text("#!/bin/sh\ncat <<'EOT'\nLISTEN 0 4096 127.0.0.1:42050 0.0.0.0:*\nEOT\n",
                  newline="\n")
    curl = stub / "curl"   # copies $QFLIX_FAKE_PAYLOAD to the -o target
    curl.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                    'cp "$QFLIX_FAKE_PAYLOAD" "$out"\n', newline="\n")
    for p in (appctl, ss, curl):
        p.chmod(0o755)
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"hello-binary")
    env = dict(os.environ,
               HOME=str(tmp_path / "home"),
               QFLIX_APPS_DIR=str(tmp_path / "apps"),
               QFLIX_SWAP_DIR=str(tmp_path / "swap"),
               QFLIX_APPCTL=str(appctl), QFLIX_SS=str(ss), QFLIX_CURL=str(curl),
               QFLIX_FAKE_PAYLOAD=str(payload))
    env.update(extra_env or {})
    script = f'set -u\nsource "{NATIVE.as_posix()}"\n{body}\n'
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                          text=True, cwd=tmp_path)


def test_bash_syntax():
    r = subprocess.run(["bash", "-n", str(NATIVE)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- render_unit ---------------------------------------------------------------

def test_render_unit_dotnet_matches_golden(tmp_path):
    r = _sh(tmp_path, "native_render_unit sonarr dotnet Sonarr "
                      "'-nobrowser -data=%h/.apps/sonarr'")
    assert r.returncode == 0, r.stderr
    assert r.stdout == (GOLDEN / "qflix-sonarr.service").read_text()


@pytest.mark.parametrize("fam,slug", [("dotnet", "radarr"), ("go", "unpackerr"),
                                      ("node", "seerr"), ("python", "tautulli"),
                                      ("db", "postgres")])
def test_every_family_has_path_line_and_no_taskmax(tmp_path, fam, slug):
    r = _sh(tmp_path, f"native_render_unit {slug} {fam} exe '--x'")
    assert r.returncode == 0, r.stderr
    assert (f"Environment=PATH=%h/.apps/{slug}/bin/current:%h/bin:"
            "/usr/local/bin:/usr/bin:/bin\n") in r.stdout
    assert f"EnvironmentFile=%h/.config/qflix/{slug}.env\n" in r.stdout
    assert "TasksMax" not in r.stdout
    assert "Restart=on-failure" in r.stdout and "StartLimitBurst=5" in r.stdout


def test_db_family_gets_120s_stop_timeout(tmp_path):
    out = _sh(tmp_path, "native_render_unit postgres db postgres '-D x'").stdout
    assert "TimeoutStopSec=120\n" in out
    assert "TimeoutStopSec=60\n" in _sh(tmp_path, "native_render_unit a go a ''").stdout


def test_node_wasm_flag_on_cli_never_in_node_options(tmp_path):
    unit = _sh(tmp_path, "native_render_unit seerr node node 'dist/index.js'").stdout
    assert "ExecStart=%h/.apps/seerr/bin/current/node --disable-wasm-trap-handler dist/index.js" in unit
    env = _sh(tmp_path, "native_render_env seerr node 1.0").stdout
    assert "NODE_OPTIONS" not in env and "UV_THREADPOOL_SIZE=4" in env


def test_unknown_family_refused(tmp_path):
    assert _sh(tmp_path, "native_render_unit x cobol x ''").returncode != 0
    assert _sh(tmp_path, "native_render_env x cobol 1").returncode != 0


def test_slug_traversal_refused(tmp_path):
    assert _sh(tmp_path, "native_render_unit ../evil go x ''").returncode != 0


def test_render_unit_absolute_exe_is_used_verbatim(tmp_path):
    """QFLX-27: an EXE starting %h/ or / replaces the bin/current prefix."""
    out = _sh(tmp_path, "native_render_unit bz python "
                        "%h/.apps/bz/venv/bin/python '%h/.apps/bz/bin/current/x.py --a'").stdout
    assert "ExecStart=%h/.apps/bz/venv/bin/python %h/.apps/bz/bin/current/x.py --a" + chr(10) in out
    plain = _sh(tmp_path, "native_render_unit bz python x.py '--a'").stdout
    assert "ExecStart=%h/.apps/bz/bin/current/x.py --a" + chr(10) in plain


def test_render_unit_workdir_defaults_to_the_data_dir_and_takes_a_subdir(tmp_path):
    """QFLX-36: seerr runs from bin/current (Next.js reads .next from the CWD)."""
    dflt = _sh(tmp_path, "native_render_unit sr node x ''").stdout
    assert "WorkingDirectory=%h/.apps/sr" + chr(10) in dflt
    sub = _sh(tmp_path, "native_render_unit sr node x '' %h/.apps/sr/bin/current").stdout
    assert "WorkingDirectory=%h/.apps/sr/bin/current" + chr(10) in sub
    for bad in ("/tmp", "%h/.apps/other", "%h/.apps/sr/../x"):
        assert _sh(tmp_path, f"native_render_unit sr node x '' '{bad}'").returncode != 0


# --- env caps -------------------------------------------------------------------

@pytest.mark.parametrize("fam,lines", [
    ("dotnet", ["DOTNET_PROCESSOR_COUNT=4", "DOTNET_gcServer=0", "MALLOC_ARENA_MAX=2"]),
    ("go", ["GOMAXPROCS=4", "MALLOC_ARENA_MAX=2"]),
    ("node", ["UV_THREADPOOL_SIZE=4", "MALLOC_ARENA_MAX=2"]),
    ("python", ["MALLOC_ARENA_MAX=2"]),
])
def test_env_caps_per_family(tmp_path, fam, lines):
    out = _sh(tmp_path, f"native_render_env app {fam} 1.0").stdout.splitlines()
    for line in lines:
        assert line in out


def test_bazarr_extra_env_hook_sets_version(tmp_path):
    out = _sh(tmp_path, "native_render_env bazarr python 1.5.2").stdout.splitlines()
    assert "BAZARR_VERSION=1.5.2" in out
    other = _sh(tmp_path, "native_render_env sonarr dotnet 4.0.20").stdout.splitlines()
    assert not any(x.startswith("BAZARR_VERSION") for x in other)


def test_extra_env_args_appended(tmp_path):
    out = _sh(tmp_path, "native_render_env app go 1 FOO=bar").stdout.splitlines()
    assert "FOO=bar" in out


# --- fetch_verify ---------------------------------------------------------------

def test_fetch_verify_ok_and_mismatch(tmp_path):
    good = hashlib.sha256(b"hello-binary").hexdigest()
    dest = tmp_path / "out.bin"
    r = _sh(tmp_path, f"native_fetch_verify http://x/y {good} '{dest.as_posix()}'")
    assert r.returncode == 0, r.stderr
    assert dest.read_bytes() == b"hello-binary"
    dest.unlink()
    r = _sh(tmp_path, f"native_fetch_verify http://x/y {'0' * 64} '{dest.as_posix()}'")
    assert r.returncode != 0 and not dest.exists()


def test_fetch_verify_rejects_malformed_sha(tmp_path):
    assert _sh(tmp_path, "native_fetch_verify http://x/y nothex /tmp/never").returncode != 0


# --- install_versioned + parity ---------------------------------------------------

def test_install_versioned_lays_out_bin_and_current(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Sonarr").write_text("bin")
    r = _sh(tmp_path, f"native_install_versioned sonarr 4.0.20 '{src.as_posix()}'")
    assert r.returncode == 0, r.stderr
    bindir = tmp_path / "apps" / "sonarr" / "bin"
    assert (bindir / "4.0.20" / "Sonarr").read_text() == "bin"
    assert (bindir / "current" / "Sonarr").read_text() == "bin"   # resolves either way
    if os.name != "nt":   # Git Bash on Windows copies instead of linking
        assert os.readlink(bindir / "current") == "4.0.20"


def test_install_versioned_refuses_version_mismatch_and_touches_nothing(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    r = _sh(tmp_path, f"native_install_versioned sonarr 4.0.21 '{src.as_posix()}'")
    assert r.returncode != 0 and "parity" in r.stderr
    assert not (tmp_path / "apps" / "sonarr" / "bin").exists()


def test_parity_fails_closed_when_version_unreadable(tmp_path):
    r = _sh(tmp_path, "native_check_parity sonarr 4.0.20", ucc_version="")
    assert r.returncode != 0


def test_parity_tolerates_leading_v(tmp_path):
    assert _sh(tmp_path, "native_check_parity sonarr v4.0.20").returncode == 0


def test_install_versioned_is_idempotent(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _sh(tmp_path, f"native_install_versioned sonarr 4.0.20 '{src.as_posix()}'")
    r = _sh(tmp_path, f"native_install_versioned sonarr 4.0.20 '{src.as_posix()}'")
    assert r.returncode == 0, r.stderr


# --- soak + listen helpers -------------------------------------------------------

def test_soak_check_and_close_window_wrap_swapstate(tmp_path):
    seed = ("import sys; sys.path.insert(0, r'%s'); from lib import swapstate as s; "
            "s.update_state('sonarr', swap_date='2026-10-25', soak_until='2999-01-01')"
            % (REPO / "scripts" / "maint"))
    env = dict(os.environ, QFLIX_SWAP_DIR=str(tmp_path / "swap"))
    subprocess.run(["python3", "-c", seed], env=env, check=True)
    assert _sh(tmp_path, "native_soak_check sonarr").returncode == 1
    assert _sh(tmp_path, "native_close_window sonarr").returncode == 0
    assert _sh(tmp_path, "native_soak_check nobody").returncode == 0


def test_listen_capture_then_compare_clean(tmp_path):
    r = _sh(tmp_path, "native_listen_capture sonarr 42050 && native_listen_compare sonarr")
    assert r.returncode == 0, r.stderr + r.stdout
    assert (tmp_path / "swap" / "sonarr" / "listen-set.before").read_text() == "127.0.0.1:42050\n"
