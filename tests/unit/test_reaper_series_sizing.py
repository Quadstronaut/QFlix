"""A series must not report 0.0 GB just because Plex hides its files.

Found while answering "is the 60-day retention window still right" on
2026-07-31. Every TV series in the reaper's plan printed `0.0 GB`:

    'The Simpsons' (1989) [series] 0.0 GB  <QFlix - TV>
    'NYPD Blue'    (1993) [series] 0.0 GB  <QFlix - TV>

`/library/sections/<k>/all` returns SHOWS, and a show object carries no
Media/Part -- those live on the episodes. So the size sum was structurally
always zero for series, and TV Shows is 1.3T of a 2.3T library.

The consequence is not cosmetic. "N GB reclaimable" is the number an operator
uses to decide a retention change, and it was wrong by roughly the whole TV
library: measured at a 30-day threshold, the tool reported 317 GB and the true
on-disk figure was 706 GB. Anyone sizing a deletion from that output would have
deleted more than twice what they agreed to.

Sizing is reporting-only -- it does not change WHICH items are selected (that is
purely addedAt vs threshold) -- so correcting it cannot widen a deletion. It can
only stop understating one.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REAPER = REPO / "scripts" / "maint" / "qflix-reaper.py"


def _load():
    sys.path.insert(0, str(REPO / "scripts" / "maint"))
    spec = importlib.util.spec_from_file_location("qflix_reaper_sizing", REAPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def m():
    return _load()


def _part(size):
    return {"Media": [{"Part": [{"size": size}]}]}


# --- the primitive -------------------------------------------------------

def test_sum_media_parts_adds_every_part(m):
    meta = {"Media": [{"Part": [{"size": 100}, {"size": 200}]},
                      {"Part": [{"size": 300}]}]}
    assert m._sum_media_parts(meta) == 600


def test_sum_media_parts_survives_missing_and_garbage(m):
    assert m._sum_media_parts({}) == 0
    assert m._sum_media_parts({"Media": None}) == 0
    assert m._sum_media_parts({"Media": [{"Part": None}]}) == 0
    assert m._sum_media_parts({"Media": [{"Part": [{"size": "x"}, {"size": 5}]}]}) == 5


# --- the fix -------------------------------------------------------------

def test_a_show_is_sized_from_its_episodes(m, monkeypatch):
    """THE REGRESSION. The show object has no Media; the leaves do."""
    import json as _json

    def fake_get(port, token, path, query="", timeout=30):
        assert "allLeaves" in path, path
        return 200, _json.dumps({"MediaContainer": {"Metadata": [
            _part(1_000_000_000), _part(2_000_000_000), _part(500_000_000)]}})

    monkeypatch.setattr(m, "_plex_get", fake_get)
    total, err = m.series_size_bytes("32400", "tok", "1234")
    assert err is None
    assert total == 3_500_000_000


def test_plex_items_fills_in_the_series_size(m, monkeypatch):
    """End to end through plex_items: a show with no Media must come back with
    a non-zero sizeGB, and a movie must keep its direct sum."""
    import json as _json

    section = {"MediaContainer": {"Metadata": [
        {"ratingKey": 1, "title": "A Movie", "year": 2020, "addedAt": 100,
         "type": "movie", **_part(4 * 1024 ** 3)},
        {"ratingKey": 2, "title": "A Show", "year": 2019, "addedAt": 200,
         "type": "show"},                      # no Media -- the whole point
    ]}}
    leaves = {"MediaContainer": {"Metadata": [
        _part(6 * 1024 ** 3), _part(4 * 1024 ** 3)]}}

    def fake_get(port, token, path, query="", timeout=30):
        return (200, _json.dumps(leaves)) if "allLeaves" in path \
            else (200, _json.dumps(section))

    monkeypatch.setattr(m, "_plex_get", fake_get)
    items, err = m.plex_items("32400", "tok", "3")
    assert err is None
    by_title = {i["title"]: i for i in items}
    assert by_title["A Movie"]["sizeGB"] == 4.0, "movie sizing regressed"
    assert by_title["A Show"]["sizeGB"] == 10.0, (
        "the show still reports %s GB -- series are invisible in the plan again"
        % by_title["A Show"]["sizeGB"]
    )


# --- and it must fail loudly, not silently back to 0 ---------------------

def test_an_unsizeable_series_warns_rather_than_reporting_a_bare_zero(m, monkeypatch, capsys):
    """A silent 0 is indistinguishable from a genuinely empty series, and that
    ambiguity is exactly what let this defect survive."""
    import json as _json

    section = {"MediaContainer": {"Metadata": [
        {"ratingKey": 9, "title": "Unreachable Show", "year": 2019,
         "addedAt": 200, "type": "show"}]}}

    def fake_get(port, token, path, query="", timeout=30):
        return (500, "") if "allLeaves" in path else (200, _json.dumps(section))

    monkeypatch.setattr(m, "_plex_get", fake_get)
    items, err = m.plex_items("32400", "tok", "3")
    assert err is None                     # one bad series must not fail the run
    assert items[0]["sizeGB"] == 0.0
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "Unreachable Show" in combined and "could not size" in combined, (
        "an unsizeable series went by silently: " + repr(combined[:200])
    )


def test_movies_do_not_trigger_a_leaf_lookup(m, monkeypatch):
    """Guard against an extra HTTP round-trip per movie -- the candidate set is
    mostly movies and this runs against a live Plex."""
    import json as _json
    calls = []

    section = {"MediaContainer": {"Metadata": [
        {"ratingKey": 1, "title": "M", "year": 2020, "addedAt": 1,
         "type": "movie", **_part(1024 ** 3)}]}}

    def fake_get(port, token, path, query="", timeout=30):
        calls.append(path)
        return 200, _json.dumps(section)

    monkeypatch.setattr(m, "_plex_get", fake_get)
    m.plex_items("32400", "tok", "3")
    assert not any("allLeaves" in c for c in calls), \
        "a sized movie triggered a needless per-item request"
