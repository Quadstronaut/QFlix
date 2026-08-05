"""Tests for scripts/mcp/arr_library_peek.py — coarse content-presence peek.

Privacy: this module answers "do we have it", never "did anyone watch it".
FakeSonarr/FakeRadarr below mirror ArrClient's ACTUAL surface (get/post/put/
delete only — verified against scripts/mcp/lib/arr_client.py), not a richer
API the brief might have imagined.
"""
import importlib.util, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

def _load():
    path = REPO / "scripts" / "mcp" / "arr_library_peek.py"
    spec = importlib.util.spec_from_file_location("arr_library_peek", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["arr_library_peek"] = mod
    spec.loader.exec_module(mod)
    return mod

class FakeSonarr:
    """Mirrors the REAL ArrClient, verified against scripts/mcp/lib/arr_client.py:
    `get()` returns an (http_code, payload) TUPLE, and paths are
    version-relative because _url() already prepends /api/{version}.
    A fake that returns a bare list would let a broken production path pass."""
    def get(self, path, **kw):
        assert path == "/series", "path must be version-relative"
        return (200, [{"title": "Show A", "statistics": {"episodeFileCount": 12,
                                                         "totalEpisodeCount": 30}},
                      {"title": "Show B", "statistics": {"episodeFileCount": 10,
                                                         "totalEpisodeCount": 10}}])

class FakeRadarr:
    def get(self, path, **kw):
        assert path == "/movie", "path must be version-relative"
        return (200, [{"title": "Movie A", "hasFile": True},
                      {"title": "Movie B", "hasFile": False}])

def test_series_peek_reports_have_over_total():
    m = _load()
    out = m.peek("sonarr", client=FakeSonarr())
    a = [t for t in out["titles"] if t["title"] == "Show A"][0]
    assert (a["have"], a["total"], a["complete"]) == (12, 30, False)

def test_a_fully_present_series_is_marked_complete():
    m = _load()
    out = m.peek("sonarr", client=FakeSonarr())
    b = [t for t in out["titles"] if t["title"] == "Show B"][0]
    assert b["complete"] is True

def test_movie_peek_is_present_or_not():
    m = _load()
    out = m.peek("radarr", client=FakeRadarr())
    assert {t["title"]: t["complete"] for t in out["titles"]} == {
        "Movie A": True, "Movie B": False}
    for t in out["titles"]:
        assert t["total"] == 1

def test_peek_reports_no_consumption_data_whatsoever():
    """Privacy: content presence only. Nothing about who watched anything."""
    m = _load()
    out = m.peek("sonarr", client=FakeSonarr())
    banned = ("watch", "view", "session", "user", "played", "seen")
    blob = repr(out).lower()
    assert not any(b in blob for b in banned)

def test_a_dead_arr_degrades_that_slug_without_raising():
    m = _load()
    class Boom:
        def get(self, path, **kw): raise RuntimeError("connection refused")
    out = m.peek("sonarr", client=Boom())
    assert out["ok"] is False
    assert "connection refused" in out["error"]
    assert out["titles"] == []

def test_a_non_200_is_an_error_not_an_empty_library():
    """An arr answering 500 must not read as 'we own nothing' — that would
    render an empty stARR page and look like catastrophic data loss."""
    m = _load()
    class ServerError:
        def get(self, path, **kw): return (500, "upstream boom")
    out = m.peek("sonarr", client=ServerError())
    assert out["ok"] is False
    assert "500" in out["error"]
    assert out["titles"] == []

def test_a_200_with_a_non_list_body_is_an_error_not_an_empty_library():
    """ArrClient._req returns payload=None on an empty 200 body, so this is a
    real path, not a hypothetical. It must not read as 'we own nothing'."""
    m = _load()
    for payload in (None, {"message": "no content"}):
        class OddBody:
            def __init__(self, p): self.p = p
            def get(self, path, **kw): return (200, self.p)
        out = m.peek("sonarr", client=OddBody(payload))
        assert out["ok"] is False, payload
        assert out["titles"] == [], payload


def test_a_title_entry_carries_exactly_four_keys_and_no_others():
    """The vocabulary test is a tripwire for KNOWN bad words; this is the
    actual guarantee. A future edit that passed the raw *arr record through
    would add dozens of keys and fail here even if none of them happened to
    contain 'watch' or 'view'.

    BOTH branches are checked: peek() builds a separate dict literal for
    series and for movies, so they can drift independently — a reviewer
    confirmed a movie-branch-only regression went undetected when this test
    covered series alone.
    """
    m = _load()
    for slug, fake in (("sonarr", FakeSonarr()), ("radarr", FakeRadarr())):
        out = m.peek(slug, client=fake)
        assert out["titles"], "%s fixture must produce at least one title" % slug
        for entry in out["titles"]:
            assert set(entry) == {"title", "have", "total", "complete"}, \
                (slug, sorted(entry))
