"""The arr re-grab loop guard: poison detection, the park, and the ledger.

WHAT THIS IS DEFENDING
----------------------
`arr-housekeeping.py --unstick` blocklists a stuck release and the *arr grabs
the next one. For a title with no good release that is a closed loop, and the
sweep could not see it because its only memory was the qBittorrent hash — and
every re-grab is a new hash. Seven measured days: 200 blocklist adds across 54
episodes, worst episode 13, top 8 = 81.

The fix has two halves and this file pins both:

  1. PARK IS AN UNMONITOR WRITE, NOT A QUERY PARAMETER. `skipRedownload=true`
     was measured NOT to stop the replacement grab on this box (16 seconds,
     remediate-2026-08-20-iso.py). An unmonitored episode cannot be
     auto-searched at all. The write is issued BEFORE the destructive DELETE
     and gated on its return code, because a delete with a failed park behind
     it feeds the very loop being guarded.
  2. THE LEDGER FAILS OPEN, ALWAYS. Corrupt, truncated, wrong-shaped,
     unwritable — every one of them degrades this build to exactly the
     pre-guard sweep. The only thing a bad ledger may do is decline to park.

Every test below is named for the invariant it carries.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "maint"))

spec = importlib.util.spec_from_file_location(
    "arr_housekeeping", ROOT / "scripts" / "maint" / "arr-housekeeping.py")
arrhk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arrhk)

from lib import regrab_ledger as rl  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "arr-queue"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["records"]


def _by_dl(records: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in records:
        out.setdefault((r.get("downloadId") or "").upper(), []).append(r)
    return out


# ---------------------------------------------------------------------------
# A sweep harness: records in, HTTP calls out.
# ---------------------------------------------------------------------------

class Sweep:
    """One cmd_unstick run against fixture records, with every request
    recorded and every response programmable."""

    def __init__(self, tmp_path: Path, monkeypatch, records, *,
                 slug="sonarr", prior_state=None, delete_code=200,
                 park_code=200, get_detail=None):
        self.records = records
        self.slug = slug
        self.delete_code = delete_code
        self.park_code = park_code
        self.get_detail = get_detail or {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.notifications: list[tuple[str, str]] = []
        self.state_file = tmp_path / "stuck.json"
        if prior_state is not None:
            self.state_file.write_text(json.dumps(prior_state), encoding="utf-8")
        monkeypatch.setattr(arrhk, "STATE_DIR", tmp_path)
        monkeypatch.setattr(arrhk, "STUCK_STATE_FILE", self.state_file)
        monkeypatch.setattr(arrhk, "_req", self._req)
        monkeypatch.setattr(arrhk, "_arr_key",
                            lambda s: "k" if s == self.slug else "")
        monkeypatch.setattr(arrhk, "_notify",
                            lambda m, level="info": self.notifications.append((level, m)))
        self.ledger_path = tmp_path / "arr-regrab-ledger.json"

    def _req(self, method, url, key, body=None, timeout=30):
        self.calls.append((method, url, body))
        if method == "GET" and "/queue" in url:
            if f"/{self.slug}/" in url:
                return 200, json.dumps({"records": self.records})
            return 200, json.dumps({"records": []})
        if method == "GET":
            for path, resp in self.get_detail.items():
                if path in url:
                    return resp
            return 404, "{}"
        if method == "PUT":
            return self.park_code, "{}"
        if method == "DELETE":
            return self.delete_code, ""
        return 500, ""

    # -- convenience views ---------------------------------------------
    @property
    def deletes(self):
        return [c for c in self.calls if c[0] == "DELETE"]

    @property
    def puts(self):
        return [c for c in self.calls if c[0] == "PUT"]

    def run(self, dry_run=False):
        return arrhk.cmd_unstick(dry_run=dry_run)

    def ledger(self) -> dict:
        if not self.ledger_path.exists():
            return {}
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))


def _aged_state(records, slug="sonarr", mode="stalled-no-peers", age_s=86400):
    now = time.time()
    return {
        arrhk._state_key(slug, r.get("downloadId", "")): {
            "title": r.get("title", "?"), "queue_id": r.get("id"),
            "first_seen_stuck": now - age_s, "slug": slug, "mode": mode,
            "sizeleft_history": [],
        }
        for r in records
    }


# ===========================================================================
# INV-1/2/3 + AC-6 — poison detection
# ===========================================================================

def test_inv1_poison_is_a_strict_subset_of_import():
    """Poison may only ACCELERATE an item MODE_IMPORT already owned. It can
    never make a previously-healthy item classifiable."""
    poison_row = _load("poison.json")[0]
    assert arrhk._classify_stuck(poison_row, {}) == arrhk.MODE_POISON
    # Same row, but no longer in an import-stuck state: the poison text is
    # still there and must buy it exactly nothing.
    for status, tds in (("downloading", "downloading"),
                        ("completed", "imported"),
                        ("queued", "downloading")):
        row = dict(poison_row, status=status, trackedDownloadState=tds)
        assert arrhk._classify_stuck(row, {}) is None, (status, tds)


def test_inv2_title_containing_exe_is_not_poison():
    """The release TITLE is not an input. Only statusMessages."""
    row = _load("poison.json")[1]
    assert ".exe." in row["title"]
    assert arrhk._is_poison_payload(row) is False
    assert arrhk._classify_stuck(row, {}) == arrhk.MODE_IMPORT


def test_inv3_phrase_and_extension_must_share_one_string():
    """Phrase in one message, a bare .exe in another — not a match."""
    row = _load("poison.json")[2]
    texts = arrhk._status_message_texts(row)
    assert any("executable" in t for t in texts)
    assert any(".exe" in t for t in texts)
    assert arrhk._is_poison_payload(row) is False
    assert arrhk._classify_stuck(row, {}) == arrhk.MODE_IMPORT


def test_extension_token_does_not_match_inside_a_longer_extension():
    """`.js` must not fire on `.json`. A metadata sidecar is not malware."""
    row = _load("poison.json")[3]
    assert arrhk._is_poison_payload(row) is False
    assert arrhk._has_ext_token("found: .json", ".js") is False
    assert arrhk._has_ext_token("found: .js", ".js") is True
    assert arrhk._has_ext_token("found: .js in the archive", ".js") is True


@pytest.mark.parametrize("payload", [
    None, [], {}, "a string", [None, 3, True],
    [{"title": None, "messages": None}],
    [{"messages": "bare string"}],
    ["already flat"],
    [{"no": "known keys"}],
])
def test_status_message_texts_never_raises_on_any_shape(payload):
    item = {"statusMessages": payload}
    out = arrhk._status_message_texts(item)
    assert isinstance(out, list)
    assert all(isinstance(s, str) for s in out)
    assert arrhk._is_poison_payload(item) in (True, False)


def test_status_messages_key_absent_is_not_poison():
    assert arrhk._status_message_texts({}) == []
    assert arrhk._is_poison_payload({"status": "completed"}) is False


def test_ac6_poison_threshold_is_zero_hours():
    assert arrhk.THRESHOLD_HOURS_BY_MODE[arrhk.MODE_POISON] == 0.0


def test_ac6_poison_acts_on_first_sight_and_the_message_names_why(
        tmp_path, monkeypatch):
    """No prior state at all: the 6h grace is bypassed and the notification
    says poison-executable-payload, so the operator reads WHY."""
    sw = Sweep(tmp_path, monkeypatch, [_load("poison.json")[0]])
    assert sw.run() == 0
    assert len(sw.deletes) == 1
    body = "\n".join(m for _, m in sw.notifications)
    assert arrhk.MODE_POISON in body
    assert "blocklisted+removed" in body


def test_import_mode_still_waits_its_full_grace(tmp_path, monkeypatch):
    """The negative control for the test above: a non-poison import stall on
    first sight is carried forward, not acted on."""
    sw = Sweep(tmp_path, monkeypatch, [_load("poison.json")[1]])
    assert sw.run() == 0
    assert sw.deletes == []
    assert json.loads(sw.state_file.read_text())  # carried forward


# ===========================================================================
# AC-7 / INV-10 — keying
# ===========================================================================

def test_ac7_season_pack_rows_collapse_to_one_key():
    records = _load("regrab.json")
    pack = [r for r in records if r["id"] in (822001, 822002)]
    by_dl = _by_dl(records)
    k1 = arrhk._guard_key("sonarr", pack[0], by_dl)
    k2 = arrhk._guard_key("sonarr", pack[1], by_dl)
    assert k1 == k2 == "sonarr|1234|5678,5679"


def test_inv10_distinct_episodes_of_one_series_are_independent():
    records = _load("regrab.json")
    by_dl = _by_dl(records)
    pack_key = arrhk._guard_key("sonarr", records[0], by_dl)
    solo_key = arrhk._guard_key("sonarr", records[2], by_dl)
    assert pack_key != solo_key
    led: dict = {}
    now = time.time()
    for _ in range(3):
        rl.record_blocklist_add(led, pack_key, "pack", now)
    assert rl.adds_in_window(led, pack_key, now) == 3
    assert rl.adds_in_window(led, solo_key, now) == 0
    assert rl.should_park(led, pack_key, now) is True
    assert rl.should_park(led, solo_key, now) is False


def test_movie_rows_key_on_movieid():
    records = _load("regrab.json")
    movie = [r for r in records if r["id"] == 822004][0]
    assert arrhk._guard_key("radarr", movie, _by_dl(records)) == "radarr|441"
    assert rl.is_movie_key("radarr|441") is True
    assert rl.is_movie_key("sonarr|1|2") is False


def test_inv7_unkeyable_rows_are_none_never_guessed():
    records = _load("regrab.json")
    unknown = [r for r in records if r["id"] == 822005][0]
    assert arrhk._guard_key("sonarr", unknown, _by_dl(records)) is None
    assert arrhk._guard_key("radarr", unknown, _by_dl(records)) is None
    assert rl.episode_key("sonarr", 0, [1]) is None
    assert rl.episode_key("sonarr", 1, []) is None
    assert rl.episode_key("sonarr", 1, [None, "x", False]) is None
    assert rl.movie_key("radarr", None) is None


def test_episode_identity_is_read_from_all_three_shapes():
    """RTFM (a): Sonarr v3 answers `episodeId`. `episodeIds` and `episodes[]`
    are accepted too so a resource-shape change cannot silently make every
    row unkeyable — which would disarm the guard without a single error."""
    by_dl: dict = {}
    assert arrhk._episode_ids_for({"episodeId": 7}, by_dl) == [7]
    assert arrhk._episode_ids_for({"episodeIds": [7, 8]}, by_dl) == [7, 8]
    assert arrhk._episode_ids_for({"episodes": [{"id": 9}, {"id": 8}]}, by_dl) == [8, 9]
    assert arrhk._episode_ids_for({"episodeId": True}, by_dl) == []


def test_inv7_unkeyable_count_reaches_stdout(tmp_path, monkeypatch, capsys):
    unknown = [r for r in _load("regrab.json") if r["id"] == 822005]
    sw = Sweep(tmp_path, monkeypatch, unknown,
               prior_state=_aged_state(unknown))
    sw.run()
    out = capsys.readouterr().out
    assert "unkeyable=1" in out


# ===========================================================================
# AC-7 durability — three sweeps, three different hashes, one park
# ===========================================================================

def _regrab_sweep(tmp_path, monkeypatch, hash_suffix, prior_state=None,
                  park_code=200, delete_code=200, detail=None):
    """One stalled episode of one series, under a NEW download hash each
    time — which is exactly what a re-grab looks like from the queue."""
    row = dict(_load("regrab.json")[2],
               downloadId=f"FEED{hash_suffix}FEED{hash_suffix}", id=900 + hash_suffix)
    return Sweep(tmp_path, monkeypatch, [row],
                 prior_state=prior_state or _aged_state([row]),
                 park_code=park_code, delete_code=delete_code,
                 get_detail=detail)


def test_ac7_park_fires_once_max_adds_is_on_the_books_across_distinct_hashes(
        tmp_path, monkeypatch):
    """The exact hole `_state_key(slug, download_id)` left open: each sweep
    sees a brand-new hash, so the OLD state file could never count past one.

    should_park reads the ledger BEFORE this sweep adds to it, so with
    MAX_ADDS=3 the park lands on the sighting that follows the third add —
    i.e. the guard lets the *arr have its three tries and then stops it."""
    for i in (1, 2, 3):
        sw = _regrab_sweep(tmp_path, monkeypatch, i)
        sw.run()
        assert sw.puts == [], "must not park before the threshold"
        assert len(sw.deletes) == 1
    led = json.loads((tmp_path / "arr-regrab-ledger.json").read_text())
    key = "sonarr|1234|5680"
    assert len(led[key]["adds"]) == 3
    assert led[key]["parked"] is False

    sw = _regrab_sweep(tmp_path, monkeypatch, 4)
    sw.run()
    assert len(sw.puts) == 1, "the sighting after MAX_ADDS must park"
    assert json.loads((tmp_path / "arr-regrab-ledger.json").read_text())[key]["parked"] is True


def test_inv4_unmonitor_is_issued_before_the_delete(tmp_path, monkeypatch):
    for i in (1, 2, 3):
        _regrab_sweep(tmp_path, monkeypatch, i).run()
    sw = _regrab_sweep(tmp_path, monkeypatch, 4)
    sw.run()
    methods = [c[0] for c in sw.calls if c[0] in ("PUT", "DELETE")]
    assert methods == ["PUT", "DELETE"]
    _, url, body = sw.puts[0]
    assert url.endswith("/episode/monitor")
    assert body == {"episodeIds": [5680], "monitored": False}


def test_inv4_a_failed_unmonitor_blocks_the_destructive_step(
        tmp_path, monkeypatch):
    """The whole point. A 500 on the park means NO delete, NO blocklist, NO
    park stamp — and the item is carried forward for the next sweep."""
    for i in (1, 2, 3):
        _regrab_sweep(tmp_path, monkeypatch, i).run()
    sw = _regrab_sweep(tmp_path, monkeypatch, 4, park_code=500)
    sw.run()
    assert len(sw.puts) == 1
    assert sw.deletes == [], "a failed park must not be followed by a delete"
    led = json.loads((tmp_path / "arr-regrab-ledger.json").read_text())
    assert led["sonarr|1234|5680"]["parked"] is False
    assert len(led["sonarr|1234|5680"]["adds"]) == 3, "no add was performed"
    assert json.loads(sw.state_file.read_text()), "must retry next sweep"


def test_movie_park_uses_the_movie_editor_write(tmp_path, monkeypatch):
    row = [r for r in _load("regrab.json") if r["id"] == 822004][0]
    led = {"radarr|441": {"adds": [time.time()] * 3, "parked": False,
                          "notified": False, "title": "m", "last": time.time()}}
    (tmp_path / "arr-regrab-ledger.json").write_text(json.dumps(led))
    sw = Sweep(tmp_path, monkeypatch, [row], slug="radarr",
               prior_state=_aged_state([row], slug="radarr"))
    sw.run()
    _, url, body = sw.puts[0]
    assert url.endswith("/movie/editor")
    assert body == {"movieIds": [441], "monitored": False, "moveFiles": False}


def test_inv5_no_code_or_comment_asserts_skipredownload_as_the_loop_stopper():
    """RULING 1. The parameter may be DISCUSSED, always negatively, and may
    never be sent."""
    src = (ROOT / "scripts" / "maint" / "arr-housekeeping.py").read_text(
        encoding="utf-8")
    # 1. It is never SENT: no query-string form of it exists anywhere.
    assert "skipRedownload=true&" not in src
    assert "&skipRedownload" not in src
    assert "?skipRedownload" not in src
    # 2. Every mention lives in the module docstring, where RULING 1 is
    #    recorded. No executable line, and no comment beside a request, may
    #    invoke it at all.
    doc = src.split('"""', 2)[1]
    body = src.split('"""', 2)[2]
    assert "skipRedownload" not in body, (
        "skipRedownload is named outside the module docstring — the only "
        "place RULING 1 permits it to be discussed")
    # 3. And where it IS named, it is named to be rejected.
    assert "That is NOT what stops the loop here" in doc
    assert "This file therefore never sends skipRedownload." in doc


# ===========================================================================
# AC-9 / INV-6 — the ledger fails open, one test per failure mode
# ===========================================================================

def test_ac9_missing_file_reads_empty(tmp_path):
    assert rl.read(tmp_path / "nope.json") == {}


def test_ac9_empty_file_reads_empty(tmp_path, capsys):
    p = tmp_path / "l.json"
    p.write_text("", encoding="utf-8")
    assert rl.read(p) == {}
    assert "regrab_ledger" in capsys.readouterr().err


def test_ac9_truncated_json_reads_empty(tmp_path, capsys):
    p = tmp_path / "l.json"
    p.write_text('{"sonarr|1|2": {"adds": [1, 2', encoding="utf-8")
    assert rl.read(p) == {}
    assert "unreadable ledger" in capsys.readouterr().err


def test_ac9_non_json_reads_empty(tmp_path):
    p = tmp_path / "l.json"
    p.write_text("this is not json at all", encoding="utf-8")
    assert rl.read(p) == {}


def test_ac9_non_dict_payload_reads_empty(tmp_path, capsys):
    p = tmp_path / "l.json"
    p.write_text('["a", "b"]', encoding="utf-8")
    assert rl.read(p) == {}
    assert "not an object" in capsys.readouterr().err


def test_ac9_malformed_entries_are_dropped_not_fatal(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({
        "good|1|2": {"adds": [1.0], "parked": True, "title": "t", "last": 5.0},
        "bad": "a string, not an entry",
        "alsobad": 7,
    }), encoding="utf-8")
    led = rl.read(p)
    assert list(led) == ["good|1|2"]
    assert led["good|1|2"]["parked"] is True


def test_ac9_unwritable_state_dir_returns_false_and_never_raises(tmp_path):
    # A path whose PARENT is a FILE: mkdir cannot succeed on any platform.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert rl.write({"a": {}}, blocker / "sub" / "l.json") is False


def test_inv6_a_corrupt_ledger_degrades_to_exactly_the_old_sweep(
        tmp_path, monkeypatch, capsys):
    """Same records, same prior state, with and without a corrupt ledger: the
    set of destructive actions must be identical."""
    row = _load("regrab.json")[2]
    sw_clean = Sweep(tmp_path, monkeypatch, [row], prior_state=_aged_state([row]))
    sw_clean.run()
    baseline = len(sw_clean.deletes)

    tmp2 = tmp_path / "second"
    tmp2.mkdir()
    (tmp2 / "arr-regrab-ledger.json").write_text("{{{ not json", encoding="utf-8")
    sw_bad = Sweep(tmp2, monkeypatch, [row], prior_state=_aged_state([row]))
    sw_bad.run()
    assert len(sw_bad.deletes) == baseline == 1
    assert sw_bad.puts == []


def test_inv6_guard_disabled_entirely_when_the_module_is_absent(
        tmp_path, monkeypatch):
    """A deploy that forgets the lib module must still repair stuck queues."""
    row = _load("regrab.json")[2]
    sw = Sweep(tmp_path, monkeypatch, [row], prior_state=_aged_state([row]))
    monkeypatch.setattr(arrhk, "_regrab", None)
    assert sw.run() == 0
    assert len(sw.deletes) == 1
    assert not (tmp_path / "arr-regrab-ledger.json").exists()


# ===========================================================================
# AC-10 / INV-8 — prune
# ===========================================================================

def _entry(last, parked=False):
    return {"adds": [last], "parked": parked, "notified": False,
            "title": "t", "last": last}


def test_ac10_prune_caps_at_max_keys_for_any_input(monkeypatch):
    monkeypatch.setattr(rl, "MAX_KEYS", 10)
    now = time.time()
    led = {f"sonarr|1|{i}": _entry(now - i) for i in range(500)}
    rl.prune(led, now)
    assert len(led) == 10


def test_ac10_parked_keys_are_evicted_last(monkeypatch):
    monkeypatch.setattr(rl, "MAX_KEYS", 3)
    now = time.time()
    led = {
        "sonarr|1|1": _entry(now - 1, parked=True),   # oldest, but parked
        "sonarr|1|2": _entry(now - 900),
        "sonarr|1|3": _entry(now - 800),
        "sonarr|1|4": _entry(now - 700),
        "sonarr|1|5": _entry(now - 600),
    }
    rl.prune(led, now)
    assert len(led) == 3
    assert "sonarr|1|1" in led, "a parked key must outlive newer unparked ones"


def test_ac10_prune_drops_keys_past_the_retention_window(monkeypatch):
    monkeypatch.setattr(rl, "RETAIN_HOURS", 1.0)
    monkeypatch.setattr(rl, "WINDOW_HOURS", 1.0)
    now = time.time()
    led = {"fresh|1|1": _entry(now), "stale|1|1": _entry(now - 7200)}
    rl.prune(led, now)
    assert list(led) == ["fresh|1|1"]


def test_ac10_prune_with_max_keys_zero_empties_rather_than_raising(monkeypatch):
    monkeypatch.setattr(rl, "MAX_KEYS", 0)
    now = time.time()
    led = {"a|1|1": _entry(now), "b|1|1": _entry(now, parked=True)}
    assert rl.prune(led, now) == {}


def test_prune_is_applied_by_the_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "MAX_KEYS", 2)
    now = time.time()
    stuffed = {f"sonarr|9|{i}": _entry(now - i) for i in range(20)}
    (tmp_path / "arr-regrab-ledger.json").write_text(json.dumps(stuffed))
    row = _load("regrab.json")[2]
    sw = Sweep(tmp_path, monkeypatch, [row], prior_state=_aged_state([row]))
    sw.run()
    assert len(sw.ledger()) <= 2


# ===========================================================================
# AC-11 / INV-9 — notify exactly once per park
# ===========================================================================

def _park_notifications(sw):
    return [m for lvl, m in sw.notifications if "parked (re-grab loop" in m]


def test_ac11_park_notifies_exactly_once_across_two_sweeps(
        tmp_path, monkeypatch):
    for i in (1, 2, 3):
        _regrab_sweep(tmp_path, monkeypatch, i).run()

    sw3 = _regrab_sweep(tmp_path, monkeypatch, 4)
    sw3.run()
    first = _park_notifications(sw3)
    assert len(first) == 1
    assert "TV parked (re-grab loop — unmonitored, manual intervention needed)" in first[0]
    assert "3 blocklist add(s) in 24h" in first[0]

    # Second sweep, still parked (the episode read says no file, still
    # unmonitored) — silence.
    sw4 = _regrab_sweep(tmp_path, monkeypatch, 5,
                        detail={"episode/5680": (200, json.dumps(
                            {"id": 5680, "hasFile": False, "monitored": False}))})
    sw4.run()
    assert _park_notifications(sw4) == []


def test_ac11_a_cleared_park_is_news_again_when_it_re_parks(
        tmp_path, monkeypatch):
    for i in (1, 2, 3, 4):
        _regrab_sweep(tmp_path, monkeypatch, i).run()

    # The episode now HAS a file: clearance drops the key entirely.
    cleared = _regrab_sweep(tmp_path, monkeypatch, 5, detail={
        "episode/5680": (200, json.dumps({"id": 5680, "hasFile": True,
                                          "monitored": False}))})
    cleared.run()
    # The park is gone AND so is its add history: the same sweep blocklists
    # once more, so the key is back at a single add with parked=False. That
    # reset is what makes the next park news again.
    entry = cleared.ledger()["sonarr|1234|5680"]
    assert entry["parked"] is False
    assert entry["notified"] is False
    assert len(entry["adds"]) == 1

    # ... and the loop starts over, so the next park notifies again.
    for i in (6, 7):
        _regrab_sweep(tmp_path, monkeypatch, i).run()
    again = _regrab_sweep(tmp_path, monkeypatch, 8)
    again.run()
    assert len(_park_notifications(again)) == 1


def test_a_read_failure_never_clears_a_park(tmp_path, monkeypatch):
    for i in (1, 2, 3, 4):
        _regrab_sweep(tmp_path, monkeypatch, i).run()
    # detail GET answers 500 (the Sweep default for an unknown path is 404):
    sw = _regrab_sweep(tmp_path, monkeypatch, 5,
                       detail={"episode/5680": (500, "boom")})
    sw.run()
    assert sw.ledger()["sonarr|1234|5680"]["parked"] is True


def test_ledger_level_notify_once_helpers():
    led: dict = {}
    rl.record_blocklist_add(led, "sonarr|1|1", "t", time.time())
    rl.mark_parked(led, "sonarr|1|1", time.time())
    assert rl.should_notify_park(led, "sonarr|1|1") is True
    rl.mark_notified(led, "sonarr|1|1")
    assert rl.should_notify_park(led, "sonarr|1|1") is False
    assert rl.clear(led, "sonarr|1|1") is True
    assert rl.clear(led, "sonarr|1|1") is False


# ===========================================================================
# INV-12 / §2.4 — budgets
# ===========================================================================

def test_inv12_park_accompanying_deletes_still_consume_the_run_cap(
        tmp_path, monkeypatch):
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "2")
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "2")
    now = time.time()
    rows, led = [], {}
    for i in range(5):
        row = dict(_load("regrab.json")[2], id=700 + i, episodeId=6000 + i,
                   downloadId=f"CAFE{i:036d}".upper())
        rows.append(row)
        led[f"sonarr|1234|{6000 + i}"] = {
            "adds": [now, now, now], "parked": False, "notified": False,
            "title": "t", "last": now}
    (tmp_path / "arr-regrab-ledger.json").write_text(json.dumps(led))
    sw = Sweep(tmp_path, monkeypatch, rows, prior_state=_aged_state(rows))
    sw.run()
    assert len(sw.deletes) == 2, "the pre-existing caps still bind"


def test_park_budget_overflow_defers_and_never_aborts(
        tmp_path, monkeypatch, capsys):
    """aab9e87 defer-oldest-N. Deferring the park defers the DELETE with it —
    a blocklist with no park behind it feeds the loop being guarded."""
    monkeypatch.setattr(rl, "MAX_PARKS_RUN", 2)
    now = time.time()
    rows, led = [], {}
    for i in range(4):
        row = dict(_load("regrab.json")[2], id=750 + i, episodeId=6100 + i,
                   downloadId=f"BEEF{i:036d}".upper())
        rows.append(row)
        led[f"sonarr|1234|{6100 + i}"] = {
            "adds": [now, now, now], "parked": False, "notified": False,
            "title": "t", "last": now}
    (tmp_path / "arr-regrab-ledger.json").write_text(json.dumps(led))
    sw = Sweep(tmp_path, monkeypatch, rows, prior_state=_aged_state(rows))
    assert sw.run() == 0, "overflow must never abort the sweep"
    out = capsys.readouterr().out
    assert len(sw.puts) == 2
    assert len(sw.deletes) == 2, "deferred parks must not be deleted either"
    assert "deferred_parks=2" in out
    # The deferred ones are still tracked, so next sweep retries them.
    assert len(json.loads(sw.state_file.read_text())) == 2


def test_park_reads_are_bounded_per_run(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "MAX_PARK_READS", 3)
    now = time.time()
    led = {f"sonarr|1234|{i}": {"adds": [now], "parked": True,
                                "notified": True, "title": "t", "last": now}
           for i in range(20)}
    (tmp_path / "arr-regrab-ledger.json").write_text(json.dumps(led))
    sw = Sweep(tmp_path, monkeypatch, [])
    sw.run()
    detail_gets = [c for c in sw.calls
                   if c[0] == "GET" and "/episode/" in c[1]]
    assert len(detail_gets) == 3


# ===========================================================================
# INV-13 — dry-run
# ===========================================================================

def test_inv13_dry_run_issues_no_put_no_delete_no_notify_and_no_ledger(
        tmp_path, monkeypatch):
    now = time.time()
    row = _load("regrab.json")[2]
    led = {"sonarr|1234|5680": {"adds": [now, now, now], "parked": False,
                                "notified": False, "title": "t", "last": now}}
    (tmp_path / "arr-regrab-ledger.json").write_text(json.dumps(led))
    sw = Sweep(tmp_path, monkeypatch, [row], prior_state=_aged_state([row]))
    sw.run(dry_run=True)
    assert sw.puts == []
    assert sw.deletes == []
    assert sw.notifications == []
    # PINNED: dry-run does NOT write the ledger. A `parked` stamp is a record
    # of an unmonitor that happened; stamping one in dry-run would make the
    # next LIVE run skip the write it never performed.
    assert sw.ledger() == led


def test_inv13_dry_run_on_a_cold_ledger_creates_no_file(tmp_path, monkeypatch):
    row = _load("regrab.json")[2]
    sw = Sweep(tmp_path, monkeypatch, [row], prior_state=_aged_state([row]))
    sw.run(dry_run=True)
    assert not (tmp_path / "arr-regrab-ledger.json").exists()


# ===========================================================================
# INV-11 — honest counting boundary
# ===========================================================================

def test_inv11_the_docstrings_declare_the_undercount_and_no_blocklist_read():
    led_src = (ROOT / "scripts" / "maint" / "lib" / "regrab_ledger.py").read_text(
        encoding="utf-8")
    assert "UNDER-report" in led_src
    assert "/api/v3/blocklist" in led_src  # named only to disclaim it
    assert "never reads" in led_src
    # And it really does not read it: this module speaks to no HTTP client
    # at all, so a claim that the ledger is a census is unbuildable here.
    body = led_src.split('"""', 2)[2]
    assert "urllib" not in body and "requests" not in body
    assert "api/v3" not in body


def test_adds_are_only_recorded_for_a_delete_that_succeeded(
        tmp_path, monkeypatch):
    row = _load("regrab.json")[2]
    sw = Sweep(tmp_path, monkeypatch, [row], prior_state=_aged_state([row]),
               delete_code=500)
    sw.run()
    assert sw.ledger() == {}, "a failed DELETE blocklisted nothing to count"
