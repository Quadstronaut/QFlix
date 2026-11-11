"""QFLX-93: qflix-dash runs its OWN pinned portable Node, never nvm, never Seerr's.

Why: SvelteKit 3 needs Node >= 22.17. The dash ran whatever `nvm install 20`
resolved to, which is a floating major (not a pin) and too old. Seerr already has
a pinned Node 22, but sharing its dir would let a Seerr upgrade move the dash's
runtime, so the dash gets its own: ~/.apps/qflix-dash/bin/node-v<ver> behind a
bin/node symlink. Rollback is then a symlink flip and a restart.

STRUCTURAL tier: pins == versions.env, version floor, no nvm on executable
lines, the unit execs the dash's own symlink, the WASM flag survives.
BEHAVIOURAL tier: EXTRACTS the installer's step-1 heredoc verbatim and runs it
against a fake $HOME with a fake `curl` serving a fabricated tarball, proving
the sha256 gate and the placement are real.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "configure" / "90-qflix-dash-install.sh"
UNIT_TMPL = REPO / "scripts" / "qflix-dash" / "qflix-dash.service.tmpl"
BASH = shutil.which("bash")
SHA256SUM = shutil.which("sha256sum")

needs_shell = pytest.mark.skipif(
    not BASH or not SHA256SUM, reason="behavioural tier needs bash + sha256sum"
)


def _versions_env() -> dict:
    out = {}
    for line in (REPO / "versions.env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _code_lines(text: str) -> list[str]:
    return [l for l in text.splitlines() if not l.lstrip().startswith("#")]


def test_installer_pins_match_versions_env():
    v = _versions_env()
    text = INSTALLER.read_text(encoding="utf-8")
    assert f'DASH_NODE_VERSION="{v["QFLIX_DASH_NODE_VERSION"]}"' in text
    assert f'DASH_NODE_SHA256="{v["QFLIX_DASH_NODE_SHA256"]}"' in text
    assert re.fullmatch(r"[0-9a-f]{64}", v["QFLIX_DASH_NODE_SHA256"])


def test_pinned_node_meets_the_sveltekit_3_floor():
    major, minor, _ = (int(x) for x in _versions_env()["QFLIX_DASH_NODE_VERSION"].split("."))
    assert (major, minor) >= (22, 17), "SvelteKit 3 engines: node >= 22.17"


def test_dash_node_is_not_seerrs():
    """A Seerr upgrade must never be able to move the dash's runtime."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert ".apps/seerr" not in text
    assert "NODE='%h/.apps/qflix-dash/bin/node/bin/node'" in text


def test_no_nvm_on_executable_lines():
    code = "\n".join(_code_lines(INSTALLER.read_text(encoding="utf-8")))
    assert "nvm" not in code, "the dash is back on nvm (a floating major, not a pin)"
    assert "v20" not in code


def test_unit_execs_the_rendered_node_and_wasm_flag_survives():
    unit = UNIT_TMPL.read_text(encoding="utf-8")
    assert re.search(r"^ExecStart=@@NODE@@ %h/\.apps/qflix-dash/build/index\.js$", unit, re.M)
    text = INSTALLER.read_text(encoding="utf-8")
    assert re.search(r"^NODE_OPTIONS=--disable-wasm-trap-handler", text, re.M), (
        "the Ultra.cc undici WASM OOM fix (node-ultracc-wasm-fix) was dropped")


# ---------------------------------------------------------------------------
# BEHAVIOURAL TIER
# ---------------------------------------------------------------------------


def _step1_block() -> str:
    src = INSTALLER.read_text(encoding="utf-8")
    blocks = re.findall(r"<<'REMOTE'\n(.*?)\nREMOTE\n", src, re.S)
    hits = [b for b in blocks if "node-v$V-linux-x64.tar.gz" in b]
    assert len(hits) == 1, "the Node-install heredoc could not be located"
    return hits[0]


def _fake_tarball(ver: str, prints: str | None = None) -> bytes:
    """node-v<ver>-linux-x64/bin/node: a shell script that prints v<ver>
    (or `prints`, to fabricate a junk binary inside a correctly-hashed tarball)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        body = f"#!/bin/sh\necho {prints or 'v' + ver}\n".encode()
        ti = tarfile.TarInfo(f"node-v{ver}-linux-x64/bin/node")
        ti.size, ti.mode = len(body), 0o755
        tf.addfile(ti, io.BytesIO(body))
    return buf.getvalue()


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    if not home.exists():
        sec = home / "secrets"
        sec.mkdir(parents=True)
        for name in ("port", "session_secret", "plex_client_id", "discord_webhook"):
            (sec / f"qflix-dash.{name}").write_text("x\n", encoding="utf-8")
    return home


def _bash_env(home: Path) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(home)
    # Git Bash's `ln -s` silently COPIES unless told to make a real symlink; the
    # flip semantics under test only exist with real symlinks (Linux CI has them).
    env["MSYS"] = "winsymlinks:nativestrict"
    return env


def _bash(home: Path, cmd: str) -> str:
    r = subprocess.run([BASH, "-c", cmd], env=_bash_env(home), timeout=30,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return (r.stdout + r.stderr).decode().strip()


OLD = "22.0.0"


def _seed_live_old_node(tmp_path: Path) -> Path:
    """A working install of an OLDER Node that bin/node currently points at."""
    home = _home(tmp_path)
    _bash(home, f'B="$HOME/.apps/qflix-dash/bin"; mkdir -p "$B/node-v{OLD}/bin" && '
                f'printf "#!/bin/sh\\necho v{OLD}\\n" > "$B/node-v{OLD}/bin/node" && '
                f'chmod 755 "$B/node-v{OLD}/bin/node" && ln -sfn node-v{OLD} "$B/node"')
    assert _live(home) == f"v{OLD}"
    return home


def _live(home: Path) -> str:
    """What the unit would exec right now: bin/node/bin/node --version."""
    return _bash(home, '"$HOME/.apps/qflix-dash/bin/node/bin/node" --version')


def _assert_old_target_untouched(home: Path):
    b = home / ".apps" / "qflix-dash" / "bin"
    assert _live(home) == f"v{OLD}", "bin/node was moved off the working Node"
    assert _bash(home, f'"$HOME/.apps/qflix-dash/bin/node-v{OLD}/bin/node" --version') == f"v{OLD}"
    assert not list(b.glob("node-v*.part.*")), "a .part leftover survived the failure"
    assert not list(b.glob(".node.*")), "a .node.<pid> temp link survived the failure"


def _run(tmp_path: Path, ver: str, sha: str, tarball: bytes, fail_mv_dst: str = ""):
    home = _home(tmp_path)
    fixture = tmp_path / "node.tgz"
    fixture.write_bytes(tarball)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    # mv shim: fails (doing nothing) when the DESTINATION matches $FAIL_MV_DST,
    # standing in for a quota-full or EXDEV move; otherwise the real mv.
    real_mv = Path(shutil.which("mv")).as_posix()
    (fakebin / "mv").write_text(
        '#!/usr/bin/env bash\ncase "${@: -1}" in $FAIL_MV_DST) '
        'echo "fake mv: refusing ${@: -1}" >&2; exit 1;; esac\n'
        f'exec "{real_mv}" "$@"\n', encoding="utf-8", newline="\n")
    os.chmod(fakebin / "mv", 0o755)
    # Fake curl: copy the fixture to whatever follows -o. Records the URL.
    (fakebin / "curl").write_text(
        "#!/usr/bin/env bash\nout=\nwhile [ $# -gt 0 ]; do case $1 in -o) out=$2; shift;; "
        "http*) echo \"$1\" > \"$HOME/curl.url\";; esac; shift; done\n"
        f"cp '{fixture.as_posix()}' \"$out\"\n",
        encoding="utf-8", newline="\n")
    os.chmod(fakebin / "curl", 0o755)
    script = tmp_path / "step1.sh"
    script.write_text(_step1_block() + "\n", encoding="utf-8", newline="\n")
    env = _bash_env(home)
    env["PATH"] = str(fakebin) + os.pathsep + env.get("PATH", "")
    env["FAIL_MV_DST"] = fail_mv_dst
    r = subprocess.run([BASH, str(script), ver, sha], env=env, timeout=60,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r, home


@needs_shell
def test_step1_installs_verifies_and_links_the_pinned_node(tmp_path):
    ver = "22.99.1"
    tgz = _fake_tarball(ver)
    r, home = _run(tmp_path, ver, hashlib.sha256(tgz).hexdigest(), tgz)
    out = (r.stdout + r.stderr).decode()
    assert r.returncode == 0, out
    assert f"node=v{ver}" in out, out
    b = home / ".apps" / "qflix-dash" / "bin"
    assert (b / f"node-v{ver}" / "bin" / "node").is_file()
    assert (b / "node" / "bin" / "node").is_file()
    url = (home / "curl.url").read_text(encoding="utf-8").strip()
    assert url == f"https://nodejs.org/dist/v{ver}/node-v{ver}-linux-x64.tar.gz"


@needs_shell
def test_step1_refuses_a_tarball_whose_sha256_does_not_match(tmp_path):
    ver = "22.99.1"
    r, home = _run(tmp_path, ver, "0" * 64, _fake_tarball(ver))
    out = (r.stdout + r.stderr).decode()
    assert r.returncode != 0, "a sha256 mismatch was installed:\n" + out
    assert "sha256 mismatch" in out, out
    assert not (home / ".apps" / "qflix-dash" / "bin" / f"node-v{ver}").exists()


# ---------------------------------------------------------------------------
# PR #153 review: a FAILED install must never move bin/node. The unit execs
# bin/node, so a dangling or junk target turns the next restart into a
# homepage outage. Each case starts from a working older Node that bin/node
# points at, and must end with bin/node still running it.
# ---------------------------------------------------------------------------

NEW = "22.99.1"


@needs_shell
@pytest.mark.parametrize("fail_mv_dst", [
    "*.part.*",            # first mv (scratch -> node-v<ver>.part.<pid>) fails
    f"*/node-v{NEW}",      # second mv (.part -> node-v<ver>) fails: .part must be cleaned
], ids=["first-mv", "second-mv"])
def test_failed_move_leaves_bin_node_on_the_old_target(tmp_path, fail_mv_dst):
    home = _seed_live_old_node(tmp_path)
    tgz = _fake_tarball(NEW)
    r, _ = _run(tmp_path, NEW, hashlib.sha256(tgz).hexdigest(), tgz, fail_mv_dst=fail_mv_dst)
    out = (r.stdout + r.stderr).decode()
    assert r.returncode != 0, "a failed mv reported success:\n" + out
    assert "fake mv: refusing" in out, out          # the failure really was injected
    _assert_old_target_untouched(home)
    assert not (home / ".apps" / "qflix-dash" / "bin" / f"node-v{NEW}").exists()


@needs_shell
def test_stray_empty_version_dir_is_refetched_not_linked(tmp_path):
    """A leftover empty node-v<ver>/ used to skip nothing and swallow the move:
    plain `mv part node-v<ver>` moves INTO an existing dir and exits 0, then the
    flip pointed bin/node at a dir with no bin/node in it."""
    home = _seed_live_old_node(tmp_path)
    (home / ".apps" / "qflix-dash" / "bin" / f"node-v{NEW}").mkdir()
    tgz = _fake_tarball(NEW)
    r, _ = _run(tmp_path, NEW, hashlib.sha256(tgz).hexdigest(), tgz)
    out = (r.stdout + r.stderr).decode()
    assert r.returncode == 0, out
    assert "re-fetching" in out, out
    assert _live(home) == f"v{NEW}", "bin/node does not run the re-fetched Node"
    # The previous target itself is never touched (it is the rollback).
    assert _bash(home, f'"$HOME/.apps/qflix-dash/bin/node-v{OLD}/bin/node" --version') == f"v{OLD}"


@needs_shell
def test_junk_binary_is_refused_before_the_flip(tmp_path):
    """Correct sha256, but the binary inside does not report the pinned version
    (wrong build, truncated, wrong arch). The flip must not happen."""
    home = _seed_live_old_node(tmp_path)
    tgz = _fake_tarball(NEW, prints="v0.0.0-junk")
    r, _ = _run(tmp_path, NEW, hashlib.sha256(tgz).hexdigest(), tgz)
    out = (r.stdout + r.stderr).decode()
    assert r.returncode != 0, "a junk binary was installed:\n" + out
    assert "bin/node NOT flipped" in out, out
    _assert_old_target_untouched(home)
    assert not (home / ".apps" / "qflix-dash" / "bin" / f"node-v{NEW}").exists(), (
        "the junk node-v<ver> was kept, so the next run would skip the fetch")
