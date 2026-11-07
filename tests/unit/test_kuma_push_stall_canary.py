"""kuma-push-stall canary (QFLX-104), executed against a fixture ledger.

The embedded python body is lifted out of the shell wrapper and EXECUTED with a
real tmp state dir and a pinned clock, because grepping it proves nothing about
behaviour. Ledger contract (written by the pusher, only read here): one JSON
object per line, {"event":"open","at":...} and
{"event":"close","at":...,"opened_at":...,"seconds":...,"deferred":N}.
"""
from __future__ import annotations

import calendar
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts" / "canaries" / "kuma-push-stall.sh"
NOW = calendar.timegm((2026, 11, 7, 3, 0, 0))   # fixed clock, epoch seconds


def _body() -> str:
    src = CANARY.read_text(encoding="utf-8")
    opener = "python3 - <<PYEOF\n"
    start = src.index(opener) + len(opener)
    body = src[start:src.index("\nPYEOF", start)]
    assert "kuma-stall-over-budget" in body, "wrong block"
    return body


def _iso(offset_s: float) -> str:
    """ISO stamp `offset_s` seconds relative to the pinned NOW (negative = past)."""
    t = NOW + offset_s
    whole = int(t // 1)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + ".%dZ" % int((t - whole) * 10)


def _open(ago):
    return {"event": "open", "at": _iso(-ago)}


def _close(ago, seconds):
    return {"event": "close", "at": _iso(-ago), "opened_at": _iso(-ago - seconds),
            "seconds": float(seconds), "deferred": 31}


def _run(tmp_path, lines=None, *, make_dir=True, env_extra=None):
    state = tmp_path / "state"
    if make_dir:
        state.mkdir()
    if lines is not None:
        text = "\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n"
        (state / "kuma-push-stalls.jsonl").write_text(text, encoding="utf-8")
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("QFLIX_CANARY_KUMA_STALL") or k == "MANITOBA_STATE_DIR":
            del env[k]
    env.update({"QFLIX_CANARY_KUMA_STALL_DIR": str(state),
                "QFLIX_CANARY_KUMA_STALL_NOW": str(NOW)})
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", _body()], env=env,
                          capture_output=True, text=True, timeout=60)


def test_body_survives_the_remote_quoting():
    """Runs inside sshm '...' and an UNQUOTED heredoc: an apostrophe ends the
    sshm string, and $ or a backslash gets expanded on the box."""
    body = _body()
    assert "'" not in body
    assert "$" not in body
    assert "\\" not in body
    assert "`" not in body


def test_absent_ledger_with_readable_dir_is_green(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "stalls24h=0" in r.stdout and "ledger=absent" in r.stdout


@pytest.mark.skipif(os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX modes and a non-root uid")
def test_unreadable_state_dir_is_red_unreachable(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0)
    try:
        r = _run(tmp_path, make_dir=False)
    finally:
        state.chmod(stat.S_IRWXU)
    assert r.returncode == 1
    assert "STAGE=kuma-stall-unreachable" in r.stderr


def test_missing_state_dir_is_red_unreachable_not_green(tmp_path):
    """Absent ledger is benign only if we could have SEEN it."""
    r = _run(tmp_path, make_dir=False)
    assert r.returncode == 1
    assert "STAGE=kuma-stall-unreachable" in r.stderr
    assert "PASS" not in r.stdout


def test_75s_stall_is_green_and_shows_the_trend(tmp_path):
    r = _run(tmp_path, [_open(3600 + 75), _close(3600, 75)])
    assert r.returncode == 0, r.stderr
    assert "stalls24h=1 max=75s budget=120s" in r.stdout
    assert "bad_lines=0" in r.stdout


def test_130s_stall_is_red_over_budget(tmp_path):
    r = _run(tmp_path, [_open(3600 + 130), _close(3600, 130)])
    assert r.returncode == 1
    assert "STAGE=kuma-stall-over-budget" in r.stderr
    assert "stalls24h=1" in r.stderr and "max=130s" in r.stderr


def test_exactly_the_budget_is_red(tmp_path):
    r = _run(tmp_path, [_open(500), _close(380, 120)])
    assert r.returncode == 1
    assert "STAGE=kuma-stall-over-budget" in r.stderr


def test_budget_is_overridable_by_env(tmp_path):
    lines = [_open(3600 + 130), _close(3600, 130)]
    r = _run(tmp_path, lines, env_extra={"QFLIX_CANARY_KUMA_STALL_BUDGET_S": "300"})
    assert r.returncode == 0, r.stderr
    assert "budget=300s" in r.stdout


@pytest.mark.parametrize("bad", ["abc", "0", "-5"])
def test_bad_budget_knob_cannot_assert(tmp_path, bad):
    """A typo in a unit Environment= line must not silently disable the check."""
    r = _run(tmp_path, [], env_extra={"QFLIX_CANARY_KUMA_STALL_BUDGET_S": bad})
    assert r.returncode == 2
    assert "STAGE=kuma-stall-bad-config" in r.stderr


def test_ongoing_open_older_than_budget_is_red(tmp_path):
    r = _run(tmp_path, [_open(300)])
    assert r.returncode == 1
    assert "STAGE=kuma-stall-ongoing" in r.stderr


def test_fresh_open_within_budget_is_still_green(tmp_path):
    r = _run(tmp_path, [_open(30)])
    assert r.returncode == 0, r.stderr


def test_open_then_close_is_not_ongoing(tmp_path):
    r = _run(tmp_path, [_open(400), _close(300, 70)])
    assert r.returncode == 0, r.stderr
    assert "stalls24h=1 max=70s" in r.stdout


def test_a_later_open_after_an_old_close_is_ongoing(tmp_path):
    r = _run(tmp_path, [_open(5000), _close(4900, 70), _open(200)])
    assert r.returncode == 1
    assert "STAGE=kuma-stall-ongoing" in r.stderr


def test_events_older_than_24h_are_ignored(tmp_path):
    day = 24 * 3600
    r = _run(tmp_path, [_open(day + 4000), _close(day + 3000, 500), _open(day + 2000)])
    assert r.returncode == 0, r.stderr
    assert "stalls24h=0" in r.stdout


def test_malformed_lines_are_counted_not_crashed_on(tmp_path):
    lines = ["not json at all", "[1,2]", '{"event":"close","at":"garbage","seconds":5}',
             '{"event":"close","at":"%s","seconds":"x"}' % _iso(-100),
             '{"event":"weird","at":"%s"}' % _iso(-100),
             _open(3600 + 75), _close(3600, 75)]
    r = _run(tmp_path, lines)
    assert r.returncode == 0, r.stderr
    assert "bad_lines=5" in r.stdout
    assert "stalls24h=1 max=75s" in r.stdout


def test_bad_lines_never_hide_a_real_red(tmp_path):
    r = _run(tmp_path, ["{{{", _open(500), _close(300, 200)])
    assert r.returncode == 1
    assert "bad_lines=1" in r.stderr


def test_max_across_several_stalls(tmp_path):
    r = _run(tmp_path, [_open(9000), _close(8900, 60), _open(5000), _close(4900, 90)])
    assert r.returncode == 0, r.stderr
    assert "stalls24h=2 max=90s" in r.stdout
