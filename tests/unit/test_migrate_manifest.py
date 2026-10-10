"""scripts/migrate python helpers (QFLX-39): tables, verdicts, freeze, re-point, Kuma.

The shell scripts never name an app; these helpers turn manifest/apps.yaml into
every table they use, and hold the logic that is worth pinning without SSH:

  * migrate_manifest: every real manifest app gets a data strategy (fail closed
    on an unknown one), the native-installer list is exactly the ever-UCC set,
    the comms jobs resolve from jobs.yaml, the window comes from hostpolicy,
    and evaluate_green's pre/post verdicts (I-1 muted/loud, I-5 disarmed);
  * freeze: only ACTIVE torrents are snapshotted (the hashes=all gap);
  * repoint: only blue's gateway host and collided ports are rewritten, and an
    entity with nothing to change is not touched (no PUT, I-4);
  * kuma_channels: human channels = default channels minus auto-heal.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MIG = REPO / "scripts" / "migrate"


def _load(name: str):
    key = "_test_mig_" + name
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, str(MIG / (name + ".py")))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


mm = _load("migrate_manifest")
freeze = _load("freeze")
repoint = _load("repoint")
kuma = _load("kuma_channels")
REAL_APPS = yaml.safe_load((REPO / "manifest" / "apps.yaml").read_text(encoding="utf-8"))["apps"]


# --- tables ---------------------------------------------------------------------

def test_every_real_manifest_app_has_a_strategy():
    rows = mm.app_rows()
    assert sorted(r["name"] for r in rows) == sorted(k for k, v in REAL_APPS.items() if isinstance(v, dict))
    assert all(r["strategy"] in mm.STRATEGIES for r in rows)


def test_strategy_tables_only_use_known_strategies():
    assert set(mm.FAMILY_STRATEGY.values()) <= set(mm.STRATEGIES)
    assert set(mm.APP_STRATEGY.values()) <= set(mm.STRATEGIES)


def test_unknown_app_fails_closed():
    with pytest.raises(mm.NoStrategy):
        mm.strategy_of("mystery", {"class": "ucc", "ucc_slug": "mystery"}, fam={})


def test_family_comes_from_native_sanitize():
    fam = mm.families()
    assert fam["sonarr"] == "arr" and fam["seerr"] == "seerr"
    assert mm.strategy_of("radarr2", {"class": "ucc", "ucc_slug": "radarr2"}) == "sqlite-tree"


def test_plex_is_fresh_identity_and_postgres_is_dumped():
    rows = {r["name"]: r for r in mm.app_rows()}
    assert rows["plex"]["strategy"] == "fresh-identity"      # P1, spec section 7
    assert rows["postgres"]["strategy"] == "pg-dump"
    assert rows["qbittorrent"]["strategy"] == "qbit-profile"


def test_green_unit_naming():
    assert mm.green_unit("sonarr", {"class": "ucc", "ucc_slug": "sonarr"}) == "qflix-sonarr.service"
    assert mm.green_unit("bazarr2", {"class": "systemd", "unit": "bazarr2.service"}) == "bazarr2.service"
    assert mm.green_unit("lib", {"class": "library", "unit": None}) == ""


def test_native_installers_are_the_ever_ucc_set(tmp_path):
    (tmp_path / "300-native-unpackerr-install.sh").write_text("#!/bin/sh\n")
    rows = mm.native_installers(tmp_path)
    want = sorted(k for k, v in REAL_APPS.items() if isinstance(v, dict) and v.get("ucc_slug"))
    assert [r["name"] for r in rows] == want
    got = {r["slug"]: r["installer"] for r in rows}
    assert got["unpackerr"] == "300-native-unpackerr-install.sh"
    assert got["sonarr"] == ""


def test_converted_app_still_needs_an_installer(tmp_path):
    man = tmp_path / "apps.yaml"
    man.write_text("apps:\n  sonarr:\n    class: systemd\n    unit: qflix-sonarr.service\n"
                   "    ucc_slug: sonarr\n    ucc_dormant: true\n")
    assert [r["slug"] for r in mm.native_installers(tmp_path, man)] == ["sonarr"]


def test_comms_jobs_resolve_from_jobs_yaml():
    rows = mm.comms_jobs(["qflix-newsletter", "cron-listmonk-sync"])
    assert rows[0] == {"key": "qflix-newsletter", "kind": "timer", "target": "qflix-newsletter.timer"}
    assert rows[1]["kind"] == "cron" and rows[1]["target"].endswith("listmonk-sync.py")


def test_comms_unknown_key_is_an_error():
    with pytest.raises(KeyError):
        mm.comms_jobs(["no-such-job"])


def test_window_comes_from_hostpolicy_ultra():
    mon = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)
    tue = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.timezone.utc)
    assert mm.in_window("ultra", mon) is True
    assert mm.in_window("ultra", tue) is False
    assert mm.in_window("ultra", mon.replace(hour=15)) is False      # end exclusive
    with pytest.raises(ValueError):
        mm.in_window("bogus", mon)


def test_cli_in_window_exit_codes():
    assert mm.main(["in-window", "--profile", "ultra", "--now", "2026-10-12T11:00:00Z"]) == 0
    assert mm.main(["in-window", "--profile", "ultra", "--now", "2026-10-12T10:59:00Z"]) == 1


def test_docker_gateway_is_policy_owned():
    assert mm.docker_gateway("ultra")          # non-empty on Ultra
    assert mm.docker_gateway("generic") == ""


def test_tables_are_deterministic():
    assert mm.app_rows() == mm.app_rows()


# --- evaluate_green -------------------------------------------------------------

APPS = {"sonarr": {}, "plex": {}}


def _status(ok=True):
    return {"apps": [{"app": "sonarr", "ok": ok, "probe_kind": "http_api"},
                     {"app": "plex", "ok": True, "probe_kind": "http_root"}],
            "canaries": [{"name": "movie", "ok": True, "reason": "success", "stale": False}]}


def _facts(**kw):
    f = {"gate_dropin": False, "members_armed": False, "parity_violations": [],
         "webhook_parked": True, "ffmpeg_shim": True, "media_files": 20, "timer_count": 60,
         "kuma": {"reachable": True, "monitors_with_human": 0, "monitors_without_human": 78}}
    f.update(kw)
    return f


def _fails(rows):
    return {c for v, c, _ in rows if v == "FAIL"}


def test_pre_all_green():
    assert _fails(mm.evaluate_green(APPS, _status(), _facts(), "pre", 61, 1)) == set()


@pytest.mark.parametrize("kw,check", [
    ({"gate_dropin": True}, "gate-disarmed"),
    ({"gate_dropin": None}, "gate-disarmed"),
    ({"members_armed": True}, "gate-armed-false"),
    ({"members_armed": None}, "gate-armed-false"),
    ({"parity_violations": ["runtime-parity: two process trees"]}, "runtime-parity"),
    ({"parity_violations": None}, "runtime-parity"),
    ({"webhook_parked": False}, "discord-webhook"),
    ({"ffmpeg_shim": False}, "tdarr-threadcap-shim"),
    ({"media_files": 0}, "media-present"),
    ({"kuma": {"reachable": True, "monitors_with_human": 3}}, "kuma-muted"),
    ({"kuma": {"reachable": False, "error": "x"}}, "kuma"),
])
def test_pre_failures(kw, check):
    assert check in _fails(mm.evaluate_green(APPS, _status(), _facts(**kw), "pre"))


def test_missing_and_down_apps_fail():
    st = _status(ok=False)
    st["apps"] = st["apps"][:1]
    fails = _fails(mm.evaluate_green(APPS, st, _facts(), "pre"))
    assert {"app:sonarr", "app:plex"} <= fails


def test_stale_canary_fails():
    st = _status()
    st["canaries"][0]["stale"] = True
    assert "canary:movie" in _fails(mm.evaluate_green(APPS, st, _facts(), "pre"))


def test_post_requires_loud():
    loud = _facts(webhook_parked=False,
                  kuma={"reachable": True, "monitors_with_human": 78, "monitors_without_human": 0})
    assert _fails(mm.evaluate_green(APPS, _status(), loud, "post")) == set()
    quiet = _facts()     # still parked + no human channels
    fails = _fails(mm.evaluate_green(APPS, _status(), quiet, "post"))
    assert {"kuma-loud", "discord-webhook"} <= fails


def test_timer_baseline_tolerates_held_timers():
    rows = mm.evaluate_green(APPS, _status(), _facts(timer_count=60), "pre", 61, 1)
    assert "timer-count" not in _fails(rows)
    rows = mm.evaluate_green(APPS, _status(), _facts(timer_count=59), "pre", 61, 1)
    assert "timer-count" in _fails(rows)


# --- freeze ---------------------------------------------------------------------

INFO = [{"hash": "a", "state": "downloading"}, {"hash": "b", "state": "stoppedUP"},
        {"hash": "c", "state": "uploading"}, {"hash": "d", "state": "pausedDL"}]


def test_snapshot_keeps_only_active_torrents():
    assert freeze.active_hashes(INFO) == ["a", "c"]


def test_not_in_state_checks_only_the_snapshot():
    assert freeze.not_in_state(INFO, ["a", "c"], want_paused=True) == ["a", "c"]
    assert freeze.not_in_state(INFO, ["b"], want_paused=True) == []
    # an operator-paused torrent outside the snapshot is never touched/required
    assert freeze.not_in_state(INFO, ["a"], want_paused=False) == []


def test_freeze_bad_snapshot_is_usage_error():
    assert freeze.main(["pause", "not json"]) == 2
    assert freeze.main(["resume", '{"no": 1}']) == 2


# --- repoint --------------------------------------------------------------------

OLD, NEW = ["10.9.8.7"], "127.0.0.1"


def test_rewrite_url_host_and_port():
    assert repoint.rewrite_url("http://10.9.8.7:8989/sonarr", OLD, NEW, {}) == "http://127.0.0.1:8989/sonarr"
    assert repoint.rewrite_url("http://127.0.0.1:8989", [], NEW, {"8989": "9999"}) == "http://127.0.0.1:9999"
    assert repoint.rewrite_url("http://other:1/", OLD, NEW, {}) == "http://other:1/"


def test_rewrite_entity_fields_and_top_level():
    ent = {"id": 3, "name": "qBit", "hostname": "10.9.8.7", "port": 8080,
           "fields": [{"name": "host", "value": "10.9.8.7"}, {"name": "port", "value": 8080},
                      {"name": "baseUrl", "value": "http://10.9.8.7:7878"},
                      {"name": "apiKey", "value": "k"}]}
    new, changed = repoint.rewrite_entity(ent, OLD, NEW, {"8080": "8081"})
    assert changed
    assert new["hostname"] == NEW and new["port"] == 8081
    vals = {f["name"]: f["value"] for f in new["fields"]}
    assert vals == {"host": NEW, "port": 8081, "baseUrl": "http://127.0.0.1:7878", "apiKey": "k"}
    assert ent["hostname"] == "10.9.8.7"           # input untouched


def test_rewrite_entity_noop_is_unchanged():
    ent = {"id": 1, "hostname": "127.0.0.1", "port": 1, "fields": [{"name": "apiKey", "value": "k"}]}
    _new, changed = repoint.rewrite_entity(ent, OLD, NEW, {})
    assert changed is False


def test_repoint_nothing_to_do_exits_clean(capsys):
    assert repoint.main(['{"old_hosts": [], "new_host": "127.0.0.1", "port_map": {}}']) == 0
    assert "nothing to rewrite" in capsys.readouterr().out


def test_repoint_bad_plan():
    assert repoint.main(["{not json"]) == 2


# --- kuma -----------------------------------------------------------------------

NOTIFS = [{"id": 1, "name": "Discord", "isDefault": True},
          {"id": 2, "name": kuma.AUTOHEAL, "isDefault": True},
          {"id": 3, "name": "spare", "isDefault": False}]


def test_human_ids_exclude_autoheal():
    assert kuma.human_ids(NOTIFS) == {1}


def test_mute_and_loud_plans_are_idempotent():
    mons = [{"id": 10, "notificationIDList": {"1": True, "2": True}},
            {"id": 11, "notificationIDList": {"2": True}}]
    assert kuma.plan(mons, {1}, "mute") == [(10, {2})]
    assert kuma.plan(mons, {1}, "loud") == [(11, {1, 2})]
    muted = [{"id": 10, "notificationIDList": {"2": True}}]
    assert kuma.plan(muted, {1}, "mute") == []
    s = kuma.summary(mons, {1})
    assert s["monitors_with_human"] == 1 and s["monitors_without_human"] == 1
