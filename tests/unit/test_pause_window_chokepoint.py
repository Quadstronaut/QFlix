"""Arbitration hardening tests (Council Stage 3).

Two convergent findings from verification:
  - Gemini cross-vendor + boundaries lens: `_in_pause_window` with a *naive*
    datetime would shift by the box's local offset. Harden a single canonical
    predicate to treat naive as UTC.
  - Arbiter diligence: `deep_check.run_deep_check` and the Kuma webhook are a
    SECOND recovery entry point that bypassed the pusher-only guard. Guard the
    chokepoint `recovery.trigger_async` so no caller can revive a paused app.

Plus a config-drift lock: the manifest window must stay in sync with the
systemd pause timers (50c) and the heartbeat guard.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "maint"))

from lib.manifest import App, HealthConfig, PauseWindow
from lib.manifest import load as manifest_load

_REPO = os.path.join(os.path.dirname(__file__), "..", "..")


def _app(name="x", *, pause_window=None, class_="systemd"):
    return App(
        name=name,
        class_=class_,
        kuma_monitor="X",
        health=HealthConfig(kind="systemd_only", raw={}),
        defaults={},
        pause_window=pause_window,
    )


# --- canonical predicate: suppression.in_pause_window -----------------------

class TestSuppressionInPauseWindow:
    def test_aware_utc_inside(self):
        from lib import suppression
        app = _app(pause_window=PauseWindow(18, 23))
        now = datetime(2026, 6, 12, 19, 0, tzinfo=timezone.utc)
        assert suppression.in_pause_window(app, now=now) is True

    def test_aware_nonutc_converted(self):
        from lib import suppression
        app = _app(pause_window=PauseWindow(18, 23))
        # 20:30 at +02:00 (CEST) == 18:30 UTC -> inside [18,23)
        cest = timezone(timedelta(hours=2))
        now = datetime(2026, 6, 12, 20, 30, tzinfo=cest)
        assert suppression.in_pause_window(app, now=now) is True

    def test_naive_treated_as_utc_not_local(self):
        from lib import suppression
        app = _app(pause_window=PauseWindow(18, 23))
        naive = datetime(2026, 6, 12, 19, 0)  # no tzinfo
        # Must be read as 19:00 UTC (inside), NOT shifted by the host's offset.
        assert suppression.in_pause_window(app, now=naive) is True

    def test_no_pause_window_is_false(self):
        from lib import suppression
        assert suppression.in_pause_window(_app(pause_window=None)) is False

    def test_fail_open_on_bad_object(self):
        from lib import suppression
        # An object with a pause_window that explodes on use -> False, never raises.
        class Boom:
            pause_window = "not-a-window"
            name = "boom"
        assert suppression.in_pause_window(Boom()) is False


# --- chokepoint guard: recovery.trigger_async -------------------------------

class TestRecoveryChokepointHonorsPause:
    def test_in_window_returns_paused_and_does_not_recover(self):
        from lib import recovery
        app = _app("tdarr-node", pause_window=PauseWindow(18, 23))
        with patch("lib.recovery.suppression.in_pause_window", return_value=True), \
             patch("lib.recovery.run") as mock_run:
            decision = recovery.trigger_async(app)
        assert decision == "paused"
        mock_run.assert_not_called()  # no restart of an intentionally-paused unit

    def test_outside_window_proceeds_normally(self):
        from lib import recovery
        app = _app("tdarr-node-x", pause_window=PauseWindow(18, 23))
        with patch("lib.recovery.suppression.in_pause_window", return_value=False), \
             patch("lib.recovery.run") as mock_run:
            decision = recovery.trigger_async(app)
        assert decision == "started"


# --- config-drift lock ------------------------------------------------------

class TestTdarrRuns247:
    """The 2026-08-20 inversion.

    This class used to be TestQuietHoursSourcesInSync and pinned the SAME hours
    (18,23) across the manifest, 50c's OnCalendar and the heartbeat guard —
    three surfaces for one number. tdarr-node now runs 24/7 and fair-use is the
    worker cap, so the lock inverts: assert the window is GONE everywhere,
    rather than that three copies of it agree.

    Worth keeping as a test rather than deleting. The pause is exactly the kind
    of thing that comes back via a slot rebuild, an old runbook, or a
    well-meaning "Tdarr is eating CPU" reflex. Reintroducing it silently would
    stop the node five hours a night AND re-blind the surfaces the retirement
    un-blinded, because the canary that used to watch the pause now watches the
    throttle instead.
    """

    def test_tdarr_node_declares_no_pause_window(self):
        m = manifest_load(os.path.join(_REPO, "manifest", "apps.yaml"))
        assert m.app("tdarr-node").pause_window is None

    def test_no_app_declares_a_pause_window(self):
        # The mechanism stays (see PauseWindow's docstring) but nothing uses it.
        # If this ever fails, the new holder is fine — just make sure its window
        # is honoured by every surface, the way tdarr-node's had to be.
        m = manifest_load(os.path.join(_REPO, "manifest", "apps.yaml"))
        holders = [a.name for a in m.apps() if a.pause_window is not None]
        assert holders == [], f"unexpected pause_window holders: {holders}"

    def test_heartbeat_has_no_hardcoded_quiet_hours_guard(self):
        with open(os.path.join(_REPO, "scripts", "ops",
                               "heartbeat-tdarr-node.sh"), encoding="utf-8") as fh:
            hb = fh.read()
        # The literal guard that used to skip every restart path 18:00-23:00.
        assert "-ge 18" not in hb and "-lt 23" not in hb

    def test_50c_removes_the_timers_instead_of_installing_them(self):
        with open(os.path.join(_REPO, "scripts", "configure",
                               "50c-tdarr-247.sh"), encoding="utf-8") as fh:
            sh = fh.read()
        # No OnCalendar at all — the file no longer writes timer units.
        assert "OnCalendar" not in sh
        # And it actively removes both rather than leaving them disabled: a unit
        # on the box with no manifest/jobs.yaml entry is its own finding.
        assert "tdarr-node-pause" in sh and "tdarr-node-resume" in sh
        assert "rm -f" in sh

    def test_old_quiet_hours_installer_is_gone(self):
        assert not os.path.exists(os.path.join(
            _REPO, "scripts", "configure", "50c-tdarr-quiet-hours.sh"))


class TestTdarrThrottleIsTheFairUseLever:
    """The cap that replaced the clock. One number, three consumers."""

    def test_manifest_declares_the_throttle(self):
        m = manifest_load(os.path.join(_REPO, "manifest", "apps.yaml"))
        t = m.app("tdarr-node").throttle
        assert t is not None
        # STAGED AT 1. Target is 2, blocked on capping ffmpeg's thread count.
        # Raising this without that cap took the slot to its `ulimit -u 2000`
        # task ceiling on 2026-08-20 — bash could not fork, so cron and every
        # canary went down with it, not just Tdarr. Measured that night: node
        # off 962 tasks, one worker 1411 (70.5%), two workers 2000 and wedged.
        #
        # Tdarr runs the two pipelines concurrently, so the load the box feels
        # is the sum, not the max. Pinned so a "bump transcode to 2" edit has
        # to read this line and confirm the cap landed first.
        assert (t.transcode, t.health_check) == (1, 1)
        assert t.total == 2

    def test_50b_writes_the_same_numbers_to_both_tdarr_layers(self):
        with open(os.path.join(_REPO, "scripts", "configure",
                               "50b-tdarr-config.py"), encoding="utf-8") as fh:
            cfg = fh.read()
        ns: dict = {}
        # Executing the whole module would need SSH; lift just the two dicts.
        for name in ("WORKER_LIMITS", "NODE_WORKER_LIMITS"):
            start = cfg.index(name + " = {")
            end = cfg.index("}", start) + 1
            exec(cfg[start:end], ns)  # noqa: S102 - literal dict from our own repo
        m = manifest_load(os.path.join(_REPO, "manifest", "apps.yaml"))
        t = m.app("tdarr-node").throttle
        # Layer 1 (global seed) and layer 2 (per-node override, the one that
        # actually gates work) must BOTH match the manifest. Editing only one
        # looks like it worked — that is the whole 2026-08-07 lesson.
        assert ns["WORKER_LIMITS"]["transcodeWorkerLimit"] == t.transcode
        assert ns["WORKER_LIMITS"]["healthcheckWorkerLimit"] == t.health_check
        assert ns["NODE_WORKER_LIMITS"]["transcodecpu"] == t.transcode
        assert ns["NODE_WORKER_LIMITS"]["healthcheckcpu"] == t.health_check
        # No GPU on this slot, either layer.
        assert ns["WORKER_LIMITS"]["transcodeWorkerLimitGpu"] == 0
        assert ns["NODE_WORKER_LIMITS"]["transcodegpu"] == 0

    def test_canary_reads_the_cap_from_the_manifest(self):
        with open(os.path.join(_REPO, "scripts", "canaries",
                               "tdarr-throttle-integrity.sh"), encoding="utf-8") as fh:
            sh = fh.read()
        assert "transcode_workers" in sh and "health_check_workers" in sh
        # Never a restated literal — that is how the pause hours drifted across
        # four files before they were centralised.
        assert "WANT_T=" in sh and "WANT_H=" in sh
        # A node that is down must be "cannot assert" (2), never a clean pass.
        assert "tdarr-throttle-no-nodes" in sh
        assert "tdarr-throttle-server-unreachable" in sh


class TestThrottleParsing:
    def test_rejects_non_mapping(self):
        from lib.manifest import _parse_throttle, ManifestError
        with pytest.raises(ManifestError):
            _parse_throttle("2/1", app_name="x")

    def test_rejects_missing_key(self):
        from lib.manifest import _parse_throttle, ManifestError
        with pytest.raises(ManifestError):
            _parse_throttle({"transcode_workers": 2}, app_name="x")

    def test_rejects_out_of_range(self):
        from lib.manifest import _parse_throttle, ManifestError
        # An unbounded cap is the failure this field exists to prevent, so it
        # is rejected at parse time rather than politely honoured.
        with pytest.raises(ManifestError):
            _parse_throttle({"transcode_workers": 99,
                             "health_check_workers": 1}, app_name="x")
        with pytest.raises(ManifestError):
            _parse_throttle({"transcode_workers": -1,
                             "health_check_workers": 1}, app_name="x")

    def test_zero_is_legal(self):
        from lib.manifest import _parse_throttle
        t = _parse_throttle({"transcode_workers": 0,
                             "health_check_workers": 0}, app_name="x")
        assert t.total == 0
