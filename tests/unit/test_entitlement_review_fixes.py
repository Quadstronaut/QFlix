"""Regression tests for the defects a 46-agent adversarial review confirmed.

Every test here corresponds to a finding that survived a refute-by-default
verification pass on 2026-08-06. They are collected in one file, rather than
scattered into the suites they belong to, because the point is not the
individual assertions -- it is that this specific class of bug was invisible to
a suite that already had 97 passing tests, and a reader deciding whether to
"simplify" one of these guards should be able to see the whole list at once.

The two that mattered most were both in the same family: a clock anchored to a
date in the PAST, so its "deadline" had already arrived.

NOTHING IN THIS FILE MAY NAME A REAL MEMBER.
"""
import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "maint" / "lib"))

import access_state as ST      # noqa: E402
import seerrusers as SU        # noqa: E402


def _load_gate():
    p = ROOT / "scripts" / "maint" / "qflix-entitlement.py"
    spec = importlib.util.spec_from_file_location("qflix_entitlement_fixes", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


G = _load_gate()

NOW = dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.timezone.utc)
AMNESTY = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
# What the live shares actually look like: accepted MONTHS ago.
LONG_AGO = dt.datetime(2026, 2, 10, tzinfo=dt.timezone.utc)


def _kw(**over):
    base = dict(amnesty_until=AMNESTY, grace_days=7, new_arrival_days=30, now=NOW)
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# FINDING 2 (high) -- the one the existing suite was actively hiding
# ---------------------------------------------------------------------------

def test_missing_amnesty_does_not_expire_the_launch_cohort_immediately(tmp_path):
    """A mistyped `amnesty_untill:` must not reduce every existing member at once.

    The launch cohort is seeded from Plex's real `acceptedAt`, which is months
    in the past. Falling back to `acceptedAt + 30d` yields a deadline that has
    ALREADY PASSED, so one typo in one roster key expires the entire membership
    on the first armed run -- the exact event the amnesty exists to prevent.

    The original unit test missed this because it seeded the bare-email form,
    which anchors at "now". Production seeds the (email, acceptedAt) pair form.
    That difference is why this test seeds the pair form explicitly.
    """
    st = ST.AccessState.load(tmp_path / "s.json")
    st.seed([("a@example.com", LONG_AGO)], now=NOW)
    assert st.get("a@example.com").first_seen_accepted == LONG_AGO
    assert st.get("a@example.com").cohort == ST.COHORT_LAUNCH

    deadline = st.deadline_for("a@example.com", **_kw(amnesty_until=None))
    assert deadline > NOW, (
        "a launch-cohort account with no amnesty date got a deadline in the "
        "past (%s) -- one roster typo would reduce the whole membership" % deadline)
    assert not st.is_expired("a@example.com", **_kw(amnesty_until=None))
    assert deadline == NOW + dt.timedelta(days=30), \
        "the fallback must be measured from first_run_at, not from acceptedAt"


def test_amnesty_date_still_wins_when_present(tmp_path):
    """The floor must not override the operator's chosen date.

    first_run_at + 7d is 14 August; the operator said end of August. The
    anti-footgun floor must be a floor, not a competing policy.
    """
    st = ST.AccessState.load(tmp_path / "s.json")
    st.seed([("a@example.com", LONG_AGO)], now=NOW)
    assert st.deadline_for("a@example.com", **_kw()) == AMNESTY


def test_state_loss_after_the_amnesty_expired_does_not_expire_everyone(tmp_path):
    """FINDING 3. State is lost in October; the roster still carries the stale
    1 September amnesty because the operator has not deleted it yet.

    Everyone re-seeds as launch cohort. Without the floor, the deadline is the
    stale date -- already passed -- and the entire membership is reduced on the
    next run purely because a file went missing.
    """
    later = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
    st = ST.AccessState.load(tmp_path / "s.json")
    st.seed([("a@example.com", LONG_AGO)], now=later)
    d = st.deadline_for("a@example.com", **_kw(now=later))
    assert d >= later + dt.timedelta(days=ST.LAUNCH_FLOOR_DAYS)
    assert not st.is_expired("a@example.com", **_kw(now=later))


# ---------------------------------------------------------------------------
# FINDING 4 (high) -- the grace that was never granted
# ---------------------------------------------------------------------------

def test_the_run_that_first_sees_a_lapse_grants_the_full_week(tmp_path):
    """Recording must happen BEFORE planning, or the 7-day clock contributes
    nothing on the run that starts it.

    Post-amnesty the cohort clock is in the past, so a member who lapses is
    reduced on the same run that first notices -- zero of the week they were
    promised. This asserts the ordering contract at the state level: once
    record_not_entitled has run, the deadline is a week out.
    """
    later = dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc)
    st = ST.AccessState.load(tmp_path / "s.json")
    st.seed([("a@example.com", LONG_AGO)], now=NOW)
    st.record_entitled("a@example.com", now=NOW)

    # Before recording: nothing protects them but the (long past) cohort clock.
    assert st.is_expired("a@example.com", **_kw(now=later))
    # After recording the lapse: a full week.
    st.record_not_entitled("a@example.com", now=later)
    assert not st.is_expired("a@example.com", **_kw(now=later))
    assert st.deadline_for("a@example.com", **_kw(now=later)) == \
        later + dt.timedelta(days=7)


def test_gate_records_answers_before_it_plans():
    """Structural check on the source: the recording loop must precede the
    planning loop. Ordering is invisible at runtime until somebody is wrongly
    reduced, so it is pinned here."""
    src = (ROOT / "scripts" / "maint" / "qflix-entitlement.py").read_text(encoding="utf-8")
    rec = src.index("state.record_not_entitled(share.email")
    plan = src.index("plans.append(plan_for_share(")
    assert rec < plan, (
        "answers must be recorded into the clocks BEFORE planning, or the run "
        "that first observes a lapse computes its deadline without the lapse "
        "clock and reduces the member with zero grace")


# ---------------------------------------------------------------------------
# FINDING 5 (high) -- roster errors quote member data
# ---------------------------------------------------------------------------

def test_roster_validation_failure_does_not_push_member_data_to_kuma():
    """members.py quotes offending rows verbatim ("%s appears in both %r and
    %r"), so its errors embed real addresses. Kuma is a status surface; this
    repo has leaked member data through a public surface once already."""
    src = (ROOT / "scripts" / "maint" / "qflix-entitlement.py").read_text(encoding="utf-8")
    block = src[src.index("except MEM.MembersError"):]
    block = block[:block.index("return EXIT_CONFIG")]
    assert '_push_kuma("down", "roster invalid: %s" % e)' not in block
    assert "durable log" in block


# ---------------------------------------------------------------------------
# FINDING 6 (high) -- the fallback grant was the minority bitfield
# ---------------------------------------------------------------------------

def test_member_permission_default_is_the_value_the_membership_actually_holds():
    """1155539104 is what 12 of 14 live accounts carry. Seerr's
    defaultPermissions setting read 1153433760, which is NARROWER by two bits.

    Copying the setting would silently demote any member whose saved prior
    value was lost -- and a silent demotion is worse than a loud failure,
    because the log says "restored" and the person never reports losing a
    feature they rarely use.
    """
    assert SU.MEMBER_PERMISSIONS == 1155539104
    assert SU.MEMBER_PERMISSIONS > 1153433760
    assert SU.MEMBER_PERMISSIONS & 1153433760 == 1153433760, \
        "the default must be a superset of the narrower setting, never a subset"


# ---------------------------------------------------------------------------
# FINDINGS 7 / 9 / 11 / 13 / 17 -- notification storms
# ---------------------------------------------------------------------------

def test_an_alert_is_sent_once_a_day_not_ninety_six_times(tmp_path):
    st = ST.AccessState.load(tmp_path / "s.json")
    text = "unnamed share holds 4 sections"
    assert st.should_alert("a@example.com", text, NOW)
    st.mark_alert("a@example.com", text, NOW)
    assert not st.should_alert("a@example.com", text, NOW + dt.timedelta(minutes=15))
    assert not st.should_alert("a@example.com", text, NOW + dt.timedelta(hours=6))
    assert st.should_alert("a@example.com", text, NOW + dt.timedelta(days=1))


def test_an_escalating_alert_is_not_suppressed(tmp_path):
    """A changed message is a new fact. Holding "holds 5 sections" for a day
    because "holds 1 section" was sent this morning hides an escalation."""
    st = ST.AccessState.load(tmp_path / "s.json")
    st.mark_alert("a@example.com", "unnamed share holds 1 section", NOW)
    assert st.should_alert("a@example.com", "unnamed share holds 5 sections", NOW)


def test_the_countdown_digest_is_sent_once_per_day(tmp_path):
    """The timer fires at :07/:22/:37/:52, so an hour-keyed check is true four
    times. The day key is what actually deduplicates."""
    st = ST.AccessState.load(tmp_path / "s.json")
    five_pm = dt.datetime(2026, 8, 17, 17, 7, tzinfo=dt.timezone.utc)
    assert st.should_digest(five_pm)
    st.mark_digest(five_pm)
    for minute in (22, 37, 52):
        assert not st.should_digest(five_pm.replace(minute=minute))
    assert st.should_digest(five_pm + dt.timedelta(days=1))


def test_digest_throttle_survives_a_save_load_round_trip(tmp_path):
    st = ST.AccessState.load(tmp_path / "s.json")
    st.mark_digest(NOW)
    st.save()
    assert not ST.AccessState.load(tmp_path / "s.json").should_digest(NOW)


# ---------------------------------------------------------------------------
# FINDING 8 (medium) -- cohort discarded on an outage exit
# ---------------------------------------------------------------------------

def test_cohort_is_persisted_before_any_early_return():
    """A first run during an entitlement outage used to discard first_run_at and
    every cohort tag. The next successful run would be "first" again, and a
    share that genuinely arrived in between would be back-dated into the launch
    cohort and handed an amnesty it never earned."""
    src = (ROOT / "scripts" / "maint" / "qflix-entitlement.py").read_text(encoding="utf-8")
    # Anchored inside main(): the 2026-08-08 money-path merge moved the
    # outage into compute_plans()/AllLookupsFailed, and the exception class
    # (plus --arm-check's own catch) now mention the message earlier in the
    # file. The invariant is unchanged: within the real run, the seeded
    # cohort is persisted before the outage early-return.
    seg = src[src.index("def main("):]
    seed = seg.index("added = state.seed(accepted")
    save = seg.index("could not persist the seeded cohort")
    outage = seg.index("except AllLookupsFailed")
    assert seed < save < outage, \
        "the seeded cohort must be saved before the outage early-return"


# ---------------------------------------------------------------------------
# FINDING 10 (medium) -- SystemExit bypassed the error handling
# ---------------------------------------------------------------------------

def test_machine_id_failure_is_catchable_not_a_bare_systemexit():
    """SystemExit skips main()'s handler, exits 1 (the "partial failure" code)
    and never pushes to Kuma -- so a total inability to reach Plex reads as a
    normal partial run and the monitor stays green."""
    src = (ROOT / "scripts" / "maint" / "qflix-entitlement.py").read_text(encoding="utf-8")
    fn = src[src.index("def _plex_machine_id"):]
    fn = fn[:fn.index("\ndef ")]
    assert "raise SystemExit" not in fn
    assert "raise ValueError" in fn


# ---------------------------------------------------------------------------
# FINDING 12 (low) -- no run lock despite the spec claiming one
# ---------------------------------------------------------------------------

@pytest.mark.skipif(__import__("os").name != "posix",
                    reason="liveness is checked with os.kill(pid, 0), which only "
                           "means anything on POSIX. The gate runs on Linux; on "
                           "Windows the lock degrades to advisory and the test "
                           "would assert behaviour that does not exist there.")
def test_run_lock_excludes_a_second_live_run(tmp_path):
    """Two concurrent runs each read the pre-mutation permissions; if one has
    already written 0, the other saves 0 as the value to RESTORE, permanently
    downgrading the member while the log reports a clean restore."""
    import os
    lock = tmp_path / "run.lock"
    assert G._take_lock(lock) is True
    assert lock.exists()
    lock.write_text("%d\n" % os.getpid(), encoding="utf-8")
    assert G._take_lock(lock) is False, "a live PID must exclude a second run"


def test_missing_first_run_at_does_not_slide_the_deadline_forever(tmp_path):
    """A state file with accounts but no first_run_at must be healed on load.

    The launch-cohort floor is measured from that timestamp, so a missing one
    pushes the deadline forward on every single run and nobody is EVER reduced
    -- the protection half stops working silently, which is the failure mode
    nobody reports because nothing visibly breaks.
    """
    import json
    p = tmp_path / "s.json"
    p.write_text(json.dumps({
        "schema": 1,
        "accounts": {"a@example.com": {"first_seen_accepted": "2026-02-10T00:00:00Z",
                                       "cohort": "launch"}},
    }), encoding="utf-8")
    st = ST.AccessState.load(p)
    assert st.first_run_at is not None, "a state with accounts must have an anchor"
    assert st.dirty, "the healed anchor must be persisted, not recomputed each run"
    assert not st.is_first_run()


def test_a_genuinely_empty_state_is_still_a_first_run(tmp_path):
    """The heal above must not fire on a real first run -- doing so would make
    is_first_run() false before seed(), tagging the entire launch cohort as
    arrivals and losing the amnesty for everybody."""
    st = ST.AccessState.load(tmp_path / "does-not-exist.json")
    assert st.is_first_run()
    st.seed([("a@example.com", LONG_AGO)], now=NOW)
    assert st.get("a@example.com").cohort == ST.COHORT_LAUNCH


def test_a_stale_lock_is_taken_over_not_obeyed_forever(tmp_path):
    """One SIGKILL must not wedge the gate permanently -- that would turn a
    crash into a silent outage of both provisioning and protection."""
    lock = tmp_path / "run.lock"
    lock.write_text("999999999\n", encoding="utf-8")   # certainly dead
    assert G._take_lock(lock) is True


def test_an_unwritable_lock_does_not_stop_the_run(tmp_path):
    """Refusing to run because the lock cannot be written would let a read-only
    state directory silently stop all provisioning and all protection."""
    assert G._take_lock(Path("/nonexistent-root-xyz/run.lock")) is True


# ---------------------------------------------------------------------------
# FINDINGS 15 / 16 / 18 (low) -- manifests grew forever
# ---------------------------------------------------------------------------

def test_both_durable_artefacts_are_bounded_each_by_the_rule_that_fits_it():
    """96 a day, forever, each a per-member decision record: an unbounded disk
    leak on a shared slot AND a growing pile of member data with a lifetime
    nobody chose.

    The ORIGINAL fix put manifests on the log's 30-day age rule, and this test
    pinned that by reading the glob list inside _open_log. It was pinning the
    implementation, and the implementation was wrong: logs rotate DAILY, so an
    age rule bounds them at 30 files, while manifests are per-RUN, so the same
    rule settles at ~2880 and had not fired once in the thirteen days before it
    was found (1264 files / 14 MB live on 2026-08-20). An age rule bounds a
    daily artefact; only a COUNT bounds a bursty one.

    So this asserts the PROPERTY -- each artefact is bounded -- and leaves the
    two rules free to differ, because they should. The count rule's own
    contract (keeps the newest, idempotent, never prunes under budget) lives in
    tests/unit/test_entitlement_manifest_prune.py.
    """
    src = (ROOT / "scripts" / "maint" / "qflix-entitlement.py").read_text(encoding="utf-8")
    fn = src[src.index("def _open_log"):]
    fn = fn[:fn.index("\ndef ")]
    assert "entitlement-*.log" in fn, "the daily log keeps its age rule"
    assert "LOG_RETENTION_DAYS" in fn
    assert G.MANIFEST_RETENTION_RUNS > 0, "the manifest bound must be a real cap"
    assert callable(G.prune_manifests)


def test_the_manifest_cap_actually_bounds_the_directory(tmp_path):
    """The bound, exercised rather than grepped: a directory over budget comes
    back to the cap, and the newest record -- the one an operator asks about
    first -- is never the one that goes."""
    made = []
    for i in range(12):
        p = tmp_path / ("manifest-20260820T%04dZ.json" % i)
        p.write_text("{}\n", encoding="utf-8")
        made.append(p)
    assert G.prune_manifests(tmp_path, keep=4) == 8
    survivors = sorted(q.name for q in tmp_path.glob("manifest-*.json"))
    assert len(survivors) == 4
    assert made[-1].name in survivors, "the newest manifest must never be pruned"


def test_the_prune_runs_after_the_manifest_is_written():
    """Order is the difference between a cap and a cap-plus-one-per-run: prune
    BEFORE the write and the run's own file lands outside the count every time.
    It must also be non-fatal -- a full or read-only state dir is a housekeeping
    problem, never a reason for an armed gate to stop deciding."""
    src = (ROOT / "scripts" / "maint" / "qflix-entitlement.py").read_text(encoding="utf-8")
    write_at = src.index("mpath.write_text(")
    prune_at = src.index("prune_manifests(state_dir)")
    assert write_at < prune_at, "pruning before the write leaks one file per run"
    seg = src[prune_at - 400:prune_at + 400]
    assert "warn(" in seg, "a failed prune must warn, not raise"


# ---------------------------------------------------------------------------
# FINDING 14 (low) -- emailless shares vanished
# ---------------------------------------------------------------------------

def test_shares_without_an_email_are_counted_and_reported():
    """"14 shares, 14 planned" is the only arithmetic that proves nothing was
    skipped. A silently dropped share is one the operator cannot audit."""
    src = (ROOT / "scripts" / "maint" / "qflix-entitlement.py").read_text(encoding="utf-8")
    assert "nameless += 1" in src
    assert "carry no email address" in src


# ---------------------------------------------------------------------------
# Defence in depth -- the blast-radius tripwire
# ---------------------------------------------------------------------------

def test_blast_radius_tripwire_exists_and_gates_reductions_only():
    """A rail on the SHAPE of the failure, not on any particular cause.

    Both mass-shrink defects the review found -- a missing amnesty key and a
    lost state file -- were different bugs producing an identical shape: many
    accounts expiring in the same run. A tripwire on that shape stops both
    without knowing anything about clocks, and stops the next one too.

    It must gate reductions ONLY. Withholding a GRANT during an anomaly would
    punish members for a bug, and granting too much access is not the harm this
    system defends against.
    """
    src = (ROOT / "scripts" / "maint" / "qflix-entitlement.py").read_text(encoding="utf-8")
    assert "DEFAULT_MAX_REDUCE_PCT" in src
    assert "--max-reduce-pct" in src
    block = src[src.index("# ---- blast-radius tripwire"):]
    block = block[:block.index("# ---- apply")]
    # Only S_EXPIRED plans are filtered out; grants survive the trip.
    assert "p.state != S_EXPIRED" in block
    assert "S_ENTITLED" in block, "grants must remain in the governed denominator"


def test_a_tripped_tripwire_turns_the_monitor_red():
    """A green monitor would let a refused-to-act state sit unnoticed. The whole
    point is that the system has declared its own inputs untrustworthy."""
    src = (ROOT / "scripts" / "maint" / "qflix-entitlement.py").read_text(encoding="utf-8")
    tail = src[src.index('status = "up"'):]
    assert "if tripped:" in tail
    idx = tail.index("if tripped:")
    seg = tail[idx:idx + 600]
    assert '"down"' in seg and "EXIT_PARTIAL" in seg


def test_single_lapse_does_not_trip_the_wire():
    """One member lapsing out of ten governed is 10%, well under the 34%
    threshold. A rail that fires on normal operation is a rail that gets
    disabled."""
    G_ = _load_gate()
    assert G_.DEFAULT_MAX_REDUCE_PCT >= 20, \
        "threshold must sit above the rate of ordinary independent lapses"
    assert 100.0 * 1 / 10 < G_.DEFAULT_MAX_REDUCE_PCT
    assert 100.0 * 2 / 10 < G_.DEFAULT_MAX_REDUCE_PCT
    assert 100.0 * 4 / 10 > G_.DEFAULT_MAX_REDUCE_PCT, \
        "four simultaneous lapses out of ten is not a coincidence"
