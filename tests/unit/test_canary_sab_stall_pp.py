"""canary-sab-stall: the pause_on_post_processing drift leg (QFLX-33).

Runs the real scripts/canaries/sab-stall.sh in on-host mode (QFLIX_HOST_ID_FILE
marker, so sshm runs the body locally) against a fake $HOME (secrets + state
dir) with a fake curl on PATH that answers SAB's queue API and get_config for
misc.pause_on_post_processing, plus a python3 shim pointing at this interpreter.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts" / "canaries" / "sab-stall.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _posix(p) -> str:
    return Path(p).as_posix()


FAKE_CURL = r'''#!/usr/bin/env bash
url=""
for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done
case "$url" in
  *"mode=queue"*) cat "$FAKE_DIR/queue.json" ;;
  *"mode=get_config"*"keyword=pause_on_post_processing"*)
    [ -f "$FAKE_DIR/config.json" ] || exit 22
    cat "$FAKE_DIR/config.json" ;;
  *) exit 22 ;;
esac
'''

# python3 -> this interpreter. Under Git Bash a native Windows python cannot
# open the canary's /tmp/... paths, so (only where cygpath exists) the script
# on stdin and /tmp argv are rewritten to the Windows form. On Linux CI it is a
# plain exec.
FAKE_PY3 = r'''#!/usr/bin/env bash
if command -v cygpath >/dev/null 2>&1 && [ "${1:-}" = - ]; then
  T="$(cygpath -m /tmp)"; shift; args=()
  for a in "$@"; do case "$a" in /tmp/*) a="$T/${a#/tmp/}" ;; esac; args+=("$a"); done
  sed "s#/tmp/#$T/#g" | "@PY@" - "${args[@]}"
  exit $?
fi
exec "@PY@" "$@"
'''


def _run(tmp: Path, pp, queue=None):
    home = tmp / "home"
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    (home / "secrets" / "sabnzbd.port").write_text("17007\n")
    (home / "secrets" / "sabnzbd.key").write_text("k\n")
    fake = tmp / "fake"
    fake.mkdir(exist_ok=True)
    (fake / "queue.json").write_text(json.dumps(
        {"queue": queue or {"kbpersec": "0", "paused": False, "slots": []}}))
    cfg = fake / "config.json"
    if pp is None:
        cfg.unlink(missing_ok=True)
    else:
        cfg.write_text(json.dumps({"config": {"misc": {"pause_on_post_processing": pp}}}))
    for name, body in (("curl", FAKE_CURL),
                       ("python3", FAKE_PY3.replace("@PY@", _posix(sys.executable)))):
        p = fake / name
        p.write_text(body, newline="\n")
        p.chmod(0o755)
    marker = tmp / "host.id"
    marker.write_text("test-slot\n")
    # Native PATH form (os.pathsep): Git Bash converts it at startup, so the
    # fakes win over the real curl/python3 on Windows and on CI alike.
    env = dict(os.environ, HOME=_posix(home), FAKE_DIR=_posix(fake),
               QFLIX_HOST_ID_FILE=_posix(marker),
               PATH=str(fake) + os.pathsep + os.environ.get("PATH", ""))
    return subprocess.run(["bash", _posix(CANARY)], env=env, capture_output=True,
                          text=True, timeout=60)


@pytest.mark.parametrize("pp", [True, 1, "1"])
def test_pause_on_post_processing_on_is_red(tmp_path, pp):
    r = _run(tmp_path, pp)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=sab-pause-on-pp" in r.stderr


@pytest.mark.parametrize("pp", [False, 0, "0"])
def test_pause_on_post_processing_off_is_green(tmp_path, pp):
    r = _run(tmp_path, pp)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sab-flowing" in r.stdout
    assert "sab-pause-on-pp" not in r.stderr


def test_config_api_failure_is_sab_up_fail_not_green(tmp_path):
    r = _run(tmp_path, None)
    assert r.returncode == 1
    assert "STAGE=sab-up-fail msg=config-api-fail" in r.stderr


def test_drift_leg_does_not_mask_a_healthy_flowing_queue(tmp_path):
    r = _run(tmp_path, False, queue={"kbpersec": "5000", "paused": False,
                                     "slots": [{"status": "Downloading"}]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sab-flowing kbps=5000 slots=1" in r.stdout
