"""The Welcome library must be visible to the NON-entitled and to nobody else.

WHY THIS FILE EXISTS
The Welcome section holds a single video whose entire content is an instruction
to go to Patreon and activate a subscription. It is the floor that a lapsed or
never-paid account is reduced to. Until 2026-08-07 `full_access_ids()` returned
*every* section on the server, so the two access levels were NESTED rather than
disjoint and an entitled member would have been shown the "please subscribe"
library the moment the gate was armed -- asking people who already pay to pay.

Nothing would have caught that. The gate is disarmed, so no share had been
written yet; the mistake would have surfaced as a member complaint after the
first armed run, which is the most expensive place to find it.

WHAT IT ASSERTS
  * Welcome is in the minimum set.
  * Welcome is NOT in the full set.
  * The two sets are disjoint, which is the property that actually matters:
    membership of Welcome is then a truthful signal of "not currently entitled",
    which is what makes the video's instruction correct for everyone who can
    read it.
  * Neither set can be empty. An empty section list does not RESTRICT a Plex
    share, it DELETES it -- so the degenerate cases (Welcome missing, Welcome
    being the only library) must raise rather than quietly evict.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "maint"))

from lib import plexshare as PS  # noqa: E402

WELCOME = "QFlix - Welcome"


def _sections(*titles):
    """Sections with ids deliberately unequal to keys.

    Plex's `library_section_ids` wants Section@id while the local PMS talks in
    @key, and confusing the two silently shares the wrong libraries. Keeping
    them different here means a test that accidentally reads `.key` fails.
    """
    return [PS.Section(id=100 + i, key=i + 1, title=t, type="movie")
            for i, t in enumerate(titles)]


def test_welcome_is_the_whole_minimum_set():
    secs = _sections("Movies", "TV", WELCOME, "Anime")
    assert PS.minimum_access_ids(secs, WELCOME) == [102]


def test_welcome_is_absent_from_full_access():
    """The regression this file was written for."""
    secs = _sections("Movies", "TV", WELCOME, "Anime")
    full = PS.full_access_ids(secs, WELCOME)
    assert 102 not in full, "an entitled member would be shown the 'go subscribe' library"
    assert full == [100, 101, 103]


def test_the_two_access_levels_are_disjoint():
    secs = _sections("Movies", "TV", WELCOME, "Anime", "Music")
    full = set(PS.full_access_ids(secs, WELCOME))
    minimum = set(PS.minimum_access_ids(secs, WELCOME))
    assert not (full & minimum), (
        "Welcome leaks into full access, so its presence no longer means "
        "'this account is not entitled'")


def test_full_access_still_picks_up_a_brand_new_library():
    """Recomputed per run on purpose -- a library added at 3pm must reach
    entitled members by 3:15 rather than never."""
    before = PS.full_access_ids(_sections("Movies", WELCOME), WELCOME)
    after = PS.full_access_ids(_sections("Movies", WELCOME, "Documentaries"), WELCOME)
    assert set(after) - set(before), "a new section did not reach full access"


def test_missing_welcome_raises_rather_than_evicting():
    secs = _sections("Movies", "TV")
    with pytest.raises(PS.PlexShareError):
        PS.minimum_access_ids(secs, WELCOME)


def test_welcome_as_the_only_library_raises_rather_than_evicting():
    """Subtracting Welcome from a one-library server yields []. An empty list
    unshares the server instead of granting it, so it must be refused."""
    secs = _sections(WELCOME)
    with pytest.raises(PS.PlexShareError):
        PS.full_access_ids(secs, WELCOME)


def test_full_access_raises_on_an_empty_section_list():
    """A section list that failed to load must not read as 'grant nothing'."""
    with pytest.raises(PS.PlexShareError):
        PS.full_access_ids([], WELCOME)


def test_welcome_match_is_case_and_whitespace_tolerant():
    """find_section() is lenient, and the subtraction must be exactly as
    lenient -- otherwise a stray space in the title silently re-adds Welcome
    to full access without failing anything."""
    secs = [PS.Section(id=100, key=1, title="Movies", type="movie"),
            PS.Section(id=101, key=2, title="  qflix - WELCOME  ", type="movie")]
    assert 101 not in PS.full_access_ids(secs, WELCOME)


# ---------------------------------------------------------------------------
# QFLX-4: the floor is a SET -- Welcome (required) + optional extras.
# `QFlix - Test` holds playback test clips for prospects; operator 2026-10-03:
# "for pre-subscription testing" -- unentitled see it, entitled never do.
# ---------------------------------------------------------------------------
TEST = "QFlix - Test"


def test_extra_floor_title_joins_the_minimum_set():
    secs = _sections("Movies", WELCOME, TEST)          # ids 100, 101, 102
    assert PS.minimum_access_ids(secs, WELCOME, (TEST,)) == [101, 102]


def test_extra_floor_title_is_absent_from_full_access():
    secs = _sections("Movies", "TV", WELCOME, TEST)
    assert PS.full_access_ids(secs, WELCOME, (TEST,)) == [100, 101]


def test_floor_set_and_full_access_stay_disjoint():
    secs = _sections("Movies", "TV", WELCOME, TEST, "Anime")
    full = set(PS.full_access_ids(secs, WELCOME, (TEST,)))
    floor = set(PS.minimum_access_ids(secs, WELCOME, (TEST,)))
    assert not (full & floor)


def test_missing_extra_floor_title_degrades_to_welcome_only():
    """Test is optional: absent means the floor is Welcome alone -- never a raise,
    because the anti-eviction rail is about Welcome, not about Test."""
    secs = _sections("Movies", WELCOME)
    assert PS.minimum_access_ids(secs, WELCOME, (TEST,)) == [101]
    assert PS.missing_floor_titles(secs, (TEST,)) == [TEST]


def test_missing_welcome_still_raises_even_when_test_exists():
    """A Test-only floor would hand prospects the clips but drop the
    go-subscribe video; worse, it hides that Welcome is gone. Refuse."""
    secs = _sections("Movies", TEST)
    with pytest.raises(PS.PlexShareError):
        PS.minimum_access_ids(secs, WELCOME, (TEST,))


def test_floor_only_server_raises_rather_than_evicting():
    secs = _sections(WELCOME, TEST)
    with pytest.raises(PS.PlexShareError):
        PS.full_access_ids(secs, WELCOME, (TEST,))


def test_extra_floor_match_is_case_and_whitespace_tolerant():
    secs = [PS.Section(id=100, key=1, title="Movies", type="movie"),
            PS.Section(id=101, key=2, title=WELCOME, type="movie"),
            PS.Section(id=102, key=3, title="  qflix - TEST ", type="movie")]
    assert PS.full_access_ids(secs, WELCOME, (TEST,)) == [100]
    assert PS.missing_floor_titles(secs, (TEST,)) == []


def test_default_extra_floor_is_empty_so_old_callers_are_unchanged():
    secs = _sections("Movies", WELCOME, TEST)
    assert PS.full_access_ids(secs, WELCOME) == [100, 102]
    assert PS.minimum_access_ids(secs, WELCOME) == [101]
