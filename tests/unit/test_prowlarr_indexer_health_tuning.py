"""Config-drift lock for scripts/canaries/prowlarr-indexer-health.sh Probe 1.

WHY (diagnosed 2026-08-02/03): the canary was GREEN through a real, sustained
429 cascade -- 10-minute buckets of 234/177/126/108/86/79 TooManyRequests lines
on 2026-08-02 while every grab from Knaben was failing -- for two reasons that
are both numbers in files rather than logic:

  1. THE WINDOW DID NOT COVER THE TIMER. The unit is OnCalendar=*:0/15 with
     RandomizedDelaySec=60, so consecutive runs land up to 15m+60s apart, and
     the script looked back only 10m. That is at least five minutes of every
     cycle unobserved, and these bursts are minutes long. Measured live: the
     same LogsQL query returned n=0 at start=10m while the 24h buckets above
     were in the record.

  2. THREE SURFACES, TWO VALUES. The script header said "default 25",
     manifest/apps.yaml said "(default 25)", and the code said `:-40`. No
     Environment= override existed on the box, so 40 was live and both docs
     lied. That is the "prompt and rules are two policy surfaces" failure this
     repo has already been bitten by once (rea-noise-enforcement, 2026-07-29):
     changing one policy surface without the other is the default outcome
     unless a test forbids it.

These tests pin the numbers, not the prose. Both faults are single-token edits
away from returning and neither would fail any other test in this suite.

DELIBERATELY NOT ASSERTED HERE, and handed to whoever registers the new
prowlarr-app-sync canary: manifest/apps.yaml's prowlarr-indexer-health comment
still says "in a 10-min window". Its THRESHOLD leg is asserted below and now
agrees; the window phrase needs the same one-word edit
("10-min window" -> "20-min window") in the same commit that adds the manifest
entry. This track does not edit shared manifests.
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


def test_the_lookback_window_covers_the_timers_worst_case_run_spacing():
    """The load-bearing invariant. If the window is shorter than the gap
    between runs, a burst that starts and ends inside that gap is never
    counted -- and the canary reports PASS with the incident in the logs."""
    window = _minutes(_code_default("PROWLARR_CASCADE_WINDOW"))
    spacing = _timer_worst_case_spacing_minutes()
    assert window >= spacing, (
        "window=%dm does not cover the timer's worst-case run spacing of "
        "%.1fm -- every cycle has a %.1f-minute blind gap"
        % (window, spacing, spacing - window))


def test_the_invariant_has_teeth_the_shipped_10m_window_violated_it():
    """MUTATION PROOF. The value this canary shipped with is run through the
    same predicate and must FAIL it, so the assertion above is known to
    discriminate rather than being trivially true for any number."""
    spacing = _timer_worst_case_spacing_minutes()
    assert 10 < spacing, (
        "the original 10m window would satisfy the invariant, which means the "
        "invariant cannot be what caught the 2026-08-02 miss")


def test_the_window_overlaps_rather_than_merely_touching():
    """Exactly equal would leave zero margin for a slow run or a clock nudge.
    A couple of minutes of deliberate overlap costs at most a repeated DOWN on
    one burst, which Kuma collapses into a single incident."""
    window = _minutes(_code_default("PROWLARR_CASCADE_WINDOW"))
    assert window - _timer_worst_case_spacing_minutes() >= 2


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


def test_the_header_points_at_the_module_that_owns_the_config_faults():
    """Compartmentalize law, made findable. The next person to look at a 429
    cascade must not re-implement app-sync integrity here."""
    header = _header()
    assert "prowlarr-app-sync.sh" in header
    assert (REPO_ROOT / "scripts" / "canaries" / "prowlarr-app-sync.sh").is_file()
