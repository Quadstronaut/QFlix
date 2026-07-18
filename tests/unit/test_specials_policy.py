"""Tests for scripts/mcp/specials_policy.py — Season-0 specials janitor.

Stateless, convergent enforcement of "Season 0 is never monitored on QFlix":
unmonitor any monitored S00 episode AND clear the Season-0 season flag (the flag
clear is what makes it durable — a series refresh re-monitors episodes to match
the season flag). No network: a seriesId-aware FakeClient stands in for ArrClient.
"""
from __future__ import annotations

from pathlib import Path
import sys

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
