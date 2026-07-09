"""Tests for scripts/canaries/newsletter-digest-stale.sh.

Two jobs:
  1. AGREEMENT TEST — prove the canary's freshness verdict is byte-identical
     to qflix_newsletter.changelog._is_fresh across a boundary table (fresh,
     -1.0d / +4.0d edges, stale, unparseable). This is the guarantee the
     spec's "reimplement inline with a mandatory agreement test" clause asks
     for: the canary invokes its OWN deployed `__is_fresh__` self-test hook
     (not a re-derivation of the logic) via subprocess, so a real drift
     between the two implementations fails this test.
  2. EXECUTABLE PROOF — run the actual bash script end-to-end (real
     subprocess, real python3, real file reads) against fixtures and assert
     exit code + STAGE label / stdout per scenario: FAIL-stale (reproducing
     the real 2026-06-29-on-2026-07-06 miss), PASS-fresh, digest-missing
     (absent fixture + real 404), digest-malformed (bad JSON, missing
     week_of, non-string week_of), digest-empty (blank html),
     out-of-window (weekend, no force), force-window override, and
     transient-transport -> inconclusive.

Requires: bash + python3 on PATH. Skips cleanly if either is absent (e.g. a
bare Windows CI runner with no Git Bash) rather than failing the whole suite.
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from qflix_newsletter import changelog as C

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canaries" / "newsletter-digest-stale.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="newsletter-digest-stale.sh needs bash + python3 on PATH",
)


def _run(args, env=None, timeout=30):
    full_env = {}
    import os
    full_env.update(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 1. Agreement test: canary __is_fresh__ vs changelog._is_fresh
# ---------------------------------------------------------------------------

FRESHNESS_TABLE = [
    # (week_of, now_iso, label)
    ("2026-06-29", "2026-06-29T15:00:00Z", "same-day"),
    ("2026-06-29", "2026-06-28T00:00:00Z", "exactly -1.0d edge (fresh)"),
    ("2026-06-29", "2026-06-27T23:59:59Z", "just past -1.0d edge (stale)"),
    ("2026-06-29", "2026-07-03T00:00:00Z", "exactly +4.0d edge (fresh)"),
    ("2026-06-29", "2026-07-03T00:00:01Z", "just past +4.0d edge (stale)"),
    ("2026-06-29", "2026-07-06T15:05:00Z", "real 07-06 miss (stale)"),
    ("2026-06-22", "2026-06-29T14:01:00Z", "prior-week blurb at next send (stale)"),
    ("bad-date", "2026-07-06T15:05:00Z", "unparseable week_of (stale)"),
    ("", "2026-07-06T15:05:00Z", "empty week_of (stale)"),
    ("2026-07-06", "2026-07-06T14:01:56Z", "same-day fresh publish"),
]


@pytest.mark.parametrize("week_of,now_iso,label", FRESHNESS_TABLE, ids=[t[2] for t in FRESHNESS_TABLE])
def test_agreement_with_changelog_is_fresh(week_of, now_iso, label):
    now = _dt.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    expected = "fresh" if C._is_fresh(week_of, now) else "stale"

    result = _run(["__is_fresh__", week_of, now_iso])
    assert result.returncode == 0, result.stderr
    got = result.stdout.strip()

    assert got == expected, (
        f"DRIFT between canary and changelog._is_fresh for week_of={week_of!r} "
        f"now={now_iso!r} ({label}): changelog says {expected}, canary says {got}"
    )


# ---------------------------------------------------------------------------
# 2. Executable proof — end-to-end script runs
# ---------------------------------------------------------------------------

MONDAY_SEND_NOW = "2026-07-06T15:05:00Z"  # matches Section-0's real miss
WEEKEND_NOW = "2026-07-05T12:00:00Z"


def _write(tmp_path, name, obj_or_text):
    p = tmp_path / name
    if isinstance(obj_or_text, str):
        p.write_text(obj_or_text, encoding="utf-8")
    else:
        p.write_text(json.dumps(obj_or_text), encoding="utf-8")
    return p


def test_fails_on_the_real_stale_branch_state(tmp_path):
    """Reproduces Section 0 verbatim: origin/newsletter-digest's newest
    commit is week_of=2026-06-29; evaluated at the real 2026-07-06 Monday
    send moment, that is 7.6 days stale — the exact silent-fallback
    condition. MUST fail loudly."""
    fixture = _write(tmp_path, "stale.json", {
        "week_of": "2026-06-29",
        "generated_at": "2026-06-29T14:01:56Z",
        "since": "2026-06-22T14:01:56Z",
        "html": "<p>We shipped some fixes.</p>",
    })
    result = _run([], env={
        "QFLIX_DIGEST_CANARY_NOW": MONDAY_SEND_NOW,
        "QFLIX_DIGEST_CANARY_URL": str(fixture),
    })
    assert result.returncode != 0
    assert "STAGE=digest-stale" in result.stderr
    assert "week_of=2026-06-29" in result.stderr


def test_passes_on_a_fresh_week_fixture(tmp_path):
    fixture = _write(tmp_path, "fresh.json", {
        "week_of": "2026-07-06",
        "generated_at": "2026-07-06T14:01:56Z",
        "html": "<p>Great week!</p>",
    })
    result = _run([], env={
        "QFLIX_DIGEST_CANARY_NOW": MONDAY_SEND_NOW,
        "QFLIX_DIGEST_CANARY_URL": str(fixture),
    })
    assert result.returncode == 0
    assert "digest-fresh" in result.stdout
    assert "week_of=2026-07-06" in result.stdout


def test_out_of_window_weekend_is_a_silent_pass(tmp_path):
    """No fixture given at all — if the window gate didn't short-circuit
    before the fetch, this would try (and fail on) the default GitHub URL
    resolution path in a way unrelated to this test's intent. It should
    never get there."""
    result = _run([], env={"QFLIX_DIGEST_CANARY_NOW": WEEKEND_NOW})
    assert result.returncode == 0
    assert "not-in-eval-window" in result.stdout


def test_force_window_overrides_weekday_check(tmp_path):
    fixture = _write(tmp_path, "fresh.json", {"week_of": "2026-07-06", "html": "<p>x</p>"})
    result = _run([], env={
        "QFLIX_DIGEST_CANARY_NOW": WEEKEND_NOW,
        "QFLIX_DIGEST_CANARY_FORCE_WINDOW": "1",
        "QFLIX_DIGEST_CANARY_URL": str(fixture),
    })
    assert result.returncode == 0
    assert "digest-fresh" in result.stdout


def test_digest_missing_on_absent_fixture(tmp_path):
    result = _run([], env={
        "QFLIX_DIGEST_CANARY_NOW": MONDAY_SEND_NOW,
        "QFLIX_DIGEST_CANARY_URL": str(tmp_path / "does-not-exist.json"),
    })
    assert result.returncode != 0
    assert "STAGE=digest-missing" in result.stderr


def test_digest_missing_on_real_404():
    result = _run([], env={
        "QFLIX_DIGEST_CANARY_NOW": MONDAY_SEND_NOW,
        "QFLIX_DIGEST_CANARY_URL": (
            "https://raw.githubusercontent.com/Quadstronaut/QFlix/"
            "newsletter-digest/digest/does-not-exist.json"
        ),
    }, timeout=60)
    assert result.returncode != 0
    assert "STAGE=digest-missing" in result.stderr


def test_digest_malformed_on_bad_json(tmp_path):
    fixture = _write(tmp_path, "bad.json", "not json {{{")
    result = _run([], env={
        "QFLIX_DIGEST_CANARY_NOW": MONDAY_SEND_NOW,
        "QFLIX_DIGEST_CANARY_URL": str(fixture),
    })
    assert result.returncode != 0
    assert "STAGE=digest-malformed" in result.stderr


def test_digest_malformed_on_missing_week_of_key(tmp_path):
    fixture = _write(tmp_path, "nokey.json", {"html": "<p>x</p>"})
    result = _run([], env={
        "QFLIX_DIGEST_CANARY_NOW": MONDAY_SEND_NOW,
        "QFLIX_DIGEST_CANARY_URL": str(fixture),
    })
    assert result.returncode != 0
    assert "STAGE=digest-malformed" in result.stderr
    assert "missing-week_of" in result.stderr


def test_digest_malformed_on_non_string_week_of(tmp_path):
    fixture = _write(tmp_path, "badtype.json", {"week_of": 20260706, "html": "<p>x</p>"})
    result = _run([], env={
        "QFLIX_DIGEST_CANARY_NOW": MONDAY_SEND_NOW,
        "QFLIX_DIGEST_CANARY_URL": str(fixture),
    })
    assert result.returncode != 0
    assert "STAGE=digest-malformed" in result.stderr


def test_digest_empty_on_blank_html(tmp_path):
    fixture = _write(tmp_path, "empty.json", {"week_of": "2026-07-06", "html": "   "})
    result = _run([], env={
        "QFLIX_DIGEST_CANARY_NOW": MONDAY_SEND_NOW,
        "QFLIX_DIGEST_CANARY_URL": str(fixture),
    })
    assert result.returncode != 0
    assert "STAGE=digest-empty" in result.stderr


def test_transient_transport_failure_is_inconclusive_not_a_fail():
    """3 retries against a refused connection (not a definitive 404) must
    NOT be conflated with a real digest failure — exit 0 / UP with a note.
    Short timeout/sleep overrides keep this fast without touching the
    mandatory 3-var contract."""
    result = _run([], env={
        "QFLIX_DIGEST_CANARY_NOW": MONDAY_SEND_NOW,
        "QFLIX_DIGEST_CANARY_URL": "https://127.0.0.1:1/nope",
        "QFLIX_DIGEST_CANARY_TIMEOUT_S": "2",
        "QFLIX_DIGEST_CANARY_RETRY_SLEEP_S": "0",
    }, timeout=30)
    assert result.returncode == 0
    assert "digest-check-inconclusive" in result.stdout
