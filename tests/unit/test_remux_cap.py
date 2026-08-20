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
# Scope pin: radarr main only, on purpose.
# ---------------------------------------------------------------------------
def test_scope_is_radarr_main_only(m):
    """Enumerated 2026-08-19, not assumed:

      * sonarr2 profile 7 is recyclarr TRaSH template
        20e0fc959f1f1704bed501f23bdae76f - capping it here is reverted on the
        next sync, so widening ARRS produces a script that lies about prod.
      * radarr2 profiles 1/4/6 allow remux but are factory defaults on a
        6-movie instance with exactly 1 remux file. Blast radius, not a wave.
      * sonarr MAIN profile 6 allows Bluray-1080p Remux and all 36 series sit
        on it. TV is ARMED, not safe - deliberately left to a separate,
        separately-revertable change. This pin exists so nobody widens the
        scope here by reflex and loses that separation.
    """
    assert set(m.ARRS) == {"radarr"}


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
# MINOR 2 - unlinked, not freed. copyUsingHardlinks=true + empty recycleBin,
# both re-read live on 2026-08-19.
# ---------------------------------------------------------------------------
def test_byte_figures_are_reported_as_unlinked_not_freed(rg):
    src = REGRAB.read_text(encoding="utf-8")
    assert "unlinked " in src
    assert "qflix-torrent-janitor" in src
    assert "ratio>=2.0" in src
    # The only surviving "freed" is the header sentence forbidding the word.
    assert src.count('"freed"') == 1
    assert 'freed " + str' not in src


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
