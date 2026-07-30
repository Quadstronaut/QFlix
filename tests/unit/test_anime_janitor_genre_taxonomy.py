"""TheTVDB's "Anime" genre is not "Animation", and the janitor must match both.

WHY (2026-07-30): the operator asked why "Mob Psycho 100" was not in the anime
library. It is unambiguously anime -- Bones, 2016, ONE's manga -- and its Sonarr
record already carries seriesType=anime and originalLanguage Japanese. It sat in
the main TV library and had never once been flagged for review.

Cause: TheTVDB carries "Anime" as a genre SEPARATE from "Animation" and does not
always tag both. Sonarr surfaces that taxonomy verbatim.

    Chainsaw Man    ['Action','Adventure','Animation','Anime','Comedy',...]  -> flagged daily
    Mob Psycho 100  ['Action','Anime','Comedy','Fantasy']                    -> SILENTLY IGNORED

`has_anim = ANIMATION_GENRE in genres` was a literal match on "Animation", so
classify_main_lib fell through to ("ignore", "") -- no flag, no signal, nothing
in any log. Not a visible disagreement, a silent miss.

Widening to {"Animation", "Anime"} is safe in both directions and strictly
reduces risk: `auto_out` requires NOT has_anim, so a wider match can only reduce
auto-moves, never cause a false one.

The two series records below are the REAL genre arrays read from the live Sonarr
API on 2026-07-30, not invented fixtures.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
JANITOR = REPO / "scripts" / "maint" / "qflix-anime-janitor.py"


def _load():
    sys.path.insert(0, str(REPO / "scripts" / "maint"))
    sys.path.insert(0, str(REPO / "scripts" / "mcp"))
    spec = importlib.util.spec_from_file_location("anime_janitor", JANITOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


ANIME_LANGS = {"Japanese"}

# Verbatim from the live main-Sonarr API, 2026-07-30.
MOB_PSYCHO = {
    "title": "Mob Psycho 100",
    "seriesType": "anime",
    "genres": ["Action", "Anime", "Comedy", "Fantasy"],
    "originalLanguage": {"name": "Japanese"},
}
CHAINSAW_MAN = {
    "title": "Chainsaw Man",
    "seriesType": "anime",
    "genres": ["Action", "Adventure", "Animation", "Anime", "Comedy",
               "Fantasy", "Horror", "Thriller"],
    "originalLanguage": {"name": "Japanese"},
}
SOUTH_PARK = {
    "title": "South Park",
    "seriesType": "standard",
    "genres": ["Animation", "Comedy"],
    "originalLanguage": {"name": "English"},
}


def test_anime_genre_alone_is_recognised(mod):
    """The regression. Against the pre-fix code this returns ('ignore', '')."""
    action, reason = mod.classify_main_lib(MOB_PSYCHO, ANIME_LANGS)
    assert (action, reason) == ("flag_reverse", "anime-in-main-lib"), (
        "Mob Psycho 100 carries 'Anime' but not 'Animation' and was silently "
        "ignored by the reverse classifier"
    )


def test_the_title_that_already_worked_still_works(mod):
    """Chainsaw Man carries BOTH tags and was already flagged -- do not regress."""
    assert mod.classify_main_lib(CHAINSAW_MAN, ANIME_LANGS) == \
        ("flag_reverse", "anime-in-main-lib")


def test_western_cartoon_in_main_lib_is_still_ignored(mod):
    """Animation + non-JP origin belongs in the main library. No false flag."""
    assert mod.classify_main_lib(SOUTH_PARK, ANIME_LANGS) == ("ignore", "")


def test_jp_live_action_is_not_flagged_as_anime(mod):
    """Japanese origin alone must not be enough — that would flag every drama."""
    rec = {"title": "Midnight Diner", "genres": ["Drama"],
           "originalLanguage": {"name": "Japanese"}}
    assert mod.classify_main_lib(rec, ANIME_LANGS) == ("ignore", "")


def test_forward_direction_now_leaves_anime_tagged_only_anime(mod):
    """The fix also removes a FALSE FLAG in the forward direction.

    Pre-fix, a JP title tagged only "Anime" sitting in the anime library where it
    belongs reached ('flag', 'jp-live-action-or-mislabel'). It now correctly
    returns ('leave', 'anime').
    """
    assert mod.classify_anime_lib(MOB_PSYCHO, ANIME_LANGS) == ("leave", "anime")


def test_widening_can_never_create_a_false_auto_move(mod):
    """auto_out requires NOT has_anim, so a wider genre match only shrinks it.

    Exhaustive over the genre/origin quadrants: nothing carrying either anime
    genre may ever be auto-moved out of the anime library.
    """
    for genres in (["Anime"], ["Animation"], ["Anime", "Animation"]):
        for origin in ("Japanese", "English", "Korean"):
            rec = {"title": "t", "genres": genres,
                   "originalLanguage": {"name": origin}}
            action, _ = mod.classify_anime_lib(rec, ANIME_LANGS)
            assert action != "auto_out", (
                f"genres={genres} origin={origin} would be auto-moved out"
            )


def test_live_action_non_jp_is_still_auto_moved_out(mod):
    """The janitor's actual job must survive the widening."""
    rec = {"title": "Some Drama", "genres": ["Drama"],
           "originalLanguage": {"name": "English"}}
    assert mod.classify_anime_lib(rec, ANIME_LANGS) == \
        ("auto_out", "live-action-non-jp")


def test_both_classifiers_share_one_genre_set(mod):
    """Forward and reverse must not drift apart into two policy surfaces."""
    assert "Anime" in mod.ANIME_GENRES and "Animation" in mod.ANIME_GENRES
    src = JANITOR.read_text(encoding="utf-8")
    assert src.count("has_anim = bool(ANIME_GENRES.intersection(genres))") == 2, \
        "the two classifiers no longer share the same genre test"
    assert "has_anim = ANIMATION_GENRE in genres" not in src, \
        "a classifier reverted to the literal single-genre check"
