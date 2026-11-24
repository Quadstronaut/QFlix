"""QFLX-126: canaries must not leave credentials in the shared temp dir.

The box is shared hosting: its system temp dir is one tmpfs used by ~150
tenants, and the box umask is 0002. Until 2026-11-24 qbit-stall.sh and
hardlink-integrity.sh logged in with `curl -c /tmp/<fixed name>`, which left a
LIVE qBittorrent session cookie world-readable, refreshed every 15/30 minutes
and never removed. sab-stall.sh wrote, then read back, a fixed name there with
no failure check, so a file planted by another tenant was parsed instead of
the real queue and could hold the canary green on a dead queue.

Static half: no canary names a path under /tmp, and each of the three sets
umask 077 and works in a mktemp dir.
Behavioural half: run the real qbit-stall.sh and sab-stall.sh against fakes and
check where the cookie went, under what umask, and that nothing is left.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.test_canary_sab_stall_pp import FAKE_CURL as SAB_FAKE_CURL
from tests.unit.test_canary_sab_stall_pp import FAKE_PY3, _posix

REPO = Path(__file__).resolve().parents[2]
CANARIES = REPO / "scripts" / "canaries"
FIXED = ("qbit-stall.sh", "hardlink-integrity.sh", "sab-stall.sh")


@pytest.mark.parametrize("name", FIXED)
def test_canary_works_in_a_private_mktemp_dir(name):
    src = (CANARIES / name).read_text(encoding="utf-8")
    assert "/tmp/" not in src, "%s names a path in the shared temp dir" % name
    assert "\numask 077\n" in src, "%s does not tighten the umask" % name
    assert 'T=$(mktemp -d "$' in src, "%s does not use a private mktemp dir" % name
    assert '/.opt/maint' in src or "$STATE_DIR/tmp." in src
    assert 'rmdir -- \\"$T\\"' in src, "%s does not remove its scratch dir on exit" % name
    # No recursive delete on a variable root (2026-10-21 incident rule).
    assert "rm -rf" not in src and "rm -r " not in src


def test_no_canary_writes_a_curl_cookie_jar_into_the_shared_temp_dir():
    offenders = []
    for p in sorted(CANARIES.glob("*.sh")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if ("-c /tmp/" in s) or ("-b /tmp/" in s) or ("--cookie-jar /tmp/" in s):
                offenders.append("%s:%d" % (p.name, i))
    assert offenders == [], offenders


pytestmark_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# Records where the cookie jar was asked for and under which umask, then
# answers like qBittorrent.
QBIT_FAKE_CURL = r'''#!/usr/bin/env bash
url=""; out=""; jar=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w) shift ;;
    -c) jar="$2"; shift ;;
    http*) url="$1" ;;
  esac
  shift
done
case "$url" in
  *auth/login*)
    printf 'Ok.\n' > "$out"
    printf 'SID\tsecret-session\n' > "$jar"
    printf '%s\n' "$jar" > "$FAKE_DIR/jar.path"
    umask > "$FAKE_DIR/umask.seen"
    printf '200' ;;
  *transfer/info*) cat "$FAKE_DIR/transfer.json" ;;
  *torrents/info*) cat "$FAKE_DIR/torrents.body" ;;
  *) exit 22 ;;
esac
'''


def _env(tmp: Path, curl_body: str):
    home = tmp / "home"
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    fake = tmp / "fake"
    fake.mkdir(exist_ok=True)
    for name, body in (("curl", curl_body),
                       ("python3", FAKE_PY3.replace("@PY@", _posix(sys.executable)))):
        p = fake / name
        p.write_text(body, newline="\n")
        p.chmod(0o755)
    marker = tmp / "host.id"
    marker.write_text("test-slot\n")
    env = dict(os.environ, HOME=_posix(home), FAKE_DIR=_posix(fake),
               QFLIX_HOST_ID_FILE=_posix(marker),
               PATH=str(fake) + os.pathsep + os.environ.get("PATH", ""))
    return home, fake, env


def _leftovers(home: Path):
    maint = home / ".opt" / "maint"
    return sorted(p.name for p in maint.glob("tmp.*")) if maint.exists() else []


@pytestmark_bash
def test_qbit_stall_keeps_the_session_cookie_private_and_removes_it(tmp_path):
    home, fake, env = _env(tmp_path, QBIT_FAKE_CURL)
    (home / "secrets" / "htpasswd.password").write_text("pw\n")
    (fake / "transfer.json").write_text(json.dumps({"dl_info_speed": 0, "up_info_speed": 0}))
    (fake / "torrents.body").write_text("[]")
    r = subprocess.run(["bash", _posix(CANARIES / "qbit-stall.sh")], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "qbit-flowing" in r.stdout
    jar = (fake / "jar.path").read_text().strip()
    assert "/.opt/maint/tmp.qbit-stall." in jar.replace("\\", "/"), jar
    assert not jar.startswith("/tmp/"), jar
    assert (fake / "umask.seen").read_text().strip().lstrip("0") == "77", \
        "the cookie jar was written under a permissive umask"
    assert not Path(jar).exists(), "session cookie left on disk after the run"
    assert _leftovers(home) == [], "scratch dir left behind"


@pytestmark_bash
def test_qbit_stall_cleans_up_on_a_failing_run_too(tmp_path):
    home, fake, env = _env(tmp_path, QBIT_FAKE_CURL)
    (home / "secrets" / "htpasswd.password").write_text("pw\n")
    (fake / "transfer.json").write_text(json.dumps({"dl_info_speed": 0, "up_info_speed": 0}))
    (fake / "torrents.body").write_text("<html>502</html>")
    r = subprocess.run(["bash", _posix(CANARIES / "qbit-stall.sh")], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "STAGE=qbit-up-fail" in r.stderr
    assert not Path((fake / "jar.path").read_text().strip()).exists()
    assert _leftovers(home) == []


@pytestmark_bash
def test_sab_stall_reads_only_its_own_private_queue_file_and_leaves_nothing(tmp_path):
    home, fake, env = _env(tmp_path, SAB_FAKE_CURL)
    (home / "secrets" / "sabnzbd.port").write_text("17007\n")
    (home / "secrets" / "sabnzbd.key").write_text("k\n")
    # A dead queue with work waiting: 0 KB/s, 2 active slots.
    (fake / "queue.json").write_text(json.dumps({"queue": {
        "kbpersec": "0", "paused": False,
        "slots": [{"status": "Downloading"}, {"status": "Queued"}]}}))
    (fake / "config.json").write_text(json.dumps(
        {"config": {"misc": {"pause_on_post_processing": 0}}}))
    r = subprocess.run(["bash", _posix(CANARIES / "sab-stall.sh")], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    # First sight of a stall: the state file is armed. Before the fix a planted
    # shared file reading kbpersec=5000 produced "sab-flowing" here instead.
    assert (home / ".opt" / "maint" / "sab-stall-since.epoch").exists(), r.stdout + r.stderr
    assert "sab-flowing" not in r.stdout
    assert _leftovers(home) == []
