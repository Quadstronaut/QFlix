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
        return 404, None

    def delete(self, path, query="", timeout=None):
        self.deletes.append((path, query))
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
def test_max_items_cap_trips_exit_2_no_mutation(reaper, tmpdir, monkeypatch):
    items = [{"ratingKey": str(i), "title": "M%d" % i, "year": 2000,
              "addedAt": _old(reaper), "sizeGB": 1.0} for i in range(5)]
    ids = {str(i): {"tmdbId": 600 + i, "tvdbId": None} for i in range(5)}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": items}, ids)
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[{"id": 100 + i, "tmdbId": 600 + i, "hasFile": True}
                                     for i in range(5)])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, max_items=3, max_pct=100,
                          manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_CAP
    assert fake.deletes == []                       # aborted before any delete
    assert list(Path(str(tmpdir)).glob("qflix-reaper-*.json")) == []  # no manifest


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
