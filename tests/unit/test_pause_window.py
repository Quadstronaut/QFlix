"""Council-fix tests: the pusher must honor an app's fair-use pause window.

Root cause (2026-06-12): the pusher's auto-heal revived tdarr-node every day
during its 18:00-23:00 UTC quiet-hours pause, emitting a false "recovered"
alert and defeating the pause. These tests pin the fix — a declared
`pause_window` makes the pusher push UP (Kuma stays green) and skip probe +
recovery for the duration of the window, with zero change outside it.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "maint"))

from lib.health import HealthResult
from lib.manifest import App, HealthConfig, Manifest, ManifestError, PauseWindow
from lib.manifest import load as manifest_load


def _app(name, *, pause_window=None, kuma_monitor="Mon"):
    return App(
        name=name,
        class_="systemd",
        kuma_monitor=kuma_monitor,
        health=HealthConfig(kind="systemd_only", raw={}),
        defaults={},
        pause_window=pause_window,
    )


# --- PauseWindow.contains ---------------------------------------------------

class TestPauseWindowContains:
    def test_simple_window_boundaries(self):
        w = PauseWindow(start_hour_utc=18, end_hour_utc=23)
        assert w.contains(18) is True
        assert w.contains(22) is True
        assert w.contains(17) is False
        assert w.contains(23) is False  # end exclusive — resume fires at 23:00

    def test_wraparound_window_spans_midnight(self):
        w = PauseWindow(start_hour_utc=22, end_hour_utc=6)
        assert w.contains(22) is True
        assert w.contains(23) is True
        assert w.contains(5) is True
        assert w.contains(6) is False
        assert w.contains(12) is False
        assert w.contains(21) is False

    def test_zero_width_never_pauses(self):
        w = PauseWindow(start_hour_utc=5, end_hour_utc=5)
        for h in range(24):
            assert w.contains(h) is False


# --- manifest parsing -------------------------------------------------------

class TestManifestPauseWindow:
    def _write(self, tmp_path, body):
        p = tmp_path / "apps.yaml"
        p.write_text(body, encoding="utf-8")
        return p

    def test_absent_pause_window_is_none(self, tmp_path):
        m = manifest_load(self._write(tmp_path, """
apps:
  tdarr-node:
    class: systemd
    kuma_monitor: "Tdarr Node"
    health:
      kind: systemd_only
"""))
        assert m.app("tdarr-node").pause_window is None

    def test_pause_window_parsed(self, tmp_path):
        m = manifest_load(self._write(tmp_path, """
apps:
  tdarr-node:
    class: systemd
    kuma_monitor: "Tdarr Node"
    pause_window:
      start_hour_utc: 18
      end_hour_utc: 23
    health:
      kind: systemd_only
"""))
        pw = m.app("tdarr-node").pause_window
        assert pw is not None
        assert pw.start_hour_utc == 18
        assert pw.end_hour_utc == 23

    def test_pause_window_missing_key_raises(self, tmp_path):
        with pytest.raises(ManifestError):
            manifest_load(self._write(tmp_path, """
apps:
  x:
    class: systemd
    kuma_monitor: "X"
    pause_window:
      start_hour_utc: 18
    health:
      kind: systemd_only
"""))

    def test_pause_window_out_of_range_raises(self, tmp_path):
        with pytest.raises(ManifestError):
            manifest_load(self._write(tmp_path, """
apps:
  x:
    class: systemd
    kuma_monitor: "X"
    pause_window:
      start_hour_utc: 18
      end_hour_utc: 24
    health:
      kind: systemd_only
"""))


# --- pusher integration -----------------------------------------------------

class TestPusherHonorsPauseWindow:
    def _run(self, app, *, hour_utc, prior_strikes=None):
        from lib import pusher

        if prior_strikes is not None:
            pusher._consecutive_failures[app.name] = prior_strikes
        else:
            pusher._consecutive_failures.pop(app.name, None)

        manifest = Manifest({app.name: app})
        tokens = {app.name: "tok-1"}

        fixed_now = datetime(2026, 6, 12, hour_utc, 30, 0, tzinfo=timezone.utc)
        resp = MagicMock()
        resp.status_code = 200
        mock_get = MagicMock(return_value=resp)
        # probe would report the paused unit as down — the fix must not call it.
        mock_probe = MagicMock(
            return_value=HealthResult(ok=False, latency_ms=None, reason="inactive")
        )
        mock_recover = MagicMock(return_value="started")

        with patch("lib.pusher._utcnow", return_value=fixed_now), \
             patch("lib.pusher.health_mod.probe", mock_probe), \
             patch("lib.pusher.recovery_mod.trigger_async", mock_recover), \
             patch("lib.pusher.requests.get", mock_get):
            results = pusher.push_once(manifest=manifest, tokens=tokens)

        return results, mock_get, mock_probe, mock_recover

    def test_in_window_pushes_up_skips_probe_and_recovery(self):
        app = _app("tdarr-node", pause_window=PauseWindow(18, 23))
        results, mock_get, mock_probe, mock_recover = self._run(
            app, hour_utc=19, prior_strikes=2
        )

        params = mock_get.call_args[1]["params"]
        assert params["status"] == "up"               # Kuma stays green
        assert "paused" in params["msg"].lower()      # but clearly labelled
        mock_probe.assert_not_called()                # probe skipped
        mock_recover.assert_not_called()              # recovery skipped
        from lib import pusher
        assert "tdarr-node" not in pusher._consecutive_failures  # strikes cleared
        assert results["tdarr-node"] == "ok"

    def test_outside_window_probes_normally(self):
        app = _app("tdarr-node", pause_window=PauseWindow(18, 23))
        _, _, mock_probe, _ = self._run(app, hour_utc=12)
        mock_probe.assert_called_once()               # normal path intact

    def test_no_pause_window_probes_normally(self):
        app = _app("plex", pause_window=None)
        _, _, mock_probe, _ = self._run(app, hour_utc=19)
        mock_probe.assert_called_once()               # unaffected app unchanged
