"""Tests for scripts/mcp/arr_disk_usage.py — bytes on disk managed by one *arr.

FakeSonarr/FakeRadarr below mirror ArrClient's ACTUAL surface (verified
against scripts/mcp/lib/arr_client.py, same correction Task 6 landed for
arr_library_peek.py): `get()` returns an (http_code, payload) TUPLE, and
paths are version-relative because _url() already prepends /api/{version}.
A fake that returned a bare list would let a broken production path pass —
that is exactly the defect Task 6 found.

PRIVACY: usage() reports bytes and a count only — no titles, no member
identity. See Global Constraints in the task-7 brief and the three prior
member-data leaks in this plan.
"""
import importlib.util, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

def _load():
    path = REPO / "scripts" / "mcp" / "arr_disk_usage.py"
    spec = importlib.util.spec_from_file_location("arr_disk_usage", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["arr_disk_usage"] = mod
    spec.loader.exec_module(mod)
    return mod

class FakeSonarr:
    def get(self, path, **kw):
        assert path == "/series", "path must be version-relative"
        return (200, [{"statistics": {"sizeOnDisk": 1024 ** 3}},
                      {"statistics": {"sizeOnDisk": 2 * 1024 ** 3}}])

class FakeRadarr:
    def get(self, path, **kw):
        assert path == "/movie", "path must be version-relative"
        return (200, [{"sizeOnDisk": 5 * 1024 ** 3}, {"sizeOnDisk": 0}])

def test_series_usage_sums_size_on_disk():
    m = _load()
    out = m.usage("sonarr", client=FakeSonarr())
    assert out["bytes"] == 3 * 1024 ** 3
    assert out["title_count"] == 2

def test_movie_usage_sums_size_on_disk():
    m = _load()
    out = m.usage("radarr", client=FakeRadarr())
    assert out["bytes"] == 5 * 1024 ** 3
    assert out["title_count"] == 2

def test_human_is_a_short_string_a_phone_row_can_hold():
    m = _load()
    out = m.usage("sonarr", client=FakeSonarr())
    assert out["human"] == "3.0 GB"

def test_zero_bytes_is_reported_not_hidden():
    m = _load()
    class Empty:
        def get(self, path, **kw): return (200, [])
    out = m.usage("sonarr", client=Empty())
    assert out["ok"] is True and out["bytes"] == 0 and out["human"] == "0.0 B"

def test_a_dead_arr_degrades_without_raising():
    m = _load()
    class Boom:
        def get(self, path, **kw): raise RuntimeError("timed out")
    out = m.usage("sonarr", client=Boom())
    assert out["ok"] is False and "timed out" in out["error"]

def test_a_non_200_is_an_error_not_zero_bytes():
    """An arr answering 500 must not read as '0 bytes managed' — on the phone
    that reads as catastrophic data loss, not as a transport blip."""
    m = _load()
    class ServerError:
        def get(self, path, **kw): return (500, "upstream boom")
    out = m.usage("sonarr", client=ServerError())
    assert out["ok"] is False
    assert "500" in out["error"]

def test_a_200_with_a_non_list_body_is_an_error_not_zero_bytes():
    """ArrClient._req returns payload=None on an empty 200 body, so this is a
    real path, not a hypothetical. It must not read as '0 bytes managed'."""
    m = _load()
    for payload in (None, {"message": "no content"}):
        class OddBody:
            def __init__(self, p): self.p = p
            def get(self, path, **kw): return (200, self.p)
        out = m.usage("sonarr", client=OddBody(payload))
        assert out["ok"] is False, payload

def test_usage_reports_bytes_only_no_titles_or_identity():
    """Privacy: bytes + count only. Nothing from the raw *arr record leaks
    into the output dict."""
    m = _load()
    out = m.usage("sonarr", client=FakeSonarr())
    assert set(out) == {"slug", "bytes", "human", "title_count", "ok", "error"}

def test_human_boundary_just_under_1024_stays_bytes():
    m = _load()
    assert m.human(1023) == "1023.0 B"

def test_human_boundary_exactly_1024_rolls_to_kb():
    m = _load()
    assert m.human(1024) == "1.0 KB"

def test_human_tb_scale_value():
    m = _load()
    assert m.human(5 * 1024 ** 4) == "5.0 TB"
