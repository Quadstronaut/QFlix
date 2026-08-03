"""Config-drift lock for scripts/canaries/prowlarr-indexer-health.sh Probe 1.

WHY (diagnosed 2026-08-02/03): the canary was GREEN through a real, sustained
429 cascade -- 10-minute buckets of 234/177/126/108/86/79 TooManyRequests lines
on 2026-08-02 while every grab from Knaben was failing -- for two reasons that
are both numbers in files rather than logic:

  1. THE WINDOW COVERED NEITHER OF THE TWO TERMS THAT MATTER. The first fix
     sized it against the TIMER alone (OnCalendar=*:0/15 + RandomizedDelaySec=60
     -> 17m worst-case run spacing) and that is only half of it. vlogs is a
     5-minute BATCH ingest (qflix-vlogs-ingest.timer, OnUnitActiveSec=5min +
     RandomizedDelaySec=30), so an event at T is not queryable until ~T+6m and
     a window of W only sees it from a tick landing in [T+lag, T+W]. Coverage
     of every T therefore requires W >= spacing + lag, not W >= spacing.
     Measured from Kuma heartbeat history on 2026-08-02 (UTC): the 5m bucket
     at 02:15 held 334 lines and the 02:30:16 run looked at [02:20, 02:30] --
     that burst was never inside ANY window.

  2. THREE SURFACES, TWO VALUES. The script header said "default 25",
     manifest/apps.yaml said "(default 25)", and the code said `:-40`. No
     Environment= override existed on the box, so 40 was live and both docs
     lied. That is the "prompt and rules are two policy surfaces" failure this
     repo has already been bitten by once (rea-noise-enforcement, 2026-07-29):
     changing one policy surface without the other is the default outcome
     unless a test forbids it.

These tests pin the numbers, not the prose. Both faults are single-token edits
away from returning and neither would fail any other test in this suite.

Probe 1 also used to fold EVERY data-source failure into a zero -- an
unreachable vlogs, an unreachable Prowlarr and a genuinely quiet stack all
printed the same "prowlarr-indexer-flowing 429_count=0" and exited 0. The
exit-2 leg is asserted below so that cannot come back either.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canaries" / "prowlarr-indexer-health.sh"
TIMER = (REPO_ROOT / "scripts" / "maint" / "systemd"
         / "manitoba-maint-canary-prowlarr-indexer-health.timer")
APPS_YAML = REPO_ROOT / "manifest" / "apps.yaml"


def _script():
    return SCRIPT.read_text(encoding="utf-8")


def _header():
    """Everything above the first executable line."""
    return _script().split("set -uo pipefail", 1)[0]


def _code_default(var):
    m = re.search(r"%s:-([0-9a-z]+)\}" % re.escape(var), _script())
    assert m, "%s default not found in the script body" % var
    return m.group(1)


def _minutes(token):
    """'20m' -> 20. The script passes this straight to VictoriaLogs as
    `start=`, so the unit suffix is part of the contract."""
    m = re.fullmatch(r"(\d+)m", token)
    assert m, "window %r is not in the Nm form VictoriaLogs start= expects" % token
    return int(m.group(1))


def _timer_worst_case_spacing_minutes():
    """Worst case gap between two consecutive runs of an OnCalendar=*:0/N timer
    with RandomizedDelaySec=D: the earlier run can be delayed 0 and the later
    one D, so the gap is N + D. Systemd may also delay the earlier run by up to
    D, which pulls the following gap the other way -- so the safe bound a
    lookback window must clear is N + 2D."""
    body = TIMER.read_text(encoding="utf-8")
    m = re.search(r"OnCalendar=\*:0/(\d+)", body)
    assert m, "timer OnCalendar is no longer a */N minute schedule:\n" + body
    period = int(m.group(1))
    d = re.search(r"RandomizedDelaySec=(\d+)", body)
    jitter_min = (int(d.group(1)) / 60.0) if d else 0.0
    return period + 2 * jitter_min


# ---------------------------------------------------------------------------
# 1. The window must cover the timer -- the blind gap, closed
# ---------------------------------------------------------------------------


def test_the_lookback_window_covers_run_spacing_PLUS_ingest_lag():
    """The load-bearing invariant, in its corrected form.

    A burst at time T is queryable only from T+lag, and only stays inside the
    window until T+WINDOW. For EVERY T to be seen by SOME tick, the visible
    interval (WINDOW - lag) must be at least the gap between ticks. Sizing the
    window against the timer alone leaves the ingest-lag hole, which is what
    the first fix missed."""
    window = _minutes(_code_default("PROWLARR_CASCADE_WINDOW"))
    spacing = _timer_worst_case_spacing_minutes()
    lag_budget = int(_code_default("PROWLARR_INGEST_LAG_BUDGET_MIN"))
    assert window >= spacing + lag_budget, (
        "window=%dm < run-spacing %.1fm + ingest-lag budget %dm -- a burst can "
        "land in the hole between what is queryable and what is still in the "
        "window" % (window, spacing, lag_budget))


def test_the_invariant_has_teeth_both_shipped_windows_violated_it():
    """MUTATION PROOF. Both values this canary shipped with -- the original 10m
    and the timer-only 20m -- are run through the same predicate and must FAIL
    it, so a green result above is discriminating rather than trivially true."""
    required = _timer_worst_case_spacing_minutes() + int(
        _code_default("PROWLARR_INGEST_LAG_BUDGET_MIN"))
    assert 10 < required, "the original 10m window would satisfy the invariant"
    assert 20 < required, (
        "the 20m window (sized against the timer alone) would satisfy the "
        "invariant, so the invariant cannot be what caught the ingest-lag hole")


def test_the_lag_budget_itself_is_asserted_not_assumed():
    """An unmeasured assumption inside a watchdog is the thing the watchdog was
    supposed to remove. The script must MEASURE ingest lag and refuse to report
    a clean count when the measurement exceeds the budget."""
    body = _script()
    assert "PROWLARR_INGEST_LAG_BUDGET_MIN" in body
    assert "prowlarr-vlogs-lagging" in body, (
        "no stage label for an over-budget ingest lag -- a zero count under a "
        "stale index would still print flowing")


# ---------------------------------------------------------------------------
# 2. Every surface that quotes the threshold quotes the same number
# ---------------------------------------------------------------------------


def test_script_header_and_script_code_agree_on_the_threshold():
    code = _code_default("PROWLARR_CASCADE_429_THRESHOLD")
    m = re.search(r"PROWLARR_CASCADE_429_THRESHOLD\s+default (\d+)", _header())
    assert m, "the header no longer documents a threshold default"
    assert m.group(1) == code, (
        "header says %s, code says %s -- the exact drift that made both docs "
        "lie about the live value" % (m.group(1), code))


def test_apps_yaml_and_script_code_agree_on_the_threshold():
    code = _code_default("PROWLARR_CASCADE_429_THRESHOLD")
    text = APPS_YAML.read_text(encoding="utf-8")
    i = text.find("prowlarr-indexer-health:")
    assert i != -1, "prowlarr-indexer-health entry not found in apps.yaml"
    m = re.search(r"default (\d+)", text[max(0, i - 1200):i + 1200])
    assert m, "apps.yaml no longer quotes a threshold default"
    assert m.group(1) == code, (
        "apps.yaml says %s, the script uses %s" % (m.group(1), code))


def test_script_header_and_script_code_agree_on_the_window():
    code = _code_default("PROWLARR_CASCADE_WINDOW")
    m = re.search(r"PROWLARR_CASCADE_WINDOW\s+default (\d+m)", _header())
    assert m, "the header no longer documents a window default"
    assert m.group(1) == code


# ---------------------------------------------------------------------------
# 3. The number must be readable for what it is
# ---------------------------------------------------------------------------


def test_the_header_says_the_count_is_log_lines_not_events():
    """One failed grab emits ~10 matching lines (vlogs indexes each
    stack-trace frame), so the threshold reads ten times stricter than it is.
    An operator retuning it without that sentence will retune it wrong."""
    header = _header().lower()
    assert "lines" in header and "not events" in header, (
        "the header must state that Probe 1 counts LOG LINES, not events")


def test_no_data_source_failure_is_folded_into_a_zero():
    """RULE 5. `|| RAW=""` and `|| HEALTH="[]"` turned an unreachable vlogs and
    an unreachable Prowlarr into a count of zero, i.e. into a green push. The
    canary must have a BROKEN state and must not default either payload."""
    body = _script()
    assert 'RAW=""' not in body, "vlogs failure is still defaulted to an empty body"
    assert 'HEALTH="[]"' not in body, "Prowlarr failure is still defaulted to []"
    for stage in ("prowlarr-vlogs-unreachable", "prowlarr-health-unreachable",
                  "prowlarr-vlogs-no-data"):
        assert stage in body, "missing BROKEN stage label %s" % stage
    assert "exit 2" in body, "the script has no exit-2 state at all"


def test_the_header_documents_the_broken_exit():
    header = _header()
    assert re.search(r"^#\s+2 —", header, re.M), (
        "the Exits block must document exit 2, or the 1-vs-2 split is invisible "
        "to the next reader")


def test_config_missing_is_broken_not_a_finding():
    """An unreadable secret means the canary asserted NOTHING. Exiting 1 there
    makes it indistinguishable from a real cascade."""
    body = _script()
    m = re.search(r"STAGE=prowlarr-canary-config-missing.*?\n(.*?\n)?\s*exit (\d)",
                  body, re.S)
    assert m and m.group(2) == "2", (
        "config-missing must exit 2, not %s" % (m.group(2) if m else "?"))


def test_the_header_points_at_the_module_that_owns_the_config_faults():
    """Compartmentalize law, made findable. The next person to look at a 429
    cascade must not re-implement app-sync integrity here."""
    header = _header()
    assert "prowlarr-app-sync.sh" in header
    assert (REPO_ROOT / "scripts" / "canaries" / "prowlarr-app-sync.sh").is_file()
