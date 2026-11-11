"""movie canary: an empty/non-JSON API answer is a named STAGE, never a traceback.

QFLX-115: on 2026-11-11 05:31 UTC GET /api/v1/settings/radarr answered nothing
(curl -sf timed out) and `curl | python3 json.load` crashed, so Kuma got a
Python traceback and no STAGE= line. Runs the real scripts/canaries/movie.sh in
on-host mode (QFLIX_HOST_ID_FILE marker, so sshm runs the body locally) against
a fake $HOME and a fake curl on PATH, same harness as test_canary_sab_stall_pp.
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

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _posix(p) -> str:
    return Path(p).as_posix()


# Answers by URL. A missing fixture file = curl exit 28 (timeout) with no body,
# which is what the incident looked like from the canary's side.
FAKE_CURL = r'''#!/usr/bin/env bash
url=""; w=""; method=GET; out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -w) w="$2"; shift ;;
    -X) method="$2"; shift ;;
    -o) out="$2"; shift ;;
    -H|-m|--data) shift ;;
    http*) url="$1" ;;
  esac
  shift
done
code=200; body="{}"
case "$method $url" in
  "GET "*"/system/status"|"GET "*"/api/v1/status") ;;
  "GET "*"/api/v3/movie")
    [ -f "$FAKE_DIR/movies.json" ] || exit 28; body="$(cat "$FAKE_DIR/movies.json")" ;;
  "GET "*"/settings/radarr")
    [ -f "$FAKE_DIR/settings.json" ] || exit 28; body="$(cat "$FAKE_DIR/settings.json")" ;;
  "GET "*"/api/v1/movie/"*) ;;
  "POST "*"/api/v1/request") code=201; body='{"id":7}' ;;
  "GET "*"/api/v1/request/7") body='{"media":{"externalServiceId":5}}' ;;
  "DELETE "*) code=204; body="" ;;
  *) exit 22 ;;
esac
[ "$out" = "/dev/null" ] || printf "%s" "$body"
# %b: curl expands the \n in -w "\n__HTTP__=%{http_code}" itself.
[ -n "$w" ] && printf "%b" "${w//%\{http_code\}/$code}"
exit 0
'''

FAKE_PY3 = r'''#!/usr/bin/env bash
exec "@PY@" "$@"
'''


def _run(tmp: Path, *, settings, movies):
    home = tmp / "home"
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    for name, val in (("seerr.port", "1"), ("seerr.key", "k"),
                      ("radarr.port", "2"), ("radarr.key", "k")):
        (home / "secrets" / name).write_text(val + "\n")
    fake = tmp / "fake"
    fake.mkdir(exist_ok=True)
    for name, doc in (("settings.json", settings), ("movies.json", movies)):
        p = fake / name
        if doc is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    for name, body in (("curl", FAKE_CURL),
                       ("python3", FAKE_PY3.replace("@PY@", _posix(sys.executable)))):
        p = fake / name
        p.write_text(body, newline="\n")
        p.chmod(0o755)
    marker = tmp / "host.id"
    marker.write_text("test-slot\n")
    # movie.sh runs under `set -e` and ssh.sh resolves <root>/secrets with a
    # `cd` that fails when the dir is absent (a fresh checkout / CI), so the
    # canary runs from a copied tree that has one, exactly as on the box.
    root = tmp / "root"
    for rel in ("scripts/canaries/movie.sh", "scripts/lib/ssh.sh"):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, dst)
    (root / "secrets").mkdir(exist_ok=True)
    env = dict(os.environ, HOME=_posix(home), FAKE_DIR=_posix(fake),
               QFLIX_HOST_ID_FILE=_posix(marker),
               PATH=str(fake) + os.pathsep + os.environ.get("PATH", ""))
    return subprocess.run(["bash", _posix(root / "scripts/canaries/movie.sh")], env=env,
                          capture_output=True, text=True, timeout=120)


MOVIES = [{"id": 5, "tmdbId": 99}]
SETTINGS = [{"id": 0, "isDefault": True}]


def test_happy_path_passes(tmp_path):
    r = _run(tmp_path, settings=SETTINGS, movies=MOVIES)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS: movie canary" in r.stdout


def test_settings_timeout_is_a_stage_not_a_traceback(tmp_path):
    """The exact 2026-11-11 failure: settings/radarr gives no body."""
    r = _run(tmp_path, settings=None, movies=MOVIES)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=seerr-api-fail msg=settings-radarr-no-json-after-3-tries" in r.stderr
    assert "Traceback" not in r.stderr + r.stdout


def test_settings_non_json_is_a_stage(tmp_path):
    r = _run(tmp_path, settings="<html>502 Bad Gateway</html>", movies=MOVIES)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=seerr-api-fail" in r.stderr
    assert "Traceback" not in r.stderr + r.stdout


def test_radarr_list_timeout_is_a_stage(tmp_path):
    r = _run(tmp_path, settings=SETTINGS, movies=None)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=radarr-api-fail msg=movie-list-no-json-after-3-tries" in r.stderr
    assert "Traceback" not in r.stderr + r.stdout


def test_radarr_list_wrong_shape_is_seed_pick_fail(tmp_path):
    r = _run(tmp_path, settings=SETTINGS, movies={"not": "a list"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=seed-pick-fail" in r.stderr
    assert "Traceback" not in r.stderr + r.stdout


def test_empty_library_still_skips_green(tmp_path):
    r = _run(tmp_path, settings=SETTINGS, movies=[])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SKIP: Radarr up but 0 movies" in r.stdout
