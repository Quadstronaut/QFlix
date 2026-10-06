"""QFLX-7: thread-ceiling WARN/FAIL lines name the top-5 thread owners.

Runs the real script with a stub `sshm` (executes the body locally via bash),
a stub `ulimit` (2000) and a fake `ps`, so no box is touched.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "canaries" / "thread-ceiling.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# comm -> thread count; 7 owners so the top-5 cut is exercised, one with a space.
OWNERS = {"python3": 177, "PMS": 127, "Plex Script": 20, "node": 9,
          "ffmpeg": 8, "sshd": 3, "bash": 1}


def _env(tmp_path):
    return dict(os.environ, HOME=str(tmp_path / "home"),
                PATH=str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])


def _setup(tmp_path, threads_total):
    root = tmp_path / "repo"
    (root / "scripts" / "canaries").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "canaries" / "thread-ceiling.sh")
    (root / "scripts" / "lib" / "ssh.sh").write_text(
        'sshm() { bash -c "ulimit() { echo 2000; }; $1"; }\n', newline="\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "home").mkdir()
    comm = tmp_path / "comm.txt"
    comm.write_text("".join("%s\n" % c for c, n in OWNERS.items()
                            for _ in range(n)), newline="\n")
    fake = tmp_path / "bin" / "ps"
    fake.write_text(
        '#!/usr/bin/env bash\n'
        'for a in "$@"; do [ "$a" = "comm=" ] && { cat "%s"; exit 0; }; done\n'
        'for a in "$@"; do [ "$a" = "-L" ] && { seq 1 %d; exit 0; }; done\n'
        'seq 1 10\n' % (comm.as_posix(), threads_total), newline="\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path):
    return subprocess.run(
        ["bash", str(tmp_path / "repo" / "scripts" / "canaries" / "thread-ceiling.sh")],
        capture_output=True, text=True, env=_env(tmp_path))


def test_warn_line_names_top5_owners(tmp_path):
    _setup(tmp_path, 1400)  # 70% -> WARN band
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("PASS-WARN:")
    assert "-top=python3:177,PMS:127,Plex_Script:20,node:9,ffmpeg:8" in r.stdout
    assert "sshd" not in r.stdout
    assert " " not in r.stdout.strip().split(": ", 1)[1]


def test_first_over_trip_sample_also_carries_owners(tmp_path):
    _setup(tmp_path, 1800)  # over 85%, first sample -> PASS-WARN
    r = _run(tmp_path)
    assert r.returncode == 0 and "-top=python3:177," in r.stdout


def test_sustained_fail_stage_line_carries_owners(tmp_path):
    _setup(tmp_path, 1800)
    _run(tmp_path)          # arms the streak
    r = _run(tmp_path)      # second consecutive sample pages
    assert r.returncode == 1
    assert r.stderr.startswith("STAGE=thread-fail msg=")
    assert "-top=python3:177,PMS:127" in r.stderr
    assert " " not in r.stderr.strip().split("msg=", 1)[1]


def test_plain_pass_has_no_owner_token(tmp_path):
    _setup(tmp_path, 1000)
    r = _run(tmp_path)
    assert r.stdout.startswith("PASS:") and "-top=" not in r.stdout
