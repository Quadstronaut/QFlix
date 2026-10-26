"""canary-qbit-stall: an unreadable torrents/info body must be red (QFLX-79).

Before the fix a non-JSON body made the embedded python fail, `read` hit EOF,
the zero-initialised counters survived and the script printed qbit-flowing,
exit 0 -- green on a client it could not read. Runs the real script in on-host
mode against a fake curl (login + transfer/info + torrents/info) and a python3
shim, same harness as test_canary_sab_stall_pp.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.test_canary_sab_stall_pp import FAKE_PY3, _posix

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts" / "canaries" / "qbit-stall.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_CURL = r'''#!/usr/bin/env bash
url=""; out=""; wfmt=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w) wfmt="$2"; shift ;;
    http*) url="$1" ;;
  esac
  shift
done
case "$url" in
  *auth/login*) printf 'Ok.\n' > "$out"; printf '200' ;;
  *transfer/info*) cat "$FAKE_DIR/transfer.json" ;;
  *torrents/info*) cat "$FAKE_DIR/torrents.body" ;;
  *) exit 22 ;;
esac
'''


def _run(tmp: Path, torrents_body: str):
    home = tmp / "home"
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    (home / "secrets" / "htpasswd.password").write_text("pw\n")
    fake = tmp / "fake"
    fake.mkdir(exist_ok=True)
    (fake / "transfer.json").write_text(json.dumps({"dl_info_speed": 0, "up_info_speed": 0}))
    (fake / "torrents.body").write_text(torrents_body)
    for name, body in (("curl", FAKE_CURL),
                       ("python3", FAKE_PY3.replace("@PY@", _posix(sys.executable)))):
        p = fake / name
        p.write_text(body, newline="\n")
        p.chmod(0o755)
    marker = tmp / "host.id"
    marker.write_text("test-slot\n")
    env = dict(os.environ, HOME=_posix(home), FAKE_DIR=_posix(fake),
               QFLIX_HOST_ID_FILE=_posix(marker),
               PATH=str(fake) + os.pathsep + os.environ.get("PATH", ""))
    return subprocess.run(["bash", _posix(CANARY)], env=env, capture_output=True,
                          text=True, timeout=60)


@pytest.mark.parametrize("body", ["<html>502 Bad Gateway</html>", "{\"truncated\": "])
def test_unparseable_torrents_body_is_red_not_flowing(tmp_path, body):
    r = _run(tmp_path, body)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=qbit-up-fail msg=torrents-json-parse" in r.stderr
    assert "qbit-flowing" not in r.stdout


def test_parseable_idle_client_is_still_green(tmp_path):
    r = _run(tmp_path, "[]")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "qbit-flowing" in r.stdout
