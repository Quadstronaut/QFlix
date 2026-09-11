"""Tests for scripts/canaries/prowlarr-proxy-link-fatal.sh.

There is no shellcheck or shell-lint gate in this repo's CI, so a pytest test
that actually runs `bash <script>` is the ONLY real gate on this canary. Every
case below drives the REAL artifact end to end: real bash, real grep, real
`date -d`, real files on disk. Nothing is mocked except the clock, which is
injected through the documented PROWLARR_FATAL_NOW override so the window
arithmetic is deterministic rather than wall-clock dependent.

Six jobs:

  1. THE FAULT IT WAS BUILT FOR, replayed verbatim. REAL_EPISODE carries the
     eight "|Fatal|ProwlarrErrorPipeline|Request Failed. GET /27/api" lines
     exactly as they appear in ~/.apps/prowlarr/logs/prowlarr.txt on the box,
     and `test_the_real_episode_reds` asserts the canary reds on them. A canary
     whose fixtures are invented can pass while matching nothing real.

  2. THE ROTATION STRADDLE. prowlarr.txt rotates on size roughly every 17h and
     the window is 6h, so a third of the time the evidence is in prowlarr.0.txt.
     `test_fatal_only_in_previous_rotation_still_reds` is the reason both files
     are concatenated; drop the `cat "$PREV"` and this is the test that fails.

  3. THE WINDOW IS A WINDOW. A fatal older than WINDOW_H must NOT red, or the
     monitor never clears and the next real episode is invisible inside a
     permanently-red check.

  4. RULE 5: exit 0 / 1 / 2 are three distinct states and "the canary cannot
     tell" is never collapsed into "clean". Missing log, stale log and an
     unparseable clock are all exit 2 with their own STAGE, and none of them
     may print a zero count as health.

  5. THE STALENESS BUDGET, pinned to the measured gap distribution. The default
     45 min must clear the largest real inter-line gap (11.8 min measured over
     a full rotation) and must red beyond itself.

  6. THE PREDICATE IS THE LEVEL TOKEN. An |Error| or |Warn| line must not red
     (Prowlarr logs those routinely and they are not 500s to an *arr), and a
     Fatal from a DIFFERENT logger must red (the whole point of matching the
     level rather than the UriFormatException text).

Requires bash + GNU coreutils (`date -d`) on PATH -- the same dependency
scripts/canaries/rea-liveness.sh already has. Skips cleanly if absent.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canaries" / "prowlarr-proxy-link-fatal.sh"
SYSTEMD_DIR = REPO_ROOT / "scripts" / "maint" / "systemd"
UNIT_STEM = "manitoba-maint-canary-prowlarr-proxy-link-fatal"


def _has_gnu_tools() -> bool:
    if shutil.which("bash") is None:
        return False
    try:
        p = subprocess.run(["bash", "-c", "date -u -d '2026-01-01T00:00:00Z' +%s"],
                           capture_output=True, text=True, timeout=20)
    except Exception:
        return False
    return p.returncode == 0 and p.stdout.strip() == "1767225600"


pytestmark = pytest.mark.skipif(
    not _has_gnu_tools(),
    reason="prowlarr-proxy-link-fatal.sh needs bash + GNU date on PATH",
)

NOW = "2026-09-07 10:00:00"

# The eight lines off the box, 2026-09-06 22:18 -> 2026-09-07 09:56, byte for
# byte. Kept as the primary fixture so a rule that only matches a line the
# author imagined cannot pass.
REAL_EPISODE = [
    "2026-09-06 22:18:19.1|Fatal|ProwlarrErrorPipeline|Request Failed. GET /27/api",
    "2026-09-06 22:33:49.2|Fatal|ProwlarrErrorPipeline|Request Failed. GET /27/api",
    "2026-09-06 22:49:19.1|Fatal|ProwlarrErrorPipeline|Request Failed. GET /27/api",
    "2026-09-06 23:04:49.5|Fatal|ProwlarrErrorPipeline|Request Failed. GET /27/api",
    "2026-09-06 23:35:49.7|Fatal|ProwlarrErrorPipeline|Request Failed. GET /27/api",
    "2026-09-07 00:37:50.1|Fatal|ProwlarrErrorPipeline|Request Failed. GET /27/api",
    "2026-09-07 03:43:52.7|Fatal|ProwlarrErrorPipeline|Request Failed. GET /27/api",
    "2026-09-07 09:56:06.2|Fatal|ProwlarrErrorPipeline|Request Failed. GET /27/api",
]

# Routine traffic, also real shapes: the Info lines Prowlarr emits every few
# minutes are what makes the freshness probe pass.
def _chatter(stamp: str) -> str:
    return (stamp + "|Info|ReleaseSearchService|Searching indexer(s): [nekoBT] "
            "for Term: [] for Season / Episode:[], Offset: 0, Limit: 100, "
            "Categories: [5000, 5070]")


def _logdir(tmp_path: Path, live: list[str], prev: list[str] | None = None) -> Path:
    d = tmp_path / "logs"
    d.mkdir(exist_ok=True)
    (d / "prowlarr.txt").write_text("\n".join(live) + "\n", encoding="utf-8",
                                    newline="\n")
    if prev is not None:
        (d / "prowlarr.0.txt").write_text("\n".join(prev) + "\n", encoding="utf-8",
                                          newline="\n")
    return d


def _default_trail(logdir: Path | str) -> Path:
    """A throwaway trail beside the fixture, so every run has somewhere real to
    write and no test ever depends on the production path."""
    return Path(logdir).parent / "trail-scratch.log"


def _run(logdir: Path | str, *, now: str = NOW, window_h: int | None = None,
         threshold: int | None = None, stale_min: int | None = None,
         trail: Path | None = None):
    env = dict(os.environ)
    env["PROWLARR_FATAL_LOG_DIR"] = str(logdir)
    env["PROWLARR_FATAL_NOW"] = now
    env["PROWLARR_FATAL_SKIP_LOOKUP"] = "1"
    # NEVER os.devnull here. On Windows that is the string "nul", and the
    # canary appends to it from Git Bash, which cheerfully creates a FILE
    # called `nul` in the repo root that git then refuses to index
    # ("short read while indexing nul"). The default trail always lands
    # inside the test's own tmp_path.
    env["PROWLARR_FATAL_TRAIL"] = str(trail if trail else _default_trail(logdir))
    if window_h is not None:
        env["PROWLARR_FATAL_WINDOW_H"] = str(window_h)
    if threshold is not None:
        env["PROWLARR_FATAL_THRESHOLD"] = str(threshold)
    if stale_min is not None:
        env["PROWLARR_FATAL_STALE_BUDGET_MIN"] = str(stale_min)
    return subprocess.run(["bash", str(SCRIPT)], env=env,
                          capture_output=True, text=True, timeout=60)


def _stage(res) -> str | None:
    m = re.search(r"STAGE=([a-z0-9-]+)", res.stderr or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# 1. The real episode
# ---------------------------------------------------------------------------
def test_the_real_episode_reds(tmp_path):
    """The 2026-09-06/07 lines, verbatim, must red. The last one is 3m54s
    before NOW, so at least one is always inside any sane window."""
    d = _logdir(tmp_path, REAL_EPISODE + [_chatter("2026-09-07 09:59:00.0")])
    res = _run(d)
    assert res.returncode == 1, res.stderr
    assert _stage(res) == "prowlarr-fatal-500"
    assert "indexers=27" in res.stderr, res.stderr
    # The trailing-space bug: "27," instead of "27" is a real regression that
    # shipped once and reads as a truncated list in the Discord page.
    assert "indexers=27-arrs" in res.stderr, res.stderr


def test_the_message_names_the_count_and_window(tmp_path):
    d = _logdir(tmp_path, REAL_EPISODE + [_chatter("2026-09-07 09:59:00.0")])
    res = _run(d, window_h=24)
    assert res.returncode == 1
    # All eight are inside 24h of 2026-09-07 10:00.
    assert "msg=8-fatal-responses-in-24h" in res.stderr, res.stderr


def test_clean_log_passes(tmp_path):
    d = _logdir(tmp_path, [_chatter("2026-09-07 09:40:00.0"),
                           _chatter("2026-09-07 09:59:00.0")])
    res = _run(d)
    assert res.returncode == 0, res.stderr
    assert "PASS: prowlarr-proxy-link-fatal - 0 fatal in 6h" in res.stdout


# ---------------------------------------------------------------------------
# 2. The rotation straddle
# ---------------------------------------------------------------------------
def test_fatal_only_in_previous_rotation_still_reds(tmp_path):
    """prowlarr.txt rotates ~every 17h against a 6h window, so about a third of
    the time the evidence is in prowlarr.0.txt. Reading only the live file
    silently shortens the window to 'however long ago the rotation was'."""
    d = _logdir(tmp_path,
                live=[_chatter("2026-09-07 09:59:00.0")],
                prev=REAL_EPISODE)
    res = _run(d)
    assert res.returncode == 1, (res.stdout, res.stderr)
    assert _stage(res) == "prowlarr-fatal-500"


def test_absent_previous_rotation_is_not_an_error(tmp_path):
    """A freshly-installed Prowlarr has no prowlarr.0.txt. That must read as
    'nothing there to count', never as a failure to assert."""
    d = _logdir(tmp_path, [_chatter("2026-09-07 09:59:00.0")])
    assert not (d / "prowlarr.0.txt").exists()
    res = _run(d)
    assert res.returncode == 0, res.stderr


# ---------------------------------------------------------------------------
# 3. The window is a window
# ---------------------------------------------------------------------------
def test_fatal_older_than_the_window_does_not_red(tmp_path):
    """Self-clearing is the whole paging design: 6h after the last fatal the
    monitor must go green again, or the next episode is invisible inside a
    permanently red check."""
    d = _logdir(tmp_path, ["2026-09-06 22:18:19.1|Fatal|ProwlarrErrorPipeline|"
                           "Request Failed. GET /27/api",
                           _chatter("2026-09-07 09:59:00.0")])
    res = _run(d, window_h=6)
    assert res.returncode == 0, res.stderr
    assert "0 fatal in 6h" in res.stdout


def test_the_same_fatal_reds_once_the_window_is_widened(tmp_path):
    """Guard-the-guard for the test above: the line IS matchable, so a green
    verdict there is the window working, not the predicate failing."""
    d = _logdir(tmp_path, ["2026-09-06 22:18:19.1|Fatal|ProwlarrErrorPipeline|"
                           "Request Failed. GET /27/api",
                           _chatter("2026-09-07 09:59:00.0")])
    assert _run(d, window_h=6).returncode == 0
    assert _run(d, window_h=24).returncode == 1


# ---------------------------------------------------------------------------
# 4. Three states, never two
# ---------------------------------------------------------------------------
def test_missing_log_is_cannot_assert(tmp_path):
    res = _run(tmp_path / "nowhere")
    assert res.returncode == 2
    assert _stage(res) == "prowlarr-log-missing"
    assert "PASS" not in res.stdout


def test_stale_log_is_cannot_assert(tmp_path):
    """A zero count read out of a log nothing is writing to is an absence of
    evidence, not evidence of health."""
    d = _logdir(tmp_path, [_chatter("2026-09-07 08:00:00.0")])
    res = _run(d)  # 120 min old against a 45 min budget
    assert res.returncode == 2
    assert _stage(res) == "prowlarr-log-stale"
    assert "PASS" not in res.stdout


def test_log_with_no_timestamped_line_is_cannot_assert(tmp_path):
    d = _logdir(tmp_path, ["this is not a prowlarr log line at all"])
    res = _run(d)
    assert res.returncode == 2
    assert _stage(res) == "prowlarr-log-unparseable"


def test_unparseable_clock_override_is_cannot_assert(tmp_path):
    d = _logdir(tmp_path, [_chatter("2026-09-07 09:59:00.0")])
    res = _run(d, now="not-a-date")
    assert res.returncode == 2
    assert _stage(res) == "prowlarr-log-unparseable"


def test_every_non_pass_writes_the_durable_trail(tmp_path):
    """Kuma heartbeats live in a Docker volume the SSH user cannot read, so a
    host-readable trail is the only triage surface that survives."""
    trail = tmp_path / "trail.log"
    d = _logdir(tmp_path, REAL_EPISODE + [_chatter("2026-09-07 09:59:00.0")])
    assert _run(d, trail=trail).returncode == 1
    assert _run(tmp_path / "nowhere", trail=trail).returncode == 2
    body = trail.read_text(encoding="utf-8")
    assert "prowlarr-fatal-500" in body
    assert "prowlarr-log-missing" in body


# ---------------------------------------------------------------------------
# 5. The staleness budget, pinned to the measured distribution
# ---------------------------------------------------------------------------
def test_staleness_budget_clears_the_measured_worst_gap(tmp_path):
    """Measured 2026-09-11 over a full rotation of prowlarr.txt: the largest
    inter-line gap was 11.8 minutes. The 45-minute default is ~4x that. If
    Prowlarr ever goes quieter, this is the test that says so."""
    d = _logdir(tmp_path, [_chatter("2026-09-07 09:48:00.0")])  # 12 min old
    assert _run(d).returncode == 0
    # And the budget is real, not decorative.
    assert _run(d, stale_min=10).returncode == 2


# ---------------------------------------------------------------------------
# 6. The predicate is the LEVEL token
# ---------------------------------------------------------------------------
def test_error_and_warn_lines_do_not_red(tmp_path):
    """Prowlarr logs |Error| routinely (CommandExecutor,
    ServerSideNotificationService, FlareSolverr -- 9 lines over the same 22 days
    that carried 8 Fatals). None of them is a 500 to an *arr."""
    d = _logdir(tmp_path, [
        "2026-09-07 09:50:00.0|Error|CommandExecutor|Error occurred while executing task",
        "2026-09-07 09:51:00.0|Warn|Cardigann|Request for Nyaa failed with status 522.",
        "2026-09-07 09:52:00.0|Error|ServerSideNotificationService|Failed to retrieve notifications",
        _chatter("2026-09-07 09:59:00.0"),
    ])
    res = _run(d)
    assert res.returncode == 0, res.stderr


def test_a_fatal_from_another_logger_still_reds(tmp_path):
    """The predicate is deliberately the level, not the UriFormatException
    text: a clean 22-day zero baseline on |Fatal| is what buys the wider net,
    and the next 500-to-an-*arr may have a different cause entirely."""
    d = _logdir(tmp_path, [
        "2026-09-07 09:50:00.0|Fatal|SomeOtherPipeline|Everything is on fire",
        _chatter("2026-09-07 09:59:00.0"),
    ])
    res = _run(d)
    assert res.returncode == 1
    assert _stage(res) == "prowlarr-fatal-500"
    # No "GET /<id>/api" in the line, so there is no indexer to name. The page
    # must say so rather than render an empty list.
    assert "no-indexer-id-in-line" in res.stderr, res.stderr


def test_threshold_is_honoured(tmp_path):
    d = _logdir(tmp_path, REAL_EPISODE + [_chatter("2026-09-07 09:59:00.0")])
    assert _run(d, window_h=24, threshold=8).returncode == 1
    assert _run(d, window_h=24, threshold=9).returncode == 0


# ---------------------------------------------------------------------------
# Wiring: the unit pair exists and points at this canary
# ---------------------------------------------------------------------------
def test_systemd_units_exist_and_are_consistent():
    svc = (SYSTEMD_DIR / (UNIT_STEM + ".service")).read_text(encoding="utf-8")
    tmr = (SYSTEMD_DIR / (UNIT_STEM + ".timer")).read_text(encoding="utf-8")
    assert "canary push prowlarr-proxy-link-fatal" in svc
    assert "OnCalendar=*:0/30" in tmr
    assert "Persistent=true" in tmr


def test_header_documents_the_defaults_the_code_uses():
    """The three-policy-surface defect, in miniature: a threshold documented in
    the header and a different one in the code is exactly how the
    prowlarr-indexer-health 25-vs-40 split shipped."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "PROWLARR_FATAL_WINDOW_H:-6}" in body
    assert "PROWLARR_FATAL_THRESHOLD:-1}" in body
    assert "PROWLARR_FATAL_STALE_BUDGET_MIN:-45}" in body
    assert "WINDOW IS 6h" in body
    assert "THRESHOLD is 1" in body
    assert "default 45" in body
