"""The janitor's min-ratio and qBittorrent's max_ratio are one policy in two places.

WHY (measured 2026-07-30): qBit was configured `max_ratio=2.0` with
`max_ratio_act=pause` — seed to 2.0, then stop. The janitor's `--min-ratio`
defaulted to **1.0**, so it deleted every *arr-complete torrent at HALF the ratio
qBit had been told to seed to. qBit's 2.0 was unreachable dead config: no torrent
could ever survive long enough to hit it.

Evidence, from the janitor's own durable log:

    2026-07-28T00:28:38Z DELETED 8637b20ba3cd ... (32.4 GB)   ratio 1.85
    2026-07-28T00:28:38Z DELETED b76be1cbf5dc ... (5.59 GB)   ratio 1.18

Both below 2.0. Every run afterwards logged "qBittorrent: 0 torrent(s)" — the
pool sat permanently drained for six days while the qBit setting still claimed
it should be seeding. No data was lost (the delete is hardlink-safe and both
library files survived); what was lost was half the intended give-back.

Raised to 2.0 on operator instruction. The flow is now coherent: seed to 2.0 ->
qBit pauses -> the janitor reaps what qBit has finished with.

These tests pin the CONSTANT and the classifier's behaviour at the boundary.
They cannot read the live qBit preference — that is asserted by the smoke test
against the running box — so the docstring carries the "change one, change the
other" instruction and this file pins the value it must equal.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
JANITOR = REPO / "scripts" / "maint" / "qflix-torrent-janitor.py"

# qBittorrent's live max_ratio, read from /api/v2/app/preferences on 2026-07-30.
QBIT_MAX_RATIO = 2.0


def _load():
    sys.path.insert(0, str(REPO / "scripts" / "maint"))
    sys.path.insert(0, str(REPO / "scripts" / "mcp"))
    spec = importlib.util.spec_from_file_location("torrent_janitor", JANITOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def m():
    return _load()


def _t(ratio, *, state="stalledUP", cat="radarr", h="a" * 40, added=0):
    return {"hash": h, "name": "x", "category": cat, "state": state,
            "ratio": ratio, "added_on": added, "progress": 1.0, "size": 1}


def test_min_ratio_matches_qbit_max_ratio(m):
    """The regression. At 1.0 the janitor undercut qBit by half."""
    assert m.DEFAULT_MIN_RATIO == QBIT_MAX_RATIO, (
        "janitor --min-ratio (%s) must equal qBittorrent's max_ratio (%s); the "
        "LOWER of the two silently wins and the other becomes dead config"
        % (m.DEFAULT_MIN_RATIO, QBIT_MAX_RATIO)
    )


def test_the_two_torrents_that_were_deleted_would_now_be_kept(m):
    """Replay the real 2026-07-28 deletions against the new threshold."""
    now = 10 * 86400
    for ratio in (1.85, 1.1756117712512522):
        action, reason = m.classify_torrent(
            _t(ratio), tracked_hashes=set(), now_epoch=now,
            min_ratio=m.DEFAULT_MIN_RATIO, max_seed_days=30)
        assert action == "keep", (
            "ratio %s was deleted on 2026-07-28 and would be again: %s"
            % (ratio, reason))


def test_a_torrent_that_has_actually_finished_seeding_is_still_reaped(m):
    """Raising the bar must not stop the janitor doing its job."""
    action, reason = m.classify_torrent(
        _t(2.0), tracked_hashes=set(), now_epoch=10 * 86400,
        min_ratio=m.DEFAULT_MIN_RATIO, max_seed_days=30)
    assert action == "reap" and "ratio-met" in reason


def test_qbit_paused_at_target_is_a_reapable_state(m):
    """The intended flow: qBit pauses at max_ratio, THEN the janitor collects.

    If pausedUP/stoppedUP were not reapable states, raising min-ratio to qBit's
    pause threshold would deadlock — qBit stops seeding and nothing ever cleans
    up, so the pool would grow without bound.
    """
    for state in ("pausedUP", "stoppedUP"):
        action, _ = m.classify_torrent(
            _t(2.5, state=state), tracked_hashes=set(), now_epoch=10 * 86400,
            min_ratio=m.DEFAULT_MIN_RATIO, max_seed_days=30)
        assert action == "reap", state + " must be reapable or the pool deadlocks"


def test_age_backstop_still_reaps_what_can_never_reach_ratio(m):
    """Raising the bar makes this rail carry more traffic, not less.

    More torrents will now never reach 2.0, so 'nothing seeds forever' is what
    stops the pool growing without bound.
    """
    now = 100 * 86400
    action, reason = m.classify_torrent(
        _t(0.0, added=50 * 86400), tracked_hashes=set(), now_epoch=now,
        min_ratio=m.DEFAULT_MIN_RATIO, max_seed_days=30)
    assert action == "reap" and "aged-out" in reason, reason


def test_a_torrent_just_under_the_age_limit_is_kept(m):
    """Boundary — the backstop must not reap early."""
    now = 100 * 86400
    action, _ = m.classify_torrent(
        _t(0.0, added=now - 29 * 86400), tracked_hashes=set(), now_epoch=now,
        min_ratio=m.DEFAULT_MIN_RATIO, max_seed_days=30)
    assert action == "keep"


def test_unknown_add_date_is_kept_not_reaped(m):
    """`added_on` absent or 0 means UNKNOWN, and unknown never grants a delete.

    This is deliberate and matches the repo's standing principle that absence of
    evidence is not evidence: the anime janitor likewise refuses to move a title
    whose originalLanguage is missing. A torrent with no usable add-date simply
    waits for its ratio instead.
    """
    for added in (0, None):
        action, reason = m.classify_torrent(
            _t(0.0, added=added), tracked_hashes=set(), now_epoch=10 ** 9,
            min_ratio=m.DEFAULT_MIN_RATIO, max_seed_days=30)
        assert action == "keep", "added_on=%r must not authorise a delete" % added
        assert reason == "seeding-duty-not-done"


def test_still_never_touches_a_non_arr_torrent(m):
    """The personal-torrent protection is unaffected by the threshold change."""
    action, reason = m.classify_torrent(
        _t(9.0, cat=""), tracked_hashes=set(), now_epoch=10 * 86400,
        min_ratio=m.DEFAULT_MIN_RATIO, max_seed_days=30)
    assert action == "keep" and "non-arr-category" in reason


def test_still_never_touches_an_arr_tracked_torrent(m):
    h = "b" * 40
    action, reason = m.classify_torrent(
        _t(9.0, h=h), tracked_hashes={h}, now_epoch=10 * 86400,
        min_ratio=m.DEFAULT_MIN_RATIO, max_seed_days=30)
    assert action == "keep" and reason == "arr-tracked"


def test_preview_and_armed_run_use_the_same_cap(m):
    """The drop-in must not carry a threshold the default disagrees with.

    Until 2026-07-30 `--max-pct 100` lived only in the on-box drop-in, so the
    armed run had the tripwire OFF while a bare hand-run had it ON at 90. The
    preview was STRICTER than reality: on 2026-07-28 the dry-run printed
    "would ABORT on --execute" and the execute deleted both torrents two minutes
    later. A preview that does not predict the real run is worse than none.

    Pinning the default at 100 means a bare dry-run behaves exactly like the
    armed run minus the mutation, with no flag needed to get a faithful preview.
    """
    # The invariant is PREVIEW == ARMED, not any particular number. The armed
    # drop-in passes only `--execute`, so whatever this default is governs both
    # paths -- which is the property that was broken when the threshold lived in
    # the drop-in alone.
    #
    # The value moved 100.0 -> 95.0 on council review: at 100.0 the breaker was
    # DEAD, not permissive, because the tripwire is `pct > max_pct` and
    # 100.0 > 100.0 is False for every pool size. It could never fire, which
    # mattered because the council also proved the *arr-queue rail is VACUOUS for
    # the deletable population.
    assert m.DEFAULT_MAX_PCT < 100.0, (
        "at 100.0 the tripwire `pct > max_pct` can never fire for any pool size "
        "-- that is a dead breaker, not a permissive one"
    )
    assert m.DEFAULT_MAX_PCT >= 90.0, (
        "too low and a legitimate full-pool reap trips it every run; this box "
        "routinely reaps its whole small pool once everything has seeded"
    )


def test_the_mass_delete_rail_is_not_the_percentage_cap(m):
    """Turning the cap off is only safe because a stronger guard exists.

    The scenario max-pct guarded -- everything suddenly looking untracked -- is
    caused by an unreadable *arr queue, and that aborts the run outright,
    independent of pool size. Pin that the guard is still documented and wired.
    """
    src = JANITOR.read_text(encoding="utf-8")
    assert "queue fetch fails" in src or "queue-fetch failure ABORTS" in src, \
        "the abort-if-any-arr-queue-unreadable rail is no longer documented"
    assert "EXIT_FATAL" in src, "the fatal abort path is gone"
