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
