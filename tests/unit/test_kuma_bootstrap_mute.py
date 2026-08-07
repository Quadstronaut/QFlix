"""A monitor must never be left with zero notification channels.

WHY: on 2026-07-30 `Canary Dash Asset Integrity` was created by
240-maintenance-install.sh and landed with NO notification channels, while the
other 61 active monitors all carried 2. The canary was live, scheduled, and
completely deaf: no Discord, no auto-heal webhook.

`_ensure_notifications_attached` already existed to prevent exactly this (it was
written for the 32-of-60 incident on 2026-07-28) and it is correct in intent. It
misses on the CREATING run because it iterates `api.get_monitors()`, a
client-side cache the server refreshes over the socket; a monitor created
seconds earlier in the same session is not in it yet. So the loop never sees the
monitor at all -- no [notify] line, no [notify-fail] line, nothing to notice.
Reconcile-on-every-run then repairs it on the NEXT run, which means the guard
ships deaf and stays deaf until someone happens to re-run the bootstrap.

These tests pin the verification that turns that silent miss into a loud
failure. They deliberately do NOT test the race itself -- the race lives in the
API client's cache and cannot be reproduced without it. They test the property
that makes the race survivable: the run refuses to report success while any
active monitor is mute.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "scripts" / "maint" / "bootstrap-kuma-monitors.py"


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


class FakeApi:
    """Minimal stand-in for the two reads the verifier makes.

    `monitors` is the STALE list the real client caches (get_monitors reads
    `_get_event_data(MONITOR_LIST)`). `live` is what the SERVER would answer to
    `get_monitor(id)` (`_call('getMonitor', id)` — a real round trip). Leaving
    `live` unset means the server agrees with the cache, which is the ordinary
    case; setting it models the create-run lag that produced the false FATAL.
    """

    def __init__(self, monitors, live=None):
        self._monitors = monitors
        self._live = live or {}
        self.live_reads = []

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
    assert mod._mute_monitor_names(api) == ["Canary Dash Asset Integrity"]


def test_silent_when_every_monitor_is_wired(mod):
    api = FakeApi([_mon(1, "Plex", [1, 2]), _mon(2, "Sonarr", [1, 2])])
    assert mod._mute_monitor_names(api) == []


def test_a_channel_switched_off_still_counts_as_mute(mod):
    """Kuma stores the set as {id: bool}; a False is not an attachment."""
    api = FakeApi([{
        "id": 7, "name": "Canary Quota", "active": True,
        "notificationIDList": {"1": False, "2": False},
    }])
    assert mod._mute_monitor_names(api) == ["Canary Quota"]


def test_partial_attachment_is_not_mute(mod):
    """One channel is a different (lesser) problem -- reconcile's job, not this."""
    api = FakeApi([_mon(3, "Canary Movie", [1])])
    assert mod._mute_monitor_names(api) == []


def test_paused_monitors_are_ignored(mod):
    """A paused monitor cannot go red, so it cannot go red in silence."""
    api = FakeApi([_mon(4, "Retired Thing", [], active=False)])
    assert mod._mute_monitor_names(api) == []


@pytest.mark.parametrize("shape", [{}, None, []])
def test_missing_or_empty_notification_field_is_mute(mod, shape):
    api = FakeApi([{"id": 5, "name": "Canary X", "active": True,
                    "notificationIDList": shape}])
    assert mod._mute_monitor_names(api) == ["Canary X"]


def test_verifier_does_not_crash_the_run_when_kuma_is_unreadable(mod):
    """An unreadable Kuma must not masquerade as 'everything is fine'...

    ...but it also must not hard-fail a deploy over a transient read. It
    returns empty and warns; the reconcile above it has its own warning path.
    """
    class Broken:
        def get_monitors(self):
            raise RuntimeError("socket closed")

    assert mod._mute_monitor_names(Broken()) == []


def test_result_is_sorted_so_output_is_stable(mod):
    api = FakeApi([_mon(1, "Zeta", []), _mon(2, "Alpha", []), _mon(3, "Mid", [])])
    assert mod._mute_monitor_names(api) == ["Alpha", "Mid", "Zeta"]


def test_stale_cache_accusation_is_dropped_when_the_server_says_wired(mod):
    """The false FATAL of 2026-08-03, pinned.

    Bootstrap created monitors 115/116/117, attached channels [1, 2] to each
    (its own [notify] lines said so), then reported all three mute and exited 1
    — because the verifier re-read `get_monitors()`, the same client-side cache
    the write had not yet propagated into. kuma.db showed 2 channels on each.
    The retry re-read that identical cache, so it could never clear.

    The cache may accuse; only the server convicts.
    """
    api = FakeApi(
        monitors=[_mon(115, "Canary Prowlarr App Sync", []),
                  _mon(116, "Canary Plex Unmatched", []),
                  _mon(117, "Canary REA Liveness", [])],
        live={115: _mon(115, "Canary Prowlarr App Sync", [1, 2]),
              116: _mon(116, "Canary Plex Unmatched", [1, 2]),
              117: _mon(117, "Canary REA Liveness", [1, 2])},
    )
    assert mod._mute_monitor_names(api) == []


def test_a_monitor_the_server_agrees_is_mute_still_fails(mod):
    """The live confirm must not become a blanket amnesty."""
    api = FakeApi(monitors=[_mon(110, "Canary Dash Asset Integrity", [])])
    assert mod._mute_monitor_names(api) == ["Canary Dash Asset Integrity"]
    assert api.live_reads == [110], "the accusation was never checked live"


def test_a_monitor_the_server_cannot_answer_for_stays_accused(mod):
    """Unverifiable is not innocent. A read failure must not launder a mute
    monitor into a clean run — that is the exact silence this guard exists to
    break."""
    class HalfBroken(FakeApi):
        def get_monitor(self, mid):
            raise RuntimeError("socket closed")

    api = HalfBroken(monitors=[_mon(9, "Canary Ghost", [])])
    assert mod._mute_monitor_names(api) == ["Canary Ghost"]


def test_wired_monitors_cost_no_server_round_trips(mod):
    """Only the accused are re-read. A live read per monitor would be ~70 extra
    socket calls on every install for no added signal."""
    api = FakeApi(monitors=[_mon(1, "Plex", [1, 2]), _mon(2, "Sonarr", [1, 2])])
    assert mod._mute_monitor_names(api) == []
    assert api.live_reads == []


def test_main_asserts_before_returning_success(mod):
    """The verification must be WIRED, not merely defined.

    Pins that main() calls the verifier and can return non-zero on mute
    monitors. Deleting the call from main() leaves every unit test above
    passing, which is precisely how the original guard shipped ineffective.
    """
    src = BOOTSTRAP.read_text(encoding="utf-8")
    main_src = src[src.index("def main("):]

    # Pin the FIRST assignment to `mute`, not merely that the verifier is
    # mentioned somewhere. The retry block calls it a second time, so a
    # substring check passes even when the gating call is stubbed out --
    # verified by mutation: replacing only the first assignment with `mute = []`
    # left a substring-based version of this test green.
    first = None
    for line in main_src.splitlines():
        stripped = line.strip()
        if stripped.startswith("mute ") and "=" in stripped:
            first = stripped
            break
    assert first == "mute = _mute_monitor_names(api, created_names)", (
        "main()'s first `mute` assignment must come from the verifier, AND must "
        "pass created_names — without it a monitor created this run but missing "
        "from the socket-refreshed cache is never enumerated, which is how the "
        "guard reported success over a mute monitor three times "
        "(2026-07-30 Dash Asset Integrity, 2026-08-07 Tdarr Pause Integrity); "
        f"found: {first!r}"
    )

    # created_names must be populated at EVERY create site, not just the ones
    # whose token came back — a tokenless create is exactly the case that ships
    # mute, so keying off created_tokens would miss it. Three create sites.
    assert main_src.count("created_names.add(") == 3, (
        "expected 3 created_names.add() calls (apps, canaries, standalone "
        f"self-pushers); found {main_src.count('created_names.add(')}"
    )

    tail = main_src[main_src.index("_mute_monitor_names(api, created_names)"):]
    assert "return 1" in tail, \
        "main() no longer fails the run when monitors are left mute"


# --- created-this-run monitors are enumerated even when the cache cannot see
# --- them (2026-08-07). Third occurrence of one class; see _mute_monitor_names.


def test_a_created_monitor_absent_from_the_cache_is_accused_not_skipped(mod):
    """THE REGRESSION. get_monitors() is a socket-refreshed client cache, so a
    monitor created seconds earlier can be missing from it entirely — not
    checked, not accused, not printed. The verifier then returns [] and the
    caller prints "verified: every active monitor has notification channels"
    over a monitor with none.

    Live proof this is not theoretical: run 1 of the 2026-08-07 bootstrap created
    "Canary Tdarr Pause Integrity", attached nothing, and printed the verified
    line; run 2 reported `+ channels [1, 2] (was NONE)`. "Canary Dash Asset
    Integrity" did the same on 2026-07-30 with this guard already in place."""
    api = FakeApi([_mon(1, "Plex", [1, 2])])          # cache has NOT caught up
    assert mod._mute_monitor_names(api, {"Canary Tdarr Pause Integrity"}) == [
        "Canary Tdarr Pause Integrity"
    ]


def test_created_monitor_present_and_wired_is_not_accused(mod):
    """The union must not manufacture a false FATAL once the cache does catch
    up — otherwise every fresh-monitor deploy fails forever."""
    api = FakeApi([_mon(1, "Plex", [1, 2]), _mon(119, "Canary Tdarr Pause Integrity", [1, 2])])
    assert mod._mute_monitor_names(api, {"Canary Tdarr Pause Integrity"}) == []


def test_created_monitor_present_but_mute_is_accused_once_not_twice(mod):
    """It is in BOTH the cache and created_names; it must appear exactly once."""
    api = FakeApi([_mon(119, "Canary Tdarr Pause Integrity", [])])
    assert mod._mute_monitor_names(api, {"Canary Tdarr Pause Integrity"}) == [
        "Canary Tdarr Pause Integrity"
    ]


def test_unreadable_kuma_still_accuses_what_this_run_created(mod):
    """A failed re-read means we can vouch for nothing — but we still KNOW what
    we created, and waving those through is how a mute monitor ships. Monitors
    we did NOT create stay unaccused: absent evidence is not evidence."""
    class Broken:
        def get_monitors(self):
            raise RuntimeError("socket closed")

    assert mod._mute_monitor_names(Broken(), {"Canary New"}) == ["Canary New"]
    assert mod._mute_monitor_names(Broken()) == []


def test_no_creates_means_no_added_accusations(mod):
    """The ordinary steady-state run: nothing created, so the union adds
    nothing and behaviour is exactly as before this change."""
    api = FakeApi([_mon(1, "Plex", [1, 2])])
    assert mod._mute_monitor_names(api, set()) == []
    assert mod._mute_monitor_names(api, None) == []
