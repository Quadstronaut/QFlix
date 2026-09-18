"""arr-housekeeping's hourly unstick sweep, de-stormed.

The fault: one genuinely stuck item the *arr keeps re-grabbing produced one
Discord page per hour, forever, for one unchanged condition. The fix is a page
ledger keyed by the *arr's STABLE identity (seriesId/movieId) plus a durable
log that records what Discord no longer shows.

The two load-bearing tests here are:
  * test_same_item_under_eleven_titles_pages_once — the measured evidence that
    killed the title-hash fallback (one episode, eleven release titles);
  * test_cap_hit_survives_dedup_on_its_own_key — the escalation must outlive
    the suppressor, or the dedup swallowed the one message that mattered.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "maint" / "arr-housekeeping.py"

# Eleven release titles measured on the live box 2026-09-17 for ONE Sonarr
# episode: separator, case, episode-title presence, source, codec, group and
# suffix all vary. Synthetic show name; the SHAPE is what was measured.
ELEVEN_TITLES = [
    "Yellowjackets.S03E01.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb",
    "Yellowjackets S03E01 1080p AMZN WEB-DL DDP5 1 H 264-NTb",
    "Yellowjackets.S03E01.Its.Morning.Somewhere.1080p.AMZN.WEB-DL.H264-FLUX",
    "yellowjackets.s03e01.1080p.web.h264-successfulcrab",
    "Yellowjackets.S03E01.1080p.PMTP.WEB-DL.DDP5.1.HEVC-NTb",
    "Yellowjackets.S03E01.1080p.WEBRip.x265-RARBG",
    "Yellowjackets.S03E01.NORDiC.1080p.WEB-DL.H.264-QUARK",
    "Yellowjackets.S03E01.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb-AsRequested",
    "Yellowjackets.S03E01.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb-Scrambled",
    "Yellowjackets S03E01 1080p WEB h264-EZTV",
    "Yellowjackets.S03E01.REPACK.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb",
]


@pytest.fixture
def arrhk(monkeypatch):
    """Load arr-housekeeping AFTER conftest's autouse fixture has pointed
    MANITOBA_STATE_DIR at a per-test tmp dir — its STATE_DIR-derived constants
    are evaluated at import time."""
    spec = importlib.util.spec_from_file_location("arr_housekeeping_dedup", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ARRS", [("sonarr", "v3", "MissingEpisodeSearch")])
    monkeypatch.setattr(mod, "_arr_key", lambda slug: "fake-key")
    monkeypatch.setattr(mod, "_save_state", lambda st: None)
    return mod


class _Notify:
    """Spy standing in for the Discord post."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, msg, level="info"):
        self.calls.append((msg, level))

    @property
    def bodies(self):
        return [c[0] for c in self.calls]


def _item(qid, dlid, title, series_id=None, movie_id=None):
    row = {
        "id": qid,
        "downloadId": dlid,
        "title": title,
        "status": "queued",
        "errorMessage": "qBittorrent is downloading metadata",
        "sizeleft": 1234,
    }
    if series_id is not None:
        row["seriesId"] = series_id
    if movie_id is not None:
        row["movieId"] = movie_id
    return row


def _aged_state(rows, slug="sonarr", hours=100):
    """State that makes every row already past its grace period."""
    now = time.time()
    return {
        f"{slug}:{r['downloadId'].lower()}": {
            "title": r["title"][:80],
            "queue_id": r["id"],
            "first_seen_stuck": now - hours * 3600,
            "slug": slug,
            "mode": "metadata-stuck",
        }
        for r in rows
    }


def _sweep(mod, monkeypatch, rows, notify, state=None, delete_code=200):
    """Run one cmd_unstick pass over `rows`."""
    monkeypatch.setattr(mod, "_load_state",
                        lambda: state if state is not None else _aged_state(rows))
    monkeypatch.setattr(mod, "_notify", notify)

    def fake_req(method, url, key, payload=None):
        if method == "GET":
            return 200, json.dumps({"records": rows})
        return delete_code, ""

    monkeypatch.setattr(mod, "_req", fake_req)
    return mod.cmd_unstick(dry_run=False)


def _log_lines(mod):
    if not mod.UNSTICK_LOG.exists():
        return []
    return mod.UNSTICK_LOG.read_text(encoding="utf-8").splitlines()


def _decisions(mod):
    return [ln.split("\t")[1] for ln in _log_lines(mod)]


# ---------------------------------------------------------------------------
# D3 — identity boundaries
# ---------------------------------------------------------------------------

def test_page_key_series_zero_is_a_valid_id(arrhk):
    assert arrhk._page_key("sonarr", {"seriesId": 0}) == "unstick:sonarr:series:0"


def test_page_key_movie_zero_is_a_valid_id(arrhk):
    assert arrhk._page_key("radarr", {"movieId": 0}) == "unstick:radarr:movie:0"


def test_page_key_without_ids_folds(arrhk):
    assert arrhk._page_key("sonarr", {}) == "unstick:unknown-items:sonarr"


def test_page_key_none_series_falls_through_to_movie(arrhk):
    assert arrhk._page_key("radarr", {"seriesId": None, "movieId": 0}) \
        == "unstick:radarr:movie:0"


def test_page_key_rejects_bools(arrhk):
    """isinstance(True, int) is True in Python; `seriesId: true` is malformed
    data, not id 1, and must not be minted as an identity."""
    assert arrhk._page_key("sonarr", {"seriesId": False}) \
        == "unstick:unknown-items:sonarr"
    assert arrhk._page_key("sonarr", {"seriesId": True}) \
        == "unstick:unknown-items:sonarr"


def test_page_key_distinct_ids_never_collide(arrhk):
    assert arrhk._page_key("sonarr", {"seriesId": 0}) \
        != arrhk._page_key("sonarr", {"seriesId": 1})
    assert arrhk._page_key("sonarr", {"seriesId": 7}) \
        != arrhk._page_key("sonarr", {"movieId": 7})
    assert arrhk._page_key("sonarr", {"seriesId": 7}) \
        != arrhk._page_key("sonarr2", {"seriesId": 7})


def test_no_hashlib_import():
    """A normalized-title hash mutates on nearly every re-grab (see
    ELEVEN_TITLES) and would reproduce the exact storm being fixed."""
    assert "hashlib" not in SCRIPT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The dedup window
# ---------------------------------------------------------------------------

def test_same_item_under_eleven_titles_pages_once(arrhk, monkeypatch):
    notify = _Notify()
    for i, title in enumerate(ELEVEN_TITLES):
        rows = [_item(1000 + i, f"HASH{i:02d}", title, series_id=42)]
        _sweep(arrhk, monkeypatch, rows, notify)
    # Eleven real unstick actions, ONE page. (Without the acted-count check
    # this test would also pass if the sweep had silently done nothing.)
    assert _decisions(arrhk).count("acted") == 11
    assert _decisions(arrhk).count("page-suppressed") == 10
    assert len(notify.calls) == 1, notify.bodies
    assert json.loads(arrhk.UNSTICK_PAGE_LEDGER.read_text(encoding="utf-8")) \
        .keys() == {"unstick:sonarr:series:42"}


def test_new_series_pages_on_the_very_next_run(arrhk, monkeypatch):
    notify = _Notify()
    first = [_item(1, "H1", "Show.A.S01E01", series_id=101)]
    _sweep(arrhk, monkeypatch, first, notify)
    assert len(notify.calls) == 1
    assert "101" in notify.bodies[0] or "Show.A" in notify.bodies[0]

    second = first + [_item(2, "H2", "Show.B.S01E01", series_id=202)]
    _sweep(arrhk, monkeypatch, second, notify)
    assert len(notify.calls) == 2
    body = notify.bodies[1]
    assert "Show.B" in body
    assert "Show.A" not in body


def test_id_less_rows_fold_into_one_bounded_key(arrhk, monkeypatch):
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "500")
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "500")
    rows = [_item(i, f"NOID{i:03d}", f"Orphan.Row.{i}") for i in range(50)]
    notify = _Notify()
    _sweep(arrhk, monkeypatch, rows, notify)

    assert len(notify.calls) == 1
    body = notify.bodies[0]
    fold = [ln for ln in body.splitlines() if "id-less queue row" in ln]
    assert len(fold) == 1
    assert "50" in fold[0]
    assert fold[0].count("Orphan.Row.") <= 3

    ledger = json.loads(arrhk.UNSTICK_PAGE_LEDGER.read_text(encoding="utf-8"))
    assert list(ledger) == ["unstick:unknown-items:sonarr"]


def test_nothing_due_means_no_notify_at_all(arrhk, monkeypatch):
    arrhk.UNSTICK_PAGE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    arrhk.UNSTICK_PAGE_LEDGER.write_text(
        json.dumps({"unstick:sonarr:series:42": time.time()}), encoding="utf-8")
    notify = _Notify()
    _sweep(arrhk, monkeypatch,
           [_item(1, "H1", "Show.S01E01", series_id=42)], notify)
    assert notify.calls == []


# ---------------------------------------------------------------------------
# Escalation survives dedup
# ---------------------------------------------------------------------------

def test_cap_hit_survives_dedup_on_its_own_key(arrhk, monkeypatch):
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "1")
    arrhk.UNSTICK_PAGE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    arrhk.UNSTICK_PAGE_LEDGER.write_text(
        json.dumps({"unstick:sonarr:series:42": time.time()}), encoding="utf-8")

    rows = [_item(1, "H1", "Show.S01E01", series_id=42),
            _item(2, "H2", "Show.S01E02", series_id=42)]
    notify = _Notify()
    _sweep(arrhk, monkeypatch, rows, notify)

    assert len(notify.calls) == 1
    body, level = notify.calls[0]
    assert level == "error"
    assert body.startswith("⚠ cap hit")
    assert "1 per-item line(s) suppressed" in body
    assert "Show.S01E01" not in body


def test_cap_hit_not_repaged_within_window_but_new_item_still_pages(
        arrhk, monkeypatch):
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "1")
    notify = _Notify()

    run1 = [_item(1, "H1", "Show.S01E01", series_id=42),
            _item(2, "H2", "Show.S01E02", series_id=42)]
    _sweep(arrhk, monkeypatch, run1, notify)
    assert notify.calls[0][1] == "error"
    assert "⚠ cap hit" in notify.calls[0][0]

    # Same cap hit inside the window: no cap block. A brand-new seriesId in
    # the same run is a key the ledger has never seen, so it still pages.
    run2 = [_item(3, "H3", "Fresh.S01E01", series_id=999),
            _item(4, "H4", "Show.S01E03", series_id=42)]
    _sweep(arrhk, monkeypatch, run2, notify)
    assert len(notify.calls) == 2
    body, level = notify.calls[1]
    assert "⚠ cap hit" not in body
    assert level == "warning"
    assert "Fresh.S01E01" in body


# ---------------------------------------------------------------------------
# The durable log
# ---------------------------------------------------------------------------

def test_suppression_still_writes_the_durable_log(arrhk, monkeypatch):
    arrhk.UNSTICK_PAGE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    arrhk.UNSTICK_PAGE_LEDGER.write_text(
        json.dumps({"unstick:sonarr:series:42": time.time()}), encoding="utf-8")
    notify = _Notify()
    _sweep(arrhk, monkeypatch,
           [_item(1, "H1", "Show.S01E01", series_id=42)], notify)

    assert notify.calls == []
    decisions = _decisions(arrhk)
    assert decisions.count("acted") == 1
    assert decisions.count("page-suppressed") == 1
    assert decisions.count("sweep-summary") == 1
    # The suppressed run is fully reconstructible: the title is on the acted
    # line even though Discord never saw it.
    assert "Show.S01E01" in arrhk.UNSTICK_LOG.read_text(encoding="utf-8")


def test_sweep_summary_written_on_no_op_run(arrhk, monkeypatch):
    notify = _Notify()
    _sweep(arrhk, monkeypatch, [], notify, state={})
    lines = _log_lines(arrhk)
    assert len(lines) == 1
    assert lines[0].split("\t")[1] == "sweep-summary"
    assert notify.calls == []


def test_every_log_line_has_exactly_seven_tabs(arrhk, monkeypatch):
    notify = _Notify()
    _sweep(arrhk, monkeypatch,
           [_item(1, "H1", "Show.S01E01", series_id=42)], notify)
    lines = _log_lines(arrhk)
    assert lines
    for ln in lines:
        assert ln.count("\t") == 7, ln


# ---------------------------------------------------------------------------
# D4 — log injection
# ---------------------------------------------------------------------------

HOSTILE = ("ok\n2026-09-17T00:00:00Z\tpage-suppressed\tsonarr\tmetadata-stuck"
           "\tunstick:sonarr:series:9\t0\t0\tforged")


def test_hostile_title_cannot_forge_a_log_row(arrhk, monkeypatch):
    notify = _Notify()
    _sweep(arrhk, monkeypatch,
           [_item(1, "H1", HOSTILE, series_id=42)], notify)

    lines = _log_lines(arrhk)
    # acted + the per-key paged decision + sweep-summary. Nothing else.
    assert len(lines) == 3, lines
    for ln in lines:
        assert ln.count("\t") == 7, ln
    forged = [ln for ln in lines if "forged" in ln]
    assert len(forged) == 1
    fields = forged[0].split("\t")
    assert "forged" in fields[7]
    assert all("forged" not in f for f in fields[:7])
    # No line claims a decision the code did not emit for its own record.
    assert _decisions(arrhk) == ["acted", "paged", "sweep-summary"]


def test_hostile_title_control_and_bidi_stripped(arrhk, monkeypatch):
    nasty = "Show\x00S01\x1b[31mE01‮evil﻿​"
    notify = _Notify()
    _sweep(arrhk, monkeypatch,
           [_item(1, "H1", nasty, series_id=42)], notify)
    text = arrhk.UNSTICK_LOG.read_text(encoding="utf-8")
    for ch in ("\x00", "\x1b", "‮", "﻿", "​"):
        assert ch not in text
    assert " [sanitized]" in text


# ---------------------------------------------------------------------------
# Fail-open + scope fence
# ---------------------------------------------------------------------------

def test_import_failure_of_page_dedup_fails_open(arrhk, monkeypatch, capsys):
    def boom():
        raise ImportError("no module named lib.page_dedup")

    monkeypatch.setattr(arrhk, "_page_dedup", boom)
    notify = _Notify()
    _sweep(arrhk, monkeypatch,
           [_item(1, "H1", "Show.S01E01", series_id=42)], notify)
    # Paged as before the change — a deploy-time import error degrades to
    # noise, never to silence and never to a crashed sweep.
    assert len(notify.calls) == 1
    assert "unavailable, paging anyway" in capsys.readouterr().err


def _master_source() -> str:
    return subprocess.run(
        ["git", "show", "origin/master:scripts/maint/arr-housekeeping.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout


def _slice(text: str, start: str, stop: str) -> str:
    i = text.index(start)
    j = text.index(stop, i + 1)
    return text[i:j]


def test_classify_and_threshold_untouched():
    """Scope fence: a separate council owns the re-grab loop. Only the paging
    changes here."""
    mine = SCRIPT.read_text(encoding="utf-8")
    theirs = _master_source()
    for start, stop in (
        ("def _classify_stuck(", "def _state_key("),
        ("THRESHOLD_HOURS_BY_MODE = {", "# (slug, api version"),
    ):
        assert _slice(mine, start, stop) == _slice(theirs, start, stop), start
