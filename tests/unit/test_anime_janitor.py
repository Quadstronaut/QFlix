"""tests/unit/test_anime_janitor.py - classifier + safety-envelope tests for
the qflix-anime-janitor (sibling of test_qflix_reaper.py). Loads the hyphenated
module via spec_from_file_location, exactly like the reaper tests do.

Focus: the pure classifier quadrants, exclusion parsing/matching, and the run()
safety envelope (dry-run mutates nothing, max-pct tripwire, max-moves defer,
window skip, reverse flag-only).
"""
import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[2] / "scripts" / "maint" / "qflix-anime-janitor.py"


def _load():
    spec = importlib.util.spec_from_file_location("qflix_anime_janitor", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def m():
    return _load()


# ---- record helpers -------------------------------------------------------
def series(id_, title, genres, lang, tvdb=None, **extra):
    d = {"id": id_, "title": title, "genres": genres,
         "originalLanguage": ({"name": lang} if lang else None),
         "tvdbId": tvdb if tvdb is not None else id_,
         "path": "/home/quadstronaut/media/Anime/" + title,
         "monitored": True}
    d.update(extra)
    return d


def movie(id_, title, genres, lang, tmdb=None, **extra):
    d = {"id": id_, "title": title, "genres": genres,
         "originalLanguage": ({"name": lang} if lang else None),
         "tmdbId": tmdb if tmdb is not None else id_,
         "path": "/home/quadstronaut/media/Anime Movies/" + title,
         "monitored": True}
    d.update(extra)
    return d


# ===========================================================================
# Classifier (the 4 quadrants + missing metadata)
# ===========================================================================
def test_liveaction_nonjp_is_auto_out(m):
    r = series(1, "Some Western Drama", ["Drama", "Crime"], "English")
    assert m.classify_anime_lib(r, {"Japanese"}) == ("auto_out", "live-action-non-jp")


def test_animation_japanese_is_left(m):
    r = series(2, "Real Anime", ["Animation", "Action"], "Japanese")
    assert m.classify_anime_lib(r, {"Japanese"}) == ("leave", "anime")


def test_animation_nonjp_is_flagged(m):
    r = series(3, "Rick and Morty", ["Animation", "Comedy"], "English")
    action, reason = m.classify_anime_lib(r, {"Japanese"})
    assert action == "flag" and reason == "animation-non-jp"


def test_jp_liveaction_is_flagged_not_moved(m):
    # No Animation genre but Japanese origin -> ambiguous -> FLAG, never auto-move.
    r = series(4, "Japanese Live Drama", ["Drama"], "Japanese")
    action, reason = m.classify_anime_lib(r, {"Japanese"})
    assert action == "flag" and reason == "jp-live-action-or-mislabel"


def test_missing_genres_is_skip(m):
    r = series(5, "No Metadata Yet", [], "English")
    assert m.classify_anime_lib(r, {"Japanese"}) == ("skip", "missing-metadata")
    r2 = series(6, "Null Genres", None, "English")
    assert m.classify_anime_lib(r2, {"Japanese"}) == ("skip", "missing-metadata")


def test_anime_lang_config_widens(m):
    # With Korean added, a Korean animation is left (treated as anime).
    r = series(7, "Korean Aeni", ["Animation"], "Korean")
    assert m.classify_anime_lib(r, {"Japanese", "Korean"})[0] == "leave"
    assert m.classify_anime_lib(r, {"Japanese"})[0] == "flag"


def test_reverse_anime_in_main_flagged(m):
    r = series(8, "Attack on Titan", ["Animation"], "Japanese")
    assert m.classify_main_lib(r, {"Japanese"}) == ("flag_reverse", "anime-in-main-lib")


def test_reverse_nonanime_ignored(m):
    r = series(9, "Breaking Bad", ["Drama"], "English")
    assert m.classify_main_lib(r, {"Japanese"}) == ("ignore", "")


# ===========================================================================
# Exclusions
# ===========================================================================
def test_load_exclusions_parses(tmp_path, m):
    p = tmp_path / "excl"
    p.write_text("# comment\n\ntvdb:123\nTMDB: 456\nCowboy Bebop  # inline\n",
                 encoding="utf-8")
    toks = m.load_exclusions(p)
    assert "tvdb:123" in toks
    assert "tmdb:456" in toks           # whitespace stripped, lowercased
    assert "title:cowboy bebop" in toks


def test_is_excluded_by_id_and_title(m):
    toks = {"tvdb:100", "title:cowboy bebop"}
    assert m.is_excluded(series(1, "X", ["Drama"], "English", tvdb=100), "tvdbId", toks)
    assert m.is_excluded(series(1, "Cowboy Bebop", ["Drama"], "English", tvdb=999), "tvdbId", toks)
    assert not m.is_excluded(series(1, "Other", ["Drama"], "English", tvdb=999), "tvdbId", toks)
    assert not m.is_excluded(series(1, "X", ["Drama"], "English"), "tvdbId", set())


# ===========================================================================
# run() safety envelope
# ===========================================================================
def _wire(m, monkeypatch, titles_by_slug, *, window=False):
    """Common monkeypatching for run() tests: stub I/O, capture the plan."""
    monkeypatch.setattr(m, "_setup_file_log", lambda: None)
    monkeypatch.setattr(m, "in_maintenance_window", lambda now=None: window)
    monkeypatch.setattr(m, "_plex_creds", lambda: ("32400", "tok"))
    monkeypatch.setattr(m, "_plex_section_keys", lambda p, t: {})
    kuma = []
    monkeypatch.setattr(m, "_push_kuma", lambda s, msg: kuma.append((s, msg)))
    monkeypatch.setattr(m, "_list_titles", lambda slug, kind: titles_by_slug.get(slug))
    cap = {}
    def cap_emit(args, auto, flags, **kw):
        cap["auto"] = auto
        cap["flags"] = flags
        cap["kw"] = kw
    monkeypatch.setattr(m, "_emit", cap_emit)
    return kuma, cap


def _args(m, argv):
    return m.build_parser().parse_args(argv)


def test_dryrun_plan_no_rehome(m, monkeypatch):
    titles = {
        "sonarr2": [series(1, "Western Show", ["Drama"], "English"),
                    series(2, "Real Anime", ["Animation"], "Japanese")],
        "radarr2": [], "sonarr": [], "radarr": [],
    }
    kuma, cap = _wire(m, monkeypatch, titles)
    # rehome must NEVER be called in dry-run.
    monkeypatch.setattr(m, "rehome", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rehome called in dry-run")))
    rc = m.run(_args(m, []))
    assert rc == m.EXIT_OK
    titles_auto = [r.get("title") for (_p, r) in cap["auto"]]
    assert titles_auto == ["Western Show"]           # only the live-action
    assert kuma[-1][0] == "up"


def test_maxpct_tripwire_aborts(m, monkeypatch):
    # 3/3 = 100% live-action > default 25% -> EXIT_CAP, no rehome.
    titles = {
        "sonarr2": [series(i, "LA%d" % i, ["Drama"], "English") for i in range(3)],
        "radarr2": [], "sonarr": [], "radarr": [],
    }
    kuma, cap = _wire(m, monkeypatch, titles)
    monkeypatch.setattr(m, "rehome", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rehome on cap trip")))
    rc = m.run(_args(m, ["--execute"]))
    assert rc == m.EXIT_CAP
    assert kuma[-1][0] == "down"


def test_maxpct_force_overrides(m, monkeypatch):
    titles = {
        "sonarr2": [series(i, "LA%d" % i, ["Drama"], "English") for i in range(3)],
        "radarr2": [], "sonarr": [], "radarr": [],
    }
    calls = []
    kuma, cap = _wire(m, monkeypatch, titles)
    monkeypatch.setattr(m, "rehome", lambda pair, rec, **k: calls.append(rec["title"]) or (True, "moved"))
    rc = m.run(_args(m, ["--execute", "--force"]))
    assert rc == m.EXIT_OK
    assert len(calls) == 3            # force bypasses the tripwire, all moved


def test_maxmoves_defers_excess(m, monkeypatch):
    titles = {
        "sonarr2": [series(i, "LA%d" % i, ["Drama"], "English") for i in range(3)],
        "radarr2": [], "sonarr": [], "radarr": [],
    }
    calls = []
    kuma, cap = _wire(m, monkeypatch, titles)
    monkeypatch.setattr(m, "rehome", lambda pair, rec, **k: calls.append(rec["title"]) or (True, "moved"))
    rc = m.run(_args(m, ["--execute", "--force", "--max-moves", "1"]))
    assert rc == m.EXIT_OK
    assert len(calls) == 1                       # only 1 moved
    assert cap["kw"]["deferred"] == 2            # 2 deferred to next run


def test_exclusions_honored(tmp_path, m, monkeypatch):
    excl = tmp_path / "e"
    excl.write_text("tvdb:1\n", encoding="utf-8")
    titles = {
        "sonarr2": [series(1, "Excluded LA", ["Drama"], "English", tvdb=1),
                    series(2, "Kept LA", ["Drama"], "English", tvdb=2)],
        "radarr2": [], "sonarr": [], "radarr": [],
    }
    kuma, cap = _wire(m, monkeypatch, titles)
    rc = m.run(_args(m, ["--exclude-file", str(excl)]))
    assert rc == m.EXIT_OK
    titles_auto = [r.get("title") for (_p, r) in cap["auto"]]
    assert titles_auto == ["Kept LA"]            # excluded one never considered


def test_window_skips_run(m, monkeypatch):
    titles = {"sonarr2": None}   # would fatal if enumerated
    kuma, cap = _wire(m, monkeypatch, titles, window=True)
    # _list_titles must not be called inside the window.
    monkeypatch.setattr(m, "_list_titles", lambda slug, kind: (_ for _ in ()).throw(AssertionError("enumerated in window")))
    rc = m.run(_args(m, ["--execute"]))
    assert rc == m.EXIT_OK
    assert kuma[-1][0] == "up" and "window" in kuma[-1][1]


def test_reverse_flag_surfaced(m, monkeypatch):
    titles = {
        "sonarr2": [], "radarr2": [],
        "sonarr": [series(10, "Misplaced Anime", ["Animation"], "Japanese", tvdb=10)],
        "radarr": [],
    }
    kuma, cap = _wire(m, monkeypatch, titles)
    rc = m.run(_args(m, []))
    assert rc == m.EXIT_OK
    reasons = [f["reason"] for f in cap["flags"]]
    assert "anime-in-main-lib" in reasons


def test_execute_failure_is_partial(m, monkeypatch):
    titles = {
        "sonarr2": [series(1, "LA", ["Drama"], "English")],
        "radarr2": [], "sonarr": [], "radarr": [],
    }
    kuma, cap = _wire(m, monkeypatch, titles)
    monkeypatch.setattr(m, "rehome", lambda *a, **k: (False, "boom"))
    rc = m.run(_args(m, ["--execute", "--force"]))
    assert rc == m.EXIT_PARTIAL
    assert kuma[-1][0] == "down"


def test_enumerate_failure_is_fatal(m, monkeypatch):
    titles = {"sonarr2": None, "radarr2": None, "sonarr": None, "radarr": None}
    kuma, cap = _wire(m, monkeypatch, titles)
    rc = m.run(_args(m, []))
    assert rc == m.EXIT_FATAL
