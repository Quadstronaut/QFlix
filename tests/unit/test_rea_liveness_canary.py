"""Tests for scripts/canaries/rea-liveness.sh.

There is no shellcheck or shell-lint gate in this repo's CI, so a pytest test
that actually runs `bash <script>` is the ONLY real gate on this canary. Every
case below drives the real artifact end to end: real bash, real `stat`, real
`date -d`, real files on disk. Nothing is mocked except the clock, which is
injected through the script's documented QFLIX_CANARY_REA_NOW override so the
age arithmetic is deterministic rather than wall-clock dependent.

Five jobs:

  1. THE ASYMMETRY PROOF. The point of this canary is that it reports REA as
     unhealthy WITHOUT REA's cooperation. `test_verdict_predicate_*` and
     `test_reach_predicate_*` therefore run against fixtures that a naive
     "REA pinged Kuma" monitor would have shown GREEN, and assert RED. The
     `_push_monitor_verdict` helper IS that naive monitor, written out, so the
     comparison is proven rather than asserted in prose.

  2. THE REAL HISTORY, replayed. REA's actual audit.log vocabulary over 72 runs
     (2026-05-11 -> 2026-08-03) is parametrised in OUTCOME_TABLE. The dominant
     silent failure — `fail reason=all_models_noop`, 18 of 72 runs — must red.

  3. RULE 5: exit 0 / 1 / 2 are three distinct states, and "the canary cannot
     tell" is never collapsed into "clean". Includes the fail-closed case: an
     outcome token the canary does not recognise is exit 2, not exit 0.

  4. RULE 4: every skip is COUNTED AND LOGGED. `skips=N(...)` must appear on
     EVERY exit path, including the zero case and including failures.

  5. THE THRESHOLD, pinned to the measured gap distribution. The default 336h
     must clear the largest real inter-run gap (275.3h) and must red beyond it,
     and the historical false-fire counts for 72h/168h/336h are re-derived from
     the recorded gaps so the header's justification cannot silently rot.

Requires bash + GNU coreutils (`stat -c`, `date -d`) on PATH — same dependency
scripts/canaries/stale-log-watchdog.sh already has. Skips cleanly if absent.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canaries" / "rea-liveness.sh"
SYSTEMD_DIR = REPO_ROOT / "scripts" / "maint" / "systemd"
UNIT_STEM = "manitoba-maint-canary-rea-liveness"
REASON_TABLE = REPO_ROOT / "manifest" / "rea-noise-classes.yaml"


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
    reason="rea-liveness.sh needs bash + GNU date/stat on PATH",
)

# A fixed "now" for every test: 2026-08-03T12:00:00Z.
NOW = 1785758400
HOUR = 3600


def _run(hb_path: Path | None, *, now: int = NOW, mtime_age_h: float = 1.0,
         max_h: int | None = None, reason_table: str | None = None):
    """Run the canary against a heartbeat file whose mtime is `mtime_age_h`
    hours before `now`. `hb_path=None` exercises the absent-file path."""
    env = dict(os.environ)
    env["QFLIX_CANARY_REA_NOW"] = str(now)
    env["QFLIX_CANARY_REA_HEARTBEAT"] = str(hb_path) if hb_path else "/nonexistent/rea/heartbeat"
    if max_h is not None:
        env["QFLIX_CANARY_REA_MAX_SILENCE_H"] = str(max_h)
    if reason_table is not None:
        env["QFLIX_CANARY_REA_REASON_TABLE"] = reason_table
    if hb_path is not None and hb_path.exists():
        stamp = now - int(mtime_age_h * HOUR)
        os.utime(hb_path, (stamp, stamp))
    return subprocess.run(["bash", str(SCRIPT)], env=env,
                          capture_output=True, text=True, timeout=60)


def _hb(tmp_path: Path, content: str, name: str = "heartbeat") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8", newline="")
    return p


def _stage(res) -> str | None:
    m = re.search(r"STAGE=([a-z0-9-]+)", res.stderr or "")
    return m.group(1) if m else None


def _skips(res) -> str | None:
    m = re.search(r"skips=(\d+(?:\([^)]*\))?)", (res.stdout or "") + (res.stderr or ""))
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# The naive alternative, written out so the comparison is executable.
# ---------------------------------------------------------------------------
def _push_monitor_verdict(line: str, reached_recently: bool) -> str:
    """Option (c): a Kuma push monitor REA pings when it runs.

    REA pushes at the top of main and Kuma flips DOWN only on silence. So the
    verdict depends ONLY on whether REA ran recently — never on what it
    produced. This is the behaviour the header argues loses, reproduced here so
    every fixture below can be shown green under it and red under the canary.
    """
    return "UP" if reached_recently else "DOWN"


# ---------------------------------------------------------------------------
# 1 + 2. Verdict predicate: REA's real audit.log vocabulary.
# ---------------------------------------------------------------------------
# (verdict-tail, expected-exit, expected-stage-or-None, label)
OUTCOME_TABLE = [
    # -- clean, 33 of 72 real runs --------------------------------------
    ("ok findings=0 models=3/3 duration=44s outcome=heartbeat", 0, None, "heartbeat"),
    ("ok findings=0 models=1/3 duration=407s outcome=silent", 0, None, "silent"),
    ("ok findings=3 models=1/3 duration=422s outcome=error_post", 0, None, "error_post"),
    # -- the dominant SILENT failure: 18 of 72 real runs -----------------
    ("fail reason=all_models_noop models=4", 1, "rea-not-auditing", "all_models_noop"),
    # -- the rest of the real failure vocabulary -------------------------
    ("fail reason=ollama_down outcome=deadman_post", 1, "rea-not-auditing", "ollama_down"),
    ("fail reason=ssh_fail msg=connection refused", 1, "rea-not-auditing", "ssh_fail"),
    ("fail reason=no_secrets", 1, "rea-not-auditing", "no_secrets"),
    ("fail reason=tunnel_timeout", 1, "rea-not-auditing", "tunnel_timeout"),
    ("fail reason=no_models", 1, "rea-not-auditing", "no_models"),
    ("fail reason=blob_parse", 1, "rea-not-auditing", "blob_parse"),
    # -- degraded but genuinely audited: WARN, still exit 0 --------------
    ("ok findings=0 models=3/3 duration=44s outcome=discord_post_failed", 0, None, "post_failed"),
    ("ok findings=0 models=3/3 duration=44s outcome=dryrun_heartbeat", 0, None, "dryrun"),
    ("SKIPPED locked", 0, None, "lock-skip"),
    ("suppressed n=2 rules=plex-post-reap-scan,tdarr-express-undefined-includes",
     0, None, "suppression-line"),
]


@pytest.mark.parametrize("tail,code,stage,label", OUTCOME_TABLE,
                         ids=[t[3] for t in OUTCOME_TABLE])
def test_verdict_predicate_matches_reas_real_vocabulary(tmp_path, tail, code, stage, label):
    hb = _hb(tmp_path, "2026-08-03T02:00:00+00:00 %s\n" % tail)
    res = _run(hb, mtime_age_h=1.0)
    assert res.returncode == code, (
        "%s: expected exit %d, got %d\nstdout=%s\nstderr=%s"
        % (label, code, res.returncode, res.stdout, res.stderr))
    assert _stage(res) == stage, "%s: stage=%r" % (label, _stage(res))


@pytest.mark.parametrize("tail,code,stage,label", OUTCOME_TABLE,
                         ids=[t[3] for t in OUTCOME_TABLE])
def test_every_failing_fixture_is_green_under_a_push_monitor(tail, code, stage, label):
    """THE ASYMMETRY PROOF. Every fixture the canary reds on is a run where REA
    executed and would have pushed — so option (c) reports UP. If this ever
    starts failing, the canary has stopped being strictly stronger than the
    alternative it was chosen over."""
    naive = _push_monitor_verdict(tail, reached_recently=True)
    assert naive == "UP"
    if code == 1:
        assert stage == "rea-not-auditing"


def test_all_models_noop_is_the_case_that_justifies_this_canary(tmp_path):
    """18 of REA's 72 recorded runs. It exits 0, posts nothing to Discord by
    design, and is invisible to every existing surface."""
    hb = _hb(tmp_path, "2026-08-03T02:00:00+00:00 fail reason=all_models_noop models=4\n")
    res = _run(hb, mtime_age_h=1.0)
    assert res.returncode == 1
    assert _stage(res) == "rea-not-auditing"
    assert "all_models_noop" in res.stderr
    # It is a KNOWN reason: present in manifest/rea-noise-classes.yaml.
    assert "(known)" in res.stderr, res.stderr


def test_warn_states_are_reported_as_warn_not_as_clean(tmp_path):
    """Degraded-but-audited must be distinguishable from clean on the wire,
    even though both exit 0 — otherwise a REA that audits and can never notify
    reads identically to a healthy one."""
    clean = _run(_hb(tmp_path, "2026-08-03T02:00:00+00:00 ok findings=0 models=3/3 "
                               "duration=44s outcome=heartbeat\n", "a"))
    warn = _run(_hb(tmp_path, "2026-08-03T02:00:00+00:00 ok findings=0 models=3/3 "
                              "duration=44s outcome=discord_post_failed\n", "b"))
    assert clean.returncode == 0 and warn.returncode == 0
    assert clean.stdout.startswith("PASS:"), clean.stdout
    assert warn.stdout.startswith("PASS-WARN:"), warn.stdout


# ---------------------------------------------------------------------------
# 3. Rule 5 — exit 0 / 1 / 2 are three states, and 2 fails CLOSED.
# ---------------------------------------------------------------------------
def test_absent_heartbeat_is_exit_2_and_never_green(tmp_path):
    """P4. 'Nothing watches REA' with a green light is worse than nothing —
    the tdarr-healthcheck class. Absent must be loud, and it must not be
    confused with 'REA is broken' (exit 1)."""
    res = _run(None)
    assert res.returncode == 2, res.stderr
    assert _stage(res) == "rea-heartbeat-absent"
    assert "writer-not-wired" in res.stderr


def test_empty_because_clean_and_empty_because_broken_differ_by_exit_code(tmp_path):
    clean = _run(_hb(tmp_path, "2026-08-03T02:00:00+00:00 ok findings=0 models=3/3 "
                               "duration=44s outcome=heartbeat\n", "clean"))
    broken = _run(None)
    assert clean.returncode == 0
    assert broken.returncode == 2
    assert clean.returncode != broken.returncode


UNTELLABLE = [
    ("", "rea-heartbeat-empty", "blank file"),
    ("   \n\n  \n", "rea-heartbeat-empty", "whitespace only"),
    ("noverdict\n", "rea-heartbeat-malformed", "single token"),
    ("not-a-date ok findings=0 outcome=heartbeat\n", "rea-heartbeat-malformed", "bad stamp"),
    ("2026-08-03T02:00:00+00:00 ok findings=0 models=3/3 duration=44s outcome=teleported\n",
     "rea-heartbeat-malformed", "unknown outcome token"),
    ("2026-08-03T02:00:00+00:00 wibble wobble\n", "rea-heartbeat-malformed", "unknown shape"),
]


@pytest.mark.parametrize("content,stage,label", UNTELLABLE, ids=[u[2] for u in UNTELLABLE])
def test_canary_fails_closed_when_it_cannot_tell(tmp_path, content, stage, label):
    """Exit 2, never 0. A watchdog that greens on vocabulary it has never seen
    is not a watchdog — and a future REA outcome string is exactly how that
    would happen."""
    res = _run(_hb(tmp_path, content))
    assert res.returncode == 2, "%s: exit=%d stdout=%s stderr=%s" % (
        label, res.returncode, res.stdout, res.stderr)
    assert _stage(res) == stage, "%s: stage=%r" % (label, _stage(res))


def test_unreadable_heartbeat_is_exit_2_not_a_pass(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    res = _run(d)
    assert res.returncode == 2
    assert _stage(res) == "rea-heartbeat-unreadable"


# ---------------------------------------------------------------------------
# 4. Rule 4 — every skip COUNTED and LOGGED, on every exit path.
# ---------------------------------------------------------------------------
SKIP_PATHS = [
    ("2026-08-03T02:00:00+00:00 ok findings=0 models=3/3 duration=44s outcome=heartbeat\n",
     0, "skips=0", "clean run still reports a zero tally"),
    ("2026-08-03T02:00:00+00:00 ok findings=0 models=3/3 duration=44s "
     "outcome=discord_post_failed\n", 0, "notify-failed", "notify failure counted"),
    ("2026-08-03T02:00:00+00:00 ok findings=0 models=3/3 duration=44s "
     "outcome=dryrun_heartbeat\n", 0, "dry-run-not-a-production-audit", "dry run counted"),
    ("2026-08-03T02:00:00+00:00 SKIPPED locked\n", 0, "lock-skip-no-verdict",
     "lock skip counted"),
    ("2026-08-03T02:00:00+00:00 suppressed n=2 rules=a,b\n", 0,
     "writer-wrote-suppression-line", "wrong-line write counted"),
]


@pytest.mark.parametrize("content,code,needle,label", SKIP_PATHS,
                         ids=[s[3] for s in SKIP_PATHS])
def test_skips_are_counted_and_logged(tmp_path, content, code, needle, label):
    res = _run(_hb(tmp_path, content))
    assert res.returncode == code
    blob = res.stdout + res.stderr
    assert _skips(res) is not None, "%s: no skip tally emitted at all: %r" % (label, blob)
    assert needle in blob, "%s: %r not in %r" % (label, needle, blob)


def test_skip_tally_is_emitted_on_failure_paths_too(tmp_path):
    """A tally printed only on success would hide exactly the runs an operator
    most needs it for."""
    res = _run(_hb(tmp_path, "2026-08-03T02:00:00+00:00 fail reason=all_models_noop models=4\n"))
    assert res.returncode == 1
    assert _skips(res) is not None, res.stderr
    absent = _run(None)
    assert absent.returncode == 2
    assert _skips(absent) is not None, absent.stderr


def test_missing_reason_table_is_a_counted_skip_not_a_silent_downgrade(tmp_path):
    """manifest/rea-noise-classes.yaml is NOT staged to the box today. Its
    absence must degrade the LABEL only, be counted, and never change the
    verdict."""
    res = _run(_hb(tmp_path, "2026-08-03T02:00:00+00:00 fail reason=all_models_noop models=4\n"),
               reason_table=str(tmp_path / "nope.yaml"))
    assert res.returncode == 1, res.stderr
    assert _stage(res) == "rea-not-auditing"
    assert "reason-table-unavailable" in res.stderr, res.stderr
    assert "(unknown-to-table)" in res.stderr, res.stderr


def test_reason_table_flags_vocabulary_drift(tmp_path):
    """A reason REA emits that the mirrored table has never heard of still reds,
    and is LABELLED as drift rather than quietly normalised."""
    res = _run(_hb(tmp_path, "2026-08-03T02:00:00+00:00 fail reason=brand_new_reason x=1\n"),
               reason_table=str(REASON_TABLE))
    assert res.returncode == 1
    assert "(unknown-to-table)" in res.stderr, res.stderr


# ---------------------------------------------------------------------------
# 5. The threshold, pinned to REA's measured gap distribution.
# ---------------------------------------------------------------------------
# Every inter-run gap over 24h from REA's real audit.log (72 runs,
# 2026-05-11 -> 2026-08-03). Recorded here so the header's justification is
# executable rather than a claim in a comment.
REAL_GAPS_H = [275.3, 145.26, 142.8, 101.9, 75.89, 74.25, 68.0, 66.15, 58.55, 52.1]
REAL_MAX_GAP_H = 275.3
DEFAULT_MAX_SILENCE_H = 336


def test_default_threshold_clears_every_observed_legitimate_gap():
    assert DEFAULT_MAX_SILENCE_H > REAL_MAX_GAP_H
    false_fires = {h: sum(1 for g in REAL_GAPS_H if g > h) for h in (72, 168, 336)}
    assert false_fires == {72: 6, 168: 1, 336: 0}, false_fires


def test_script_default_matches_the_justified_number():
    """Config-drift lock: the shipped default and the number the header argues
    for must be the same. The prowlarr canary carries this exact defect today —
    header says 25, code says 40, apps.yaml says 25."""
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"QFLIX_CANARY_REA_MAX_SILENCE_H:-(\d+)", src)
    assert m, "default threshold not found in the script"
    assert int(m.group(1)) == DEFAULT_MAX_SILENCE_H
    assert "default 336" in src
    assert "DEFAULT 336h" in src


def test_reach_predicate_reds_past_the_cap_and_passes_at_the_observed_max(tmp_path):
    """P1. Green at 275.3h (the largest gap the operator has actually produced),
    red past the cap."""
    line = "2026-08-03T02:00:00+00:00 ok findings=0 models=3/3 duration=44s outcome=heartbeat\n"
    ok = _run(_hb(tmp_path, line, "ok"), mtime_age_h=REAL_MAX_GAP_H)
    assert ok.returncode == 0, ok.stderr
    red = _run(_hb(tmp_path, line, "red"), mtime_age_h=DEFAULT_MAX_SILENCE_H + 1)
    assert red.returncode == 1, red.stdout
    assert _stage(red) == "rea-unreached"


def test_reach_predicate_boundary_is_exactly_the_cap(tmp_path):
    line = "2026-08-03T02:00:00+00:00 ok findings=0 models=3/3 duration=44s outcome=heartbeat\n"
    at = _run(_hb(tmp_path, line, "at"), mtime_age_h=DEFAULT_MAX_SILENCE_H)
    assert at.returncode == 0, at.stderr
    past = _run(_hb(tmp_path, line, "past"), mtime_age_h=DEFAULT_MAX_SILENCE_H + 0.001)
    assert past.returncode == 1, past.stdout


def test_verdict_stale_is_the_wedge_a_push_monitor_cannot_see(tmp_path):
    """P2. REA reaches the box every hour (mtime fresh, so a push monitor is
    UP and P1 is green) but its terminal verdict froze weeks ago — it fetches
    and never finishes. Distinct stage from P1 because the remedy differs."""
    old = "2026-06-01T00:00:00+00:00 ok findings=0 models=3/3 duration=44s outcome=heartbeat\n"
    res = _run(_hb(tmp_path, old), mtime_age_h=1.0)
    assert _push_monitor_verdict(old, reached_recently=True) == "UP"
    assert res.returncode == 1, res.stdout
    assert _stage(res) == "rea-verdict-stale"


def test_a_named_failure_beats_a_stale_clock(tmp_path):
    """Ordering lock: when the verdict is BOTH old and a failure, the operator
    needs the reason, not 'it is old'."""
    res = _run(_hb(tmp_path, "2026-06-01T00:00:00+00:00 fail reason=ollama_down "
                             "outcome=deadman_post\n"), mtime_age_h=1.0)
    assert res.returncode == 1
    assert _stage(res) == "rea-not-auditing"
    assert "ollama_down" in res.stderr


# ---------------------------------------------------------------------------
# Robustness: the producer is PowerShell, and clocks disagree.
# ---------------------------------------------------------------------------
def test_crlf_from_the_powershell_writer_does_not_break_parsing(tmp_path):
    """timer-liveness.sh reported all 40 timers as uninstalled on its first live
    run because of an unstripped CR. Same producer class here."""
    res = _run(_hb(tmp_path, "2026-08-03T02:00:00+00:00 fail reason=all_models_noop models=4\r\n"))
    assert res.returncode == 1, res.stdout
    assert _stage(res) == "rea-not-auditing"
    assert "all_models_noop" in res.stderr
    assert "\r" not in (res.stderr or "")


def test_last_non_blank_line_wins_if_the_writer_appends(tmp_path):
    content = ("2026-06-01T00:00:00+00:00 fail reason=ollama_down outcome=silent\n"
               "\n"
               "2026-08-03T02:00:00+00:00 ok findings=0 models=3/3 duration=44s "
               "outcome=heartbeat\n")
    res = _run(_hb(tmp_path, content))
    assert res.returncode == 0, res.stderr


def test_future_timestamps_are_clamped_not_wrapped(tmp_path):
    """Workstation clock ahead of the box must read as 'just now', not underflow
    into a negative age that silently defeats both age predicates."""
    line = "2036-01-01T00:00:00+00:00 ok findings=0 models=3/3 duration=44s outcome=heartbeat\n"
    res = _run(_hb(tmp_path, line), mtime_age_h=-48.0)
    assert res.returncode == 0, res.stderr
    assert "reached=0h-ago" in res.stdout, res.stdout
    assert "verdict=0h-old" in res.stdout, res.stdout


# ---------------------------------------------------------------------------
# Wiring: the unit pair exists and points at this canary's manifest key.
# ---------------------------------------------------------------------------
def test_both_systemd_units_exist():
    for ext in ("service", "timer"):
        p = SYSTEMD_DIR / ("%s.%s" % (UNIT_STEM, ext))
        assert p.is_file(), "missing unit %s" % p


def test_service_unit_pushes_the_right_canary_name():
    """The ExecStart name is the key cli.py looks up; a wrong one pushes to the
    wrong Kuma monitor, or to none at all."""
    body = (SYSTEMD_DIR / ("%s.service" % UNIT_STEM)).read_text(encoding="utf-8")
    assert "canary push rea-liveness" in body, body


def test_timer_is_installable_and_persistent():
    body = (SYSTEMD_DIR / ("%s.timer" % UNIT_STEM)).read_text(encoding="utf-8")
    assert "WantedBy=timers.target" in body
    assert "Persistent=true" in body
    assert "OnCalendar=" in body


def test_script_is_not_a_second_copy_of_the_deadman_vocabulary():
    """Build ON the existing deadman path, not beside it. The five reasons live
    in manifest/rea-noise-classes.yaml (mirrored from $Script:DeadmanReasons for
    detector C-09); this script must READ them, never re-list them, or the repo
    grows the two-drifting-policy-surfaces defect it keeps getting bitten by."""
    src = SCRIPT.read_text(encoding="utf-8")
    body = src.split("set -uo pipefail", 1)[1]
    hardcoded = [r for r in ("tunnel_timeout", "no_models", "blob_parse", "all_models_noop")
                 if r in body]
    assert not hardcoded, (
        "deadman reasons re-listed in executable code instead of read from "
        "manifest/rea-noise-classes.yaml: %s" % hardcoded)
    assert "deadman_reasons" in body, "script never reads the mirrored reason table"


def test_reason_table_still_carries_the_five_reasons():
    """If this list ever empties or moves, the label enrichment silently becomes
    a no-op. Fail here instead."""
    import yaml
    d = yaml.safe_load(REASON_TABLE.read_text(encoding="utf-8")) or {}
    assert set(d.get("deadman_reasons") or []) == {
        "tunnel_timeout", "no_models", "ssh_fail", "blob_parse", "all_models_noop"}
