"""QFLX-44: every stored VictoriaLogs _time is the true UTC instant.

One fixture per ingested source format, classified from real box samples taken
2026-10-10 (stamp vs `date -u` vs file mtime; box = Europe/Amsterdam, CEST).
Fixture text is synthetic: same shape as the live line, no real content.
The suite pins QFLIX_LOG_TZ=Europe/Amsterdam (tests/unit/conftest.py).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "mcp"))

import logs  # noqa: E402


def _ts(app: str, line: str) -> str | None:
    """parse_line the way the ingester does: source = the app's real route."""
    plan = logs.route(app)
    source = plan["path"] if plan["kind"] == "file" else f"journalctl:{plan['unit']}"
    return logs.parse_line(line, source=source)["ts"]


# (app, line, expected UTC _time). Wall clock in the line is 06:44:28 CEST
# (= 04:44:28Z) unless the source is UTC/zoned.
FORMATS = [
    # zone-less LOCAL
    ("sonarr", "2026-10-10 06:44:28.2|Info|ImportListSyncService|x", "2026-10-10T04:44:28.2Z"),
    ("sonarr2", "2026-10-10 06:44:28.2|Info|RssSyncService|x", "2026-10-10T04:44:28.2Z"),
    ("radarr", "2026-10-10 06:44:28.2|Info|RssSyncService|x", "2026-10-10T04:44:28.2Z"),
    ("radarr2", "2026-10-10 06:44:28.2|Info|RssSyncService|x", "2026-10-10T04:44:28.2Z"),
    ("prowlarr", "2026-10-10 06:44:28.2|Info|ReleaseSearchService|x", "2026-10-10T04:44:28.2Z"),
    ("bazarr", "2026-10-10 06:44:28|INFO    |root                            |x|", "2026-10-10T04:44:28Z"),
    ("kometa", "[2026-10-10 06:44:28,819] [kometa.py:480]             [INFO]     | x |", "2026-10-10T04:44:28.819Z"),
    ("buildarr", "2026-10-10 06:44:28,472 buildarr:3623184 buildarr.cli.run [INFO] x", "2026-10-10T04:44:28.472Z"),
    ("qbittorrent", "(N) 2026-10-10T06:44:28 - WebAPI login success.", "2026-10-10T04:44:28Z"),
    ("listmonk", "2026/10/10 06:44:28.559655 manager.go:442: x", "2026-10-10T04:44:28.559655Z"),
    ("unpackerr", "[INFO] 2026/10/10 06:44:28 [Radarr] Updated", "2026-10-10T04:44:28Z"),
    ("tdarr-server", "\x1b[91m[2026-10-10T06:44:28.251] [ERROR] Tdarr_Server - \x1b[39mx", "2026-10-10T04:44:28.251Z"),
    ("tdarr-node", "\x1b[32m[2026-10-10T06:44:28.855] [INFO] Tdarr_Node - \x1b[39mx", "2026-10-10T04:44:28.855Z"),
    ("nginx", "2026/10/10 06:44:28 [error] 1#1: x", "2026-10-10T04:44:28Z"),
    # zone-less UTC (proven)
    ("tautulli", "2026-10-10 04:44:28 - DEBUG   :: Thread-18 (run) : x", "2026-10-10T04:44:28Z"),
    ("plex", "Oct 10, 2026 04:44:28.832 [139931718089528] INFO - x", "2026-10-10T04:44:28.832Z"),
    # explicit zone
    ("seerr", "2026-10-10T04:44:28.036Z [info][Jobs]: Starting scheduled job", "2026-10-10T04:44:28.036Z"),
    ("listmonk-sync", "2026-10-10T04:44:28.686+00:00 listmonk-sync [INFO] x", "2026-10-10T04:44:28.686Z"),
    ("maint-pusher", "2026-10-10T06:44:41+02:00 manitoba manitoba-maint[480517]: INFO x", "2026-10-10T04:44:41Z"),
    ("maint-window", "2026-10-10T06:44:41+0200 manitoba systemd[4942]: Finished x", "2026-10-10T04:44:41Z"),
]


@pytest.mark.parametrize("app,line,expected", FORMATS, ids=[f[0] for f in FORMATS])
def test_each_source_format_stores_true_utc(app, line, expected):
    assert _ts(app, line) == expected


def test_seerr_message_keeps_its_text():
    rec = logs.parse_line("2026-10-10T04:44:28.036Z [info][Jobs]: Starting job",
                          source=logs._FILE_LOGS["seerr"])
    assert rec["level"] == "INFO"
    assert rec["message"] == "[Jobs]: Starting job"


def test_unstamped_sources_get_no_ts_for_the_ingest_clock():
    # recyclarr writes no stamp; ts=None -> vlogs stamps ingest time (UTC).
    assert _ts("recyclarr", "[INF] anime: All quality profiles are up to date!") is None
    assert _ts("bazarr2", "Bazarr starting child process with PID 30592...") is None


def test_every_routed_source_has_an_explicit_zone_policy():
    """A new route must declare what its zone-less stamps mean; falling to
    DEFAULT_ZONE silently is how a 2h skew gets in."""
    routed = set(logs._FILE_LOGS) | set(logs._GLOB_LOGS) | set(logs._SYSTEMD_LOGS)
    assert routed - set(logs.SOURCE_ZONE) == set()
    assert set(logs.SOURCE_ZONE) - routed == set()
    assert set(logs.SOURCE_ZONE.values()) <= {"UTC", "LOCAL", "ZONED", "none"}


def test_unknown_source_defaults_to_box_zone():
    line = "2026-10-10 06:44:28,000 x [INFO] m"
    assert logs.parse_line(line, source="/nowhere.log")["ts"] == "2026-10-10T04:44:28.000Z"


def test_explicit_zone_beats_the_source_policy():
    # A LOCAL source that happens to emit +00:00 must not be shifted again.
    line = "2026-10-10T04:44:28.000+00:00 x [INFO] m"
    assert logs.parse_line(line, source=logs._FILE_LOGS["listmonk"])["ts"] == "2026-10-10T04:44:28.000Z"


# ── DST math (Europe/Amsterdam) ──────────────────────────────────────────────

@pytest.mark.parametrize("stamp,expected", [
    ("2026-01-15 12:00:00", "2026-01-15T11:00:00Z"),   # CET  +01:00
    ("2026-07-15 12:00:00", "2026-07-15T10:00:00Z"),   # CEST +02:00
    ("2026-03-29 01:59:59", "2026-03-29T00:59:59Z"),   # last second before the gap
    ("2026-03-29 03:00:00", "2026-03-29T01:00:00Z"),   # first second after it
    ("2026-10-25 01:59:59", "2026-10-24T23:59:59Z"),   # still CEST
    ("2026-10-25 02:30:00", "2026-10-25T00:30:00Z"),   # ambiguous -> first (CEST)
    ("2026-10-25 03:00:00", "2026-10-25T02:00:00Z"),   # CET again
    ("2026-12-31 23:59:59", "2026-12-31T22:59:59Z"),   # crosses the day boundary
    ("2026-01-01 00:30:00", "2025-12-31T23:30:00Z"),   # crosses the year boundary
])
def test_local_zone_math_across_dst(stamp, expected):
    line = f"{stamp}|Info|X|m"
    assert logs.parse_line(line, source=logs._FILE_LOGS["sonarr"])["ts"] == expected


def test_spring_forward_gap_does_not_crash():
    out = logs.parse_line("2026-03-29 02:30:00|Info|X|m", source=logs._FILE_LOGS["sonarr"])["ts"]
    assert out in ("2026-03-29T00:30:00Z", "2026-03-29T01:30:00Z")


def test_box_tz_override_and_fallback(monkeypatch):
    monkeypatch.setenv("QFLIX_LOG_TZ", "America/New_York")
    logs.box_tz.cache_clear()
    try:
        assert str(logs.box_tz()) == "America/New_York"
        line = "2026-07-15 12:00:00|Info|X|m"
        assert logs.parse_line(line, source=logs._FILE_LOGS["sonarr"])["ts"] == "2026-07-15T16:00:00Z"
    finally:
        monkeypatch.setenv("QFLIX_LOG_TZ", "Europe/Amsterdam")
        logs.box_tz.cache_clear()


# ── The writer we own ────────────────────────────────────────────────────────

def test_owned_writer_emits_explicit_zone(capsys):
    import re
    spec = importlib.util.spec_from_file_location(
        "listmonk_sync_zone", ROOT / "scripts" / "ops" / "listmonk-sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.log("hello")
    err = capsys.readouterr().err
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00 ", err)


# ── Exactly-once, with the cursor, end to end ────────────────────────────────

def _load_ingest():
    spec = importlib.util.spec_from_file_location(
        "qflix_vlogs_ingest_zone", ROOT / "scripts" / "maint" / "qflix-vlogs-ingest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_replay_ships_each_line_exactly_once_with_utc_time(tmp_path, monkeypatch):
    mod = _load_ingest()
    log = tmp_path / "sonarr.txt"
    log.write_text("2026-10-10 06:00:00.0|Info|A|first\n")
    monkeypatch.setattr(mod.logs_mod, "_FILE_LOGS", {"sonarr": str(log)})
    monkeypatch.setattr(mod.logs_mod, "_GLOB_LOGS", {})
    monkeypatch.setattr(mod.logs_mod, "_SYSTEMD_LOGS", {})
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(mod, "_read_port", lambda: 1)
    shipped: list[dict] = []
    monkeypatch.setattr(mod, "_post_jsonline",
                        lambda port, app, lines: (shipped.extend(
                            {"app": app, "_time": ln["ts"], "_msg": ln["message"]} for ln in lines)
                            or True, f"{len(lines)} lines"))
    monkeypatch.setattr(sys, "argv", ["qflix-vlogs-ingest.py"])

    def cycle():
        mod.main()

    cycle()                                             # bootstrap read
    with log.open("a") as fh:
        fh.write("2026-10-10 06:01:00.0|Info|A|second\n")
    cycle()
    cycle()                                             # replay: nothing new
    cycle()
    with log.open("a") as fh:
        fh.write("2026-10-10 06:02:00.0|Info|A|third\n")
    cycle()
    cycle()

    keys = [(s["app"], s["_time"], s["_msg"]) for s in shipped]
    assert len(keys) == len(set(keys)), keys            # no duplicates
    assert sorted(s["_msg"] for s in shipped) == ["first", "second", "third"]
    # 06:xx CEST is 04:xx UTC, never the 06:xx a UTC reader would store.
    assert {s["_time"] for s in shipped} == {
        "2026-10-10T04:00:00.0Z", "2026-10-10T04:01:00.0Z", "2026-10-10T04:02:00.0Z"}
