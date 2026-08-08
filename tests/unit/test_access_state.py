"""The clocks decide the day somebody's television stops working.

Every test here is written from the position of a real person on the wrong end
of an off-by-one. The rules being enforced:

  * the LATEST applicable clock wins, so no promised window is ever shortened
    by the existence of another;
  * a never-entitled account is PENDING, not LAPSING, and never gets a lapse
    clock underneath its real deadline;
  * losing or corrupting the state file is safe in the generous direction --
    everyone re-seeds as a fresh arrival, nobody is cut off early;
  * the launch amnesty is a ONE-TIME migration allowance that can only ever be
    handed to accounts that existed on the first run;
  * an unparseable amnesty date falls back to a real window, never to "never".

NOTHING IN THIS FILE MAY NAME A REAL MEMBER.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "maint" / "lib"))

import access_state as A  # noqa: E402


AMNESTY = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
T0 = dt.datetime(2026, 8, 6, 12, 0, tzinfo=dt.timezone.utc)


def _at(days=0, hours=0):
    return T0 + dt.timedelta(days=days, hours=hours)


def _state(tmp_path):
    return A.AccessState.load(tmp_path / "state.json")


def _kw(**over):
    base = dict(amnesty_until=AMNESTY, grace_days=7, new_arrival_days=30)
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Cohorts
# ---------------------------------------------------------------------------

def test_first_run_tags_everyone_launch_and_later_runs_do_not(tmp_path):
    """The amnesty is one-time. It must be impossible to join the launch cohort
    late -- otherwise somebody invited in November inherits an August deadline
    that has already passed and is cut off on their first day."""
    st = _state(tmp_path)
    assert st.is_first_run()
    st.seed(["a@example.com", "b@example.com"], now=T0)
    assert st.get("a@example.com").cohort == A.COHORT_LAUNCH
    assert not st.is_first_run()

    st.seed(["c@example.com"], now=_at(days=5))
    assert st.get("c@example.com").cohort == A.COHORT_ARRIVAL


def test_seeding_persists_across_a_disarmed_run(tmp_path):
    """Seeding happens even when the gate may not act.

    A system that only learns who existed once it is allowed to mutate cannot
    tell 'pre-existing' from 'appeared while I was disarmed', and would hand the
    launch amnesty to somebody who joined last week.
    """
    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    st.save()

    reloaded = _state(tmp_path)
    assert reloaded.get("a@example.com").cohort == A.COHORT_LAUNCH
    reloaded.seed(["b@example.com"], now=_at(days=1))
    assert reloaded.get("b@example.com").cohort == A.COHORT_ARRIVAL


def test_reseeding_a_known_account_does_not_reset_its_anchor(tmp_path):
    """Otherwise every run would push the deadline out by one run's interval and
    the clock would never expire -- a silent, permanent amnesty for everyone."""
    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    st.seed(["a@example.com"], now=_at(days=40))
    assert st.get("a@example.com").first_seen_accepted == T0


# ---------------------------------------------------------------------------
# The three clocks
# ---------------------------------------------------------------------------

def test_launch_cohort_uses_the_amnesty_date(tmp_path):
    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    assert st.deadline_for("a@example.com", **_kw()) == AMNESTY


def test_new_arrival_gets_thirty_days_from_acceptance_not_the_amnesty(tmp_path):
    """The scenario that motivated this clock: invited 28 August, three days
    before a fixed 31 August deadline. A shared deadline would give them a
    token window; their own clock gives them a real one."""
    st = _state(tmp_path)
    st.seed(["existing@example.com"], now=T0)
    late = dt.datetime(2026, 8, 28, tzinfo=dt.timezone.utc)
    st.seed(["late@example.com"], now=late)
    assert st.deadline_for("late@example.com", **_kw()) == late + dt.timedelta(days=30)
    assert st.deadline_for("late@example.com", **_kw()) > AMNESTY


def test_latest_clock_wins_when_lapse_would_be_earlier(tmp_path):
    """A launch-cohort member who lapses mid-August still gets until the amnesty
    date, not seven days. Taking the min here would quietly cancel the amnesty
    for exactly the people it was written for."""
    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    st.record_entitled("a@example.com", now=_at(days=1))
    st.record_not_entitled("a@example.com", now=_at(days=2))     # 8 Aug + 7 = 15 Aug
    assert st.deadline_for("a@example.com", **_kw()) == AMNESTY


def test_latest_clock_wins_when_lapse_would_be_later(tmp_path):
    """And the mirror: once the amnesty is in the past, a lapse gets its full
    week from the day it happened."""
    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    st.record_entitled("a@example.com", now=_at(days=1))
    fell = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    st.record_not_entitled("a@example.com", now=fell)
    assert st.deadline_for("a@example.com", **_kw()) == fell + dt.timedelta(days=7)


def test_never_entitled_account_gets_no_lapse_clock(tmp_path):
    """Pending is not lapsing.

    Someone who never subscribed has one deadline -- their cohort's. Starting a
    seven-day lapse clock for them would put a second, shorter deadline
    underneath the real one. The max() means it changes no outcome today, which
    is exactly why it has to be enforced rather than commented: it is a live
    trap for the next person to edit these rules.
    """
    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    st.record_not_entitled("a@example.com", now=_at(days=1))
    acct = st.get("a@example.com")
    assert acct.went_false_at is None, "a never-entitled account must not be 'lapsing'"
    assert acct.first_not_entitled_at is not None, "but it is still reportable"
    assert st.deadline_for("a@example.com", **_kw()) == AMNESTY


def test_returning_member_gets_a_full_fresh_week_not_the_remainder(tmp_path):
    """Lapse -> return -> lapse again. The second week must be a whole week."""
    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    st.record_entitled("a@example.com", now=_at(days=1))
    st.record_not_entitled("a@example.com", now=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
    st.record_entitled("a@example.com", now=dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc))
    assert st.get("a@example.com").went_false_at is None, "returning clears the clock"

    second = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    st.record_not_entitled("a@example.com", now=second)
    assert st.deadline_for("a@example.com", **_kw()) == second + dt.timedelta(days=7)


def test_grace_of_seven_days_is_honoured_from_the_roster(tmp_path):
    """The operator raised this from 3 to 7. Prove the knob is wired, not baked."""
    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    st.record_entitled("a@example.com", now=_at(days=1))
    fell = dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc)
    st.record_not_entitled("a@example.com", now=fell)
    assert st.deadline_for("a@example.com", **_kw(grace_days=7)) == fell + dt.timedelta(days=7)
    assert st.deadline_for("a@example.com", **_kw(grace_days=3)) == fell + dt.timedelta(days=3)


def test_expiry_is_inclusive_of_the_deadline_instant(tmp_path):
    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    assert not st.is_expired("a@example.com", **_kw(now=AMNESTY - dt.timedelta(seconds=1)))
    assert st.is_expired("a@example.com", **_kw(now=AMNESTY))


def test_unknown_account_is_treated_as_arriving_now(tmp_path):
    """An account with no recorded history must get a full window, not a cut.

    This is the state-file-loss path: if the file vanishes, every account looks
    unknown, and the safe reading is 'brand new'.
    """
    st = _state(tmp_path)
    d = st.deadline_for("never-seen@example.com", **_kw(now=T0))
    assert d == T0 + dt.timedelta(days=30)
    assert not st.is_expired("never-seen@example.com", **_kw(now=T0))


# ---------------------------------------------------------------------------
# amnesty_until parsing -- a typo here must not free the whole launch cohort
# ---------------------------------------------------------------------------

def test_amnesty_accepts_a_bare_yaml_date_and_a_timestamp():
    assert A.parse_amnesty(dt.date(2026, 9, 1)) == AMNESTY
    assert A.parse_amnesty("2026-09-01") == AMNESTY
    assert A.parse_amnesty("2026-09-01T00:00:00Z") == AMNESTY


def test_unparseable_amnesty_falls_back_to_a_real_window_not_to_never(tmp_path):
    """A typo in amnesty_until must not grant the entire launch cohort permanent
    immunity. It degrades to the new-arrival window measured from the first run,
    which is a deadline that actually arrives."""
    assert A.parse_amnesty("not-a-date") is None
    assert A.parse_amnesty(None) is None

    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    d = st.deadline_for("a@example.com", **_kw(amnesty_until=None))
    assert d == T0 + dt.timedelta(days=30)


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------

def test_prior_permissions_survive_a_round_trip(tmp_path):
    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    st.remember_seerr("a@example.com", user_id=17, perms_prior=1155539104)
    st.save()
    assert _state(tmp_path).get("a@example.com").seerr_perms_prior == 1155539104


def test_zero_never_overwrites_the_remembered_permissions(tmp_path):
    """The bug this prevents: this system writes 0 to Seerr, then records 0 as
    'prior', destroying the only record of what to restore. Every subsequent
    restore becomes a guess at what member default used to be."""
    st = _state(tmp_path)
    st.remember_seerr("a@example.com", user_id=17, perms_prior=1155539104)
    st.remember_seerr("a@example.com", user_id=17, perms_prior=0)
    assert st.get("a@example.com").seerr_perms_prior == 1155539104


def test_corrupt_state_file_yields_empty_state_not_a_crash(tmp_path):
    """Refusing to run on corruption would stop PROVISIONING too, locking new
    members out of Seerr indefinitely over one bad byte. Losing the file is
    self-correcting in the generous direction; refusing to start is not."""
    p = tmp_path / "state.json"
    for junk in ("", "{", "null", "[]", '{"accounts": "not-a-dict"}'):
        p.write_text(junk, encoding="utf-8")
        st = A.AccessState.load(p)
        assert st.accounts == {}
        assert st.is_first_run()


def test_malformed_timestamp_degrades_one_account_not_the_run(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "schema": 1,
        "first_run_at": "2026-08-06T12:00:00Z",
        "accounts": {
            "good@example.com": {"first_seen_accepted": "2026-08-06T12:00:00Z",
                                 "cohort": "launch"},
            "bad@example.com": {"first_seen_accepted": "yesterday-ish",
                                "cohort": "launch"},
        },
    }), encoding="utf-8")
    st = A.AccessState.load(p)
    assert st.get("good@example.com").first_seen_accepted == T0
    assert st.get("bad@example.com").first_seen_accepted is None
    # ...and the degraded one gets a fresh window rather than an instant cut.
    assert not st.is_expired("bad@example.com", **_kw(amnesty_until=None, now=T0))


def test_save_is_atomic_and_leaves_no_tmp_file(tmp_path):
    st = _state(tmp_path)
    st.seed(["a@example.com"], now=T0)
    st.save()
    assert (tmp_path / "state.json").exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads((tmp_path / "state.json").read_text())["schema"] == A.SCHEMA


def test_emails_are_matched_case_insensitively(tmp_path):
    """Plex and Seerr disagree about case more often than anyone expects, and a
    case-sensitive miss would read as 'never seen' -- a fresh 30-day clock for
    someone who should be expiring, or a duplicate row."""
    st = _state(tmp_path)
    st.seed(["Person@Example.com"], now=T0)
    assert st.get("person@example.com").first_seen_accepted == T0
    assert st.deadline_for("PERSON@EXAMPLE.COM", **_kw()) == AMNESTY
