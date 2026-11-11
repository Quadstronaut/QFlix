"""QFLX-123: only member-facing failures page Discord; the rest roll up weekly.

Operator, 2026-11-11: "qflix to basically be silent and stable, with weekly
notifications and checks." Measured that day: 86 monitors all wired to Discord,
4 real monitor-specific flaps in 7 days, ~3-4 Discord sends a day. The product
was stable; the alert stream was the noise.

These tests pin the one list (manifest `paging:`) and every consumer of it:
lib.notify's gate, the weekly roll-up file, the Kuma webhook's edge recorder,
the migration mute/loud helper, the cutover check, and live-audit L-09.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "maint"))

from lib import manifest as manifest_mod  # noqa: E402
from lib import paging  # noqa: E402

REAL_MANIFEST = REPO / "manifest" / "apps.yaml"

# The member-facing set as decided for QFLX-123. Changing it is a policy
# decision; this pin makes that decision visible in review instead of a quiet
# manifest edit that starts paging the operator again.
EXPECTED = {
    "Plex", "Canary Plex Playback", "Seerr", "Canary Movie", "Canary Anime",
    "QFlix Dashboard", "Canary Mobile-UX", "Canary Dash Asset Integrity",
    "Canary Quota", "QFlix Entitlement Gate", "Manitoba Pusher", "QFlix Fleet",
    "Canary REA Liveness",
}


def test_member_facing_count_is_thirteen():
    assert len(EXPECTED) == 13


def _load_script(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve their module via sys.modules
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fixture_manifest(tmp_path, monkeypatch):
    m = tmp_path / "apps.yaml"
    m.write_text(
        'paging:\n  "Plex": "members cannot stream"\n'
        'apps:\n  plex:\n    class: ucc\n    kuma_monitor: "Plex"\n'
        '  sonarr:\n    class: systemd\n    kuma_monitor: "Sonarr"\n',
        encoding="utf-8")
    monkeypatch.setenv("MANITOBA_MANIFEST_PATH", str(m))
    return m


def _weekly(tmp_path=None):
    p = paging.weekly_events_path()
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


# --- the one list -----------------------------------------------------------

def test_real_manifest_declares_exactly_the_decided_member_facing_set():
    m = manifest_mod.load(REAL_MANIFEST)
    assert set(m.paging_monitors()) == EXPECTED


def test_every_member_facing_name_is_a_real_monitor():
    """A typo in `paging:` would page nobody. Every name must be a monitor the
    bootstrap creates: an app, a canary, the two daemon monitors, or a
    standalone self-pusher."""
    from lib.kuma import STANDALONE_SELF_PUSH_MONITORS
    m = manifest_mod.load(REAL_MANIFEST)
    known = m.all_kuma_monitor_names() | {"Manitoba Pusher", "QFlix Fleet"} \
        | set(STANDALONE_SELF_PUSH_MONITORS)
    assert set(m.paging_monitors()) - known == set()


def test_every_member_facing_entry_carries_a_reason():
    m = manifest_mod.load(REAL_MANIFEST)
    assert all(len(why) > 10 for why in m.paging_monitors().values())


@pytest.mark.parametrize("block", [
    '"Plex": ""', '"Plex": null', '"Plex": 3',
])
def test_an_unjustified_entry_fails_the_manifest_load(tmp_path, block):
    p = tmp_path / "apps.yaml"
    p.write_text("paging:\n  %s\napps: {}\n" % block, encoding="utf-8")
    with pytest.raises(manifest_mod.ManifestError):
        manifest_mod.load(p)


def test_paging_block_must_be_a_mapping(tmp_path):
    p = tmp_path / "apps.yaml"
    p.write_text('paging:\n  - "Plex"\napps: {}\n', encoding="utf-8")
    with pytest.raises(manifest_mod.ManifestError):
        manifest_mod.load(p)


def test_lightweight_reader_agrees_with_the_manifest_loader(monkeypatch):
    monkeypatch.setenv("MANITOBA_MANIFEST_PATH", str(REAL_MANIFEST))
    assert paging.paging_monitors() == manifest_mod.load(REAL_MANIFEST).paging_monitors()
    assert paging.monitor_for_app("plex") == "Plex"
    assert paging.monitor_for_app("qflix-dash") == "QFlix Dashboard"
    assert paging.monitor_for_app("no-such-app") is None


# --- the gate ---------------------------------------------------------------

def test_untagged_never_pages(fixture_manifest):
    assert paging.should_page(None) is False
    assert paging.should_page("") is False


def test_member_facing_source_pages(fixture_manifest):
    assert paging.should_page("Plex") is True


def test_other_source_does_not_page(fixture_manifest):
    assert paging.should_page("Sonarr") is False


@pytest.mark.parametrize("body", ["apps: {}\n", "paging: {}\napps: {}\n",
                                  "paging:\napps: {}\n"])
def test_absent_or_empty_paging_block_fails_open(tmp_path, monkeypatch, body):
    """Review finding A: {} would mean total silence - every tagged page quiet
    and the bootstrap stripping Discord everywhere. It must read as unknown."""
    m = tmp_path / "apps.yaml"
    m.write_text(body, encoding="utf-8")
    monkeypatch.setenv("MANITOBA_MANIFEST_PATH", str(m))
    assert paging.paging_monitors() is None
    assert paging.manifest_readable() is True
    assert paging.should_page("Sonarr") is True
    assert paging.should_page(None) is False


def test_bootstrap_refuses_to_run_with_no_paging_block():
    """Review finding A: guard sits before the reconcile and exits non-zero."""
    src = (REPO / "scripts/maint/bootstrap-kuma-monitors.py").read_text(encoding="utf-8")
    main = src[src.index("def main("):]
    guard = main.index("FATAL: manifest has no paging: block")
    reconcile = main.index("_ensure_notifications_attached(")
    assert main.index("if not paging:") < guard < reconcile
    assert "return 1" in main[guard:reconcile]


def test_unreadable_manifest_fails_open_for_a_tagged_page(tmp_path, monkeypatch):
    """alerting-needs-cross-run-dedup law: a read error must never eat a page."""
    monkeypatch.setenv("MANITOBA_MANIFEST_PATH", str(tmp_path / "missing.yaml"))
    assert paging.should_page("Sonarr") is True
    assert paging.should_page(None) is False


# --- lib.notify -------------------------------------------------------------

def _hook(tmp_path, monkeypatch):
    sd = tmp_path / "secrets"
    sd.mkdir()
    (sd / "discord-webhook.url").write_text("https://discord.com/api/webhooks/1/x\n")
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(sd))


def _ok():
    r = MagicMock()
    r.status_code = 204
    r.raise_for_status.return_value = None
    return r


def test_non_member_facing_notify_posts_nothing_and_rolls_up(tmp_path, monkeypatch,
                                                             fixture_manifest):
    from lib import notify
    _hook(tmp_path, monkeypatch)
    with patch("requests.post") as post:
        assert notify.notify("audio-disposition: fixed 2 file(s)", "info") is True
    assert not post.called
    log = (paging._state_dir() / "notify.log").read_text(encoding="utf-8")
    assert "\tweekly\t" in log
    (rec,) = _weekly()
    assert rec["kind"] == "notify" and rec["monitor"] is None
    assert rec["state"] == "info" and rec["msg"] == "audio-disposition: fixed 2 file(s)"
    assert set(rec) == {"ts", "kind", "monitor", "origin", "state", "msg"}


def test_member_facing_notify_pages_and_is_not_rolled_up(tmp_path, monkeypatch,
                                                         fixture_manifest):
    from lib import notify
    _hook(tmp_path, monkeypatch)
    with patch("requests.post", return_value=_ok()) as post:
        assert notify.notify("plex could not be started", "error", source="Plex") is True
    assert post.call_count == 1
    assert _weekly() == []


def test_tagged_non_member_source_is_recorded_under_its_monitor(tmp_path, monkeypatch,
                                                                fixture_manifest):
    from lib import notify
    _hook(tmp_path, monkeypatch)
    with patch("requests.post") as post:
        notify.notify("sonarr recovered", "info", source="Sonarr")
    assert not post.called
    assert _weekly()[0]["monitor"] == "Sonarr"


def test_summary_replaces_the_message_in_the_roll_up(tmp_path, monkeypatch,
                                                     fixture_manifest):
    """Entitlement alerts name accounts; the roll-up must carry only the
    caller's member-free summary."""
    from lib import notify
    notify.notify("QFlix entitlement: someone@example.org holds 5 sections",
                  "warning", summary="entitlement: per-account alert")
    assert _weekly()[0]["msg"] == "entitlement: per-account alert"


def test_roll_up_masks_email_and_keeps_one_line(fixture_manifest):
    paging.record_weekly("notify", None, "warning",
                         "a@b.example.com did x\n2020-01-01T00:00:00Z\tforged")
    raw = paging.weekly_events_path().read_text(encoding="utf-8")
    assert raw.count("\n") == 1, "a hostile payload forged a second record"
    rec = json.loads(raw)
    assert rec["msg"] == "<email> did x"


def test_warn_level_is_normalised(tmp_path, monkeypatch, fixture_manifest):
    """qflix-entitlement passed "warn", which matched no colour or mention."""
    from lib import notify
    _hook(tmp_path, monkeypatch)
    with patch("requests.post", return_value=_ok()) as post:
        notify.notify("tripwire", "warn", source="Plex")
    assert post.call_args.kwargs["json"]["embeds"][0]["title"] == "[WARNING]"


def test_roll_up_rotates_past_its_byte_cap(fixture_manifest, monkeypatch):
    monkeypatch.setattr(paging, "_WEEKLY_MAX_BYTES", 200)
    monkeypatch.setattr(paging, "_WEEKLY_KEEP_LINES", 3)
    for i in range(10):
        paging.record_weekly("kuma", "Sonarr", "down", "event %d" % i)
    recs = _weekly()
    assert len(recs) <= 3 and recs[-1]["msg"] == "event 9"


# --- tagged direct senders --------------------------------------------------

def test_recovery_pages_under_the_apps_monitor(fixture_manifest):
    from lib import recovery
    assert recovery._page_source("plex") == "Plex"
    assert recovery._page_source("sonarr") == "Sonarr"
    assert recovery._page_source("ghost") == "ghost"


def test_recovery_emit_tags_its_notify(fixture_manifest, tmp_path):
    from lib import recovery
    with patch("lib.recovery.notify.notify") as n, patch("lib.recovery.state.record"):
        recovery._emit("failed", "plex", 3, "down", "n/a", "plex down", "error")
    assert n.call_args.kwargs["source"] == "Plex"


def test_storm_and_tripwire_are_tagged_member_facing():
    """Source pins: the two non-recovery pages must carry their monitor, or
    they silently become weekly-only."""
    pusher = (REPO / "scripts/maint/lib/pusher.py").read_text(encoding="utf-8")
    assert pusher.count('source="QFlix Fleet"') == 2
    ent = (REPO / "scripts/maint/qflix-entitlement.py").read_text(encoding="utf-8")
    assert 'GATE_MONITOR = "QFlix Entitlement Gate"' in ent
    # tripwire + per-account alert + countdown digest (arbiter ruling: the
    # last two are the human check before a member loses access)
    assert ent.count("source=GATE_MONITOR") == 3
    for anchor in ('"QFlix entitlement: " + msg', "% p.alert",
                   '"QFlix entitlement countdown'):
        i = ent.index(anchor)
        call = ent[ent.rindex("_notify(", 0, i):ent.index("\n\n", i)]
        assert "source=GATE_MONITOR" in call, anchor
    applied = ent[ent.index('"QFlix entitlement: %s" % line'):]
    assert "source=" not in applied.split(")", 1)[0], "applied changes stay weekly"


def test_entitlement_notifications_page_under_the_real_manifest(monkeypatch):
    """With the real list, the gate's tagged notifies reach Discord WITH their
    detail (the summary is only for the roll-up file)."""
    from lib import notify
    monkeypatch.setenv("MANITOBA_MANIFEST_PATH", str(REAL_MANIFEST))
    ent = _load_script("qflix_entitlement_t", "scripts/maint/qflix-entitlement.py")
    with patch.object(notify, "_try_read_webhook_url",
                      return_value="https://discord.example/webhooks/1/y"), \
         patch.object(notify, "_post_discord", return_value=(True, "")) as post:
        ent._notify("QFlix entitlement countdown\nhousehold A: 3d", "info",
                    source=ent.GATE_MONITOR, summary="entitlement: countdown digest")
    assert post.call_count == 1
    assert "household A" in post.call_args.args[1]


# --- Kuma webhook: the eyes on every suppressed Kuma edge --------------------

def test_webhook_records_non_member_edges_only(fixture_manifest):
    from lib import kuma
    m = manifest_mod.load(fixture_manifest)
    kuma._record_unpaged_edge(m, "Sonarr", 0, {"heartbeat": {"msg": "timeout"}})
    kuma._record_unpaged_edge(m, "Sonarr", 2, {"heartbeat": {"msg": "pending"}})
    kuma._record_unpaged_edge(m, "Plex", 0, {"heartbeat": {"msg": "down"}})
    kuma._record_unpaged_edge(m, "Sonarr", 1, {"heartbeat": {"msg": "ok"}})
    recs = _weekly()
    assert [(r["monitor"], r["state"], r["msg"]) for r in recs] == [
        ("Sonarr", "down", "timeout"), ("Sonarr", "up", "ok")]
    assert all(r["kind"] == "kuma" for r in recs)


def test_webhook_ignores_monitors_it_does_not_own(fixture_manifest):
    """The 240 install smoke POSTs a fake 'InstallSmokeNonExistent' DOWN; the
    first deploy (2026-11-11) wrote it into the weekly roll-up. Owned
    non-manifest monitors (daemon + standalone self-pushers) still count."""
    from lib import kuma
    m = manifest_mod.load(fixture_manifest)
    kuma._record_unpaged_edge(m, "InstallSmokeNonExistent", 0,
                              {"heartbeat": {"msg": "install-smoke"}})
    kuma._record_unpaged_edge(m, "QFlix Reaper", 0, {"heartbeat": {"msg": "x"}})
    kuma._record_unpaged_edge(m, "Manitoba Pusher", 0, {"heartbeat": {"msg": "y"}})
    assert [r["monitor"] for r in _weekly()] == ["QFlix Reaper", "Manitoba Pusher"]


def test_webhook_recorder_never_raises():
    from lib import kuma
    kuma._record_unpaged_edge(object(), "X", 0, {"heartbeat": "not-a-dict"})


# --- migration helper: loud = the member-facing set, not everything ----------

def test_kuma_channels_mirror_reads_the_same_set():
    kc = _load_script("kuma_channels", "scripts/migrate/kuma_channels.py")
    assert kc.paging_names(REAL_MANIFEST) == EXPECTED


def test_kuma_channels_loud_follows_the_paging_set():
    kc = _load_script("kuma_channels", "scripts/migrate/kuma_channels.py")
    mons = [{"id": 1, "name": "Plex", "notificationIDList": {"2": True}},
            {"id": 2, "name": "Sonarr", "notificationIDList": {"1": True, "2": True}}]
    assert kc.plan(mons, {1}, "loud", {"Plex"}) == [(1, {1, 2}), (2, {2})]
    # unreadable paging block -> old behaviour: everything loud
    assert kc.plan(mons, {1}, "loud", None) == [(1, {1, 2})]
    s = kc.summary(mons, {1}, {"Plex"})
    assert s["paging_expected"] == 1


def test_kuma_channels_missing_manifest_is_none(tmp_path):
    kc = _load_script("kuma_channels", "scripts/migrate/kuma_channels.py")
    assert kc.paging_names(tmp_path / "nope.yaml") is None


@pytest.mark.parametrize("body", ["apps: {}\n", "paging: {}\n", "paging:\n"])
def test_kuma_channels_empty_block_fails_open_to_all_loud(tmp_path, body):
    """Review finding A: the migration manifest has no block; loud must then
    page on every monitor, never on none."""
    kc = _load_script("kuma_channels", "scripts/migrate/kuma_channels.py")
    m = tmp_path / "apps.yaml"
    m.write_text(body, encoding="utf-8")
    names = kc.paging_names(m)
    assert names is None
    mons = [{"id": 1, "name": "Sonarr", "notificationIDList": {"2": True}}]
    assert kc.plan(mons, {1}, "loud", names) == [(1, {1, 2})]


def test_cutover_loud_check_compares_against_the_paging_set():
    mm = _load_script("migrate_manifest", "scripts/migrate/migrate_manifest.py")
    rows_for = lambda kuma: [r for r in mm.evaluate_green(  # noqa: E731
        {}, {"apps": [], "canaries": []},
        {"gate_dropin": False, "members_armed": False, "parity_violations": [],
         "webhook_parked": False, "ffmpeg_shim": True, "media_files": 1,
         "kuma": kuma}, "post") if r[1] == "kuma-loud"][0][0]
    base = {"reachable": True, "monitors_without_human": 75}
    assert rows_for(dict(base, monitors_with_human=11, paging_expected=11)) == "PASS"
    assert rows_for(dict(base, monitors_with_human=86, paging_expected=11)) == "FAIL"
    assert rows_for(dict(base, monitors_with_human=10, paging_expected=11)) == "FAIL"


# --- live audit L-09 --------------------------------------------------------

def _live():
    return _load_script("qflix_audit_live", "scripts/maint/qflix-audit-live.py")


D, A = paging.DISCORD_CHANNEL_NAME, paging.AUTOHEAL_CHANNEL_NAME


def test_l09_clean_when_wired_to_policy():
    rows = [("Plex", D), ("Plex", A), ("Sonarr", A)]
    assert _live().channel_wiring_findings(rows, {"Plex"}, D, A) == []


def test_l09_catches_both_directions_and_the_born_mute_class():
    rows = [("Plex", A), ("Sonarr", D), ("Sonarr", A), ("New Canary", None)]
    got = {(f["instance_id"], f["detail"]) for f in
           _live().channel_wiring_findings(rows, {"Plex", "Typo"}, D, A)}
    assert got == {("Plex", "member-facing-silent"),
                   ("Sonarr", "pages-not-member-facing"),
                   ("New Canary", "no-auto-heal"),
                   ("Typo", "paging-monitor-absent")}


def test_l09_empty_paging_set_is_a_finding_not_a_pass():
    got = _live().channel_wiring_findings([("Sonarr", A)], set(), D, A)
    assert [f["detail"] for f in got] == ["paging-block-missing"]


def test_l09_leg_reports_missing_block_as_finding(tmp_path, monkeypatch):
    live = _live()
    m = tmp_path / "apps.yaml"
    m.write_text("apps: {}\n", encoding="utf-8")
    monkeypatch.setenv("MANITOBA_MANIFEST_PATH", str(m))
    monkeypatch.setattr(live, "_monitor_channel_rows", lambda: [("Sonarr", A)])
    findings, coverage = [], {}
    live._channel_wiring_leg(findings, coverage)
    assert coverage["L-09"] == "checked"
    assert [f["detail"] for f in findings] == ["paging-block-missing"]


def test_l09_leg_unreadable_manifest_is_unavailable(tmp_path, monkeypatch):
    live = _live()
    monkeypatch.setenv("MANITOBA_MANIFEST_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setattr(live, "_monitor_channel_rows", lambda: [("Sonarr", A)])
    findings, coverage = [], {}
    live._channel_wiring_leg(findings, coverage)
    assert coverage["L-09"] == "unavailable" and findings == []


def test_l09_skips_external_monitors():
    rows = [("Other Project", D)]
    assert _live().channel_wiring_findings(rows, {"Plex"}, D, A, skip={"Other Project"}) == [
        {"class_id": "L-09", "instance_id": "Plex", "detail": "paging-monitor-absent"}]
