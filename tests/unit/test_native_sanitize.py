"""tests/unit/test_native_sanitize.py -- fixture-sqlite tests for I-11 (inert proofs).

Every test builds a throwaway data dir in tmp_path with the real per-app
table shapes (only the columns sanitize touches), runs sanitize, and asserts
the enabled counts are zero. No network, no SSH, no real ~/.apps.
"""
from __future__ import annotations

import json
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

import native_sanitize as ns


def _db(path: Path, ddl, rows=()) -> None:
    con = sqlite3.connect(path)
    for d in ddl:
        con.execute(d)
    for sql in rows:
        con.execute(sql)
    con.commit()
    con.close()


def _q(path: Path, sql: str):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------- arr
def _sonarr_dir(tmp: Path) -> Path:
    d = tmp / "proof" / "sonarr"
    d.mkdir(parents=True)
    _db(d / "sonarr.db", [
        "CREATE TABLE DownloadClients (Id INTEGER PRIMARY KEY, Name TEXT, Enable INTEGER)",
        "CREATE TABLE Indexers (Id INTEGER PRIMARY KEY, Name TEXT, EnableRss INTEGER,"
        " EnableAutomaticSearch INTEGER, EnableInteractiveSearch INTEGER)",
        "CREATE TABLE ImportLists (Id INTEGER PRIMARY KEY, Name TEXT, EnableAutomaticAdd INTEGER)",
        "CREATE TABLE Notifications (Id INTEGER PRIMARY KEY, Name TEXT)",
    ], [
        "INSERT INTO DownloadClients VALUES (1,'qbit',1),(2,'sab',1)",
        "INSERT INTO Indexers VALUES (1,'a',1,1,1),(2,'b',1,0,1)",
        "INSERT INTO ImportLists VALUES (1,'l',1)",
        "INSERT INTO Notifications VALUES (1,'discord')",
    ])
    (d / "config.xml").write_text(
        "<Config>\n  <Port>8989</Port>\n  <UpdateAutomatically>True</UpdateAutomatically>\n</Config>\n")
    return d


def test_arr_sonarr_zero_after_sanitize(tmp_path):
    d = _sonarr_dir(tmp_path)
    counts = ns.sanitize("sonarr", d)
    assert all(v == 0 for v in counts.values()), counts
    db = d / "sonarr.db"
    assert _q(db, "SELECT count(*) FROM DownloadClients WHERE Enable=1") == [(0,)]
    assert _q(db, "SELECT count(*) FROM Indexers WHERE EnableRss=1 OR EnableAutomaticSearch=1"
                  " OR EnableInteractiveSearch=1") == [(0,)]
    assert _q(db, "SELECT count(*) FROM ImportLists WHERE EnableAutomaticAdd=1") == [(0,)]
    assert _q(db, "SELECT count(*) FROM Notifications") == [(0,)]
    # rows kept (only flags flipped) so a count-parity check still works
    assert _q(db, "SELECT count(*) FROM DownloadClients") == [(2,)]
    root = ET.parse(d / "config.xml").getroot()
    assert root.findtext("UpdateAutomatically") == "False"
    assert root.findtext("Port") == "8989"


def test_arr_radarr_import_list_columns(tmp_path):
    d = tmp_path / "radarr2"
    d.mkdir()
    _db(d / "radarr.db", [
        "CREATE TABLE DownloadClients (Id INTEGER PRIMARY KEY, Enable INTEGER)",
        "CREATE TABLE Indexers (Id INTEGER PRIMARY KEY, EnableRss INTEGER,"
        " EnableAutomaticSearch INTEGER, EnableInteractiveSearch INTEGER)",
        "CREATE TABLE ImportLists (Id INTEGER PRIMARY KEY, Enabled INTEGER, EnableAuto INTEGER)",
        "CREATE TABLE Notifications (Id INTEGER PRIMARY KEY)",
    ], ["INSERT INTO ImportLists VALUES (1,1,1)", "INSERT INTO Notifications VALUES (1)"])
    (d / "config.xml").write_text("<Config><Port>7878</Port></Config>")  # no UpdateAutomatically
    counts = ns.sanitize("radarr2", d)
    assert sum(counts.values()) == 0
    assert _q(d / "radarr.db", "SELECT Enabled, EnableAuto FROM ImportLists") == [(0, 0)]
    assert ET.parse(d / "config.xml").getroot().findtext("UpdateAutomatically") == "False"


def test_arr_missing_expected_column_fails_closed(tmp_path):
    d = tmp_path / "sonarr"
    d.mkdir()
    # DownloadClients exists but has no Enable column: we cannot prove inert.
    _db(d / "sonarr.db", ["CREATE TABLE DownloadClients (Id INTEGER PRIMARY KEY, Name TEXT)"])
    with pytest.raises(ns.SanitizeError):
        ns.sanitize("sonarr", d)


def test_arr_missing_db_fails_closed(tmp_path):
    d = tmp_path / "sonarr"
    d.mkdir()
    with pytest.raises(ns.SanitizeError):
        ns.sanitize("sonarr", d)


# ------------------------------------------------------------- prowlarr
def test_prowlarr_apps_sync_disabled(tmp_path):
    d = tmp_path / "prowlarr"
    d.mkdir()
    _db(d / "prowlarr.db", [
        "CREATE TABLE Applications (Id INTEGER PRIMARY KEY, Name TEXT, SyncLevel INTEGER)",
        "CREATE TABLE DownloadClients (Id INTEGER PRIMARY KEY, Enable INTEGER)",
        "CREATE TABLE Notifications (Id INTEGER PRIMARY KEY, Name TEXT)",
        "CREATE TABLE Indexers (Id INTEGER PRIMARY KEY, Enable INTEGER)",
    ], [
        "INSERT INTO Applications VALUES (1,'sonarr',2),(2,'radarr',1),(3,'x',0)",
        "INSERT INTO DownloadClients VALUES (1,1)",
        "INSERT INTO Notifications VALUES (1,'d')",
        "INSERT INTO Indexers VALUES (1,1)",
    ])
    (d / "config.xml").write_text("<Config><UpdateAutomatically>True</UpdateAutomatically></Config>")
    counts = ns.sanitize("prowlarr", d)
    assert sum(counts.values()) == 0
    db = d / "prowlarr.db"
    assert _q(db, "SELECT count(*) FROM Applications WHERE SyncLevel!=0") == [(0,)]
    assert _q(db, "SELECT count(*) FROM Notifications") == [(0,)]
    assert _q(db, "SELECT count(*) FROM DownloadClients WHERE Enable=1") == [(0,)]
    # indexers stay enabled: the one manual proof search needs them (spec section 6)
    assert _q(db, "SELECT Enable FROM Indexers") == [(1,)]


# --------------------------------------------------------------- bazarr
def test_bazarr_providers_and_arr_sync_off(tmp_path):
    d = tmp_path / "bazarr"
    (d / "config").mkdir(parents=True)
    (d / "config" / "config.yaml").write_text(yaml.safe_dump({
        "general": {"use_sonarr": True, "use_radarr": True, "auto_update": True,
                    "enabled_providers": ["opensubtitlescom", "podnapisi"], "port": 6767},
        "analytics": {"enabled": True},
    }))
    (d / "db").mkdir()
    _db(d / "db" / "bazarr.db", [
        "CREATE TABLE table_settings_notifier (name TEXT, url TEXT, enabled INTEGER)",
    ], ["INSERT INTO table_settings_notifier VALUES ('discord','x',1),('tg','y',1)"])
    counts = ns.sanitize("bazarr2", d)
    assert sum(counts.values()) == 0
    cfg = yaml.safe_load((d / "config" / "config.yaml").read_text())
    assert cfg["general"]["enabled_providers"] == []
    assert cfg["general"]["use_sonarr"] is False and cfg["general"]["use_radarr"] is False
    assert cfg["general"]["auto_update"] is False
    assert cfg["general"]["port"] == 6767
    assert _q(d / "db" / "bazarr.db",
              "SELECT count(*) FROM table_settings_notifier WHERE enabled=1") == [(0,)]


# ---------------------------------------------------------------- seerr
def test_seerr_settings_json(tmp_path):
    d = tmp_path / "seerr"
    d.mkdir()
    (d / "settings.json").write_text(json.dumps({
        "main": {"apiKey": "k"},
        "radarr": [{"name": "r", "hostname": "h"}],
        "sonarr": [{"name": "s"}, {"name": "s2"}],
        "plex": {"name": "p", "libraries": [{"id": "1", "enabled": True}, {"id": "2", "enabled": True}]},
        "notifications": {"agents": {
            "discord": {"enabled": True, "options": {"webhookUrl": "u"}},
            "email": {"enabled": True, "options": {}},
        }},
    }))
    counts = ns.sanitize("seerr", d)
    assert sum(counts.values()) == 0
    s = json.loads((d / "settings.json").read_text())
    assert s["radarr"] == [] and s["sonarr"] == []
    assert all(not a["enabled"] for a in s["notifications"]["agents"].values())
    assert all(not lib["enabled"] for lib in s["plex"]["libraries"])
    assert s["main"]["apiKey"] == "k"


def test_seerr_missing_settings_fails_closed(tmp_path):
    d = tmp_path / "seerr"
    d.mkdir()
    with pytest.raises(ns.SanitizeError):
        ns.sanitize("seerr", d)


# ------------------------------------------------------------- tautulli
def test_tautulli_notifiers_newsletters_off(tmp_path):
    d = tmp_path / "tautulli"
    d.mkdir()
    _db(d / "tautulli.db", [
        "CREATE TABLE notifiers (id INTEGER PRIMARY KEY, agent_id INTEGER, notify_on_play INTEGER)",
        "CREATE TABLE newsletters (id INTEGER PRIMARY KEY, agent_id INTEGER, active INTEGER)",
    ], ["INSERT INTO notifiers VALUES (1,1,1)", "INSERT INTO newsletters VALUES (1,1,1)"])
    (d / "config.ini").write_text(
        "[General]\ncheck_github = 1\ncheck_github_on_startup = 1\nhttp_port = 8181\n")
    counts = ns.sanitize("tautulli", d)
    assert sum(counts.values()) == 0
    assert _q(d / "tautulli.db", "SELECT count(*) FROM notifiers") == [(0,)]
    assert _q(d / "tautulli.db", "SELECT count(*) FROM newsletters WHERE active=1") == [(0,)]
    ini = (d / "config.ini").read_text()
    assert "check_github = 0" in ini and "check_github_on_startup = 0" in ini
    assert "http_port = 8181" in ini


# ------------------------------------------------------------------ sab
SAB_INI = """__version__ = 19
[misc]
check_new_rel = 1
port = 17007
[servers]
[[news.frugal.example]]
enable = 1
host = news.frugal.example
[[second]]
enable = 1
[rss]
[[feed1]]
enable = 1
[categories]
[[*]]
name = *
"""


def test_sab_servers_and_rss_off(tmp_path):
    d = tmp_path / "sabnzbd"
    d.mkdir()
    (d / "sabnzbd.ini").write_text(SAB_INI)
    counts = ns.sanitize("sabnzbd", d)
    assert sum(counts.values()) == 0
    out = (d / "sabnzbd.ini").read_text()
    assert out.count("enable = 0") == 3 and "enable = 1" not in out
    assert "check_new_rel = 0" in out and "port = 17007" in out
    assert "host = news.frugal.example" in out


# ------------------------------------------------------- refusal & misc
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def test_refuses_live_apps_path(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    live = tmp_path / ".apps" / "prowlarr"
    live.mkdir(parents=True)
    _db(live / "prowlarr.db", ["CREATE TABLE Applications (Id INTEGER, SyncLevel INTEGER)"],
        ["INSERT INTO Applications VALUES (1,2)"])
    with pytest.raises(ns.LivePathRefused):
        ns.sanitize("prowlarr", live)
    # untouched
    assert _q(live / "prowlarr.db", "SELECT SyncLevel FROM Applications") == [(2,)]


def test_refuses_live_path_via_symlink_and_subdir(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    live = tmp_path / ".apps" / "seerr" / "config"
    live.mkdir(parents=True)
    with pytest.raises(ns.LivePathRefused):
        ns.sanitize("seerr", live)
    link = tmp_path / "innocent"
    try:
        link.symlink_to(tmp_path / ".apps" / "seerr", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ns.LivePathRefused):
        ns.sanitize("seerr", link)


def test_allows_prove_dir_under_apps(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    d = tmp_path / ".apps" / ".prove" / "seerr"
    d.mkdir(parents=True)
    (d / "settings.json").write_text(json.dumps({"radarr": [{"x": 1}]}))
    ns.sanitize("seerr", d)
    assert json.loads((d / "settings.json").read_text())["radarr"] == []


def test_unknown_slug_refused(tmp_path):
    with pytest.raises(ns.SanitizeError):
        ns.sanitize("plex", tmp_path)


def test_idempotent(tmp_path):
    d = _sonarr_dir(tmp_path)
    ns.sanitize("sonarr", d)
    assert sum(ns.sanitize("sonarr", d).values()) == 0


def test_cli_exit_codes(tmp_path):
    d = _sonarr_dir(tmp_path)
    assert ns.main(["sonarr", str(d)]) == 0
    assert ns.main(["plex", str(d)]) == 2
