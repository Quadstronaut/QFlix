"""Pins the *arr extra-file policy (scripts/configure/62-arr-extra-files.py).

WHY THIS FILE EXISTS. Two failures converge on this enforcer, and they pull in
opposite directions.

1. THE DEFECT IT CLOSES. Radarr main shipped importExtraFiles=True with
   extraFileExtensions='srt,sub,subtitles,nfo,txt,jpg,jpeg', so every release
   that carries a promo image named like the video file put that image beside
   the .mkv. Plex then selected it as the poster - four movies in
   "QFlix - Movies" on 2026-08-24, with the selected poster byte-identical to
   the sidecar on disk. useLocalAssets was already "0" on all four libraries
   and did not prevent it. The file must not arrive.

2. THE REGRESSION IT MUST NOT CAUSE. Members use subtitles. Sonarr main's
   imported .srt files are REAL (11 Debris episodes at 28-44 KB) and Bazarr
   still has 19 wanted episodes, so the release-subtitle fallback is
   load-bearing on TV. An enforcer that "harmonises" the four instances by
   rewriting every extension list to one target string, or by flipping every
   importExtraFiles to False, silently takes subtitles away from members.

So the policy is deliberately a DENYLIST REMOVAL plus a per-instance boolean,
and the tests below pin both halves: the forbidden set may never contain
anything subtitle-shaped, and sonarr main must stay assert-only.

Pure offline tests: no box, no network, no secrets. The module is loaded by
path because its filename starts with a digit and contains dashes.
"""
import copy
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENFORCER = REPO / "scripts" / "configure" / "62-arr-extra-files.py"


def _load():
    spec = importlib.util.spec_from_file_location("arr_extra_files", ENFORCER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def m():
    return _load()


# ---------------------------------------------------------------------------
# LIVE payload shapes, measured on the box 2026-08-24. config/mediamanagement
# carries dozens of keys; only the three that matter are modelled, plus one
# unrelated key to prove the enforcer does not touch the rest of the payload.
# ---------------------------------------------------------------------------
def radarr_live():
    return {"id": 1, "importExtraFiles": True,
            "extraFileExtensions": "srt,sub,subtitles,nfo,txt,jpg,jpeg",
            "recycleBin": ""}


def radarr2_live():
    return {"id": 1, "importExtraFiles": False,
            "extraFileExtensions": "srt", "recycleBin": ""}


def sonarr_live():
    return {"id": 1, "importExtraFiles": True,
            "extraFileExtensions": "srt,sub,nfo", "recycleBin": ""}


def sonarr2_live():
    return {"id": 1, "importExtraFiles": False,
            "extraFileExtensions": "srt", "recycleBin": ""}


# Everything a member could plausibly be watching with. None of these may ever
# be removable by this enforcer.
SUBTITLE_EXTENSIONS = ("srt", "sub", "subtitles", "idx", "ass", "ssa", "vtt",
                       "sup", "smi", "ttml", "dfxp")


# ---------------------------------------------------------------------------
# 1. THE POLICY CONSTANT
# ---------------------------------------------------------------------------
def test_policy_constant_denies_the_image_class_not_just_the_two_that_bit(m):
    """jpg/jpeg are what landed on disk; the constant denies the class, so a
    release shipping .png or a Kodi-style .tbn cannot re-open the door."""
    forbidden = set(m.FORBIDDEN_EXTRA_EXTENSIONS)
    assert {"jpg", "jpeg", "png", "webp", "tbn"} <= forbidden


def test_policy_constant_is_normalised_lowercase_dotless(m):
    """normalise_ext() compares against this tuple verbatim - an entry stored
    as '.JPG' would silently never match anything."""
    for ext in m.FORBIDDEN_EXTRA_EXTENSIONS:
        assert ext == ext.strip().lower().lstrip(".")


def test_no_subtitle_extension_may_ever_enter_the_forbidden_set(m):
    """THE REGRESSION GUARD. Adding a subtitle extension here would strip
    subtitles from every instance on the next run, silently."""
    overlap = set(m.FORBIDDEN_EXTRA_EXTENSIONS) & set(SUBTITLE_EXTENSIONS)
    assert overlap == set(), "subtitle extension in the deny list: " + str(overlap)


def test_sonarr_main_is_assert_only_and_the_others_are_written_false(m):
    """sonarr main keeps importExtraFiles=True on purpose: its imported .srt
    are real subtitles and Bazarr does not fully cover TV."""
    assert m.INSTANCES["sonarr"]["import_extra"] is None
    assert m.INSTANCES["radarr"]["import_extra"] is False
    assert m.INSTANCES["radarr2"]["import_extra"] is False
    assert m.INSTANCES["sonarr2"]["import_extra"] is False


def test_scope_is_all_four_instances(m):
    assert set(m.INSTANCES) == {"radarr", "radarr2", "sonarr", "sonarr2"}


# ---------------------------------------------------------------------------
# 2. THE SCRUB - a denylist removal, never a whitelist rewrite
# ---------------------------------------------------------------------------
def test_scrub_drops_only_the_images_from_the_live_radarr_list(m):
    new, removed = m.scrub_extensions(radarr_live()["extraFileExtensions"])
    assert removed == ["jpg", "jpeg"]
    assert new == "srt,sub,subtitles,nfo,txt"


def test_scrub_keeps_every_subtitle_token_on_every_live_list(m):
    """The requirement stated plainly: this change may not cost a member a
    subtitle. Runs the real four payloads."""
    for payload in (radarr_live(), radarr2_live(), sonarr_live(),
                    sonarr2_live()):
        before = m.parse_extensions(payload["extraFileExtensions"])
        new, _ = m.scrub_extensions(payload["extraFileExtensions"])
        after = m.parse_extensions(new)
        for tok in before:
            if m.normalise_ext(tok) in SUBTITLE_EXTENSIONS:
                assert tok in after, tok + " was dropped from " + repr(payload)


def test_scrub_cannot_remove_a_subtitle_extension_even_alone(m):
    """A list of nothing but subtitles must survive the scrub untouched."""
    raw = ",".join(SUBTITLE_EXTENSIONS)
    new, removed = m.scrub_extensions(raw)
    assert removed == []
    assert new == raw


def test_scrub_is_case_and_dot_tolerant(m):
    """The *arr UI accepts '.JPG'; a hand-edit leaves spaces. Those are the
    same forbidden thing and must not sneak past."""
    new, removed = m.scrub_extensions("srt, .JPG , PNG,sub")
    assert removed == [".JPG", "PNG"]
    assert new == "srt,sub"


def test_scrub_preserves_order_and_spelling_of_kept_tokens(m):
    """The scrub must never be the thing that rewrites a list cosmetically -
    a gratuitous rewrite is an unnecessary PUT against a live *arr."""
    new, removed = m.scrub_extensions("nfo,srt,txt,sub")
    assert removed == []
    assert new == "nfo,srt,txt,sub"


def test_scrub_handles_empty_and_none(m):
    assert m.scrub_extensions("") == ("", [])
    assert m.scrub_extensions(None) == ("", [])


# ---------------------------------------------------------------------------
# 3. THE POLICY APPLICATION
# ---------------------------------------------------------------------------
def test_radarr_gets_both_levers(m):
    cfg = radarr_live()
    rep = m.apply_extra_file_policy(cfg, m.INSTANCES["radarr"]["import_extra"])
    assert rep["changed"] is True
    assert rep["removed"] == ["jpg", "jpeg"]
    assert cfg["extraFileExtensions"] == "srt,sub,subtitles,nfo,txt"
    assert cfg["importExtraFiles"] is False


def test_sonarr_keeps_its_boolean_and_its_subtitles(m):
    """Assert-only: the scrub still applies, the switch does not move."""
    cfg = sonarr_live()
    rep = m.apply_extra_file_policy(cfg, m.INSTANCES["sonarr"]["import_extra"])
    assert cfg["importExtraFiles"] is True
    assert cfg["extraFileExtensions"] == "srt,sub,nfo"
    assert rep["changed"] is False


def test_sonarr_with_an_image_extension_is_scrubbed_but_stays_armed(m):
    """If someone ever adds jpg to sonarr, close that door WITHOUT taking the
    TV subtitle fallback away as a side effect."""
    cfg = sonarr_live()
    cfg["extraFileExtensions"] = "srt,sub,nfo,jpg"
    rep = m.apply_extra_file_policy(cfg, m.INSTANCES["sonarr"]["import_extra"])
    assert rep["changed"] is True
    assert cfg["extraFileExtensions"] == "srt,sub,nfo"
    assert cfg["importExtraFiles"] is True


def test_the_payload_is_otherwise_untouched(m):
    """config/mediamanagement carries dozens of unrelated keys; a PUT sends the
    whole object back, so anything this function mutates by accident ships."""
    cfg = radarr_live()
    m.apply_extra_file_policy(cfg, False)
    assert cfg["recycleBin"] == ""
    assert cfg["id"] == 1


# ---------------------------------------------------------------------------
# 4. IDEMPOTENCE - the "safe to re-run, reports NO-OP" contract
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload,arr", [
    (radarr2_live(), "radarr2"),
    (sonarr2_live(), "sonarr2"),
    (sonarr_live(), "sonarr"),
])
def test_already_correct_instances_are_a_declared_noop(m, payload, arr):
    cfg = copy.deepcopy(payload)
    rep = m.apply_extra_file_policy(cfg, m.INSTANCES[arr]["import_extra"])
    assert rep["changed"] is False
    assert "already correct" in rep["reasons"]
    assert cfg == payload


def test_second_run_over_the_first_runs_output_is_a_noop(m):
    """The real idempotence question: feed the enforcer its own result."""
    cfg = radarr_live()
    first = m.apply_extra_file_policy(cfg, False)
    assert first["changed"] is True

    settled = copy.deepcopy(cfg)
    second = m.apply_extra_file_policy(cfg, False)
    assert second["changed"] is False
    assert "already correct" in second["reasons"]
    assert cfg == settled


def test_a_partially_correct_instance_still_gets_the_missing_lever(m):
    """Boolean already False, list still dirty - and the mirror case. Neither
    half may be skipped because the other one happens to be right."""
    cfg = {"id": 1, "importExtraFiles": False,
           "extraFileExtensions": "srt,jpg"}
    rep = m.apply_extra_file_policy(cfg, False)
    assert rep["changed"] is True
    assert cfg["extraFileExtensions"] == "srt"

    cfg = {"id": 1, "importExtraFiles": True, "extraFileExtensions": "srt"}
    rep = m.apply_extra_file_policy(cfg, False)
    assert rep["changed"] is True
    assert cfg["importExtraFiles"] is False
    assert cfg["extraFileExtensions"] == "srt"


# ---------------------------------------------------------------------------
# 5. THE RE-READ GATE - a 200 from an *arr is not evidence the value stuck
# ---------------------------------------------------------------------------
def test_verify_after_accepts_a_settled_instance(m):
    assert m.verify_after({"importExtraFiles": False,
                           "extraFileExtensions": "srt,sub,subtitles"},
                          False) == ""


def test_verify_after_catches_an_extension_that_did_not_stick(m):
    why = m.verify_after({"importExtraFiles": False,
                          "extraFileExtensions": "srt,jpg"}, False)
    assert "jpg" in why


def test_verify_after_catches_a_boolean_that_did_not_stick(m):
    why = m.verify_after({"importExtraFiles": True,
                          "extraFileExtensions": "srt"}, False)
    assert "importExtraFiles" in why


def test_verify_after_does_not_grade_the_boolean_on_an_assert_only_instance(m):
    """sonarr main is meant to read back True. That is not a failure."""
    assert m.verify_after({"importExtraFiles": True,
                           "extraFileExtensions": "srt,sub,nfo"}, None) == ""


# ===========================================================================
# Council blocker (a), 2026-08-25: a NEW *arr instance was silently unenforced.
# ===========================================================================

def _mk_secrets(tmp_path, slugs, with_port=True):
    for s in slugs:
        (tmp_path / (s + ".key")).write_text("k", encoding="utf-8")
        if with_port:
            (tmp_path / (s + ".port")).write_text("1", encoding="utf-8")
    return str(tmp_path)


def test_discovery_finds_every_arr_on_the_box(m, tmp_path):
    d = _mk_secrets(tmp_path, ["radarr", "radarr2", "sonarr", "sonarr2"])
    assert m.discover_instances(d) == ["radarr", "radarr2", "sonarr", "sonarr2"]


def test_a_new_arr_instance_is_a_named_failure_not_a_silent_skip(m, tmp_path):
    """THE BLOCKER. INSTANCES is a hand-edited constant. Add radarr3 tomorrow
    and, before this, it would import release artwork forever because nothing
    in the repo would ever mention it -- the same shape as the defect this file
    exists to close, one layer up."""
    d = _mk_secrets(tmp_path, ["radarr", "radarr2", "sonarr", "sonarr2", "radarr3"])
    assert m.unknown_instances(d) == ["radarr3"]


def test_the_known_set_is_clean(m, tmp_path):
    """NEGATIVE CONTROL: the guard must not cry wolf on the real roster, or it
    gets ignored the day it matters."""
    d = _mk_secrets(tmp_path, sorted(m.INSTANCES))
    assert m.unknown_instances(d) == []


def test_secrets_without_a_port_are_not_instances(m, tmp_path):
    """A stray .key with no .port is not a reachable instance. Counting it would
    make the guard fire on decommission debris and train the operator to ignore
    it -- the failure mode that produced the Maintainerr collections."""
    d = _mk_secrets(tmp_path, ["radarr"], with_port=True)
    (tmp_path / "plex.key").write_text("k", encoding="utf-8")
    (tmp_path / "radarr9.key").write_text("k", encoding="utf-8")   # no .port
    assert m.discover_instances(d) == ["radarr"]


def test_forbidden_extensions_cover_every_image_shape_a_release_ships(m):
    """The upstream half of the poster defect. jpg alone is not enough -- a
    release can ship png/webp/tbn art and Plex ingests all of them as local."""
    for ext in ("jpg", "jpeg", "png", "webp", "tbn"):
        assert ext in m.FORBIDDEN_EXTRA_EXTENSIONS
