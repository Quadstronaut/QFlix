"""unstick-rate.sh's arr-regrab sub-check, EXECUTED rather than grepped.

WHY THE SUB-CHECK EXISTS
------------------------
`unstick-rate.sh` is named after a destructive ACTION — `DELETE /queue/{id}
?removeFromClient=true&blocklist=true` — and watched exactly one of the two
actors that perform it. `scripts/maint/arr-housekeeping.py --unstick` runs
hourly, does up to ten of them per run, and writes no events JSONL at all, so
the busier actor was invisible to the canary whose whole purpose is that this
class of action must never be silent.

WHAT THESE TESTS HAVE TO PROVE
------------------------------
  * a parked population at or over the threshold REDS (exit 1)
  * a ledger that exists and will not parse is CANNOT-ASSERT (exit 2)
  * an ABSENT ledger PASSES — the deliberate opposite of the events-dir rule
    ten lines above it in the same script. The events dir is created by its
    actor before the first write, so absence means breakage. The regrab ledger
    is written only when the guard has something to remember, so absence is
    the documented cold start and may last months. Exiting 2 on it would page
    daily for a healthy system, which is using missing data as an interlock.
  * INV-14: the existing WARN=3/FAIL=5 daily action counters, their defaults
    and their source path are untouched, and the arr-housekeeping actor is NOT
    blended into them. Blending an hourly actor that does up to ten per run
    into counters calibrated for one daily-capped actor would red this monitor
    permanently, which gets it muted, which is worse than the blind spot.

The canary is a shell wrapper around python heredocs that run on the box, so
the heredoc is lifted and executed against fixture ledgers (same technique as
test_tdarr_transcode_stall_canary.py). The shell-level structure that carries
the absent-file rule is additionally run under bash where bash is available.
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
CANARY = REPO / "scripts" / "canaries" / "unstick-rate.sh"


def _src() -> str:
    return CANARY.read_text(encoding="utf-8")


def _regrab_python() -> str:
    """The sub-check heredoc, pinned on a symbol so a rename fails HERE rather
    than silently making every test below vacuous."""
    src = _src()
    opener = 'python3 - "$REGRAB_LEDGER" "$REGRAB_PARK_FAIL" <<PY\n'
    start = src.index(opener) + len(opener)
    body = src[start:src.index("\nPY\n", start)]
    assert "regrab-parked-population" in body
    assert "regrab-ledger-unreadable" in body
    return body


def _run(body: str, ledger: Path, fail_at: int = 10):
    proc = subprocess.run([sys.executable, "-c", body, str(ledger), str(fail_at)],
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _ledger(tmp_path: Path, parked: int, unparked: int = 0) -> Path:
    data = {}
    for i in range(parked):
        data[f"sonarr|1234|{9000 + i}"] = {
            "adds": [1.0, 2.0, 3.0], "parked": True, "notified": True,
            "title": f"Invented Series {i}", "last": 3.0}
    for i in range(unparked):
        data[f"sonarr|4321|{8000 + i}"] = {
            "adds": [1.0], "parked": False, "notified": False,
            "title": f"Invented Other {i}", "last": 1.0}
    p = tmp_path / "arr-regrab-ledger.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# The extractor found the right block
# ---------------------------------------------------------------------------

def test_the_sub_check_body_is_found():
    body = _regrab_python()
    assert "parked" in body
    assert len(body.splitlines()) > 10


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

def test_clean_ledger_passes(tmp_path):
    rc, out, err = _run(_regrab_python(), _ledger(tmp_path, parked=0, unparked=7))
    assert rc == 0, err
    assert "PASS" in out and "0 item(s) parked" in out


def test_population_below_threshold_passes(tmp_path):
    rc, out, err = _run(_regrab_python(), _ledger(tmp_path, parked=9))
    assert rc == 0, err
    assert "9 item(s) parked" in out


def test_population_at_threshold_reds(tmp_path):
    rc, out, err = _run(_regrab_python(), _ledger(tmp_path, parked=10))
    assert rc == 1
    assert "STAGE=regrab-parked-population" in err
    assert "10-item(s)" in err


def test_threshold_is_env_tunable(tmp_path):
    led = _ledger(tmp_path, parked=3)
    assert _run(_regrab_python(), led, fail_at=10)[0] == 0
    assert _run(_regrab_python(), led, fail_at=3)[0] == 1


def test_only_parked_entries_count(tmp_path):
    """An entry with strikes against it but no park is not a finding: the
    guard is WORKING when it counts without acting."""
    rc, out, _ = _run(_regrab_python(), _ledger(tmp_path, parked=0, unparked=50))
    assert rc == 0
    assert "0 item(s) parked" in out


@pytest.mark.parametrize("payload", [
    "",                                  # truncated to nothing
    '{"a": {"parked": true}',            # truncated mid-object
    "not json at all",
    '["a list", "not an object"]',       # right JSON, wrong shape
    "null",
])
def test_ac14_present_but_unparseable_ledger_exits_2(tmp_path, payload):
    p = tmp_path / "arr-regrab-ledger.json"
    p.write_text(payload, encoding="utf-8")
    rc, _, err = _run(_regrab_python(), p)
    assert rc == 2, payload
    assert "STAGE=regrab-ledger-unreadable" in err


def test_entries_that_are_not_objects_do_not_crash_the_check(tmp_path):
    p = tmp_path / "arr-regrab-ledger.json"
    p.write_text(json.dumps({"a|1|1": "string", "b|1|1": 7,
                             "c|1|1": {"parked": True}}), encoding="utf-8")
    rc, out, err = _run(_regrab_python(), p)
    assert rc == 0, err
    assert "1 item(s) parked" in out


# ---------------------------------------------------------------------------
# AC-14 — the absent-file asymmetry, at the shell level
# ---------------------------------------------------------------------------

def test_ac14_absent_ledger_is_guarded_by_a_file_test_and_explained():
    src = _src()
    assert 'if [ -f "$REGRAB_LEDGER" ]; then' in src, (
        "the sub-check must be wrapped in a file test — an absent ledger is "
        "the documented cold start and must PASS")
    # The asymmetry with the events-dir rule directly below is deliberate and
    # must be argued in the file, not just implemented.
    assert "opposite of the events-dir rule" in src
    assert "missing data as an" in src and "interlock" in src


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_ac14_absent_ledger_passes_end_to_end(tmp_path):
    """The whole remote body, run for real, with NO ledger present and the
    events dir faked as a clean day. Must exit 0."""
    body = _remote_body()
    events = tmp_path / "events"
    events.mkdir()
    env = dict(os.environ)
    env["QFLIX_CANARY_REGRAB_LEDGER"] = str(tmp_path / "does-not-exist.json")
    env["QFLIX_CANARY_UNSTICK_EVENTS"] = _posix(events)
    proc = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                          env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 destructive action(s) today" in proc.stdout


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_a_parked_population_reds_end_to_end(tmp_path):
    body = _remote_body()
    events = tmp_path / "events"
    events.mkdir()
    env = dict(os.environ)
    env["QFLIX_CANARY_REGRAB_LEDGER"] = _posix(_ledger(tmp_path, parked=12))
    env["QFLIX_CANARY_UNSTICK_EVENTS"] = _posix(events)
    proc = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                          env=env)
    assert proc.returncode == 1
    assert "STAGE=regrab-parked-population" in proc.stderr


def _posix(p) -> str:
    return str(p).replace(os.sep, "/")


def _remote_body() -> str:
    """The single-quoted body that is actually shipped to the box, with the
    python3 invocation retargeted at whichever interpreter runs the tests
    (git-bash has no `python3` on PATH on a Windows workstation)."""
    src = _src()
    opener = "RES=$(sshm '\n"
    start = src.index(opener) + len(opener)
    body = src[start:src.index("\n')", start)]
    return body.replace("python3 - ", f'"{_posix(sys.executable)}" - ')


# ---------------------------------------------------------------------------
# INV-14 / AC-13 — the existing behaviour is pinned, not adjusted
# ---------------------------------------------------------------------------

def test_inv14_existing_thresholds_defaults_and_source_path_unchanged():
    src = _src()
    assert "WARN_AT=${QFLIX_CANARY_UNSTICK_WARN:-3}" in src
    assert "FAIL_AT=${QFLIX_CANARY_UNSTICK_FAIL:-5}" in src
    assert "CAP=${QFLIX_COLLECT_MAX_ACTIONS:-10}" in src
    assert ("EVENTS=${QFLIX_CANARY_UNSTICK_EVENTS:-"
            "$HOME/.opt/qflix-collect/events}") in src
    # The events-dir rule keeps its own, opposite, missing-data verdict.
    assert "STAGE=unstick-events-missing" in src
    assert "STAGE=unstick-events-unreadable" in src
    assert "STAGE=unstick-rate-high" in src
    assert "STAGE=unstick-cap-reached" in src


def test_ac13_arr_housekeeping_actions_are_not_blended_into_the_counters():
    """The counting heredoc must know nothing about the regrab ledger. If the
    two ever merge, this monitor reds permanently and gets muted."""
    src = _src()
    opener = 'python3 - "$F" <<PY 2>/dev/null\n'
    start = src.index(opener) + len(opener)
    counting = src[start:src.index("\nPY\n", start)]
    assert 'd.get("action") == "unstick"' in counting
    for forbidden in ("regrab", "REGRAB", "parked", "arr-regrab-ledger"):
        assert forbidden not in counting, (
            f"{forbidden!r} leaked into the daily action counter — RULING 2 "
            f"forbids blending the two actors")


def test_the_new_env_knobs_have_the_documented_defaults():
    src = _src()
    assert ("REGRAB_LEDGER=${QFLIX_CANARY_REGRAB_LEDGER:-"
            "$HOME/.opt/maint/arr-regrab-ledger.json}") in src
    assert "REGRAB_PARK_FAIL=${QFLIX_CANARY_REGRAB_PARK_FAIL:-10}" in src


def test_a5_no_new_timer_script_or_monitor_was_added():
    """RULING 2 / AC-12: the signal rides the existing surface."""
    systemd = REPO / "scripts" / "maint" / "systemd"
    assert not list(systemd.glob("*regrab*"))
    assert not list((REPO / "scripts" / "canaries").glob("*regrab*"))
    jobs = (REPO / "manifest" / "jobs.yaml").read_text(encoding="utf-8")
    assert "regrab" not in jobs
    apps = (REPO / "manifest" / "apps.yaml").read_text(encoding="utf-8")
    # Mentioned ONLY inside the existing unstick-rate comment block.
    assert "Canary Regrab" not in apps
    assert apps.count("kuma_monitor: \"Canary Unstick Rate\"") == 1
