"""tests/unit/test_arr_housekeeping_page_dedup.py — Stage-0 Cluster C
(alert hygiene, 2026-09-17): cross-run page dedup for arr-housekeeping.py's
--unstick sweep.

Root cause measured 2026-09-16/17: 12/13 Discord messages from cmd_unstick
in 24h, one per hourly run that took any action, because the re-grab loop
mints a fresh downloadId on every grab and nothing collapsed repeats of the
SAME underlying fault. These tests replay that shape through cmd_unstick
with lib.page_ledger wired in and assert the storm collapses.

Synthetic titles/ids throughout — no real host, member data, or secret
(AC-23).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "maint"))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "arr_housekeeping_dedup",
    ROOT / "scripts" / "maint" / "arr-housekeeping.py",
)
arrhk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arrhk)


def _peers_item(qid: int, download_id: str, *, movie_id=None, series_id=None,
                 episode_ids=None, title=None) -> dict:
    """A stalled-no-peers-shaped queue record — see _classify_stuck."""
    item = {
        "id": qid,
        "title": title or f"Synthetic.Movie.{qid}.1080p.WEB-GRP",
        "status": "warning",
        "trackedDownloadState": "downloading",
        "errorMessage": "The download is stalled with no connections",
        "downloadId": download_id,
        "size": 1000, "sizeleft": 1000,
    }
    if movie_id is not None:
        item["movieId"] = movie_id
    if series_id is not None:
        item["seriesId"] = series_id
    if episode_ids is not None:
        item["episodeIds"] = episode_ids
    return item


def _metadata_item(qid: int, download_id: str, *, movie_id=None) -> dict:
    """A metadata-stuck-shaped queue record — a different mode, same content
    identity when movie_id matches a _peers_item's."""
    item = {
        "id": qid,
        "title": f"Synthetic.Movie.{qid}.1080p.WEB-GRP",
        "status": "queued",
        "trackedDownloadState": "queued",
        "errorMessage": "downloading metadata",
        "downloadId": download_id,
        "size": 1000, "sizeleft": 1000,
    }
    if movie_id is not None:
        item["movieId"] = movie_id
    return item


def _seed_aged_state(state_file: Path, slug: str, items: list) -> None:
    """Pre-populate stuck-queue-state.json so every given item is already
    past its grace period on the very next cmd_unstick call — the same
    technique tests/unit/test_arr_housekeeping.py's cap tests use."""
    seeded = {}
    for it in items:
        sk = arrhk._state_key(slug, it["downloadId"])
        seeded[sk] = {
            "title": it["title"], "queue_id": it["id"],
            "first_seen_stuck": time.time() - 86400,
            "slug": slug, "mode": "stalled-no-peers",
            "sizeleft_history": [],
        }
    state_file.write_text(json.dumps(seeded))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state_file = tmp_path / "stuck.json"
    monkeypatch.setattr(arrhk, "STATE_DIR", tmp_path)
    monkeypatch.setattr(arrhk, "STUCK_STATE_FILE", state_file)
    monkeypatch.setattr(arrhk, "_arr_key", lambda slug: "k" if slug == "radarr" else "")
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "10")
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "10")

    notifies: list[tuple[str, str]] = []
    monkeypatch.setattr(arrhk, "_notify",
                        lambda msg, level="info": notifies.append((level, msg)))

    def run(items: list, *, cap_per_run=None, cap_per_slug=None):
        _seed_aged_state(state_file, "radarr", items)
        if cap_per_run is not None:
            monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", str(cap_per_run))
        if cap_per_slug is not None:
            monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", str(cap_per_slug))

        def fake_req(method, url, key, __items=items, **kw):
            if method == "GET" and "radarr/" in url and "/queue" in url and "radarr2" not in url:
                return 200, json.dumps({"records": __items})
            if method == "GET":
                return 200, json.dumps({"records": []})
            return 500, ""
        monkeypatch.setattr(arrhk, "_req", fake_req)
        arrhk.cmd_unstick(dry_run=True)

    return {"tmp_path": tmp_path, "state_file": state_file,
            "notifies": notifies, "run": run}


# ---------------------------------------------------------------------------
# AC-3: key choice is re-grab-proof
# ---------------------------------------------------------------------------

class TestKeyChoice:

    def test_page_key_unchanged_across_downloadid_mutation_movie(self):
        a = _peers_item(1, "AAAA", movie_id=42)
        b = _peers_item(2, "BBBB", movie_id=42)
        assert arrhk._page_key("radarr", a, "stalled-no-peers") == \
               arrhk._page_key("radarr", b, "stalled-no-peers")

    def test_page_key_unchanged_across_downloadid_mutation_tv(self):
        a = _peers_item(1, "AAAA", series_id=7, episode_ids=[101, 102])
        b = _peers_item(2, "BBBB", series_id=7, episode_ids=[102, 101])  # order-insens.
        assert arrhk._page_key("sonarr", a, "stalled-no-peers") == \
               arrhk._page_key("sonarr", b, "stalled-no-peers")

    def test_new_content_id_yields_a_new_key(self):
        a = _peers_item(1, "AAAA", movie_id=42)
        b = _peers_item(2, "BBBB", movie_id=43)
        assert arrhk._page_key("radarr", a, "stalled-no-peers") != \
               arrhk._page_key("radarr", b, "stalled-no-peers")

    def test_downloadid_alone_is_not_the_key(self):
        """A pure downloadId-keyed ledger would dedup nothing across
        re-grabs. Assert the key does NOT embed the downloadId at all."""
        item = _peers_item(1, "DEADBEEF00", movie_id=42)
        pk = arrhk._page_key("radarr", item, "stalled-no-peers")
        assert "deadbeef" not in pk.lower()

    def test_new_content_pages_immediately_while_old_content_stays_muted(self, harness):
        harness["run"]([_peers_item(1, "AAA0", movie_id=777)])
        harness["run"]([
            _peers_item(2, "BBB0", movie_id=777),   # same content, re-grabbed -> muted
            _peers_item(3, "CCC0", movie_id=888),   # genuinely new title -> due
        ])
        warnings = [m for lvl, m in harness["notifies"] if lvl == "warning"]
        assert len(warnings) == 2, warnings
        # First page: run 1's item (qid=1).
        assert "Synthetic.Movie.1." in warnings[0]
        # Second page: only the NEW content (qid=3) is due; the re-grab of
        # the already-paged content (qid=2) must be muted out of the body.
        assert "Synthetic.Movie.3." in warnings[1]
        assert "Synthetic.Movie.2." not in warnings[1]


# ---------------------------------------------------------------------------
# AC-4: mode is part of the key
# ---------------------------------------------------------------------------

class TestModeInKey:

    def test_page_key_differs_by_mode_for_same_content(self):
        item = _peers_item(1, "AAAA", movie_id=42)
        k1 = arrhk._page_key("radarr", item, "stalled-no-peers")
        k2 = arrhk._page_key("radarr", item, "slow-cluster")
        assert k1 != k2

    def test_mode_transition_pages_once_per_mode(self, harness):
        harness["run"]([_peers_item(1, "AAA0", movie_id=555)])
        harness["run"]([_metadata_item(2, "BBB0", movie_id=555)])
        # Same content (movieId=555), two DIFFERENT modes -> two pages.
        warnings = [m for lvl, m in harness["notifies"] if lvl == "warning"]
        assert len(warnings) == 2, warnings


# ---------------------------------------------------------------------------
# AC-2: storm collapse
# ---------------------------------------------------------------------------

def test_storm_of_24_hourly_runs_collapses_to_one_warning_and_one_error(harness):
    """Replays the measured shape: 24 hourly runs, one fault (fixed content
    identity, downloadId changing every run because each run's grab is
    pre-aged into "just deleted+re-grabbed"), one of which also hits the
    action cap. Expect EXACTLY 1 warning + 1 error over the whole sequence."""
    for run_n in range(24):
        cap = 0 if run_n == 23 else 10
        harness["run"]([_peers_item(9000 + run_n, f"{run_n:040x}".upper(), movie_id=777)],
                       cap_per_run=cap, cap_per_slug=5)

    warnings = [m for lvl, m in harness["notifies"] if lvl == "warning"]
    errors = [m for lvl, m in harness["notifies"] if lvl == "error"]
    assert len(warnings) == 1, warnings
    assert len(errors) == 1, errors
    assert "SYSTEMIC" in errors[0]


# ---------------------------------------------------------------------------
# AC-6, AC-7, AC-8: escalation carve-out
# ---------------------------------------------------------------------------

class TestEscalationCarveOut:

    def test_cap_hit_pages_separately_even_with_every_item_key_muted(self, harness):
        # Prime + mute one content item.
        harness["run"]([_peers_item(1, "AAA0", movie_id=1)])
        assert len([m for lvl, m in harness["notifies"] if lvl == "warning"]) == 1

        # Run 2: the SAME (now-muted) item, plus a second item that trips the
        # cap. cap_per_run=1 -> item A consumes the one slot (muted, since
        # its key already paged), item B hits the cap outright.
        harness["run"](
            [_peers_item(2, "BBB0", movie_id=1), _peers_item(3, "CCC0", movie_id=2)],
            cap_per_run=1, cap_per_slug=1,
        )
        new_calls = harness["notifies"][1:]
        assert len(new_calls) == 1, new_calls
        level, msg = new_calls[0]
        assert level == "error"
        assert msg.startswith("⚠ SYSTEMIC — arr-unstick CAP HIT")
        assert "arr-unstick swept:" not in msg, "cap text must never be concatenated onto the sweep body"

    def test_routine_body_never_carries_error_level(self, harness):
        harness["run"]([_peers_item(1, "AAA0", movie_id=1)])
        for level, _ in harness["notifies"]:
            assert level != "error"

    def test_cap_hit_again_within_window_does_not_repage(self, harness):
        harness["run"]([_peers_item(1, "AAA0", movie_id=1)], cap_per_run=0, cap_per_slug=0)
        harness["run"]([_peers_item(2, "BBB0", movie_id=1)], cap_per_run=0, cap_per_slug=0)
        errors = [m for lvl, m in harness["notifies"] if lvl == "error"]
        assert len(errors) == 1, errors

    def test_clean_run_clears_the_cap_stamp_so_next_cap_hit_pages_immediately(self, harness):
        harness["run"]([_peers_item(1, "AAA0", movie_id=1)], cap_per_run=0, cap_per_slug=0)
        assert len([m for lvl, m in harness["notifies"] if lvl == "error"]) == 1

        # A clean run: actions taken, cap NOT hit.
        harness["run"]([_peers_item(2, "BBB0", movie_id=2)], cap_per_run=10, cap_per_slug=10)

        # Next cap-hit: must page again immediately (per-OUTAGE cooldown).
        harness["run"]([_peers_item(3, "CCC0", movie_id=3)], cap_per_run=0, cap_per_slug=0)
        errors = [m for lvl, m in harness["notifies"] if lvl == "error"]
        assert len(errors) == 2, errors

    def test_cap_still_hit_line_appears_when_routine_page_is_also_sent(self, harness):
        # First cap-hit pages and stamps the cap key.
        harness["run"]([_peers_item(1, "AAA0", movie_id=1)], cap_per_run=0, cap_per_slug=0)
        assert len([m for lvl, m in harness["notifies"] if lvl == "error"]) == 1

        # Second run: new content (pages routine) + cap hit again (muted,
        # within cooldown) via a third item that overflows cap_per_run=2.
        harness["run"](
            [_peers_item(2, "BBB0", movie_id=2),   # new -> due
             _peers_item(3, "CCC0", movie_id=1),   # re-grab of muted content #1
             _peers_item(4, "DDD0", movie_id=3)],  # overflows the cap
            cap_per_run=2, cap_per_slug=2,
        )
        new_calls = harness["notifies"][1:]
        warnings = [m for lvl, m in new_calls if lvl == "warning"]
        errors = [m for lvl, m in new_calls if lvl == "error"]
        assert len(warnings) == 1, new_calls
        assert len(errors) == 0, "the cap is still within its own cooldown -- no second error"
        assert warnings[0].count("cap still hit") == 1


# ---------------------------------------------------------------------------
# AC-9, AC-10, AC-11: silence / logging fidelity / footer accounting
# ---------------------------------------------------------------------------

class TestNotificationVsLogging:

    def test_silence_when_everything_is_muted_and_no_cap_hit(self, harness):
        harness["run"]([_peers_item(1, "AAA0", movie_id=1)])
        assert len(harness["notifies"]) == 1

        harness["run"]([_peers_item(2, "BBB0", movie_id=1)])  # re-grab, still muted
        assert len(harness["notifies"]) == 1, "a fully-muted, non-cap run must send nothing"

    def test_muted_run_still_writes_every_action_and_verdict_to_the_log(self, harness):
        harness["run"]([_peers_item(1, "AAA0", movie_id=1)])
        harness["run"]([_peers_item(2, "BBB0", movie_id=1)])  # muted

        log_path = harness["tmp_path"] / arrhk.UNSTICK_LOG_FILE
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2, lines  # one 'due' row (run1), one 'muted' row (run2)
        assert "\tdue\t" in lines[0]
        assert "\tmuted\t" in lines[1]

    def test_footer_accounting(self, harness):
        harness["run"]([_peers_item(1, "AAA0", movie_id=1)])  # pages, mutes key 1

        harness["run"]([
            _peers_item(2, "BBB0", movie_id=2),  # new -> due
            _peers_item(3, "CCC0", movie_id=1),  # re-grab of muted content -> muted
        ])
        new_calls = harness["notifies"][1:]
        assert len(new_calls) == 1
        _, body = new_calls[0]
        assert body.count("ongoing condition(s) already paged") == 1
        assert "(+1 ongoing condition(s)" in body
        # The due line (item id=2, movieId=2) appears; the muted line's
        # (item id=3, movieId=1's re-grab) title text must not leak in.
        assert "Synthetic.Movie.2." in body
        assert "Synthetic.Movie.3." not in body


# ---------------------------------------------------------------------------
# AC-12: fail-open propagates through cmd_unstick itself
# ---------------------------------------------------------------------------

def test_cmd_unstick_still_pages_when_the_ledger_is_corrupt(harness):
    ledger_path = harness["tmp_path"] / arrhk.UNSTICK_PAGE_LEDGER
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("{not valid json")

    harness["run"]([_peers_item(1, "AAA0", movie_id=1)])

    warnings = [m for lvl, m in harness["notifies"] if lvl == "warning"]
    assert len(warnings) == 1, "a corrupt ledger must fail OPEN, never swallow the page"


def test_cmd_unstick_never_raises_when_state_dir_is_unwritable(harness, monkeypatch):
    import lib.page_ledger as page_ledger_mod

    def _boom(*a, **kw):
        raise OSError("simulated unwritable state dir")
    monkeypatch.setattr(page_ledger_mod, "_write", _boom)

    # Must not raise.
    harness["run"]([_peers_item(1, "AAA0", movie_id=1)])
    warnings = [m for lvl, m in harness["notifies"] if lvl == "warning"]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# AC-13: prune runs on every cmd_unstick call
# ---------------------------------------------------------------------------

def test_prune_runs_on_every_call_even_with_no_stuck_items(harness):
    ledger_path = harness["tmp_path"] / arrhk.UNSTICK_PAGE_LEDGER
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    expired = {f"unstick:stale-{i}": time.time() - (86400 * 2) for i in range(50)}
    ledger_path.write_text(json.dumps(expired))

    harness["run"]([])  # no stuck items this run at all

    remaining = json.loads(ledger_path.read_text())
    assert remaining == {}, "prune must run even on a run with zero stuck items"
