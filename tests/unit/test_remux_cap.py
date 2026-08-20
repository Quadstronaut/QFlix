"""Pins the remux cap's nested-items[] tree walk (58-remux-cap-enforce.py).

WHY THIS FILE EXISTS. Two separate failures converge on this walker.

1. The DIAGNOSIS failure, 2026-08-19. An audit agent read a Radarr quality
   profile's items[] NON-recursively and concluded profile 7 "HD Bluray + WEB"
   was the source of the remux wave. It was not: profile 7 does not allow remux
   and holds 1 movie. The culprit was profile 6 "HD 720p/1080p" (112 of 114
   movies), whose Remux-1080p entry sits at the TOP of a tree in which the
   sibling "WEB 1080p" entry is a GROUP carrying its own nested items[]. Any
   walker that treats items[] as flat reads the wrong allowed set. Every test
   below therefore uses the real nested shape.

2. The REPAIR failure this guards against. Disabling qualities is destructive
   to a profile: strip the last allowed entry and Radarr can never grab against
   it again, silently, forever. The cap must REFUSE such a profile, not empty
   it. That is the last test here and it is the one that matters most.

The live evidence behind the cap, measured 2026-08-19 against Radarr main:
one member's Plex client could not play a single movie from 2026-07-25 onward
while TV played fine on that same client. 23 of 46 movies-with-a-file were
Remux-1080p (20-37 Mbps, TrueHD/DTS-HD MA, 572 GB), and the client pins
targetBitrate 1927 kbps with videoDecision=transcode and no hardware
acceleration. (Identity stays out of this repo by operator directive.)

These are pure offline tests: no box, no network, no secrets. The module is
loaded by path because its filename starts with a digit and contains dashes.
"""
import importlib.util
import copy
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENFORCER = REPO / "scripts" / "configure" / "58-remux-cap-enforce.py"


def _load():
    spec = importlib.util.spec_from_file_location("remux_cap_enforce", ENFORCER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def m():
    return _load()


# ---------------------------------------------------------------------------
# Fixtures modelled on the LIVE profile shapes, dumped from Radarr main
# 2026-08-19. items[] is ordered worst-to-best; group ids are 1000+.
# ---------------------------------------------------------------------------
def _q(qid, name, allowed):
    return {"quality": {"id": qid, "name": name}, "items": [], "allowed": allowed}


def _grp(gid, name, kids, allowed=True):
    return {"id": gid, "name": name, "items": kids, "allowed": allowed}


def profile_6():
    """The real culprit: 'HD 720p/1080p', id 6, cutoff 6, upgradeAllowed false,
    Remux-1080p (id 30) allowed at the top."""
    return {
        "id": 6,
        "name": "HD 720p/1080p",
        "cutoff": 6,
        "upgradeAllowed": False,
        "items": [
            _q(4, "HDTV-720p", True),
            _grp(1001, "WEB 720p", [_q(5, "WEBDL-720p", True),
                                    _q(14, "WEBRip-720p", True)]),
            _q(6, "Bluray-720p", True),
            _q(9, "HDTV-1080p", True),
            _grp(1002, "WEB 1080p", [_q(3, "WEBDL-1080p", True),
                                     _q(15, "WEBRip-1080p", True)]),
            _q(7, "Bluray-1080p", True),
            _q(30, "Remux-1080p", True),
            _q(31, "Remux-2160p", False),
        ],
    }


def profile_7():
    """'HD Bluray + WEB', id 7. The profile the earlier audit WRONGLY blamed:
    it allows no remux tier at all."""
    return {
        "id": 7,
        "name": "HD Bluray + WEB",
        "cutoff": 7,
        "upgradeAllowed": True,
        "items": [
            _q(6, "Bluray-720p", True),
            _grp(1002, "WEB 1080p", [_q(3, "WEBDL-1080p", True),
                                     _q(15, "WEBRip-1080p", True)]),
            _q(7, "Bluray-1080p", True),
            _q(30, "Remux-1080p", False),
        ],
    }


def profile_remux_only():
    """Pathological: the ONLY allowed quality is a remux. Capping this would
    empty the profile, so the cap must refuse it."""
    return {
        "id": 99,
        "name": "Remux Only",
        "cutoff": 30,
        "upgradeAllowed": False,
        "items": [
            _q(7, "Bluray-1080p", False),
            _grp(1002, "WEB 1080p", [_q(3, "WEBDL-1080p", False),
                                     _q(15, "WEBRip-1080p", False)], allowed=False),
            _q(30, "Remux-1080p", True),
        ],
    }


def profile_remux_cutoff():
    """Cutoff points AT the remux tier, so capping must repair it."""
    p = profile_6()
    p["id"] = 60
    p["cutoff"] = 30
    return p


def profile_grouped_remux():
    """A remux nested INSIDE a group - the flat-read blind spot. The group also
    holds a non-remux sibling, so the group itself must survive."""
    return {
        "id": 61,
        "name": "Grouped Remux",
        "cutoff": 1003,
        "upgradeAllowed": True,
        "items": [
            _q(6, "Bluray-720p", True),
            _grp(1003, "Bluray Tier", [_q(7, "Bluray-1080p", True),
                                       _q(30, "Bluray-1080p Remux", True)]),
        ],
    }


def _allowed_names(mod, items):
    """Flatten to the set of allowed LEAF quality names, for assertions."""
    out = set()
    for it in items or []:
        if mod.is_group(it):
            out |= _allowed_names(mod, it.get("items"))
        elif it.get("allowed"):
            out.add(it["quality"]["name"])
    return out


# ---------------------------------------------------------------------------
# is_remux_name - the rule, not a name list.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", [
    "Remux-1080p", "Remux-2160p", "remux-1080p", "REMUX-2160P",
    "Bluray-1080p Remux", "Anime Remux-1080p",
])
def test_is_remux_name_matches_every_shipped_spelling(m, name):
    assert m.is_remux_name(name) is True


@pytest.mark.parametrize("name", [
    "Bluray-1080p", "WEBDL-1080p", "WEBRip-1080p", "HDTV-1080p",
    "Bluray-2160p", "SDTV", "DVD", "REGIONAL", "", None,
])
def test_is_remux_name_rejects_non_remux(m, name):
    assert m.is_remux_name(name) is False


# ---------------------------------------------------------------------------
# 1. Remux gets disabled - including one nested inside a group.
# ---------------------------------------------------------------------------
def test_remux_disabled_on_the_real_culprit_profile(m):
    p = profile_6()
    rep = m.apply_remux_cap(p)
    assert rep["changed"] is True
    assert rep["refused"] is False
    # Remux-2160p was already disabled, so only the 1080p entry toggles.
    assert rep["toggled"] == 1
    assert "Remux-1080p" not in _allowed_names(m, p["items"])
    assert 30 not in m.collect_allowed_quality_ids(p["items"])


def test_remux_nested_inside_a_group_is_found(m):
    """The flat-read blind spot: a non-recursive walker misses this entirely."""
    p = profile_grouped_remux()
    rep = m.apply_remux_cap(p)
    assert rep["toggled"] == 1
    names = _allowed_names(m, p["items"])
    assert "Bluray-1080p Remux" not in names
    # The group had a non-remux sibling, so the group must stay allowed.
    assert "Bluray-1080p" in names
    grp = p["items"][1]
    assert grp["allowed"] is True


def test_group_collapses_only_when_all_children_are_disabled(m):
    p = {
        "id": 70, "name": "Remux Group", "cutoff": 6,
        "items": [
            _q(6, "Bluray-720p", True),
            _grp(1004, "Remux Tier", [_q(30, "Remux-1080p", True),
                                      _q(31, "Remux-2160p", True)]),
        ],
    }
    rep = m.apply_remux_cap(p)
    assert rep["toggled"] == 2
    assert p["items"][1]["allowed"] is False        # group collapsed
    assert _allowed_names(m, p["items"]) == {"Bluray-720p"}


# ---------------------------------------------------------------------------
# 2. Non-remux entries are untouched.
# ---------------------------------------------------------------------------
def test_non_remux_qualities_are_untouched(m):
    p = profile_6()
    before = _allowed_names(m, p["items"]) - {"Remux-1080p"}
    m.apply_remux_cap(p)
    assert _allowed_names(m, p["items"]) == before
    assert before == {"HDTV-720p", "WEBDL-720p", "WEBRip-720p", "Bluray-720p",
                      "HDTV-1080p", "WEBDL-1080p", "WEBRip-1080p", "Bluray-1080p"}


def test_profile_without_remux_is_reported_unchanged(m):
    """Regression pin for the misattribution: profile 7 must come back as a
    no-op, so nobody can 'fix' it and think they fixed the wave."""
    p = profile_7()
    snapshot = copy.deepcopy(p)
    rep = m.apply_remux_cap(p)
    assert rep["changed"] is False
    assert rep["refused"] is False
    assert rep["toggled"] == 0
    assert p == snapshot


# ---------------------------------------------------------------------------
# 3. Cutoff repair.
# ---------------------------------------------------------------------------
def test_cutoff_pointing_at_remux_is_repaired(m):
    p = profile_remux_cutoff()
    rep = m.apply_remux_cap(p)
    assert rep["cutoff_repaired"] is True
    assert rep["cutoff_before"] == 30
    # Bluray-1080p (7) is the LAST still-allowed top-level entry.
    assert p["cutoff"] == 7
    assert rep["cutoff_after"] == 7
    assert p["cutoff"] in m.collect_allowed_ids(p["items"])


def test_cutoff_repair_does_not_pick_the_numerically_largest_id(m):
    """57's max(allowed) picks group 1002 ('WEB 1080p'), silently downgrading a
    Bluray cutoff to WEB, because group ids are 1000+. items[] is ordered
    worst-to-best, so position is the oracle - not the id."""
    p = profile_remux_cutoff()
    m.apply_remux_cap(p)
    assert max(m.collect_allowed_ids(p["items"])) == 1002    # what 57 would pick
    assert p["cutoff"] == 7                                  # what we pick


def test_valid_cutoff_is_left_alone(m):
    """The live case: profile 6's cutoff is 6 (Bluray-720p), still allowed."""
    p = profile_6()
    rep = m.apply_remux_cap(p)
    assert rep["cutoff_repaired"] is False
    assert p["cutoff"] == 6


def test_cutoff_may_reference_a_group_when_that_group_is_the_best_left(m):
    p = {
        "id": 71, "name": "Group Top", "cutoff": 30,
        "items": [
            _q(6, "Bluray-720p", True),
            _grp(1002, "WEB 1080p", [_q(3, "WEBDL-1080p", True),
                                     _q(15, "WEBRip-1080p", True)]),
            _q(30, "Remux-1080p", True),
        ],
    }
    m.apply_remux_cap(p)
    assert p["cutoff"] == 1002


def test_cutoff_on_an_emptied_group_is_repaired(m):
    """The group the cutoff pointed at lost every child, so the group id is no
    longer a legal cutoff even though its own allowed flag says True."""
    p = {
        "id": 72, "name": "Remux Group Cutoff", "cutoff": 1004,
        "items": [
            _q(6, "Bluray-720p", True),
            _grp(1004, "Remux Tier", [_q(30, "Remux-1080p", True)]),
        ],
    }
    m.apply_remux_cap(p)
    assert 1004 not in m.collect_allowed_ids(p["items"])
    assert p["cutoff"] == 6


# ---------------------------------------------------------------------------
# 4. Idempotence - the whole point of "safe to re-run".
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("factory_name", [
    "profile_6", "profile_7", "profile_remux_cutoff",
    "profile_grouped_remux", "profile_remux_only",
])
def test_second_pass_is_a_no_op(m, factory_name):
    p = globals()[factory_name]()
    m.apply_remux_cap(p)
    settled = copy.deepcopy(p)

    rep2 = m.apply_remux_cap(p)
    assert rep2["changed"] is False
    assert rep2["toggled"] == 0
    assert rep2["cutoff_repaired"] is False
    assert p == settled, "second pass mutated an already-capped profile"

    # And a third, because "idempotent" means fixed point, not just stable once.
    m.apply_remux_cap(p)
    assert p == settled


# ---------------------------------------------------------------------------
# 5. THE SAFETY RAIL: refuse, never empty.
# ---------------------------------------------------------------------------
def test_profile_that_would_be_emptied_is_refused_not_emptied(m):
    p = profile_remux_only()
    snapshot = copy.deepcopy(p)

    rep = m.apply_remux_cap(p)

    assert rep["refused"] is True
    assert rep["changed"] is False, "a refused profile must never be PUT"
    assert rep["toggled"] == 0
    assert "zero allowed qualities" in rep["reason"].lower()
    # Byte-for-byte untouched: partial stripping is as broken as full stripping.
    assert p == snapshot
    assert m.collect_allowed_quality_ids(p["items"]) == {30}
    assert p["cutoff"] == 30


def test_refusal_survives_a_remux_nested_in_the_only_allowed_group(m):
    p = {
        "id": 98, "name": "Only A Remux Group", "cutoff": 1005,
        "items": [
            _q(6, "Bluray-720p", False),
            _grp(1005, "Remux Tier", [_q(30, "Remux-1080p", True)]),
        ],
    }
    snapshot = copy.deepcopy(p)
    rep = m.apply_remux_cap(p)
    assert rep["refused"] is True
    assert p == snapshot
    assert p["items"][1]["allowed"] is True
    assert p["items"][1]["items"][0]["allowed"] is True


def test_emptiness_oracle_ignores_a_group_flag_with_no_allowed_child(m):
    """collect_allowed_quality_ids must count LEAVES only. If it counted group
    flags, a profile whose sole 'allowed' entry is an empty group would look
    populated and the refusal would not fire."""
    items = [_grp(1006, "Empty Tier", [_q(3, "WEBDL-1080p", False)], allowed=True)]
    assert m.collect_allowed_quality_ids(items) == set()
    assert m.collect_allowed_ids(items) == set()
    assert m.highest_allowed_id(items) is None


# ---------------------------------------------------------------------------
# Scope pin: every instance EXCEPT sonarr2, and sonarr2's absence is structural.
# ---------------------------------------------------------------------------
def test_scope_covers_every_instance_recyclarr_does_not_own(m):
    """Widened 2026-08-20 after both original exclusions failed within a day.

    The first version pinned {"radarr"} and argued both omissions honestly:
    radarr2 held "exactly 1" remux so it was blast radius rather than a wave,
    and sonarr main was ARMED but left for a separately-revertable change.
    Then the library-container-sanity size leg named that single radarr2 file
    on its first run - "Cowboy Bebop: The Movie", 35.9 GB, the same
    unplayable-on-a-capped-client shape in a library served to the same
    members - and sonarr main was still loaded. A hazard that is written down
    and left loaded still fires.

    sonarr2 stays out for a reason that is NOT caution: profile 7 "[Anime]
    Remux-1080p" IS recyclarr TRaSH template
    20e0fc959f1f1704bed501f23bdae76f, bound in 56-recyclarr-install.sh under
    sonarr:anime. Capping it here would be reverted on the next sync while
    this script kept passing its own re-run check - a script that lies about
    prod. That belongs in the recyclarr config or nowhere.
    """
    assert set(m.ARRS) == {"radarr", "radarr2", "sonarr"}
    assert "sonarr2" not in m.ARRS, (
        "sonarr2 profile 7 is recyclarr-owned; capping it here drifts back")


# ===========================================================================
# PART 2 - scripts/maint/qflix-remux-regrab.py, the DESTRUCTIVE half.
#
# 58 (above) changes policy; the regrab deletes files. Everything below pins a
# defect the 2026-08-19 review proved by execution, not by reading:
#
#   * a failed MoviesSearch was documented as self-healing "because targets are
#     re-derived live". It cannot: step 1 deletes the file, so hasFile goes
#     false, so the movie stops being a remux target and is never searched
#     again. The repair is a journal, and these tests pin the journal.
#   * `--execute --max-items -1` sliced targets[:-1] and issued 22 DELETEs
#     against a 23-movie target set. A cap that widens the blast radius.
#   * an empty /qualityprofile response folded into the "run 58 first" refusal,
#     which is a confident wrong instruction derived from no data at all.
#
# Offline: the module is imported by path, and nothing here touches Radarr.
# ===========================================================================
REGRAB = REPO / "scripts" / "maint" / "qflix-remux-regrab.py"
SMOKE = REPO / "scripts" / "smoke-test.sh"


def _load_regrab():
    spec = importlib.util.spec_from_file_location("qflix_remux_regrab", REGRAB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rg():
    return _load_regrab()


@pytest.fixture
def journal(tmp_path, monkeypatch, rg):
    """Point the durable state dir at tmp_path so the journal helpers are
    exercised for real (atomic replace included) with no box."""
    monkeypatch.setenv("QFLIX_REMUX_REGRAB_LOG_DIR", str(tmp_path))
    return tmp_path / rg.PENDING_NAME


class FakeArr:
    """Records MoviesSearch calls. `poison` is the set of movie_ids whose
    search raises - a batch containing any of them fails as a whole, which is
    how Radarr's command endpoint behaves on a bad id."""

    def __init__(self, poison=()):
        self.poison = set(poison)
        self.calls = []

    def movies_search(self, movie_ids):
        ids = list(movie_ids)
        self.calls.append(ids)
        if self.poison.intersection(ids):
            raise RuntimeError("HTTP 400 bad movieIds")
        return {"id": len(self.calls)}


def _row(mid, title="T"):
    return {"movie_id": mid, "title": title, "queued_at": ""}


# ---------------------------------------------------------------------------
# MINOR 1 - --max-items can never widen the blast radius.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["-1", "0", "-23", "abc", "1.5"])
def test_max_items_parser_rejects_anything_below_one(rg, bad):
    with pytest.raises(SystemExit):
        rg.build_parser().parse_args(["--max-items", bad])


@pytest.mark.parametrize("good,expected", [("1", 1), ("10", 10), ("23", 23)])
def test_max_items_parser_accepts_real_caps(rg, good, expected):
    assert rg.build_parser().parse_args(["--max-items", good]).max_items == expected


def test_negative_cap_can_never_widen_the_blast_radius(rg):
    """The proven bug, expressed as the slice that caused it. 23 targets is the
    live count measured against Radarr main on 2026-08-19."""
    targets = [_row(i) for i in range(23)]
    assert len(targets[:-1]) == 22, "the old path: a -1 cap deleted 22 of 23"
    for hostile in (-1, 0, -23):
        cap = rg.effective_max_items(hostile)
        assert cap == 1
        assert len(targets[:cap]) == 1


def test_effective_max_items_passes_real_caps_through(rg):
    assert rg.effective_max_items(10) == 10
    assert rg.effective_max_items("3") == 3
    # Garbage falls back to the documented default, never to "unlimited".
    assert rg.effective_max_items(None) == rg.DEFAULT_MAX_ITEMS


# ---------------------------------------------------------------------------
# MAJOR 1 - the deleted movie is journalled, because it can never be re-derived.
# ---------------------------------------------------------------------------
def test_a_deleted_movie_is_no_longer_a_target(rg):
    """The whole reason the journal has to exist. Same movie, before and after
    the delete: it drops out of the derivation entirely, so "re-run and it
    self-heals" was false."""
    movie = {"id": 1, "title": "M", "year": 2024, "qualityProfileId": 6,
             "hasFile": True,
             "movieFile": {"id": 11, "size": 30 * 1024 ** 3,
                           "dateAdded": "2026-07-05T17:47:00Z",
                           "quality": {"quality": {"id": 30,
                                                   "name": "Remux-1080p"}}}}
    targets, skipped = rg.select_targets([movie], set(), {6})
    assert [r["movie_id"] for r in targets] == [1]

    after = dict(movie, hasFile=False, movieFile=None)
    targets2, skipped2 = rg.select_targets([after], set(), {6})
    assert targets2 == [] and skipped2 == []


def test_batched_search_success_leaves_nothing_pending(rg):
    arr = FakeArr()
    remaining, searched = rg.issue_searches(arr, [_row(1), _row(2), _row(3)])
    assert remaining == []
    assert searched == [1, 2, 3]
    assert arr.calls == [[1, 2, 3]], "one batch, not N calls, on the happy path"


def test_one_poisoned_id_cannot_strand_the_rest(rg):
    """Batch fails -> per-movie retry. 9 of 10 must still get searched."""
    rows = [_row(i) for i in range(10)]
    arr = FakeArr(poison={7})
    remaining, searched = rg.issue_searches(arr, rows)
    assert searched == [0, 1, 2, 3, 4, 5, 6, 8, 9]
    assert [r["movie_id"] for r in remaining] == [7]
    assert arr.calls[0] == list(range(10))          # the batch attempt
    assert len(arr.calls) == 11                     # batch + 10 singles


def test_total_search_outage_keeps_every_id_pending(rg, journal):
    rows = [_row(1), _row(2)]
    arr = FakeArr(poison={1, 2})
    remaining, searched = rg.issue_searches(arr, rows)
    assert searched == []
    assert [r["movie_id"] for r in remaining] == [1, 2]

    # The caller persists exactly `remaining`, and the NEXT run reads it back.
    rg.save_pending(remaining)
    assert journal.exists()
    assert [r["movie_id"] for r in rg.load_pending()] == [1, 2]


def test_journal_round_trip_and_drain(rg, journal):
    rg.save_pending([_row(4, "Four"), _row(5, "Five")])
    back = rg.load_pending()
    assert [r["movie_id"] for r in back] == [4, 5]
    assert back[0]["title"] == "Four"

    arr = FakeArr()
    remaining, searched = rg.issue_searches(arr, back)
    rg.save_pending(remaining)
    assert searched == [4, 5]
    assert rg.load_pending() == [], "a drained journal must not replay forever"


def test_missing_journal_reads_as_empty(rg, journal):
    assert not journal.exists()
    assert rg.load_pending() == []


def test_garbage_journal_degrades_instead_of_raising(rg, journal):
    """An unreadable journal must not abort a run whose deletes already
    happened - it warns and continues."""
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{not json", encoding="utf-8")
    assert rg.load_pending() == []
    journal.write_text('{"pending": [{"title": "no id"}, 7, null]}',
                       encoding="utf-8")
    assert rg.load_pending() == []


def test_merge_pending_is_a_union_not_a_replace(rg):
    """A run that deletes new movies must not drop the backlog it inherited."""
    merged = rg.merge_pending([_row(1), _row(2)], [_row(2), _row(3)])
    assert [r["movie_id"] for r in merged] == [1, 2, 3]


def test_header_no_longer_claims_the_search_self_heals(rg):
    src = REGRAB.read_text(encoding="utf-8")
    assert "targets are re-derived live each time" not in src
    assert "pending-search" in src.lower()
    assert "PENDING_NAME" in src


# ---------------------------------------------------------------------------
# MINOR 2 - unlinked vs freed is a PROPERTY OF THE FILE, not of the config.
#
# The first version of this guard pinned an unconditional "unlinked, never
# freed" claim, because Radarr main runs copyUsingHardlinks=true. That flag
# describes how files ARRIVE; it says nothing about whether a given file still
# has its seeding twin. On the 2026-08-20 run every one of the 23 targets was
# st_nlink == 1 and the quota went 2231G -> 1658G at delete time, so the
# warning was not merely imprecise -- it pointed the operator's capacity
# planning at a peak that could not happen. A test that pins the wrong sentence
# keeps the wrong sentence. These pin the measurement instead.
# ---------------------------------------------------------------------------
def test_byte_verdict_is_measured_from_link_counts(rg):
    src = REGRAB.read_text(encoding="utf-8")
    # Targets carry a MEASURED link count, and it comes from stat(), not config.
    assert '"nlink": _nlink(' in src
    assert "os.stat(path).st_nlink" in src
    # The pending-reap clause still exists, but only as one branch of a verdict.
    assert "qflix-torrent-janitor" in src
    assert "ratio>=2.0" in src
    # No unconditional byte claim survives at either report site.
    assert "0 bytes come back until" not in src
    # The phrase survives exactly once, in the _bytes_verdict docstring that
    # records WHY the unconditional version was wrong. Zero occurrences would
    # mean the reasoning was deleted along with the bug.
    assert src.count("space reclaims") == 1
    assert "log(\"note: the delete UNLINKS one hardlink" not in src


def test_byte_verdict_buckets_by_measured_nlink(rg):
    """nlink 1 => freed now; nlink >= 2 => pending reap; None => unknown."""
    verdict = rg._bytes_verdict([
        {"size_gb": 10.0, "nlink": 1},
        {"size_gb": 4.0, "nlink": 2},
        {"size_gb": 1.0, "nlink": None},
    ])
    assert "freed 10.0 GB now" in verdict
    assert "unlinked 4.0 GB pending" in verdict
    assert "1.0 GB of unstat-able files" in verdict


def test_byte_verdict_omits_buckets_that_are_empty(rg):
    """The all-nlink-1 case must not print a torrent-janitor clause at all --
    that is the exact noise the 2026-08-20 run exposed."""
    verdict = rg._bytes_verdict([{"size_gb": 572.16, "nlink": 1}])
    assert verdict == "freed 572.16 GB now (no seeding twin)"
    assert "torrent-janitor" not in verdict

    pending = rg._bytes_verdict([{"size_gb": 30.0, "nlink": 3}])
    assert "torrent-janitor" in pending
    assert "freed" not in pending

    assert rg._bytes_verdict([]) == "0 GB"


def test_nlink_never_raises_on_a_bad_path(rg):
    assert rg._nlink(None) is None
    assert rg._nlink("") is None
    assert rg._nlink("/nonexistent/definitely/not/here.mkv") is None


# ---------------------------------------------------------------------------
# MINOR 3 - the two skip classes are not the same instruction.
# ---------------------------------------------------------------------------
def test_skip_classes_are_distinguished(rg):
    def mv(mid, pid):
        return {"id": mid, "title": "M" + str(mid), "year": 2024,
                "qualityProfileId": pid, "hasFile": True,
                "movieFile": {"id": mid * 10, "size": 1,
                              "dateAdded": "2026-08-0" + str(mid) + "T00:00:00Z",
                              "quality": {"quality": {"id": 30,
                                                      "name": "Remux-1080p"}}}}

    targets, skipped = rg.select_targets([mv(1, 6), mv(2, 42)], {6}, {7})
    assert targets == []
    classes = {r["movie_id"]: r["skip_class"] for r in skipped}
    assert classes == {1: "uncapped_profile", 2: "unknown_profile"}
    by_id = {r["movie_id"]: r["reason"] for r in skipped}
    assert "58-remux-cap-enforce" in by_id[1]
    assert "not found" in by_id[2]


# ---------------------------------------------------------------------------
# MINOR 3 (second half) - an unreadable profile list is FATAL, not a refusal.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [[], None, {}, "", {"error": "unauthorized"}])
def test_empty_or_non_list_profiles_is_fatal_not_refused(rg, bad):
    with pytest.raises(ValueError):
        rg.split_profiles(bad)


def test_split_profiles_recurses_into_groups(rg):
    """Same nested-tree blind spot as 58: a grouped remux must classify the
    profile as still-allowing, or its movies become "targets" and get deleted
    only to be re-grabbed as remux."""
    remux_ids, capped_ids = rg.split_profiles([profile_grouped_remux(),
                                               profile_7()])
    assert remux_ids == {61}
    assert capped_ids == {7}


# ---------------------------------------------------------------------------
# MAJOR 2 - 58 has a live verification surface, and it recurses.
# ---------------------------------------------------------------------------
def test_smoke_test_has_a_remux_cap_gate():
    smoke = SMOKE.read_text(encoding="utf-8")
    assert "remux-cap-radarr" in smoke, "58 needs a re-read gate, like 57's"
    gate = smoke.split("# 13n.")[1].split("# 13. Listmonk")[0]
    assert "qualityprofile" in gate
    assert "remux" in gate.lower()
    # Recursion is the load-bearing half: measured on the box 2026-08-19,
    # sonarr2 reads flat=0 / recursive=1, so a copy of 13f's non-recursive walk
    # would pass a profile that allows remux.
    assert 'w(i.get(\\"items\\"))' in gate


def test_58_header_points_at_that_gate():
    src = ENFORCER.read_text(encoding="utf-8")
    assert "smoke-test.sh" in src and "13n" in src


# ===========================================================================
# PART 3 - scripts/configure/59-brdisk-block.py, the FULL-DISC block.
#
# 58 (Part 1) capped Remux. The next day the re-grab it triggered pulled a
# 44.38 GiB BD-50 .iso into the movie library, because:
#
#   * the release NAME said "1080p Blu-ray" and parsed as Bluray-1080p, which
#     IS allowed - so neither the quality profile nor Radarr's own
#     RawDiskSpecification (both of which key off the PARSED name) could fire;
#   * Radarr re-graded the payload BR-DISK on import from the .iso extension
#     and imported it anyway;
#   * the import decision engine was then measured directly: with the BR-DISK
#     custom format at -10000 and minFormatScore 0, GET /manualimport returned
#     customFormatScore -10000 and rejections []. There is no import-side gate.
#
# So every lever 59 arms is a GRAB-time lever, and the two that matter are an
# absolute size ceiling (catches a MISLABELLED payload - the actual bug) and a
# custom-format score (catches a disc-shaped TITLE). These tests pin the pure
# logic behind both, plus the measured grab corpus that proves the ceiling does
# not reject anything the profiles still allow.
#
# Offline: the module is imported by path, and nothing here touches Radarr.
# ===========================================================================
import ast

BRDISK = REPO / "scripts" / "configure" / "59-brdisk-block.py"


def _load_brdisk():
    spec = importlib.util.spec_from_file_location("brdisk_block", BRDISK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bd():
    return _load_brdisk()


def _profile(pid=6, name="HD 720p/1080p", brdisk_score=0, min_score=0,
             include_brdisk=True):
    """Shape dumped from Radarr main 2026-08-20: 40 formatItems, all at 0,
    minFormatScore 0. The BR-DISK entry is one of forty, which is exactly why
    it went unnoticed for as long as it did."""
    items = [{"format": 21, "name": "x265 (HD)", "score": 0},
             {"format": 17, "name": "LQ", "score": 0}]
    if include_brdisk:
        items.insert(1, {"format": 14, "name": "BR-DISK",
                         "score": brdisk_score})
    return {"id": pid, "name": name, "minFormatScore": min_score,
            "formatItems": items}


# ---------------------------------------------------------------------------
# LEVER 1 - the size ceiling. needs_size_cap decides whether to WRITE.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("current", [0, None, "", "0"])
def test_unset_maximum_size_is_always_capped(bd, current):
    """Radarr logged "Maximum size is not set." for every release for years.
    0 is not a large limit, it is NO limit."""
    assert bd.needs_size_cap(current, 25000) is True


def test_a_looser_existing_ceiling_is_tightened(bd):
    assert bd.needs_size_cap(60000, 25000) is True


@pytest.mark.parametrize("current", [25000, 24999, 20000, 1])
def test_an_equal_or_tighter_ceiling_is_left_alone(bd, current):
    """THE SAFETY RAIL. An operator who hand-set 20000 chose something stricter
    than policy; an enforcer that raises it back to 25000 is a tool for
    defeating the policy it claims to enforce."""
    assert bd.needs_size_cap(current, 25000) is False


def test_a_none_target_never_writes(bd):
    """Sonarr's policy value. maximumSize is per RELEASE and a season pack
    dwarfs any movie, so the correct Sonarr ceiling is no ceiling."""
    assert bd.needs_size_cap(0, None) is False
    assert bd.needs_size_cap(999999, None) is False


def test_unparseable_current_value_is_treated_as_unset(bd):
    assert bd.needs_size_cap("garbage", 25000) is True


def test_apply_size_cap_writes_and_is_idempotent(bd):
    cfg = {"maximumSize": 0, "id": 1}
    rep = bd.apply_size_cap(cfg, 25000)
    assert rep["changed"] is True
    assert (rep["before"], rep["after"]) == (0, 25000)
    assert cfg["maximumSize"] == 25000

    settled = copy.deepcopy(cfg)
    rep2 = bd.apply_size_cap(cfg, 25000)
    assert rep2["changed"] is False
    assert cfg == settled


def test_apply_size_cap_never_loosens(bd):
    cfg = {"maximumSize": 12000, "id": 1}
    rep = bd.apply_size_cap(cfg, 25000)
    assert rep["changed"] is False
    assert cfg["maximumSize"] == 12000


def test_sonarr_config_is_never_touched(bd):
    cfg = {"maximumSize": 0, "id": 1}
    rep = bd.apply_size_cap(cfg, None)
    assert rep["changed"] is False
    assert cfg["maximumSize"] == 0
    assert "uncapped" in rep["reason"]


# ---------------------------------------------------------------------------
# release_exceeds_cap - the unit conversion, isolated because it is the part
# everyone gets wrong. Radarr uses MiB and prints the result labelled "GB".
# ---------------------------------------------------------------------------
def test_cap_is_mebibytes_not_megabytes(bd):
    """25000 MiB is 26,214,400,000 bytes. Read it as 25,000,000,000 and the
    ceiling is 4.9 percent tighter than intended - invisible until it rejects
    something wanted."""
    assert bd.release_exceeds_cap(26214400000, 25000) is False
    assert bd.release_exceeds_cap(26214400001, 25000) is True
    assert bd.release_exceeds_cap(25000000000, 25000) is False


def test_cap_boundary_is_strictly_greater(bd):
    """Radarr rejects on `size > max`, so a release exactly on the ceiling is
    accepted. An off-by-one here silently rejects an edge release."""
    exact = 25000 * 1024 * 1024
    assert bd.release_exceeds_cap(exact, 25000) is False
    assert bd.release_exceeds_cap(exact + 1, 25000) is True


@pytest.mark.parametrize("cap", [0, None])
def test_no_cap_never_rejects_anything(bd, cap):
    assert bd.release_exceeds_cap(99000000000, cap) is False


@pytest.mark.parametrize("size", [None, "", "garbage"])
def test_unreadable_size_is_not_treated_as_oversize(bd, size):
    """Fail OPEN on a missing size. A gate that rejects on absent data blocks
    the whole library the first time an indexer omits a field."""
    assert bd.release_exceeds_cap(size, 25000) is False


# ---------------------------------------------------------------------------
# THE FALSE-POSITIVE PROOF. Measured grab corpus, radarr main, last 40 grabs
# read live 2026-08-20 - sizes in bytes with the quality Radarr assigned.
#
# "Legit" means STILL ALLOWED BY POLICY. Seven of these are Remux-1080p, a tier
# 58 banned the day before, and they run up to 25,471 MiB - above the ceiling.
# That is correct behaviour, not a false positive, and pinning it here stops
# someone reading the unsplit history and "fixing" the ceiling upward.
# ---------------------------------------------------------------------------
RADARR_GRAB_CORPUS = [
    (51311448000, "Bluray-1080p"),   # THE INCIDENT: a BD-50 .iso mislabelled
    (26708050000, "Remux-1080p"),
    (24283925000, "Remux-1080p"),
    (23231527000, "Remux-1080p"),
    (22661469000, "Remux-1080p"),
    (22570053140, "Remux-1080p"),
    (20701742366, "Remux-1080p"),
    (19307565728, "Remux-1080p"),
    (18858470509, "Bluray-1080p"),   # largest STILL-ALLOWED grab: 17,985 MiB
    (18777786000, "Bluray-1080p"),
    (18146236825, "Bluray-1080p"),
    (17684223000, "Bluray-1080p"),
    (17684223000, "Bluray-1080p"),
    (15802294272, "Bluray-1080p"),
    (15193446809, "Bluray-1080p"),
    (15161234554, "Bluray-1080p"),
    (15089819922, "Bluray-1080p"),
    (14237816586, "Bluray-1080p"),
    (13973500000, "Bluray-1080p"),
    (13292923781, "Bluray-1080p"),
    (12455405158, "Bluray-1080p"),
    (11725260718, "Bluray-1080p"),
    (11628623953, "Bluray-1080p"),
    (11326905709, "Bluray-1080p"),
    (11209864642, "Bluray-1080p"),
    (10887742095, "Bluray-1080p"),
    (10656795000, "Bluray-1080p"),
    (8525510082, "Bluray-1080p"),
    (8214124953, "Bluray-1080p"),
    (7734834000, "WEBDL-1080p"),
    (7259501000, "Bluray-1080p"),
    (6141620373, "WEBDL-1080p"),
    (6077378723, "Bluray-1080p"),
    (6055903887, "Bluray-1080p"),
    (5637144576, "Bluray-1080p"),
    (5174003000, "Bluray-1080p"),
    (2394444267, "WEBRip-1080p"),
    (2072321720, "WEBRip-1080p"),
    (1750199173, "WEBRip-1080p"),
    (1632087572, "WEBRip-1080p"),
]

RADARR_CAP = 25000
RADARR2_CAP = 42000
# radarr2 still ALLOWS Remux-1080p by deliberate decision (58's SCOPE block),
# and this is its largest grab - 34,930 MiB. Its ceiling has to clear it.
RADARR2_LARGEST_LEGIT = 36627251000


def _still_allowed(corpus):
    """Remux is banned on radarr main by 58, so those rows are not candidates
    for a false positive. Everything else is."""
    return [(s, q) for s, q in corpus if "remux" not in q.lower()]


def test_ceiling_rejects_the_incident_release(bd):
    """The whole point. 48,934 MiB against a 25,000 MiB ceiling."""
    size, quality = RADARR_GRAB_CORPUS[0]
    assert quality == "Bluray-1080p", "it was graded ALLOWED at grab time"
    assert bd.release_exceeds_cap(size, RADARR_CAP) is True


def test_zero_false_positives_across_the_measured_grab_corpus(bd):
    """Every grab the profiles still allow must survive the ceiling."""
    allowed = _still_allowed(RADARR_GRAB_CORPUS)
    assert len(allowed) == 33, "corpus shape changed - re-derive the ceiling"
    rejected = [(s, q) for s, q in allowed
                if bd.release_exceeds_cap(s, RADARR_CAP)]
    # The incident release is the ONLY still-allowed-quality row over the line.
    assert rejected == [RADARR_GRAB_CORPUS[0]]


def test_headroom_over_the_largest_still_allowed_grab(bd):
    """1.39x. Pinned as a number so a future ceiling change has to confront
    what it costs, rather than being nudged by feel."""
    largest = max(s for s, q in _still_allowed(RADARR_GRAB_CORPUS)
                  if s != RADARR_GRAB_CORPUS[0][0])
    assert largest == 18858470509
    assert bd.release_exceeds_cap(largest, RADARR_CAP) is False
    headroom = (RADARR_CAP * 1024 * 1024) / largest
    assert 1.35 < headroom < 1.45


def test_radarr2_ceiling_clears_its_largest_remux(bd):
    """radarr2 keeps Remux-1080p, so its ceiling is set above a real remux and
    only catches the BD-50 disc class. Applying radarr main's 25000 here would
    reject a wanted file - that is why the two numbers differ."""
    assert bd.release_exceeds_cap(RADARR2_LARGEST_LEGIT, RADARR2_CAP) is False
    assert bd.release_exceeds_cap(RADARR2_LARGEST_LEGIT, RADARR_CAP) is True
    # It still stops what actually landed.
    assert bd.release_exceeds_cap(RADARR_GRAB_CORPUS[0][0], RADARR2_CAP) is True


def test_documented_residual_a_bd25_slips_under_both_ceilings(bd):
    """Honesty pin. The header claims a mislabelled single-layer BD-25 gets
    through BOTH instances; if someone later tightens a ceiling, this test
    fails and the header has to be corrected with it."""
    bd25 = 23 * 1024 ** 3          # ~23.0 GiB, a full single-layer disc
    assert bd.release_exceeds_cap(bd25, RADARR_CAP) is False
    assert bd.release_exceeds_cap(bd25, RADARR2_CAP) is False
    bd50 = 44 * 1024 ** 3          # what actually landed
    assert bd.release_exceeds_cap(bd50, RADARR_CAP) is True
    assert bd.release_exceeds_cap(bd50, RADARR2_CAP) is True


# ---------------------------------------------------------------------------
# The one live library file the ceiling would have blocked. Found BY the
# library scan on the first armed run, not by the corpus - the 40-grab window
# does not reach back to 2026-07-12. Pinned because the next person to see
# "Interstellar rejected" will otherwise assume the ceiling is broken.
# ---------------------------------------------------------------------------
INTERSTELLAR_FILE_BYTES = 39970613377      # 38,118 MiB, graded Bluray-1080p
INTERSTELLAR_RUNTIME_MIN = 169


def test_the_one_allowed_quality_library_file_over_the_ceiling(bd):
    """Graded Bluray-1080p (profile-allowed) yet over the ceiling. Deliberate:
    at ~31.5 Mbps it is a remux-class bitrate under a rip's name, which is the
    exact class 58 exists to remove and the exact thing the 1927 kbps client
    cannot play. 58 filters that class by NAME, this filters it by SIZE."""
    assert bd.release_exceeds_cap(INTERSTELLAR_FILE_BYTES, RADARR_CAP) is True
    mbps = INTERSTELLAR_FILE_BYTES * 8 / (INTERSTELLAR_RUNTIME_MIN * 60) / 1e6
    assert 30 < mbps < 33, "if the bitrate premise changes, revisit the ceiling"
    # A normal 1080p rip of the same film is nowhere near the ceiling.
    assert bd.release_exceeds_cap(14 * 1024 ** 3, RADARR_CAP) is False


def test_that_file_is_reported_as_oversize_and_not_as_a_disc(bd):
    """The two reasons must stay distinguishable. Folding them into one string
    would make an honest 31 Mbps x265 rip read as a disc image in the report."""
    found = bd.disc_offenders(
        [_movie(412, "Bluray-1080p", INTERSTELLAR_FILE_BYTES)], RADARR_CAP)
    assert found[0]["reasons"] == ["over the 25000 MiB ceiling"]
    assert "disc-class quality" not in found[0]["reasons"]


def test_59_header_discloses_that_blocked_library_file(bd):
    """Honesty pin. The corpus test proves zero false positives across the
    40-grab window; this proves the header does not stop there and pretend the
    window is the whole history."""
    src = BRDISK.read_text(encoding="utf-8")
    assert "Interstellar" in src
    assert "38,118 MiB" in src


def test_instance_ceilings_match_the_tested_numbers(bd):
    """Binds the corpus proof above to the values actually shipped."""
    assert bd.INSTANCES["radarr"]["max_size_mb"] == RADARR_CAP
    assert bd.INSTANCES["radarr2"]["max_size_mb"] == RADARR2_CAP
    assert bd.INSTANCES["sonarr"]["max_size_mb"] is None
    assert "sonarr2" not in bd.INSTANCES


# ---------------------------------------------------------------------------
# LEVER 2 - the custom-format score, and the minFormatScore that can kill it.
# ---------------------------------------------------------------------------
def test_format_score_reads_the_named_format_out_of_forty(bd):
    assert bd.format_score(_profile(brdisk_score=0), "BR-DISK") == 0
    assert bd.format_score(_profile(include_brdisk=False), "BR-DISK") is None


def test_a_zero_scored_block_is_armed(bd):
    p = _profile(brdisk_score=0)
    rep = bd.apply_disc_block(p)
    assert rep["changed"] is True
    assert rep["score_changed"] is True
    assert (rep["score_before"], rep["score_after"]) == (0, -10000)
    assert bd.format_score(p, "BR-DISK") == -10000


def test_an_already_blocked_profile_is_a_no_op(bd):
    """recyclarr writes exactly -10000 on the profiles it manages. Matching
    that number is what makes writing there harmless."""
    p = _profile(brdisk_score=-10000)
    snapshot = copy.deepcopy(p)
    rep = bd.apply_disc_block(p)
    assert rep["changed"] is False
    assert p == snapshot


def test_a_stricter_hand_set_score_is_preserved(bd):
    p = _profile(brdisk_score=-20000)
    rep = bd.apply_disc_block(p)
    assert rep["changed"] is False
    assert bd.format_score(p, "BR-DISK") == -20000


def test_a_profile_without_the_format_is_reported_absent_not_mutated(bd):
    """sonarr2's shape. Nothing to score, so nothing may be invented."""
    p = _profile(include_brdisk=False)
    snapshot = copy.deepcopy(p)
    rep = bd.apply_disc_block(p)
    assert rep["absent"] is True
    assert rep["changed"] is False
    assert p == snapshot


def test_absent_format_blocks_the_unrelated_min_score_repair(bd):
    """A profile carrying no BR-DISK format holds no gate of ours, so a hostile
    minFormatScore there is not ours to rewrite. The early return also stops a
    mutation the caller would never PUT - a silent in-memory-only edit that
    reads as a fix in the report and changes nothing on the box."""
    p = _profile(include_brdisk=False, min_score=-10000)
    snapshot = copy.deepcopy(p)
    rep = bd.apply_disc_block(p)
    assert rep["absent"] is True
    assert rep["changed"] is False
    assert rep["min_repaired"] is False
    assert p == snapshot
    assert p["minFormatScore"] == -10000


@pytest.mark.parametrize("min_score,dead", [
    (0, False), (100, False), (-1, False), (-9999, False),
    (-10000, True), (-20000, True), (None, False),
])
def test_min_format_score_neutralisation_is_detected(bd, min_score, dead):
    """Radarr rejects when total score < minFormatScore. At minFormatScore
    -10000 a -10000 block stops blocking, while every field still reads correct
    in the UI. One number away, always."""
    assert bd.min_format_score_is_dead(min_score, -10000) is dead


def test_a_neutralised_profile_is_repaired_even_when_the_score_is_right(bd):
    p = _profile(brdisk_score=-10000, min_score=-10000)
    rep = bd.apply_disc_block(p)
    assert rep["score_changed"] is False
    assert rep["min_repaired"] is True
    assert rep["changed"] is True, "the profile must still be PUT"
    assert p["minFormatScore"] == 0


def test_both_repairs_can_fire_on_one_profile(bd):
    p = _profile(brdisk_score=0, min_score=-30000)
    rep = bd.apply_disc_block(p)
    assert rep["score_changed"] is True and rep["min_repaired"] is True
    assert bd.format_score(p, "BR-DISK") == -10000
    assert p["minFormatScore"] == 0


@pytest.mark.parametrize("kwargs", [
    {"brdisk_score": 0},
    {"brdisk_score": -10000},
    {"brdisk_score": 0, "min_score": -10000},
    {"include_brdisk": False},
])
def test_disc_block_reaches_a_fixed_point(bd, kwargs):
    """Idempotent means fixed point, not merely stable once."""
    p = _profile(**kwargs)
    bd.apply_disc_block(p)
    settled = copy.deepcopy(p)
    rep2 = bd.apply_disc_block(p)
    assert rep2["changed"] is False
    assert p == settled
    bd.apply_disc_block(p)
    assert p == settled


# ---------------------------------------------------------------------------
# Detection half - is_disc_quality / disc_offenders.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["BR-DISK", "br-disk", "Raw-HD", "raw-hd",
                                  "BD-DISK", "UHD-DISC"])
def test_disc_qualities_are_recognised(bd, name):
    assert bd.is_disc_quality(name) is True


@pytest.mark.parametrize("name", ["Bluray-1080p", "Remux-1080p", "WEBDL-1080p",
                                  "WEBRip-1080p", "HDTV-1080p", "DVD",
                                  "", None])
def test_normal_qualities_are_not_disc_qualities(bd, name):
    """Remux is emphatically NOT disc-class here. 58 owns remux; conflating the
    two would make this script silently re-litigate that policy."""
    assert bd.is_disc_quality(name) is False


def _movie(mid, quality, size, has_file=True):
    return {"id": mid, "title": "M" + str(mid), "hasFile": has_file,
            "movieFile": ({"size": size,
                           "quality": {"quality": {"name": quality}}}
                          if has_file else None)}


def test_the_landed_iso_is_found_by_the_library_scan(bd):
    """movieId 441, 44.38 GiB, graded BR-DISK - the real row, 2026-08-20."""
    found = bd.disc_offenders([_movie(441, "BR-DISK", 47649253376),
                               _movie(1, "Bluray-1080p", 8000000000)],
                              RADARR_CAP)
    assert [r["id"] for r in found] == [441]
    assert "disc-class quality" in found[0]["reasons"]
    assert "over the 25000 MiB ceiling" in found[0]["reasons"]


def test_an_oversize_file_is_flagged_even_with_an_innocent_quality_name(bd):
    """A file policy would refuse to grab today is out of policy today,
    whatever its quality name says."""
    found = bd.disc_offenders([_movie(2, "Bluray-1080p", 40 * 1024 ** 3)],
                              RADARR_CAP)
    assert found[0]["reasons"] == ["over the 25000 MiB ceiling"]


def test_movies_without_a_file_are_never_offenders(bd):
    assert bd.disc_offenders([_movie(3, "BR-DISK", 0, has_file=False)],
                             RADARR_CAP) == []


def test_a_clean_library_reports_nothing(bd):
    movies = [_movie(i, "Bluray-1080p", 8000000000) for i in range(5)]
    assert bd.disc_offenders(movies, RADARR_CAP) == []


def test_library_scan_without_a_ceiling_still_finds_disc_files(bd):
    """A None ceiling must not disable the disc-class half of the scan."""
    found = bd.disc_offenders([_movie(4, "BR-DISK", 47649253376)], None)
    assert [r["id"] for r in found] == [4]
    assert found[0]["reasons"] == ["disc-class quality"]


# ---------------------------------------------------------------------------
# 59 has a live verification surface, and its header records why the obvious
# alternatives were rejected. Both are load-bearing.
# ---------------------------------------------------------------------------
def test_smoke_test_has_a_brdisk_gate():
    smoke = SMOKE.read_text(encoding="utf-8")
    assert "brdisk-block" in smoke, "59 needs a re-read gate, like 58's 13n"
    gate = smoke.split("# 13o.")[1].split("# 13. Listmonk")[0]
    # Both levers must be asserted; either alone leaves the hole open.
    assert "config/indexer" in gate and "maximumSize" in gate
    assert "BR-DISK" in gate and "qualityprofile" in gate
    # minFormatScore is the silent kill-switch for lever 2.
    assert "minFormatScore" in gate
    # Lever 3: the unconditional allowed=false gate, walked RECURSIVELY.
    assert "ALLOWS" in gate and "raw-hd" in gate.lower()
    # Zero carriers must be red, not a silent OK - see the dedicated test.
    assert "carriers" in gate


def test_the_gate_derives_its_ceilings_from_59_instead_of_retyping_them():
    """THE CEILING AND ITS GUARD MUST NOT BE TWO SOURCES OF TRUTH.

    This gate used to hardcode "radarr 25000" / "radarr2 42000", and this test
    used to assert those same literals - three copies of one policy, two of
    which drift silently the moment INSTANCES changes. The gate now parses
    INSTANCES out of 59 with ast.literal_eval, so there is exactly one number.
    """
    gate = SMOKE.read_text(encoding="utf-8").split("# 13o.")[1] \
                .split("# 13. Listmonk")[0]
    assert "59-brdisk-block.py" in gate
    assert "INSTANCES" in gate and "literal_eval" in gate
    # Importing would execute the module and reach for ~/secrets. Parse only.
    assert "ast.parse" in gate
    # The literals must be GONE from the EXECUTABLE lines, or the derivation is
    # decoration. Comments are stripped first: the rationale comment quotes the
    # old hardcoded strings on purpose, and that quote is worth keeping.
    code = "\n".join(l for l in gate.split("\n")
                     if not l.lstrip().startswith("#"))
    for arr in ("radarr", "radarr2", "sonarr"):
        cap = _load_brdisk().INSTANCES[arr]["max_size_mb"]
        if cap:
            assert (arr + " " + str(cap)) not in code, \
                "hardcoded ceiling is back; derive it from INSTANCES"
    # An unparseable table must fail, never fall back to a baked-in default.
    assert "could not parse INSTANCES" in gate


def test_the_gate_derivation_actually_reproduces_the_shipped_table():
    """Runs the gate's own parser against the real 59 and compares. A
    derivation that silently yields an empty list would make the gate red
    forever; one that mis-parses would assert the wrong ceiling."""
    tree = ast.parse(BRDISK.read_text(encoding="utf-8"))
    inst = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "INSTANCES" for t in node.targets):
            inst = ast.literal_eval(node.value)
    assert inst is not None, "INSTANCES must stay a literal dict, not built"
    derived = {arr: (int(v["max_size_mb"]) if v["max_size_mb"] else 0)
               for arr, v in inst.items()}
    assert derived == {"radarr": RADARR_CAP, "radarr2": RADARR2_CAP,
                       "sonarr": 0}


def test_a_partially_checked_gate_never_reports_pass():
    """MINOR from review: an instance with an unreadable key/port went to
    BD_SKIPPED and was continued past, but BD_SKIPPED was only consulted in the
    BD_SEEN=0 branch. A run that checked 2 of 3 printed a green "2 instance(s)"
    line and the dropped instance was invisible. Un-checked is un-known."""
    gate = SMOKE.read_text(encoding="utf-8").split("# 13o.")[1] \
                .split("# 13. Listmonk")[0]
    assert "BD_TOTAL" in gate
    assert '[ "$BD_SEEN" -lt "$BD_TOTAL" ]' in gate
    # ...and that branch must not record a pass. Comments stripped first - the
    # branch carries the "never as pass" rationale and that must stay readable.
    partial = "\n".join(
        l for l in gate.split('-lt "$BD_TOTAL"')[1].split("else")[0].split("\n")
        if not l.lstrip().startswith("#"))
    assert 'record "brdisk-block" skip' in partial
    assert 'record "brdisk-block" pass' not in partial


def test_59_header_points_at_that_gate():
    src = BRDISK.read_text(encoding="utf-8")
    assert "smoke-test.sh" in src and "13o" in src


def test_59_header_records_the_proven_import_finding():
    """The single most important fact in the file: import has no gate, and it
    was MEASURED, not assumed. If someone deletes this reasoning they will
    re-propose a custom format as an import fix."""
    src = BRDISK.read_text(encoding="utf-8")
    assert "manualimport" in src
    assert "rejections" in src
    assert "GRAB-time" in src or "GRAB TIME" in src.upper()


def test_59_header_records_why_quality_definitions_were_not_used():
    """recyclarr owns quality_definition on both radarr instances, so a maxSize
    written there is reverted on the next sync - the same trap 58 documented
    for sonarr2. Losing this note invites a script that lies about prod."""
    src = BRDISK.read_text(encoding="utf-8")
    assert "quality_definition" in src
    assert "recyclarr" in src
    assert "reset_unmatched_scores" in src


def test_59_creates_no_new_custom_format():
    """The design constraint that keeps recyclarr's reset_unmatched_scores from
    zeroing our work: score the format TRaSH already installs, never add one. A
    POST to /customformat here would silently rot on the managed profiles, so
    the endpoint must not appear anywhere outside the header's rationale."""
    src = BRDISK.read_text(encoding="utf-8")
    doc = ast.get_docstring(ast.parse(src))
    code = src.replace(doc, "", 1)
    assert "customformat" not in code.lower()
    assert "BLOCK_SCORE = -10000" in code


# ---------------------------------------------------------------------------
# LEVER 3 - the disc-class QUALITY ban. Added 2026-08-20 after review found the
# header premise ("BR-DISK is allowed on NO profile, on either Radarr") was
# false on two of the three instances in scope. Levers 1 and 2 are both
# conditional - a size comparison and a score sum. This one is not: an
# un-allowed quality is rejected outright, before any score is added up.
# ---------------------------------------------------------------------------
def _leaf(qid, name, allowed):
    return {"quality": {"id": qid, "name": name}, "allowed": allowed}


def _quality_profile(items, cutoff=None, pid=1, name="Any",
                     include_brdisk=True):
    p = _profile(pid=pid, name=name, brdisk_score=-10000,
                 include_brdisk=include_brdisk)
    p["items"] = items
    p["cutoff"] = cutoff
    return p


# The two live shapes, dumped from the box 2026-08-20 before the repair.
def _radarr2_any():
    """radarr2 p1 "Any": BR-DISK allowed=True, cutoff 20 (Bluray-480p).
    Holds 3 of that instance 6 movies."""
    return _quality_profile([
        _leaf(20, "Bluray-480p", True),
        _leaf(7, "Bluray-1080p", True),
        _leaf(30, "Remux-1080p", True),
        _leaf(22, "BR-DISK", True),
        _leaf(10, "Raw-HD", False),
    ], cutoff=20, pid=1, name="Any")


def _sonarr_p6():
    """sonarr p6 "HD 720p/1080p": Raw-HD allowed=True, cutoff 1002 (a GROUP).
    Carries all 36 series on the box."""
    return _quality_profile([
        _leaf(4, "HDTV-720p", True),
        _leaf(10, "Raw-HD", True),
        {"id": 1002, "name": "WEB 1080p", "allowed": True, "items": [
            _leaf(15, "WEBRip-1080p", True),
            _leaf(3, "WEBDL-1080p", True),
        ]},
        _leaf(7, "Bluray-1080p", True),
    ], cutoff=1002, pid=6, name="HD 720p/1080p")


def test_the_live_radarr2_any_profile_had_brdisk_switched_on(bd):
    """The measured false premise, pinned as a test. If this ever reads as a
    no-op again, someone has quietly re-allowed the tier."""
    p = _radarr2_any()
    rep = bd.apply_disc_block(p)
    assert rep["qualities_banned"] == ["BR-DISK"]
    assert rep["changed"] is True
    allowed = {l["quality"]["name"]: l["allowed"] for l in p["items"]
               if "quality" in l}
    assert allowed["BR-DISK"] is False
    # Everything else on the profile is untouched.
    assert allowed["Bluray-1080p"] is True and allowed["Remux-1080p"] is True


def test_sonarr_raw_hd_is_disc_class_and_is_banned(bd):
    """Sonarr has no quality NAMED BR-DISK, and an earlier header reasoned from
    that to "the parse-based half cannot exist there". Raw-HD is the same
    class - is_disc_quality() in 59 already says so - and it was ALLOWED on the
    profile carrying 100 percent of TV."""
    assert bd.is_disc_quality("Raw-HD") is True
    p = _sonarr_p6()
    rep = bd.apply_disc_block(p)
    assert rep["qualities_banned"] == ["Raw-HD"]
    raw = [l for l in p["items"]
           if (l.get("quality") or {}).get("name") == "Raw-HD"]
    assert raw[0]["allowed"] is False


def test_the_ban_walk_is_recursive(bd):
    """items[] is a NESTED tree. A flat walk measured 2026-08-19 called sonarr2
    clean while it allowed a remux tier inside a group; a disc tier hidden the
    same way would be just as invisible."""
    p = _quality_profile([
        {"id": 1004, "name": "Discs", "allowed": True, "items": [
            _leaf(22, "BR-DISK", True),
            _leaf(10, "Raw-HD", True),
        ]},
    ], cutoff=7)
    rep = bd.apply_disc_block(p)
    assert rep["qualities_banned"] == ["BR-DISK", "Raw-HD"]
    assert all(l["allowed"] is False for l in p["items"][0]["items"])


def test_the_profile_cutoff_is_never_banned(bd):
    """Radarr/Sonarr reject (400) a profile whose cutoff is not allowed. That
    PUT carries the other repairs too, so banning a cutoff would take them down
    with it. Report it instead of breaking the instance."""
    p = _quality_profile([_leaf(22, "BR-DISK", True)], cutoff=22)
    rep = bd.apply_disc_block(p)
    assert rep["qualities_banned"] == []
    assert rep["qualities_blocked_by_cutoff"] == ["BR-DISK"]
    assert p["items"][0]["allowed"] is True, "must not be mutated"


def test_a_cutoff_naming_the_enclosing_group_also_protects_the_leaf(bd):
    """cutoff names EITHER a quality id or a GROUP id (groups start at 1000).
    Checking only the leaf id would ban a quality inside the cutoff group and
    fail the PUT exactly the same way."""
    p = _quality_profile([
        {"id": 1004, "name": "Discs", "allowed": True,
         "items": [_leaf(22, "BR-DISK", True)]},
    ], cutoff=1004)
    rep = bd.apply_disc_block(p)
    assert rep["qualities_blocked_by_cutoff"] == ["BR-DISK"]
    assert rep["qualities_banned"] == []


def test_an_already_banned_disc_quality_is_a_no_op(bd):
    """radarr main shape - all four of its profiles already disallowed BR-DISK.
    Generalising from THAT is how the false premise happened, but it does have
    to stay a true no-op or every run rewrites four profiles for nothing."""
    p = _quality_profile([_leaf(22, "BR-DISK", False),
                          _leaf(7, "Bluray-1080p", True)], cutoff=7)
    snapshot = copy.deepcopy(p)
    rep = bd.apply_disc_block(p)
    assert rep["changed"] is False
    assert rep["qualities_banned"] == []
    assert p == snapshot


def test_the_ban_runs_even_when_the_custom_format_is_absent(bd):
    """THE HOLE THE EARLY RETURN USED TO LEAVE. apply_disc_block returns early
    when the profile carries no BR-DISK format. The quality ban must happen
    BEFORE that return, or a profile recyclarr never touched keeps the disc
    tier switched on and the caller never PUTs it."""
    p = _quality_profile([_leaf(22, "BR-DISK", True)], cutoff=7,
                         include_brdisk=False)
    rep = bd.apply_disc_block(p)
    assert rep["absent"] is True
    assert rep["qualities_banned"] == ["BR-DISK"]
    assert rep["changed"] is True, "an absent format must not suppress the PUT"
    assert p["items"][0]["allowed"] is False


def test_non_disc_qualities_are_never_touched(bd):
    """The ban keys off is_disc_quality only. A profile that allows Remux is
    58 business, not this script. Two scripts writing one field is how policy
    rots."""
    p = _quality_profile([_leaf(30, "Remux-1080p", True),
                          _leaf(7, "Bluray-1080p", True)], cutoff=7)
    rep = bd.apply_disc_block(p)
    assert rep["qualities_banned"] == []
    assert all(l["allowed"] is True for l in p["items"])


def test_a_profile_with_no_items_key_is_safe(bd):
    """Every LEVER 2 test above builds a profile with no items[] at all. The
    ban must treat that as nothing-to-walk, not raise."""
    p = _profile(brdisk_score=-10000)
    rep = bd.apply_disc_block(p)
    assert rep["qualities_banned"] == []
    assert rep["changed"] is False


# ---------------------------------------------------------------------------
# Honesty pins on the corrected header. Each one exists because the claim it
# guards was WRONG in a shipped version of this file.
# ---------------------------------------------------------------------------
def test_59_header_records_the_false_premise_it_used_to_carry():
    """The retracted claim was load-bearing: it was used to argue a profile
    edit was unnecessary. Deleting the correction invites the same mistake."""
    src = BRDISK.read_text(encoding="utf-8")
    assert "PREMISE CORRECTION" in src
    assert "BR-DISK is allowed on NO profile" in src, \
        "quote the retracted claim verbatim or the correction is unreadable"
    assert "FALSE" in src


def test_59_header_names_both_profiles_that_were_actually_open():
    src = BRDISK.read_text(encoding="utf-8")
    assert "LEVER 3" in src
    assert "Raw-HD" in src and "36 series" in src
    assert "3 of" in src and "Any" in src


def test_59_header_states_the_radarr2_sample_size_honestly():
    """It claimed both ceilings came from the last 40 grabs per instance.
    radarr2 ENTIRE history is 4 rows containing 1 grab."""
    src = BRDISK.read_text(encoding="utf-8")
    assert "n = 1" in src or "n=1" in src
    assert "36,627,251,000" in src
    assert str(RADARR2_LARGEST_LEGIT) == "36627251000"
    # The old blanket claim must be gone.
    assert "last 40 grabs per instance" not in src


def test_59_header_records_that_a_never_grabbed_file_bypasses_every_lever():
    """The sharpest limit on all three levers, measured the same day: Tdarr
    transcoded the imported .iso and wrote a 39.43 GiB .mkv into the library at
    11:03, through no indexer and no decision engine at all."""
    src = BRDISK.read_text(encoding="utf-8")
    assert "Tdarr" in src
    assert "42,341,133,540" in src
    assert "NO event after" in src


def test_59_header_answers_whether_the_ceiling_is_enforced():
    """The question the incident raised. Answered from the logs, with the
    timeline, so nobody has to re-derive it from a file size."""
    src = BRDISK.read_text(encoding="utf-8")
    assert "Maximum size is not set." in src
    assert "is too big, maximum" in src
    assert "THE CEILING WORKS" in src


def test_59_reports_zero_custom_format_carriers_as_a_failure():
    """Every per-profile check in 59 is "if the format is here, assert it". If
    BR-DISK is ever deleted or pruned off every profile, those loops match
    nothing and the run reports clean. Zero carriers is lever 2 being GONE."""
    src = BRDISK.read_text(encoding="utf-8")
    assert "carriers == 0" in src
    assert "LEVER 2 is absent" in src

import json


# ---------------------------------------------------------------------------
# The 13o gate is a shell string containing a Python program. Text assertions
# prove the right words are present; these two prove the program WORKS. The
# snippet is extracted from the shipped file and executed, so a gate that
# stopped detecting anything would still fail here.
# ---------------------------------------------------------------------------
def _gate_detector():
    """Pull 13o's embedded checker out of smoke-test.sh and un-escape it."""
    gate = SMOKE.read_text(encoding="utf-8") \
                .split('echo "13o. Full-disc (BR-DISK) block policy"')[1] \
                .split("# 13. Listmonk health + subscribers")[0]
    snippet = gate.split("python3 -c '")[1].split("'\"", 1)[0]
    return snippet.replace('\\"', '"')


def _run_detector(cap, doc):
    import subprocess
    import sys as _sys
    body = _gate_detector().replace("cap=$BD_CAP", "cap=" + str(cap))
    p = subprocess.run([_sys.executable, "-c", body], input=json.dumps(doc),
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-400:]
    return p.stdout.strip()


def _det_leaf(name, allowed):
    return {"quality": {"id": 1, "name": name}, "allowed": allowed}


def _det_profile(pid, score=-10000, minfs=0, items=None, has_cf=True):
    return {"id": pid, "minFormatScore": minfs,
            "formatItems": ([{"name": "BR-DISK", "score": score}]
                            if has_cf else []),
            "items": items or []}


def test_the_gate_detector_passes_a_healthy_instance():
    doc = {"idx": {"maximumSize": RADARR_CAP},
           "profiles": [_det_profile(6, items=[_det_leaf("Bluray-1080p", True),
                                               _det_leaf("BR-DISK", False)])]}
    assert _run_detector(RADARR_CAP, doc) == "OK"


@pytest.mark.parametrize("label,cap,doc_kw,expected", [
    # LEVER 1
    ("ceiling unset", RADARR_CAP, {"maximumSize": 0}, "maximumSize=0"),
    ("ceiling loosened", RADARR_CAP, {"maximumSize": 90000}, "maximumSize=90000"),
])
def test_the_gate_detector_catches_ceiling_drift(label, cap, doc_kw, expected):
    doc = {"idx": doc_kw, "profiles": [_det_profile(6)]}
    assert expected in _run_detector(cap, doc)


def test_the_gate_detector_catches_both_live_major_holes():
    """The two profiles that were actually open on the box 2026-08-20. If the
    gate cannot see these, it would have passed the broken state it exists to
    catch."""
    brdisk = {"idx": {"maximumSize": RADARR_CAP},
              "profiles": [_det_profile(1, items=[_det_leaf("BR-DISK", True)])]}
    assert "p1 ALLOWS BR-DISK" in _run_detector(RADARR_CAP, brdisk)
    rawhd = {"idx": {"maximumSize": 0},
             "profiles": [_det_profile(6, items=[_det_leaf("Raw-HD", True)])]}
    # cap 0 = sonarr, no ceiling asserted; the quality ban still must fire.
    assert _run_detector(0, rawhd) == "p6 ALLOWS Raw-HD"


def test_the_gate_detector_walks_into_groups():
    doc = {"idx": {"maximumSize": 0},
           "profiles": [_det_profile(6, items=[
               {"id": 1004, "allowed": True,
                "items": [_det_leaf("BR-DISK", True)]}])]}
    assert _run_detector(0, doc) == "p6 ALLOWS BR-DISK"


def test_the_gate_detector_catches_lever_2_being_neutralised():
    base = {"idx": {"maximumSize": 0}}
    assert "BR-DISK=0" in _run_detector(
        0, dict(base, profiles=[_det_profile(6, score=0)]))
    assert "minFormatScore=-10000" in _run_detector(
        0, dict(base, profiles=[_det_profile(6, minfs=-10000)]))


def test_the_gate_detector_reds_when_no_profile_carries_the_format():
    """MINOR from review: `if not sc: continue` meant a deleted or fully pruned
    custom format made the loop match nothing and print OK."""
    doc = {"idx": {"maximumSize": 0},
           "profiles": [_det_profile(6, has_cf=False),
                        _det_profile(7, has_cf=False)]}
    assert _run_detector(0, doc) == "no profile carries the BR-DISK custom format"


def test_the_gate_loop_never_reads_from_stdin():
    """sshm() calls plain `ssh` with no -n (scripts/lib/ssh.sh), and ssh SLURPS
    STDIN. An earlier draft of this gate iterated `while read ... done <<EOF`;
    the first sshm in the body ate the remaining spec lines and the loop ended
    after ONE instance, reporting "1/3 checked" with an EMPTY skip list.
    Measured 2026-08-20 by running the extracted gate against the live box."""
    gate = SMOKE.read_text(encoding="utf-8").split("# 13o.")[1] \
                .split("# 13. Listmonk")[0]
    code = "\n".join(l for l in gate.split("\n")
                     if not l.lstrip().startswith("#"))
    assert "while read" not in code
    assert "for BD_SPEC in $BD_SPECS" in code
