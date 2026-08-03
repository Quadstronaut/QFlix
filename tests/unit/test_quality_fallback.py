"""Tests for scripts/mcp/quality_fallback.py."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "mcp"))

import quality_fallback as qf  # noqa: E402


def test_state_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    s = qf.load_state(p)
    assert s == {"movies": {}, "tv": {}}
    s["movies"]["radarr:100"] = {"days": 1}
    qf.save_state(p, s)
    assert qf.load_state(p) == s


def test_state_survives_corrupt_file(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json", encoding="utf-8")
    assert qf.load_state(p) == {"movies": {}, "tv": {}}


def test_parse_arr_ts_handles_z_suffix():
    dt = qf.parse_arr_ts("2026-06-06T09:00:06Z")
    assert dt == datetime(2026, 6, 6, 9, 0, 6, tzinfo=timezone.utc)
    assert qf.parse_arr_ts(None) is None
    assert qf.parse_arr_ts("") is None


# ---------------------------------------------------------------------------
# Profile builder
# ---------------------------------------------------------------------------

def _ladder_item(qid, name, allowed=False):
    return {"quality": {"id": qid, "name": name}, "items": [], "allowed": allowed}


def _group_item(gid, name, members, allowed=False):
    return {"id": gid, "name": name, "quality": None, "allowed": allowed,
            "items": [_ladder_item(i, n, allowed) for i, n in members]}


def _source_profile():
    # Shape mirrors deployed GET /qualityprofile/7 (trimmed formatItems).
    return {
        "id": 7, "name": "HD Bluray + WEB", "upgradeAllowed": True, "cutoff": 7,
        "minFormatScore": 0, "cutoffFormatScore": 10000, "minUpgradeFormatScore": 1,
        "language": {"id": -2, "name": "Original"},
        "formatItems": [{"format": 1, "name": "SomeCF", "score": 100}],
        "items": [
            _ladder_item(24, "WORKPRINT"), _ladder_item(25, "CAM"),
            _ladder_item(26, "TELESYNC"), _ladder_item(27, "TELECINE"),
            _ladder_item(29, "REGIONAL"), _ladder_item(28, "DVDSCR"),
            _ladder_item(1, "SDTV"), _ladder_item(2, "DVD"),
            _group_item(1000, "WEB 480p", [(8, "WEBDL-480p"), (12, "WEBRip-480p")]),
            _ladder_item(20, "Bluray-480p"),
            _ladder_item(4, "HDTV-720p"),
            _group_item(1001, "WEB 720p", [(5, "WEBDL-720p"), (14, "WEBRip-720p")]),
            _ladder_item(6, "Bluray-720p", allowed=True),
            _ladder_item(9, "HDTV-1080p"),
            _group_item(1002, "WEB 1080p", [(3, "WEBDL-1080p"), (15, "WEBRip-1080p")],
                        allowed=True),
            _ladder_item(7, "Bluray-1080p", allowed=True),
        ],
    }


def _allowed_names(profile):
    return {(i["quality"]["name"] if i.get("quality") else i["name"])
            for i in profile["items"] if i["allowed"]}


def test_build_fallback_hdtv_menu():
    p = qf.build_fallback_profile(_source_profile(), qf.FALLBACK_HDTV, qf.STAGE1_ALLOW)
    assert "id" not in p
    assert p["name"] == "QFlix Fallback HDTV"
    assert _allowed_names(p) == {"Bluray-720p", "WEB 1080p", "Bluray-1080p",
                                 "HDTV-720p", "HDTV-1080p", "WEB 720p"}
    assert p["cutoff"] == 7                      # copied, still an allowed id
    assert p["cutoffFormatScore"] == 10000       # CF config copied verbatim
    assert p["language"] == {"id": -2, "name": "Original"}


def test_build_fallback_sd_menu_regional_in_no_preretail():
    p = qf.build_fallback_profile(_source_profile(), qf.FALLBACK_SD, qf.STAGE2_ALLOW)
    names = _allowed_names(p)
    assert {"SDTV", "DVD", "WEB 480p", "Bluray-480p", "REGIONAL"} <= names
    assert names & {"CAM", "TELESYNC", "TELECINE", "DVDSCR", "WORKPRINT"} == set()


def test_build_fallback_group_members_follow_group():
    p = qf.build_fallback_profile(_source_profile(), qf.FALLBACK_SD, qf.STAGE2_ALLOW)
    web480 = next(i for i in p["items"] if i.get("name") == "WEB 480p")
    assert web480["allowed"] is True
    assert all(sub["allowed"] for sub in web480["items"])


def test_build_fallback_never_unbans_even_if_source_corrupt():
    src = _source_profile()
    for item in src["items"]:
        if item.get("quality") and item["quality"]["name"] == "CAM":
            item["allowed"] = True  # simulate a corrupted/edited source
    p = qf.build_fallback_profile(src, qf.FALLBACK_HDTV, qf.STAGE1_ALLOW)
    assert "CAM" not in _allowed_names(p)


# ---------------------------------------------------------------------------
# Movie planner
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 6, 8, 0, 0, tzinfo=timezone.utc)
TODAY = "2026-06-06"
FB = {"hdtv": 90, "sd": 91}


def mk_movie(mid=1, tmdb=None, profile=7, monitored=True, available=True,
             has_file=False, searched_hours_ago=1, title="Movie"):
    ts = (NOW - timedelta(hours=searched_hours_ago)).isoformat().replace("+00:00", "Z")
    return {"id": mid, "tmdbId": tmdb or (1000 + mid), "title": title,
            "monitored": monitored, "isAvailable": available, "hasFile": has_file,
            "qualityProfileId": profile, "lastSearchTime": ts}


def _run_plan(missing, movies=None, state=None):
    movies = movies if movies is not None else {m["id"]: m for m in missing}
    state = state if state is not None else {}
    return qf.plan_movies("radarr", missing, movies, FB, state, TODAY, NOW)


def test_day_accrues_once_per_day():
    m = mk_movie()
    state = {}
    _run_plan([m], state=state)
    _run_plan([m], state=state)  # same-day rerun
    assert state["radarr:1001"]["days"] == 1


def test_unreleased_and_stale_search_accrue_nothing():
    state = {}
    _run_plan([mk_movie(mid=1, available=False),
               mk_movie(mid=2, searched_hours_ago=72)], state=state)
    assert state == {}


def test_promote_at_threshold():
    m = mk_movie()
    state = {"radarr:1001": {"movie_id": 1, "days": 4, "stage": 0,
                             "original_profile_id": None,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = _run_plan([m], state=state)
    actions = [a for a in acts if a["action"] == "promote"]
    assert len(actions) == 1
    rec = state["radarr:1001"]
    assert rec["days"] == 5 and rec["stage"] == 1
    assert rec["original_profile_id"] == 7
    assert actions[0]["to_profile"] == FB["hdtv"] and actions[0]["movie_id"] == 1


def test_deepen_and_park():
    m = mk_movie(profile=FB["hdtv"])
    state = {"radarr:1001": {"movie_id": 1, "days": 9, "stage": 1,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = _run_plan([m], state=state)
    assert [a["action"] for a in acts] == ["deepen"]
    assert acts[0]["to_profile"] == FB["sd"]

    m2 = mk_movie(profile=FB["sd"])
    state2 = {"radarr:1001": {"movie_id": 1, "days": 14, "stage": 2,
                              "original_profile_id": 7,
                              "last_counted": "2026-06-05", "parked": False,
                              "title": "Movie"}}
    acts2 = _run_plan([m2], state=state2)
    assert [a["action"] for a in acts2] == ["park"]
    assert acts2[0]["to_profile"] == 7      # restore original
    assert state2["radarr:1001"]["parked"] is True


def test_grab_at_fallback_restores_original():
    grabbed = mk_movie(has_file=True, profile=FB["hdtv"])
    state = {"radarr:1001": {"movie_id": 1, "days": 6, "stage": 1,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = qf.plan_movies("radarr", [], {1: grabbed}, FB, state, TODAY, NOW)
    assert [a["action"] for a in acts] == ["restore_grabbed"]
    assert acts[0]["to_profile"] == 7
    assert "radarr:1001" not in state


def test_stage0_grab_drops_silently():
    grabbed = mk_movie(has_file=True)
    state = {"radarr:1001": {"movie_id": 1, "days": 3, "stage": 0,
                             "original_profile_id": None,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = qf.plan_movies("radarr", [], {1: grabbed}, FB, state, TODAY, NOW)
    assert acts == []
    assert state == {}


def test_operator_profile_change_is_hands_off():
    moved = mk_movie(profile=42)  # operator picked some other profile
    state = {"radarr:1001": {"movie_id": 1, "days": 6, "stage": 1,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = qf.plan_movies("radarr", [moved], {1: moved}, FB, state, TODAY, NOW)
    assert acts == []           # no restore — operator owns it now
    assert state == {}


def test_operator_unmonitor_mid_fallback_restores():
    um = mk_movie(monitored=False, profile=FB["hdtv"])
    state = {"radarr:1001": {"movie_id": 1, "days": 6, "stage": 1,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = qf.plan_movies("radarr", [], {1: um}, FB, state, TODAY, NOW)
    assert [a["action"] for a in acts] == ["restore_operator"]
    assert "radarr:1001" not in state


def test_deleted_movie_drops_but_says_so_when_it_was_in_fallback():
    """A movie that genuinely leaves radarr while still on a fallback profile
    cannot be restored -- it is gone -- but dropping the record threw away the
    only copy of its real profile id in SILENCE. Report-only action, warning
    level, no write."""
    state = {"radarr:1001": {"movie_id": 1, "days": 6, "stage": 1,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = qf.plan_movies("radarr", [], {}, FB, state, TODAY, NOW)
    assert [a["action"] for a in acts] == ["orphaned_in_fallback"]
    assert acts[0]["to_profile"] == 7
    assert state == {}


def test_deleted_stage_zero_movie_drops_silently():
    """At stage 0 nothing was changed in radarr, so there is nothing to say."""
    state = {"radarr:1001": {"movie_id": 1, "days": 2, "stage": 0,
                             "original_profile_id": None,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = qf.plan_movies("radarr", [], {}, FB, state, TODAY, NOW)
    assert acts == []
    assert state == {}


def test_parked_remonitored_restarts_cycle():
    back = mk_movie(profile=7)
    state = {"radarr:1001": {"movie_id": 1, "days": 15, "stage": 2,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-01", "parked": True,
                             "title": "Movie"}}
    qf.plan_movies("radarr", [back], {1: back}, FB, state, TODAY, NOW)
    rec = state["radarr:1001"]
    assert rec["parked"] is False and rec["stage"] == 0 and rec["days"] == 1


def test_cap_blocks_26th_promotion():
    missing, state = [], {}
    for i in range(1, 26):  # 25 already in fallback
        m = mk_movie(mid=i, profile=FB["hdtv"])
        missing.append(m)
        state[f"radarr:{1000+i}"] = {"movie_id": i, "days": 6, "stage": 1,
                                     "original_profile_id": 7,
                                     "last_counted": "2026-06-05",
                                     "parked": False, "title": f"M{i}"}
    waiting = mk_movie(mid=26)
    missing.append(waiting)
    state["radarr:1026"] = {"movie_id": 26, "days": 4, "stage": 0,
                            "original_profile_id": None,
                            "last_counted": "2026-06-05", "parked": False,
                            "title": "M26"}
    movies = {m["id"]: m for m in missing}
    acts = qf.plan_movies("radarr", missing, movies, FB, state, TODAY, NOW)
    assert "promote" not in [a["action"] for a in acts]
    assert state["radarr:1026"]["days"] == 5      # still counts while waiting
    assert state["radarr:1026"]["stage"] == 0


# ---------------------------------------------------------------------------
# TV planner (park-only v2 — day-5 digest, day-15 unmonitor)
# ---------------------------------------------------------------------------

def mk_episode(eid=1, series_id=10, aired_days_ago=30, monitored=True,
               searched_hours_ago=1, season=1, ep=1, title="Ep"):
    aired = (NOW - timedelta(days=aired_days_ago)).isoformat().replace("+00:00", "Z")
    ts = (NOW - timedelta(hours=searched_hours_ago)).isoformat().replace("+00:00", "Z")
    return {"id": eid, "seriesId": series_id, "title": title,
            "seasonNumber": season, "episodeNumber": ep, "monitored": monitored,
            "airDateUtc": aired, "lastSearchTime": ts}


def test_tv_digest_fires_once_at_threshold():
    e = mk_episode()
    state = {"sonarr:1": {"days": 4, "last_counted": "2026-06-05",
                          "alerted": False, "parked": False}}
    plan = qf.plan_tv("sonarr", [e], state, TODAY, NOW)
    assert len(plan["digest"]) == 1
    assert plan["digest"][0]["series_id"] == 10 and plan["digest"][0]["episode_id"] == 1
    assert state["sonarr:1"]["alerted"] is True
    # next day: no repeat
    plan2 = qf.plan_tv("sonarr", [e], state, "2026-06-07", NOW + timedelta(days=1))
    assert plan2["digest"] == []


def test_tv_unaired_and_unmonitored_skipped():
    state = {}
    plan = qf.plan_tv("sonarr", [mk_episode(eid=1, aired_days_ago=-2),
                                 mk_episode(eid=2, monitored=False)],
                      state, TODAY, NOW)
    assert plan["digest"] == [] and plan["parks"] == [] and state == {}


def test_tv_grabbed_episode_pruned():
    state = {"sonarr:1": {"days": 6, "last_counted": "2026-06-05",
                          "alerted": True, "parked": False}}
    qf.plan_tv("sonarr", [], state, TODAY, NOW)
    assert state == {}


def test_tv_parks_at_day_15_and_sets_flag():
    e = mk_episode()
    state = {"sonarr:1": {"days": 14, "last_counted": "2026-06-05",
                          "alerted": True, "parked": False}}
    plan = qf.plan_tv("sonarr", [e], state, TODAY, NOW)
    assert len(plan["parks"]) == 1
    p = plan["parks"][0]
    assert p["episode_id"] == 1 and p["days"] == 15
    assert state["sonarr:1"]["parked"] is True


def test_tv_park_fires_once_never_repeats():
    e = mk_episode()
    state = {"sonarr:1": {"days": 20, "last_counted": "2026-06-05",
                          "alerted": True, "parked": True}}
    plan = qf.plan_tv("sonarr", [e], state, TODAY, NOW)
    assert plan["parks"] == []


def test_tv_season0_never_counted_digested_or_parked():
    e = mk_episode(season=0)          # a special that slipped past the janitor
    state = {}
    plan = qf.plan_tv("sonarr", [e], state, TODAY, NOW)
    assert plan["digest"] == [] and plan["parks"] == []
    assert state == {}               # never even accrues a day


def test_tv_park_blast_cap_defers_overflow():
    missing, state = [], {}
    for i in range(1, qf.MAX_TV_PARKS_PER_RUN + 3):
        missing.append(mk_episode(eid=i, ep=i))
        state[f"sonarr:{i}"] = {"days": 14, "last_counted": "2026-06-05",
                                "alerted": True, "parked": False}
    plan = qf.plan_tv("sonarr", missing, state, TODAY, NOW)
    assert len(plan["parks"]) == qf.MAX_TV_PARKS_PER_RUN
    parked = sum(1 for r in state.values() if r["parked"])
    assert parked == qf.MAX_TV_PARKS_PER_RUN     # overflow still parked=False


# ---------------------------------------------------------------------------
# API layer / run() / bootstrap — FakeClient, no urllib
# ---------------------------------------------------------------------------

class FakeClient:
    """Minimal ArrClient stand-in: canned GET routes, records writes."""
    def __init__(self, routes):
        self.routes = routes          # {(method, path_prefix): (code, body)}
        self.writes = []              # [(method, path, body)]

    def _find(self, method, path):
        for (m, p), resp in self.routes.items():
            if m == method and path.startswith(p):
                return resp
        return (404, {"error": f"no route {method} {path}"})

    def get(self, path, **kw):
        return self._find("GET", path)

    def post(self, path, *, body=None, **kw):
        self.writes.append(("POST", path, body))
        return self._find("POST", path)

    def put(self, path, *, body=None, **kw):
        self.writes.append(("PUT", path, body))
        return self._find("PUT", path)


def _radarr_routes(missing, movies, profiles=None):
    profiles = profiles if profiles is not None else [
        {"id": 7, "name": "HD Bluray + WEB"},
        {"id": 90, "name": qf.FALLBACK_HDTV},
        {"id": 91, "name": qf.FALLBACK_SD},
    ]
    return {
        ("GET", "/qualityprofile"): (200, profiles),
        ("GET", "/wanted/missing"): (200, {"records": missing,
                                           "totalRecords": len(missing)}),
        ("GET", "/movie"): (200, movies),
        ("PUT", "/movie/editor"): (202, []),
        ("POST", "/command"): (201, {"id": 555}),
    }


def _empty_tv_routes():
    return {("GET", "/wanted/missing"): (200, {"records": [], "totalRecords": 0})}


def _tv_routes(missing, series=None):
    return {
        ("GET", "/wanted/missing"): (200, {"records": missing,
                                           "totalRecords": len(missing)}),
        ("GET", "/series"): (200, series if series is not None else []),
        ("PUT", "/episode/monitor"): (202, []),
    }


def _mk_clients(radarr_routes):
    return {"radarr": FakeClient(radarr_routes),
            "radarr2": FakeClient(_radarr_routes([], [])),
            "sonarr": FakeClient(_empty_tv_routes()),
            "sonarr2": FakeClient(_empty_tv_routes())}


def _isolate_notify(monkeypatch, tmp_path):
    # Keep lib.notify audit logs out of the real ~/.opt/maint during tests.
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path / "maint-state"))


def test_run_promotes_and_searches(tmp_path, monkeypatch):
    _isolate_notify(monkeypatch, tmp_path)
    m = mk_movie()
    clients = _mk_clients(_radarr_routes([m], [m]))
    state_path = tmp_path / "state.json"
    state = {"movies": {"radarr:1001": {"movie_id": 1, "days": 4, "stage": 0,
                                        "original_profile_id": None,
                                        "last_counted": "2026-06-05",
                                        "parked": False, "title": "Movie"}},
             "tv": {}}
    qf.save_state(state_path, state)
    res = qf.run(client_factory=lambda slug: clients[slug],
                 state_path=state_path, now=NOW, dry_run=False)
    r = clients["radarr"]
    put_idx = r.writes.index(("PUT", "/movie/editor",
                              {"movieIds": [1], "qualityProfileId": 90,
                               "moveFiles": False}))
    post_idx = r.writes.index(("POST", "/command",
                               {"name": "MoviesSearch", "movieIds": [1]}))
    assert put_idx < post_idx          # profile swap lands BEFORE the search
    assert res["per_arr"]["radarr"]["actions"][0]["action"] == "promote"
    assert qf.load_state(state_path)["movies"]["radarr:1001"]["stage"] == 1


def test_run_dry_run_writes_nothing(tmp_path, monkeypatch):
    _isolate_notify(monkeypatch, tmp_path)
    m = mk_movie()
    clients = _mk_clients(_radarr_routes([m], [m]))
    state_path = tmp_path / "state.json"
    state = {"movies": {"radarr:1001": {"movie_id": 1, "days": 4, "stage": 0,
                                        "original_profile_id": None,
                                        "last_counted": "2026-06-05",
                                        "parked": False, "title": "Movie"}},
             "tv": {}}
    qf.save_state(state_path, state)
    qf.run(client_factory=lambda slug: clients[slug],
           state_path=state_path, now=NOW, dry_run=True)
    assert clients["radarr"].writes == []
    # state untouched on dry-run
    assert qf.load_state(state_path)["movies"]["radarr:1001"]["days"] == 4


def test_run_skips_instance_missing_fallback_profiles(tmp_path, monkeypatch):
    _isolate_notify(monkeypatch, tmp_path)
    m = mk_movie()
    routes = _radarr_routes([m], [m], profiles=[{"id": 7, "name": "HD Bluray + WEB"}])
    clients = _mk_clients(routes)
    state_path = tmp_path / "state.json"
    res = qf.run(client_factory=lambda slug: clients[slug],
                 state_path=state_path, now=NOW, dry_run=False)
    assert res["per_arr"]["radarr"]["status"] == "skipped-no-fallback-profiles"
    assert clients["radarr"].writes == []


def test_run_tv_parks_and_unmonitors(tmp_path, monkeypatch):
    _isolate_notify(monkeypatch, tmp_path)
    e = mk_episode(eid=1, series_id=10, season=1, ep=3, title="Ep3")
    clients = {"radarr": FakeClient(_radarr_routes([], [])),
               "radarr2": FakeClient(_radarr_routes([], [])),
               "sonarr": FakeClient(_tv_routes([e], series=[{"id": 10,
                                                             "title": "Show"}])),
               "sonarr2": FakeClient(_empty_tv_routes())}
    state_path = tmp_path / "state.json"
    state = {"movies": {},
             "tv": {"sonarr:1": {"days": 14, "last_counted": "2026-06-05",
                                 "alerted": True, "parked": False}}}
    qf.save_state(state_path, state)
    res = qf.run(client_factory=lambda slug: clients[slug],
                 state_path=state_path, now=NOW, dry_run=False)
    assert ("PUT", "/episode/monitor",
            {"episodeIds": [1], "monitored": False}) in clients["sonarr"].writes
    assert len(res["tv_parks"]) == 1 and res["tv_parks"][0]["ok"] is True
    assert qf.load_state(state_path)["tv"]["sonarr:1"]["parked"] is True


def test_run_tv_park_rolls_back_on_failed_unmonitor(tmp_path, monkeypatch):
    # D2: if the unmonitor PUT fails, parked must roll back so the next run
    # retries — never persist parked=True on a write that did not land.
    _isolate_notify(monkeypatch, tmp_path)
    e = mk_episode(eid=1, series_id=10, season=1, ep=3, title="Ep3")
    routes = _tv_routes([e], series=[{"id": 10, "title": "Show"}])
    routes[("PUT", "/episode/monitor")] = (500, {"error": "boom"})
    clients = {"radarr": FakeClient(_radarr_routes([], [])),
               "radarr2": FakeClient(_radarr_routes([], [])),
               "sonarr": FakeClient(routes),
               "sonarr2": FakeClient(_empty_tv_routes())}
    state_path = tmp_path / "state.json"
    state = {"movies": {},
             "tv": {"sonarr:1": {"days": 14, "last_counted": "2026-06-05",
                                 "alerted": True, "parked": False}}}
    qf.save_state(state_path, state)
    res = qf.run(client_factory=lambda slug: clients[slug],
                 state_path=state_path, now=NOW, dry_run=False)
    assert res["tv_parks"][0]["ok"] is False
    assert qf.load_state(state_path)["tv"]["sonarr:1"]["parked"] is False


def test_run_had_failures_flags_failed_tv_park():
    # D3: a failed TV park must drive the process exit code (Kuma/systemd red),
    # not just movies.
    assert qf._run_had_failures(
        {"per_arr": {"radarr": {"status": "ok", "actions": []}},
         "tv_parks": [{"episode_id": 1, "ok": False}]}) is True
    assert qf._run_had_failures(
        {"per_arr": {"radarr": {"status": "ok", "actions": []}},
         "tv_parks": [{"episode_id": 1, "ok": True}]}) is False


def test_run_tv_dry_run_no_unmonitor(tmp_path, monkeypatch):
    _isolate_notify(monkeypatch, tmp_path)
    e = mk_episode(eid=1, series_id=10, season=1, ep=3, title="Ep3")
    clients = {"radarr": FakeClient(_radarr_routes([], [])),
               "radarr2": FakeClient(_radarr_routes([], [])),
               "sonarr": FakeClient(_tv_routes([e], series=[{"id": 10,
                                                             "title": "Show"}])),
               "sonarr2": FakeClient(_empty_tv_routes())}
    state_path = tmp_path / "state.json"
    state = {"movies": {},
             "tv": {"sonarr:1": {"days": 14, "last_counted": "2026-06-05",
                                 "alerted": True, "parked": False}}}
    qf.save_state(state_path, state)
    qf.run(client_factory=lambda slug: clients[slug],
           state_path=state_path, now=NOW, dry_run=True)
    assert clients["sonarr"].writes == []
    assert qf.load_state(state_path)["tv"]["sonarr:1"]["parked"] is False


def test_bootstrap_creates_and_updates():
    src = _source_profile()
    routes = {
        ("GET", "/qualityprofile/7"): (200, src),
        ("GET", "/qualityprofile"): (200, [
            {"id": 7, "name": "HD Bluray + WEB"},
            {"id": 90, "name": qf.FALLBACK_HDTV},   # exists -> PUT
        ]),
        ("POST", "/qualityprofile"): (201, {"id": 91}),
        ("PUT", "/qualityprofile/90"): (202, {"id": 90}),
    }
    client = FakeClient(routes)
    ids = qf.bootstrap_profiles("radarr", client)
    assert ids == {qf.FALLBACK_HDTV: 90, qf.FALLBACK_SD: 91}
    methods = [(m, p) for (m, p, _) in client.writes]
    assert ("PUT", "/qualityprofile/90") in methods
    assert ("POST", "/qualityprofile") in methods
    # every written profile keeps the ban
    for _, _, body in client.writes:
        for item in body["items"]:
            nm = item["quality"]["name"] if item.get("quality") else item["name"]
            if nm in qf.BANNED:
                assert item["allowed"] is False


# ---------------------------------------------------------------------------
# A failed fetch must not look like an empty one (arbiter fix 2026-08-03)
# ---------------------------------------------------------------------------

def test_tv_fetch_failure_does_not_wipe_the_day_counters(tmp_path, monkeypatch):
    """THE DEFECT: _fetch_paged swallowed every non-200 and returned [], so a
    transient 500 from Sonarr on /wanted/missing was indistinguishable from
    "nothing is missing". plan_tv then pruned every state key not in the
    fetched set -- wiping ALL TV day counters and alerted flags for that
    instance -- and the run still exited 0 / Kuma green.

    Measured on the real module before the fix, with radarr/radarr2 healthy and
    only the sonarrs answering 500:
        TV before        {'sonarr2:7001': 13, 'sonarr:9001': 14, 'sonarr:9002': 9}
        TV after         {}
        _run_had_failures  False
    The wipe was durable -- save_state persisted the emptied dict."""
    _isolate_notify(monkeypatch, tmp_path)
    broken = {("GET", "/wanted/missing"): (500, {"error": "boom"})}
    clients = {"radarr": FakeClient(_radarr_routes([], [])),
               "radarr2": FakeClient(_radarr_routes([], [])),
               "sonarr": FakeClient(broken),
               "sonarr2": FakeClient(_empty_tv_routes())}
    state_path = tmp_path / "state.json"
    before = {"movies": {},
              "tv": {"sonarr:9001": {"days": 14, "last_counted": "2026-06-05",
                                     "alerted": True, "parked": False},
                     "sonarr:9002": {"days": 9, "last_counted": "2026-06-05",
                                     "alerted": True, "parked": False}}}
    qf.save_state(state_path, before)
    res = qf.run(client_factory=lambda slug: clients[slug],
                 state_path=state_path, now=NOW, dry_run=False)

    after = qf.load_state(state_path)["tv"]
    assert after == before["tv"], ("counters were pruned on unverified data",
                                   after)
    assert res["tv_per_arr"]["sonarr"]["status"] == "failed-wanted-missing"
    assert qf._run_had_failures(res) is True, res
    assert clients["sonarr"].writes == []


def test_a_healthy_tv_instance_still_prunes(tmp_path, monkeypatch):
    """MUTATION PROOF. The prune is correct behaviour on VERIFIED data -- an
    episode that was grabbed must leave the state -- so the fix must not be a
    blanket "never prune"."""
    _isolate_notify(monkeypatch, tmp_path)
    clients = {"radarr": FakeClient(_radarr_routes([], [])),
               "radarr2": FakeClient(_radarr_routes([], [])),
               "sonarr": FakeClient(_tv_routes([])),
               "sonarr2": FakeClient(_empty_tv_routes())}
    state_path = tmp_path / "state.json"
    qf.save_state(state_path, {"movies": {}, "tv": {
        "sonarr:9001": {"days": 14, "last_counted": "2026-06-05",
                        "alerted": True, "parked": False}}})
    res = qf.run(client_factory=lambda slug: clients[slug],
                 state_path=state_path, now=NOW, dry_run=False)
    assert qf.load_state(state_path)["tv"] == {}
    assert qf._run_had_failures(res) is False, res


def test_movie_wanted_missing_failure_is_also_a_failure(tmp_path, monkeypatch):
    _isolate_notify(monkeypatch, tmp_path)
    routes = dict(_radarr_routes([], []))
    routes[("GET", "/wanted/missing")] = (503, {"error": "boom"})
    clients = _mk_clients(routes)
    res = qf.run(client_factory=lambda slug: clients[slug],
                 state_path=tmp_path / "state.json", now=NOW, dry_run=False)
    assert res["per_arr"]["radarr"]["status"] == "failed-wanted-missing"
    assert qf._run_had_failures(res) is True
    assert clients["radarr"].writes == []


def test_a_mid_pagination_failure_is_not_a_complete_answer():
    """Partial data must never be treated as the whole set -- it is the prune
    input."""
    class _Paging:
        def __init__(self):
            self.calls = 0

        def get(self, path, **kw):
            self.calls += 1
            if self.calls == 1:
                return 200, {"records": [{"id": 1}], "totalRecords": 5}
            return 500, {"error": "boom"}

    ok, records = qf._fetch_paged(_Paging(), "/wanted/missing", page_size=1)
    assert ok is False
    assert len(records) == 1


# ---------------------------------------------------------------------------
# Re-add must not strand a movie on a fallback profile (arbiter fix)
# ---------------------------------------------------------------------------

def test_a_readded_movie_keeps_its_stage_and_original_profile():
    """THE DEFECT: Phase 1 looked the record up by movie_id only, so a
    delete-and-re-add (operator cleanup, a declined/re-made Seerr request)
    dropped a stage-2 record while the movie was still sitting on QFlix
    Fallback SD. original_profile_id -- the only record of its real profile --
    went with it, Phase 2 re-created the record at stage 0 in the SAME run, and
    five days later recorded the FALLBACK profile as the original. Every future
    restore then pinned the movie to SD.

    Reproduced end to end before the fix: real profile 4, radarr on 12, the
    module recorded original_profile_id=12."""
    readded = mk_movie(mid=981, tmdb=603, profile=91)   # new id, still on SD
    state = {"radarr:603": {"movie_id": 500, "days": 12, "stage": 2,
                            "original_profile_id": 4,
                            "last_counted": "2026-06-05", "parked": False,
                            "title": "The Matrix"}}
    acts = qf.plan_movies("radarr", [readded], {981: readded}, FB, state,
                          TODAY, NOW)
    rec = state["radarr:603"]
    assert rec["stage"] == 2, "the re-add reset the stage"
    assert rec["original_profile_id"] == 4, "the real profile was lost"
    assert rec["movie_id"] == 981, "the record still points at the dead id"
    assert [a["action"] for a in acts] == [], acts


def test_the_readd_fix_does_not_resurrect_a_genuine_deletion():
    """MUTATION PROOF: same record, movie absent from radarr entirely."""
    state = {"radarr:603": {"movie_id": 500, "days": 12, "stage": 2,
                            "original_profile_id": 4,
                            "last_counted": "2026-06-05", "parked": False,
                            "title": "The Matrix"}}
    acts = qf.plan_movies("radarr", [], {}, FB, state, TODAY, NOW)
    assert state == {}
    assert [a["action"] for a in acts] == ["orphaned_in_fallback"]


# ---------------------------------------------------------------------------
# --emit-json is READ-ONLY (arbiter fix)
# ---------------------------------------------------------------------------

def test_emit_json_issues_no_arr_writes(tmp_path, monkeypatch):
    """Everywhere else under scripts/mcp/ --emit-json means "read and print
    JSON". Here it ran the full LIVE mutation path and always exited 0."""
    _isolate_notify(monkeypatch, tmp_path)
    due = mk_movie(mid=1, profile=7)
    clients = _mk_clients(_radarr_routes([due], [due]))
    state_path = tmp_path / "state.json"
    qf.save_state(state_path, {"movies": {
        "radarr:1001": {"movie_id": 1, "days": 4, "stage": 0,
                        "original_profile_id": None,
                        "last_counted": "2026-06-05", "parked": False,
                        "title": "Movie"}}, "tv": {}})
    monkeypatch.setattr(qf, "ArrClient", lambda slug, ver: clients[slug])
    monkeypatch.setattr(qf, "STATE_PATH", state_path)
    monkeypatch.setattr(sys, "argv", ["quality_fallback.py", "--emit-json"])
    rc = qf.main()
    assert rc == 0
    assert all(c.writes == [] for c in clients.values()), {
        s: c.writes for s, c in clients.items()}
    # And the counters must not have advanced on disk either.
    assert qf.load_state(state_path)["movies"]["radarr:1001"]["days"] == 4
