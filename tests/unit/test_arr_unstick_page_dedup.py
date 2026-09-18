"""tests/unit/test_arr_unstick_page_dedup.py — cmd_unstick's cross-run page
dedup (council round 2, 2026-09-17): stable seriesId/movieId identity (D3),
the deleted title-hash fallback, the bounded id-less fold, the cap-hit
carve-out, and the durable audit log (D4) that survives every suppression.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ARR_HOUSEKEEPING_PATH = ROOT / "scripts" / "maint" / "arr-housekeeping.py"
sys.path.insert(0, str(ROOT / "scripts" / "maint"))

# Loaded as its OWN module object (distinct from test_arr_housekeeping.py's
# "arr_housekeeping") so monkeypatches in this file never leak module state
# into that file's tests or vice versa.
_spec = importlib.util.spec_from_file_location(
    "arr_housekeeping_page_dedup", ARR_HOUSEKEEPING_PATH,
)
arrhk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arrhk)


PEER_STALL_ERROR = "The download is stalled with no connections"

# The eleven real measured Yellowjackets S03E01 release titles for the SAME
# re-grab (2026-09-17) — the evidence that killed the title-hash fallback.
YELLOWJACKETS_TITLES = [
    "Yellowjackets.S03E01.1080p.WEB.H264-EDITH",
    "Yellowjackets.S03E01.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb",
    "Yellowjackets.S03E01.HDTV.x264-TORRENTGALAXY",
    "Yellowjackets.S03E01.Storytelling.1080p.WEB.h264-ETHEL",
    "Yellowjackets.S03E01.720p.WEB.h264-EDITH",
    "yellowjackets.s03e01.1080p.web.h264-successfulcrab",
    "Yellowjackets.S03E01.PROPER.1080p.WEB.H264-CAKES",
    "Yellowjackets.S03E01.1080p.NF.WEB-DL.DDP5.1.Atmos.H.264-FLUX",
    "Yellowjackets.S03E01.REPACK.1080p.WEB.H264-NHTFS",
    "Yellowjackets.S03E01.1080p.WEBRip.x265-RARBG-EZTV",
    "Yellowjackets.S03E01.Storytelling.2160p.WEB.h264-ETHEL-Scrambled",
]
assert len(YELLOWJACKETS_TITLES) == 11


def _item(qid: int, title: str, *, series_id=None, movie_id=None,
          download_id: str | None = None) -> dict:
    it = {
        "id": qid,
        "title": title,
        "status": "warning",
        "trackedDownloadStatus": "ok",
        "errorMessage": PEER_STALL_ERROR,
        "downloadId": download_id or f"{qid:040x}".upper(),
        "size": 100,
        "sizeleft": 100,
    }
    if series_id is not None:
        it["seriesId"] = series_id
    if movie_id is not None:
        it["movieId"] = movie_id
    return it


def _aged_state_for(items: list[dict], *, slug="sonarr", mode="stalled-no-peers") -> dict:
    """Pre-populate stuck-queue-state.json so every item is ALREADY aged out
    (first_seen_stuck 24h ago) — this is what makes cmd_unstick act on it
    immediately instead of merely recording a first sighting."""
    out = {}
    for it in items:
        sk = arrhk._state_key(slug, it["downloadId"])
        out[sk] = {
            "title": it["title"], "queue_id": it["id"],
            "first_seen_stuck": time.time() - 86400,
            "slug": slug, "mode": mode, "sizeleft_history": [],
        }
    return out


def _fake_req_factory(records: list[dict], *, slug="sonarr"):
    def fake_req(method, url, key, **kw):
        if method == "GET" and f"{slug}/" in url and "/queue" in url \
                and f"{slug}2" not in url:
            return 200, json.dumps({"records": records})
        if method == "GET":
            return 200, json.dumps({"records": []})
        if method == "DELETE":
            return 200, ""
        return 500, ""
    return fake_req


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Common isolation: state/ledger/log paths, only sonarr has a key, and
    a spy on _notify."""
    state_file = tmp_path / "stuck.json"
    page_ledger = tmp_path / "unstick-pages.json"
    unstick_log = tmp_path / "unstick.log"
    monkeypatch.setattr(arrhk, "STATE_DIR", tmp_path)
    monkeypatch.setattr(arrhk, "STUCK_STATE_FILE", state_file)
    monkeypatch.setattr(arrhk, "UNSTICK_PAGE_LEDGER", page_ledger)
    monkeypatch.setattr(arrhk, "UNSTICK_LOG", unstick_log)
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")

    notify_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        arrhk, "_notify",
        lambda body, level="info": notify_calls.append((body, level)),
    )

    class Rig:
        pass

    r = Rig()
    r.state_file = state_file
    r.page_ledger = page_ledger
    r.unstick_log = unstick_log
    r.notify_calls = notify_calls
    r.monkeypatch = monkeypatch
    return r


# ---------------------------------------------------------------------------
# D3 boundaries — is not None, never truthiness
# ---------------------------------------------------------------------------

def test_page_key_series_zero_is_a_valid_id():
    assert arrhk._page_key("sonarr", {"seriesId": 0}) == "unstick:sonarr:series:0"


def test_page_key_movie_zero_is_a_valid_id():
    assert arrhk._page_key("radarr", {"movieId": 0}) == "unstick:radarr:movie:0"


def test_page_key_no_ids_folds_to_unknown():
    assert arrhk._page_key("sonarr", {}) == "unstick:unknown-items:sonarr"


def test_page_key_none_series_falls_through_to_movie_zero():
    assert arrhk._page_key(
        "sonarr", {"seriesId": None, "movieId": 0}) == "unstick:sonarr:movie:0"


def test_page_key_bool_series_is_rejected_not_treated_as_id_one():
    # isinstance(True, int) is True in Python -- must be explicitly excluded.
    assert arrhk._page_key("sonarr", {"seriesId": False}) == "unstick:unknown-items:sonarr"


def test_page_key_distinct_ids_never_collide():
    assert (arrhk._page_key("sonarr", {"seriesId": 0})
            != arrhk._page_key("sonarr", {"seriesId": 1}))


def test_no_hashlib_import():
    text = ARR_HOUSEKEEPING_PATH.read_text(encoding="utf-8")
    assert "hashlib" not in text


# ---------------------------------------------------------------------------
# The measured evidence that killed the title-hash fallback
# ---------------------------------------------------------------------------

def test_same_item_under_eleven_titles_pages_once(rig):
    for i, title in enumerate(YELLOWJACKETS_TITLES):
        item = _item(9000 + i, title, series_id=101)
        rig.state_file.write_text(json.dumps(_aged_state_for([item])))
        rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory([item]))
        arrhk.cmd_unstick(dry_run=False)

    assert len(rig.notify_calls) == 1


def test_new_series_pages_on_the_very_next_run(rig):
    item1 = _item(9001, "Series101.S01E01.WEB", series_id=101)
    rig.state_file.write_text(json.dumps(_aged_state_for([item1])))
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory([item1]))
    arrhk.cmd_unstick(dry_run=False)
    assert len(rig.notify_calls) == 1

    item2 = _item(9002, "Series202.S01E01.WEB", series_id=202)
    rig.state_file.write_text(json.dumps(_aged_state_for([item2])))
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory([item2]))
    arrhk.cmd_unstick(dry_run=False)

    assert len(rig.notify_calls) == 2
    body2, _level2 = rig.notify_calls[1]
    assert "Series202" in body2
    assert "Series101" not in body2


def test_id_less_rows_fold_into_one_bounded_key(rig):
    items = [_item(5000 + i, f"IdLess.Title.{i:02d}.WEB") for i in range(50)]
    rig.state_file.write_text(json.dumps(_aged_state_for(items)))
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory(items))
    rig.monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "1000")
    rig.monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "1000")

    arrhk.cmd_unstick(dry_run=False)

    assert len(rig.notify_calls) == 1
    body, _level = rig.notify_calls[0]
    assert "50" in body
    assert 1 <= body.count("IdLess.Title") <= 3

    ledger = json.loads(rig.page_ledger.read_text())
    unknown_keys = [k for k in ledger if k.startswith("unstick:unknown-items:")]
    assert unknown_keys == ["unstick:unknown-items:sonarr"]


# ---------------------------------------------------------------------------
# Cap-hit carve-out
# ---------------------------------------------------------------------------

def test_cap_hit_survives_dedup_on_its_own_key(rig):
    items = [_item(6000 + i, f"Cap.Title.{i}.WEB", series_id=300 + i) for i in range(4)]
    rig.state_file.write_text(json.dumps(_aged_state_for(items)))
    rig.monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "2")
    rig.monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "2")
    # Pre-stamp the two items that WILL act this run as already-paged, so
    # dedup suppresses them -- proving the cap-hit page survives even when
    # every per-item key is suppressed.
    now = time.time()
    rig.page_ledger.write_text(json.dumps({
        "unstick:sonarr:series:300": now,
        "unstick:sonarr:series:301": now,
    }))
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory(items))

    arrhk.cmd_unstick(dry_run=False)

    assert len(rig.notify_calls) == 1
    body, level = rig.notify_calls[0]
    assert level == "error"
    assert body.startswith("⚠ cap hit")
    assert "2" in body  # the suppressed per-item count


def test_cap_hit_not_repaged_within_window_but_new_item_still_pages(rig):
    rig.monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "1")
    rig.monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "1")

    it1 = _item(1, "Run1.A.WEB", series_id=700)
    it2 = _item(2, "Run1.B.WEB", series_id=701)
    rig.state_file.write_text(json.dumps(_aged_state_for([it1, it2])))
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory([it1, it2]))
    arrhk.cmd_unstick(dry_run=False)

    assert len(rig.notify_calls) == 1
    assert rig.notify_calls[0][0].startswith("⚠ cap hit")
    assert rig.notify_calls[0][1] == "error"

    it3 = _item(3, "Run2.Fresh.WEB", series_id=800)
    it4 = _item(4, "Run2.Capped.WEB", series_id=801)
    rig.state_file.write_text(json.dumps(_aged_state_for([it3, it4])))
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory([it3, it4]))
    arrhk.cmd_unstick(dry_run=False)

    assert len(rig.notify_calls) == 2
    body2, level2 = rig.notify_calls[1]
    assert "⚠ cap hit" not in body2
    assert level2 == "warning"
    assert "Run2.Fresh" in body2


# ---------------------------------------------------------------------------
# Suppression is a NOTIFICATION policy only — the durable log survives it
# ---------------------------------------------------------------------------

def test_nothing_due_means_no_notify_at_all(rig):
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory([]))
    arrhk.cmd_unstick(dry_run=False)
    assert rig.notify_calls == []


def test_suppression_still_writes_the_durable_log(rig):
    items = [_item(8000 + i, f"Suppressed.Title.{i}.WEB", series_id=900 + i)
             for i in range(3)]
    rig.state_file.write_text(json.dumps(_aged_state_for(items)))
    now = time.time()
    rig.page_ledger.write_text(json.dumps(
        {f"unstick:sonarr:series:{900 + i}": now for i in range(3)}))
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory(items))

    arrhk.cmd_unstick(dry_run=False)

    assert rig.notify_calls == []
    lines = rig.unstick_log.read_text(encoding="utf-8").splitlines()
    decisions = [l.split("\t")[1] for l in lines]
    assert decisions.count("acted") == 3
    assert decisions.count("page-suppressed") == 3
    assert decisions.count("sweep-summary") == 1


def test_sweep_summary_written_on_no_op_run(rig):
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory([]))
    arrhk.cmd_unstick(dry_run=False)
    lines = rig.unstick_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].split("\t")[1] == "sweep-summary"


# ---------------------------------------------------------------------------
# D4 — audit log injection safety
# ---------------------------------------------------------------------------

def test_hostile_title_cannot_forge_a_log_row(rig):
    # Under 80 chars: the queue title is hard-truncated to 80 BEFORE
    # sanitization (existing, unchanged behaviour) -- a longer forged payload
    # would just get chopped before the injected chars, which proves nothing.
    hostile_title = (
        "a\n2026-09-17T00:00:00Z\tpage-suppressed\tx\tmode\tkey\t0\t0\tforged"
    )
    assert len(hostile_title) <= 80
    item = _item(7001, hostile_title, series_id=501)
    rig.state_file.write_text(json.dumps(_aged_state_for([item])))
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory([item]))

    arrhk.cmd_unstick(dry_run=False)

    lines = rig.unstick_log.read_text(encoding="utf-8").splitlines()
    # acted (sweep) + paged (aggregation) + sweep-summary
    assert len(lines) == 3
    for line in lines:
        assert line.count("\t") == 7
        decision = line.split("\t")[1]
        assert decision in {
            "acted", "dry-run", "cap-hit", "delete-failed",
            "paged", "page-suppressed", "sweep-summary",
        }

    forged_lines = [l for l in lines if "forged" in l]
    assert len(forged_lines) == 1
    forged_fields = forged_lines[0].split("\t")
    assert "forged" in forged_fields[-1]
    for field in forged_fields[:-1]:
        assert "forged" not in field


def test_hostile_title_control_and_bidi_stripped(rig):
    hostile_title = "Poisoned\x00Title\x1b[31mRed‮Evil﻿BOM"
    item = _item(7002, hostile_title, series_id=502)
    rig.state_file.write_text(json.dumps(_aged_state_for([item])))
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory([item]))

    arrhk.cmd_unstick(dry_run=False)

    text = rig.unstick_log.read_text(encoding="utf-8")
    assert "\x00" not in text
    assert "\x1b" not in text
    assert "‮" not in text
    assert "﻿" not in text
    assert " [sanitized]" in text


# ---------------------------------------------------------------------------
# Import-failure fail-open + scope fence
# ---------------------------------------------------------------------------

def test_import_failure_of_page_dedup_fails_open(rig, capsys):
    def _boom():
        raise ImportError("simulated deploy-time import failure")

    rig.monkeypatch.setattr(arrhk, "_load_page_dedup", _boom)
    item = _item(7003, "ImportFailure.Title.WEB", series_id=503)
    rig.state_file.write_text(json.dumps(_aged_state_for([item])))
    rig.monkeypatch.setattr(arrhk, "_req", _fake_req_factory([item]))

    rc = arrhk.cmd_unstick(dry_run=False)

    assert rc == 0
    assert len(rig.notify_calls) == 1  # paged, exactly as before dedup existed
    captured = capsys.readouterr()
    assert "import failed" in captured.err


def test_classify_and_threshold_untouched():
    """Structural guard, no git dependency on a clean tree: the diff between
    HEAD and origin/master for these two symbols must be empty. Falls back to
    a same-file self-check (still meaningful — proves the symbols exist
    verbatim and untouched THIS session) if origin/master isn't reachable."""
    current_text = ARR_HOUSEKEEPING_PATH.read_text(encoding="utf-8")

    def _extract(text: str, start_marker: str, stop_markers: list[str]) -> str:
        start = text.index(start_marker)
        end = len(text)
        for m in stop_markers:
            idx = text.find(m, start + len(start_marker))
            if idx != -1:
                end = min(end, idx)
        return text[start:end]

    classify_current = _extract(
        current_text, "def _classify_stuck(", ["\ndef _state_key"])
    thresh_current = _extract(
        current_text, "THRESHOLD_HOURS_BY_MODE = {", ["\n}\n"])

    try:
        master_text = subprocess.run(
            ["git", "show", "origin/master:scripts/maint/arr-housekeeping.py"],
            cwd=ROOT, capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except Exception as exc:
        pytest.skip(f"origin/master not reachable for the scope-fence diff: {exc}")

    classify_master = _extract(
        master_text, "def _classify_stuck(", ["\ndef _state_key"])
    thresh_master = _extract(
        master_text, "THRESHOLD_HOURS_BY_MODE = {", ["\n}\n"])

    assert classify_current == classify_master, (
        "_classify_stuck must be byte-unchanged -- a separate council owns "
        "the re-grab loop"
    )
    assert thresh_current == thresh_master, (
        "THRESHOLD_HOURS_BY_MODE must be byte-unchanged"
    )
