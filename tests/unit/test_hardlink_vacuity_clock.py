"""A canary that stops asserting must not keep reporting green forever.

COUNCIL FINDING 8. hardlink-integrity has two exits that pass WITHOUT running
its assertion -- an empty completed-pool, and a sample below MIN_SAMPLE. Both
are individually justified (the torrent janitor legitimately empties the pool,
and a 2-torrent sample cannot evidence a systemic regression). The defect was
that "inconclusive" and "verified good" both exited 0, so a guard that had
asserted nothing for a week looked exactly like a guard that had just checked.

That is not hypothetical any more: the torrent janitor reaps to ratio, and the
live pool is down to a couple of torrents. The guard can quietly retire itself.

These tests EXECUTE the canary's embedded python rather than grepping it, so
they exercise the real clock: state creation, the streak surviving across runs,
the trip, and the reset. Threshold logic is driven through the empty-pool exit,
which short-circuits before the library walk and so needs no seedbox layout.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts" / "canaries" / "hardlink-integrity.sh"

DAY = 86400


def _embedded_python() -> str:
    """Lift the python heredoc out of the shell wrapper.

    Pinned deliberately: if the delimiter or the heredoc style changes, this
    raises instead of silently testing an empty string and passing.
    """
    src = CANARY.read_text(encoding="utf-8")
    start = src.index('python3 <<"PYEND"') + len('python3 <<"PYEND"')
    end = src.index("\nPYEND", start)
    body = src[start:end]
    assert "_vacuous_exit" in body, "extracted the wrong block"
    return body


@pytest.fixture(scope="module")
def code():
    return _embedded_python()


def _run(code, tmp_path, torrents, *, max_days="7", completed_path=None):
    """Run the embedded canary python with a fake HOME and torrent payload."""
    payload = tmp_path / "qfh-completed.json"
    payload.write_text(json.dumps(torrents), encoding="utf-8")

    # The script reads a fixed /tmp path; rewrite it to the fixture's file so the
    # test never depends on (or pollutes) a real /tmp.
    body = code.replace('"/tmp/qfh-completed.json"',
                        json.dumps(str(completed_path or payload)))

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)          # expanduser on Windows
    env["MAX_VACUOUS_DAYS"] = max_days
    script = tmp_path / "canary_body.py"
    script.write_text(body, encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], env=env,
                          capture_output=True, text=True)


def _state_file(tmp_path) -> Path:
    return tmp_path / ".opt" / "maint" / "hardlink-integrity" / "vacuity.json"


# --- arming ----------------------------------------------------------------

def test_first_vacuous_run_passes_and_starts_the_clock(code, tmp_path):
    r = _run(code, tmp_path, [])
    assert r.returncode == 0, r.stderr
    assert "inconclusive" in r.stdout
    assert "blind 0.0d of 7d" in r.stdout, (
        "the operator must be able to SEE the streak before it pages: " + r.stdout
    )
    st = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))
    assert st["reason"] == "empty-pool"
    assert abs(st["since"] - int(time.time())) < 120


def test_the_streak_survives_across_runs(code, tmp_path):
    """The clock must measure the STREAK, not the last run -- rewriting `since`
    every tick would mean it can never reach the threshold."""
    _run(code, tmp_path, [])
    first = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))["since"]
    _run(code, tmp_path, [])
    second = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))["since"]
    assert first == second, "each run reset the clock; it could never trip"


# --- tripping --------------------------------------------------------------

def test_it_fails_once_it_has_been_blind_too_long(code, tmp_path):
    """THE POINT OF THE FINDING. Same input as the passing case above -- the
    only difference is how long it has been saying nothing."""
    sf = _state_file(tmp_path)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"since": int(time.time()) - 8 * DAY,
                              "reason": "empty-pool"}), encoding="utf-8")

    r = _run(code, tmp_path, [])
    assert r.returncode == 1, "8 days of asserting nothing still reported green"
    assert "STAGE=hardlink-blind" in r.stderr, r.stderr
    assert "reason=empty-pool" in r.stderr


def test_just_under_the_threshold_still_passes(code, tmp_path):
    sf = _state_file(tmp_path)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"since": int(time.time()) - 6 * DAY}),
                  encoding="utf-8")
    r = _run(code, tmp_path, [])
    assert r.returncode == 0
    assert "blind 6.0d of 7d" in r.stdout


def test_the_threshold_is_tunable(code, tmp_path):
    """The unit passes QFLIX_CANARY_HARDLINK_MAX_VACUOUS_DAYS through; a
    hardcoded 7 would make that Environment= line a no-op."""
    sf = _state_file(tmp_path)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"since": int(time.time()) - 2 * DAY}),
                  encoding="utf-8")
    assert _run(code, tmp_path, [], max_days="1").returncode == 1
    assert _run(code, tmp_path, [], max_days="30").returncode == 0


# --- robustness: the bookkeeping must never be the thing that pages --------

def test_corrupt_state_re_arms_instead_of_crashing(code, tmp_path):
    sf = _state_file(tmp_path)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text("{not json at all", encoding="utf-8")
    r = _run(code, tmp_path, [])
    assert r.returncode == 0, "a corrupt state file crashed the canary: " + r.stderr
    assert "re-arming" in r.stderr
    assert json.loads(sf.read_text(encoding="utf-8"))["since"] > 0


def test_a_future_timestamp_does_not_disable_the_clock(code, tmp_path):
    """A `since` in the future yields a negative age, which would sail under any
    threshold forever -- silently reinstating the exact bug being fixed."""
    sf = _state_file(tmp_path)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"since": int(time.time()) + 400 * DAY}),
                  encoding="utf-8")
    r = _run(code, tmp_path, [], max_days="7")
    assert r.returncode == 0
    assert "blind 0.0d" in r.stdout, "negative age accepted: " + r.stdout


def test_missing_payload_is_not_swallowed_as_a_pass(code, tmp_path):
    """Guard the fixture itself: if the torrents file cannot be read the run must
    error, not be mistaken for an empty pool (which would pass)."""
    r = _run(code, tmp_path, [], completed_path=tmp_path / "does-not-exist.json")
    assert r.returncode != 0
    assert not _state_file(tmp_path).exists(), (
        "a read error armed the vacuity clock, so a broken canary would look "
        "merely inconclusive"
    )
