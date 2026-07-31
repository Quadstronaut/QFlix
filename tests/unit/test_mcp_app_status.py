"""Tests for scripts/mcp/app_status.py — Heartbeat v2 server aggregator.

Fixtures below are built to reproduce the plan's own worked example numbers
(docs/superpowers/plans/2026-07-15-heartbeat-android.md) wherever possible,
so a passing test is also a direct check against the documented contract:
  - quota -s: used=2073G quota=2794G -> pct 74.2
  - app-traffic info: available 96.58% -> used_pct 3.42
  - Tautulli get_activity: stream_count=3, 2 distinct user_id, 1 transcode,
    wan_bandwidth=12000 -> streams:3 users:2 transcodes:1 wan_kbps:12000
  - Tautulli get_home_stats: BAsylum total_duration=281520s -> hours 78.2
  - stale-state.json hash f0a3658d... -> hash8 "f0a3658d"
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "mcp"))

import app_status  # noqa: E402


UTC = dt.timezone.utc


def _now():
    return dt.datetime(2026, 7, 15, 20, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# parse_quota
# ---------------------------------------------------------------------------

QUOTA_TEXT_INLINE = """\
Disk quotas for user quadstronaut (uid 1013):
     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace
  /dev/mapper/data-vg-data 2073G   2794G   2794G           184213       0       0
"""

QUOTA_TEXT_WRAPPED = """\
Disk quotas for user quadstronaut (uid 1013):
     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace
  /dev/mapper/data-vg-data
                2073G   2794G   2794G           184213       0       0
"""

QUOTA_TEXT_NO_ROW = "quota: no limits found for user quadstronaut\n"


def test_parse_quota_inline_layout():
    out = app_status.parse_quota(QUOTA_TEXT_INLINE)
    assert out == {"used_gb": 2073, "total_gb": 2794, "pct": 74.2}


def test_parse_quota_wrapped_layout():
    """Long device paths push the numeric columns to the next line."""
    out = app_status.parse_quota(QUOTA_TEXT_WRAPPED)
    assert out == {"used_gb": 2073, "total_gb": 2794, "pct": 74.2}


def test_parse_quota_no_row_raises():
    import pytest
    with pytest.raises(ValueError):
        app_status.parse_quota(QUOTA_TEXT_NO_ROW)


# ---------------------------------------------------------------------------
# parse_traffic
# ---------------------------------------------------------------------------

TRAFFIC_TEXT = """\
Traffic information for quadstronaut
-------------------------------------
Traffic available: 96.58%
Last traffic reset: 2026-06-28 00:00:00
Next traffic reset: 2026-07-28 00:00:00
"""

TRAFFIC_TEXT_NO_MATCH = "app-traffic: command not found\n"


def test_parse_traffic():
    out = app_status.parse_traffic(TRAFFIC_TEXT)
    assert out == {
        "used_pct": 3.42,
        "available_pct": 96.58,
        "last_reset": "2026-06-28T00:00:00",
        "next_reset": "2026-07-28T00:00:00",
    }


def test_parse_traffic_no_match_raises():
    import pytest
    with pytest.raises(ValueError):
        app_status.parse_traffic(TRAFFIC_TEXT_NO_MATCH)


# ---------------------------------------------------------------------------
# _collect_quota — disk/bandwidth parsers are independent: a failure in one
# must not discard an already-fetched, successfully-parsed reading from the
# other (regression for the asymmetry where a disk parse failure used to
# blank bandwidth too, even though `app-traffic info` had already run).
# ---------------------------------------------------------------------------

class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout


def _fake_subprocess_run(quota_stdout, traffic_stdout):
    def _run(cmd, **kwargs):
        if cmd[0] == "quota":
            return _FakeCompleted(quota_stdout)
        if cmd[0] == "app-traffic":
            return _FakeCompleted(traffic_stdout)
        raise AssertionError("unexpected subprocess.run call: {}".format(cmd))
    return _run


def test_collect_quota_disk_parse_failure_preserves_bandwidth(monkeypatch):
    """A malformed `quota -s` output must not discard the already-fetched,
    successfully-parsed `app-traffic info` reading."""
    monkeypatch.setattr(app_status.subprocess, "run",
                         _fake_subprocess_run(QUOTA_TEXT_NO_ROW, TRAFFIC_TEXT))
    section = app_status._collect_quota()
    assert section["ok"] is False
    assert "quota_parse" in section["error"]
    assert "traffic_parse" not in section["error"]
    assert section["disk"] is None
    assert section["bandwidth"] == {
        "used_pct": 3.42,
        "available_pct": 96.58,
        "last_reset": "2026-06-28T00:00:00",
        "next_reset": "2026-07-28T00:00:00",
    }


def test_collect_quota_bandwidth_parse_failure_preserves_disk(monkeypatch):
    """Symmetric case: a malformed `app-traffic info` output must not
    discard the already-parsed disk reading."""
    monkeypatch.setattr(app_status.subprocess, "run",
                         _fake_subprocess_run(QUOTA_TEXT_INLINE, TRAFFIC_TEXT_NO_MATCH))
    section = app_status._collect_quota()
    assert section["ok"] is False
    assert "traffic_parse" in section["error"]
    assert "quota_parse" not in section["error"]
    assert section["disk"] == {"used_gb": 2073, "total_gb": 2794, "pct": 74.2}
    assert section["bandwidth"] is None


def test_collect_quota_both_parsers_fail_reports_both(monkeypatch):
    monkeypatch.setattr(app_status.subprocess, "run",
                         _fake_subprocess_run(QUOTA_TEXT_NO_ROW, TRAFFIC_TEXT_NO_MATCH))
    section = app_status._collect_quota()
    assert section["ok"] is False
    assert "quota_parse" in section["error"]
    assert "traffic_parse" in section["error"]
    assert section["disk"] is None
    assert section["bandwidth"] is None


# ---------------------------------------------------------------------------
# classify_qbit — state vocabulary incl. qBit5 stoppedDL rename
# ---------------------------------------------------------------------------

def _t(state, **overrides):
    base = {"hash": "h", "name": "n", "state": state}
    base.update(overrides)
    return base


def test_classify_qbit_all_buckets():
    torrents = [
        _t("downloading"), _t("forcedDL"),         # active x2
        _t("stalledDL"),                            # stalled_dl x1
        _t("error"), _t("missingFiles"),            # errored x2
        _t("stoppedDL"), _t("pausedDL"),             # stopped_dl x2 (see below)
        _t("uploading"),                             # seeding x1
        _t("checkingDL"),                            # unclassified — counts to total only
    ]
    out = app_status.classify_qbit(torrents)
    assert out["total"] == 9
    assert out["active"] == 2
    assert out["stalled_dl"] == 1
    assert out["errored"] == 2
    assert out["stopped_dl"] == 2
    assert out["seeding"] == 1


def test_classify_qbit_stoppedDL_rename_equivalence():
    """qBit 5.x renamed pausedDL -> stoppedDL. Both spellings must land in
    the same bucket so the classifier works unchanged across the upgrade."""
    legacy = app_status.classify_qbit([_t("pausedDL"), _t("pausedDL")])
    renamed = app_status.classify_qbit([_t("stoppedDL"), _t("stoppedDL")])
    assert legacy["stopped_dl"] == renamed["stopped_dl"] == 2


def test_classify_qbit_empty():
    out = app_status.classify_qbit([])
    assert out == {"total": 0, "active": 0, "stalled_dl": 0, "errored": 0,
                    "stopped_dl": 0, "seeding": 0, "dl_bps": 0, "up_bps": 0}


# ---------------------------------------------------------------------------
# transfer rates — the Heartbeat downloads card showed SAB's kbps and NO qBit
# rate at all, so "is anything actually moving" was unanswerable for the
# torrent half. Summed here from the per-torrent fields already in the
# torrents/info payload rather than a second API call, because the
# forced-command SSH channel pays this script's latency on every refresh.
# ---------------------------------------------------------------------------

def test_rates_are_summed_across_torrents():
    out = app_status.classify_qbit([
        _t("downloading", dlspeed=1_500_000, upspeed=10_000),
        _t("downloading", dlspeed=500_000, upspeed=5_000),
        _t("uploading", dlspeed=0, upspeed=250_000),
    ])
    assert out["dl_bps"] == 2_000_000
    assert out["up_bps"] == 265_000


def test_missing_or_garbage_rate_fields_do_not_break_the_counts():
    """A rate is a nice-to-have; the counts are what the card is FOR. A torrent
    with an absent or non-numeric speed must contribute 0 to the rate and still
    be counted in its bucket."""
    out = app_status.classify_qbit([
        _t("downloading"),                              # no speed keys at all
        _t("downloading", dlspeed=None, upspeed=None),
        _t("downloading", dlspeed="not-a-number"),
        _t("downloading", dlspeed=1_000),
    ])
    assert out["active"] == 4, "a bad speed field cost us a state count"
    assert out["dl_bps"] == 1_000
    assert out["up_bps"] == 0


def test_rates_are_reported_even_when_nothing_is_active():
    """Seeding-only is the steady state on this box; upload rate must survive."""
    out = app_status.classify_qbit([_t("stalledUP", dlspeed=0, upspeed=42_000)])
    assert out["active"] == 0 and out["seeding"] == 1
    assert out["up_bps"] == 42_000


# ---------------------------------------------------------------------------
# top5_requests — Seerr 30d window filter + displayName fallback
# ---------------------------------------------------------------------------

def _seerr_request(rid, days_ago, requested_by):
    created = (_now() - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"id": rid, "createdAt": created, "requestedBy": requested_by}


SEERR_REQUESTS_JSON = {
    "pageInfo": {"pages": 1, "pageSize": 200, "results": 15, "page": 1},
    "results": (
        # sarahvanpelt: 12 requests within 30d window
        [_seerr_request(i, 5, {"id": 5, "displayName": "sarahvanpelt",
                                "plexUsername": "sarah_vp"})
         for i in range(12)]
        # one request just outside the 30d window — excluded
        + [_seerr_request(100, 31, {"id": 5, "displayName": "sarahvanpelt",
                                     "plexUsername": "sarah_vp"})]
        # a user with no displayName set — falls back to plexUsername
        + [_seerr_request(101, 2, {"id": 9, "displayName": None,
                                    "plexUsername": "BAsylum"})]
        # a user missing requestedBy entirely — falls back to "unknown"
        + [dict(id=102, createdAt=(_now() - dt.timedelta(days=1))
                .strftime("%Y-%m-%dT%H:%M:%S.000Z"))]
    ),
}


def test_top5_requests_30d_window_and_count():
    out = app_status.top5_requests(SEERR_REQUESTS_JSON, _now())
    sarah = next(e for e in out if e["user"] == "sarahvanpelt")
    assert sarah["count"] == 12  # the 31-days-ago one is excluded


def test_top5_requests_displayname_fallback_to_plexusername():
    out = app_status.top5_requests(SEERR_REQUESTS_JSON, _now())
    basylum = next(e for e in out if e["user"] == "BAsylum")
    assert basylum["count"] == 1


def test_top5_requests_missing_requestedby_falls_back_to_unknown():
    out = app_status.top5_requests(SEERR_REQUESTS_JSON, _now())
    unknown = next(e for e in out if e["user"] == "unknown")
    assert unknown["count"] == 1


def test_top5_requests_excludes_outside_window_entirely():
    """A user whose ONLY request is >30d old must not appear at all."""
    payload = {"results": [_seerr_request(1, 45, {"id": 1, "displayName": "ghost"})]}
    out = app_status.top5_requests(payload, _now())
    assert out == []


def test_top5_requests_caps_at_five():
    payload = {"results": [
        _seerr_request(i, 1, {"id": i, "displayName": "user{}".format(i)})
        for i in range(8)
    ]}
    out = app_status.top5_requests(payload, _now())
    assert len(out) == 5


# ---------------------------------------------------------------------------
# top5_watch
# ---------------------------------------------------------------------------

TAUTULLI_TOP_USERS_ROWS = [
    {"friendly_name": "BAsylum", "total_plays": 105, "total_duration": 281520},
    {"friendly_name": "sarahvanpelt", "total_plays": 40, "total_duration": 90000},
    {"friendly_name": "quiet_user", "total_plays": 1, "total_duration": 300},
]


def test_top5_watch_hours_conversion_matches_contract_example():
    out = app_status.top5_watch(TAUTULLI_TOP_USERS_ROWS)
    top = out[0]
    assert top == {"user": "BAsylum", "hours": 78.2, "plays": 105}


def test_top5_watch_sorted_desc_by_hours():
    out = app_status.top5_watch(TAUTULLI_TOP_USERS_ROWS)
    hours = [e["hours"] for e in out]
    assert hours == sorted(hours, reverse=True)


def test_top5_watch_caps_at_five():
    rows = [{"friendly_name": "u{}".format(i), "total_plays": 1,
              "total_duration": i * 100} for i in range(9)]
    out = app_status.top5_watch(rows)
    assert len(out) == 5


def test_top5_watch_empty():
    assert app_status.top5_watch([]) == []


# ---------------------------------------------------------------------------
# parse_streams — distinct users, matches contract example numbers exactly
# ---------------------------------------------------------------------------

TAUTULLI_ACTIVITY_JSON = {
    "response": {
        "result": "success",
        "message": None,
        "data": {
            "stream_count": "3",
            "stream_count_direct_play": "1",
            "stream_count_direct_stream": "1",
            "stream_count_transcode": "1",
            "total_bandwidth": "15000",
            "lan_bandwidth": "3000",
            "wan_bandwidth": "12000",
            "sessions": [
                {"user_id": 101, "user": "sarahvanpelt", "transcode_decision": "direct play"},
                {"user_id": 202, "user": "BAsylum", "transcode_decision": "transcode"},
                {"user_id": 202, "user": "BAsylum", "transcode_decision": "direct play"},
            ],
        },
    }
}


def test_parse_streams_matches_contract_example():
    out = app_status.parse_streams(TAUTULLI_ACTIVITY_JSON)
    assert out == {"streams": 3, "users": 2, "transcodes": 1, "wan_kbps": 12000}


def test_parse_streams_distinct_user_count():
    """3 sessions, 2 distinct user_id -> users=2 (BAsylum multi-streaming),
    proving this is a set() over user_id, not len(sessions)."""
    out = app_status.parse_streams(TAUTULLI_ACTIVITY_JSON)
    assert out["streams"] == 3
    assert out["users"] == 2
    assert out["streams"] != out["users"]


def test_parse_streams_stream_count_is_stringly_typed_in_source():
    """Guard against a regression to int(str) being skipped — Tautulli's
    stream_count really is a JSON string, not a number."""
    assert isinstance(TAUTULLI_ACTIVITY_JSON["response"]["data"]["stream_count"], str)
    out = app_status.parse_streams(TAUTULLI_ACTIVITY_JSON)
    assert out["streams"] == 3


def test_parse_streams_empty_activity():
    out = app_status.parse_streams({"response": {"data": {}}})
    assert out == {"streams": 0, "users": 0, "transcodes": 0, "wan_kbps": 0}


# ---------------------------------------------------------------------------
# parse_kuma_rows (+ live sqlite integration through _collect_kuma)
# ---------------------------------------------------------------------------

def test_parse_kuma_rows_counts_and_red_list():
    rows = (
        [{"name": "App{}".format(i), "status": 1, "msg": "", "time": "t"} for i in range(51)]
        + [{"name": "QFlix Reaper", "status": 0,
            "msg": "No heartbeat in the time window", "time": "2026-07-15 18:02:11"}]
        + [{"name": "Sonarr", "status": 0, "msg": "Connection refused", "time": "2026-07-15 19:00:00"}]
        + [{"name": "Pending Thing", "status": 2, "msg": "", "time": "t"}]
        + [{"name": "Maint Thing", "status": 3, "msg": "", "time": "t"}]
    )
    out = app_status.parse_kuma_rows(rows)
    assert out["total"] == 55
    assert out["up"] == 51
    assert out["down"] == 2
    names = {r["name"] for r in out["red"]}
    assert names == {"QFlix Reaper", "Sonarr"}
    reaper = next(r for r in out["red"] if r["name"] == "QFlix Reaper")
    assert reaper["msg"] == "No heartbeat in the time window"
    assert reaper["since"] == "2026-07-15 18:02:11"


def _make_kuma_db(tmp_path, monitors):
    """monitors: list of (name, active, [(status, msg, time), ...heartbeats-oldest-first])."""
    db_path = tmp_path / "kuma.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE monitor (id INTEGER PRIMARY KEY, name TEXT, active INTEGER)")
    con.execute("CREATE TABLE heartbeat (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "monitor_id INTEGER, status INTEGER, msg TEXT, time TEXT)")
    for mid, (name, active, heartbeats) in enumerate(monitors, start=1):
        con.execute("INSERT INTO monitor (id, name, active) VALUES (?, ?, ?)",
                    (mid, name, active))
        for status, msg, time_ in heartbeats:
            con.execute(
                "INSERT INTO heartbeat (monitor_id, status, msg, time) VALUES (?, ?, ?, ?)",
                (mid, status, msg, time_))
    con.commit()
    con.close()
    return db_path


def test_collect_kuma_live_sqlite_read_only_uri(tmp_path):
    """Exercises the real python sqlite3 read-only URI path end-to-end
    (not mocked) — proves the JOIN-on-latest-heartbeat query the plan
    specifies actually picks the LATEST heartbeat per monitor, not just
    any row."""
    db_path = _make_kuma_db(tmp_path, [
        ("Sonarr", 1, [(1, "", "2026-07-15 10:00:00"), (0, "down now", "2026-07-15 20:00:00")]),
        ("Radarr", 1, [(1, "", "2026-07-15 20:00:00")]),
        ("Inactive App", 0, [(0, "should be excluded", "2026-07-15 20:00:00")]),
    ])
    section = app_status._collect_kuma(db_path=db_path)
    assert section["ok"] is True
    assert section["error"] is None
    assert section["total"] == 2  # Inactive App excluded by m.active=1
    assert section["up"] == 1
    assert section["down"] == 1
    assert section["red"] == [{"name": "Sonarr", "msg": "down now", "since": "2026-07-15 20:00:00"}]


def test_collect_kuma_missing_db_isolates_failure(tmp_path):
    section = app_status._collect_kuma(db_path=tmp_path / "does-not-exist.db")
    assert section["ok"] is False
    assert section["error"]
    assert section["total"] == 0
    assert section["red"] == []


# ---------------------------------------------------------------------------
# parse_sab_queue — matches contract example numbers exactly
# ---------------------------------------------------------------------------

SAB_QUEUE_JSON = {
    "queue": {
        "paused": False,
        "noofslots": 1,
        "mbleft": "4203.90",
        "mb": "4801.70",
        "kbpersec": "0.00",
    }
}


def test_parse_sab_queue_matches_contract_example():
    out = app_status.parse_sab_queue(SAB_QUEUE_JSON)
    assert out == {"queued": 1, "paused": False, "mb_left": 4203.9,
                    "mb_total": 4801.7, "kbps": 0}


def test_parse_sab_queue_paused_true():
    payload = {"queue": {"paused": True, "noofslots": 0, "mbleft": "0",
                          "mb": "0", "kbpersec": "0"}}
    out = app_status.parse_sab_queue(payload)
    assert out["paused"] is True


def test_parse_sab_queue_empty_payload():
    out = app_status.parse_sab_queue({})
    assert out == {"queued": 0, "paused": False, "mb_left": 0.0,
                    "mb_total": 0.0, "kbps": 0}


# ---------------------------------------------------------------------------
# parse_sab_slots — queue.slots -> build_stuck_list's second name/liveness
# source (C6). Field mapping only, no numeric conversion.
# ---------------------------------------------------------------------------

SAB_QUEUE_WITH_SLOTS = {
    "queue": {
        "paused": False,
        "slots": [
            {"nzo_id": "SABnzbd_nzo_p6f6zj0e", "filename": "Some.Show.S01E01",
             "cat": "sonarr", "status": "Downloading", "mb": "800.00", "mbleft": "400.00"},
        ],
    }
}


def test_parse_sab_slots_field_mapping():
    out = app_status.parse_sab_slots(SAB_QUEUE_WITH_SLOTS)
    assert out == [{
        "id": "SABnzbd_nzo_p6f6zj0e", "name": "Some.Show.S01E01", "cat": "sonarr",
        "state": "Downloading", "mb": "800.00", "mbleft": "400.00",
    }]


def test_parse_sab_slots_empty_payload():
    assert app_status.parse_sab_slots({}) == []
    assert app_status.parse_sab_slots({"queue": {}}) == []


# ---------------------------------------------------------------------------
# count_sab_failed — SAB history slots -> failed-in-window count
# ---------------------------------------------------------------------------

_NOW = 1_700_000_000.0


def _sab_history(slots):
    return {"history": {"slots": slots}}


def test_count_sab_failed_in_window():
    slots = [
        {"status": "Failed", "completed": _NOW - 3600},        # 1h ago
        {"status": "Failed", "completed": _NOW - 23 * 3600},   # 23h ago
        {"status": "Completed", "completed": _NOW - 3600},     # success
    ]
    assert app_status.count_sab_failed(_sab_history(slots), _NOW) == 2


def test_count_sab_failed_outside_window_excluded():
    slots = [{"status": "Failed", "completed": _NOW - 25 * 3600}]  # 25h ago
    assert app_status.count_sab_failed(_sab_history(slots), _NOW) == 0


def test_count_sab_failed_malformed_and_empty():
    slots = [{"status": "Failed", "completed": "not-a-number"},
             {"status": "Failed"}]  # completed missing -> 0 -> outside window
    assert app_status.count_sab_failed(_sab_history(slots), _NOW) == 0
    assert app_status.count_sab_failed({}, _NOW) == 0


# ---------------------------------------------------------------------------
# has_sab_unpack_failure — the FDH blind-spot detector (C6): SAB reports
# this particular unpack failure as a Warning, never Failed, so Sonarr's
# FailedDownloadService silently skips it. Case-insensitive substring.
# ---------------------------------------------------------------------------

def test_has_sab_unpack_failure_true_on_exact_message():
    payload = _sab_history([
        {"status": "Failed",
         "fail_message": "Unpacking failed, write error or disk is full?"},
    ])
    assert app_status.has_sab_unpack_failure(payload) is True


def test_has_sab_unpack_failure_case_insensitive():
    payload = _sab_history([
        {"status": "Failed",
         "fail_message": "UNPACKING FAILED, WRITE ERROR OR DISK IS FULL?"},
    ])
    assert app_status.has_sab_unpack_failure(payload) is True


def test_has_sab_unpack_failure_false_on_unrelated_failure():
    payload = _sab_history([
        {"status": "Failed", "fail_message": "Unknown encoding"},
    ])
    assert app_status.has_sab_unpack_failure(payload) is False


def test_has_sab_unpack_failure_empty_and_missing_field():
    assert app_status.has_sab_unpack_failure({}) is False
    assert app_status.has_sab_unpack_failure(_sab_history([{"status": "Failed"}])) is False


def test_derive_alerts_sab_unpack_disk_full_is_crit():
    doc = {"downloads": {"sab": {"paused": False, "unpack_disk_full": True}}}
    alerts = app_status.derive_alerts(doc)
    assert {"level": "crit",
            "text": "SAB unpack failed (disk full?) — FDH blind spot"} in alerts


def test_derive_alerts_no_unpack_alert_without_flag():
    doc = {"downloads": {"sab": {"paused": False, "unpack_disk_full": False}}}
    assert app_status.derive_alerts(doc) == []


def test_alert_on_sab_failures():
    doc = {"downloads": {"sab": {"paused": False, "failed_24h": 3}}}
    alerts = app_status.derive_alerts(doc)
    assert {"level": "warn", "text": "3 Usenet download(s) failed (24h)"} in alerts


def test_no_alert_on_zero_sab_failures():
    doc = {"downloads": {"sab": {"paused": False, "failed_24h": 0}}}
    assert app_status.derive_alerts(doc) == []


# ---------------------------------------------------------------------------
# build_stuck_list — stale-state.json shape (verbatim from qflix-collect.py)
# ---------------------------------------------------------------------------

STUCK_HASH = "f0a3658d" + "0" * 32  # 40-char hex-shaped hash

STALE_STATE_JSON = {
    "hashes": {
        STUCK_HASH: {
            "first_zero_movement_at": "2026-07-15T16:00:00Z",
            "consecutive_zero_hours": 3,
            "last_progress": 0.62,
            "rule_matched": "stalledDL",
            "candidate_for_unstick": True,
            "acted_on_at": "2026-07-15T19:00:39Z",
        },
        "deadbeef" + "0" * 32: {
            "first_zero_movement_at": "2026-07-15T19:30:00Z",
            "consecutive_zero_hours": 0,
            "last_progress": 0.10,
            "rule_matched": "dead-slow",
            "candidate_for_unstick": False,  # not yet promoted — excluded
            "acted_on_at": None,
        },
    },
    "updated_at": "2026-07-15T19:00:00Z",
}

QBIT_TORRENTS_FOR_STUCK = [
    {"hash": STUCK_HASH, "name": "Some.Movie.2026.1080p"},
    {"hash": "deadbeef" + "0" * 32, "name": "Cooling.Down.2026"},
]


def test_build_stuck_list_matches_contract_example():
    """2-arg call (no sab_slots) must keep working unchanged -- backward
    compat for existing call sites -- with the new "kind" field now
    always present (torrent, since this is a 40-char-hex qBit hash)."""
    out = app_status.build_stuck_list(STALE_STATE_JSON, QBIT_TORRENTS_FOR_STUCK)
    assert out == [{
        "hash8": "f0a3658d",
        "name": "Some.Movie.2026.1080p",
        "hours": 3,
        "rule": "stalledDL",
        "acted": True,
        "kind": "torrent",
    }]


def test_build_stuck_list_excludes_non_candidates():
    """Only the candidate_for_unstick=True hash is included; the other
    tracked hash (candidate_for_unstick=False, still cooling down) must
    not appear even though its torrent is live in qBit."""
    out = app_status.build_stuck_list(STALE_STATE_JSON, QBIT_TORRENTS_FOR_STUCK)
    assert len(out) == 1
    assert out[0]["hash8"] == "f0a3658d"


def test_build_stuck_list_filters_ghost_hashes():
    """A candidate whose hash is no longer in qBit was already resolved
    (unstick removed it) — it must NOT surface as a phantom stuck row.
    Regression: 2026-07-19, heartbeat app showed 5 stuck vs 0 real."""
    assert app_status.build_stuck_list(STALE_STATE_JSON, []) == []


def test_build_stuck_list_empty_state():
    assert app_status.build_stuck_list({}, []) == []


# ---------------------------------------------------------------------------
# build_stuck_list — SAB parity (C6): second name/liveness source, id-shape
# kind + label decision, no collision between the two namespaces.
# ---------------------------------------------------------------------------

# Chosen so the tail after the literal "SABnzbd_nzo_" prefix IS exactly 8
# chars -- makes the expected last-8 label trivially verifiable by eye.
SAB_STUCK_ID = "SABnzbd_nzo_p6f6zj0e"

STALE_STATE_MIXED = {
    "hashes": {
        STUCK_HASH: {
            "consecutive_zero_hours": 3,
            "rule_matched": "stalledDL",
            "candidate_for_unstick": True,
            "acted_on_at": None,
        },
        SAB_STUCK_ID: {
            "consecutive_zero_hours": 5,
            "rule_matched": "sab-paused-pinned",
            "candidate_for_unstick": True,
            "acted_on_at": None,
        },
    },
}

SAB_SLOTS_FOR_STUCK = [
    {"id": SAB_STUCK_ID, "name": "Some.Show.S01E01", "cat": "sonarr", "state": "Paused"},
]


def test_build_stuck_list_mixed_kinds_both_resolve_no_collision():
    """One qBit hash + one SABnzbd_nzo id in the same stale-state map: both
    must resolve their own name via their own source, with distinct kinds
    and distinct labels — proving the merged names dict doesn't cross-wire
    the two namespaces."""
    out = app_status.build_stuck_list(
        STALE_STATE_MIXED, QBIT_TORRENTS_FOR_STUCK, SAB_SLOTS_FOR_STUCK)
    assert len(out) == 2
    by_kind = {e["kind"]: e for e in out}
    assert set(by_kind) == {"torrent", "usenet"}
    assert by_kind["torrent"]["name"] == "Some.Movie.2026.1080p"
    assert by_kind["torrent"]["hash8"] == "f0a3658d"
    assert by_kind["usenet"]["name"] == "Some.Show.S01E01"
    assert by_kind["usenet"]["hash8"] == "p6f6zj0e"
    assert by_kind["torrent"]["hash8"] != by_kind["usenet"]["hash8"]


def test_build_stuck_list_sab_label_is_last8_not_first8():
    out = app_status.build_stuck_list(STALE_STATE_MIXED, [], SAB_SLOTS_FOR_STUCK)
    assert len(out) == 1
    assert out[0]["hash8"] == "p6f6zj0e"
    assert out[0]["hash8"] != SAB_STUCK_ID[:8]  # would collide across every job if first-8


def test_build_stuck_list_sab_ghost_pruned_when_slot_gone():
    """A SAB-kind candidate whose id no longer appears in the live SAB
    slots list was already resolved (unstick removed it, or SAB itself
    completed/cleared it) — must not surface as a phantom row, same
    guarantee build_stuck_list already gives qBit hashes."""
    out = app_status.build_stuck_list(STALE_STATE_MIXED, QBIT_TORRENTS_FOR_STUCK, sab_slots=[])
    assert len(out) == 1
    assert out[0]["kind"] == "torrent"


def test_build_stuck_list_sab_only_no_qbit_torrents():
    out = app_status.build_stuck_list(
        {"hashes": {SAB_STUCK_ID: STALE_STATE_MIXED["hashes"][SAB_STUCK_ID]}},
        [], SAB_SLOTS_FOR_STUCK)
    assert out == [{
        "hash8": "p6f6zj0e",
        "name": "Some.Show.S01E01",
        "hours": 5,
        "rule": "sab-paused-pinned",
        "acted": False,
        "kind": "usenet",
    }]


# ---------------------------------------------------------------------------
# recent_unsticks_from_lines — events/<date>.jsonl shape (verbatim from
# unstick.py's _record_event)
# ---------------------------------------------------------------------------

def _event_line(**overrides):
    base = {
        "ts": "2026-07-15T19:00:39Z", "action": "unstick", "slug": "sonarr",
        "queue_id": 42, "hash": STUCK_HASH, "title": "Some.Movie.2026.1080p",
        "reason": "stale", "result": "qbit-orphan-removed", "post_action": None,
    }
    base.update(overrides)
    return json.dumps(base)


def test_recent_unsticks_matches_contract_example():
    lines = [_event_line()]
    out = app_status.recent_unsticks_from_lines(lines)
    assert out == [{"ts": "2026-07-15T19:00:39Z", "hash8": "f0a3658d",
                     "result": "qbit-orphan-removed", "kind": "torrent"}]


def test_recent_unsticks_filters_non_unstick_actions():
    lines = [_event_line(action="refused-cap-hit")]
    assert app_status.recent_unsticks_from_lines(lines) == []


def test_recent_unsticks_skips_malformed_and_blank_lines():
    lines = ["", "not json", _event_line()]
    out = app_status.recent_unsticks_from_lines(lines)
    assert len(out) == 1


def test_recent_unsticks_sorted_newest_first():
    lines = [
        _event_line(ts="2026-07-15T10:00:00Z", hash="1111" + "0" * 36),
        _event_line(ts="2026-07-15T19:00:39Z", hash="2222" + "0" * 36),
    ]
    out = app_status.recent_unsticks_from_lines(lines)
    assert out[0]["hash8"] == "22220000"


# ---------------------------------------------------------------------------
# derive_alerts — exact thresholds from the plan
# ---------------------------------------------------------------------------

def _base_doc(**overrides):
    doc = {
        "kuma": {"red": []},
        "quota": {"disk": {"pct": 10.0}, "bandwidth": {"available_pct": 90.0}},
        "downloads": {"stuck": [], "sab": {"paused": False}},
        "maint": {"apps": {}},
        "_now": _now(),
    }
    doc.update(overrides)
    return doc


def test_derive_alerts_all_clear_is_empty():
    assert app_status.derive_alerts(_base_doc()) == []


def test_derive_alerts_kuma_red_is_crit():
    doc = _base_doc(kuma={"red": [{"name": "QFlix Reaper",
                                    "msg": "No heartbeat in the time window",
                                    "since": "2026-07-15 18:02:11"}]})
    out = app_status.derive_alerts(doc)
    assert out == [{"level": "crit",
                     "text": "Kuma down: QFlix Reaper — No heartbeat in the time window"}]


def test_derive_alerts_quadstronix_trio_rolls_into_one_line():
    doc = _base_doc(kuma={"red": [
        {"name": "Sonarr", "msg": "down", "since": "t"},
        {"name": "Quadstronix", "msg": "parent down", "since": "t"},
        {"name": "Quadstronix Node 1", "msg": "node1 down", "since": "t"},
        {"name": "Quadstronix Node 2", "msg": "node2 down", "since": "t"},
    ]})
    out = app_status.derive_alerts(doc)
    assert len(out) == 2  # Sonarr line + one combined Quadstronix line
    texts = [a["text"] for a in out]
    assert any("Sonarr" in t for t in texts)
    combined = next(t for t in texts if "Quadstronix" in t)
    assert "Quadstronix Node 1" in combined
    assert "Quadstronix Node 2" in combined
    assert all(a["level"] == "crit" for a in out)


def test_derive_alerts_disk_crit_at_90():
    doc = _base_doc(quota={"disk": {"pct": 90.0}, "bandwidth": {"available_pct": 90.0}})
    out = app_status.derive_alerts(doc)
    assert out == [{"level": "crit", "text": "Disk quota 90.0% used"}]


def test_derive_alerts_disk_warn_at_80():
    doc = _base_doc(quota={"disk": {"pct": 80.0}, "bandwidth": {"available_pct": 90.0}})
    out = app_status.derive_alerts(doc)
    assert out == [{"level": "warn", "text": "Disk quota 80.0% used"}]


def test_derive_alerts_disk_below_80_is_clean():
    doc = _base_doc(quota={"disk": {"pct": 79.9}, "bandwidth": {"available_pct": 90.0}})
    assert app_status.derive_alerts(doc) == []


def test_derive_alerts_bandwidth_crit_below_10():
    doc = _base_doc(quota={"disk": {"pct": 10.0}, "bandwidth": {"available_pct": 9.99}})
    out = app_status.derive_alerts(doc)
    assert out == [{"level": "crit", "text": "Bandwidth available 9.99%"}]


def test_derive_alerts_bandwidth_warn_below_20():
    doc = _base_doc(quota={"disk": {"pct": 10.0}, "bandwidth": {"available_pct": 19.99}})
    out = app_status.derive_alerts(doc)
    assert out == [{"level": "warn", "text": "Bandwidth available 19.99%"}]


def test_derive_alerts_bandwidth_at_exactly_20_is_clean():
    doc = _base_doc(quota={"disk": {"pct": 10.0}, "bandwidth": {"available_pct": 20.0}})
    assert app_status.derive_alerts(doc) == []


def test_derive_alerts_disk_none_still_alerts_on_low_bandwidth():
    """quota["disk"] can be None (the disk parser failed in _collect_quota
    while the bandwidth parser succeeded) -- derive_alerts must not choke on
    that and must still fire off the preserved bandwidth reading."""
    doc = _base_doc(quota={"disk": None, "bandwidth": {"available_pct": 5.0}})
    out = app_status.derive_alerts(doc)
    assert out == [{"level": "crit", "text": "Bandwidth available 5.0%"}]


def test_derive_alerts_bandwidth_none_still_alerts_on_high_disk():
    """Symmetric case: quota["bandwidth"] can be None without suppressing
    the disk alert derived from the preserved disk reading."""
    doc = _base_doc(quota={"disk": {"pct": 95.0}, "bandwidth": None})
    out = app_status.derive_alerts(doc)
    assert out == [{"level": "crit", "text": "Disk quota 95.0% used"}]


def test_derive_alerts_maint_failed_within_48h_is_crit():
    doc = _base_doc(maint={"apps": {"sonarr": {
        "event": "failed", "final_health": "down",
        "updated_at": (_now() - dt.timedelta(hours=47)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }}})
    out = app_status.derive_alerts(doc)
    assert out == [{"level": "crit", "text": "Auto-heal failed: sonarr"}]


def test_derive_alerts_maint_failed_beyond_48h_is_excluded():
    doc = _base_doc(maint={"apps": {"sonarr": {
        "event": "failed", "final_health": "down",
        "updated_at": (_now() - dt.timedelta(hours=49)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }}})
    assert app_status.derive_alerts(doc) == []


def test_derive_alerts_maint_non_failed_event_ignored():
    doc = _base_doc(maint={"apps": {"sonarr": {
        "event": "recovered", "final_health": "up",
        "updated_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }}})
    assert app_status.derive_alerts(doc) == []


def test_derive_alerts_stuck_nonzero_is_warn():
    doc = _base_doc(downloads={"stuck": [{"hash8": "f0a3658d", "name": "x", "hours": 3,
                                           "rule": "stalledDL", "acted": True}],
                                "sab": {"paused": False}})
    out = app_status.derive_alerts(doc)
    assert out == [{"level": "warn", "text": "1 download(s) stuck, pending unstick"}]


def test_derive_alerts_sab_paused_is_warn():
    doc = _base_doc(downloads={"stuck": [], "sab": {"paused": True}})
    out = app_status.derive_alerts(doc)
    assert out == [{"level": "warn", "text": "SABnzbd queue paused"}]


def test_derive_alerts_ordered_crit_before_warn():
    doc = _base_doc(
        kuma={"red": [{"name": "Sonarr", "msg": "down", "since": "t"}]},
        quota={"disk": {"pct": 10.0}, "bandwidth": {"available_pct": 90.0}},
        downloads={"stuck": [{"hash8": "a", "name": "x", "hours": 1,
                               "rule": "stalledDL", "acted": False}],
                   "sab": {"paused": True}},
    )
    out = app_status.derive_alerts(doc)
    levels = [a["level"] for a in out]
    # all crit entries precede all warn entries
    assert levels == sorted(levels, key=lambda lv: {"crit": 0, "warn": 1}[lv])
    assert levels[0] == "crit"
    assert levels[-1] == "warn"


# ---------------------------------------------------------------------------
# _collect_downloads — threads parse_sab_slots through to build_stuck_list
# and derives downloads.sab.slots_stuck (C6 end-to-end wiring).
# ---------------------------------------------------------------------------

class _FakeQbitClient:
    """Stand-in for lib.qbit_client.QbitClient — just enough surface for
    _collect_downloads (login + list_torrents)."""
    def login(self):
        return True

    def list_torrents(self):
        return [{"hash": STUCK_HASH, "name": "Some.Movie.2026.1080p", "state": "stalledDL"}]


def test_collect_downloads_threads_sab_slots_and_counts_slots_stuck(monkeypatch, tmp_path):
    monkeypatch.setattr(app_status, "QbitClient", _FakeQbitClient)
    sab_queue_payload = {
        "queue": {"paused": False, "noofslots": 1, "mbleft": "100", "mb": "100",
                   "kbpersec": "0",
                   "slots": [{"nzo_id": SAB_STUCK_ID, "filename": "Some.Show.S01E01",
                              "cat": "sonarr", "status": "Paused", "mb": "100", "mbleft": "100"}]},
    }
    monkeypatch.setattr(app_status, "_collect_sab_queue", lambda: sab_queue_payload)
    monkeypatch.setattr(app_status, "_collect_sab_history", lambda: {"history": {"slots": []}})
    monkeypatch.setattr(app_status, "QFLIX_COLLECT_DATA", tmp_path)
    (tmp_path / "stale-state.json").write_text(json.dumps(STALE_STATE_MIXED), encoding="utf-8")

    section = app_status._collect_downloads()
    assert section["ok"] is True
    assert len(section["stuck"]) == 2
    kinds = {e["kind"] for e in section["stuck"]}
    assert kinds == {"torrent", "usenet"}
    assert section["sab"]["slots_stuck"] == 1  # only the usenet-kind entry counts


def test_collect_downloads_slots_stuck_zero_when_no_usenet_stuck(monkeypatch, tmp_path):
    monkeypatch.setattr(app_status, "QbitClient", _FakeQbitClient)
    monkeypatch.setattr(app_status, "_collect_sab_queue", lambda: {"queue": {}})
    monkeypatch.setattr(app_status, "_collect_sab_history", lambda: {"history": {"slots": []}})
    monkeypatch.setattr(app_status, "QFLIX_COLLECT_DATA", tmp_path)
    (tmp_path / "stale-state.json").write_text(json.dumps(STALE_STATE_JSON), encoding="utf-8")

    section = app_status._collect_downloads()
    assert section["sab"]["slots_stuck"] == 0


# ---------------------------------------------------------------------------
# run() — section-failure isolation + restricted `sections` + doc shape
# ---------------------------------------------------------------------------

def _mock_all_sections(monkeypatch, *, streams_ok=True):
    monkeypatch.setattr(app_status, "_collect_quota", lambda: {
        "ok": True, "error": None,
        "disk": {"used_gb": 2073, "total_gb": 2794, "pct": 74.2},
        "bandwidth": {"used_pct": 3.42, "available_pct": 96.58,
                       "last_reset": "2026-06-28T00:00:00",
                       "next_reset": "2026-07-28T00:00:00"},
    })
    monkeypatch.setattr(app_status, "_collect_kuma", lambda: {
        "ok": True, "error": None, "total": 55, "up": 55, "down": 0, "red": [],
    })
    if streams_ok:
        monkeypatch.setattr(app_status, "_collect_streams", lambda: {
            "ok": True, "error": None, "streams": 3, "users": 2,
            "transcodes": 1, "wan_kbps": 12000,
        })
    else:
        def _dead_streams():
            raise RuntimeError("connection refused")
        monkeypatch.setattr(app_status, "_collect_streams", _dead_streams)
    monkeypatch.setattr(app_status, "_collect_top5", lambda now: {
        "ok": True, "error": None, "requests_30d": [], "watch_30d": [],
    })
    monkeypatch.setattr(app_status, "_collect_downloads", lambda: {
        "ok": True, "error": None,
        "qbit": {"total": 12, "active": 1, "stalled_dl": 0, "errored": 0, "seeding": 10},
        "sab": {"queued": 1, "paused": False, "mb_left": 4203.9, "mb_total": 4801.7, "kbps": 0},
        "stuck": [], "recent_unsticks": [],
    })
    monkeypatch.setattr(app_status, "_collect_maint", lambda: {"apps": {}})


CONTRACT_TOP_KEYS = {"meta", "quota", "kuma", "streams", "top5", "downloads", "maint", "alerts"}


def test_run_full_doc_shape_matches_contract_keys(monkeypatch):
    _mock_all_sections(monkeypatch)
    doc = app_status.run()
    assert set(doc.keys()) == CONTRACT_TOP_KEYS
    assert doc["meta"]["version"] == 2
    assert "generated_at" in doc["meta"]
    assert "elapsed_ms" in doc["meta"]
    assert "host" in doc["meta"]
    assert doc["alerts"] == []  # every mocked section is healthy


def test_run_dead_tautulli_isolates_to_streams_section(monkeypatch):
    """Section-failure isolation: a dead tautulli must only degrade
    streams.ok — every other section and the doc's top-level shape must
    remain intact."""
    _mock_all_sections(monkeypatch, streams_ok=False)
    doc = app_status.run()
    assert set(doc.keys()) == CONTRACT_TOP_KEYS
    assert doc["streams"]["ok"] is False
    assert doc["streams"]["error"]
    assert doc["quota"]["ok"] is True
    assert doc["kuma"]["ok"] is True
    assert doc["top5"]["ok"] is True
    assert doc["downloads"]["ok"] is True


def test_run_restricted_sections_still_returns_complete_doc(monkeypatch):
    _mock_all_sections(monkeypatch)
    doc = app_status.run(sections=["quota"])
    assert set(doc.keys()) == CONTRACT_TOP_KEYS
    assert doc["quota"]["ok"] is True
    # unrequested sections are still present, marked not-fetched
    assert doc["kuma"]["ok"] is False
    assert doc["streams"]["ok"] is False
    assert doc["top5"]["ok"] is False
    assert doc["downloads"]["ok"] is False


def test_run_section_raising_exception_is_isolated(monkeypatch):
    """Even an uncaught exception inside a collector (not just a returned
    ok:false) must not take down the rest of the doc."""
    _mock_all_sections(monkeypatch)

    def _boom():
        raise RuntimeError("kaboom")
    monkeypatch.setattr(app_status, "_collect_kuma", _boom)
    doc = app_status.run()
    assert doc["kuma"]["ok"] is False
    assert "kaboom" in doc["kuma"]["error"]
    assert doc["quota"]["ok"] is True


# ---------------------------------------------------------------------------
# main() — emits JSON to stdout with no args (forced-command channel)
# ---------------------------------------------------------------------------

def test_main_emits_json_with_no_args(monkeypatch, capsys):
    fixed = {"meta": {"generated_at": "x", "elapsed_ms": 1, "host": "manitoba", "version": 1},
             "quota": {}, "kuma": {}, "streams": {}, "top5": {}, "downloads": {}, "alerts": []}
    monkeypatch.setattr(app_status, "run", lambda sections=None: fixed)
    monkeypatch.setattr(sys, "argv", ["app_status.py"])
    rc = app_status.main()
    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed == fixed
    assert parsed["meta"]["version"] == 1


# ---------------------------------------------------------------------------
# Council 2026-07-20, Defect 3: recent_unsticks_from_lines must label SAB
# unstick ids the SAME way build_stuck_list does (last-8 + kind), or every
# usenet unstick renders hash8="SABnzbd" and collides.
# ---------------------------------------------------------------------------

def test_recent_unsticks_sab_id_label_last8_and_kind():
    sab_id = "SABnzbd_nzo_abcd1234"
    qbit_hash = "deadbeef" + "0" * 32
    lines = [
        json.dumps({"action": "unstick", "ts": "2026-07-20T01:00:00Z",
                    "hash": sab_id, "result": "sab-orphan-removed"}),
        json.dumps({"action": "unstick", "ts": "2026-07-20T00:00:00Z",
                    "hash": qbit_hash, "result": "deleted+blocklisted"}),
    ]
    out = app_status.recent_unsticks_from_lines(lines)
    by_result = {e["result"]: e for e in out}
    assert by_result["sab-orphan-removed"]["hash8"] == sab_id[-8:]      # last-8
    assert by_result["sab-orphan-removed"]["hash8"] != "SABnzbd"        # no collapse
    assert by_result["sab-orphan-removed"]["kind"] == "usenet"
    assert by_result["deleted+blocklisted"]["hash8"] == qbit_hash[:8]   # first-8
    assert by_result["deleted+blocklisted"]["kind"] == "torrent"


def test_recent_unsticks_two_sab_ids_do_not_collide():
    """Two different SAB unsticks must produce two different labels."""
    lines = [
        json.dumps({"action": "unstick", "ts": "2026-07-20T02:00:00Z",
                    "hash": "SABnzbd_nzo_aaaa1111", "result": "sab-orphan-removed"}),
        json.dumps({"action": "unstick", "ts": "2026-07-20T01:00:00Z",
                    "hash": "SABnzbd_nzo_bbbb2222", "result": "sab-orphan-removed"}),
    ]
    labels = {e["hash8"] for e in app_status.recent_unsticks_from_lines(lines)}
    assert len(labels) == 2


# ---- maint health: failed units + anime-janitor activity (v2) ----------------

def test_derive_alerts_flags_failed_units_as_crit():
    doc = {
        "maint": {"apps": {}, "failed_units": ["manitoba-maint-anime-janitor.service"]},
        "_now": dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc),
    }
    texts = [a["text"] for a in app_status.derive_alerts(doc) if a["level"] == "crit"]
    assert any("Maint unit failed: manitoba-maint-anime-janitor.service" in t for t in texts)


def test_anime_janitor_summary_counts_recent_and_last():
    now = dt.datetime(2026, 7, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
    moved = [
        {"title": "Old", "from": "sonarr2", "to": "sonarr", "ts": "2026-07-01T00:00:00Z"},   # >7d
        {"title": "Recent A", "from": "sonarr2", "to": "sonarr", "ts": "2026-07-24T00:00:00Z"},
        {"title": "Cowboy Bebop (2021)", "from": "sonarr2", "to": "sonarr", "ts": "2026-07-25T10:00:00Z"},
    ]
    s = app_status.anime_janitor_summary(moved, now)
    assert s["recent_moves"] == 2                        # only the last 7 days
    assert s["last_move"]["title"] == "Cowboy Bebop (2021)"


def test_anime_janitor_summary_empty():
    now = dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc)
    assert app_status.anime_janitor_summary([], now) == {"recent_moves": 0, "last_move": None}
