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

class TestQuietHoursSourcesInSync:
    def test_manifest_matches_timer_and_heartbeat_hours(self):
        m = manifest_load(os.path.join(_REPO, "manifest", "apps.yaml"))
        pw = m.app("tdarr-node").pause_window
        assert pw is not None
        assert (pw.start_hour_utc, pw.end_hour_utc) == (18, 23)

        with open(os.path.join(_REPO, "scripts", "configure",
                               "50c-tdarr-quiet-hours.sh"), encoding="utf-8") as fh:
            timer = fh.read()
        assert "18:00:00 UTC" in timer and "23:00:00 UTC" in timer

        with open(os.path.join(_REPO, "scripts", "ops",
                               "heartbeat-tdarr-node.sh"), encoding="utf-8") as fh:
            hb = fh.read()
        assert "-ge 18" in hb and "-lt 23" in hb
