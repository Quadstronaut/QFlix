"""movie/anime canaries: an empty/non-JSON API answer is a named STAGE, never a traceback.

QFLX-115: on 2026-11-11 05:31 UTC GET /api/v1/settings/radarr answered nothing
(curl -sf timed out) and `curl | python3 json.load` crashed, so Kuma got a
Python traceback and no STAGE= line. Runs the real scripts/canaries/movie.sh
and anime.sh in on-host mode (QFLIX_HOST_ID_FILE marker, so sshm runs the body
locally) against a fake $HOME and a fake curl on PATH, same harness as
test_canary_sab_stall_pp.
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


# Answers by URL from fixture files in $FAKE_DIR. A missing fixture = curl exit
# 28 (timeout) with http_code 000 and no body, which is what the incident looked
# like from the canary's side. `<fixture>.code` overrides the HTTP status (401).
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
code=200; body="{}"; rc=0
f() {
  if [ ! -f "$FAKE_DIR/$1" ]; then code=000; body=""; rc=28; return; fi
  body="$(cat "$FAKE_DIR/$1")"
  [ -f "$FAKE_DIR/$1.code" ] && code="$(cat "$FAKE_DIR/$1.code")"
}
case "$method $url" in
  "GET "*"/system/status"|"GET "*"/api/v1/status") ;;
  "GET "*"/api/v3/movie") f movies.json ;;
  "GET "*"/api/v3/series") f series.json ;;
  "GET "*"/settings/radarr"|"GET "*"/settings/sonarr") f settings.json ;;
  "GET "*"/api/v1/search"*) f search.json ;;
  "GET "*"/api/v1/movie/"*|"GET "*"/api/v1/tv/"*) ;;
  "POST "*"/api/v1/request") code=$(cat "$FAKE_DIR/postcode"); body=$(cat "$FAKE_DIR/postbody") ;;
  "GET "*"/api/v1/request?take=200") f reqs.json ;;
  "GET "*"/api/v1/request/7") body='{"media":{"externalServiceId":5}}' ;;
  "DELETE "*) code=204; body="" ;;
  *) exit 22 ;;
esac
[ "$out" = "/dev/null" ] || printf "%s" "$body"
# %b: curl expands the \n in -w "\n__HTTP__=%{http_code}" itself.
[ -n "$w" ] && printf "%b" "${w//%\{http_code\}/$code}"
exit $rc
'''

FAKE_PY3 = r'''#!/usr/bin/env bash
exec "@PY@" "$@"
'''


def _run(tmp: Path, script: str, files: dict, *, postcode="201", postbody='{"id":7}'):
    home = tmp / "home"
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    for svc in ("seerr", "radarr", "sonarr2"):
        (home / "secrets" / f"{svc}.port").write_text("1\n")
        (home / "secrets" / f"{svc}.key").write_text("SECRETKEY123\n")
    fake = tmp / "fake"
    fake.mkdir(exist_ok=True)
    for name, doc in files.items():
        (fake / name).write_text(doc if isinstance(doc, str) else json.dumps(doc))
    (fake / "postcode").write_text(postcode)
    (fake / "postbody").write_text(postbody)
    for name, body in (("curl", FAKE_CURL),
                       ("python3", FAKE_PY3.replace("@PY@", _posix(sys.executable)))):
        p = fake / name
        p.write_text(body, newline="\n")
        p.chmod(0o755)
    marker = tmp / "host.id"
    marker.write_text("test-slot\n")
    # The canaries run under `set -e` and ssh.sh resolves <root>/secrets with
    # a `cd` that fails when the dir is absent (a fresh checkout / CI), so the
    # canary runs from a copied tree that has one, exactly as on the box.
    root = tmp / "root"
    for rel in (f"scripts/canaries/{script}", "scripts/lib/ssh.sh"):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, dst)
    (root / "secrets").mkdir(exist_ok=True)
    env = dict(os.environ, HOME=_posix(home), FAKE_DIR=_posix(fake),
               QFLIX_HOST_ID_FILE=_posix(marker),
               PATH=str(fake) + os.pathsep + os.environ.get("PATH", ""))
    return subprocess.run(["bash", _posix(root / "scripts" / "canaries" / script)],
                          env=env, capture_output=True, text=True, timeout=120)


MOVIES = [{"id": 5, "tmdbId": 99}]
SERIES = [{"id": 5, "tvdbId": 1, "tmdbId": 99}]
SETTINGS = [{"id": 0, "isDefault": True, "name": "anime"}]


def _no_traceback(r):
    assert "Traceback" not in r.stderr + r.stdout


# ── movie.sh ──────────────────────────────────────────────────────────────────

def test_movie_happy_path_passes(tmp_path):
    r = _run(tmp_path, "movie.sh", {"settings.json": SETTINGS, "movies.json": MOVIES})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS: movie canary" in r.stdout


def test_movie_settings_timeout_is_a_stage_not_a_traceback(tmp_path):
    """The exact 2026-11-11 failure: settings/radarr gives no body."""
    r = _run(tmp_path, "movie.sh", {"movies.json": MOVIES})
    assert r.returncode == 1, r.stdout + r.stderr
    assert ("STAGE=seerr-api-fail msg=settings-radarr-no-json-after-3-tries"
            "-curl-rc-28-http-000") in r.stderr
    _no_traceback(r)


def test_movie_settings_401_names_the_http_code(tmp_path):
    """No -f any more: an auth failure must read as http-401, not a timeout."""
    r = _run(tmp_path, "movie.sh", {"settings.json": '{"message":"Unauthorized"}',
                                    "settings.json.code": "401", "movies.json": MOVIES})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=seerr-api-fail msg=settings-radarr-no-json-after-3-tries-curl-rc-0-http-401" in r.stderr
    assert "SECRETKEY123" not in r.stderr + r.stdout      # no key echoed
    _no_traceback(r)


def test_movie_settings_non_json_is_a_stage(tmp_path):
    r = _run(tmp_path, "movie.sh", {"settings.json": "<html>502 Bad Gateway</html>",
                                    "movies.json": MOVIES})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=seerr-api-fail" in r.stderr and "http-200" in r.stderr
    _no_traceback(r)


def test_movie_radarr_list_timeout_is_a_stage(tmp_path):
    r = _run(tmp_path, "movie.sh", {"settings.json": SETTINGS})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=radarr-api-fail msg=movie-list-no-json-after-3-tries-curl-rc-28" in r.stderr
    _no_traceback(r)


def test_movie_radarr_list_wrong_shape_is_seed_pick_fail(tmp_path):
    r = _run(tmp_path, "movie.sh", {"settings.json": SETTINGS, "movies.json": {"not": "a list"}})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=seed-pick-fail" in r.stderr
    _no_traceback(r)


def test_movie_201_without_id_is_push_fail(tmp_path):
    r = _run(tmp_path, "movie.sh", {"settings.json": SETTINGS, "movies.json": MOVIES},
             postbody="")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=seerr-push-fail msg=http-201-but-no-request-id-in-body" in r.stderr
    _no_traceback(r)


def test_movie_409_reuses_existing_request(tmp_path):
    r = _run(tmp_path, "movie.sh", {"settings.json": SETTINGS, "movies.json": MOVIES,
                                    "reqs.json": {"results": [{"id": 7, "media": {"tmdbId": 99}}]}},
             postcode="409", postbody="{}")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "created=0" in r.stdout


def test_movie_409_request_list_timeout_is_a_stage(tmp_path):
    r = _run(tmp_path, "movie.sh", {"settings.json": SETTINGS, "movies.json": MOVIES},
             postcode="409", postbody="{}")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=seerr-api-fail msg=request-list-no-json-after-3-tries" in r.stderr
    _no_traceback(r)


def test_movie_empty_library_still_skips_green(tmp_path):
    r = _run(tmp_path, "movie.sh", {"settings.json": SETTINGS, "movies.json": []})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SKIP: Radarr up but 0 movies" in r.stdout


# ── anime.sh (same bug class, same helper) ────────────────────────────────────

def test_anime_happy_path_passes(tmp_path):
    r = _run(tmp_path, "anime.sh", {"series.json": SERIES, "settings.json": SETTINGS})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS: anime canary" in r.stdout


def test_anime_settings_timeout_is_a_stage_not_a_traceback(tmp_path):
    r = _run(tmp_path, "anime.sh", {"series.json": SERIES})
    assert r.returncode == 1, r.stdout + r.stderr
    assert ("STAGE=seerr-api-fail msg=settings-sonarr-no-json-after-3-tries"
            "-curl-rc-28-http-000") in r.stderr
    _no_traceback(r)


def test_anime_series_timeout_is_a_stage(tmp_path):
    r = _run(tmp_path, "anime.sh", {"settings.json": SETTINGS})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=sonarr2-api-fail msg=series-list-no-json-after-3-tries" in r.stderr
    _no_traceback(r)


def test_anime_search_timeout_is_a_stage(tmp_path):
    """No tmdbId on the series: the tvdb search leg runs, and times out."""
    r = _run(tmp_path, "anime.sh", {"series.json": [{"id": 5, "tvdbId": 1}],
                                    "settings.json": SETTINGS})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=seerr-api-fail msg=search-no-json-after-3-tries" in r.stderr
    _no_traceback(r)


def test_anime_series_wrong_shape_is_seed_pick_fail(tmp_path):
    r = _run(tmp_path, "anime.sh", {"series.json": {"x": 1}, "settings.json": SETTINGS})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=seed-pick-fail" in r.stderr
    _no_traceback(r)


def test_anime_empty_library_still_skips_green(tmp_path):
    r = _run(tmp_path, "anime.sh", {"series.json": [], "settings.json": SETTINGS})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SKIP" in r.stdout


def test_anime_202_soft_pass_unchanged(tmp_path):
    r = _run(tmp_path, "anime.sh", {"series.json": SERIES, "settings.json": SETTINGS},
             postcode="202", postbody="{}")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "seerr-202 no-op" in r.stdout
