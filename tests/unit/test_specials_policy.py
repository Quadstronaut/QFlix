"""Tests for scripts/mcp/specials_policy.py — Season-0 specials janitor.

Stateless, convergent enforcement of "Season 0 is never monitored on QFlix":
unmonitor any monitored S00 episode AND clear the Season-0 season flag (the flag
clear is what makes it durable — a series refresh re-monitors episodes to match
the season flag). No network: a seriesId-aware FakeClient stands in for ArrClient.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "mcp"))

import specials_policy as sp  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures + fake client
# ---------------------------------------------------------------------------

def mk_series(sid, title="Show", s0_monitored=None, s0_total=0, seasons_extra=(1,)):
    """A series object. s0_monitored=None => no Season 0 entry at all."""
    seasons = []
    if s0_monitored is not None or s0_total:
        seasons.append({"seasonNumber": 0,
                        "monitored": bool(s0_monitored),
                        "statistics": {"totalEpisodeCount": s0_total}})
    for n in seasons_extra:
        seasons.append({"seasonNumber": n, "monitored": True,
                        "statistics": {"totalEpisodeCount": 5}})
    return {"id": sid, "title": title, "monitored": True, "seasons": seasons}


def mk_ep(eid, sid, season, ep, monitored, has_file=False):
    return {"id": eid, "seriesId": sid, "seasonNumber": season,
            "episodeNumber": ep, "monitored": monitored, "hasFile": has_file}


class FakeClient:
    """ArrClient stand-in with seriesId-aware /episode routing; records writes."""

    def __init__(self, series, episodes=None):
        self.series = series                 # list[dict]
        self.episodes = episodes or {}       # {series_id: [episode, ...]}
        self.writes = []                     # [(method, path, body)]
        self.episode_gets = 0                # how many /episode fetches happened

    def get(self, path, *, query="", **kw):
        if path.startswith("/series"):
            return (200, self.series)
        if path.startswith("/episode"):
            self.episode_gets += 1
            sid = None
            for part in query.split("&"):
                if part.startswith("seriesId="):
                    sid = int(part.split("=", 1)[1])
            return (200, self.episodes.get(sid, []))
        return (404, {"error": f"no route GET {path}"})

    def put(self, path, *, body=None, **kw):
        self.writes.append(("PUT", path, body))
        if "episode/monitor" in path:
            return (202, [])
        return (202, body)


def _one_instance(series, episodes, dry_run=False):
    """Run only the sonarr instance through run(); return (result, client)."""
    client = FakeClient(series, episodes)
    empty = FakeClient([])
    res = sp.run(client_factory=lambda s: client if s == "sonarr" else empty,
                 dry_run=dry_run, slug="sonarr")
    return res["per_arr"]["sonarr"], client


# ---------------------------------------------------------------------------
# Core behavior
# ---------------------------------------------------------------------------

def test_unmonitors_monitored_s0_and_clears_flag():
    series = [mk_series(10, "Ted Lasso", s0_monitored=True, s0_total=9)]
    eps = {10: [mk_ep(101, 10, 0, 1, True), mk_ep(102, 10, 0, 2, True),
                mk_ep(103, 10, 1, 1, True)]}          # S01 must be untouched
    res, client = _one_instance(series, eps)

    assert res["status"] == "ok"
    assert res["episodes_unmonitored"] == 2
    assert res["series_changed"] == 1

    ep_write = next(w for w in client.writes if "episode/monitor" in w[1])
    assert ep_write[2] == {"episodeIds": [101, 102], "monitored": False}

    series_write = next(w for w in client.writes if w[1] == "/series/10")
    s0 = next(s for s in series_write[2]["seasons"] if s["seasonNumber"] == 0)
    s1 = next(s for s in series_write[2]["seasons"] if s["seasonNumber"] == 1)
    assert s0["monitored"] is False
    assert s1["monitored"] is True                     # other seasons preserved


def test_idempotent_noop_when_already_clean():
    series = [mk_series(10, "Ted Lasso", s0_monitored=False, s0_total=9)]
    eps = {10: [mk_ep(101, 10, 0, 1, False), mk_ep(103, 10, 1, 1, True)]}
    res, client = _one_instance(series, eps)
    assert res["series_changed"] == 0
    assert client.writes == []


def test_clears_orphan_season_flag_with_zero_monitored_eps():
    # The live Chainsaw Man case: flag still True, but episodes already off.
    series = [mk_series(20, "Chainsaw Man", s0_monitored=True, s0_total=8)]
    eps = {20: [mk_ep(201, 20, 0, 1, False), mk_ep(202, 20, 0, 2, False)]}
    res, client = _one_instance(series, eps)
    assert res["series_changed"] == 1
    assert res["episodes_unmonitored"] == 0
    # no episode write, but the season flag IS cleared
    assert not any("episode/monitor" in w[1] for w in client.writes)
    assert any(w[1] == "/series/20" for w in client.writes)


def test_dry_run_writes_nothing_but_reports():
    series = [mk_series(10, "Ted Lasso", s0_monitored=True, s0_total=9)]
    eps = {10: [mk_ep(101, 10, 0, 1, True)]}
    res, client = _one_instance(series, eps, dry_run=True)
    assert client.writes == []
    assert res["series_changed"] == 1
    assert res["episodes_unmonitored"] == 1


def test_series_without_season0_not_even_scanned():
    series = [mk_series(30, "Regular Show", s0_monitored=None)]  # no S0 entry
    res, client = _one_instance(series, {})
    assert res["series_changed"] == 0
    assert client.writes == []
    assert client.episode_gets == 0                    # skipped before I/O


def test_empty_instance_handled():
    res, client = _one_instance([], {})
    assert res["status"] == "ok"
    assert res["series_changed"] == 0
    assert client.writes == []


def test_failed_series_list_surfaces():
    class Broken(FakeClient):
        def get(self, path, *, query="", **kw):
            if path.startswith("/series"):
                return (500, {"error": "boom"})
            return super().get(path, query=query, **kw)
    client = Broken([])
    empty = FakeClient([])
    res = sp.run(client_factory=lambda s: client if s == "sonarr" else empty,
                 dry_run=False, slug="sonarr")
    assert res["per_arr"]["sonarr"]["status"] == "failed-series-list"


def test_episode_fetch_failure_surfaces_and_skips_writes():
    # D1: a GET /episode failure must NOT clear the season flag blind (we can't
    # see the episodes) and MUST surface a non-ok status so --cron exits 1.
    class EpFail(FakeClient):
        def get(self, path, *, query="", **kw):
            if path.startswith("/episode"):
                return (500, {"error": "boom"})
            return super().get(path, query=query, **kw)

    series = [mk_series(10, "Ted Lasso", s0_monitored=True, s0_total=9)]
    client = EpFail(series, {10: [mk_ep(101, 10, 0, 1, True)]})
    empty = FakeClient([])
    res = sp.run(client_factory=lambda s: client if s == "sonarr" else empty,
                 dry_run=False, slug="sonarr")
    r = res["per_arr"]["sonarr"]
    assert r["status"] != "ok"                    # failure surfaced -> exit 1
    assert client.writes == []                    # no blind flag-clear / unmonitor


def test_per_instance_exception_is_isolated():
    # D5: a malformed series (missing 'id') must not crash the whole run and
    # take sonarr2 down with it.
    bad = [{"title": "NoId", "seasons": [{"seasonNumber": 0, "monitored": True,
                                          "statistics": {"totalEpisodeCount": 1}}]}]
    s1 = FakeClient(bad, {})
    s2 = FakeClient([mk_series(20, "B", s0_monitored=True, s0_total=1)],
                    {20: [mk_ep(201, 20, 0, 1, True)]})
    clients = {"sonarr": s1, "sonarr2": s2}
    res = sp.run(client_factory=lambda s: clients[s], dry_run=False)
    assert res["per_arr"]["sonarr"]["status"].startswith("failed")   # isolated
    assert res["per_arr"]["sonarr2"]["episodes_unmonitored"] == 1     # ran anyway


def test_non_s0_season_without_monitored_key_preserved_verbatim():
    # D7: rebuilding seasons must not inject monitored=None on a season that
    # arrived without the key.
    series = {"id": 10, "title": "X", "seasons": [
        {"seasonNumber": 0, "monitored": True},
        {"seasonNumber": 1},                       # no 'monitored' key
    ]}
    out = sp._with_season0_unmonitored(series)
    s0 = next(s for s in out["seasons"] if s["seasonNumber"] == 0)
    s1 = next(s for s in out["seasons"] if s["seasonNumber"] == 1)
    assert s0["monitored"] is False
    assert "monitored" not in s1                    # verbatim — no None injected


def test_run_covers_both_tv_instances():
    s1 = FakeClient([mk_series(10, "A", s0_monitored=True, s0_total=1)],
                    {10: [mk_ep(101, 10, 0, 1, True)]})
    s2 = FakeClient([mk_series(20, "B", s0_monitored=True, s0_total=1)],
                    {20: [mk_ep(201, 20, 0, 1, True)]})
    clients = {"sonarr": s1, "sonarr2": s2}
    res = sp.run(client_factory=lambda s: clients[s], dry_run=False)
    assert set(res["per_arr"]) == {"sonarr", "sonarr2"}
    assert res["per_arr"]["sonarr"]["episodes_unmonitored"] == 1
    assert res["per_arr"]["sonarr2"]["episodes_unmonitored"] == 1


# ---------------------------------------------------------------------------
# Blast rails: cap, exclusions, escalation (arbiter fix 2026-08-03)
# ---------------------------------------------------------------------------
#
# WHY: this was the only *arr-mutating script in the repo with NONE of the three
# safety mechanisms the others carry. Counted across the four mutators:
#     specials_policy.py       exclude=0   cap=0   --execute=0
#     quality_fallback.py      exclude=0   cap=6   --execute=0
#     qflix-reaper.py          exclude=16  cap=21  --execute=14
#     qflix-torrent-janitor.py exclude=10  cap=20  --execute=7
# And it is CONVERGENT -- it re-asserts daily -- so an operator exception had no
# lever short of disabling the timer for all 38 series.


def _many_specials(n, series_id=1, tvdb=7001, title="Ted Lasso"):
    """A series whose Season 0 holds n monitored episodes -- the shape an
    upstream TheTVDB reclassification produces on Sonarr's nightly refresh."""
    s = {"id": series_id, "title": title, "tvdbId": tvdb,
         "seasons": [{"seasonNumber": 0, "monitored": True,
                      "statistics": {"totalEpisodeCount": n}},
                     {"seasonNumber": 1, "monitored": True}]}
    eps = [{"id": 1000 * series_id + i, "seasonNumber": 0, "monitored": True}
           for i in range(n)]
    return s, eps


class _Recorder:
    def __init__(self, series, episodes_by_series):
        self.series = series
        self.eps = episodes_by_series
        self.writes = []

    def get(self, path, query=None, **kw):
        if path == "/series":
            return 200, self.series
        if path == "/episode":
            sid = int(str(query).split("=")[-1])
            return 200, self.eps.get(sid, [])
        return 404, {}

    def put(self, path, *, body=None, **kw):
        self.writes.append((path, body))
        return 202, {}


def test_a_mass_reclassification_is_capped_and_the_overflow_is_deferred():
    """Before this, ONE run could unmonitor an unbounded number of episodes.
    Reproduced on the real enforce_instance with a 24-episode season plus 7
    operator-monitored specials: 31 unmonitored in a single run, largest single
    episodeIds batch 24, zero cap constants in the module."""
    s1, e1 = _many_specials(24, series_id=1, tvdb=7001, title="Big Show")
    s2, e2 = _many_specials(24, series_id=2, tvdb=7002, title="Other Show")
    s3, e3 = _many_specials(24, series_id=3, tvdb=7003, title="Third Show")
    client = _Recorder([s1, s2, s3], {1: e1, 2: e2, 3: e3})
    res = sp.enforce_instance(client, dry_run=False)
    assert res["episodes_unmonitored"] <= sp.MAX_UNMONITORS_PER_RUN, res
    assert res["deferred_count"] >= 1, res
    assert res["deferred"], "the deferral must be NAMED, not just counted"
    touched = {p for p, _b in client.writes}
    assert "/episode/monitor" in touched


def test_the_deferred_work_is_picked_up_by_the_next_run():
    """DEFER, not abort: convergence must still reach the same end state, just
    across more runs. Otherwise the cap would stall the janitor permanently."""
    s1, e1 = _many_specials(60, series_id=1, tvdb=7001, title="Huge Show")
    s2, e2 = _many_specials(3, series_id=2, tvdb=7002, title="Small Show")
    client = _Recorder([s1, s2], {1: e1, 2: e2})
    first = sp.enforce_instance(client, dry_run=False)
    assert first["deferred"] == ["Small Show"], first

    # The next run sees the first series already converged.
    s1["seasons"][0]["monitored"] = False
    s1["seasons"][0]["statistics"] = {"totalEpisodeCount": 60}
    for e in e1:
        e["monitored"] = False
    second = sp.enforce_instance(_Recorder([s1, s2], {1: e1, 2: e2}),
                                 dry_run=False)
    assert second["deferred_count"] == 0
    assert "Small Show" in [c["title"] for c in second["changes"]]


def test_an_excluded_series_survives_convergence(tmp_path):
    """The whole point of an exclusion on a CONVERGENT janitor: it must still
    hold on the second run, and the third, and forever."""
    s1, e1 = _many_specials(4, series_id=1, tvdb=7001, title="Ted Lasso")
    exclude = tmp_path / "specials_policy.exclude"
    exclude.write_text("# operator keeps these specials\n7001\n",
                       encoding="utf-8")
    tokens = sp.load_exclusions(exclude)
    for _ in range(3):
        client = _Recorder([s1], {1: e1})
        res = sp.enforce_instance(client, dry_run=False, exclusions=tokens)
        assert client.writes == [], client.writes
        assert res["excluded"] == ["Ted Lasso"], res
        assert res["episodes_unmonitored"] == 0


def test_exclusion_matches_by_title_too(tmp_path):
    s1, _e1 = _many_specials(4, series_id=1, tvdb=7001, title="Ted Lasso")
    exclude = tmp_path / "x.exclude"
    exclude.write_text("Ted Lasso\n", encoding="utf-8")
    assert sp._is_excluded(s1, sp.load_exclusions(exclude)) is True


def test_exclusions_discriminate(tmp_path):
    """MUTATION PROOF: a non-matching entry must not accidentally exclude
    everything."""
    s1, e1 = _many_specials(4, series_id=1, tvdb=7001, title="Ted Lasso")
    exclude = tmp_path / "x.exclude"
    exclude.write_text("9999\n", encoding="utf-8")
    client = _Recorder([s1], {1: e1})
    res = sp.enforce_instance(client, dry_run=False,
                              exclusions=sp.load_exclusions(exclude))
    assert res["excluded"] == []
    assert res["episodes_unmonitored"] == 4


def test_a_missing_exclude_file_is_normal_but_an_unreadable_one_is_not(tmp_path):
    """No file means no exceptions. A file that EXISTS and cannot be read must
    not quietly become "there are no exceptions"."""
    assert sp.load_exclusions(tmp_path / "nope") == set()
    bad = tmp_path / "isadir.exclude"
    bad.mkdir()
    with pytest.raises(OSError):
        sp.load_exclusions(bad)


def test_comments_and_blanks_are_ignored(tmp_path):
    f = tmp_path / "x.exclude"
    f.write_text("# a comment\n\n7001  # trailing\n   \n7002\n",
                 encoding="utf-8")
    assert sp.load_exclusions(f) == {"7001", "7002"}


def test_emit_json_issues_no_arr_writes(monkeypatch, tmp_path):
    """--emit-json means "read and print JSON" everywhere else under
    scripts/mcp/. Here it ran the full live mutation path and exited 0."""
    s1, e1 = _many_specials(4, series_id=1, tvdb=7001)
    clients = {"sonarr": _Recorder([s1], {1: e1}),
               "sonarr2": _Recorder([], {})}
    monkeypatch.setattr(sp, "ArrClient", lambda slug, ver: clients[slug])
    monkeypatch.setattr(sp, "EXCLUDE_PATH", tmp_path / "none.exclude")
    monkeypatch.setattr(sys, "argv", ["specials_policy.py", "--emit-json"])
    assert sp.main() == 0
    assert clients["sonarr"].writes == [], clients["sonarr"].writes


def test_dry_run_still_writes_nothing(tmp_path):
    s1, e1 = _many_specials(4, series_id=1, tvdb=7001)
    client = _Recorder([s1], {1: e1})
    res = sp.enforce_instance(client, dry_run=True)
    assert client.writes == []
    assert res["episodes_unmonitored"] == 4      # planned, not applied


def test_a_loud_run_escalates_the_notification(monkeypatch, tmp_path):
    """A mass unmonitor must not read like a Tuesday."""
    s1, e1 = _many_specials(sp.LOUD_UNMONITORS, series_id=1, tvdb=7001)
    clients = {"sonarr": _Recorder([s1], {1: e1}),
               "sonarr2": _Recorder([], {})}
    seen = []
    monkeypatch.setattr(sp, "ArrClient", lambda slug, ver: clients[slug])
    monkeypatch.setattr(sp, "EXCLUDE_PATH", tmp_path / "none.exclude")
    monkeypatch.setattr(sp, "_notify", lambda msg, level="info": seen.append(level))
    monkeypatch.setattr(sys, "argv", ["specials_policy.py", "--cron"])
    sp.main()
    assert "warning" in seen, seen


def test_a_small_run_stays_quiet(monkeypatch, tmp_path):
    """MUTATION PROOF for the escalation: routine convergence stays info."""
    s1, e1 = _many_specials(2, series_id=1, tvdb=7001)
    clients = {"sonarr": _Recorder([s1], {1: e1}),
               "sonarr2": _Recorder([], {})}
    seen = []
    monkeypatch.setattr(sp, "ArrClient", lambda slug, ver: clients[slug])
    monkeypatch.setattr(sp, "EXCLUDE_PATH", tmp_path / "none.exclude")
    monkeypatch.setattr(sp, "_notify", lambda msg, level="info": seen.append(level))
    monkeypatch.setattr(sys, "argv", ["specials_policy.py", "--cron"])
    sp.main()
    assert seen == ["info"], seen


def test_a_single_series_bigger_than_the_cap_still_converges():
    """FORWARD PROGRESS. A cap that defers the very first series would stall the
    janitor permanently on any series holding more specials than the cap."""
    s1, e1 = _many_specials(sp.MAX_UNMONITORS_PER_RUN + 10, series_id=1,
                            tvdb=7001, title="Enormous Show")
    client = _Recorder([s1], {1: e1})
    res = sp.enforce_instance(client, dry_run=False)
    assert res["deferred_count"] == 0, res
    assert res["episodes_unmonitored"] == sp.MAX_UNMONITORS_PER_RUN + 10
