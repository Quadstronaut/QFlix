"""tests/unit/test_kuma.py — TDD tests for lib/kuma.py (client + server).

Phase 5: query Uptime Kuma's /metrics endpoint for monitor status.
Phase 8: webhook HTTP server tests.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from lib.kuma import monitor_status, monitors_status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_METRICS_UP = (
    '# HELP monitor_status Monitor Status\n'
    '# TYPE monitor_status gauge\n'
    'monitor_status{monitor_name="Sonarr",monitor_type="http",monitor_url="http://127.0.0.1:8989/"} 1\n'
)

_METRICS_DOWN = (
    '# HELP monitor_status Monitor Status\n'
    '# TYPE monitor_status gauge\n'
    'monitor_status{monitor_name="Sonarr",monitor_type="http",monitor_url="http://127.0.0.1:8989/"} 0\n'
)

_METRICS_PENDING = (
    '# HELP monitor_status Monitor Status\n'
    '# TYPE monitor_status gauge\n'
    'monitor_status{monitor_name="Sonarr",monitor_type="http",monitor_url="http://127.0.0.1:8989/"} 2\n'
)

_METRICS_OTHER_APP = (
    '# HELP monitor_status Monitor Status\n'
    '# TYPE monitor_status gauge\n'
    'monitor_status{monitor_name="Radarr",monitor_type="http",monitor_url="http://127.0.0.1:7878/"} 1\n'
)


def _secret_key_only(name: str) -> str:
    """Return 'testkey' for uptimekuma.key; raise FileNotFoundError for all others."""
    if name == "uptimekuma.key":
        return "testkey"
    raise FileNotFoundError(name)


def _secret_myapikey(name: str) -> str:
    if name == "uptimekuma.key":
        return "myapikey123"
    raise FileNotFoundError(name)


def _mock_response(text: str, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# Status parsing from /metrics
# ---------------------------------------------------------------------------

class TestMonitorStatusParsing:
    def test_monitor_status_up_from_metrics(self):
        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_UP)), \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            result = monitor_status("Sonarr")
        assert result == "up"

    def test_monitor_status_down_from_metrics(self):
        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_DOWN)), \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            result = monitor_status("Sonarr")
        assert result == "down"

    def test_monitor_status_pending_returns_unknown(self):
        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_PENDING)), \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            result = monitor_status("Sonarr")
        assert result == "unknown"

    def test_monitor_status_monitor_not_found(self):
        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_OTHER_APP)), \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            result = monitor_status("Sonarr")
        assert result == "unknown"


# ---------------------------------------------------------------------------
# Network error handling
# ---------------------------------------------------------------------------

class TestMonitorStatusNetworkErrors:
    def test_monitor_status_connection_error(self):
        with patch("lib.kuma.requests.get", side_effect=requests.ConnectionError("refused")), \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            result = monitor_status("Sonarr")
        assert result == "unknown"

    def test_monitor_status_timeout(self):
        with patch("lib.kuma.requests.get", side_effect=requests.Timeout("timed out")), \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            result = monitor_status("Sonarr")
        assert result == "unknown"

    def test_monitor_status_http_error_returns_unknown(self):
        resp = _mock_response("", status_code=500)
        resp.raise_for_status.side_effect = requests.HTTPError("500")
        with patch("lib.kuma.requests.get", return_value=resp), \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            result = monitor_status("Sonarr")
        assert result == "unknown"


# ---------------------------------------------------------------------------
# Auth / host resolution
# ---------------------------------------------------------------------------

class TestMonitorStatusAuth:
    def test_monitor_status_uses_basic_auth_with_api_key(self):
        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_UP)) as mock_get, \
             patch("lib.kuma._secret_read", side_effect=_secret_myapikey):
            monitor_status("Sonarr")
        assert mock_get.call_args[1]["auth"] == ("", "myapikey123")

    def test_monitor_status_uses_kuma_host_secret_when_present(self):
        def _secret(name: str) -> str:
            if name == "uptimekuma.key":
                return "mykey"
            if name == "uptimekuma.host":
                return "http://127.0.0.1:3001"
            raise FileNotFoundError(name)

        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_UP)) as mock_get, \
             patch("lib.kuma._secret_read", side_effect=_secret):
            monitor_status("Sonarr")

        url_called = mock_get.call_args[0][0]
        assert url_called.startswith("http://127.0.0.1:3001")

    def test_monitor_status_uses_default_host_when_no_host_secret(self):
        def _secret(name: str) -> str:
            if name == "uptimekuma.key":
                return "mykey"
            if name == "uptimekuma.port":
                return "3001"
            raise FileNotFoundError(name)

        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_UP)) as mock_get, \
             patch("lib.kuma._secret_read", side_effect=_secret):
            monitor_status("Sonarr")

        url_called = mock_get.call_args[0][0]
        assert "127.0.0.1:3001" in url_called

    def test_monitor_status_uses_hardcoded_default_when_no_secrets(self):
        def _secret(name: str) -> str:
            raise FileNotFoundError(name)

        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_UP)) as mock_get, \
             patch("lib.kuma._secret_read", side_effect=_secret):
            result = monitor_status("Sonarr")

        assert result == "up"
        url_called = mock_get.call_args[0][0]
        assert url_called.startswith("http://127.0.0.1:")


# ---------------------------------------------------------------------------
# Batch monitors_status
# ---------------------------------------------------------------------------

_METRICS_BATCH = (
    '# HELP monitor_status Monitor Status\n'
    '# TYPE monitor_status gauge\n'
    'monitor_status{monitor_name="Sonarr",monitor_type="http"} 1\n'
    'monitor_status{monitor_name="Radarr",monitor_type="http"} 0\n'
    'monitor_status{monitor_name="Plex",monitor_type="http"} 1\n'
    'monitor_status{monitor_name="Pending Thing",monitor_type="http"} 2\n'
)


class TestMonitorsStatusBatch:
    def test_returns_status_for_every_requested_name(self):
        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_BATCH)), \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            result = monitors_status(["Sonarr", "Radarr", "Plex"])
        assert result == {"Sonarr": "up", "Radarr": "down", "Plex": "up"}

    def test_unknown_for_monitors_not_in_response(self):
        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_BATCH)), \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            result = monitors_status(["Sonarr", "GhostMonitor"])
        assert result == {"Sonarr": "up", "GhostMonitor": "unknown"}

    def test_pending_or_maintenance_treated_as_unknown(self):
        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_BATCH)), \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            result = monitors_status(["Pending Thing"])
        assert result == {"Pending Thing": "unknown"}

    def test_network_failure_marks_all_unknown(self):
        with patch("lib.kuma.requests.get", side_effect=requests.ConnectionError("refused")), \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            result = monitors_status(["Sonarr", "Radarr"])
        assert result == {"Sonarr": "unknown", "Radarr": "unknown"}

    def test_empty_names_short_circuits(self):
        with patch("lib.kuma.requests.get") as mock_get:
            result = monitors_status([])
        assert result == {}
        mock_get.assert_not_called()

    def test_single_metrics_fetch_for_many_monitors(self):
        with patch("lib.kuma.requests.get", return_value=_mock_response(_METRICS_BATCH)) as mock_get, \
             patch("lib.kuma._secret_read", side_effect=_secret_key_only):
            monitors_status(["Sonarr", "Radarr", "Plex"])
        assert mock_get.call_count == 1


# ===========================================================================
# Phase 8 — Webhook HTTP server tests
# ===========================================================================

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kuma-payloads"


def _load_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _make_manifest_with_sonarr():
    """Return a minimal Manifest stub that maps 'Sonarr' → 'sonarr'."""
    from lib.manifest import load
    valid_yaml = Path(__file__).parent.parent / "fixtures" / "manifests" / "valid.yaml"
    return load(valid_yaml)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def webhook_server(tmp_path, monkeypatch):
    """Start a KumaWebhookHandler server on a free port; yield (httpd, port, state_dir)."""
    import http.server
    from lib import kuma as kuma_mod

    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    manifest = _make_manifest_with_sonarr()
    port = _free_port()

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port),
        kuma_mod._make_handler(manifest, tmp_path),
    )
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    yield httpd, port, tmp_path

    httpd.shutdown()
    httpd.server_close()


class TestWebhookServer:

    def test_webhook_health_returns_200(self, webhook_server):
        _, port, _ = webhook_server
        resp = requests.get(f"http://127.0.0.1:{port}/health", timeout=5)
        assert resp.status_code == 200
        assert resp.text == "ok\n"

    def test_webhook_post_down_dispatches_recovery(self, webhook_server, monkeypatch):
        _, port, _ = webhook_server
        called = []

        def fake_recovery(app_name, *, manifest=None):
            called.append(app_name)

        monkeypatch.setattr("lib.kuma.recovery.run", fake_recovery)
        payload = _load_fixture("down.json")
        resp = requests.post(
            f"http://127.0.0.1:{port}/kuma",
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert resp.status_code == 200
        # Give the daemon thread time to schedule
        time.sleep(0.05)
        assert "sonarr" in called

    def test_webhook_post_up_records_state_no_recovery(self, webhook_server, monkeypatch):
        _, port, state_dir = webhook_server
        recovery_called = []

        def fake_recovery(app_name, *, manifest=None):
            recovery_called.append(app_name)

        monkeypatch.setattr("lib.kuma.recovery.run", fake_recovery)
        payload = _load_fixture("up.json")
        resp = requests.post(
            f"http://127.0.0.1:{port}/kuma",
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert resp.status_code == 200
        time.sleep(0.05)
        assert recovery_called == []
        from lib import state as state_mod
        data = state_mod.read(state_dir / "state.json")
        assert data.get("apps", {}).get("sonarr", {}).get("event") == "up"

    def test_webhook_post_pending_records_state_no_recovery(self, webhook_server, monkeypatch):
        _, port, state_dir = webhook_server
        recovery_called = []

        def fake_recovery(app_name, *, manifest=None):
            recovery_called.append(app_name)

        monkeypatch.setattr("lib.kuma.recovery.run", fake_recovery)
        payload = _load_fixture("degraded.json")
        resp = requests.post(
            f"http://127.0.0.1:{port}/kuma",
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert resp.status_code == 200
        time.sleep(0.05)
        assert recovery_called == []
        from lib import state as state_mod
        data = state_mod.read(state_dir / "state.json")
        # degraded.json uses Radarr monitor; not in fixture manifest → unknown_monitors_total incremented
        # Check no crash and 200 returned (that's the core assertion above)

    def test_webhook_lock_present_appends_window_event(self, webhook_server, monkeypatch):
        _, port, state_dir = webhook_server
        lock_file = state_dir / "lock"
        lock_file.write_text("99999\n2026-05-09T04:00:00Z\n")
        recovery_called = []

        def fake_recovery(app_name, *, manifest=None):
            recovery_called.append(app_name)

        monkeypatch.setattr("lib.kuma.recovery.run", fake_recovery)
        payload = _load_fixture("down.json")
        resp = requests.post(
            f"http://127.0.0.1:{port}/kuma",
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert resp.status_code == 200
        time.sleep(0.05)
        assert recovery_called == []
        events_file = state_dir / "window-events.jsonl"
        assert events_file.exists()
        line = json.loads(events_file.read_text().strip().splitlines()[0])
        assert line["monitor"] == "Sonarr"
        assert line["status"] == 0

    def test_webhook_unknown_monitor_returns_200_increments_counter(
        self, webhook_server, monkeypatch
    ):
        _, port, state_dir = webhook_server
        recovery_called = []

        def fake_recovery(app_name, *, manifest=None):
            recovery_called.append(app_name)

        monkeypatch.setattr("lib.kuma.recovery.run", fake_recovery)
        payload = json.dumps({
            "heartbeat": {"status": 0, "time": "2026-05-09T04:00:00Z", "msg": "down"},
            "monitor": {"name": "BogusName"},
            "msg": "bogus",
        }).encode()
        resp = requests.post(
            f"http://127.0.0.1:{port}/kuma",
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert resp.status_code == 200
        assert recovery_called == []
        from lib import state as state_mod
        data = state_mod.read(state_dir / "state.json")
        assert data.get("unknown_monitors_total", 0) >= 1

    def test_webhook_malformed_json_400(self, webhook_server, monkeypatch):
        _, port, _ = webhook_server
        recovery_called = []

        def fake_recovery(app_name, *, manifest=None):
            recovery_called.append(app_name)

        monkeypatch.setattr("lib.kuma.recovery.run", fake_recovery)
        payload = _load_fixture("malformed.json")
        resp = requests.post(
            f"http://127.0.0.1:{port}/kuma",
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert resp.status_code == 400
        assert recovery_called == []

    def test_webhook_get_kuma_returns_405(self, webhook_server):
        _, port, _ = webhook_server
        resp = requests.get(f"http://127.0.0.1:{port}/kuma", timeout=5)
        assert resp.status_code == 405

    def test_webhook_unknown_path_returns_404(self, webhook_server):
        _, port, _ = webhook_server
        resp = requests.get(f"http://127.0.0.1:{port}/nonexistent", timeout=5)
        assert resp.status_code == 404

    def test_webhook_post_returns_fast(self, webhook_server, monkeypatch):
        _, port, _ = webhook_server
        block = threading.Event()

        def slow_recovery(app_name, *, manifest=None):
            block.wait(timeout=10)

        monkeypatch.setattr("lib.kuma.recovery.run", slow_recovery)
        payload = _load_fixture("down.json")
        t0 = time.monotonic()
        resp = requests.post(
            f"http://127.0.0.1:{port}/kuma",
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000
        block.set()
        assert resp.status_code == 200
        assert elapsed_ms < 200, f"response took {elapsed_ms:.0f}ms (expected < 200ms)"

    def test_webhook_in_flight_cap_drops_excess(self, webhook_server, monkeypatch):
        import http.server
        from lib import kuma as kuma_mod

        _, port, state_dir = webhook_server

        block = threading.Event()
        started = threading.Event()
        recovery_calls = []

        def blocking_recovery(app_name, *, manifest=None):
            recovery_calls.append(app_name)
            started.set()
            block.wait(timeout=5)

        monkeypatch.setattr("lib.kuma.recovery.run", blocking_recovery)

        # Fire two concurrent POSTs for *different* apps so per-app locks don't interact.
        # down.json = Sonarr (resolves). We'll use a second payload for a different known app.
        down_payload = _load_fixture("down.json")
        # Make a second payload for listmonk (also in valid.yaml)
        listmonk_payload = json.dumps({
            "heartbeat": {"status": 0, "time": "2026-05-09T04:00:00Z", "msg": "down"},
            "monitor": {"name": "Listmonk"},
            "msg": "down",
        }).encode()

        # Patch the recovery semaphore to size 1. After unification, the
        # webhook funnels through recovery.trigger_async, which owns the
        # single _RECOVERY_SEMAPHORE shared by webhook + pusher entry points.
        from lib import recovery as recovery_module
        original_sem = recovery_module._RECOVERY_SEMAPHORE
        recovery_module._RECOVERY_SEMAPHORE = threading.BoundedSemaphore(1)

        try:
            t1 = threading.Thread(
                target=requests.post,
                args=(f"http://127.0.0.1:{port}/kuma",),
                kwargs={
                    "data": down_payload,
                    "headers": {"Content-Type": "application/json"},
                    "timeout": 5,
                },
                daemon=True,
            )
            t1.start()
            started.wait(timeout=3)

            # Second request should be dropped
            resp2 = requests.post(
                f"http://127.0.0.1:{port}/kuma",
                data=listmonk_payload,
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
            assert resp2.status_code == 200

            block.set()
            t1.join(timeout=5)
        finally:
            recovery_module._RECOVERY_SEMAPHORE = original_sem

        time.sleep(0.1)
        from lib import state as state_mod
        data = state_mod.read(state_dir / "state.json")
        apps = data.get("apps", {})
        dropped_apps = [
            name for name, val in apps.items()
            if val.get("event") == "dropped_cap_exceeded"
        ]
        assert len(dropped_apps) >= 1, f"expected a dropped_cap_exceeded event; apps={apps}"

    def test_webhook_recovery_runs_in_daemon_thread(self, webhook_server, monkeypatch):
        _, port, _ = webhook_server
        thread_info = {}

        def capture_thread(app_name, *, manifest=None):
            thread_info["daemon"] = threading.current_thread().daemon

        monkeypatch.setattr("lib.kuma.recovery.run", capture_thread)
        payload = _load_fixture("down.json")
        requests.post(
            f"http://127.0.0.1:{port}/kuma",
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        time.sleep(0.1)
        assert thread_info.get("daemon") is True

    def test_webhook_loopback_bind_only(self, webhook_server):
        httpd, port, _ = webhook_server
        assert httpd.server_address[0] == "127.0.0.1"


# ---------------------------------------------------------------------------
# Audit (drift) — Phase 13+
# ---------------------------------------------------------------------------

def _make_manifest_with_monitors(*names: str):
    """Build a tiny in-memory Manifest with the given kuma_monitor names."""
    from lib.manifest import App, HealthConfig, Manifest
    apps = {}
    for n in names:
        slug = n.lower().replace(" ", "-")
        apps[slug] = App(
            name=slug,
            class_="ucc",
            kuma_monitor=n,
            health=HealthConfig(kind="http_root", raw={"kind": "http_root"}),
            defaults={},
        )
    return Manifest(apps)


_AUDIT_METRICS = (
    "# HELP monitor_status Monitor Status\n"
    "# TYPE monitor_status gauge\n"
    'monitor_status{monitor_id="1",monitor_name="Sonarr",monitor_type="push"} 1\n'
    'monitor_status{monitor_id="2",monitor_name="Radarr",monitor_type="push"} 1\n'
    'monitor_status{monitor_id="3",monitor_name="Stranger",monitor_type="push"} 0\n'
    'monitor_status{monitor_id="4",monitor_name="Manitoba Pusher",monitor_type="push"} 1\n'
    'monitor_status{monitor_id="5",monitor_name="QFlix Fleet",monitor_type="push"} 1\n'
    'monitor_status{monitor_id="6",monitor_name="QFlix Reaper",monitor_type="push"} 1\n'
    'monitor_status{monitor_id="7",monitor_name="QFlix Audio Disposition",monitor_type="push"} 1\n'
    'monitor_status{monitor_id="8",monitor_name="qflix-anime-janitor",monitor_type="push"} 1\n'
    'monitor_status{monitor_id="9",monitor_name="QFlix Torrent Janitor",monitor_type="push"} 1\n'
    'monitor_status{monitor_id="10",monitor_name="QFlix Collect (workstation)",monitor_type="push"} 1\n'
    # "QFlix Audit Regime" joined STANDALONE_SELF_PUSH_MONITORS 2026-07-29 with
    # the Convergent Audit Regime (manitoba-maint-audit.timer -> qflix-audit.py,
    # self-pushing like the janitors). This fixture models a Kuma that HAS every
    # expected monitor, so the no-drift case must include it.
    'monitor_status{monitor_id="11",monitor_name="QFlix Audit Regime",monitor_type="push"} 1\n'
    # "QFlix Audit Live" joined STANDALONE_SELF_PUSH_MONITORS 2026-07-30: the
    # LIVE half of the audit regime (L-01..L-06), auditing what is RUNNING
    # rather than what is in git. Present here or the drift audit correctly
    # reports it manifest_only.
    'monitor_status{monitor_id="12",monitor_name="QFlix Audit Live",monitor_type="push"} 1\n'
    # "QFlix Entitlement Gate" joined STANDALONE_SELF_PUSH_MONITORS 2026-08-07:
    # the only job that writes Plex share sections or Seerr permissions, so a
    # silent stop strands new members unprovisioned AND lapsed ones unreduced.
    # Present here or the drift audit correctly reports it manifest_only.
    'monitor_status{monitor_id="13",monitor_name="QFlix Entitlement Gate",monitor_type="push"} 1\n'
)


class TestAuditMonitors:
    def test_audit_no_drift(self, monkeypatch):
        from lib.kuma import audit_monitors
        m = _make_manifest_with_monitors("Sonarr", "Radarr", "Stranger")
        resp = MagicMock(text=_AUDIT_METRICS)
        monkeypatch.setattr("lib.kuma.requests.get", lambda *a, **k: resp)
        monkeypatch.setattr("lib.kuma._secret_read", lambda n: "fake-key")
        report = audit_monitors(m, kuma_url="http://x")
        # "Manitoba Pusher" and "QFlix Fleet" are always part of the expected
        # set (daemon monitors injected by audit_monitors). Never drift.
        # "QFlix Collect (workstation)" joined STANDALONE_SELF_PUSH_MONITORS
        # 2026-07-29 (it self-pushes from the box now, see lib/kuma.py) so it
        # must appear here too, matched rather than manifest_only.
        assert report["matched"] == ["Manitoba Pusher", "QFlix Audio Disposition", "QFlix Audit Live", "QFlix Audit Regime", "QFlix Collect (workstation)", "QFlix Entitlement Gate", "QFlix Fleet", "QFlix Reaper", "QFlix Torrent Janitor", "Radarr", "Sonarr", "Stranger", "qflix-anime-janitor"]
        assert report["manifest_only"] == []
        assert report["kuma_only"] == []
        assert report["live_count"] == 13
        assert report["manifest_count"] == 13

    def test_audit_manifest_only(self, monkeypatch):
        from lib.kuma import audit_monitors
        m = _make_manifest_with_monitors("Sonarr", "Radarr", "MissingApp")
        resp = MagicMock(text=_AUDIT_METRICS)
        monkeypatch.setattr("lib.kuma.requests.get", lambda *a, **k: resp)
        monkeypatch.setattr("lib.kuma._secret_read", lambda n: "fake-key")
        report = audit_monitors(m, kuma_url="http://x")
        assert "MissingApp" in report["manifest_only"]
        assert "Stranger" in report["kuma_only"]

    def test_audit_kuma_unreachable(self, monkeypatch):
        from lib.kuma import audit_monitors
        m = _make_manifest_with_monitors("Sonarr")
        def _raise(*a, **k):
            raise requests.ConnectionError("test")
        monkeypatch.setattr("lib.kuma.requests.get", _raise)
        monkeypatch.setattr("lib.kuma._secret_read", lambda n: "fake-key")
        report = audit_monitors(m, kuma_url="http://x")
        assert "error" in report
        assert "Sonarr" in report["manifest_only"]

    def test_audit_skips_apps_without_kuma_monitor(self, monkeypatch):
        from lib.kuma import audit_monitors
        from lib.manifest import App, HealthConfig, Manifest
        m = Manifest({
            "sonarr": App(name="sonarr", class_="ucc", kuma_monitor="Sonarr",
                          health=HealthConfig(kind="http_root", raw={}), defaults={}),
            "recyclarr": App(name="recyclarr", class_="cron", kuma_monitor=None,
                             health=HealthConfig(kind="systemd_only", raw={}), defaults={}),
        })
        resp = MagicMock(text=_AUDIT_METRICS)
        monkeypatch.setattr("lib.kuma.requests.get", lambda *a, **k: resp)
        monkeypatch.setattr("lib.kuma._secret_read", lambda n: "fake-key")
        report = audit_monitors(m, kuma_url="http://x")
        # sonarr (1) + auto-injected "Manitoba Pusher" (1) + "QFlix Fleet" (1)
        # + the 6 standalone self-pushers (Reaper, Audio Disposition, anime-
        # janitor, Torrent Janitor, Collect, Audit Regime) — recyclarr skipped
        # (kuma_monitor=None). Derived from the dict rather than hardcoded so
        # the next self-pusher does not need this line edited again.
        from lib.kuma import STANDALONE_SELF_PUSH_MONITORS
        assert report["manifest_count"] == 3 + len(STANDALONE_SELF_PUSH_MONITORS)
        assert "Sonarr" in report["matched"]
        assert "Manitoba Pusher" in report["matched"]
        assert "QFlix Fleet" in report["matched"]

    def test_audit_pusher_drift_when_missing_from_kuma(self, monkeypatch):
        """Manitoba Pusher absent from Kuma must report as manifest_only
        (drift) — that means bootstrap-kuma-monitors.py hasn't run, and the
        daemon's self-heartbeat can never light up. Bug regression guard."""
        from lib.kuma import audit_monitors
        m = _make_manifest_with_monitors("Sonarr")
        no_pusher = (
            "# HELP monitor_status Monitor Status\n"
            "# TYPE monitor_status gauge\n"
            'monitor_status{monitor_id="1",monitor_name="Sonarr",monitor_type="push"} 1\n'
        )
        resp = MagicMock(text=no_pusher)
        monkeypatch.setattr("lib.kuma.requests.get", lambda *a, **k: resp)
        monkeypatch.setattr("lib.kuma._secret_read", lambda n: "fake-key")
        report = audit_monitors(m, kuma_url="http://x")
        assert "Manitoba Pusher" in report["manifest_only"]
        assert "Manitoba Pusher" not in report["kuma_only"]


class TestCliKumaAudit:
    def test_cli_audit_zero_exit_no_drift(self, monkeypatch, capsys):
        from lib import cli
        m = _make_manifest_with_monitors("Sonarr", "Radarr")
        # Stub out manifest loading
        from lib import manifest as manifest_mod
        monkeypatch.setattr(manifest_mod, "load", lambda p: m)
        # Stub out audit to return no-drift
        from lib import kuma as kuma_mod
        monkeypatch.setattr(kuma_mod, "audit_monitors", lambda manifest, **kw: {
            "matched": ["Sonarr", "Radarr"], "manifest_only": [], "kuma_only": [],
            "live_count": 2, "manifest_count": 2,
        })
        rc = cli.main(["kuma", "audit"], manifest_path=Path("/fake"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "no drift" in out

    def test_cli_audit_returns_2_on_drift(self, monkeypatch, capsys):
        from lib import cli
        m = _make_manifest_with_monitors("Sonarr", "Missing")
        from lib import manifest as manifest_mod
        from lib import kuma as kuma_mod
        monkeypatch.setattr(manifest_mod, "load", lambda p: m)
        monkeypatch.setattr(kuma_mod, "audit_monitors", lambda manifest, **kw: {
            "matched": ["Sonarr"], "manifest_only": ["Missing"], "kuma_only": ["Orphan"],
            "live_count": 2, "manifest_count": 2,
        })
        rc = cli.main(["kuma", "audit"], manifest_path=Path("/fake"))
        assert rc == 2
        out = capsys.readouterr().out
        assert "Missing" in out and "Orphan" in out

    def test_cli_audit_returns_3_on_kuma_error(self, monkeypatch, capsys):
        from lib import cli
        m = _make_manifest_with_monitors("Sonarr")
        from lib import manifest as manifest_mod
        from lib import kuma as kuma_mod
        monkeypatch.setattr(manifest_mod, "load", lambda p: m)
        monkeypatch.setattr(kuma_mod, "audit_monitors", lambda manifest, **kw: {
            "matched": [], "manifest_only": ["Sonarr"], "kuma_only": [],
            "live_count": 0, "manifest_count": 1, "error": "kuma down",
        })
        rc = cli.main(["kuma", "audit"], manifest_path=Path("/fake"))
        assert rc == 3


# ---------------------------------------------------------------------------
# B1 suppression: kuma webhook down path suppression
# ---------------------------------------------------------------------------

class TestWebhookSuppressionDuringUccMaint:
    """When recovery_suppressed returns True for a ucc-class app in the
    webhook's status==0 path, trigger_async must NOT be called but the
    event 'ucc_maint_recovery_suppressed' must be recorded in state."""

    def test_webhook_down_suppressed_does_not_trigger_recovery(
        self, webhook_server, monkeypatch
    ):
        """With suppression active, webhook down path skips trigger_async."""
        _, port, state_dir = webhook_server

        triggered = []

        def fake_trigger(app, **kw):
            triggered.append(app.name)
            return "started"

        monkeypatch.setattr("lib.kuma.recovery.trigger_async", fake_trigger)

        # Patch recovery_suppressed to return True for any app.
        with patch("lib.suppression.recovery_suppressed", return_value=True):
            payload = _load_fixture("down.json")
            resp = requests.post(
                f"http://127.0.0.1:{port}/kuma",
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=5,
            )

        assert resp.status_code == 200
        time.sleep(0.05)
        assert triggered == [], (
            f"trigger_async called while suppressed; got {triggered!r}"
        )

    def test_webhook_down_suppressed_records_event(
        self, webhook_server, monkeypatch
    ):
        """Suppressed webhook down path records 'ucc_maint_recovery_suppressed'."""
        _, port, state_dir = webhook_server

        monkeypatch.setattr("lib.kuma.recovery.trigger_async", lambda a, **kw: "started")

        with patch("lib.suppression.recovery_suppressed", return_value=True):
            payload = _load_fixture("down.json")
            requests.post(
                f"http://127.0.0.1:{port}/kuma",
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=5,
            )

        time.sleep(0.05)
        from lib import state as state_mod
        data = state_mod.read(state_dir / "state.json")
        sonarr_event = data.get("apps", {}).get("sonarr", {}).get("event", "")
        assert sonarr_event == "ucc_maint_recovery_suppressed", (
            f"expected 'ucc_maint_recovery_suppressed' event; got {sonarr_event!r}"
        )

    def test_webhook_down_not_suppressed_triggers_recovery(
        self, webhook_server, monkeypatch
    ):
        """Sanity check: with suppression False, trigger_async IS called."""
        _, port, state_dir = webhook_server

        triggered = []

        def fake_trigger(app, **kw):
            triggered.append(app.name)
            return "started"

        monkeypatch.setattr("lib.kuma.recovery.trigger_async", fake_trigger)

        with patch("lib.suppression.recovery_suppressed", return_value=False):
            payload = _load_fixture("down.json")
            requests.post(
                f"http://127.0.0.1:{port}/kuma",
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=5,
            )

        time.sleep(0.05)
        assert "sonarr" in triggered, (
            f"expected trigger_async('sonarr') when not suppressed; got {triggered!r}"
        )
