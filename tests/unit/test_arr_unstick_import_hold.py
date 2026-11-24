"""QFLX-126: the hourly --unstick sweep must not turn an import OUTAGE into a
mass delete + blocklist + unmonitor of good releases.

MODE_IMPORT judges one item at a time ("completed, not imported for 6h = bad
release"). When the import path itself is broken (root folder moved, a
permission slip, quota full) every completed download looks like that, and the
sweep used to remove up to 10 an hour from the download client and blocklist
them, then park the titles after three rounds. Nothing stopped the next hour.

These tests drive the real cmd_unstick through the Sweep harness from
test_arr_regrab_loop_guard.py.
"""
from __future__ import annotations

import json
import time

import pytest

from tests.unit.test_arr_regrab_loop_guard import Sweep, arrhk


def _import_stuck(i: int, *, poison: bool = False, series: int | None = None) -> dict:
    msgs = [{"title": "x", "messages": ["Access to the path is denied."]}]
    if poison:
        msgs = [{"title": "x", "messages": [
            "Caution: Found executable file with extension: '.exe'"]}]
    return {
        "id": 100 + i, "downloadId": "DL%04d" % i, "title": "Good.Release.%d" % i,
        "status": "completed", "trackedDownloadState": "importPending",
        "seriesId": series if series is not None else 500 + i,
        "episodeId": 9000 + i, "seasonNumber": 1,
        "statusMessages": msgs, "sizeleft": 0,
    }


def _aged(records, hours=8.0, slug="sonarr"):
    now = time.time()
    return {
        arrhk._state_key(slug, r["downloadId"]): {
            "title": r["title"], "queue_id": r["id"],
            "first_seen_stuck": now - hours * 3600, "slug": slug,
            "mode": arrhk.MODE_IMPORT, "sizeleft_history": [],
        } for r in records
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("ARR_IMPORT_SYSTEMIC_MIN", "ARR_MAX_ACTIONS_PER_RUN",
              "ARR_MAX_ACTIONS_PER_SLUG"):
        monkeypatch.delenv(k, raising=False)


def test_an_import_outage_deletes_blocklists_and_parks_nothing(tmp_path, monkeypatch):
    """20 good downloads, all past the 6h grace, all failing to import for the
    same reason. Before the gate: 5 DELETE ...blocklist=true this sweep (the
    per-slug cap), 5 more every hour after."""
    recs = [_import_stuck(i) for i in range(20)]
    sw = Sweep(tmp_path, monkeypatch, recs, prior_state=_aged(recs))
    rc = sw.run()
    assert rc == 0
    assert sw.deletes == [], "good downloads were deleted during an import outage"
    assert sw.puts == [], "titles were unmonitored during an import outage"
    assert sw.notifications, "the hold must be reported"
    level, msg = sw.notifications[-1]
    assert level == "error"
    assert "import hold on sonarr: 20 completed downloads" in msg
    assert "Nothing was deleted or blocklisted" in msg
    # Still tracked, so the sweep picks them up again once imports work.
    state = json.loads(sw.state_file.read_text(encoding="utf-8"))
    assert len(state) == 20


def test_the_hold_persists_across_sweeps_instead_of_draining_the_queue(tmp_path, monkeypatch):
    recs = [_import_stuck(i) for i in range(8)]
    sw = Sweep(tmp_path, monkeypatch, recs, prior_state=_aged(recs))
    for _ in range(5):
        sw.run()
    assert sw.deletes == [] and sw.puts == []


def test_a_few_bad_releases_are_still_removed(tmp_path, monkeypatch):
    """Below the threshold the old behaviour is unchanged: this is the normal
    case the sweep exists for (about one a day on the box)."""
    recs = [_import_stuck(i) for i in range(4)]
    sw = Sweep(tmp_path, monkeypatch, recs, prior_state=_aged(recs))
    sw.run()
    assert len(sw.deletes) == 4
    assert all("blocklist=true" in c[1] for c in sw.deletes)


def test_one_stuck_season_pack_is_one_download_not_a_systemic_failure(tmp_path, monkeypatch):
    """A season pack is many queue rows sharing ONE downloadId."""
    recs = []
    for ep in range(12):
        r = _import_stuck(0, series=77)
        r["id"] = 300 + ep
        r["episodeId"] = 7000 + ep
        recs.append(r)
    sw = Sweep(tmp_path, monkeypatch, recs, prior_state=_aged(recs[:1]))
    sw.run()
    assert sw.deletes, "a single stuck pack must still be handled"
    assert not any("import hold" in m for _, m in sw.notifications)


def test_fresh_stuck_imports_do_not_count_toward_the_hold(tmp_path, monkeypatch):
    """Only downloads PAST the grace count: a burst of just-completed
    downloads waiting on a slow import is not an outage."""
    old = [_import_stuck(i) for i in range(2)]
    new = [_import_stuck(i) for i in range(2, 12)]
    state = _aged(old)
    state.update(_aged(new, hours=1.0))
    sw = Sweep(tmp_path, monkeypatch, old + new, prior_state=state)
    sw.run()
    assert len(sw.deletes) == 2
    assert not any("import hold" in m for _, m in sw.notifications)


def test_poison_payloads_are_still_removed_during_a_hold(tmp_path, monkeypatch):
    """Poison does not depend on the import path, so the hold leaves it alone."""
    recs = [_import_stuck(i) for i in range(6)] + [_import_stuck(50, poison=True)]
    sw = Sweep(tmp_path, monkeypatch, recs, prior_state=_aged(recs))
    sw.run()
    assert len(sw.deletes) == 1 and "/queue/150" in sw.deletes[0][1]
    assert any("import hold on sonarr: 6" in m for _, m in sw.notifications)


def test_the_gate_can_be_released_for_one_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ARR_IMPORT_SYSTEMIC_MIN", "0")
    recs = [_import_stuck(i) for i in range(8)]
    sw = Sweep(tmp_path, monkeypatch, recs, prior_state=_aged(recs))
    sw.run()
    assert len(sw.deletes) == 5, "with the gate off the per-slug cap is the only brake"


def test_dry_run_reports_the_hold_and_touches_nothing(tmp_path, monkeypatch, capsys):
    recs = [_import_stuck(i) for i in range(9)]
    sw = Sweep(tmp_path, monkeypatch, recs, prior_state=_aged(recs))
    sw.run(dry_run=True)
    out = capsys.readouterr().out
    assert "[import-hold] sonarr: 9 completed download(s)" in out
    assert "would unstick" not in out
    assert sw.deletes == [] and sw.puts == [] and sw.notifications == []
