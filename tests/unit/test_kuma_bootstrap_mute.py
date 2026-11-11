"""A monitor must never be left with the wrong notification channels.

WHY: on 2026-07-30 `Canary Dash Asset Integrity` was created by
240-maintenance-install.sh and landed with NO notification channels, while the
other 61 active monitors all carried 2. The canary was live, scheduled, and
completely deaf: no Discord, no auto-heal webhook.

`_ensure_notifications_attached` already existed to prevent exactly this (it was
written for the 32-of-60 incident on 2026-07-28) and it is correct in intent. It
misses on the CREATING run because it iterates `api.get_monitors()`, a
client-side cache the server refreshes over the socket; a monitor created
seconds earlier in the same session is not in it yet.

QFLX-123 (2026-11-11) generalised the guard: auto-heal on EVERY monitor,
Discord on EXACTLY the manifest's member-facing `paging:` set (86 monitors all
paging = ~3-4 Discord sends a day from a stable product). `_channel_violations`
replaced `_mute_monitor_names`. Every historical case below still holds; the
new directions (Discord on a non-member monitor, a member-facing one without
it, a paging name with no monitor) are pinned at the bottom.

Policy under test: Discord = channel id 1, auto-heal = id 2, member-facing =
{"Plex"}. So "wired" is [1, 2] for Plex and [2] for everything else.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "scripts" / "maint" / "bootstrap-kuma-monitors.py"
PAGING = {"Plex"}


def _load():
    """Import the hyphenated script by path (not a legal module name)."""
    sys.path.insert(0, str(REPO / "scripts" / "maint"))
    spec = importlib.util.spec_from_file_location("kuma_bootstrap", BOOTSTRAP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _v(mod, api, created=None, paging=PAGING):
    return mod._channel_violations(api, paging, 1, 2, created)


class FakeApi:
    """Stand-in for the reads (and the one write) the bootstrap makes.

    `monitors` is the STALE list the real client caches (get_monitors reads
    `_get_event_data(MONITOR_LIST)`). `live` is what the SERVER would answer to
    `get_monitor(id)` (`_call('getMonitor', id)` - a real round trip). Leaving
    `live` unset means the server agrees with the cache.
    """

    def __init__(self, monitors, live=None):
        self._monitors = monitors
        self._live = live or {}
        self.live_reads = []
        self.edits = []

    def get_monitors(self):
        return self._monitors

    def get_monitor(self, mid):
        self.live_reads.append(mid)
        if mid in self._live:
            return self._live[mid]
        for m in self._monitors:
            if m["id"] == mid:
                return m
        raise RuntimeError(f"no such monitor {mid}")

    def edit_monitor(self, mid, **kw):
        self.edits.append((mid, kw))


def _mon(mid, name, channels, active=True):
    return {
        "id": mid,
        "name": name,
        "active": active,
        "notificationIDList": {str(c): True for c in channels},
    }


def test_names_the_monitor_that_has_no_channels(mod):
    api = FakeApi([
        _mon(1, "Plex", [1, 2]),
        _mon(110, "Canary Dash Asset Integrity", []),
    ])
    assert _v(mod, api) == ["Canary Dash Asset Integrity"]


def test_silent_when_every_monitor_is_wired(mod):
    api = FakeApi([_mon(1, "Plex", [1, 2]), _mon(2, "Sonarr", [2])])
    assert _v(mod, api) == []


def test_a_channel_switched_off_still_counts_as_mute(mod):
    """Kuma stores the set as {id: bool}; a False is not an attachment."""
    api = FakeApi([_mon(1, "Plex", [1, 2]), {
        "id": 7, "name": "Canary Quota", "active": True,
        "notificationIDList": {"1": False, "2": False},
    }])
    assert _v(mod, api) == ["Canary Quota"]


def test_discord_without_auto_heal_is_still_miswired(mod):
    """Pages a human but lib.recovery never hears of it."""
    api = FakeApi([_mon(3, "Plex", [1])])
    assert _v(mod, api) == ["Plex"]


def test_paused_monitors_are_ignored(mod):
    """A paused monitor cannot go red, so it cannot go red in silence."""
    api = FakeApi([_mon(1, "Plex", [1, 2]), _mon(4, "Retired Thing", [], active=False)])
    assert _v(mod, api) == []


@pytest.mark.parametrize("shape", [{}, None, []])
def test_missing_or_empty_notification_field_is_mute(mod, shape):
    api = FakeApi([_mon(1, "Plex", [1, 2]),
                   {"id": 5, "name": "Canary X", "active": True,
                    "notificationIDList": shape}])
    assert _v(mod, api) == ["Canary X"]


def test_verifier_does_not_crash_the_run_when_kuma_is_unreadable(mod):
    class Broken:
        def get_monitors(self):
            raise RuntimeError("socket closed")

    assert _v(mod, Broken()) == []


def test_result_is_sorted_so_output_is_stable(mod):
    api = FakeApi([_mon(9, "Plex", [1, 2]), _mon(1, "Zeta", []),
                   _mon(2, "Alpha", []), _mon(3, "Mid", [])])
    assert _v(mod, api) == ["Alpha", "Mid", "Zeta"]


def test_stale_cache_accusation_is_dropped_when_the_server_says_wired(mod):
    """The false FATAL of 2026-08-03, pinned. The cache may accuse; only the
    server convicts."""
    api = FakeApi(
        monitors=[_mon(1, "Plex", [1, 2]),
                  _mon(115, "Canary Prowlarr App Sync", []),
                  _mon(116, "Canary Plex Unmatched", []),
                  _mon(117, "Canary REA Liveness", [])],
        live={115: _mon(115, "Canary Prowlarr App Sync", [2]),
              116: _mon(116, "Canary Plex Unmatched", [2]),
              117: _mon(117, "Canary REA Liveness", [2])},
    )
    assert _v(mod, api) == []


def test_a_monitor_the_server_agrees_is_mute_still_fails(mod):
    api = FakeApi(monitors=[_mon(1, "Plex", [1, 2]),
                            _mon(110, "Canary Dash Asset Integrity", [])])
    assert _v(mod, api) == ["Canary Dash Asset Integrity"]
    assert api.live_reads == [110], "the accusation was never checked live"


def test_a_monitor_the_server_cannot_answer_for_stays_accused(mod):
    class HalfBroken(FakeApi):
        def get_monitor(self, mid):
            raise RuntimeError("socket closed")

    api = HalfBroken(monitors=[_mon(1, "Plex", [1, 2]), _mon(9, "Canary Ghost", [])])
    assert _v(mod, api) == ["Canary Ghost"]


def test_wired_monitors_cost_no_server_round_trips(mod):
    api = FakeApi(monitors=[_mon(1, "Plex", [1, 2]), _mon(2, "Sonarr", [2])])
    assert _v(mod, api) == []
    assert api.live_reads == []


def test_main_asserts_before_returning_success(mod):
    """The verification must be WIRED, not merely defined. Deleting the call
    from main() leaves every unit test above passing, which is precisely how
    the original guard shipped ineffective."""
    src = BOOTSTRAP.read_text(encoding="utf-8")
    main_src = src[src.index("def main("):]
    first = None
    for line in main_src.splitlines():
        stripped = line.strip()
        if stripped.startswith("mute ") and "=" in stripped:
            first = stripped
            break
    assert first == "mute = _channel_violations(api, paging, discord_id, autoheal_id,", (
        "main()'s first `mute` assignment must come from the verifier and pass "
        f"the paging set; found: {first!r}")
    first_call = main_src[main_src.index("mute = _channel_violations("):]
    assert first_call.split(")", 1)[0].count("created_names") == 1, \
        "the verifier must receive created_names (2026-07-30 / 2026-08-07)"
    assert main_src.count("created_names.add(") == 3, (
        "expected 3 created_names.add() calls (apps, canaries, standalone "
        f"self-pushers); found {main_src.count('created_names.add(')}")
    tail = main_src[main_src.index("mute = _channel_violations("):]
    assert "return 1" in tail, \
        "main() no longer fails the run when monitors are mis-wired"
    assert "paging = set(manifest.paging_monitors())" in main_src


def test_a_created_monitor_absent_from_the_cache_is_accused_not_skipped(mod):
    api = FakeApi([_mon(1, "Plex", [1, 2])])          # cache has NOT caught up
    assert _v(mod, api, {"Canary Tdarr Pause Integrity"}) == [
        "Canary Tdarr Pause Integrity"]


def test_created_monitor_present_and_wired_is_not_accused(mod):
    api = FakeApi([_mon(1, "Plex", [1, 2]),
                   _mon(119, "Canary Tdarr Pause Integrity", [2])])
    assert _v(mod, api, {"Canary Tdarr Pause Integrity"}) == []


def test_created_monitor_present_but_mute_is_accused_once_not_twice(mod):
    api = FakeApi([_mon(1, "Plex", [1, 2]),
                   _mon(119, "Canary Tdarr Pause Integrity", [])])
    assert _v(mod, api, {"Canary Tdarr Pause Integrity"}) == [
        "Canary Tdarr Pause Integrity"]


def test_unreadable_kuma_still_accuses_what_this_run_created(mod):
    class Broken:
        def get_monitors(self):
            raise RuntimeError("socket closed")

    assert _v(mod, Broken(), {"Canary New"}) == ["Canary New"]
    assert _v(mod, Broken()) == []


def test_no_creates_means_no_added_accusations(mod):
    api = FakeApi([_mon(1, "Plex", [1, 2])])
    assert _v(mod, api, set()) == []
    assert _v(mod, api, None) == []


# --- QFLX-123: the two new directions ----------------------------------------


def test_discord_on_a_non_member_monitor_is_accused(mod):
    """The noise this ticket removed, creeping back (a UI-created monitor gets
    Discord as a default channel)."""
    api = FakeApi([_mon(1, "Plex", [1, 2]), _mon(2, "Sonarr", [1, 2])])
    assert _v(mod, api) == ["Sonarr"]


def test_member_facing_without_discord_is_accused(mod):
    """A Plex outage that pages nobody is the worst outcome of quiet paging."""
    api = FakeApi([_mon(1, "Plex", [2])])
    assert _v(mod, api) == ["Plex"]


def test_paging_name_with_no_monitor_is_accused(mod):
    """A typo in the paging block must not silently page nobody."""
    api = FakeApi([_mon(1, "Plex", [1, 2])])
    assert _v(mod, api, paging={"Plex", "Pelx"}) == ["Pelx"]


def test_other_channels_are_left_alone(mod):
    """Channel 3 (an external project's) is neither required nor stripped."""
    api = FakeApi([_mon(1, "Plex", [1, 2, 3]), _mon(2, "Sonarr", [2, 3])])
    assert _v(mod, api) == []


def test_reconcile_strips_discord_from_non_members_and_keeps_auto_heal(mod):
    api = FakeApi([_mon(1, "Plex", [2]), _mon(2, "Sonarr", [1, 2]),
                   _mon(3, "Radarr", [2]), _mon(4, "Canary New", [])])
    fixed = mod._ensure_notifications_attached(api, PAGING, 1, 2)
    assert fixed == 3
    got = {mid: {k for k, v in kw["notificationIDList"].items() if v}
           for mid, kw in api.edits}
    assert got == {1: {"1", "2"}, 2: {"2"}, 4: {"2"}}
    assert api.edits[1][1]["notificationIDList"]["1"] is False


def test_reconcile_skips_external_monitors(mod):
    api = FakeApi([_mon(9, "Other Project", [1])])
    assert mod._ensure_notifications_attached(api, PAGING, 1, 2,
                                              skip_names={"Other Project"}) == 0


def test_channel_roles_resolve_by_name_not_default_flag(mod):
    notifs = [{"id": 7, "name": "Mission Control - QFlix", "isDefault": False},
              {"id": 8, "name": "Manitoba auto-heal webhook", "isDefault": True},
              {"id": 3, "name": "Mission Control - qAlert Critical", "isDefault": True}]
    assert mod._channel_roles(notifs) == (7, 8)
    assert mod._channel_roles([]) == (None, None)
