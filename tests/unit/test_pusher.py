"""Unit tests for lib/pusher.py."""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

# Ensure lib/ is importable
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "maint"))

from lib.health import HealthResult
from lib.manifest import App, HealthConfig, Manifest


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_health(kind: str = "http_api") -> HealthConfig:
    return HealthConfig(kind=kind, raw={})


def _make_app(name: str, kuma_monitor: Optional[str]) -> App:
    return App(
        name=name,
        class_="ucc",
        kuma_monitor=kuma_monitor,
        health=_make_health(),
        defaults={},
    )


def _make_manifest(*apps: App) -> Manifest:
    return Manifest({a.name: a for a in apps})


# ---------------------------------------------------------------------------
# push_once tests
# ---------------------------------------------------------------------------

class TestPushOnce:
    def _run(self, manifest, tokens, probe_results, get_side_effect=None):
        """Helper: patch health.probe and requests.get, call push_once."""
        from lib import pusher

        probe_map = {app.name: result for app, result in zip(manifest.apps(), probe_results)}

        def fake_probe(app, **kwargs):
            return probe_map.get(app.name, HealthResult(ok=False, latency_ms=None, reason="no mock"))

        mock_get = MagicMock()
        if get_side_effect:
            mock_get.side_effect = get_side_effect
        else:
            resp = MagicMock()
            resp.status_code = 200
            mock_get.return_value = resp

        with patch("lib.pusher.health_mod.probe", side_effect=fake_probe), \
             patch("lib.pusher.requests.get", mock_get):
            result = pusher.push_once(
                manifest=manifest,
                kuma_url="http://127.0.0.1:42005",
                tokens=tokens,
            )

        return result, mock_get

    def test_push_once_calls_kuma_for_each_token(self):
        """Exactly 1 GET to Kuma for the 1 app that has a token."""
        apps = [
            _make_app("sonarr", "Sonarr"),        # has token
            _make_app("radarr", "Radarr"),         # no token in dict
            _make_app("recyclarr", None),           # no kuma_monitor
        ]
        manifest = _make_manifest(*apps)
        tokens = {"sonarr": "tok-abc"}
        probe_results = [
            HealthResult(ok=True, latency_ms=42, reason="ok"),
            HealthResult(ok=True, latency_ms=10, reason="ok"),
            HealthResult(ok=True, latency_ms=None, reason="active"),
        ]

        result, mock_get = self._run(manifest, tokens, probe_results)

        assert mock_get.call_count == 1
        call_url = mock_get.call_args[0][0]
        assert call_url == "http://127.0.0.1:42005/api/push/tok-abc"
        assert "sonarr" in result
        assert "radarr" not in result
        assert "recyclarr" not in result

    def test_push_once_status_up_when_health_ok(self):
        manifest = _make_manifest(_make_app("sonarr", "Sonarr"))
        tokens = {"sonarr": "tok-abc"}
        probe_results = [HealthResult(ok=True, latency_ms=10, reason="ok")]

        _, mock_get = self._run(manifest, tokens, probe_results)

        params = mock_get.call_args[1]["params"]
        assert params["status"] == "up"

    def test_push_once_status_down_when_health_fails(self):
        manifest = _make_manifest(_make_app("sonarr", "Sonarr"))
        tokens = {"sonarr": "tok-abc"}
        probe_results = [HealthResult(ok=False, latency_ms=None, reason="connection refused")]

        _, mock_get = self._run(manifest, tokens, probe_results)

        params = mock_get.call_args[1]["params"]
        assert params["status"] == "down"

    def test_push_once_passes_msg_and_ping(self):
        manifest = _make_manifest(_make_app("sonarr", "Sonarr"))
        tokens = {"sonarr": "tok-abc"}
        probe_results = [HealthResult(ok=True, latency_ms=77, reason="ok")]

        _, mock_get = self._run(manifest, tokens, probe_results)

        params = mock_get.call_args[1]["params"]
        assert params["msg"] == "ok"
        assert params["ping"] == 77

    def test_push_once_handles_kuma_500_gracefully(self):
        manifest = _make_manifest(_make_app("sonarr", "Sonarr"))
        tokens = {"sonarr": "tok-abc"}
        probe_results = [HealthResult(ok=True, latency_ms=5, reason="ok")]

        from requests import ConnectionError as ReqConnErr
        result, _ = self._run(
            manifest, tokens, probe_results,
            get_side_effect=ReqConnErr("connection refused"),
        )

        assert "sonarr" in result
        assert result["sonarr"].startswith("error:")

    def test_push_once_handles_kuma_http_500(self):
        from lib import pusher

        def fake_probe(app, **kwargs):
            return HealthResult(ok=True, latency_ms=5, reason="ok")

        resp = MagicMock()
        resp.status_code = 500

        manifest = _make_manifest(_make_app("sonarr", "Sonarr"))
        tokens = {"sonarr": "tok-abc"}

        with patch("lib.pusher.health_mod.probe", side_effect=fake_probe), \
             patch("lib.pusher.requests.get", return_value=resp):
            result = pusher.push_once(
                manifest=manifest,
                kuma_url="http://127.0.0.1:42005",
                tokens=tokens,
            )

        assert result["sonarr"] == "http_500"

    def test_push_once_skips_app_without_token(self):
        """App with kuma_monitor but no token entry: not probed, not in result."""
        from lib import pusher

        probe_mock = MagicMock()
        manifest = _make_manifest(_make_app("radarr", "Radarr"))
        tokens = {}  # no token for radarr

        with patch("lib.pusher.health_mod.probe", probe_mock), \
             patch("lib.pusher.requests.get", MagicMock()):
            result = pusher.push_once(manifest=manifest, kuma_url="http://127.0.0.1:42005", tokens=tokens)

        probe_mock.assert_not_called()
        assert "radarr" not in result

    def test_push_once_skips_app_without_kuma_monitor(self):
        """App with kuma_monitor=None: not probed, not in result."""
        from lib import pusher

        probe_mock = MagicMock()
        manifest = _make_manifest(_make_app("recyclarr", None))
        tokens = {"recyclarr": "tok-xyz"}

        with patch("lib.pusher.health_mod.probe", probe_mock), \
             patch("lib.pusher.requests.get", MagicMock()):
            result = pusher.push_once(manifest=manifest, kuma_url="http://127.0.0.1:42005", tokens=tokens)

        probe_mock.assert_not_called()
        assert "recyclarr" not in result

    # -- Kuma push-endpoint resilience (intermittent read-timeout) -----------

    def test_push_retries_on_timeout_then_succeeds(self):
        """A single read-timeout is retried; the retry's 200 yields 'ok'.
        Closes the sporadic 'Read timed out' heartbeat drops."""
        import requests as _rq
        from lib import pusher
        manifest = _make_manifest(_make_app("sonarr", "Sonarr"))
        tokens = {"sonarr": "tok-abc"}
        probe_results = [HealthResult(ok=True, latency_ms=5, reason="ok")]
        ok_resp = MagicMock(status_code=200)
        result, mock_get = self._run(
            manifest, tokens, probe_results,
            get_side_effect=[_rq.ReadTimeout("slow"), ok_resp],
        )
        assert result["sonarr"] == "ok"
        assert mock_get.call_count == pusher._PUSH_RETRIES + 1

    def test_push_timeout_exhausted_returns_error(self):
        """Timeout on every attempt → 'error:' after retries exhausted."""
        import requests as _rq
        from lib import pusher
        manifest = _make_manifest(_make_app("sonarr", "Sonarr"))
        tokens = {"sonarr": "tok-abc"}
        probe_results = [HealthResult(ok=True, latency_ms=5, reason="ok")]
        result, mock_get = self._run(
            manifest, tokens, probe_results,
            get_side_effect=_rq.ReadTimeout("always slow"),
        )
        assert result["sonarr"].startswith("error:")
        assert mock_get.call_count == pusher._PUSH_RETRIES + 1

    def test_push_connection_error_not_retried(self):
        """ConnectionError (Kuma actually down) fails fast — no retry, unlike
        a transient read-timeout."""
        import requests as _rq
        manifest = _make_manifest(_make_app("sonarr", "Sonarr"))
        tokens = {"sonarr": "tok-abc"}
        probe_results = [HealthResult(ok=True, latency_ms=5, reason="ok")]
        result, mock_get = self._run(
            manifest, tokens, probe_results,
            get_side_effect=_rq.ConnectionError("refused"),
        )
        assert result["sonarr"].startswith("error:")
        assert mock_get.call_count == 1

    def test_push_uses_configurable_timeout_not_aggressive_5s(self):
        """Pusher passes the longer configurable timeout (>=15s), not the old
        5s that tripped on Kuma's transient stalls."""
        from lib import pusher
        manifest = _make_manifest(_make_app("sonarr", "Sonarr"))
        tokens = {"sonarr": "tok-abc"}
        probe_results = [HealthResult(ok=True, latency_ms=5, reason="ok")]
        _, mock_get = self._run(manifest, tokens, probe_results)
        assert mock_get.call_args.kwargs["timeout"] == pusher._PUSH_TIMEOUT_S
        assert pusher._PUSH_TIMEOUT_S >= 15


# ---------------------------------------------------------------------------
# serve() tests
# ---------------------------------------------------------------------------

class TestServe:
    def _make_tokens_file(self, tmp_path: Path, data: dict) -> Path:
        p = tmp_path / "tokens.json"
        p.write_text(json.dumps(data))
        return p

    def _make_manifest_file(self, tmp_path: Path) -> Path:
        content = """
defaults:
  health_timeout_s: 5
  recovery_attempts: 3
  recovery_backoff_s: [10, 30, 60]
  lifecycle_timeout_s: 60
  kuma_recheck_delay_s: 90

apps:
  sonarr:
    class: ucc
    kuma_monitor: "Sonarr"
    health:
      kind: http_api
      port_secret: sonarr.port
"""
        p = tmp_path / "apps.yaml"
        p.write_text(content)
        return p

    def test_serve_run_once_is_one_pass(self, tmp_path):
        from lib import pusher

        manifest_file = self._make_manifest_file(tmp_path)
        tokens_file = self._make_tokens_file(tmp_path, {"sonarr": "tok-abc"})

        push_once_mock = MagicMock(return_value={"sonarr": "ok"})

        with patch("lib.pusher.push_once", push_once_mock), \
             patch("lib.pusher.health_mod.probe"):
            pusher.serve(
                manifest_path=manifest_file,
                tokens_path=tokens_file,
                kuma_url="http://127.0.0.1:42005",
                interval_s=60,
                run_once=True,
            )

        assert push_once_mock.call_count == 1

    def test_serve_loops_until_signal(self, tmp_path):
        """serve() exits cleanly when KeyboardInterrupt is raised inside the loop.

        We patch push_once to return normally on the first call and raise
        KeyboardInterrupt on the second, confirming serve() runs >1 cycle
        and then exits without leaking the exception.
        """
        from lib import pusher

        manifest_file = self._make_manifest_file(tmp_path)
        tokens_file = self._make_tokens_file(tmp_path, {"sonarr": "tok-abc"})

        call_count = [0]

        def fake_push_once(**kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise KeyboardInterrupt("stop")
            return {"sonarr": "ok"}

        errors = []

        def _run():
            try:
                with patch("lib.pusher.push_once", side_effect=fake_push_once):
                    pusher.serve(
                        manifest_path=manifest_file,
                        tokens_path=tokens_file,
                        kuma_url="http://127.0.0.1:42005",
                        interval_s=0,
                        run_once=False,
                    )
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=5)

        assert not t.is_alive(), "serve() did not exit within 5s"
        assert not errors, f"serve() raised an unexpected exception: {errors}"
        assert call_count[0] >= 2, "expected at least 2 push cycles"


# ---------------------------------------------------------------------------
# Auto-heal strike threshold (3 consecutive failures before recovery fires)
# ---------------------------------------------------------------------------

class TestAutoHealStrikeThreshold:
    """The pusher must NOT trigger recovery on the first failed probe — it
    must accumulate 3 consecutive failures, then fire. Resets on success."""

    def _setup(self, monkeypatch):
        # Reset module-level counter between tests
        from lib import pusher
        pusher.reset_strike_counter()
        return pusher

    def test_first_failure_does_not_trigger_recovery(self, monkeypatch):
        from lib import pusher, health, recovery
        pusher.reset_strike_counter()

        app = _make_app("sonarr", "Sonarr")
        manifest = Manifest({"sonarr": app})
        tokens = {"sonarr": "tok"}

        monkeypatch.setattr(health, "probe", lambda a, **kw: HealthResult(
            ok=False, latency_ms=42, reason="connection refused"))

        triggered = []
        monkeypatch.setattr(recovery, "trigger_async",
                             lambda a, **kw: triggered.append(a.name) or "started")

        # Stub the kuma push HTTP call so push_once doesn't actually network
        with patch("lib.pusher.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            pusher.push_once(manifest=manifest, tokens=tokens)

        assert triggered == [], "recovery fired on FIRST failure (expected silent)"

    def test_third_failure_triggers_recovery(self, monkeypatch):
        from lib import pusher, health, recovery
        pusher.reset_strike_counter()

        app = _make_app("sonarr", "Sonarr")
        manifest = Manifest({"sonarr": app})
        tokens = {"sonarr": "tok"}

        monkeypatch.setattr(health, "probe", lambda a, **kw: HealthResult(
            ok=False, latency_ms=42, reason="connection refused"))

        triggered = []
        monkeypatch.setattr(recovery, "trigger_async",
                             lambda a, **kw: triggered.append(a.name) or "started")

        with patch("lib.pusher.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            pusher.push_once(manifest=manifest, tokens=tokens)  # strike 1
            pusher.push_once(manifest=manifest, tokens=tokens)  # strike 2
            pusher.push_once(manifest=manifest, tokens=tokens)  # strike 3 -> trigger

        assert triggered == ["sonarr"], (
            f"expected single trigger on 3rd failure; got {triggered!r}"
        )

    def test_success_resets_strike_counter(self, monkeypatch):
        from lib import pusher, health, recovery
        pusher.reset_strike_counter()

        app = _make_app("sonarr", "Sonarr")
        manifest = Manifest({"sonarr": app})
        tokens = {"sonarr": "tok"}

        results_iter = iter([
            HealthResult(ok=False, latency_ms=42, reason="connection refused"),
            HealthResult(ok=False, latency_ms=42, reason="connection refused"),
            HealthResult(ok=True,  latency_ms=50, reason="ok"),  # resets
            HealthResult(ok=False, latency_ms=42, reason="connection refused"),
            HealthResult(ok=False, latency_ms=42, reason="connection refused"),
        ])
        monkeypatch.setattr(health, "probe", lambda a, **kw: next(results_iter))

        triggered = []
        monkeypatch.setattr(recovery, "trigger_async",
                             lambda a, **kw: triggered.append(a.name) or "started")

        with patch("lib.pusher.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            for _ in range(5):
                pusher.push_once(manifest=manifest, tokens=tokens)

        # Two strikes, success (reset), two strikes — never reached 3 consecutive
        assert triggered == [], (
            f"expected no trigger; success should have reset strikes. got {triggered!r}"
        )


# ---------------------------------------------------------------------------
# B1 suppression: ucc-maintenance recovery suppression in pusher
# ---------------------------------------------------------------------------

class TestPusherSuppressionDuringUccMaint:
    """When recovery_suppressed returns True, push_once must NOT call
    trigger_async but MUST still push the health status to Kuma and MUST
    annotate the Kuma msg with '[ucc-maint: recovery suppressed]'."""

    def _run_suppressed(self, monkeypatch, *, strikes: int = 3):
        """Run push_once `strikes` times with a failing health probe and
        suppression=True. Returns (results, mock_get, triggered)."""
        from lib import pusher, health, recovery
        pusher.reset_strike_counter()

        app = _make_app("sonarr", "Sonarr")
        manifest = Manifest({"sonarr": app})
        tokens = {"sonarr": "tok"}

        monkeypatch.setattr(health, "probe", lambda a, **kw: HealthResult(
            ok=False, latency_ms=None, reason="connection refused"))

        triggered = []
        monkeypatch.setattr(recovery, "trigger_async",
                             lambda a, **kw: triggered.append(a.name) or "started")

        # Patch suppression at the pusher's import site (the aliased name).
        with patch("lib.pusher.suppression_mod.recovery_suppressed", return_value=True), \
             patch("lib.pusher.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            for _ in range(strikes):
                result = pusher.push_once(manifest=manifest, tokens=tokens)

        return result, mock_get, triggered

    def test_suppressed_does_not_trigger_recovery(self, monkeypatch):
        """When suppressed, trigger_async is NOT called on the threshold strike."""
        _, _, triggered = self._run_suppressed(monkeypatch)
        assert triggered == [], (
            f"recovery fired while suppressed; got {triggered!r}"
        )

    def test_suppressed_still_pushes_status_to_kuma(self, monkeypatch):
        """Even when suppressed, the health status POST to Kuma must happen."""
        _, mock_get, _ = self._run_suppressed(monkeypatch)
        # At least one GET call to Kuma (the health push)
        kuma_calls = [
            c for c in mock_get.call_args_list
            if "api/push/" in str(c)
        ]
        assert len(kuma_calls) >= 1, "expected at least one Kuma push call while suppressed"

    def test_suppressed_kuma_msg_annotated(self, monkeypatch):
        """The Kuma msg param must include '[ucc-maint: recovery suppressed]'."""
        from lib import pusher, health
        pusher.reset_strike_counter()

        app = _make_app("sonarr", "Sonarr")
        manifest = Manifest({"sonarr": app})
        tokens = {"sonarr": "tok"}

        monkeypatch.setattr(health, "probe", lambda a, **kw: HealthResult(
            ok=False, latency_ms=None, reason="connection refused"))

        with patch("lib.pusher.suppression_mod.recovery_suppressed", return_value=True), \
             patch("lib.pusher.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            # Run 3 times to reach threshold
            for _ in range(3):
                pusher.push_once(manifest=manifest, tokens=tokens)

        # Find the push call at or after threshold (strike 3)
        all_params = [c.kwargs.get("params", {}) or c.args[1] if len(c.args) > 1 else c.kwargs.get("params", {})
                      for c in mock_get.call_args_list]
        msgs = [p.get("msg", "") for p in all_params if isinstance(p, dict)]
        suppressed_msgs = [m for m in msgs if "ucc-maint: recovery suppressed" in str(m)]
        assert len(suppressed_msgs) >= 1, (
            f"expected '[ucc-maint: recovery suppressed]' annotation in at least one Kuma push; "
            f"got msgs: {msgs!r}"
        )
# ---------------------------------------------------------------------------
# Fleet aggregate monitor tests (sub-project C)
# ---------------------------------------------------------------------------

class TestFleetAggregate:
    """Tests for the 'QFlix Fleet' aggregate push + storm notify logic."""

    _FLEET_THRESHOLD = 8

    def _run_push_once(self, manifest, tokens, probe_results, state_path,
                       get_side_effect=None, notify_calls=None):
        """Helper: patch health.probe, requests.get, fleet.evaluate, notify."""
        from lib import pusher

        probe_map = {app.name: result for app, result in zip(manifest.apps(), probe_results)}

        def fake_probe(app, **kwargs):
            return probe_map.get(app.name, HealthResult(ok=True, latency_ms=None, reason="ok"))

        mock_get = MagicMock()
        if get_side_effect:
            mock_get.side_effect = get_side_effect
        else:
            resp = MagicMock()
            resp.status_code = 200
            mock_get.return_value = resp

        captured_notify = notify_calls if notify_calls is not None else []

        def fake_notify(message, level="info"):
            captured_notify.append({"message": message, "level": level})
            return True

        # Pass an explicit state_path to push_once via fleet_state_path kwarg
        with patch("lib.pusher.health_mod.probe", side_effect=fake_probe), \
             patch("lib.pusher.requests.get", mock_get), \
             patch("lib.pusher.notify_mod.notify", side_effect=fake_notify):
            result = pusher.push_once(
                manifest=manifest,
                kuma_url="http://127.0.0.1:42005",
                tokens=tokens,
                fleet_state_path=state_path,
            )

        return result, mock_get, captured_notify

    def _make_apps_and_probes(self, n_apps: int, n_failing: int):
        """Create n_apps apps with tokens; first n_failing are down."""
        apps = [_make_app(f"app{i}", f"Monitor{i}") for i in range(n_apps)]
        probe_results = [
            HealthResult(ok=False, latency_ms=None, reason="conn refused") if i < n_failing
            else HealthResult(ok=True, latency_ms=10, reason="ok")
            for i in range(n_apps)
        ]
        tokens = {f"app{i}": f"tok{i}" for i in range(n_apps)}
        return apps, probe_results, tokens

    def test_storm_cycle_pushes_aggregate_down_and_emits_notify(self, tmp_path):
        """≥threshold failing probes → aggregate DOWN push + exactly 1 storm notify."""
        from lib import pusher
        pusher.reset_strike_counter()

        state_path = tmp_path / "fleet-window.json"
        n_apps = 15
        n_failing = self._FLEET_THRESHOLD  # exactly threshold

        apps, probe_results, tokens = self._make_apps_and_probes(n_apps, n_failing)
        tokens["qflix-fleet"] = "tok-fleet"
        manifest = _make_manifest(*apps)

        notify_calls = []
        _, mock_get, notify_calls = self._run_push_once(
            manifest, tokens, probe_results, state_path, notify_calls=notify_calls
        )

        # Aggregate push must have been called with status=down
        fleet_calls = [
            c for c in mock_get.call_args_list
            if "tok-fleet" in str(c)
        ]
        assert len(fleet_calls) == 1, "Expected exactly 1 fleet aggregate push"
        fleet_params = fleet_calls[0][1]["params"]
        assert fleet_params["status"] == "down"
        assert "storm" in fleet_params["msg"].lower()

        # Exactly 1 warning notify
        warn_notifies = [n for n in notify_calls if n["level"] == "warning"]
        assert len(warn_notifies) == 1, f"Expected 1 storm notify, got {warn_notifies}"
        assert "storm" in warn_notifies[0]["message"].lower()

    def test_healthy_cycle_pushes_aggregate_up_no_notify(self, tmp_path):
        """All probes healthy → aggregate UP push, no notify."""
        from lib import pusher
        pusher.reset_strike_counter()

        state_path = tmp_path / "fleet-window-healthy.json"
        apps, probe_results, tokens = self._make_apps_and_probes(10, 0)
        tokens["qflix-fleet"] = "tok-fleet"
        manifest = _make_manifest(*apps)

        notify_calls = []
        _, mock_get, notify_calls = self._run_push_once(
            manifest, tokens, probe_results, state_path, notify_calls=notify_calls
        )

        fleet_calls = [
            c for c in mock_get.call_args_list
            if "tok-fleet" in str(c)
        ]
        assert len(fleet_calls) == 1
        fleet_params = fleet_calls[0][1]["params"]
        assert fleet_params["status"] == "up"
        assert len(notify_calls) == 0, "No notify expected on healthy cycle"

    def test_absent_fleet_token_no_aggregate_push_no_crash(self, tmp_path):
        """No 'qflix-fleet' token → no aggregate push, no crash."""
        from lib import pusher
        pusher.reset_strike_counter()

        state_path = tmp_path / "fleet-window.json"
        apps, probe_results, tokens = self._make_apps_and_probes(10, self._FLEET_THRESHOLD)
        # Intentionally omit "qflix-fleet" from tokens
        assert "qflix-fleet" not in tokens
        manifest = _make_manifest(*apps)

        # Must not raise
        try:
            _, mock_get, _ = self._run_push_once(
                manifest, tokens, probe_results, state_path
            )
        except Exception as exc:
            pytest.fail(f"push_once raised with absent fleet token: {exc}")

        fleet_calls = [
            c for c in mock_get.call_args_list
            if "tok-fleet" in str(c)
        ]
        assert len(fleet_calls) == 0, "No fleet push expected when token absent"

    def test_storm_notify_fires_only_once_across_cycles(self, tmp_path):
        """Second storm cycle must not re-fire the notify."""
        from lib import pusher
        pusher.reset_strike_counter()

        state_path = tmp_path / "fleet-window.json"
        apps, probe_results, tokens = self._make_apps_and_probes(15, self._FLEET_THRESHOLD)
        tokens["qflix-fleet"] = "tok-fleet"
        manifest = _make_manifest(*apps)

        # First cycle → onset + notify
        n1 = []
        self._run_push_once(manifest, tokens, probe_results, state_path, notify_calls=n1)
        assert len([x for x in n1 if x["level"] == "warning"]) == 1

        # Reset strike counter to avoid recovery side-effects
        pusher.reset_strike_counter()

        # Second cycle (still storm) → no new notify
        n2 = []
        self._run_push_once(manifest, tokens, probe_results, state_path, notify_calls=n2)
        warn2 = [x for x in n2 if x["level"] == "warning"]
        assert len(warn2) == 0, f"Storm notify must not repeat; got {warn2}"

    def test_clear_notify_fires_when_storm_ends(self, tmp_path):
        """After a storm, recovery emits one 'info' notify."""
        from lib import pusher
        pusher.reset_strike_counter()

        state_path = tmp_path / "fleet-window.json"
        apps, _, tokens_storm = self._make_apps_and_probes(15, self._FLEET_THRESHOLD)
        tokens_storm["qflix-fleet"] = "tok-fleet"
        manifest = _make_manifest(*apps)

        # Establish storm
        n1 = []
        self._run_push_once(manifest, tokens_storm, _, state_path, notify_calls=n1)
        pusher.reset_strike_counter()

        # Recovery: all healthy
        apps2, probe_ok_results, tokens_ok = self._make_apps_and_probes(15, 0)
        tokens_ok["qflix-fleet"] = "tok-fleet"
        n2 = []
        self._run_push_once(manifest, tokens_ok, probe_ok_results, state_path, notify_calls=n2)

        info_notifies = [x for x in n2 if x["level"] == "info"]
        assert len(info_notifies) == 1, f"Expected 1 clear notify; got {info_notifies}"
        assert "clear" in info_notifies[0]["message"].lower()
