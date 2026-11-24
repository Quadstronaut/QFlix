"""QFLX-126: four fail-open paths in the armed nightly reaper, closed.

Every test here drives the real `reaper.run()` / `reaper.reconcile_seerr()`
against the same fakes the rest of the suite uses, and each one FAILS on the
pre-fix code:

  1. P-4 removed an ended series' record while it still had monitored,
     file-less episodes or a download in flight -> a member's live request was
     dropped and the run exited 0.
  2. An exclude file that existed but could not be decoded meant "no
     exclusions" and the run carried on.
  3. A movie whose Radarr record could not be read (or that Radarr says has no
     file) was graded on the Plex clock alone.
  4. Seerr reconciliation had no ceiling and read "Radarr holds it, monitored,
     no file yet" as gone.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.test_qflix_reaper import (  # noqa: F401  (reaper is a fixture)
    FakeArr, _args, _arr_iso, _install_plex, _old, _silence_side_effects, reaper,
)

DELETE_SERIES_Q = "deleteFiles=true&addImportListExclusion=false"


@pytest.fixture(autouse=True)
def _isolate_orphan_state(tmp_path, monkeypatch):
    monkeypatch.setenv("QFLIX_REAPER_ORPHAN_STATE", str(tmp_path / "orphans.json"))
    monkeypatch.delenv("QFLIX_REAPER_SEERR_MAX_ROWS", raising=False)


# ---------------------------------------------------------------------------
# 1. P-4: wanted or in-flight episodes keep the series record
# ---------------------------------------------------------------------------
def _ended_show(reaper, monkeypatch, *, episodes, episodefiles, queue=None, leaves=None):
    show = {"ratingKey": "21", "title": "Ended Show", "year": 1990,
            "addedAt": 1600000000, "sizeGB": 0.0}
    _install_plex(reaper, monkeypatch, {"QFlix - TV": [show]},
                  {"21": {"tmdbId": None, "tvdbId": 11111}}, leaves_by_rk=leaves or {})
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("sonarr",
                   series=[{"id": 51, "tvdbId": 11111, "ended": True, "tags": []}],
                   tags=[{"id": 13, "label": "permanent"}],
                   episodefiles={51: episodefiles}, episodes={51: episodes},
                   queue={51: queue or []})
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)
    return fake


def test_p4_keeps_an_ended_series_that_still_has_wanted_episodes(reaper, tmpdir, monkeypatch):
    """The audit's exact scenario: the season-1 file is past policy, a member
    has requested season 2 and nothing has imported yet."""
    old = _old(reaper)
    path = "/home/quadstronaut/media/tv/ended/S01E01.mkv"
    fake = _ended_show(
        reaper, monkeypatch,
        leaves={"21": [{"addedAt": old, "seasonNumber": 1, "path": path,
                        "sizeBytes": 2 * 1024 ** 3}]},
        episodefiles=[{"id": 900, "seasonNumber": 1, "path": path, "dateAdded": _arr_iso(old)}],
        episodes=[
            {"id": 1, "episodeFileId": 900, "seasonNumber": 1, "monitored": True, "hasFile": True},
            {"id": 2, "episodeFileId": 0, "seasonNumber": 2, "monitored": True, "hasFile": False},
            {"id": 3, "episodeFileId": 0, "seasonNumber": 2, "monitored": True, "hasFile": False},
        ])

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))

    assert rc == reaper.EXIT_OK
    assert ("/episodefile/900", "") in fake.deletes, "the expired file is still reaped"
    assert ("/series/51", DELETE_SERIES_Q) not in fake.deletes, \
        "P-4 removed a record with 2 monitored, file-less episodes"
    assert len(fake.series) == 1
    assert [e["monitored"] for e in fake.episodes[51]] == [False, True, True], \
        "only the reaped file's own episode is unmonitored"


def test_p4_keeps_an_ended_series_with_a_download_in_flight(reaper, tmpdir, monkeypatch):
    fake = _ended_show(reaper, monkeypatch, episodefiles=[], episodes=[],
                       queue=[{"id": 7, "seriesId": 51, "status": "downloading"}])
    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    assert ("/series/51", DELETE_SERIES_Q) not in fake.deletes
    assert ("/queue/details", "seriesId=51") in fake.gets, "the queue was never read"


def test_p4_fails_closed_when_the_queue_cannot_be_read(reaper, tmpdir, monkeypatch):
    fake = _ended_show(reaper, monkeypatch, episodefiles=[], episodes=[])
    real_get = fake.get

    def flaky(path, query="", timeout=None):
        if path == "/queue/details":
            return 0, {"error": "timed out"}
        return real_get(path, query, timeout)
    fake.get = flaky

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert ("/series/51", DELETE_SERIES_Q) not in fake.deletes
    assert rc == reaper.EXIT_PARTIAL, "could-not-look must be reported, not read as nothing-wanted"


def test_p4_fails_closed_when_the_episode_list_cannot_be_read(reaper):
    class Boom:
        def get(self, path, query="", timeout=None):
            return 500, None
    why, read_failed = reaper.series_removal_blocker(Boom(), 51)
    assert why and read_failed


def test_p4_still_removes_a_record_nobody_wants(reaper, tmpdir, monkeypatch):
    """The guard must not turn P-4 off: unmonitored leftovers do not block."""
    fake = _ended_show(reaper, monkeypatch, episodefiles=[], episodes=[
        {"id": 1, "episodeFileId": 0, "seasonNumber": 1, "monitored": False, "hasFile": False}])
    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    assert ("/series/51", DELETE_SERIES_Q) in fake.deletes
    assert ("/episode", "seriesId=51") in fake.gets and ("/queue/details", "seriesId=51") in fake.gets


def test_p4_dry_run_previews_the_same_decision_and_mutates_nothing(reaper, tmpdir, monkeypatch):
    fake = _ended_show(reaper, monkeypatch, episodefiles=[], episodes=[
        {"id": 2, "episodeFileId": 0, "seasonNumber": 2, "monitored": True, "hasFile": False}])
    rc = reaper.run(_args(reaper, execute=False, manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    assert fake.deletes == [] and fake.puts == []


# ---------------------------------------------------------------------------
# 2. Exclude file: unreadable is fatal, missing is still allowed
# ---------------------------------------------------------------------------
def test_undecodable_exclude_file_aborts_instead_of_dropping_every_rule(reaper, tmpdir, monkeypatch):
    exclude = Path(str(tmpdir)) / "reaper.exclude"
    # A valid id rule plus one title saved in Latin-1 (0xE9 is not valid UTF-8).
    exclude.write_bytes(b"tmdb:603\nAm\xe9lie\n")
    movie = {"ratingKey": "1", "title": "Protected Movie", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 603, "tvdbId": None}})
    _silence_side_effects(reaper, monkeypatch)
    notes, pushes = [], []
    monkeypatch.setattr(reaper, "_notify", lambda *a, **k: notes.append((a, k)))
    monkeypatch.setattr(reaper, "_push_kuma", lambda *a, **k: pushes.append(a))
    fake = FakeArr("radarr", movies=[{"id": 42, "tmdbId": 603, "hasFile": True}])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)

    rc = reaper.run(_args(reaper, execute=True, exclude_file=str(exclude),
                          manifest_dir=str(tmpdir)))

    assert rc == reaper.EXIT_FATAL
    assert fake.deletes == [], "the protected title was deleted on an unreadable exclude file"
    assert notes and pushes and pushes[0][0] == "down"
    with pytest.raises(reaper.ExcludeFileUnreadable):
        reaper.load_exclusions(exclude)


def test_a_missing_exclude_file_is_still_only_a_warning(reaper):
    assert reaper.load_exclusions(Path("/definitely/not/here.exclude")) == set()


# ---------------------------------------------------------------------------
# 3. Movies: never graded on the Plex clock alone when Radarr cannot vouch
# ---------------------------------------------------------------------------
def _one_old_movie(reaper, monkeypatch, movie_row):
    movie = {"ratingKey": "1", "title": "Old Movie", "year": 1999,
             "addedAt": _old(reaper), "sizeGB": 5.0}
    _install_plex(reaper, monkeypatch, {"QFlix - Movies": [movie]},
                  {"1": {"tmdbId": 603, "tvdbId": None}})
    _silence_side_effects(reaper, monkeypatch)
    fake = FakeArr("radarr", movies=[movie_row])
    monkeypatch.setattr(reaper, "_arr_client", lambda slug: fake)
    return fake


def test_movie_is_withheld_when_its_radarr_record_cannot_be_read(reaper, tmpdir, monkeypatch):
    fake = _one_old_movie(reaper, monkeypatch, {"id": 42, "tmdbId": 603, "hasFile": True})
    real_get = fake.get

    def stall(path, query="", timeout=None):
        if path == "/movie/42":
            return 0, {"error": "timed out"}       # ArrClient's transport-error shape
        return real_get(path, query, timeout)
    fake.get = stall

    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert fake.deletes == [], "deleted on the uncorroborated Plex clock after a failed read"
    assert rc == reaper.EXIT_PARTIAL


def test_movie_is_withheld_when_radarr_says_it_has_no_file(reaper, tmpdir, monkeypatch):
    fake = _one_old_movie(reaper, monkeypatch, {"id": 42, "tmdbId": 603, "hasFile": False})
    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert fake.deletes == []
    assert rc == reaper.EXIT_OK, "a steady disagreement is logged, not a nightly red"


def test_corroborated_old_movie_is_still_deleted(reaper, tmpdir, monkeypatch):
    old = _old(reaper)
    fake = _one_old_movie(reaper, monkeypatch, {
        "id": 42, "tmdbId": 603, "hasFile": True, "movieFile": {"dateAdded": _arr_iso(old)}})
    rc = reaper.run(_args(reaper, execute=True, manifest_dir=str(tmpdir)))
    assert rc == reaper.EXIT_OK
    assert [p for p, _ in fake.deletes] == ["/movie/42"]


# ---------------------------------------------------------------------------
# 4. Seerr reconciliation: a ceiling, and "still searching" is not "gone"
# ---------------------------------------------------------------------------
def _seerr(reaper, monkeypatch, rows, movies):
    monkeypatch.setattr(reaper, "_seerr_creds", lambda: ("42011", "seerrkey"))
    deletes = []

    def fake_req(method, port, key, path, query="", timeout=30, body=None):
        if method == "GET" and path == "/api/v1/media":
            return 200, {"results": rows}
        if method == "DELETE":
            deletes.append(path)
            return 200, ""
        return 404, None
    monkeypatch.setattr(reaper, "_seerr_req", fake_req)
    monkeypatch.setattr(reaper, "_arr_client",
                        lambda slug: FakeArr(slug, movies=movies if slug == "radarr" else []))
    return deletes


def _degraded_radarr(n):
    """Radarr answers 200 with every movie present, but hasFile=false on all of
    them (and unmonitored, so the 'still searching' rule does not mask it)."""
    rows = [{"id": i, "mediaType": "movie", "tmdbId": 1000 + i, "status": 5} for i in range(n)]
    movies = [{"id": i, "tmdbId": 1000 + i, "hasFile": False, "monitored": False}
              for i in range(n)]
    return rows, movies


@pytest.mark.parametrize("execute", [True, False])
def test_seerr_reconcile_refuses_a_mass_wipe(reaper, monkeypatch, execute):
    rows, movies = _degraded_radarr(39)          # the number measured on the box
    deletes = _seerr(reaper, monkeypatch, rows, movies)
    deleted, failed = reaper.reconcile_seerr(execute=execute)
    assert deletes == [], "a degraded Radarr index wiped Seerr rows"
    assert (deleted, failed) == (0, 1), "the refusal must mark the run partial"


def test_seerr_reconcile_at_the_ceiling_still_runs(reaper, monkeypatch):
    rows, movies = _degraded_radarr(reaper.SEERR_RECONCILE_MAX_ROWS_DEFAULT)
    deletes = _seerr(reaper, monkeypatch, rows, movies)
    deleted, failed = reaper.reconcile_seerr(execute=True)
    assert (deleted, failed) == (len(rows), 0) and len(deletes) == len(rows)


def test_seerr_ceiling_can_be_raised_for_one_run_and_bad_values_do_not_disable_it(
        reaper, monkeypatch):
    rows, movies = _degraded_radarr(39)
    deletes = _seerr(reaper, monkeypatch, rows, movies)
    for bad in ("", "lots", "-1"):
        monkeypatch.setenv("QFLIX_REAPER_SEERR_MAX_ROWS", bad)
        assert reaper.seerr_reconcile_max_rows() == reaper.SEERR_RECONCILE_MAX_ROWS_DEFAULT
    monkeypatch.setenv("QFLIX_REAPER_SEERR_MAX_ROWS", "100")
    assert reaper.reconcile_seerr(execute=True) == (39, 0)
    assert len(deletes) == 39


def test_seerr_keeps_a_movie_radarr_is_still_searching_for(reaper, monkeypatch):
    rows = [
        {"id": 1, "mediaType": "movie", "tmdbId": 11, "status": 5},   # has a file      -> keep
        {"id": 2, "mediaType": "movie", "tmdbId": 22, "status": 5},   # re-grab pending -> keep
        {"id": 3, "mediaType": "movie", "tmdbId": 33, "status": 5},   # unmonitored, no file -> clear
        {"id": 4, "mediaType": "movie", "tmdbId": 44, "status": 5},   # not in Radarr   -> clear
    ]
    movies = [
        {"id": 1, "tmdbId": 11, "hasFile": True, "monitored": True},
        {"id": 2, "tmdbId": 22, "hasFile": False, "monitored": True},
        {"id": 3, "tmdbId": 33, "hasFile": False, "monitored": False},
    ]
    deletes = _seerr(reaper, monkeypatch, rows, movies)
    deleted, failed = reaper.reconcile_seerr(execute=True)
    assert set(deletes) == {"/api/v1/media/3", "/api/v1/media/4"}
    assert (deleted, failed) == (2, 0)
