"""A garbage `added_on` must not be read as "this aged out, delete it".

COUNCIL FINDING 7 (2026-07-31 review, non-gating at the time, fixed here).

classify_torrent has two independent reap paths and only ONE of them consults
ratio:

    ratio >= min_ratio                    -> reap   (seeding duty done)
    age    >= max_seed_days               -> reap   (ratio NEVER consulted)

The second path trusts `added_on` completely. So the field is not merely
informational -- it is a delete authorisation. `added_on = 1` is epoch 1970,
which makes the computed age ~56 years, which clears any max_seed_days, which
reaps a torrent sitting at ratio 0.02. qBittorrent has been observed reporting
0 or a placeholder for items still being rechecked at startup, which is exactly
when a janitor timer could fire.

The rule now: an age is only usable if it is physically possible. Otherwise it
is UNKNOWN, and unknown falls through to the ratio rule -- which keeps. Keeping
a torrent too long costs disk. Reaping one early costs the client their media,
and this script runs unattended with --execute.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
JANITOR = REPO / "scripts" / "maint" / "qflix-torrent-janitor.py"

NOW = 1785000000          # ~2026-07
MIN_RATIO = 2.0
MAX_SEED_DAYS = 30


def _load():
    sys.path.insert(0, str(REPO / "scripts" / "maint"))
    spec = importlib.util.spec_from_file_location("torrent_janitor_added", JANITOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def m():
    return _load()


def _t(**over):
    """A torrent that has already cleared every gate ABOVE the age rule.

    classify_torrent short-circuits on progress, state, category, hash and
    *arr-tracking before it ever looks at added_on, so a fixture missing any of
    those tests nothing -- it just returns "incomplete". This is the exact
    population the age rule governs: complete, seeding-done, *arr-categorised,
    and no longer in any *arr queue.
    """
    base = {"hash": "a" * 40, "progress": 1.0, "state": "stalledUP",
            "category": "sonarr", "ratio": 0.02}
    base.update(over)
    return base


def _classify(m, t, tracked=frozenset()):
    return m.classify_torrent(t, tracked_hashes=set(tracked), now_epoch=NOW,
                              min_ratio=MIN_RATIO, max_seed_days=MAX_SEED_DAYS)


def test_the_fixture_actually_reaches_the_age_rule(m):
    """Guard the guard: if a gate above changes, every test below goes vacuous
    (they would all 'keep' for the wrong reason and still pass)."""
    action, reason = _classify(m, _t(added_on=NOW - 60 * 86400))
    assert (action, reason[:8]) == ("reap", "aged-out"), (
        "fixture no longer reaches the added_on branch -- got " + repr(reason)
    )


# --- the regression itself -------------------------------------------------

@pytest.mark.parametrize("bogus", [1, 0.5, -1, "1", 12345])
def test_epoch_garbage_does_not_authorise_a_delete(m, bogus):
    """THE BUG. Each of these used to compute a multi-decade age and reap."""
    action, reason = _classify(m, _t(added_on=bogus))
    assert action == "keep", (
        "added_on=" + repr(bogus) + " reaped a torrent at ratio 0.02 -- the "
        "aged-out branch never consults ratio, so a bad timestamp is a delete"
    )
    assert "implausible" in reason


def test_a_future_timestamp_is_also_refused(m):
    """Negative age would slip past `age >= max_seed_days` unnoticed; make it
    explicit rather than accidentally-safe, since clock skew flips sign."""
    action, reason = _classify(m, _t(ratio=0.0, added_on=NOW + 90 * 86400))
    assert action == "keep" and "implausible" in reason


# --- the guard must not break the real cases -------------------------------

def test_a_genuinely_aged_torrent_still_reaps(m):
    """The guard rejects impossible dates, not old ones."""
    action, reason = _classify(m, _t(ratio=0.1, added_on=NOW - 60 * 86400))
    assert action == "reap" and "aged-out" in reason


def test_ratio_met_still_reaps_regardless_of_a_bad_timestamp(m):
    """The ratio path is independent and must stay that way -- a torrent that
    HAS done its seeding duty is reapable even if added_on is garbage."""
    action, reason = _classify(m, _t(ratio=5.0, added_on=1))
    assert action == "reap" and "ratio-met" in reason


def test_within_seed_window_still_keeps(m):
    action, reason = _classify(m, _t(ratio=0.5, added_on=NOW - 3 * 86400))
    assert action == "keep" and reason == "seeding-duty-not-done"


def test_arr_tracked_wins_before_any_of_this(m):
    """Ordering guard: an in-flight *arr item is never reached by the age rule."""
    action, reason = _classify(m, _t(hash="f" * 40, ratio=0.0, added_on=1),
                               tracked={"f" * 40})
    assert action == "keep" and reason == "arr-tracked"


def test_missing_added_on_keeps(m):
    """Absent is not old. Unchanged behaviour, pinned so the guard can't
    accidentally invert it."""
    action, reason = _classify(m, _t(ratio=0.1))
    assert action == "keep" and reason == "seeding-duty-not-done"


# --- the predicate in isolation --------------------------------------------

def test_predicate_boundaries(m):
    oldest = NOW - m._MAX_PLAUSIBLE_AGE_S
    assert m._added_on_is_plausible(oldest, NOW) is True
    assert m._added_on_is_plausible(oldest - 1, NOW) is False
    assert m._added_on_is_plausible(NOW + m._MAX_ADDED_SKEW_S, NOW) is True
    assert m._added_on_is_plausible(NOW + m._MAX_ADDED_SKEW_S + 1, NOW) is False
    assert m._added_on_is_plausible(None, NOW) is False
    assert m._added_on_is_plausible("not-a-number", NOW) is False


def test_the_rule_is_relative_to_the_clock_not_to_a_hardcoded_year(m):
    """An absolute floor would make this pure function depend on wall-clock
    reality, breaking every caller on a synthetic timeline -- which is exactly
    how the existing ratio-policy suite drives it (now = 100 days past epoch).
    """
    synthetic_now = 100 * 86400
    assert m._added_on_is_plausible(50 * 86400, synthetic_now) is True, (
        "a coherent epoch-relative timeline was rejected -- the rule has an "
        "absolute date baked into it"
    )
    # ...and the same value IS garbage when the clock is real.
    assert m._added_on_is_plausible(50 * 86400, NOW) is False
