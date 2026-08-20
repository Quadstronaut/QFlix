"""tests/unit/test_qflix_reaper.py — safety-envelope acceptance tests for the
QFlix 60-day reaper (Maintainerr replacement).

This script deletes real media; the tests therefore center on the invariants
that keep it from deleting the WRONG thing or deleting at all when it shouldn't:

  * dry-run mutates NOTHING (no delete fn called, no manifest written, exit 0)
  * nothing is deleted unless it positively resolves to an *arr id
    (UNRESOLVED -> skip + exit 1, delete fns never called for it)
  * caps hold (max-items / max-pct trip -> exit 2, BEFORE any mutation)
  * exclusions hold (tmdb/tvdb/plex/title each prevent deletion)
  * manifest is written on --execute BEFORE the first delete
  * a per-item failure does not abort the run -> exit 1 (distinct from 2 / 3)
  * Seerr reconciliation reads seerr.*, deletes only orphaned available media
  * empty anime libs / empty arr instances -> zero candidates, zero errors
  * idempotent re-run -> exit 0 no-op

We load the hyphenated module via importlib (repo convention) and prove the
no-mutation / no-unresolved-delete properties by COUNTING boundary-fn calls
(MagicMock), not by inspecting logs.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REAPER_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "maint" / "qflix-reaper.py"
)


@pytest.fixture
def reaper():
    spec = importlib.util.spec_from_file_location("qflix_reaper", _REAPER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# A tiny fake ArrClient: resolve returns a match for tmdb/tvdb ids we seed, and
# records every delete call so tests can assert mutation counts.
# ---------------------------------------------------------------------------
class FakeArr:
    def __init__(self, slug, movies=None, series=None):
        self.slug = slug
        self.movies = movies or []   # [{id,tmdbId,hasFile}]
        self.series = series or []   # [{id,tvdbId}]
        self.deletes = []            # list of (path, query)

    def get(self, path, query="", timeout=None):
        if path == "/movie":
            return 200, self.movies
        if path == "/series":
            return 200, self.series
        # Single-record reads. do_delete_* re-reads here after a non-2xx
        # delete, and 404 is its proof that the delete landed anyway, so this
        # fake has to answer from its OWN state instead of blanket-404ing.
        # While it did, a FlakyArr that returned 500 without removing anything
        # was read as a successful delete and the two partial-failure guards
        # below silently stopped testing partial failure.
        for prefix, rows in (("/movie/", self.movies), ("/series/", self.series)):
            if path.startswith(prefix):
                ident = path[len(prefix):]
                if ident.isdigit() and any(r.get("id") == int(ident) for r in rows):
                    return 200, None
                return 404, None
        return 404, None

    def delete(self, path, query="", timeout=None):
        self.deletes.append((path, query))
        for prefix, rows in (("/movie/", self.movies), ("/series/", self.series)):
            if path.startswith(prefix):
                ident = path[len(prefix):]
                if ident.isdigit():
                    rows[:] = [r for r in rows if r.get("id") != int(ident)]
        return 200, ""


def _install_plex(reaper, monkeypatch, items_by_lib, ids_by_rk):
    """Wire Plex boundary fns: creds ok, sections present for the 4 libs, items
    per library, external ids per ratingKey. Counts refresh/emptyTrash calls."""
    monkeypatch.setattr(reaper, "_plex_creds", lambda: ("17025", "tok"))

    sections = {name: str(100 + i) for i, name in enumerate([
        "QFlix - Movies", "QFlix - Anime Movies", "QFlix - TV", "QFlix - Anime",
    ])}
    monkeypatch.setattr(reaper, "plex_sections", lambda p, t: (sections, None))

    key_to_lib = {v: k for k, v in sections.items()}

    def fake_items(p, t, key):
        lib = key_to_lib.get(str(key))
        return list(items_by_lib.get(lib, [])), None

    monkeypatch.setattr(reaper, "plex_items", fake_items)
    monkeypatch.setattr(
        reaper, "item_external_ids",
        lambda p, t, rk: ids_by_rk.get(str(rk), {"tmdbId": None, "tvdbId": None}),
    )

    refresh_calls = []
    trash_calls = []
    monkeypatch.setattr(reaper, "plex_refresh",
                        lambda p, t, k: refresh_calls.append(k) or True)
    monkeypatch.setattr(reaper, "plex_empty_trash",
                        lambda p, t, k: trash_calls.append(k) or True)
    return sections, refresh_calls, trash_calls


def _silence_side_effects(reaper, monkeypatch):
    """Notify + Kuma are best-effort; stub them so tests don't touch the net."""
    monkeypatch.setattr(reaper, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(reaper, "_push_kuma", lambda *a, **k: None)
    monkeypatch.setattr(reaper, "reconcile_seerr", lambda execute: (0, 0))


def _old(reaper, days_over=10, threshold=60):
    """An addedAt epoch that is `days_over` days OLDER than the threshold."""
    import time
    return int(time.time()) - (threshold + days_over) * reaper.DAY_SECONDS


def _args(reaper, **over):
    # max_pct defaults to 100 here so small single-item fixtures don't trip the
    # per-library percent cap (one candidate in a one-item library is 100%); the
    # cap-trip tests set max_pct explicitly. max_items default mirrors prod (50).
    base = dict(
        execute=False, threshold_days=60, exclude_file="/nonexistent-on-purpose",
        max_items=50, max_pct=100, force=False, manifest_dir=None,
        library=None, emit_json=False,
        orphan_grace_hours=24.0, orphan_remind_days=7.0, orphan_state=None,
    )
    base.update(over)
    return type("A", (), base)()


# ===========================================================================
# 1. Dry-run mutates nothing
# ===========================================================================
def test_dry_run_no_mutation_exit_0(reaper, tmpdir, monkeypatch):
    movie = {"ratingKey": "1", "title": "Old Movie", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    items = {"QFlix - Movies": [movie]}
    ids = {"1": {"tmdbId": 603, "tvdbId": None}}
    _install_plex(reaper, monkeypatch, items, ids)
    _silence_side_effects(reaper, monkeypatch)

    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    manifest_dir = Path(str(tmpdir)) / "manifests"
    manifest_dir.mkdir()

    rc = reaper.run(_args(reaper, execute=False, manifest_dir=str(manifest_dir)))

    assert rc == reaper.EXIT_OK
    assert fake.deletes == []                       # ZERO deletes
    assert list(manifest_dir.iterdir()) == []       # NO manifest in dry-run


# ===========================================================================
# 2. Resolved item is deleted via exact endpoint with import-exclusion=false
# ===========================================================================
def test_execute_deletes_resolved_movie_exact_endpoint(reaper, tmpdir, monkeypatch):
    movie = {"ratingKey": "1", "title": "Old Movie", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 603, "tvdbId": None}})
    _silence_side_effects(reaper, monkeypatch)

    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))

    assert rc == reaper.EXIT_OK
    assert len(fake.deletes) == 1
    path, query = fake.deletes[0]
    assert path == "/movie/42"
    assert "deleteFiles=true" in query
    assert "addImportExclusion=false" in query


def test_execute_deletes_resolved_series_exact_endpoint(reaper, tmpdir, monkeypatch):
    show = {"ratingKey": "9", "title": "Old Show", "year": 2001,
            "addedAt": _old(reaper), "sizeGB": 30.0}
    _install_plex(reaper, monkeypatch, {"QFlix - TV": [show]},
                  {"9": {"tmdbId": None, "tvdbId": 81189}})
    _silence_side_effects(reaper, monkeypatch)

    fake = FakeArr("sonarr", series=[{"id": 7, "tvdbId": 81189}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))

    assert rc == reaper.EXIT_OK
    assert fake.deletes[0][0] == "/series/7"
    assert "addImportListExclusion=false" in fake.deletes[0][1]


# ===========================================================================
# 3. Unresolved item is NEVER deleted; run exits non-zero (partial)
# ===========================================================================
def test_unresolved_is_skipped_not_deleted_exit_1(reaper, tmpdir, monkeypatch):
    movie = {"ratingKey": "1", "title": "Ghost Movie", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 999, "tvdbId": None}})   # tmdb 999 not in Radarr
    _silence_side_effects(reaper, monkeypatch)

    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))

    assert rc == reaper.EXIT_PARTIAL
    assert fake.deletes == []          # the unresolved item was NOT deleted


def test_ambiguous_match_is_unresolved(reaper, tmpdir, monkeypatch):
    movie = {"ratingKey": "1", "title": "Dup Movie", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 603, "tvdbId": None}})
    _silence_side_effects(reaper, monkeypatch)
    # Two Radarr movies share tmdb 603 -> ambiguous -> no unique match.
    fake = FakeArr("radarr", movies=[
        {"id": 42, "tmdbId": 603, "hasFile": True},
        {"id": 43, "tmdbId": 603, "hasFile": True},
    ])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_PARTIAL
    assert fake.deletes == []


# ===========================================================================
# 4. Exclusions hold (tmdb / tvdb / plex / title)
# ===========================================================================
@pytest.mark.parametrize("rule,rk,ids", [
    ("tmdb:603", "1", {"tmdbId": 603, "tvdbId": None}),
    ("plex:1", "1", {"tmdbId": 603, "tvdbId": None}),
    ("Old Movie", "1", {"tmdbId": 603, "tvdbId": None}),
])
def test_exclusion_prevents_movie_delete(reaper, tmpdir, monkeypatch, rule, rk, ids):
    exclude = Path(str(tmpdir)) / "ex.txt"
    exclude.write_text("# comment\n\n   " + rule + "  \n", encoding="utf-8")

    movie = {"ratingKey": rk, "title": "Old Movie", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]}, {rk: ids})
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, exclude_file=str(exclude),
                          manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK     # excluded -> nothing to do, clean
    assert fake.deletes == []


def test_tvdb_exclusion_prevents_series_delete(reaper, tmpdir, monkeypatch):
    exclude = Path(str(tmpdir)) / "ex.txt"
    exclude.write_text("tvdb:81189\n", encoding="utf-8")
    show = {"ratingKey": "9", "title": "Old Show", "year": 2001,
            "addedAt": _old(reaper), "sizeGB": 30.0}
    _install_plex(reaper, monkeypatch, {"QFlix - TV": [show]},
                  {"9": {"tmdbId": None, "tvdbId": 81189}})
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("sonarr", series=[{"id": 7, "tvdbId": 81189}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, exclude_file=str(exclude),
                          manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    assert fake.deletes == []


def test_missing_exclude_file_warns_and_proceeds(reaper, monkeypatch):
    rules = reaper.load_exclusions(Path("/definitely/not/here.exclude"))
    assert rules == set()


# ===========================================================================
# 5. Caps: max-items and max-pct trip -> exit 2 BEFORE mutation; --force overrides
# ===========================================================================
def test_max_items_cap_defers_excess_processes_oldest(reaper, tmpdir, monkeypatch):
    # max-items is a per-run RATE LIMIT, not a tripwire: a backlog larger than the
    # cap must DELETE the oldest max_items and DEFER the rest to the next run —
    # never abort the whole run to zero (the 2026-07-13 abort-to-zero failure on a
    # space-constrained box). Ages are distinct so "oldest 3" is deterministic:
    # ratingKey i has addedAt = base + i, so i=0 is the oldest.
    base = _old(reaper, days_over=100)
    items = [{"ratingKey": str(i), "title": "M%d" % i, "year": 2000,
              "addedAt": base + i, "sizeGB": 1.0} for i in range(5)]
    ids = {str(i): {"tmdbId": 600 + i, "tvdbId": None} for i in range(5)}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": items}, ids)
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 100 + i, "tmdbId": 600 + i, "hasFile": True}
                                     for i in range(5)])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, max_items=3, max_pct=100,
                          manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    # oldest 3 (ratingKeys 0,1,2 -> tmdbId 600,601,602 -> arrId 100,101,102) deleted
    assert len(fake.deletes) == 3
    assert sorted(int(p.rsplit("/", 1)[-1]) for p, _ in fake.deletes) == [100, 101, 102]
    # manifest records exactly the 3 processed (the deferred 2 are not in it)
    manifests = list(Path(str(tmpdir)).glob("qflix-reaper-*.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["total_count"] == 3


def test_max_items_cap_no_defer_when_at_or_below(reaper, tmpdir, monkeypatch):
    # Exactly at the cap: no deferral, all deleted, clean exit.
    items = [{"ratingKey": str(i), "title": "M%d" % i, "year": 2000,
              "addedAt": _old(reaper) - i, "sizeGB": 1.0} for i in range(3)]
    ids = {str(i): {"tmdbId": 600 + i, "tvdbId": None} for i in range(3)}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": items}, ids)
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 100 + i, "tmdbId": 600 + i, "hasFile": True}
                                     for i in range(3)])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, max_items=3, max_pct=100,
                          manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    assert len(fake.deletes) == 3


def test_max_pct_cap_trips_exit_2(reaper, tmpdir, monkeypatch):
    # 4 candidates out of a 5-item library = 80% > 30%.
    cands = [{"ratingKey": str(i), "title": "M%d" % i, "year": 2000,
              "addedAt": _old(reaper), "sizeGB": 1.0} for i in range(4)]
    fresh = {"ratingKey": "99", "title": "Fresh", "year": 2024,
             "addedAt": int(__import__("time").time()), "sizeGB": 1.0}
    ids = {str(i): {"tmdbId": 600 + i, "tvdbId": None} for i in range(4)}
    ids["99"] = {"tmdbId": 700, "tvdbId": None}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": cands + [fresh]}, ids)
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 100 + i, "tmdbId": 600 + i, "hasFile": True}
                                     for i in range(4)])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, max_items=100, max_pct=30,
                          manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_CAP
    assert fake.deletes == []


def test_force_overrides_cap_and_deletes(reaper, tmpdir, monkeypatch):
    items = [{"ratingKey": str(i), "title": "M%d" % i, "year": 2000,
              "addedAt": _old(reaper), "sizeGB": 1.0} for i in range(5)]
    ids = {str(i): {"tmdbId": 600 + i, "tvdbId": None} for i in range(5)}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": items}, ids)
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 100 + i, "tmdbId": 600 + i, "hasFile": True}
                                     for i in range(5)])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, max_items=3, max_pct=1, force=True,
                          manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    assert len(fake.deletes) == 5


# ===========================================================================
# 6. Manifest written on --execute BEFORE first delete
# ===========================================================================
def test_manifest_written_before_first_delete(reaper, tmpdir, monkeypatch):
    movie = {"ratingKey": "1", "title": "Old Movie", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 603, "tvdbId": None}})
    _silence_side_effects(reaper, monkeypatch)

    manifest_seen_at_delete = {}

    class WatchArr(FakeArr):
        def delete(self, path, query="", timeout=None):
            manifest_seen_at_delete["files"] = list(
                Path(str(tmpdir)).glob("qflix-reaper-*.json"))
            return super().delete(path, query, timeout)

    fake = WatchArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    # The manifest already existed at the moment of the first delete.
    assert manifest_seen_at_delete["files"], "manifest must exist before first DELETE"
    doc = json.loads(manifest_seen_at_delete["files"][0].read_text())
    assert doc["total_count"] == 1
    assert doc["candidates"][0]["arrId"] == 42
    assert doc["flags"]["execute"] is True


# ===========================================================================
# 7. Per-item delete failure does not abort -> exit 1 (distinct from cap=2)
# ===========================================================================
def test_partial_delete_failure_exit_1(reaper, tmpdir, monkeypatch):
    items = [{"ratingKey": str(i), "title": "M%d" % i, "year": 2000,
              "addedAt": _old(reaper), "sizeGB": 1.0} for i in range(2)]
    ids = {str(i): {"tmdbId": 600 + i, "tvdbId": None} for i in range(2)}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": items}, ids)
    _silence_side_effects(reaper, monkeypatch)

    class FlakyArr(FakeArr):
        def delete(self, path, query="", timeout=None):
            self.deletes.append((path, query))
            # Fail the first delete, succeed the second.
            return (500, "") if len(self.deletes) == 1 else (200, "")

    fake = FlakyArr("radarr", movies=[{"id": 100 + i, "tmdbId": 600 + i, "hasFile": True}
                                      for i in range(2)])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_PARTIAL
    assert len(fake.deletes) == 2           # run did NOT abort after the failure


# ===========================================================================
# 8. Boundary: item exactly at the age cutoff is NOT a candidate
# ===========================================================================
def test_item_exactly_at_threshold_not_deleted(reaper, tmpdir, monkeypatch):
    import time
    exactly = int(time.time()) - 60 * reaper.DAY_SECONDS    # age == threshold, not >
    movie = {"ratingKey": "1", "title": "Edge Movie", "year": 1999,
             "addedAt": exactly, "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 603, "tvdbId": None}})
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, threshold_days=60,
                          manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    assert fake.deletes == []               # strictly-greater-than boundary holds


# ===========================================================================
# 9. Empty anime libs / empty arr instances -> zero candidates, zero errors
# ===========================================================================
def test_empty_libraries_no_candidates_no_error(reaper, tmpdir, monkeypatch):
    # No items in any library at all.
    _install_plex(reaper, monkeypatch, {}, {})
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr")
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    assert fake.deletes == []


def test_idempotent_rerun_after_delete_is_noop(reaper, tmpdir, monkeypatch):
    # After a successful execute, Plex no longer lists the item -> zero candidates.
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": []}, {})
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    assert fake.deletes == []


# ===========================================================================
# 10. Plex refresh + emptyTrash fire after a library's deletes
# ===========================================================================
def test_plex_refresh_and_trash_after_delete(reaper, tmpdir, monkeypatch):
    movie = {"ratingKey": "1", "title": "Old Movie", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _, refresh_calls, trash_calls = _install_plex(
        reaper, monkeypatch, {"QFlix - Movies": [movie]},
        {"1": {"tmdbId": 603, "tvdbId": None}})
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert len(refresh_calls) == 1
    assert len(trash_calls) == 1


def test_dry_run_no_plex_refresh_or_trash(reaper, tmpdir, monkeypatch):
    movie = {"ratingKey": "1", "title": "Old Movie", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _, refresh_calls, trash_calls = _install_plex(
        reaper, monkeypatch, {"QFlix - Movies": [movie]},
        {"1": {"tmdbId": 603, "tvdbId": None}})
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    reaper.run(_args(reaper, execute=False, manifest_dir=str(tmpdir)))
    assert refresh_calls == []
    assert trash_calls == []


# ===========================================================================
# 11. Seerr reconciliation: deletes only orphaned available media; reads seerr.*
# ===========================================================================
def test_seerr_reconcile_deletes_only_orphans(reaper, monkeypatch):
    monkeypatch.setattr(reaper, "_seerr_creds", lambda: ("42011", "seerrkey"))

    media_rows = {
        "results": [
            {"id": 1, "mediaType": "movie", "tmdbId": 603},   # backed by radarr w/file -> keep
            {"id": 2, "mediaType": "movie", "tmdbId": 999},   # orphan -> delete
            {"id": 3, "mediaType": "tv", "tvdbId": 81189},    # backed by sonarr -> keep
            {"id": 4, "mediaType": "tv", "tvdbId": 55555},    # orphan -> delete
        ]
    }
    seen = {"requests": []}

    def fake_req(method, port, key, path, query="", timeout=30):
        seen["requests"].append((method, port, key, path, query))
        if method == "GET" and path == "/api/v1/media":
            return 200, media_rows
        if method == "DELETE":
            return 200, ""
        return 404, None

    monkeypatch.setattr(reaper, "_seerr_req", fake_req)

    def fake_arr(slug):
        if slug in ("radarr", "radarr2"):
            return FakeArr(slug, movies=[{"id": 1, "tmdbId": 603, "hasFile": True}])
        return FakeArr(slug, series=[{"id": 1, "tvdbId": 81189}])

    monkeypatch.setattr(reaper, "_arr_client", fake_arr)

    deleted, failed = reaper.reconcile_seerr(execute=True)
    assert deleted == 2 and failed == 0
    deletes = [r for r in seen["requests"] if r[0] == "DELETE"]
    assert {r[3] for r in deletes} == {"/api/v1/media/2", "/api/v1/media/4"}
    # creds came from seerr.* (port 42011), never jellyseerr.*
    assert all(r[1] == "42011" for r in seen["requests"])


def test_seerr_reconcile_paginates_past_page_cap(reaper, monkeypatch):
    # Availability spanning multiple pages must ALL be reconciled — no silent cap.
    monkeypatch.setattr(reaper, "_seerr_creds", lambda: ("42011", "seerrkey"))
    import urllib.parse as _up
    page = reaper._SEERR_MEDIA_PAGE
    total = page * 2 + 5                      # spans 3 pages (last one short)
    rows = [{"id": i, "mediaType": "movie", "tmdbId": 900000 + i} for i in range(total)]
    seen_skips = []
    deletes = []

    def fake_req(method, port, key, path, query="", timeout=30):
        if method == "GET" and path == "/api/v1/media":
            q = dict(_up.parse_qsl(query))
            skip = int(q.get("skip", 0))
            take = int(q.get("take", page))
            seen_skips.append(skip)
            return 200, {"results": rows[skip:skip + take], "pageInfo": {"results": total}}
        if method == "DELETE":
            deletes.append(path)
            return 200, ""
        return 404, None

    monkeypatch.setattr(reaper, "_seerr_req", fake_req)
    # empty arr index -> every available row is an orphan -> all get deleted
    monkeypatch.setattr(reaper, "_arr_client",
                        lambda slug: FakeArr(slug, movies=[], series=[]))

    deleted, failed = reaper.reconcile_seerr(execute=True)
    assert deleted == total and failed == 0           # ALL rows, not just page 1
    assert seen_skips == [0, page, page * 2]           # walked every page
    assert len(deletes) == total


def test_seerr_reconcile_midpagination_failure_reconciles_partial(reaper, monkeypatch):
    # A page failure mid-walk must reconcile what was fetched, not abort to zero.
    monkeypatch.setattr(reaper, "_seerr_creds", lambda: ("42011", "seerrkey"))
    import urllib.parse as _up
    page = reaper._SEERR_MEDIA_PAGE

    def fake_req(method, port, key, path, query="", timeout=30):
        if method == "GET" and path == "/api/v1/media":
            q = dict(_up.parse_qsl(query))
            skip = int(q.get("skip", 0))
            if skip == 0:
                rows = [{"id": i, "mediaType": "movie", "tmdbId": 900000 + i}
                        for i in range(page)]           # full first page
                return 200, {"results": rows, "pageInfo": {"results": page * 3}}
            return 503, "upstream error"                # second page fails
        if method == "DELETE":
            return 200, ""
        return 404, None

    monkeypatch.setattr(reaper, "_seerr_req", fake_req)
    monkeypatch.setattr(reaper, "_arr_client",
                        lambda slug: FakeArr(slug, movies=[], series=[]))

    deleted, failed = reaper.reconcile_seerr(execute=True)
    assert deleted == page and failed == 0             # first page still reconciled


def test_seerr_unreachable_tolerated(reaper, monkeypatch):
    monkeypatch.setattr(reaper, "_seerr_creds", lambda: ("42011", "seerrkey"))
    monkeypatch.setattr(reaper, "_seerr_req",
                        lambda *a, **k: (0, "connection refused"))
    deleted, failed = reaper.reconcile_seerr(execute=True)
    assert deleted == 0 and failed == 0     # tolerated, no abort


def test_seerr_dry_run_does_not_delete(reaper, monkeypatch):
    monkeypatch.setattr(reaper, "_seerr_creds", lambda: ("42011", "seerrkey"))
    media_rows = {"results": [{"id": 2, "mediaType": "movie", "tmdbId": 999}]}
    deletes = []

    def fake_req(method, port, key, path, query="", timeout=30):
        if method == "GET":
            return 200, media_rows
        deletes.append(path)
        return 200, ""

    monkeypatch.setattr(reaper, "_seerr_req", fake_req)
    monkeypatch.setattr(reaper, "_arr_client",
                        lambda slug: FakeArr(slug, movies=[], series=[]))

    deleted, failed = reaper.reconcile_seerr(execute=False)
    assert deleted == 0
    assert deletes == []                    # dry-run issues NO Seerr delete


# ===========================================================================
# 12. notify/kuma are guarded — a missing requests must not crash _notify
# ===========================================================================
def test_notify_guarded_against_missing_requests(reaper, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "lib.notify" or name.endswith("notify"):
            raise ImportError("No module named 'requests'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    # Must not raise.
    reaper._notify("hello", "info")


# ===========================================================================
# 13. exclude-file parser tolerates comments/blanks/whitespace + all 4 forms
# ===========================================================================
def test_exclude_parser_all_forms(reaper, tmpdir):
    p = Path(str(tmpdir)) / "ex.txt"
    p.write_text(
        "# header comment\n"
        "\n"
        "   tmdb:603  \n"
        "tvdb:81189\n"
        "plex:14823\n"
        "  The Matrix  \n"
        "# trailing comment\n",
        encoding="utf-8",
    )
    rules = reaper.load_exclusions(p)
    assert "tmdb:603" in rules
    assert "tvdb:81189" in rules
    assert "plex:14823" in rules
    assert "title:the matrix" in rules
    assert len(rules) == 4


# ===========================================================================
# 14. FIX (boundaries): addedAt<=0 (missing/unparseable Plex add-date) is NEVER
# a candidate — a metadata gap must not make something look ancient.
# ===========================================================================
def test_added_at_zero_is_not_a_candidate(reaper, tmpdir, monkeypatch):
    movie = {"ratingKey": "1", "title": "No Date Movie", "year": 1999,
             "addedAt": 0, "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 603, "tvdbId": None}})
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    assert fake.deletes == []          # addedAt<=0 excluded from candidacy


# ===========================================================================
# 15. FIX (security): a Seerr media row with a non-integer id is skipped — never
# reflected into a DELETE URL path, never crashes the reconcile.
# ===========================================================================
def test_seerr_noninteger_id_skipped(reaper, monkeypatch):
    monkeypatch.setattr(reaper, "_seerr_creds", lambda: ("42011", "seerrkey"))
    media_rows = {"results": [
        {"id": "../../etc", "mediaType": "movie", "tmdbId": 999},  # bad id -> skip
        {"id": 7, "mediaType": "movie", "tmdbId": 998},            # orphan -> delete
    ]}
    deletes = []

    def fake_req(method, port, key, path, query="", timeout=30):
        if method == "GET":
            return 200, media_rows
        deletes.append(path)
        return 200, ""

    monkeypatch.setattr(reaper, "_seerr_req", fake_req)
    monkeypatch.setattr(reaper, "_arr_client",
                        lambda slug: FakeArr(slug, movies=[], series=[]))

    deleted, failed = reaper.reconcile_seerr(execute=True)
    assert deleted == 1                       # only the valid orphan
    assert deletes == ["/api/v1/media/7"]     # non-integer id never hit a URL


# ===========================================================================
# 16. FIX (concurrency): the audit manifest filename carries a PID uniquifier so
# two same-second runs can't overwrite each other's pre-deletion record.
# ===========================================================================
def test_manifest_filename_has_pid_uniquifier(reaper, tmpdir, monkeypatch):
    import os
    movie = {"ratingKey": "1", "title": "Old Movie", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 603, "tvdbId": None}})
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    manifests = list(Path(str(tmpdir)).glob("qflix-reaper-*.json"))
    assert len(manifests) == 1
    assert ("-" + str(os.getpid()) + ".json") in manifests[0].name


# ===========================================================================
# 7. Durable file logging (observability — journal is restricted on the box)
# ===========================================================================
def test_file_logging_writes_durable_log(reaper, tmpdir, monkeypatch):
    monkeypatch.setenv("QFLIX_REAPER_LOG_DIR", str(tmpdir))
    reaper._setup_file_log()
    try:
        reaper.log("hello-line")
        reaper.warn("warn-line")
        logs = list(Path(str(tmpdir)).glob("reaper-*.log"))
        assert len(logs) == 1
        text = logs[0].read_text(encoding="utf-8")
        assert "[qflix-reaper] hello-line" in text
        assert "WARNING: warn-line" in text
    finally:
        if reaper._LOG_FH is not None:
            reaper._LOG_FH.close()


def test_file_logging_absent_dir_degrades_silently(reaper, tmpdir, monkeypatch):
    # An unwritable log dir must NOT break the reaper — file logging is best-effort.
    # Force mkdir to fail (OS-independent) and assert the reaper degrades cleanly.
    import pathlib
    monkeypatch.setenv("QFLIX_REAPER_LOG_DIR", str(tmpdir))
    monkeypatch.setattr(pathlib.Path, "mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no mkdir")))
    reaper._setup_file_log()
    assert reaper._LOG_FH is None
    reaper.log("still-works")   # must not raise


# ===========================================================================
# 8. Orphan grace window — un-resolvable items no longer red the run forever.
#    (spec: docs/superpowers/specs/2026-07-14-reaper-orphan-grace-design.md)
# ===========================================================================
import datetime as _dt


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _isolate_orphan_state(tmp_path, monkeypatch):
    """Every test gets a private orphan-state file so none touch the real
    ~/.opt/maint/reaper/. Harmless to tests that never enumerate orphans."""
    monkeypatch.setenv("QFLIX_REAPER_ORPHAN_STATE",
                       str(tmp_path / "orphan-state.json"))


# ---- _orphan_key: stable identity preferring external ids ------------------
def test_orphan_key_series_prefers_tvdb(reaper):
    it = {"kind": "series", "tvdbId": 424536, "tmdbId": 209867, "ratingKey": "6692"}
    assert reaper._orphan_key(it) == "tvdb:424536"


def test_orphan_key_movie_prefers_tmdb(reaper):
    it = {"kind": "movie", "tvdbId": None, "tmdbId": 603, "ratingKey": "1"}
    assert reaper._orphan_key(it) == "tmdb:603"


def test_orphan_key_falls_back_to_plex_rating_key(reaper):
    it = {"kind": "series", "tvdbId": None, "tmdbId": None, "ratingKey": "6692"}
    assert reaper._orphan_key(it) == "plex:6692"


# ---- reconcile_orphans: the grace clock -----------------------------------
_UTC = _dt.timezone.utc


def _seed_state(path, orphans):
    Path(path).write_text(json.dumps({"version": 1, "orphans": orphans}),
                          encoding="utf-8")


def _read_state(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cur(key, title="T", library="L"):
    return {"key": key, "title": title, "library": library}


def test_reconcile_new_orphan_is_fresh_and_persisted(reaper, tmp_path):
    sp = tmp_path / "st.json"
    now = _dt.datetime(2026, 7, 14, 5, 0, 0, tzinfo=_UTC)
    fresh, known, warn_due = reaper.reconcile_orphans(
        [_cur("tvdb:1")], now, grace_hours=24, remind_days=7, state_path=str(sp))
    assert [o["key"] for o in fresh] == ["tvdb:1"]
    assert known == [] and warn_due == []
    st = _read_state(sp)["orphans"]["tvdb:1"]
    assert st["first_seen"] == st["last_seen"] == "2026-07-14T05:00:00Z"


def test_reconcile_aged_orphan_is_known_not_fresh(reaper, tmp_path):
    sp = tmp_path / "st.json"
    now = _dt.datetime(2026, 7, 14, 5, 0, 0, tzinfo=_UTC)
    _seed_state(sp, {"tvdb:1": {"title": "T", "library": "L",
                                "first_seen": "2026-07-13T04:00:00Z",   # 25h ago
                                "last_seen": "2026-07-13T04:00:00Z"}})
    fresh, known, warn_due = reaper.reconcile_orphans(
        [_cur("tvdb:1")], now, grace_hours=24, remind_days=7, state_path=str(sp))
    assert fresh == []
    assert [o["key"] for o in known] == ["tvdb:1"]


def test_reconcile_resolved_orphan_dropped_from_state(reaper, tmp_path):
    sp = tmp_path / "st.json"
    now = _dt.datetime(2026, 7, 14, 5, 0, 0, tzinfo=_UTC)
    _seed_state(sp, {"tvdb:GONE": {"title": "T", "library": "L",
                                   "first_seen": "2026-07-01T00:00:00Z",
                                   "last_seen": "2026-07-13T00:00:00Z"}})
    reaper.reconcile_orphans([_cur("tvdb:NEW")], now,
                             grace_hours=24, remind_days=7, state_path=str(sp))
    keys = _read_state(sp)["orphans"].keys()
    assert "tvdb:GONE" not in keys and "tvdb:NEW" in keys


def test_reconcile_weekly_reminder_due(reaper, tmp_path):
    sp = tmp_path / "st.json"
    now = _dt.datetime(2026, 7, 14, 5, 0, 0, tzinfo=_UTC)
    _seed_state(sp, {"tvdb:1": {"title": "T", "library": "L",
                                "first_seen": "2026-06-01T00:00:00Z",
                                "last_seen": "2026-07-13T00:00:00Z",
                                "last_warned": "2026-07-06T00:00:00Z"}})  # 8d ago
    fresh, known, warn_due = reaper.reconcile_orphans(
        [_cur("tvdb:1")], now, grace_hours=24, remind_days=7, state_path=str(sp))
    assert [o["key"] for o in warn_due] == ["tvdb:1"]
    assert _read_state(sp)["orphans"]["tvdb:1"]["last_warned"] == "2026-07-14T05:00:00Z"


def test_reconcile_weekly_reminder_not_due(reaper, tmp_path):
    sp = tmp_path / "st.json"
    now = _dt.datetime(2026, 7, 14, 5, 0, 0, tzinfo=_UTC)
    _seed_state(sp, {"tvdb:1": {"title": "T", "library": "L",
                                "first_seen": "2026-06-01T00:00:00Z",
                                "last_seen": "2026-07-13T00:00:00Z",
                                "last_warned": "2026-07-13T00:00:00Z"}})  # 1d ago
    fresh, known, warn_due = reaper.reconcile_orphans(
        [_cur("tvdb:1")], now, grace_hours=24, remind_days=7, state_path=str(sp))
    assert warn_due == []
    assert _read_state(sp)["orphans"]["tvdb:1"]["last_warned"] == "2026-07-13T00:00:00Z"


def test_reconcile_corrupt_state_treats_all_as_fresh(reaper, tmp_path):
    sp = tmp_path / "st.json"
    Path(sp).write_text("{ this is not json", encoding="utf-8")
    now = _dt.datetime(2026, 7, 14, 5, 0, 0, tzinfo=_UTC)
    fresh, known, warn_due = reaper.reconcile_orphans(
        [_cur("tvdb:1")], now, grace_hours=24, remind_days=7, state_path=str(sp))
    assert [o["key"] for o in fresh] == ["tvdb:1"]     # fail TOWARD alerting
    assert known == []


# ---- classify_run: the red/green decision ---------------------------------
def _info(key="tvdb:1", title="Frieren", library="QFlix - Anime", age=30.0):
    return {"key": key, "title": title, "library": library,
            "first_seen": "2026-07-13T00:00:00Z", "age_hours": age}


def test_classify_operational_partial_is_error(reaper):
    rc, sev, note = reaper.classify_run(True, [], [], [])
    assert (rc, sev) == (reaper.EXIT_PARTIAL, "error")


def test_classify_fresh_orphan_is_error(reaper):
    o = _info(age=2.0)
    rc, sev, note = reaper.classify_run(False, [o], [], [])
    assert rc == reaper.EXIT_PARTIAL and sev == "error"
    assert "Frieren" in note


def test_classify_known_orphan_reminder_due_is_warning_green(reaper):
    o = _info(age=200.0)
    rc, sev, note = reaper.classify_run(False, [], [o], [o])
    assert rc == reaper.EXIT_OK and sev == "warning"
    assert "Frieren" in note


def test_classify_known_orphan_not_due_is_ok_green(reaper):
    o = _info(age=200.0)
    rc, sev, note = reaper.classify_run(False, [], [o], [])
    assert rc == reaper.EXIT_OK and sev == "ok"
    assert "Frieren" in note        # still surfaced in the note text


def test_classify_clean_run_is_ok(reaper):
    rc, sev, note = reaper.classify_run(False, [], [], [])
    assert (rc, sev, note) == (reaper.EXIT_OK, "ok", "")


def test_reconcile_no_emit_reminders_does_not_consume_warn_slot(reaper, tmp_path):
    # Dry-run observes orphans (updates the grace clock) but must NOT fire or
    # consume the weekly WARN — otherwise the later execute run finds it "not due"
    # and the reminder is silently swallowed.
    sp = tmp_path / "st.json"
    now = _dt.datetime(2026, 7, 14, 5, 0, 0, tzinfo=_UTC)
    _seed_state(sp, {"tvdb:1": {"title": "T", "library": "L",
                                "first_seen": "2026-06-01T00:00:00Z",
                                "last_seen": "2026-07-13T00:00:00Z",
                                "last_warned": "2026-07-06T00:00:00Z"}})  # 8d -> due
    fresh, known, warn_due = reaper.reconcile_orphans(
        [_cur("tvdb:1")], now, grace_hours=24, remind_days=7,
        state_path=str(sp), emit_reminders=False)
    assert [o["key"] for o in known] == ["tvdb:1"]
    assert warn_due == []                                     # not emitted
    # last_warned untouched, so the execute run can still fire it.
    assert _read_state(sp)["orphans"]["tvdb:1"]["last_warned"] == "2026-07-06T00:00:00Z"
    # ...but last_seen IS advanced (dry-run still observed it).
    assert _read_state(sp)["orphans"]["tvdb:1"]["last_seen"] == "2026-07-14T05:00:00Z"


# ---- run()-level integration: grace changes the run color -----------------
import os as _os


def _capture_notify(reaper, monkeypatch):
    calls = []
    monkeypatch.setattr(reaper, "_notify",
                        lambda msg, level="info": calls.append((level, msg)))
    monkeypatch.setattr(reaper, "_push_kuma", lambda *a, **k: None)
    monkeypatch.setattr(reaper, "reconcile_seerr", lambda execute: (0, 0))
    return calls


def _seed_env_state(orphans):
    _seed_state(_os.environ["QFLIX_REAPER_ORPHAN_STATE"], orphans)


def test_known_orphan_only_execute_is_green_no_deletes(reaper, tmpdir, monkeypatch):
    # An orphan that has persisted past the grace window must NOT red the run.
    aged = reaper._fmt_stamp(_dt.datetime.now(_UTC) - _dt.timedelta(hours=25))
    _seed_env_state({"tmdb:999": {"title": "Ghost", "library": "QFlix - Movies",
                                  "first_seen": aged, "last_seen": aged}})
    movie = {"ratingKey": "1", "title": "Ghost", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 999, "tvdbId": None}})   # 999 not in Radarr -> orphan
    calls = _capture_notify(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))

    assert rc == reaper.EXIT_OK                     # GREEN, not the old EXIT_PARTIAL
    assert fake.deletes == []                       # still never deleted
    assert not any(lvl == "error" for lvl, _ in calls)   # no ERROR page


def test_known_orphan_reminder_due_emits_warning(reaper, tmpdir, monkeypatch):
    # last_warned absent -> weekly reminder due -> WARN-level notify, run stays green.
    aged = reaper._fmt_stamp(_dt.datetime.now(_UTC) - _dt.timedelta(hours=48))
    _seed_env_state({"tmdb:999": {"title": "Ghost", "library": "QFlix - Movies",
                                  "first_seen": aged, "last_seen": aged}})
    movie = {"ratingKey": "1", "title": "Ghost", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 999, "tvdbId": None}})
    calls = _capture_notify(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))

    assert rc == reaper.EXIT_OK
    assert any(lvl == "warning" for lvl, _ in calls)      # weekly reminder fired
    assert not any(lvl == "error" for lvl, _ in calls)


def test_fresh_orphan_still_reds_execute(reaper, tmpdir, monkeypatch):
    # Empty state -> orphan is brand new -> still ERROR/exit 1 (operator learns).
    movie = {"ratingKey": "1", "title": "NewGhost", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 999, "tvdbId": None}})
    calls = _capture_notify(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))

    assert rc == reaper.EXIT_PARTIAL
    assert any(lvl == "error" for lvl, _ in calls)


def test_cap_abort_does_not_consume_weekly_reminder(reaper, tmpdir, monkeypatch):
    # An execute run that aborts at a cap trip must NOT stamp last_warned — else
    # the weekly reminder is swallowed and never actually sent. The warn slot is
    # only consumed at the guaranteed emit point (the summary).
    aged = reaper._fmt_stamp(_dt.datetime.now(_UTC) - _dt.timedelta(hours=48))
    _seed_env_state({"tvdb:1": {"title": "Orphan", "library": "QFlix - Anime",
                                "first_seen": aged, "last_seen": aged}})  # no last_warned -> due
    # A resolvable movie that is 100% of a 1-item library trips max_pct=50 -> EXIT_CAP.
    movie = {"ratingKey": "5", "title": "Cap Movie", "year": 2001,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    orphan = {"ratingKey": "9", "title": "Orphan", "year": 2010,
              "addedAt": _old(reaper), "sizeGB": 30.0}
    _install_plex(reaper, monkeypatch,
                  {"QFlix - Movies": [movie], "QFlix - Anime": [orphan]},
                  {"5": {"tmdbId": 603, "tvdbId": None},
                   "9": {"tmdbId": None, "tvdbId": 1}})   # tvdb 1 not in sonarr2 -> orphan
    _capture_notify(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, max_pct=50, manifest_dir=str(tmpdir)))

    assert rc == reaper.EXIT_CAP
    st = _read_state(_os.environ["QFLIX_REAPER_ORPHAN_STATE"])["orphans"]["tvdb:1"]
    assert "last_warned" not in st          # reminder NOT consumed on abort


# ---------------------------------------------------------------------------
# A SLOW DELETE IS NOT A FAILED DELETE (2026-08-20)
#
# The reaper graded do_delete_movie purely on the DELETE's own status code. On
# 2026-08-20 a 23-movie remux re-grab left Radarr main running 17 concurrent
# downloads with 23 queued MoviesSearch commands; the reaper's delete of
# 'Greyhound' (arrId=407) took 30s, answered non-2xx, and was logged
# DELETE FAILED -- while GET /movie/407 returned Not Found and the directory
# was already gone from disk. The run exited 1, the unit went to systemd
# failed, and Kuma #97 went red for work that had completed. Six clean runs
# preceded it, so it read as a real new fault.
#
# These pin the re-read contract: 404 on re-read is proof, everything else
# stays a failure, because "I could not confirm" must never be optimistic.
# ---------------------------------------------------------------------------
class _DelClient:
    """Minimal ArrClient stand-in: one canned delete status, one canned get."""

    def __init__(self, delete_status, get_status=None, get_raises=False):
        self._delete_status = delete_status
        self._get_status = get_status
        self._get_raises = get_raises
        self.deleted = []
        self.gets = []

    def delete(self, path, query=""):
        self.deleted.append((path, query))
        return self._delete_status, None

    def get(self, path, query="", timeout=None):
        self.gets.append(path)
        if self._get_raises:
            raise OSError("connection reset")
        return self._get_status, None


def test_2xx_delete_is_trusted_without_a_re_read(reaper):
    c = _DelClient(200)
    assert reaper.do_delete_movie(c, 407) is True
    assert c.gets == [], "a successful delete must not cost an extra GET"


def test_non_2xx_delete_that_actually_landed_counts_as_deleted(reaper):
    """The Greyhound case: HTTP said no, the server had already done it."""
    c = _DelClient(500, get_status=404)
    assert reaper.do_delete_movie(c, 407) is True
    assert c.gets == ["/movie/407"]


def test_non_2xx_delete_with_the_record_still_present_is_a_failure(reaper):
    c = _DelClient(500, get_status=200)
    assert reaper.do_delete_movie(c, 407) is False


def test_non_2xx_delete_is_a_failure_when_the_re_read_itself_fails(reaper):
    """Unconfirmable is reported, never assumed successful."""
    c = _DelClient(500, get_raises=True)
    assert reaper.do_delete_movie(c, 407) is False


def test_series_delete_has_the_same_re_read_contract(reaper):
    assert reaper.do_delete_series(_DelClient(200), 12) is True
    assert reaper.do_delete_series(_DelClient(504, get_status=404), 12) is True
    assert reaper.do_delete_series(_DelClient(504, get_status=200), 12) is False
    assert reaper.do_delete_series(_DelClient(504, get_raises=True), 12) is False


def test_delete_still_asks_for_files_and_no_import_exclusion(reaper):
    """The re-read must not have changed what the DELETE requests."""
    c = _DelClient(200)
    reaper.do_delete_movie(c, 407)
    assert c.deleted == [("/movie/407",
                          "deleteFiles=true&addImportExclusion=false")]
    s = _DelClient(200)
    reaper.do_delete_series(s, 12)
    assert s.deleted == [("/series/12",
                          "deleteFiles=true&addImportListExclusion=false")]
