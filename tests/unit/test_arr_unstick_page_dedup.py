"""tests/unit/test_arr_unstick_page_dedup.py — the 2026-09-17 alert storm.

MEASURED, 24h Discord capture: 13 messages, 12 of them from ONE fault, all
from `cmd_unstick`. The sweep runs hourly, a re-grab loop hands it something
to delete every hour, and it notified on every run that took any action. The
single message that carried escalation value — the cap-hit @ping, "systemic
issue likely" — was buried among eleven look-alike warnings.

The re-grab loop is NOT the bug and is not touched here. Deleting a stuck
grab hourly is what the job is for. The bug is the PAGING POLICY, so these
tests pin the policy:

  * the same ongoing condition collapses to one page per 24h,
  * the key survives the re-grab (downloadId changes every hour BY DESIGN —
    keying on it would dedup nothing and reproduce the storm),
  * a genuinely new stuck title still pages within the hour,
  * the escalation cannot be muted by routine chatter, and
  * suppression is a NOTIFICATION policy: the log still gets everything.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "maint"))

_spec = importlib.util.spec_from_file_location(
    "arr_housekeeping_dedup", REPO_ROOT / "scripts" / "maint" / "arr-housekeeping.py")
arrhk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arrhk)


# ---------------------------------------------------------------------------
# Synthetic queue records. No real titles, no real hosts — public repo.
# ---------------------------------------------------------------------------

def _movie_item(*, movie_id: int = 4242, download_id: str = "A" * 40,
                qid: int = 11, title: str = "Synthetic Movie (2019)") -> dict:
    return {
        "id": qid,
        "movieId": movie_id,
        "downloadId": download_id,
        "title": title,
        "status": "warning",
        "trackedDownloadState": "downloading",
        "errorMessage": "The download is stalled with no connections",
        "sizeleft": 1000,
    }


def _tv_item(*, series_id: int = 77, episode_ids=(901, 902),
             download_id: str = "B" * 40, qid: int = 22,
             title: str = "Synthetic Show S01E01") -> dict:
    return {
        "id": qid,
        "seriesId": series_id,
        "episodeIds": list(episode_ids),
        "downloadId": download_id,
        "title": title,
        "status": "warning",
        "trackedDownloadState": "downloading",
        "errorMessage": "The download is stalled with no connections",
        "sizeleft": 1000,
    }


class Harness:
    """Drives cmd_unstick against a fake radarr queue and records notify()."""

    def __init__(self, tmp_path: Path, monkeypatch, *, slug: str = "radarr"):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.dir = tmp_path
        self.slug = slug
        self.state_file = tmp_path / "stuck.json"
        self.calls: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.records: list[dict] = []
        monkeypatch.setattr(arrhk, "STATE_DIR", tmp_path)
        monkeypatch.setattr(arrhk, "STUCK_STATE_FILE", self.state_file)
        monkeypatch.setenv("ARR_STUCK_HOURS_PEERS", "0")
        monkeypatch.setattr(arrhk, "THRESHOLD_HOURS_BY_MODE",
                            dict(arrhk.THRESHOLD_HOURS_BY_MODE, **{arrhk.MODE_PEERS: 0.0}))
        monkeypatch.setattr(arrhk, "_arr_key",
                            lambda s: "k" if s == self.slug else "")
        monkeypatch.setattr(arrhk, "_req", self._fake_req)
        monkeypatch.setattr(arrhk, "_notify",
                            lambda msg, level="info": self.calls.append((msg, level)))

    def _fake_req(self, method, url, key, **kw):
        if method == "GET" and f"/{self.slug}/" in url and "/queue" in url:
            return 200, json.dumps({"records": self.records})
        if method == "GET":
            return 200, json.dumps({"records": []})
        if method == "DELETE":
            self.deletes.append(url)
            return 200, ""
        return 500, ""

    def run(self, items, *, aged: bool = True) -> int:
        """One hourly sweep. `aged` pre-seeds the stuck-tracking state so the
        items are past their grace period — that state is keyed by downloadId
        and is NOT the page ledger under test."""
        self.records = items
        seeded = {}
        if aged:
            for it in items:
                sk = f"{self.slug}:{(it.get('downloadId') or '').lower()}"
                seeded[sk] = {
                    "title": it["title"], "queue_id": it["id"],
                    "first_seen_stuck": time.time() - 86400,
                    "slug": self.slug, "mode": arrhk.MODE_PEERS,
                    "sizeleft_history": [],
                }
        self.state_file.write_text(json.dumps(seeded))
        return arrhk.cmd_unstick(dry_run=False)

    # -- assertions helpers --------------------------------------------
    @property
    def warnings(self):
        return [m for m, lv in self.calls if lv == "warning"]

    @property
    def errors(self):
        return [m for m, lv in self.calls if lv == "error"]

    def log_actions(self) -> list[str]:
        path = self.dir / arrhk.UNSTICK_LOG_FILE
        if not path.exists():
            return []
        # Drop the timestamp column: it is wall-clock and differs per run.
        return [ln.split("\t", 1)[1] for ln in path.read_text(encoding="utf-8").splitlines()
                if "\taction\t" in ln]

    def log_decisions(self) -> list[str]:
        path = self.dir / arrhk.UNSTICK_LOG_FILE
        if not path.exists():
            return []
        return [ln.split("\t", 1)[1] for ln in path.read_text(encoding="utf-8").splitlines()
                if "\tdecision\t" in ln]


@pytest.fixture()
def h(tmp_path, monkeypatch):
    return Harness(tmp_path / "state", monkeypatch)


# ---------------------------------------------------------------------------
# AC-3 / section 3 — the key choice is re-grab-proof
# ---------------------------------------------------------------------------

def test_page_key_ignores_downloadId(h):
    """A re-grab loop obtains a NEW infohash on every grab. A downloadId-keyed
    ledger would mint a fresh key every hour and dedup NOTHING."""
    keys = {arrhk._page_key("radarr", _movie_item(download_id=f"{i:040X}"),
                            arrhk.MODE_PEERS) for i in range(24)}
    assert keys == {"unstick:radarr:m4242:stalled-no-peers"}


def test_page_key_is_distinct_per_content():
    a = arrhk._page_key("radarr", _movie_item(movie_id=1), arrhk.MODE_PEERS)
    b = arrhk._page_key("radarr", _movie_item(movie_id=2), arrhk.MODE_PEERS)
    assert a != b


def test_tv_key_uses_series_and_episode_ids():
    k = arrhk._page_key("sonarr", _tv_item(series_id=77, episode_ids=(902, 901)),
                        arrhk.MODE_PEERS)
    assert k == "unstick:sonarr:s77e901+902:stalled-no-peers"


def test_unknown_series_row_falls_back_to_a_normalized_title_hash():
    """includeUnknownSeriesItems=true rows carry neither id. Cosmetic release
    variation must not defeat the key."""
    bare = {"id": 1, "downloadId": "C" * 40, "title": "Some.Release.1080p.WEB-DL-NTb"}
    other = dict(bare, downloadId="D" * 40,
                 title="  some.release.1080p.WEB-DL-GROUP2 ")
    k1 = arrhk._content_key("sonarr", bare)
    k2 = arrhk._content_key("sonarr", other)
    assert k1.startswith("sonarr:t") and k1 == k2
    assert arrhk._content_key("sonarr", dict(bare, title="Different Release")) != k1


def test_slug_is_part_of_the_key():
    item = _movie_item()
    assert (arrhk._page_key("radarr", item, arrhk.MODE_PEERS)
            != arrhk._page_key("radarr2", item, arrhk.MODE_PEERS))


# ---------------------------------------------------------------------------
# AC-2 — storm collapse: the captured 24h sequence
# ---------------------------------------------------------------------------

def test_24_hourly_runs_of_one_ongoing_condition_yield_one_warning(h, monkeypatch):
    """The replay. Same content, re-grab changing downloadId every run, cap
    hit on every run (2 stuck items, per-run cap of 1)."""
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "1")
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "1")
    for hour in range(24):
        h.run([
            _movie_item(movie_id=4242, download_id=f"{hour:040X}", qid=100 + hour),
            _movie_item(movie_id=5150, download_id=f"{hour + 99:040X}", qid=200 + hour,
                        title="Synthetic Other Movie (2021)"),
        ])

    assert len(h.warnings) == 1, h.warnings
    assert len(h.errors) == 1, h.errors
    assert len(h.deletes) == 24, "the sweep itself must keep its cadence"


def test_a_new_stuck_title_pages_on_the_very_next_run(h):
    h.run([_movie_item(movie_id=4242)])
    assert len(h.warnings) == 1
    h.run([_movie_item(movie_id=4242, download_id="E" * 40),
           _movie_item(movie_id=9999, qid=33, download_id="F" * 40,
                       title="Synthetic New Movie (2024)")])
    assert len(h.warnings) == 2
    assert "m9999" not in h.warnings[1], "the body carries titles, not keys"
    assert "Synthetic New Movie" in h.warnings[1]
    assert "Synthetic Movie (2019)" not in h.warnings[1], "muted line leaked"


# ---------------------------------------------------------------------------
# AC-4 — mode is part of the key
# ---------------------------------------------------------------------------

def test_mode_transition_pages_once_per_mode(h, monkeypatch):
    h.run([_movie_item()])
    assert len(h.warnings) == 1

    # Same content, now classified differently: a materially different fault.
    monkeypatch.setattr(arrhk, "_classify_stuck",
                        lambda item, by_dl: arrhk.MODE_CLUSTER)
    monkeypatch.setattr(arrhk, "THRESHOLD_HOURS_BY_MODE",
                        dict(arrhk.THRESHOLD_HOURS_BY_MODE, **{arrhk.MODE_CLUSTER: 0.0}))
    h.run([_movie_item(download_id="9" * 40)])
    assert len(h.warnings) == 2
    assert "slow-cluster" in h.warnings[1]


# ---------------------------------------------------------------------------
# AC-5 — window boundary, through cmd_unstick
# ---------------------------------------------------------------------------

def test_cooldown_env_override_reopens_the_window(h, monkeypatch):
    monkeypatch.setenv("ARR_UNSTICK_PAGE_COOLDOWN_S", "0")
    h.run([_movie_item()])
    h.run([_movie_item(download_id="7" * 40)])
    assert len(h.warnings) == 2, "a 0s cooldown must never mute"


def test_default_cooldown_is_a_day():
    assert arrhk.ARR_UNSTICK_PAGE_COOLDOWN_S == 86400.0
    assert arrhk.ARR_UNSTICK_CAP_PAGE_COOLDOWN_S == 86400.0


# ---------------------------------------------------------------------------
# AC-6 / AC-7 / AC-8 — the escalation carve-out
# ---------------------------------------------------------------------------

def _cap_run(h, monkeypatch, *, items=None, per_run="1"):
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", per_run)
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_SLUG", "5")
    items = items or [_movie_item(movie_id=4242, download_id="1" * 40),
                      _movie_item(movie_id=5150, qid=44, download_id="2" * 40,
                                  title="Synthetic Other Movie (2021)")]
    return h.run(items)


def test_cap_hit_pages_separately_even_when_every_item_key_is_muted(h, monkeypatch):
    _cap_run(h, monkeypatch, per_run="9")          # stamps both item keys, no cap
    h.calls.clear()
    _cap_run(h, monkeypatch, per_run="1")          # every item key now muted

    assert h.warnings == [], "no routine line was due"
    assert len(h.errors) == 1, "the systemic signal must survive a muted sweep"
    body = h.errors[0]
    assert body.splitlines()[0] == arrhk.CAP_BANNER
    assert "SYSTEMIC" in body and "CAP HIT" in body


def test_cap_text_is_never_concatenated_onto_the_routine_body(h, monkeypatch):
    _cap_run(h, monkeypatch, per_run="1")
    assert len(h.warnings) == 1 and len(h.errors) == 1
    assert arrhk.CAP_BANNER not in h.warnings[0]
    assert "systemic issue likely" not in h.warnings[0]
    for msg, level in h.calls:
        if msg.startswith("arr-unstick swept:"):
            assert level == "warning", "a routine body must never be error-level"


def test_cap_page_is_per_outage(h, monkeypatch):
    _cap_run(h, monkeypatch, per_run="1")
    assert len(h.errors) == 1

    _cap_run(h, monkeypatch, per_run="1")          # same condition, still capped
    assert len(h.errors) == 1, "no second page inside the window"

    # A run that takes actions and does NOT hit the cap: the outage is over.
    h.run([_movie_item(movie_id=4242, download_id="3" * 40)])
    _cap_run(h, monkeypatch, per_run="1")
    assert len(h.errors) == 2, "the next cap-hit is news again"


def test_cap_stays_visible_in_the_routine_body_while_muted(h, monkeypatch):
    _cap_run(h, monkeypatch, per_run="1")          # cap page fires, both keys stamped
    h.calls.clear()
    # New content is due, cap still hit, cap stamp unexpired.
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "1")
    h.run([_movie_item(movie_id=7777, qid=55, download_id="4" * 40,
                       title="Synthetic Third Movie (2022)"),
           _movie_item(movie_id=8888, qid=56, download_id="5" * 40,
                       title="Synthetic Fourth Movie (2023)")])

    assert len(h.errors) == 0, "cap already paged inside its window"
    assert len(h.warnings) == 1
    assert h.warnings[0].count("cap still hit") == 1


def test_cap_hit_with_zero_actions_keeps_its_wording(h, monkeypatch):
    monkeypatch.setenv("ARR_MAX_ACTIONS_PER_RUN", "0")
    h.run([_movie_item()])
    assert h.warnings == []
    assert len(h.errors) == 1
    assert "cap hit with zero successful actions" in h.errors[0]


# ---------------------------------------------------------------------------
# AC-9 / AC-10 / AC-11 — silence, logging, footer
# ---------------------------------------------------------------------------

def test_a_fully_muted_run_makes_zero_notify_calls(h):
    h.run([_movie_item()])
    h.calls.clear()
    h.run([_movie_item(download_id="6" * 40)])
    assert h.calls == []


def test_a_fully_muted_run_still_logs_everything(h, capsys):
    h.run([_movie_item()])
    first_actions = h.log_actions()
    capsys.readouterr()

    h.run([_movie_item(download_id="8" * 40)])
    out = capsys.readouterr().out
    all_actions = h.log_actions()

    # Log fidelity: the muted run wrote the SAME action row as the paged one.
    assert len(all_actions) == 2
    assert all_actions[1] == first_actions[0], "suppression changed the log"
    decisions = h.log_decisions()
    assert decisions[0].endswith("\tdue")
    assert decisions[1].endswith("\tmuted")
    assert "unstick:radarr:m4242:stalled-no-peers" in decisions[1]
    # ...and stdout still carries it on a run that said nothing to Discord.
    assert "Synthetic Movie (2019)" in out
    assert "muted unstick:radarr:m4242:stalled-no-peers" in out


def test_footer_counts_the_muted_keys_and_hides_their_lines(h):
    h.run([_movie_item(movie_id=1, qid=1, download_id="1" * 40, title="Muted A"),
           _movie_item(movie_id=2, qid=2, download_id="2" * 40, title="Muted B")])
    h.calls.clear()
    h.run([_movie_item(movie_id=1, qid=1, download_id="3" * 40, title="Muted A"),
           _movie_item(movie_id=2, qid=2, download_id="4" * 40, title="Muted B"),
           _movie_item(movie_id=3, qid=3, download_id="5" * 40, title="Fresh C")])

    body = h.warnings[0]
    assert body.count("ongoing condition(s) already paged") == 1
    assert "(+2 ongoing condition(s)" in body
    assert "Fresh C" in body
    assert "Muted A" not in body and "Muted B" not in body
    assert "arr-housekeeping.log" in body, "the footer must say where the detail is"


# ---------------------------------------------------------------------------
# AC-12 — fail open, end to end
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("break_it", ["absent", "malformed", "non-numeric", "io-error"])
def test_cmd_unstick_pages_through_every_ledger_failure(h, monkeypatch, break_it):
    from lib import page_ledger

    h.run([_movie_item()])                     # stamp it, so only a FAILURE re-pages
    h.calls.clear()
    ledger = h.dir / arrhk.UNSTICK_PAGE_LEDGER

    if break_it == "absent":
        ledger.unlink()
    elif break_it == "malformed":
        ledger.write_text("{not json", encoding="utf-8")
    elif break_it == "non-numeric":
        ledger.write_text(json.dumps(
            {"unstick:radarr:m4242:stalled-no-peers": "yesterday"}), encoding="utf-8")
    else:
        # An unreadable/unwritable state dir, at the point where it can
        # actually cost a page. A WRITE only happens when something is already
        # due, so a write failure cannot mute anything by construction; the
        # write-failure direction is pinned in test_page_ledger.py instead.
        def boom(*_a, **_k):
            raise PermissionError("read-only filesystem")
        monkeypatch.setattr(page_ledger, "read_ledger", boom)

    rc = h.run([_movie_item(download_id="A1" + "0" * 38)])

    assert rc == 0, "no exception may escape cmd_unstick"
    assert len(h.warnings) == 1, f"{break_it}: a broken ledger must never mute"


def test_cmd_unstick_survives_a_partition_that_raises(h, monkeypatch):
    monkeypatch.setattr(arrhk, "_partition_due",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = h.run([_movie_item()])
    assert rc == 0
    assert len(h.warnings) == 1


def test_log_failure_does_not_break_the_sweep(h, monkeypatch):
    monkeypatch.setattr(arrhk, "_append_unstick_log",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        # Sanity: the helper we patched really is called (if it were not, this
        # test would silently prove nothing).
        arrhk._append_unstick_log(["x"], [])
    monkeypatch.setattr(arrhk, "_append_unstick_log", lambda *a, **k: None)
    assert h.run([_movie_item()]) == 0


# ---------------------------------------------------------------------------
# AC-13 — bounded state, pruned every run
# ---------------------------------------------------------------------------

def test_every_run_prunes_expired_stamps(h):
    ledger = h.dir / arrhk.UNSTICK_PAGE_LEDGER
    h.dir.mkdir(parents=True, exist_ok=True)
    old = time.time() - 200_000
    seeded = {f"unstick:radarr:m{i}:stalled-no-peers": old for i in range(10_000)}
    seeded.update({f"unstick:radarr:m9{i}:stalled-no-peers": time.time() - 5
                   for i in range(3)})
    ledger.write_text(json.dumps(seeded))

    h.run([_movie_item(movie_id=4242)])

    kept = json.loads(ledger.read_text())
    # 3 live seeds + the key this run just stamped.
    assert len(kept) == 4, sorted(kept)[:5]


# ---------------------------------------------------------------------------
# AC-24 — no new channel, no new timer
# ---------------------------------------------------------------------------

def test_the_only_notifier_is_still_lib_notify():
    src = (REPO_ROOT / "scripts" / "maint" / "arr-housekeeping.py").read_text(encoding="utf-8")
    assert "from lib.notify import notify" in src
    # "notifiarr" appears once, in the comment recording its 2026-05-10
    # retirement; what must not appear is a second live transport.
    for forbidden in ("discord.com/api/webhooks", "notifiarr.com",
                      "smtplib", "requests.post", "urlopen(webhook"):
        assert forbidden not in src.lower(), f"new channel introduced: {forbidden}"
