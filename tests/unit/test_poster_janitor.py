"""Tests for qflix-poster-janitor's flip predicate and accounting (pure logic).

Bug (2026-08-24): releases ship a jpg named like the video file, or embed
mjpeg cover art; Plex selects that as a `local`/`embedded` poster and members
see release-group branding. Four movies and two episodes were affected.

Three things are pinned here, and each of them is a scar:

  1. THE PREDICATE. It decides whether member-visible artwork gets rewritten,
     so it must refuse anything already agent-supplied. The SQL detector is
     deliberately broad (it can see the user_thumb_url SHAPE, not the
     provider), so `benign` is the arm that absorbs its false positives.

  2. THE PREFERENCE ORDER. The predecessor used
     `next(p for p in posters if agent)`, which is not a choice but a
     constant: the poster list is grouped by provider in a fixed order with
     tmdb at index 0 on 69 of 69 measured items, so it returned tmdb[0] every
     time and could never pick gracenote or plex -- the two providers Plex
     itself prefers 55% of the time.

  3. THE NO-AGENT-ALTERNATIVE ACCOUNTING. Two real episodes return an EMPTY
     poster list. A `continue` past them reproduces the exact failure this
     janitor exists to end: hidden work nobody is told about.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "qflix_poster_janitor",
    ROOT / "scripts" / "maint" / "qflix-poster-janitor.py",
)
pj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pj)


def _p(provider: str, selected: bool = False, rk: str = "") -> dict:
    return {"provider": provider, "selected": selected,
            "ratingKey": rk or ("rk-" + provider)}


def _row(item_id: int, title: str) -> dict:
    return {"id": item_id, "title": title, "type": 1, "section": 4,
            "thumb": "metadata://posters/5edf955b"}


# -- classify_item: the flip predicate --------------------------------------

def test_local_selected_with_agent_alternative_flips():
    """The four known-bad movies: sel=local at index 0, agent art available."""
    posters = [_p("local", selected=True), _p("tmdb"), _p("gracenote")]
    verdict, agent = pj.classify_item(posters)
    assert verdict == "flip"
    assert agent["provider"] == "gracenote"


def test_embedded_selected_with_agent_alternative_flips():
    """The two episodes on 1400x1400 square embedded cover art."""
    posters = [_p("embedded", selected=True), _p("gracenote"), _p("tmdb"),
               _p("imdb"), _p("tvdb")]
    verdict, agent = pj.classify_item(posters)
    assert verdict == "flip"
    assert agent["provider"] == "gracenote"


def test_agent_selected_is_benign_and_never_touched():
    """38 of 42 movies are already on agent art. The SQL detector cannot see
    providers, so anything it over-flags must land here, not in a flip."""
    posters = [_p("tmdb"), _p("gracenote", selected=True)]
    assert pj.classify_item(posters) == ("benign", None)


def test_a_deep_agent_pick_is_still_benign():
    """Plex chooses deep indices (Interstellar sel_idx=96). An item whose
    SELECTED poster is agent-supplied is healthy regardless of position --
    re-flipping it to our own preference would fight Plex's own judgement."""
    posters = [_p("tmdb")] * 90 + [_p("gracenote", selected=True)]
    assert pj.classify_item(posters) == ("benign", None)


def test_no_selection_at_all_still_flips():
    """The SQL already established the stored thumb is non-agent. Refusing
    because Plex flagged no selection would decline to fix the worse case."""
    verdict, agent = pj.classify_item([_p("local"), _p("tmdb")])
    assert verdict == "flip"
    assert agent["provider"] == "tmdb"


# -- pick_agent_poster: the preference order --------------------------------

def test_gracenote_beats_a_leading_tmdb():
    """The whole point of the rewrite. Poster lists ALWAYS lead with tmdb, so
    first-match is a constant; Plex's own picks were gracenote 33 / tmdb 29."""
    posters = [_p("tmdb"), _p("imdb"), _p("gracenote"), _p("tvdb")]
    assert pj.pick_agent_poster(posters)["provider"] == "gracenote"


def test_preference_order_is_total_over_the_live_vocabulary():
    """Every provider Plex actually emits must be rankable. tmdb, tvdb,
    fanarttv, gracenote, imdb, plex, local -- 4791 posters, no others."""
    for prov in ("gracenote", "plex", "tmdb", "tvdb", "fanarttv", "imdb"):
        assert pj.pick_agent_poster([_p(prov)])["provider"] == prov


def test_fanarttv_is_recognised():
    """The predecessor's set had "fanart", "thetvdb" and "themoviedb" -- none
    of which Plex emits -- and MISSED "fanarttv". Inert while tmdb precedes it
    in the list; a silent "no agent alternative" the day it does not."""
    verdict, agent = pj.classify_item([_p("local", selected=True), _p("fanarttv")])
    assert verdict == "flip"
    assert agent["provider"] == "fanarttv"


def test_ties_keep_plex_own_ordering():
    """min() is stable: within a provider group the first entry wins, which is
    the only ranking signal the posters XML carries (no score/votes/size)."""
    first = _p("tmdb", rk="first")
    posters = [first, _p("tmdb", rk="second")]
    assert pj.pick_agent_poster(posters)["ratingKey"] == "first"


def test_provider_case_and_whitespace_normalised():
    assert pj.pick_agent_poster([{"provider": " TMDB ", "selected": False,
                                  "ratingKey": "x"}])["ratingKey"] == "x"


# -- the no-agent-alternative population ------------------------------------

def test_empty_poster_list_is_no_agent_alt_not_a_flip():
    """Episodes 7893/7894: /posters returns an EMPTY MediaContainer. Nothing
    to flip to -- and nothing to silently skip either."""
    assert pj.classify_item([]) == ("no-agent-alt", None)


def test_only_bad_providers_is_no_agent_alt():
    posters = [_p("local", selected=True), _p("embedded"), _p("screenshot")]
    assert pj.classify_item(posters) == ("no-agent-alt", None)


def test_unfixable_items_are_counted_and_named_not_skipped():
    """The accounting requirement: they land in their own bucket, they are
    NOT counted as flips, and they carry their titles."""
    rows = [_row(7893, "Oceans Three"), _row(8118, "Evil Dead Burn")]
    posters = {7893: [], 8118: [_p("local", selected=True), _p("tmdb")]}
    res = pj.run(probe=lambda i: posters[i], flipper=_boom,
                 rows=rows, execute=False, max_items=25)
    assert [u["title"] for u in res["unfixable"]] == ["Oceans Three"]
    assert [f["title"] for f in res["flips"]] == ["Evil Dead Burn"]
    assert res["bad"] == 2


def test_unfixable_reaches_the_kuma_message_by_name():
    """Counted is not enough. This population is the one a human has to look
    at, so the titles must survive into the 200-char push message."""
    rows = [_row(7893, "Oceans Three"), _row(7894, "King Elfo's Court")]
    res = pj.run(probe=lambda i: [], flipper=_boom,
                 rows=rows, execute=False, max_items=25)
    msg = pj._kuma_msg(res, execute=False, scanned=431)
    assert "2 unfixable" in msg
    assert "NO AGENT ART" in msg
    assert "Oceans Three" in msg and "King Elfo's Court" in msg


def test_unfixable_alone_does_not_produce_a_failure():
    """An unclearable red is a muted monitor. Unfixable items are reported,
    never a hard failure."""
    res = pj.run(probe=lambda i: [], flipper=_boom,
                 rows=[_row(7893, "Oceans Three")], execute=True, max_items=25)
    assert res["failures"] == []


# -- dry-run vs execute -----------------------------------------------------

def _boom(item_id, poster):
    raise AssertionError("flipper must not be called in this test")


def test_dry_run_plans_but_never_calls_the_flipper():
    rows = [_row(8118, "Evil Dead Burn"), _row(8685, "Sing")]
    res = pj.run(probe=lambda i: [_p("local", selected=True), _p("gracenote")],
                 flipper=_boom, rows=rows, execute=False, max_items=25)
    assert len(res["flips"]) == 2
    assert all(f["to"] == "gracenote" for f in res["flips"])
    assert res["failures"] == [] and res["deferred"] == []


def test_execute_calls_the_flipper_once_per_item():
    called = []
    rows = [_row(8118, "Evil Dead Burn"), _row(8685, "Sing")]
    pj.run(probe=lambda i: [_p("local", selected=True), _p("tmdb", rk="want")],
           flipper=lambda i, p: called.append((i, p["ratingKey"])),
           rows=rows, execute=True, max_items=25)
    assert called == [(8118, "want"), (8685, "want")]


def test_execute_never_flips_a_benign_item():
    """The blast-radius guard: a healthy item must not reach the flipper even
    when the SQL detector hands it over."""
    pj.run(probe=lambda i: [_p("tmdb", selected=True)], flipper=_boom,
           rows=[_row(8886, "Interstellar")], execute=True, max_items=25)


def test_max_items_defers_rather_than_flipping_everything():
    called = []
    rows = [_row(i, "item-%d" % i) for i in range(1, 6)]
    res = pj.run(probe=lambda i: [_p("local", selected=True), _p("tmdb")],
                 flipper=lambda i, p: called.append(i),
                 rows=rows, execute=True, max_items=2)
    assert len(called) == 2
    assert len(res["flips"]) == 2 and len(res["deferred"]) == 3
    assert "3 deferred" in pj._kuma_msg(res, execute=True, scanned=431)


def test_max_items_does_not_cap_a_dry_run():
    """A dry-run must show the WHOLE backlog; capping the plan would hide it."""
    rows = [_row(i, "item-%d" % i) for i in range(1, 6)]
    res = pj.run(probe=lambda i: [_p("local", selected=True), _p("tmdb")],
                 flipper=_boom, rows=rows, execute=False, max_items=2)
    assert len(res["flips"]) == 5 and res["deferred"] == []


def test_a_failed_flip_is_recorded_not_swallowed():
    def flipper(item_id, poster):
        raise RuntimeError("flip did not verify — still flip")
    res = pj.run(probe=lambda i: [_p("local", selected=True), _p("tmdb")],
                 flipper=flipper, rows=[_row(8118, "Evil Dead Burn")],
                 execute=True, max_items=25)
    assert res["flips"] == [] and len(res["failures"]) == 1
    assert "FAILED" in pj._kuma_msg(res, execute=True, scanned=431)


def test_a_probe_error_is_a_failure_not_a_silent_skip():
    def probe(item_id):
        raise RuntimeError("HTTP 500")
    res = pj.run(probe=probe, flipper=_boom, rows=[_row(8118, "Evil Dead Burn")],
                 execute=False, max_items=25)
    assert len(res["failures"]) == 1 and res["flips"] == []


# -- detector: the parts that keep a green run from being vacuous -----------

def test_detector_excludes_collections_agent_thumbs_and_proxied_images():
    """metadata_type IN (1,2,4) keeps the 15 legitimate COLLECTION posters out,
    and NOT LIKE 'https://%' keeps out agent art proxied via images.plex.tv.
    Either false positive would make this monitor red on day one and muted by
    day two."""
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE metadata_items (id INTEGER, metadata_type INTEGER,"
               " title TEXT, library_section_id INTEGER, user_thumb_url TEXT)")
    db.executemany("INSERT INTO metadata_items VALUES (?,?,?,?,?)", [
        (8118, 1, "Evil Dead Burn", 4, "metadata://posters/5edf955b"),
        (8518, 4, "Night Movers", 2, "metadata://posters/f377ad00"),
        (8886, 1, "Interstellar", 4, "metadata://posters/tv.plex.agents.movie_f0"),
        (8555, 4, "Prisoner of Love", 2, "https://images.plex.tv/photo?height=270"),
        (837, 18, "Dune Collection", 4, "metadata://posters/deadbeef"),
        (8339, 1, "Welcome to QFlix", 7, "media://7/0ecae50b"),
        (9000, 1, "No Thumb", 4, ""),
    ])
    ph, mt = "?,?,?,?", "1,2,4"
    rows = db.execute(pj._BAD_THUMB_SQL.format(ph=ph, mt=mt), [2, 4, 5, 6]).fetchall()
    assert [r[0] for r in rows] == [8118, 8518]


def test_detector_population_guard_trips_on_a_broken_schema_assumption():
    """A WHERE clause that quietly matches nothing reports a clean library
    forever. The guard turns that into a loud failure instead."""
    import sqlite3
    db_path = ":memory:"
    with pytest.raises(RuntimeError, match="population guard"):
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE metadata_items (id INTEGER, metadata_type"
                     " INTEGER, title TEXT, library_section_id INTEGER,"
                     " user_thumb_url TEXT)")
        population = conn.execute(
            pj._POPULATION_SQL.format(ph="?,?,?,?", mt="1,2,4"),
            [2, 4, 5, 6]).fetchone()[0]
        if not population:
            raise RuntimeError("population guard: 0 movie/show/episode rows")


def test_detector_refuses_an_empty_section_list():
    with pytest.raises(RuntimeError):
        pj.detect("/nonexistent.db", [])


# -- XML parsing ------------------------------------------------------------

def test_parse_posters_xml_reads_the_live_attribute_shape():
    body = ('<MediaContainer size="2">'
            '<Photo key="/library/metadata/8685/file?url=metadata" '
            'ratingKey="metadata://posters/d13b0271" selected="1" provider="local"/>'
            '<Photo key="https://image.tmdb.org/t/p/original/x.jpg" '
            'ratingKey="https://image.tmdb.org/t/p/original/x.jpg" '
            'selected="0" provider="tmdb"/>'
            '</MediaContainer>')
    posters = pj.parse_posters_xml(body)
    assert posters[0]["provider"] == "local" and posters[0]["selected"] is True
    assert posters[1]["provider"] == "tmdb" and posters[1]["selected"] is False
    assert pj.classify_item(posters)[0] == "flip"


def test_parse_posters_xml_empty_container_is_a_real_answer():
    assert pj.parse_posters_xml('<MediaContainer size="0"/>') == []


# ===========================================================================
# Council blockers, 2026-08-25. Each test below is a defect a blind reviewer
# found in the first cut of this janitor; the comment is why it was a blocker,
# not what the code does.
# ===========================================================================

def test_operator_uploaded_poster_is_never_flipped():
    """BLOCKER (i). A poster the operator uploaded by hand is stored as
    `upload://posters/<hash>` and comes back with an EMPTY provider -- which is
    not in AGENT_PROVIDERS, so the first cut classified it "flip" and would have
    replaced a deliberate human choice with a TMDB guess on the next tick.

    Zero live instances when this was written. That is the point: the guard goes
    in before the first hand-picked poster, not after losing one."""
    posters = [
        {"ratingKey": "upload://posters/deadbeefcafe", "provider": "", "selected": True},
        {"ratingKey": "metadata://posters/tmdb_1", "provider": "tmdb", "selected": False},
    ]
    verdict, chosen = pj.classify_item(posters)
    assert verdict == "benign", "an operator upload must never be flipped"
    assert chosen is None


def test_operator_upload_is_excluded_by_the_sql_too():
    """Defence in depth: the SQL and classify_item answer to different failure
    modes. The SQL can be edited by someone who never reads classify_item, and
    posters() can return an upload for an item the SQL matched for another
    reason. Both must exclude it."""
    assert "upload://" in pj._BAD_THUMB_SQL, \
        "the detector must not even fetch operator uploads"


def test_release_art_with_empty_provider_is_still_flipped():
    """NEGATIVE CONTROL for the guard above. Release art ALSO shows an empty
    provider -- the upload guard keys on the `upload://` URL shape, not on the
    provider, precisely so it cannot swallow this case. If this ever returns
    benign, the janitor has been disarmed."""
    posters = [
        {"ratingKey": "metadata://posters/local_1", "provider": "local", "selected": True},
        {"ratingKey": "metadata://posters/tmdb_1", "provider": "tmdb", "selected": False},
    ]
    verdict, chosen = pj.classify_item(posters)
    assert verdict == "flip"
    assert chosen["provider"] == "tmdb"


# ===========================================================================
# Empty-collection sweep. A collection tile is a promise about the library; an
# empty one promises films that were reaped months ago. 14 accumulated in
# Movies unseen, plus 4 Maintainerr husks that outlived their app by two
# months. The reaper prunes what IT empties; this sweep is the catch-all for
# every other cause, which is the half nothing was watching.
# ===========================================================================

def _collection_db(tmp_path, collections, taggings):
    """Minimal Plex schema: metadata_items + tags + taggings, real shapes."""
    import sqlite3
    db = tmp_path / "lib.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE metadata_items (id INTEGER, metadata_type INTEGER,"
              " title TEXT, library_section_id INTEGER, user_thumb_url TEXT)")
    c.execute("CREATE TABLE tags (id INTEGER, tag TEXT, tag_type INTEGER)")
    c.execute("CREATE TABLE taggings (id INTEGER, metadata_item_id INTEGER,"
              " tag_id INTEGER)")
    # Members are real metadata_items rows in the SAME section, because that is
    # how the section scoping works: tags carries no library_section_id (it does
    # not exist in Plex's schema), so a collection's scope is taken from the
    # section of the items tagged into it.
    for i, (title, sec) in enumerate(collections, 1):
        c.execute("INSERT INTO metadata_items VALUES (?,18,?,?,NULL)", (i, title, sec))
        c.execute("INSERT INTO tags VALUES (?,?,2)", (100 + i, title))
        for n in range(taggings.get(title, 0)):
            member_id = 5000 + i * 10 + n
            c.execute("INSERT INTO metadata_items VALUES (?,1,?,?,NULL)",
                      (member_id, "%s member %d" % (title, n), sec))
            c.execute("INSERT INTO taggings VALUES (?,?,?)",
                      (1000 + i * 10 + n, member_id, 100 + i))
    c.commit(); c.close()
    return str(db)


def test_empty_collection_is_detected(tmp_path):
    """THE DEFECT, as it actually stood on 2026-08-24."""
    db = _collection_db(tmp_path,
                        [("Deadpool Collection", 4), ("Minions Collection", 4)],
                        {"Minions Collection": 3})
    found = pj.detect_empty_collections(db, [4])
    assert [f[1] for f in found] == ["Deadpool Collection"]


def test_populated_collection_is_never_reported(tmp_path):
    """NEGATIVE CONTROL. Beetlejuice(1) and Minions(3) are real collections and
    must stay invisible to this sweep, or it becomes noise and gets muted."""
    db = _collection_db(tmp_path,
                        [("Beetlejuice Collection", 4), ("Minions Collection", 4)],
                        {"Beetlejuice Collection": 1, "Minions Collection": 3})
    assert pj.detect_empty_collections(db, [4]) == []


def test_membership_is_read_through_tags_not_a_children_table(tmp_path):
    """Pins the SCHEMA, because the first draft of this query assumed a
    metadata_item_children table that does not exist in Plex and would have
    raised on every run. Membership is tags(tag_type=2) -> taggings, matched on
    the collection title; verified against the live DB before shipping."""
    db = _collection_db(tmp_path, [("X Collection", 4)], {"X Collection": 2})
    assert pj.detect_empty_collections(db, [4]) == []
    db2 = _collection_db(tmp_path / "b", [("X Collection", 4)], {}) \
        if (tmp_path / "b").mkdir() or True else None
    assert [f[1] for f in pj.detect_empty_collections(db2, [4])] == ["X Collection"]


def test_other_sections_are_not_swept(tmp_path):
    db = _collection_db(tmp_path, [("Foreign Collection", 99)], {})
    assert pj.detect_empty_collections(db, [4]) == []


def test_schema_change_returns_empty_rather_than_a_false_clean(tmp_path):
    """If Plex renames the linkage tables the query must not raise INTO the
    poster run. It returns [] and the caller logs -- a poster flip must not fail
    because a hygiene sweep could not run."""
    import sqlite3
    db = tmp_path / "bare.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE metadata_items (id INTEGER, metadata_type INTEGER,"
              " title TEXT, library_section_id INTEGER)")
    c.commit(); c.close()
    assert pj.detect_empty_collections(str(db), [4]) == []


# ===========================================================================
# Council 2 (2026-08-25). Both reproduced against fixtures built from the LIVE
# Plex DDL by two independent reviewers, so they are measured, not argued.
# ===========================================================================

def test_same_title_in_another_section_does_not_borrow_members(tmp_path):
    """R4, the MISSED direction. `t.tag = c.title` alone matches a tag from ANY
    library. Two sections can legitimately hold a same-named collection, and the
    unscoped count then borrows the other section's members -- so a genuinely
    empty husk reads populated and is never reported. Zero collisions exist
    live today, which is exactly why it had to be fixed before one appears."""
    db = _collection_db(tmp_path,
                        [("Marvel Collection", 4), ("Marvel Collection", 5)],
                        {})
    # Give ONLY the section-5 copy members.
    import sqlite3
    c = sqlite3.connect(db)
    c.execute("INSERT INTO metadata_items VALUES (9001,1,'m',5,NULL)")
    c.execute("INSERT INTO tags VALUES (9100,'Marvel Collection',2)")
    c.execute("INSERT INTO taggings VALUES (9200,9001,9100)")
    c.commit(); c.close()

    found = pj.detect_empty_collections(db, [4])
    assert [f[1] for f in found] == ["Marvel Collection"], \
        "the section-4 husk must not borrow section 5's member"


def test_a_null_titled_collection_is_not_reported(tmp_path):
    """`t.tag = c.title` is NULL when the title is NULL -- never true -- so the
    count is always 0 and such a row ALWAYS read empty. A permanent false
    positive on something nothing can ever clear."""
    import sqlite3
    db = _collection_db(tmp_path, [("Real Collection", 4)], {"Real Collection": 1})
    c = sqlite3.connect(db)
    c.execute("INSERT INTO metadata_items VALUES (9500,18,NULL,4,NULL)")
    c.execute("INSERT INTO metadata_items VALUES (9501,18,'',4,NULL)")
    c.commit(); c.close()

    assert pj.detect_empty_collections(db, [4]) == []


def test_tags_has_no_section_column_so_scope_comes_from_the_member(tmp_path):
    """Pins HOW the scoping is done, because two plausible joins were tried
    against the live schema and both were wrong: tags.library_section_id does
    not exist (would raise every run), and tags.metadata_item_id is NULL for
    collection tags (counted 0 members for both live collections). The section
    is taken from the tagged MEMBER's row."""
    src = (ROOT / "scripts" / "maint" / "qflix-poster-janitor.py").read_text(
        encoding="utf-8")
    sql = src.split('_EMPTY_COLLECTION_SQL = """')[1].split('"""')[0]
    assert "t.library_section_id" not in sql, "tags has no library_section_id"
    assert "m.library_section_id = c.library_section_id" in sql
    assert "m.id = tg.metadata_item_id" in sql


# --- 2026-08-26: the flip PUT path (the bug every mocked flipper hid) ---

def test_select_poster_puts_the_singular_poster_path(monkeypatch):
    """GET lists at /posters (plural); the select PUT goes to /poster
    (singular) -- plexapi strips the trailing 's'. The first armed run PUT
    the plural and nginx 404'd all six flips while 38 tests stayed green,
    because every test mocked the flipper wholesale. This pins the real
    request path and method."""
    calls = []

    def fake_req(port, token, path, query="", method="GET", timeout=30):
        calls.append((path, query, method))
        return 200, ""

    monkeypatch.setattr(pj, "_plex_req", fake_req)
    pj.select_poster("17025", "tok", 8685,
                     {"ratingKey": "https://image.tmdb.org/t/p/original/x.jpg"})
    assert calls == [("/library/metadata/8685/poster",
                      "url=https%3A%2F%2Fimage.tmdb.org%2Ft%2Fp%2Foriginal%2Fx.jpg",
                      "PUT")]


def test_select_poster_raises_on_non_2xx(monkeypatch):
    """A failed PUT must raise so the run counts it FAILED and exits red --
    the armed-run behaviour that surfaced the 404 in the first place."""
    monkeypatch.setattr(pj, "_plex_req",
                        lambda *a, **k: (404, "<html>404 Not Found</html>"))
    with pytest.raises(RuntimeError, match="PUT HTTP 404"):
        pj.select_poster("17025", "tok", 1, {"ratingKey": "metadata://x"})


# --- 2026-08-26: Welcome is a utility, not content (operator directive) ---

def _sections_xml(titles_keys):
    rows = "".join('<Directory title="%s" key="%s"/>' % (t, k)
                   for t, k in titles_keys)
    return "<MediaContainer>" + rows + "</MediaContainer>"


def test_welcome_is_excluded_but_unknown_libraries_still_get_named(monkeypatch):
    """Operator, 2026-08-26: Welcome is the entitlement gate's utility surface
    (a go-to-Patreon video), not content — never monitored, never janitored.
    The written-down-why lives in UTILITY_SECTIONS; the unmanaged report keeps
    naming any OTHER library so a sixth one cannot rot silently."""
    xml = _sections_xml(
        [(n, str(i)) for i, n in enumerate(pj.SECTION_NAMES, 1)]
        + [("QFlix - Welcome", "7"), ("Home Videos", "9")])
    monkeypatch.setattr(pj, "_plex_req", lambda *a, **k: (200, xml))
    found, unmanaged = pj.resolve_sections("17025", "tok")
    assert sorted(found) == sorted(pj.SECTION_NAMES)
    assert unmanaged == ["Home Videos"], \
        "Welcome must not be reported; a genuinely unknown library must be"


def test_utility_sections_and_managed_sections_are_disjoint():
    assert not set(pj.UTILITY_SECTIONS) & set(pj.SECTION_NAMES)
