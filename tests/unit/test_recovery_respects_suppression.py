"""QFLX-64: recovery.trigger_async is the suppression chokepoint.

Before the fix only the pusher consulted suppression.recovery_suppressed before
calling trigger_async. deep_check (and any future caller) could therefore run
lifecycle.restart -> `app-<slug> start` on a push-suppressed app -- e.g. a
pending-swap app whose UCC container must stay dormant (I-9). These tests use a
REAL push-suppress.json under an isolated MANITOBA_STATE_DIR (no predicate
mocks) so they exercise the same file the box reads, and assert at the
lifecycle boundary: no start/restart call ever leaves the process.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from lib.health import HealthResult
from lib.manifest import load as manifest_load

_VALID = Path(__file__).parent.parent / "fixtures" / "manifests" / "valid.yaml"
_DOWN_PAYLOAD = Path(__file__).parent.parent / "fixtures" / "kuma-payloads" / "down.json"


def _suppress(state_dir: Path, app_name: str) -> None:
    (state_dir / "push-suppress.json").write_text(
        json.dumps({app_name: {"reason": "pending-swap: container dormant",
                               "since": "2026-10-20T00:00:00Z",
                               # QFLX-82: undated mutes are ignored; this one is live.
                               "until": "2099-01-01T00:00:00Z"}}),
        encoding="utf-8")


@pytest.fixture()
def manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    return manifest_load(_VALID)


@pytest.fixture()
def no_lifecycle():
    """Fail loudly if anything reaches the lifecycle layer, and stub run() so a
    non-suppressed control case never spawns a real 3-attempt loop."""
    with patch("lib.recovery.lifecycle.restart") as restart, \
         patch("lib.recovery.lifecycle.start") as start, \
         patch("lib.recovery.run", return_value=None) as run:
        yield {"restart": restart, "start": start, "run": run}


def _assert_untouched(mocks) -> None:
    mocks["run"].assert_not_called()
    mocks["restart"].assert_not_called()
    mocks["start"].assert_not_called()


# --- direct chokepoint -------------------------------------------------------

def test_trigger_async_push_suppressed_returns_suppressed(manifest, tmp_path, no_lifecycle):
    from lib import recovery
    _suppress(tmp_path, "sonarr")
    assert recovery.trigger_async(manifest.app("sonarr"), manifest=manifest) == "suppressed"
    _assert_untouched(no_lifecycle)


def test_trigger_async_unsuppressed_app_unchanged(manifest, tmp_path, no_lifecycle):
    from lib import recovery
    _suppress(tmp_path, "some-other-app")   # registry exists, sonarr not in it
    assert recovery.trigger_async(manifest.app("sonarr"), manifest=manifest) == "started"
    time.sleep(0.05)                        # let the worker thread call run()
    no_lifecycle["run"].assert_called_once()


def test_trigger_async_ucc_maint_returns_suppressed(manifest, no_lifecycle):
    # The other half of recovery_suppressed: UCC maintenance gates ucc apps.
    from lib import recovery
    with patch("lib.suppression.ucc_active", return_value=True):
        assert recovery.trigger_async(manifest.app("sonarr"), manifest=manifest) == "suppressed"
    _assert_untouched(no_lifecycle)


def test_corrupt_registry_fails_open(manifest, tmp_path, no_lifecycle):
    # A garbled registry must never silence recovery of a genuinely-down app.
    from lib import recovery
    (tmp_path / "push-suppress.json").write_text("{not json", encoding="utf-8")
    assert recovery.trigger_async(manifest.app("sonarr"), manifest=manifest) == "started"


# --- deep_check path (previously ungated) -----------------------------------

def test_deep_check_does_not_recover_push_suppressed_app(manifest, tmp_path, no_lifecycle):
    from lib import deep_check
    _suppress(tmp_path, "sonarr")
    down = HealthResult(ok=False, latency_ms=None, reason="connection refused")
    up = HealthResult(ok=True, latency_ms=5, reason="ok")
    with patch("lib.deep_check.health.probe",
               side_effect=lambda app, **kw: down if app.name == "sonarr" else up), \
         patch("lib.deep_check.notify.notify"):
        result = deep_check.run_deep_check(reason="test", manifest=manifest)
    assert result["recovery_triggered"]["sonarr"] == "suppressed"
    _assert_untouched(no_lifecycle)


# --- Kuma webhook path ------------------------------------------------------

def test_webhook_down_does_not_recover_push_suppressed_app(manifest, tmp_path, no_lifecycle):
    import http.server
    import socket
    import threading
    from lib import kuma as kuma_mod

    _suppress(tmp_path, "sonarr")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), kuma_mod._make_handler(manifest, tmp_path))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        resp = requests.post(f"http://127.0.0.1:{port}/kuma",
                             data=_DOWN_PAYLOAD.read_bytes(),
                             headers={"Content-Type": "application/json"}, timeout=5)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert resp.status_code == 200
    time.sleep(0.05)
    _assert_untouched(no_lifecycle)


# --- manual CLI path --------------------------------------------------------

def test_cli_recover_refuses_push_suppressed_app(manifest, tmp_path, no_lifecycle, capsys):
    from lib.cli import main
    _suppress(tmp_path, "sonarr")
    rc = main(["recover", "sonarr"], manifest_path=_VALID)
    assert rc == 2
    assert "--force" in capsys.readouterr().err
    _assert_untouched(no_lifecycle)


def test_cli_recover_force_bypasses_suppression(manifest, tmp_path, no_lifecycle):
    from lib.cli import main
    _suppress(tmp_path, "sonarr")
    no_lifecycle["run"].return_value = {"app": "sonarr", "event": "recovered"}
    assert main(["recover", "sonarr", "--force"], manifest_path=_VALID) == 0
    no_lifecycle["run"].assert_called_once()
