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
        "sonarr2": [series(i, "LA%d" % i, ["Drama"], "English") for i in range(1, 4)],
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
        "sonarr2": [series(i, "LA%d" % i, ["Drama"], "English") for i in range(1, 4)],
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


# ===========================================================================
# Council fixes: classifier B4, valid-id, containment, import-verify fail-safe,
# partial-enum-fatal, Plex-optional, id-match guard.
# ===========================================================================
def test_missing_origin_is_flagged_not_moved(m):
    # No Animation genre AND no originalLanguage -> flag, NEVER auto_out (B4).
    r = series(1, "No Lang", ["Drama"], None)
    assert m.classify_anime_lib(r, {"Japanese"}) == ("flag", "missing-origin")


def test_valid_id(m):
    assert m._valid_id(5) and m._valid_id("123") and m._valid_id(10)
    assert not m._valid_id(0) and not m._valid_id(None)
    assert not m._valid_id("0") and not m._valid_id("") and not m._valid_id("x")


def test_is_contained(m, tmp_path):
    root = tmp_path / "TV"
    root.mkdir()
    assert m._is_contained(str(root / "Show"), str(root))
    assert m._is_contained(str(root / "a" / "b"), str(root))
    assert not m._is_contained(str(tmp_path / "evil" / "x"), str(root))   # escape
    assert not m._is_contained(str(root), str(root))                       # equal, not inside
    assert not m._is_contained(None, str(root))


def test_partial_enum_is_fatal(m, monkeypatch):
    # One anime instance unreachable -> EXIT_FATAL even though another yielded a
    # candidate (design 10; council B6). Was silently EXIT_OK before.
    titles = {"sonarr2": None,
              "radarr2": [movie(1, "LA Movie", ["Drama"], "English")],
              "sonarr": [], "radarr": []}
    kuma, cap = _wire(m, monkeypatch, titles)
    rc = m.run(_args(m, []))
    assert rc == m.EXIT_FATAL
    assert kuma[-1][0] == "down"


def test_plex_missing_is_not_fatal(m, monkeypatch):
    titles = {"sonarr2": [series(1, "Western", ["Drama"], "English")],
              "radarr2": [], "sonarr": [], "radarr": []}
    kuma, cap = _wire(m, monkeypatch, titles)
    monkeypatch.setattr(m, "_plex_creds",
                        lambda: (_ for _ in ()).throw(FileNotFoundError("plex.token")))
    rc = m.run(_args(m, []))
    assert rc == m.EXIT_OK               # Plex not load-bearing (B7)


def test_invalid_id_flagged_not_moved(m, monkeypatch):
    titles = {"sonarr2": [series(0, "Zero Id LA", ["Drama"], "English", tvdb=0),
                          series(2, "Good LA", ["Drama"], "English", tvdb=2)],
              "radarr2": [], "sonarr": [], "radarr": []}
    kuma, cap = _wire(m, monkeypatch, titles)
    rc = m.run(_args(m, []))
    assert rc == m.EXIT_OK
    assert [r.get("title") for (_p, r) in cap["auto"]] == ["Good LA"]
    assert "invalid-id" in [f["reason"] for f in cap["flags"]]


# ---- rehome() fail-safe ordering (real tmp_path FS + fake arr clients) -----
class _FakeArr:
    def __init__(self, responses):
        self.responses = responses
        self.deletes = []
        self.posts = []

    def get(self, path, query=None):
        return self.responses.get(("GET", path), (200, None))

    def post(self, path, body=None):
        self.posts.append(path)
        return self.responses.get(("POST", path), (200, {}))

    def delete(self, path, query=None):
        self.deletes.append((path, query))
        return self.responses.get(("DELETE", path), (200, None))


def _rehome_env(m, monkeypatch, tmp_path, *, target_path=None, verified=True):
    anime = tmp_path / "Anime"
    tv = tmp_path / "TV"
    (anime / "Show").mkdir(parents=True)
    (anime / "Show" / "ep.mkv").write_text("x", encoding="utf-8")
    tv.mkdir()
    tpath = target_path if target_path is not None else str(tv / "Show")
    dst = _FakeArr({
        ("GET", "/rootfolder"): (200, [{"path": str(tv)}]),
        ("GET", "/qualityprofile"): (200, [{"id": 1}]),
        ("GET", "/series"): (200, []),                       # not present -> create
        ("GET", "/series/lookup"): (200, [{"tvdbId": 10, "title": "Show"}]),
        ("POST", "/series"): (200, {"id": 99, "path": tpath}),
        ("POST", "/command"): (200, {}),
    })
    src = _FakeArr({})
    monkeypatch.setattr(m, "_arr_client", lambda slug: dst if slug == "sonarr" else src)
    monkeypatch.setattr(m, "_verify_import", lambda *a, **k: verified)
    pair = {"kind": "series", "idkey": "tvdbId", "from_slug": "sonarr2",
            "to_slug": "sonarr", "from_root": str(anime), "to_root": str(tv),
            "plex_from": "QFlix - Anime", "plex_to": "QFlix - TV",
            "series_type": "standard"}
    record = {"id": 5, "tvdbId": 10, "title": "Show",
              "path": str(anime / "Show"), "monitored": True}
    return pair, record, src, dst, (tv / "Show")


def test_rehome_success_removes_source_after_verify(m, monkeypatch, tmp_path):
    pair, record, src, dst, tv_show = _rehome_env(m, monkeypatch, tmp_path, verified=True)
    ok, note = m.rehome(pair, record, section_keys={}, plex_port=None, plex_token=None)
    assert ok, note
    assert tv_show.exists()                                    # files moved
    assert src.deletes and src.deletes[0][0] == "/series/5"    # source removed only after verify


def test_rehome_import_unverified_keeps_source(m, monkeypatch, tmp_path):
    pair, record, src, dst, tv_show = _rehome_env(m, monkeypatch, tmp_path, verified=False)
    ok, note = m.rehome(pair, record, section_keys={}, plex_port=None, plex_token=None)
    assert not ok and "import-unverified" in note
    assert src.deletes == []                                   # source NOT removed -> no orphan


def test_rehome_path_escape_rolls_back_created(m, monkeypatch, tmp_path):
    pair, record, src, dst, _ = _rehome_env(
        m, monkeypatch, tmp_path, target_path=str(tmp_path / "evil" / "Show"))
    ok, note = m.rehome(pair, record, section_keys={}, plex_port=None, plex_token=None)
    assert not ok and "path escape" in note
    assert src.deletes == []                                   # source untouched
    assert ("/series/99", "deleteFiles=false") in dst.deletes  # our created record rolled back


def test_add_to_target_rejects_mismatched_existing_id(m):
    dst = _FakeArr({
        ("GET", "/qualityprofile"): (200, [{"id": 1}]),
        ("GET", "/series"): (200, [{"tvdbId": 999, "id": 7, "path": "/x"}]),  # wrong id
        ("GET", "/series/lookup"): (200, [{"tvdbId": 10, "title": "Show"}]),
        ("POST", "/series"): (200, {"id": 50, "path": "/home/q/media/TV/Show"}),
    })
    pair = {"kind": "series", "idkey": "tvdbId", "to_slug": "sonarr",
            "series_type": "standard"}
    nid, created, path = m._add_to_target(dst, pair, 10, {"monitored": True}, "/home/q/media/TV")
    assert nid == 50 and created is True        # did NOT adopt the tvdbId=999 record


def test_add_to_target_adopts_matching_existing(m):
    dst = _FakeArr({
        ("GET", "/qualityprofile"): (200, [{"id": 1}]),
        ("GET", "/series"): (200, [{"tvdbId": 10, "id": 8, "path": "/home/q/media/TV/Show"}]),
    })
    pair = {"kind": "series", "idkey": "tvdbId", "to_slug": "sonarr",
            "series_type": "standard"}
    nid, created, path = m._add_to_target(dst, pair, 10, {"monitored": True}, "/home/q/media/TV")
    assert nid == 8 and created is False        # adopted the matching record (never rolled back)


def test_rehome_fileless_migrates_record_only(m, monkeypatch, tmp_path):
    import shutil
    pair, record, src, dst, tv_show = _rehome_env(m, monkeypatch, tmp_path, verified=True)
    shutil.rmtree(record["path"])               # make the source fileless (no folder on disk)
    ok, note = m.rehome(pair, record, section_keys={}, plex_port=None, plex_token=None)
    assert ok and "record-only" in note
    assert src.deletes and src.deletes[0][0] == "/series/5"   # source record migrated out
    assert not tv_show.exists()                               # nothing moved on disk
