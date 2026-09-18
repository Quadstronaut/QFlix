"""tests/unit/test_arr_unstick_page_dedup.py — arr-housekeeping.py --unstick
cross-run page dedup (council round 2, cluster C, 2026-09-17).

Covers D3 identity boundaries, the deleted title-hash fallback, the bounded
id-less fold, the cap-hit carve-out, the durable audit log (incl. hostile-
title log injection), and the import-failure fail-open path.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "maint"))

spec = importlib.util.spec_from_file_location(
    "arr_housekeeping", ROOT / "scripts" / "maint" / "arr-housekeeping.py",
)
arrhk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arrhk)

FIXTURES = ROOT / "tests" / "fixtures" / "arr-queue"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())["records"]


def _wire(tmp_path, monkeypatch):
    """Common isolation: every path this module can touch is redirected
    under tmp_path, and _notify is spied rather than firing for real."""
    state_file = tmp_path / "stuck.json"
    page_ledger = tmp_path / "unstick-pages.json"
    log_file = tmp_path / "unstick.log"
    monkeypatch.setattr(arrhk, "STATE_DIR", tmp_path)
    monkeypatch.setattr(arrhk, "STUCK_STATE_FILE", state_file)
    monkeypatch.setattr(arrhk, "UNSTICK_PAGE_LEDGER", page_ledger)
    monkeypatch.setattr(arrhk, "UNSTICK_LOG", log_file)
    notified = []
    monkeypatch.setattr(arrhk, "_notify",
                        lambda msg, level="info": notified.append((msg, level)))
    return state_file, page_ledger, log_file, notified


def _seed_state(state_file, entries: dict) -> None:
    state_file.write_text(json.dumps(entries))


def _aged_entry(slug, title, mode="stalled-no-peers"):
    return {
        "title": title, "queue_id": None,
        "first_seen_stuck": time.time() - 86400,
        "slug": slug, "mode": mode, "sizeleft_history": [],
    }


# ---------------------------------------------------------------------------
# D3 boundaries
# ---------------------------------------------------------------------------

def test_page_key_series_id_zero_is_valid():
    assert arrhk._page_key("sonarr", {"seriesId": 0}) == "unstick:sonarr:series:0"


def test_page_key_movie_id_zero_is_valid():
    assert arrhk._page_key("radarr", {"movieId": 0}) == "unstick:radarr:movie:0"


def test_page_key_no_ids_folds_to_unknown():
    assert arrhk._page_key("sonarr", {}) == "unstick:unknown-items:sonarr"


def test_page_key_none_series_id_falls_through_to_movie_id():
    assert arrhk._page_key("radarr", {"seriesId": None, "movieId": 0}) == "unstick:radarr:movie:0"


def test_page_key_bool_series_id_rejected_as_id():
    # isinstance(True, int) is True in Python — must be explicitly excluded.
    assert arrhk._page_key("sonarr", {"seriesId": False}) == "unstick:unknown-items:sonarr"
    assert arrhk._page_key("sonarr", {"seriesId": True}) == "unstick:unknown-items:sonarr"


def test_page_key_different_ids_never_collide():
    s = "sonarr"
    assert arrhk._page_key(s, {"seriesId": 0}) != arrhk._page_key(s, {"seriesId": 1})


def test_no_hashlib_import():
    src = Path(ROOT / "scripts" / "maint" / "arr-housekeeping.py").read_text(encoding="utf-8")
    assert "hashlib" not in src


# ---------------------------------------------------------------------------
# The measured storm: 11 title variants, one seriesId, must page ONCE
# ---------------------------------------------------------------------------

ELEVEN_TITLE_VARIANTS = [
    "Yellowjackets.S03E01.1080p.WEB.H264-GROUPA",
    "yellowjackets s03e01 1080p web h264-groupa",
    "Yellowjackets S03E01 Turned Out To Be True 1080p AMZN WEB-DL H264-GROUPB",
    "Yellowjackets.S03E01.PROPER.1080p.WEB.H264-GROUPA",
    "Yellowjackets.S03E01.720p.WEB.H264-GROUPC",
    "Yellowjackets.S03E01.1080p.WEB.H265-GROUPA",
    "Yellowjackets-S03E01-1080p-WEB-H264-GROUPA",
    "Yellowjackets.S03E01.PMTP.WEB.H264-GROUPD-AsRequested",
    "Yellowjackets.S03E01.WEBRip.x264-GROUPE-Scrambled",
    "Yellowjackets.S03E01.NORDiC.1080p.WEB.H264-GROUPF",
    "Yellowjackets.S03E01.1080p.WEB.H264-GROUPA-EZTV",
]


def test_same_item_under_eleven_titles_pages_once(tmp_path, monkeypatch):
    """11 separate hourly sweeps, 11 different re-grabs (distinct downloadId
    per title variant) of the SAME series (seriesId=101). Under the deleted
    title-hash fallback every one of these paged separately (the measured
    2026-09-17 storm). Under the seriesId-keyed dedup, exactly ONE pages."""
    state_file, page_ledger, log_file, notified = _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")

    base = dict(_load("peers.json")[0])
    base["seriesId"] = 101

    for i, title in enumerate(ELEVEN_TITLE_VARIANTS):
        item = dict(base)
        item["title"] = title
        item["id"] = 5000 + i
        item["downloadId"] = f"{i:040x}".upper()

        # Simulate this specific re-grab having already been tracked long
        # enough to age out THIS sweep.
        sk = arrhk._state_key("sonarr", item["downloadId"])
        _seed_state(state_file, {sk: _aged_entry("sonarr", title)})

        def fake_req(method, url, key, _item=item, **kw):
            if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
                return 200, json.dumps({"records": [_item]})
            if method == "GET":
                return 200, json.dumps({"records": []})
            if method == "DELETE":
                return 200, ""
            return 500, ""

        monkeypatch.setattr(arrhk, "_req", fake_req)
        rc = arrhk.cmd_unstick(dry_run=False)
        assert rc == 0

    assert len(notified) == 1, (
        f"expected exactly 1 notify across 11 re-grabs of the same series, "
        f"got {len(notified)}: {notified}"
    )


def test_new_series_pages_on_the_very_next_run(tmp_path, monkeypatch):
    state_file, page_ledger, log_file, notified = _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")

    base = dict(_load("peers.json")[0])

    def run_one(series_id, title, dl_hex):
        item = dict(base)
        item["seriesId"] = series_id
        item["id"] = 9000 + series_id
        item["downloadId"] = dl_hex
        item["title"] = title

        sk = arrhk._state_key("sonarr", item["downloadId"])
        prior = json.loads(state_file.read_text()) if state_file.exists() else {}
        prior[sk] = _aged_entry("sonarr", title)
        _seed_state(state_file, prior)

        def fake_req(method, url, key, _item=item, **kw):
            if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
                return 200, json.dumps({"records": [_item]})
            if method == "GET":
                return 200, json.dumps({"records": []})
            if method == "DELETE":
                return 200, ""
            return 500, ""

        monkeypatch.setattr(arrhk, "_req", fake_req)
        return arrhk.cmd_unstick(dry_run=False)

    run_one(101, "ShowOne.S01E01.GROUP", "a" * 40)
    assert len(notified) == 1

    run_one(202, "ShowTwo.S02E02.GROUP", "b" * 40)
    assert len(notified) == 2
    assert "ShowTwo" in notified[1][0]
    assert "ShowOne" not in notified[1][0]


# ---------------------------------------------------------------------------
# The fallback cannot storm
# ---------------------------------------------------------------------------

def test_id_less_rows_fold_into_one_bounded_key(tmp_path, monkeypatch):
    state_file, page_ledger, log_file, notified = _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")
    # High enough that all 50 rows actually act this run — the test is about
    # the NOTIFICATION fold, not the pre-existing per-run/per-slug caps.
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "100")
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "100")
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")

    base = dict(_load("peers.json")[0])
    items = []
    prior = {}
    for i in range(50):
        item = dict(base)
        item["id"] = 7000 + i
        item["downloadId"] = f"{i:040x}".upper()
        item["title"] = f"Unknown.Release.{i}"
        # deliberately NO seriesId/movieId
        items.append(item)
        sk = arrhk._state_key("sonarr", item["downloadId"])
        prior[sk] = _aged_entry("sonarr", item["title"])
    _seed_state(state_file, prior)

    def fake_req(method, url, key, **kw):
        if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
            return 200, json.dumps({"records": items})
        if method == "GET":
            return 200, json.dumps({"records": []})
        if method == "DELETE":
            return 200, ""
        return 500, ""

    monkeypatch.setattr(arrhk, "_req", fake_req)
    arrhk.cmd_unstick(dry_run=False)

    assert len(notified) == 1
    body = notified[0][0]
    assert "50" in body
    assert body.count("Unknown.Release.") <= 3

    ledger = json.loads(page_ledger.read_text())
    unknown_keys = [k for k in ledger if k.startswith("unstick:unknown-items:")]
    assert unknown_keys == ["unstick:unknown-items:sonarr"]


# ---------------------------------------------------------------------------
# Cap-hit carve-out
# ---------------------------------------------------------------------------

def test_cap_hit_survives_dedup_on_its_own_key(tmp_path, monkeypatch):
    """3 items act successfully but their per-item keys are pre-stamped (so
    each would be suppressed on its own); 2 more items hit the per-run cap.
    The cap-hit message must still fire, at error, reporting how many
    per-item lines it swallowed — even though every per-item key is mute."""
    state_file, page_ledger, log_file, notified = _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "3")
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "10")
    monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")

    base = dict(_load("peers.json")[0])
    items = []
    prior = {}
    for i in range(5):
        item = dict(base)
        item["seriesId"] = 300 + i
        item["id"] = 8000 + i
        item["downloadId"] = f"{i:040x}".upper()
        items.append(item)
        sk = arrhk._state_key("sonarr", item["downloadId"])
        prior[sk] = _aged_entry("sonarr", item["title"])
    _seed_state(state_file, prior)

    # Pre-stamp the per-item keys for the first 3 (the ones that WILL act
    # this run, per-run cap=3) so each is suppressed on its own.
    for i in range(3):
        arrhk._page_dedup_module().page_due(
            arrhk._page_key("sonarr", items[i]),
            ledger_path=page_ledger, cooldown_s=86400)

    def fake_req(method, url, key, **kw):
        if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
            return 200, json.dumps({"records": items})
        if method == "GET":
            return 200, json.dumps({"records": []})
        if method == "DELETE":
            return 200, ""
        return 500, ""

    monkeypatch.setattr(arrhk, "_req", fake_req)
    arrhk.cmd_unstick(dry_run=False)

    assert len(notified) == 1
    body, level = notified[0]
    assert level == "error"
    assert body.startswith("⚠ cap hit")
    assert "3" in body  # suppressed-count callout


def test_cap_hit_not_repaged_within_window_but_new_item_still_pages(tmp_path, monkeypatch):
    """Run 1: two capped-slug items, max_per_run=1 — one acts (uses the slot),
    the other cap-hits -> cap message fires (error). Run 2: the still-capped
    item is present again (cap-hit again, but suppressed within the 24h
    window) alongside a BRAND NEW series that claims the one slot and pages
    normally, at warning, with no cap block."""
    state_file, page_ledger, log_file, notified = _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "1")
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "10")
    monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")

    base = dict(_load("peers.json")[0])

    def make_item(series_id, qid, dl, title):
        it = dict(base)
        it["seriesId"] = series_id
        it["id"] = qid
        it["downloadId"] = dl
        it["title"] = title
        return it

    item_a = make_item(701, 1, "a" * 40, "ShowA.S01E01.GROUP")
    item_b = make_item(702, 2, "b" * 40, "ShowB.S01E01.GROUP")

    _seed_state(state_file, {
        arrhk._state_key("sonarr", item_a["downloadId"]): _aged_entry("sonarr", item_a["title"]),
        arrhk._state_key("sonarr", item_b["downloadId"]): _aged_entry("sonarr", item_b["title"]),
    })

    def fake_req_run1(method, url, key, **kw):
        if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
            return 200, json.dumps({"records": [item_a, item_b]})
        if method == "GET":
            return 200, json.dumps({"records": []})
        if method == "DELETE":
            return 200, ""
        return 500, ""

    monkeypatch.setattr(arrhk, "_req", fake_req_run1)
    arrhk.cmd_unstick(dry_run=False)

    assert len(notified) == 1
    assert notified[0][1] == "error"
    assert notified[0][0].startswith("⚠ cap hit")

    # Run 2: item_b is still stuck (carried forward from the cap-hit skip)
    # and caps again; item_c is a brand-new series ordered FIRST so it
    # claims the single per-run slot and pages on its own merit.
    item_c = make_item(703, 3, "c" * 40, "ShowC.S01E01.GROUP")
    prior2 = json.loads(state_file.read_text())
    prior2[arrhk._state_key("sonarr", item_c["downloadId"])] = _aged_entry("sonarr", item_c["title"])
    _seed_state(state_file, prior2)

    def fake_req_run2(method, url, key, **kw):
        if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
            return 200, json.dumps({"records": [item_c, item_b]})
        if method == "GET":
            return 200, json.dumps({"records": []})
        if method == "DELETE":
            return 200, ""
        return 500, ""

    monkeypatch.setattr(arrhk, "_req", fake_req_run2)
    arrhk.cmd_unstick(dry_run=False)

    assert len(notified) == 2, notified
    body, level = notified[1]
    assert "⚠ cap hit" not in body
    assert level == "warning"
    assert "ShowC" in body


# ---------------------------------------------------------------------------
# Suppression is a notification policy only — the durable log survives it
# ---------------------------------------------------------------------------

def test_nothing_due_means_no_notify_at_all(tmp_path, monkeypatch):
    state_file, page_ledger, log_file, notified = _wire(tmp_path, monkeypatch)

    def fake_req(method, url, key, **kw):
        return 200, json.dumps({"records": []})

    monkeypatch.setattr(arrhk, "_req", fake_req)
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k")
    arrhk.cmd_unstick(dry_run=False)
    assert notified == []


def test_suppression_still_writes_the_durable_log(tmp_path, monkeypatch):
    state_file, page_ledger, log_file, notified = _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")

    base = dict(_load("peers.json")[0])
    item = dict(base)
    item["seriesId"] = 555
    item["id"] = 1
    item["downloadId"] = "c" * 40
    sk = arrhk._state_key("sonarr", item["downloadId"])
    _seed_state(state_file, {sk: _aged_entry("sonarr", item["title"])})

    # Pre-stamp the key so this run's action is suppressed.
    arrhk._page_dedup_module().page_due(
        arrhk._page_key("sonarr", item), ledger_path=page_ledger, cooldown_s=86400)

    def fake_req(method, url, key, **kw):
        if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
            return 200, json.dumps({"records": [item]})
        if method == "GET":
            return 200, json.dumps({"records": []})
        return 200, ""

    monkeypatch.setattr(arrhk, "_req", fake_req)
    arrhk.cmd_unstick(dry_run=False)

    assert notified == []  # the one key is suppressed, nothing else happened
    lines = log_file.read_text().splitlines()
    decisions = [ln.split("\t")[1] for ln in lines]
    assert "acted" in decisions
    assert "page-suppressed" in decisions
    assert "sweep-summary" in decisions


def test_sweep_summary_written_on_no_op_run(tmp_path, monkeypatch):
    state_file, page_ledger, log_file, notified = _wire(tmp_path, monkeypatch)

    def fake_req(method, url, key, **kw):
        return 200, json.dumps({"records": []})

    monkeypatch.setattr(arrhk, "_req", fake_req)
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k")
    arrhk.cmd_unstick(dry_run=False)

    lines = log_file.read_text().splitlines()
    summary_lines = [ln for ln in lines if ln.split("\t")[1] == "sweep-summary"]
    assert len(summary_lines) == 1


# ---------------------------------------------------------------------------
# D4 — log injection
# ---------------------------------------------------------------------------

def test_hostile_title_cannot_forge_a_log_row(tmp_path, monkeypatch):
    state_file, page_ledger, log_file, notified = _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")

    # Must fit within arr-housekeeping's pre-existing title[:80] cap (a repo
    # invariant unrelated to this change) so the embedded fake row survives
    # far enough to prove the point.
    forged = "ok\n2026-09-17T00Z\tpage-suppressed\ts\tm\tk\t0\t0\tforged"
    assert len(forged) <= 80
    base = dict(_load("peers.json")[0])
    item = dict(base)
    item["seriesId"] = 999
    item["id"] = 1
    item["downloadId"] = "d" * 40
    item["title"] = forged
    sk = arrhk._state_key("sonarr", item["downloadId"])
    _seed_state(state_file, {sk: _aged_entry("sonarr", forged)})

    def fake_req(method, url, key, **kw):
        if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
            return 200, json.dumps({"records": [item]})
        if method == "GET":
            return 200, json.dumps({"records": []})
        return 200, ""

    monkeypatch.setattr(arrhk, "_req", fake_req)
    arrhk.cmd_unstick(dry_run=False)

    text = log_file.read_text()
    lines = text.splitlines()
    # Every physical line has exactly 7 tabs (8 fields).
    for ln in lines:
        assert ln.count("\t") == 7, ln
    # The forged payload is real audit content (this record legitimately
    # produces both an "acted" and a "paged" line) but on EVERY line it
    # appears, it is confined to the title (last) field only — never able
    # to forge an extra physical row or bleed into another field.
    forged_lines = [ln for ln in lines if "forged" in ln]
    assert forged_lines, "expected the forged title to appear in at least one audit line"
    for ln in forged_lines:
        fields = ln.split("\t")
        assert len(fields) == 8
        assert "forged" in fields[-1]
        assert "forged" not in "\t".join(fields[:-1])
    # No line's decision field is a decision the code didn't actually emit.
    allowed = {"acted", "dry-run", "cap-hit", "delete-failed",
               "paged", "page-suppressed", "sweep-summary"}
    for ln in lines:
        assert ln.split("\t")[1] in allowed


def test_hostile_title_control_and_bidi_stripped(tmp_path, monkeypatch):
    state_file, page_ledger, log_file, notified = _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")

    hostile = "Show\x00S01E01\x1b[31m‮﻿GROUP"
    base = dict(_load("peers.json")[0])
    item = dict(base)
    item["seriesId"] = 111
    item["id"] = 1
    item["downloadId"] = "e" * 40
    item["title"] = hostile
    sk = arrhk._state_key("sonarr", item["downloadId"])
    _seed_state(state_file, {sk: _aged_entry("sonarr", hostile)})

    def fake_req(method, url, key, **kw):
        if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
            return 200, json.dumps({"records": [item]})
        if method == "GET":
            return 200, json.dumps({"records": []})
        return 200, ""

    monkeypatch.setattr(arrhk, "_req", fake_req)
    arrhk.cmd_unstick(dry_run=False)

    text = log_file.read_text()
    assert "\x00" not in text
    assert "\x1b[31m" not in text
    assert "‮" not in text
    assert "﻿" not in text
    assert " [sanitized]" in text


def test_import_failure_of_page_dedup_fails_open(tmp_path, monkeypatch):
    state_file, page_ledger, log_file, notified = _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "sonarr" else "")
    monkeypatch.setattr(arrhk, "_page_dedup_module", lambda: None)

    base = dict(_load("peers.json")[0])
    item = dict(base)
    item["seriesId"] = 42
    item["id"] = 1
    item["downloadId"] = "f" * 40
    sk = arrhk._state_key("sonarr", item["downloadId"])
    _seed_state(state_file, {sk: _aged_entry("sonarr", item["title"])})

    def fake_req(method, url, key, **kw):
        if method == "GET" and "sonarr/" in url and "/queue" in url and "sonarr2" not in url:
            return 200, json.dumps({"records": [item]})
        if method == "GET":
            return 200, json.dumps({"records": []})
        return 200, ""

    monkeypatch.setattr(arrhk, "_req", fake_req)
    rc = arrhk.cmd_unstick(dry_run=False)

    assert rc == 0
    assert len(notified) == 1, "import failure must fail open (page as before)"


# ---------------------------------------------------------------------------
# Scope fence
# ---------------------------------------------------------------------------

def test_classify_and_threshold_untouched():
    result = subprocess.run(
        ["git", "show", "origin/master:scripts/maint/arr-housekeeping.py"],
        cwd=str(ROOT), capture_output=True, text=True, check=True,
    )
    master_src = result.stdout
    current_src = (ROOT / "scripts" / "maint" / "arr-housekeeping.py").read_text(encoding="utf-8")

    m_master = re.search(r"def _classify_stuck\(.*?\n(?=\ndef )", master_src, re.S)
    m_current = re.search(r"def _classify_stuck\(.*?\n(?=\ndef )", current_src, re.S)
    assert m_master and m_current
    assert m_master.group(0) == m_current.group(0), "_classify_stuck must be byte-unchanged"

    t_master = re.search(r"THRESHOLD_HOURS_BY_MODE = \{.*?\n\}", master_src, re.S)
    t_current = re.search(r"THRESHOLD_HOURS_BY_MODE = \{.*?\n\}", current_src, re.S)
    assert t_master and t_current
    assert t_master.group(0) == t_current.group(0), "THRESHOLD_HOURS_BY_MODE must be byte-unchanged"
