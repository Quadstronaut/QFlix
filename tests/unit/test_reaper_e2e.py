"""tests/unit/test_reaper_e2e.py — front-to-back e2e coverage of the REAL
qflix-reaper pipeline: reaper.run() / reaper.main(), never a reimplementation
of the decision logic.

tests/unit/test_qflix_reaper.py already exercises most of run()'s invariants
via the same "monkeypatch every I/O boundary, call the real run()" strategy.
This file is a separate, narrower pass that targets the exact seven scenarios
requested for the e2e sign-off, with two differences from the existing suite:

  1. Every external I/O boundary (Plex GET/PUT, *arr GET/DELETE, Seerr
     GET/DELETE, Kuma's urllib.request.urlopen, Discord notify) is wired to a
     CAPTURE-ONLY fake that records the exact call shape, so assertions read
     the real call list instead of inferring behaviour from exit codes alone.
  2. The age-boundary test reads the module's own DEFAULT_THRESHOLD_DAYS /
     DAY_SECONDS constants rather than hardcoding "60" — if the script's
     default cutover ever changes, the test still exercises the real
     boundary instead of a stale literal.

No seam refactor of qflix-reaper.py was needed: every boundary this file
touches (_plex_creds, plex_sections, plex_items, item_external_ids,
plex_refresh, plex_empty_trash, _arr_client, reconcile_seerr, _notify,
_push_kuma via urllib.request.urlopen, _acquire_run_lock) was already an
importlib-patchable module-level function/attribute, matching the existing
test_qflix_reaper.py convention.
"""
from __future__ import annotations

import importlib.util
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

_REAPER_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "maint" / "qflix-reaper.py"
)

_PLEX_LIBS = [
    "QFlix - Movies", "QFlix - Anime Movies", "QFlix - TV", "QFlix - Anime",
]


@pytest.fixture
def reaper():
    """Load qflix-reaper.py fresh per test under its own module name (distinct
    from test_qflix_reaper.py's 'qflix_reaper') so the two files never share
    module-level state via sys.modules when collected together."""
    spec = importlib.util.spec_from_file_location("qflix_reaper_e2e", _REAPER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# Capture-only fakes for every boundary run() touches.
# ===========================================================================
class CaptureArr:
    """Capture-only stand-in for lib.arr_client.ArrClient. Exact-match lookup
    on tmdbId/tvdbId (mirrors the real Radarr/Sonarr filter semantics) but
    every call is recorded verbatim so tests can assert the exact request
    shape hit the *arr API, not just that "a delete happened"."""

    def __init__(self, slug, movies=None, series=None):
        self.slug = slug
        self.movies = movies or []
        self.series = series or []
        self.calls = []   # [("GET", path, query) | ("DELETE", path, query)]

    def get(self, path, query="", timeout=None):
        self.calls.append(("GET", path, query))
        if path == "/movie":
            return 200, self.movies
        if path == "/series":
            return 200, self.series
        return 404, None

    def delete(self, path, query="", timeout=None):
        self.calls.append(("DELETE", path, query))
        return 200, ""


class FlakyDeleteArr(CaptureArr):
    """Same as CaptureArr but the delete call itself can be told to fail, so
    tests can drive a genuine operational partial-failure through the real
    run() summary/classify_run/_push_kuma path."""

    def __init__(self, *a, fail_delete=False, **k):
        super().__init__(*a, **k)
        self.fail_delete = fail_delete

    def delete(self, path, query="", timeout=None):
        self.calls.append(("DELETE", path, query))
        if self.fail_delete:
            return 500, "boom"
        return 200, ""


def _movie(rk, title, added_at, size=1.0, tmdb=None):
    return {"ratingKey": rk, "title": title, "year": 2000, "addedAt": added_at,
            "sizeGB": size}, {"tmdbId": tmdb, "tvdbId": None}


def _install_full_fakes(reaper, monkeypatch, items_by_lib, ids_by_rk, arr_seed=None,
                        silence_kuma=True):
    """Wire every external boundary run() calls to a capture-only fake.
    Returns a `calls` dict with ordered capture lists for exact-call-list
    assertions:
      calls["plex_refresh"] / calls["plex_trash"]  -> [section_key, ...]
      calls["arr"][slug]                            -> CaptureArr
      calls["notify"]                                -> [(level, msg), ...]
      calls["kuma"]                                  -> [(status, msg), ...] (if silenced)
    """
    calls = {"plex_refresh": [], "plex_trash": [], "arr": {}, "notify": [], "kuma": []}

    monkeypatch.setattr(reaper, "_plex_creds", lambda: ("17025", "tok"))
    sections = {name: str(100 + i) for i, name in enumerate(_PLEX_LIBS)}
    monkeypatch.setattr(reaper, "plex_sections", lambda p, t: (dict(sections), None))
    key_to_lib = {v: k for k, v in sections.items()}

    def fake_items(p, t, key):
        lib = key_to_lib.get(str(key))
        return list(items_by_lib.get(lib, [])), None

    monkeypatch.setattr(reaper, "plex_items", fake_items)
    monkeypatch.setattr(
        reaper, "item_external_ids",
        lambda p, t, rk: dict(ids_by_rk.get(str(rk), {"tmdbId": None, "tvdbId": None})),
    )
    monkeypatch.setattr(reaper, "plex_refresh",
                        lambda p, t, k: calls["plex_refresh"].append(k) or True)
    monkeypatch.setattr(reaper, "plex_empty_trash",
                        lambda p, t, k: calls["plex_trash"].append(k) or True)

    arr_seed = arr_seed or {}

    def fake_arr_client(slug):
        if slug not in calls["arr"]:
            seed = arr_seed.get(slug, {})
            cls = seed.get("cls", CaptureArr)
            calls["arr"][slug] = cls(
                slug, movies=seed.get("movies"), series=seed.get("series"),
                **({"fail_delete": seed["fail_delete"]} if "fail_delete" in seed else {}),
            )
        return calls["arr"][slug]

    monkeypatch.setattr(reaper, "_arr_client", fake_arr_client)
    monkeypatch.setattr(reaper, "reconcile_seerr", lambda execute: (0, 0))
    monkeypatch.setattr(reaper, "_notify",
                        lambda msg, level="info": calls["notify"].append((level, msg)))
    if silence_kuma:
        monkeypatch.setattr(reaper, "_push_kuma",
                            lambda status, msg: calls["kuma"].append((status, msg)))
    return sections, calls


def _json_blocks(text):
    """run() interleaves plain log lines with one or two pretty-printed JSON
    blocks (--json plan, and on execute a second summary block). Extract
    every top-level JSON object in encounter order via raw_decode, skipping
    the log-line text around them."""
    decoder = json.JSONDecoder()
    blocks = []
    idx = 0
    while True:
        brace = text.find("{", idx)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
            blocks.append(obj)
            idx = end
        except json.JSONDecodeError:
            idx = brace + 1
    return blocks


def _mk_args(reaper, **over):
    base = dict(
        execute=False, threshold_days=reaper.DEFAULT_THRESHOLD_DAYS,
        exclude_file="/nonexistent-on-purpose",
        max_items=reaper.DEFAULT_MAX_ITEMS, max_pct=100, force=False,
        manifest_dir=None, library=None, emit_json=False,
        orphan_grace_hours=24.0, orphan_remind_days=7.0, orphan_state=None,
    )
    base.update(over)
    return type("Args", (), base)()


# ===========================================================================
# 1. Age boundary: only items strictly older than the threshold are candidates.
#    Uses the module's OWN threshold constant, never a hardcoded "60".
# ===========================================================================
def test_age_boundary_only_strictly_older_items_are_deleted(reaper, tmp_path, monkeypatch):
    threshold_days = reaper.DEFAULT_THRESHOLD_DAYS   # module's own constant
    day = reaper.DAY_SECONDS
    test_now = int(time.time())

    # A few seconds of buffer around the exact boundary absorbs the wall-clock
    # gap between "now" measured here and run()'s own now = time.time() call a
    # moment later, without weakening what's being proven: well-under,
    # just-under (still not a candidate), and well-over (a candidate).
    young = test_now - (threshold_days - 1) * day               # well under
    just_under = test_now - threshold_days * day + 5             # a hair under
    well_over = test_now - (threshold_days + 1) * day - 10       # comfortably over

    m_young, ids_young = _movie("1", "Young", young, tmdb=601)
    m_edge, ids_edge = _movie("2", "JustUnder", just_under, tmdb=602)
    m_old, ids_old = _movie("3", "WellOver", well_over, tmdb=603)
    ids = {"1": ids_young, "2": ids_edge, "3": ids_old}
    items = {"QFlix - Movies": [m_young, m_edge, m_old]}

    _, calls = _install_full_fakes(reaper, monkeypatch, items, ids, arr_seed={
        "radarr": {"movies": [
            {"id": 901, "tmdbId": 601, "hasFile": True},
            {"id": 902, "tmdbId": 602, "hasFile": True},
            {"id": 903, "tmdbId": 603, "hasFile": True},
        ]},
    })

    rc = reaper.run(_mk_args(reaper, execute=True, threshold_days=threshold_days,
                             manifest_dir=str(tmp_path)))

    assert rc == reaper.EXIT_OK
    radarr_calls = calls["arr"]["radarr"].calls
    deletes = [c for c in radarr_calls if c[0] == "DELETE"]
    assert deletes == [("DELETE", "/movie/903", "deleteFiles=true&addImportExclusion=false")]


# ===========================================================================
# 2. Caps: more candidates than the per-run cap -> exactly cap-many deleted,
#    deterministic oldest-first order, remainder untouched.
# ===========================================================================
def test_cap_deletes_exactly_cap_many_oldest_first_remainder_untouched(
    reaper, tmp_path, monkeypatch,
):
    cap = 3
    base = int(time.time()) - (reaper.DEFAULT_THRESHOLD_DAYS + 100) * reaper.DAY_SECONDS
    movies, ids = [], {}
    for i in range(6):
        m, idd = _movie(str(i), "M%d" % i, base + i, tmdb=600 + i)   # ascending age
        movies.append(m)
        ids[str(i)] = idd

    _, calls = _install_full_fakes(reaper, monkeypatch, {"QFlix - Movies": movies}, ids,
                                   arr_seed={"radarr": {"movies": [
                                       {"id": 100 + i, "tmdbId": 600 + i, "hasFile": True}
                                       for i in range(6)
                                   ]}})

    rc = reaper.run(_mk_args(reaper, execute=True, max_items=cap,
                             manifest_dir=str(tmp_path)))

    assert rc == reaper.EXIT_OK
    deletes = [c for c in calls["arr"]["radarr"].calls if c[0] == "DELETE"]
    assert len(deletes) == cap
    # oldest `cap` ratingKeys are 0,1,2 -> tmdbId 600..602 -> arrId 100..102.
    deleted_ids = sorted(int(path.rsplit("/", 1)[-1]) for _, path, _ in deletes)
    assert deleted_ids == [100, 101, 102]

    manifest = json.loads(next(tmp_path.glob("qflix-reaper-*.json")).read_text())
    assert manifest["total_count"] == cap
    assert sorted(c["arrId"] for c in manifest["candidates"]) == [100, 101, 102]
    # the deferred 3 (arrId 103,104,105) never entered the manifest or a delete call.
    assert {103, 104, 105}.isdisjoint(set(deleted_ids))
    assert {103, 104, 105}.isdisjoint({c["arrId"] for c in manifest["candidates"]})


# ===========================================================================
# 3. Exclusion rail: an item that IS eligible by age is NOT deleted once
#    excluded — and never even reaches *arr resolution (excluded before
#    resolve in run()'s per-item loop).
# ===========================================================================
def test_excluded_eligible_item_is_never_deleted_or_resolved(reaper, tmp_path, monkeypatch):
    threshold_days = reaper.DEFAULT_THRESHOLD_DAYS
    old = int(time.time()) - (threshold_days + 5) * reaper.DAY_SECONDS
    movie, idd = _movie("1", "Protected Movie", old, tmdb=603)

    exclude_file = tmp_path / "exclude.txt"
    exclude_file.write_text("tmdb:603\n", encoding="utf-8")

    _, calls = _install_full_fakes(
        reaper, monkeypatch, {"QFlix - Movies": [movie]}, {"1": idd},
        arr_seed={"radarr": {"movies": [{"id": 42, "tmdbId": 603, "hasFile": True}]}},
    )

    rc = reaper.run(_mk_args(reaper, execute=True, exclude_file=str(exclude_file),
                             manifest_dir=str(tmp_path)))

    assert rc == reaper.EXIT_OK
    # The arr client IS built once per library unconditionally (run() builds it
    # before the per-item loop), but is_excluded() short-circuits the loop
    # BEFORE resolve_radarr_id() is ever invoked for the excluded item — so
    # the client itself must have made zero GET/DELETE calls.
    assert calls["arr"]["radarr"].calls == [], (
        "excluded item must never reach *arr resolution — is_excluded() runs "
        "BEFORE resolution in the per-item loop"
    )
    manifest_files = list(tmp_path.glob("qflix-reaper-*.json"))
    assert manifest_files, "manifest is still written (execute mode) even with 0 candidates"
    assert json.loads(manifest_files[0].read_text())["total_count"] == 0


# ===========================================================================
# 4. Dry-run vs execute: same candidate set, dry-run issues ZERO delete calls,
#    execute issues the exact expected delete call list.
# ===========================================================================
def test_dry_run_reports_same_candidates_zero_deletes_execute_matches_exact_calls(
    reaper, tmp_path, monkeypatch, capsys,
):
    threshold_days = reaper.DEFAULT_THRESHOLD_DAYS
    old = int(time.time()) - (threshold_days + 5) * reaper.DAY_SECONDS
    movie, idd = _movie("1", "Old Movie", old, size=12.5, tmdb=603)
    items = {"QFlix - Movies": [movie]}
    ids = {"1": idd}
    arr_seed = {"radarr": {"movies": [{"id": 42, "tmdbId": 603, "hasFile": True}]}}

    # ---- dry-run pass ----
    _install_full_fakes(reaper, monkeypatch, items, ids, arr_seed=arr_seed)
    rc_dry = reaper.run(_mk_args(reaper, execute=False, emit_json=True,
                                 manifest_dir=str(tmp_path / "dry")))
    dry_blocks = _json_blocks(capsys.readouterr().out)
    dry_plan = dry_blocks[0]     # dry-run prints exactly one JSON block: the plan
    assert rc_dry == reaper.EXIT_OK
    assert dry_plan["total_count"] == 1
    assert dry_plan["candidates"][0]["title"] == "Old Movie"
    assert not list((tmp_path / "dry").glob("*.json")) if (tmp_path / "dry").exists() else True

    # ---- fresh capture-only fakes for the execute pass (same fixture data) ----
    (tmp_path / "exec").mkdir()
    _, calls = _install_full_fakes(reaper, monkeypatch, items, ids, arr_seed=arr_seed)
    rc_exec = reaper.run(_mk_args(reaper, execute=True, emit_json=True,
                                  manifest_dir=str(tmp_path / "exec")))
    exec_blocks = _json_blocks(capsys.readouterr().out)
    exec_plan, exec_summary = exec_blocks[0], exec_blocks[-1]   # plan, then result summary

    assert rc_exec == reaper.EXIT_OK
    # Same candidate identity resolved in both modes.
    assert exec_plan["candidates"][0]["title"] == "Old Movie"
    assert exec_summary["deleted"] == 1
    assert calls["arr"]["radarr"].calls == [
        ("GET", "/movie", ""),
        ("DELETE", "/movie/42", "deleteFiles=true&addImportExclusion=false"),
    ]
    assert len(calls["plex_refresh"]) == 1 and len(calls["plex_trash"]) == 1
    assert list((tmp_path / "exec").glob("qflix-reaper-*.json")), "execute writes a manifest"


def test_dry_run_never_calls_delete_even_when_resolved(reaper, tmp_path, monkeypatch):
    threshold_days = reaper.DEFAULT_THRESHOLD_DAYS
    old = int(time.time()) - (threshold_days + 5) * reaper.DAY_SECONDS
    movie, idd = _movie("1", "Old Movie", old, tmdb=603)
    _, calls = _install_full_fakes(
        reaper, monkeypatch, {"QFlix - Movies": [movie]}, {"1": idd},
        arr_seed={"radarr": {"movies": [{"id": 42, "tmdbId": 603, "hasFile": True}]}},
    )
    rc = reaper.run(_mk_args(reaper, execute=False, manifest_dir=str(tmp_path)))
    assert rc == reaper.EXIT_OK
    # Resolution IS attempted in dry-run (candidates are still computed/printed)...
    assert ("GET", "/movie", "") in calls["arr"]["radarr"].calls
    # ...but no DELETE call is ever issued, and no manifest is written.
    assert not any(c[0] == "DELETE" for c in calls["arr"]["radarr"].calls)
    assert list(tmp_path.glob("qflix-reaper-*.json")) == []


# ===========================================================================
# 5. Audit manifest: written to --manifest-dir with the exact expected shape.
# ===========================================================================
def test_manifest_contents_match_candidate_exactly(reaper, tmp_path, monkeypatch):
    threshold_days = reaper.DEFAULT_THRESHOLD_DAYS
    old_ts = int(time.time()) - (threshold_days + 3) * reaper.DAY_SECONDS
    movie, idd = _movie("77", "Manifest Movie", old_ts, size=9.75, tmdb=603)
    _install_full_fakes(
        reaper, monkeypatch, {"QFlix - Movies": [movie]}, {"77": idd},
        arr_seed={"radarr": {"movies": [{"id": 555, "tmdbId": 603, "hasFile": True}]}},
    )

    rc = reaper.run(_mk_args(reaper, execute=True, threshold_days=threshold_days,
                             manifest_dir=str(tmp_path)))
    assert rc == reaper.EXIT_OK

    manifests = list(tmp_path.glob("qflix-reaper-*.json"))
    assert len(manifests) == 1
    doc = json.loads(manifests[0].read_text())

    assert doc["flags"] == {
        "threshold_days": threshold_days, "max_items": reaper.DEFAULT_MAX_ITEMS,
        "max_pct": 100, "force": False, "execute": True,
    }
    assert doc["total_count"] == 1
    assert doc["total_reclaim_gb"] == 9.75
    assert doc["candidates"] == [{
        "title": "Manifest Movie", "year": 2000, "type": "movie",
        "library": "QFlix - Movies", "ratingKey": "77", "tmdbId": 603,
        "tvdbId": None, "arrId": 555, "sizeGB": 9.75, "addedAt": old_ts,
    }]


# ===========================================================================
# 6. Kuma push: exercise the REAL _push_kuma (not a stub) via a fake
#    urllib.request.urlopen, so the exact pushed URL/token/status/msg shape
#    is proven, plus the empty-token silent-skip regression (2026-07-15).
# ===========================================================================
def _capture_kuma_urlopen(monkeypatch, token, urls):
    class _Resp:
        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=None):
        urls.append(url)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    if token is not None:
        monkeypatch.setenv("QFLIX_REAPER_KUMA_TOKEN", token)


def test_kuma_push_up_on_clean_dry_run(reaper, tmp_path, monkeypatch):
    urls = []
    _capture_kuma_urlopen(monkeypatch, "topsecret-tok", urls)

    threshold_days = reaper.DEFAULT_THRESHOLD_DAYS
    old = int(time.time()) - (threshold_days + 5) * reaper.DAY_SECONDS
    movie, idd = _movie("1", "Old Movie", old, tmdb=603)
    _install_full_fakes(
        reaper, monkeypatch, {"QFlix - Movies": [movie]}, {"1": idd},
        arr_seed={"radarr": {"movies": [{"id": 42, "tmdbId": 603, "hasFile": True}]}},
        silence_kuma=False,   # exercise the REAL _push_kuma
    )

    rc = reaper.run(_mk_args(reaper, execute=False, manifest_dir=str(tmp_path)))
    assert rc == reaper.EXIT_OK
    assert len(urls) == 1
    parsed = urllib.parse.urlsplit(urls[0])
    assert parsed.path == "/api/push/topsecret-tok"
    qs = dict(urllib.parse.parse_qsl(parsed.query))
    assert qs["status"] == "up"
    assert "dry-run" in qs["msg"]


def test_kuma_push_down_on_partial_delete_failure(reaper, tmp_path, monkeypatch):
    urls = []
    _capture_kuma_urlopen(monkeypatch, "topsecret-tok", urls)

    threshold_days = reaper.DEFAULT_THRESHOLD_DAYS
    old = int(time.time()) - (threshold_days + 5) * reaper.DAY_SECONDS
    movie, idd = _movie("1", "Flaky Movie", old, tmdb=603)
    _install_full_fakes(
        reaper, monkeypatch, {"QFlix - Movies": [movie]}, {"1": idd},
        arr_seed={"radarr": {"movies": [{"id": 42, "tmdbId": 603, "hasFile": True}],
                             "cls": FlakyDeleteArr, "fail_delete": True}},
        silence_kuma=False,
    )

    rc = reaper.run(_mk_args(reaper, execute=True, manifest_dir=str(tmp_path)))
    assert rc == reaper.EXIT_PARTIAL
    assert len(urls) == 1
    parsed = urllib.parse.urlsplit(urls[0])
    assert parsed.path == "/api/push/topsecret-tok"
    qs = dict(urllib.parse.parse_qsl(parsed.query))
    assert qs["status"] == "down"
    assert "partial failures" in qs["msg"]


def test_kuma_empty_token_silently_skips_push_2026_07_15_regression(
    reaper, tmp_path, monkeypatch,
):
    """The 2026-07-15 incident: with no Kuma token configured, _push_kuma must
    return WITHOUT ever calling urlopen — silent-skip, not silent-fail. This
    is intentional degrade-to-no-op behaviour; asserted here explicitly so a
    future change that makes it try-and-swallow (masking a real outage
    differently) shows up as a test failure instead of shipping quietly."""
    urls = []
    _capture_kuma_urlopen(monkeypatch, token=None, urls=urls)   # no env token set

    threshold_days = reaper.DEFAULT_THRESHOLD_DAYS
    old = int(time.time()) - (threshold_days + 5) * reaper.DAY_SECONDS
    movie, idd = _movie("1", "Old Movie", old, tmdb=603)
    _install_full_fakes(
        reaper, monkeypatch, {"QFlix - Movies": [movie]}, {"1": idd},
        arr_seed={"radarr": {"movies": [{"id": 42, "tmdbId": 603, "hasFile": True}]}},
        silence_kuma=False,
    )

    rc = reaper.run(_mk_args(reaper, execute=False, manifest_dir=str(tmp_path)))
    assert rc == reaper.EXIT_OK
    assert urls == [], "no token configured -> _push_kuma must no-op, never call urlopen"


# ===========================================================================
# 7. Run-lock: a second concurrent --execute must be refused via flock. On
#    this Windows test host `fcntl` is unavailable, so _acquire_run_lock()
#    documents-and-degrades to a sentinel (True) that grants no real mutual
#    exclusion — assert THAT degradation explicitly rather than faking a
#    flock this platform cannot provide.
# ===========================================================================
def test_run_lock_degrades_to_sentinel_without_fcntl(reaper):
    try:
        import fcntl  # noqa: F401
        pytest.skip("fcntl IS available on this host — real flock exclusion path "
                    "applies here, not the degrade path this test documents.")
    except ImportError:
        pass

    first = reaper._acquire_run_lock()
    assert first is True, "fcntl-unavailable path must return the True sentinel"
    second = reaper._acquire_run_lock()
    assert second is True, (
        "documented degradation: without fcntl a SECOND concurrent lock "
        "acquisition is NOT refused locally (no real exclusion enforced) — "
        "true mutual exclusion only exists on the Linux seedbox target"
    )
    # Release must be a safe no-op for the sentinel, never raising.
    reaper._release_run_lock(first)
    reaper._release_run_lock(second)


def test_run_lock_sentinel_does_not_block_a_second_execute_run(reaper, tmp_path, monkeypatch):
    """Integration-level view of the same degradation: two sequential
    --execute runs both proceed to completion locally (lock never refuses),
    which is the honestly-documented Windows-host behaviour, not a bug in
    this test suite."""
    try:
        import fcntl  # noqa: F401
        pytest.skip("fcntl available — concurrent-refusal path is exercised for real here.")
    except ImportError:
        pass

    threshold_days = reaper.DEFAULT_THRESHOLD_DAYS
    old = int(time.time()) - (threshold_days + 5) * reaper.DAY_SECONDS
    movie, idd = _movie("1", "Old Movie", old, tmdb=603)

    for i in range(2):
        (tmp_path / str(i)).mkdir()
        _, calls = _install_full_fakes(
            reaper, monkeypatch, {"QFlix - Movies": [movie]}, {"1": idd},
            arr_seed={"radarr": {"movies": [{"id": 42, "tmdbId": 603, "hasFile": True}]}},
        )
        rc = reaper.run(_mk_args(reaper, execute=True, manifest_dir=str(tmp_path / str(i))))
        # Neither run is refused with EXIT_FATAL for "lock held" — both proceed.
        assert rc == reaper.EXIT_OK
        assert calls["arr"]["radarr"].calls[-1][0] == "DELETE"
