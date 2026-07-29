"""tests/unit/test_window.py — TDD tests for lib/window.py.

All file I/O uses tmp_path. No subprocess, no network, no SSH.
kill-0 process checks are skipped on Windows (os.name != "posix").
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

from lib.window import (
    WindowAlreadyOpen,
    WindowOrchestrator,
    WindowSummary,
    watchdog_clear_stale_lock,
)
from lib.manifest import App, HealthConfig, UpgradeConfig, VersionPin
from lib.health import HealthResult
from lib.lifecycle import LifecycleError, LifecycleResult


# ---------------------------------------------------------------------------
# App / manifest builders
# ---------------------------------------------------------------------------

def _make_defaults() -> dict:
    return {
        "health_timeout_s": 5,
        "recovery_attempts": 3,
        "recovery_backoff_s": [10, 30, 60],
        "lifecycle_timeout_s": 60,
        "kuma_recheck_delay_s": 90,
    }


def _make_app(
    name: str,
    *,
    class_: str = "systemd",
    unit: str | None = None,
    max_version: str | None = None,
    kuma_monitor: str | None = None,
) -> App:
    raw: dict = {"class": class_}
    if unit:
        raw["unit"] = unit

    upgrade = None
    if max_version is not None:
        vp = VersionPin(max=max_version)
        upgrade = UpgradeConfig(kind="zip_swap", version_pin=vp)

    return App(
        name=name,
        class_=class_,
        kuma_monitor=kuma_monitor,
        health=HealthConfig(kind="systemd_only", raw=raw),
        defaults=_make_defaults(),
        upgrade=upgrade,
        raw=raw,
    )


class _FakeManifest:
    """Minimal manifest stub that supports .app() and .apps()."""

    def __init__(self, apps: dict[str, App]) -> None:
        self._apps = apps

    def app(self, name: str) -> App:
        if name not in self._apps:
            raise KeyError(f"No app named '{name}' in manifest")
        return self._apps[name]

    def apps(self):
        return iter(self._apps.values())


def _simple_manifest() -> _FakeManifest:
    """Three-app manifest (systemd, cron, ucc) for general tests."""
    return _FakeManifest({
        "listmonk": _make_app("listmonk", class_="systemd", unit="listmonk.service"),
        "recyclarr": _make_app("recyclarr", class_="cron", unit="recyclarr.timer"),
        "sonarr": _make_app("sonarr", class_="ucc"),
    })


# ---------------------------------------------------------------------------
# Helper: write a lockfile
# ---------------------------------------------------------------------------

def _write_lock(state_dir: Path, pid: int, start_iso: str | None = None) -> None:
    if start_iso is None:
        start_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    (state_dir / "lock").write_text(f"{pid}\n{start_iso}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. open() creates lockfile with PID + UTC ISO
# ---------------------------------------------------------------------------

def test_open_creates_lockfile_with_pid_and_iso(tmp_path):
    manifest = _simple_manifest()
    w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
    w.open()

    lock = tmp_path / "lock"
    assert lock.exists(), "lockfile must be created by open()"

    lines = lock.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2, "lockfile must have at least 2 lines (pid, iso)"
    assert lines[0] == str(os.getpid()), "first line must be current PID"
    # second line must parse as a datetime
    dt = datetime.fromisoformat(lines[1].rstrip("Z"))
    assert dt.year >= 2020, "timestamp must be a plausible UTC datetime"


# ---------------------------------------------------------------------------
# 2. open() refuses when lock present with live PID
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="kill(0) semantics posix-only")
def test_open_refuses_when_lock_present_with_live_pid(tmp_path):
    manifest = _simple_manifest()
    _write_lock(tmp_path, os.getpid())  # current PID is definitely alive

    w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
    with pytest.raises(WindowAlreadyOpen):
        w.open()


# ---------------------------------------------------------------------------
# 3. open(force=True) overrides an existing lock
# ---------------------------------------------------------------------------

def test_open_force_overrides(tmp_path):
    manifest = _simple_manifest()
    _write_lock(tmp_path, os.getpid())

    w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
    # force=True must succeed even when a lock with an alive PID exists
    w.open(force=True)

    lines = (tmp_path / "lock").read_text(encoding="utf-8").splitlines()
    assert lines[0] == str(os.getpid())


# ---------------------------------------------------------------------------
# 4. open() takes over a dead PID without force
# ---------------------------------------------------------------------------

def test_open_takes_over_dead_pid(tmp_path):
    manifest = _simple_manifest()
    dead_pid = 999999  # practically guaranteed not to exist
    _write_lock(tmp_path, dead_pid)

    w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
    # On posix: kill(0) raises ProcessLookupError → takeover.
    # On Windows: any existing lock is treated as stale.
    w.open()  # must NOT raise

    lines = (tmp_path / "lock").read_text(encoding="utf-8").splitlines()
    assert lines[0] == str(os.getpid())


# ---------------------------------------------------------------------------
# 5. drain_queue drops unknown app
# ---------------------------------------------------------------------------

def test_drain_queue_drops_unknown_app(tmp_path):
    manifest = _FakeManifest({
        "listmonk": _make_app("listmonk", class_="systemd", unit="listmonk.service"),
    })

    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        json.dumps({"app": "ghost-app", "target_version": "1.0.0", "enqueued_at": "2026-05-09T00:00:00Z"}) + "\n"
        + json.dumps({"app": "listmonk", "target_version": "1.2.3", "enqueued_at": "2026-05-09T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    with patch("lib.window.lifecycle.upgrade", side_effect=LifecycleError("not implemented")):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        w.drain_queue()
        summary = w.close()

    assert summary.queue_dropped_unknown == 1
    assert summary.queue_processed == 1   # listmonk attempted
    assert summary.queue_succeeded == 0   # upgrade raised LifecycleError


# ---------------------------------------------------------------------------
# 6. drain_queue blocks max_version entries
# ---------------------------------------------------------------------------

def test_drain_queue_blocks_max_version(tmp_path):
    tdarr = _make_app("tdarr-server", class_="systemd", unit="tdarr-server.service", max_version="2.17.01")
    manifest = _FakeManifest({"tdarr-server": tdarr})

    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        json.dumps({"app": "tdarr-server", "target_version": "2.71.01", "enqueued_at": "2026-05-09T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    with patch("lib.window.lifecycle.upgrade") as mock_upgrade:
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        w.drain_queue()
        summary = w.close()

    mock_upgrade.assert_not_called()
    assert summary.queue_dropped_max_block == 1
    assert summary.queue_processed == 0


# ---------------------------------------------------------------------------
# 7. drain_queue defers active cron apps
# ---------------------------------------------------------------------------

def test_drain_queue_defers_active_cron(tmp_path):
    recyclarr = _make_app("recyclarr", class_="cron", unit="recyclarr.timer")
    manifest = _FakeManifest({"recyclarr": recyclarr})

    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        json.dumps({"app": "recyclarr", "target_version": "6.0.0", "enqueued_at": "2026-05-09T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    active_result = LifecycleResult(ok=True, duration_s=0.0, stdout="active", stderr="", reason="active")

    with patch("lib.window.lifecycle.upgrade") as mock_upgrade, \
         patch("lib.window.lifecycle.status", return_value=active_result):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        w.drain_queue()
        summary = w.close()

    mock_upgrade.assert_not_called()
    assert summary.queue_deferred_active_cron == 1

    deferred = tmp_path / "queue.deferred.jsonl"
    assert deferred.exists(), "deferred entry must be written to queue.deferred.jsonl"
    line = json.loads(deferred.read_text(encoding="utf-8").strip())
    assert line["app"] == "recyclarr"


# ---------------------------------------------------------------------------
# 8. drain_queue truncates queue.jsonl after processing
# ---------------------------------------------------------------------------

def test_drain_queue_truncates_after_processing(tmp_path):
    manifest = _FakeManifest({
        "listmonk": _make_app("listmonk", class_="systemd", unit="listmonk.service"),
        "recyclarr": _make_app("recyclarr", class_="cron", unit="recyclarr.timer"),
        "sonarr": _make_app("sonarr", class_="ucc"),
    })

    entries = [
        {"app": "listmonk", "target_version": "1.2.0", "enqueued_at": "2026-05-09T00:00:00Z"},
        {"app": "recyclarr", "target_version": "6.0.0", "enqueued_at": "2026-05-09T00:00:00Z"},
        {"app": "sonarr", "target_version": "3.0.0", "enqueued_at": "2026-05-09T00:00:00Z"},
    ]
    queue = tmp_path / "queue.jsonl"
    queue.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    inactive = LifecycleResult(ok=False, duration_s=0.0, stdout="inactive", stderr="", reason="inactive")

    with patch("lib.window.lifecycle.upgrade", side_effect=LifecycleError("not impl")), \
         patch("lib.window.lifecycle.status", return_value=inactive):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        w.drain_queue()
        w.close()

    content = queue.read_text(encoding="utf-8")
    assert content.strip() == "", "queue.jsonl must be empty after drain"


# ---------------------------------------------------------------------------
# 9. drain_queue lifecycle error counted as failure (not exception)
# ---------------------------------------------------------------------------

def test_drain_queue_lifecycle_error_counted_as_failure(tmp_path):
    manifest = _FakeManifest({
        "listmonk": _make_app("listmonk", class_="systemd", unit="listmonk.service"),
    })

    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        json.dumps({"app": "listmonk", "target_version": "1.2.3", "enqueued_at": "2026-05-09T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    with patch("lib.window.lifecycle.upgrade", side_effect=LifecycleError("not implemented")):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        w.drain_queue()  # must NOT raise
        summary = w.close()

    assert summary.queue_processed == 1
    assert summary.queue_succeeded == 0


# ---------------------------------------------------------------------------
# 10. smoke() runs health.probe for every app in manifest
# ---------------------------------------------------------------------------

def test_smoke_runs_health_for_every_app(tmp_path):
    manifest = _simple_manifest()  # 3 apps
    ok_result = HealthResult(ok=True, latency_ms=10, reason="ok")

    with patch("lib.window.health.probe", return_value=ok_result) as mock_probe:
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        w.smoke()
        summary = w.close()

    assert mock_probe.call_count == 3
    assert set(summary.smoke_results.keys()) == {"listmonk", "recyclarr", "sonarr"}
    assert all(v is True for v in summary.smoke_results.values())


# ---------------------------------------------------------------------------
# 11. close() removes lock and writes to window-log/<date>.log
# ---------------------------------------------------------------------------

def test_close_removes_lock_writes_summary_to_window_log(tmp_path):
    manifest = _simple_manifest()

    with patch("lib.window.health.probe", return_value=HealthResult(ok=True, latency_ms=5, reason="ok")), \
         patch("lib.window.lifecycle.upgrade", side_effect=LifecycleError("not impl")):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        w.close()

    assert not (tmp_path / "lock").exists(), "lock must be removed by close()"

    log_dir = tmp_path / "window-log"
    assert log_dir.is_dir(), "window-log directory must exist"
    log_files = list(log_dir.iterdir())
    assert len(log_files) >= 1, "at least one log file must be written"
    log_content = log_files[0].read_text(encoding="utf-8")
    assert len(log_content) > 0, "log file must not be empty"


# ---------------------------------------------------------------------------
# 12. run() calls open, drain_queue, smoke, close, and notifies twice
# ---------------------------------------------------------------------------

def test_run_calls_open_drain_smoke_close_notify_twice(tmp_path):
    manifest = _simple_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")

    call_order = []

    def _fake_open(force=False):
        call_order.append("open")

    def _fake_drain():
        call_order.append("drain_queue")

    def _fake_upgrade_sweep():
        call_order.append("upgrade_sweep")

    def _fake_smoke():
        call_order.append("smoke")

    def _fake_close():
        call_order.append("close")
        return WindowSummary(
            started_at="2026-05-09T11:00:00Z",
            closed_at="2026-05-09T11:01:00Z",
            queue_processed=0,
            queue_succeeded=0,
            queue_dropped_unknown=0,
            queue_dropped_max_block=0,
            queue_deferred_active_cron=0,
            smoke_results={},
            notes=[],
        )

    def _fake_wait_green(**_):
        call_order.append("wait_green")
        return True

    with patch("lib.window.notify.notify") as mock_notify, \
         patch("lib.window.listmonk.fire_template_campaign", return_value=True), \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.deep_check.run_deep_check"):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open = _fake_open
        w.drain_queue = _fake_drain
        w.upgrade_sweep = _fake_upgrade_sweep
        w.smoke = _fake_smoke
        w.wait_for_kuma_green = _fake_wait_green
        w.close = _fake_close
        w.run()

    assert call_order == ["open", "drain_queue", "upgrade_sweep", "smoke", "wait_green", "close"]
    assert mock_notify.call_count == 2


# ---------------------------------------------------------------------------
# 13. run() with dry_run=True skips lifecycle upgrade
# ---------------------------------------------------------------------------

def test_run_dry_run_skips_lifecycle_upgrade(tmp_path):
    manifest = _FakeManifest({
        "listmonk": _make_app("listmonk", class_="systemd", unit="listmonk.service"),
    })

    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        json.dumps({"app": "listmonk", "target_version": "1.2.3", "enqueued_at": "2026-05-09T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")

    with patch("lib.window.lifecycle.upgrade") as mock_upgrade, \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.notify.notify"):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest, dry_run=True)
        summary = w.run()

    mock_upgrade.assert_not_called()
    assert summary.queue_processed == 1
    assert summary.queue_succeeded == 0
    assert any("dry-run" in n.lower() for n in summary.notes)


# ---------------------------------------------------------------------------
# 14. status() when lock is absent
# ---------------------------------------------------------------------------

def test_status_lock_absent(tmp_path):
    manifest = _simple_manifest()
    w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
    s = w.status()

    assert s["present"] is False


# ---------------------------------------------------------------------------
# 15. status() when lock is present with current PID
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="kill(0) alive check posix-only")
def test_status_lock_present_alive_pid(tmp_path):
    manifest = _simple_manifest()
    start_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_lock(tmp_path, os.getpid(), start_iso)

    w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
    s = w.status()

    assert s["present"] is True
    assert s["pid"] == os.getpid()
    assert s["started_at"] == start_iso
    assert s["alive"] is True


# ---------------------------------------------------------------------------
# 16. watchdog_clear_stale_lock — dead PID
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="kill(0) semantics posix-only")
def test_watchdog_clears_stale_lock_dead_pid(tmp_path):
    dead_pid = 999999
    _write_lock(tmp_path, dead_pid)

    with patch("lib.window.notify.notify") as mock_notify:
        cleared = watchdog_clear_stale_lock(state_dir=tmp_path)

    assert cleared is True
    assert not (tmp_path / "lock").exists()
    mock_notify.assert_called_once()
    assert "watchdog" in mock_notify.call_args[0][0].lower() or "stale" in mock_notify.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# 17. watchdog_clear_stale_lock — old start time (> 4h ago)
# ---------------------------------------------------------------------------

def test_watchdog_clears_stale_lock_old_start_time(tmp_path):
    old_start = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
    _write_lock(tmp_path, os.getpid(), old_start)

    with patch("lib.window.notify.notify") as mock_notify:
        cleared = watchdog_clear_stale_lock(state_dir=tmp_path)

    assert cleared is True
    assert not (tmp_path / "lock").exists()
    mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# 18. watchdog_clear_stale_lock — active lock (alive PID, recent start)
# ---------------------------------------------------------------------------

def test_watchdog_leaves_active_lock_alone(tmp_path):
    recent_start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _write_lock(tmp_path, os.getpid(), recent_start)

    with patch("lib.window.notify.notify") as mock_notify:
        cleared = watchdog_clear_stale_lock(state_dir=tmp_path)

    assert cleared is False
    assert (tmp_path / "lock").exists(), "active lock must not be removed"
    mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# 19. wait_for_kuma_green — converges immediately when all monitors up
# ---------------------------------------------------------------------------

def _green_monitor_manifest() -> _FakeManifest:
    return _FakeManifest({
        "sonarr": _make_app("sonarr", class_="ucc", kuma_monitor="Sonarr"),
        "radarr": _make_app("radarr", class_="ucc", kuma_monitor="Radarr"),
        "plex": _make_app("plex", class_="systemd", unit="plex.service",
                          kuma_monitor="Plex"),
    })


def test_wait_for_kuma_green_returns_true_when_all_up(tmp_path):
    manifest = _green_monitor_manifest()
    all_up = {"Sonarr": "up", "Radarr": "up", "Plex": "up"}

    with patch("lib.window.kuma.monitors_status", return_value=all_up):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        sleep_calls: list = []
        result = w.wait_for_kuma_green(sleep=sleep_calls.append, now=lambda: 0.0)
        summary = w.close()

    assert result is True
    assert summary.kuma_converged is True
    assert summary.kuma_still_down == []
    assert sleep_calls == [], "must not sleep when monitors already green"


def test_wait_for_kuma_green_returns_true_when_no_monitors(tmp_path):
    """A manifest with zero kuma_monitor apps short-circuits to True without
    touching Kuma — there's nothing to wait on."""
    manifest = _FakeManifest({
        "listmonk": _make_app("listmonk", class_="systemd", unit="listmonk.service"),
    })

    with patch("lib.window.kuma.monitors_status") as mock_status:
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        result = w.wait_for_kuma_green(sleep=lambda _: None, now=lambda: 0.0)

    assert result is True
    mock_status.assert_not_called()


# ---------------------------------------------------------------------------
# 20. wait_for_kuma_green — polls until all green
# ---------------------------------------------------------------------------

def test_wait_for_kuma_green_polls_until_converged(tmp_path):
    manifest = _green_monitor_manifest()
    statuses = iter([
        {"Sonarr": "up", "Radarr": "down", "Plex": "up"},   # poll 1
        {"Sonarr": "up", "Radarr": "down", "Plex": "up"},   # poll 2
        {"Sonarr": "up", "Radarr": "up",   "Plex": "up"},   # poll 3 — green
    ])

    fake_clock = [0.0]
    def _now():
        return fake_clock[0]
    sleep_calls: list[int] = []
    def _sleep(s):
        sleep_calls.append(s)
        fake_clock[0] += s

    with patch("lib.window.kuma.monitors_status", side_effect=lambda names: next(statuses)):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        result = w.wait_for_kuma_green(
            max_wait_s=3600, poll_interval_s=60,
            sleep=_sleep, now=_now,
        )
        summary = w.close()

    assert result is True
    assert summary.kuma_converged is True
    assert summary.kuma_wait_s == 120, "two 60s sleeps before the green poll"
    assert sleep_calls == [60, 60]


# ---------------------------------------------------------------------------
# 21. wait_for_kuma_green — times out when monitors stay down
# ---------------------------------------------------------------------------

def test_wait_for_kuma_green_times_out(tmp_path):
    manifest = _green_monitor_manifest()
    perma_down = {"Sonarr": "up", "Radarr": "down", "Plex": "down"}

    fake_clock = [0.0]
    def _now():
        return fake_clock[0]
    def _sleep(s):
        fake_clock[0] += s

    with patch("lib.window.kuma.monitors_status", return_value=perma_down):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        result = w.wait_for_kuma_green(
            max_wait_s=300, poll_interval_s=60,
            sleep=_sleep, now=_now,
        )
        summary = w.close()

    assert result is False
    assert summary.kuma_converged is False
    assert sorted(summary.kuma_still_down) == ["Plex", "Radarr"]
    assert summary.kuma_wait_s >= 300


def test_wait_for_kuma_green_treats_unknown_as_not_green(tmp_path):
    """A monitor stuck at 'unknown' (pending / maintenance / network blip)
    must NOT count as green — keep polling until it goes up or we time out."""
    manifest = _green_monitor_manifest()
    statuses = iter([
        {"Sonarr": "up", "Radarr": "unknown", "Plex": "up"},
        {"Sonarr": "up", "Radarr": "up",      "Plex": "up"},
    ])

    fake_clock = [0.0]
    def _now():
        return fake_clock[0]
    def _sleep(s):
        fake_clock[0] += s

    with patch("lib.window.kuma.monitors_status", side_effect=lambda names: next(statuses)):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        result = w.wait_for_kuma_green(
            max_wait_s=3600, poll_interval_s=60,
            sleep=_sleep, now=_now,
        )

    assert result is True


# ---------------------------------------------------------------------------
# 22. run() integration — pre/post notify + green-poll + close
# ---------------------------------------------------------------------------

def test_run_fires_pre_and_post_notifications_with_kuma_outcome(tmp_path):
    manifest = _green_monitor_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")
    all_up = {"Sonarr": "up", "Radarr": "up", "Plex": "up"}

    # Two queue entries so the pre-maint ping reports queued=2
    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        json.dumps({"app": "sonarr", "target_version": "1.0.0"}) + "\n"
        + json.dumps({"app": "radarr", "target_version": "1.0.0"}) + "\n",
        encoding="utf-8",
    )

    with patch("lib.window.notify.notify") as mock_notify, \
         patch("lib.window.listmonk.fire_template_campaign", return_value=True) as mock_listmonk, \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.lifecycle.upgrade"), \
         patch("lib.window.run_upgrade_sweep", return_value={}), \
         patch("lib.window.kuma.monitors_status", return_value=all_up), \
         patch("lib.window.deep_check.run_deep_check"):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        summary = w.run()

    assert summary.kuma_converged is True
    # exactly two Discord notifications: pre-maint and post-maint
    assert mock_notify.call_count == 2
    pre_msg = mock_notify.call_args_list[0].args[0]
    post_msg = mock_notify.call_args_list[1].args[0]
    assert "queued=2" in pre_msg
    assert "opened" in pre_msg
    assert "closed" in post_msg
    assert "kuma green" in post_msg

    # exactly two Listmonk campaigns: start (open) + complete (close)
    assert mock_listmonk.call_count == 2
    titles = [c.kwargs["template_title"] for c in mock_listmonk.call_args_list]
    assert titles == ["Maintenance Window Start", "Maintenance Window Complete"]


def test_run_escalates_notification_level_on_kuma_timeout(tmp_path):
    """When wait_for_kuma_green times out, the post-maint ping must use
    level='warning' so the operator actually notices."""
    manifest = _green_monitor_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")
    perma_down = {"Sonarr": "up", "Radarr": "down", "Plex": "down"}

    fake_clock = [0.0]
    def _now():
        return fake_clock[0]
    def _sleep(s):
        fake_clock[0] += s

    # Force a tiny timeout so the test runs fast
    with patch("lib.window.notify.notify") as mock_notify, \
         patch("lib.window.listmonk.fire_template_campaign", return_value=True), \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.run_upgrade_sweep", return_value={}), \
         patch("lib.window.kuma.monitors_status", return_value=perma_down), \
         patch("lib.window._KUMA_GREEN_MAX_WAIT_S", 120), \
         patch("lib.window._KUMA_GREEN_POLL_INTERVAL_S", 60), \
         patch("lib.window.time.sleep", side_effect=_sleep), \
         patch("lib.window.time.monotonic", side_effect=_now):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        summary = w.run()

    assert summary.kuma_converged is False
    post_call = mock_notify.call_args_list[1]
    assert post_call.kwargs.get("level") == "warning"
    assert "TIMEOUT" in post_call.args[0]
    assert "Radarr" in post_call.args[0] or "Plex" in post_call.args[0]


def test_run_dry_run_skips_kuma_poll_and_listmonk(tmp_path):
    """dry-run mode must not call wait_for_kuma_green (it's a fast preview)
    nor fire real Listmonk emails to subscribers."""
    manifest = _green_monitor_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")

    with patch("lib.window.notify.notify"), \
         patch("lib.window.listmonk.fire_template_campaign") as mock_listmonk, \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.kuma.monitors_status") as mock_status:
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest, dry_run=True)
        summary = w.run()

    mock_status.assert_not_called()
    mock_listmonk.assert_not_called()
    assert summary.kuma_converged is True
    assert any("dry-run" in n.lower() for n in summary.notes)


# ---------------------------------------------------------------------------
# Sub-project D: deep_check.run_deep_check integration in run()
# ---------------------------------------------------------------------------

def test_run_calls_deep_check_after_close(tmp_path):
    """run() must call deep_check.run_deep_check(reason='qflix-window', ...) after close."""
    manifest = _simple_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")

    with patch("lib.window.notify.notify"), \
         patch("lib.window.listmonk.fire_template_campaign", return_value=True), \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.run_upgrade_sweep", return_value={}), \
         patch("lib.window.kuma.monitors_status", return_value={}), \
         patch("lib.window.deep_check.run_deep_check") as mock_dc:
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.run()

    mock_dc.assert_called_once()
    kwargs = mock_dc.call_args[1]
    assert kwargs.get("reason") == "qflix-window"
    assert kwargs.get("manifest") is manifest


def test_run_dry_run_skips_deep_check(tmp_path):
    """dry_run=True must skip deep_check.run_deep_check entirely."""
    manifest = _simple_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")

    with patch("lib.window.notify.notify"), \
         patch("lib.window.listmonk.fire_template_campaign", return_value=True), \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.kuma.monitors_status", return_value={}), \
         patch("lib.window.deep_check.run_deep_check") as mock_dc:
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest, dry_run=True)
        w.run()

    mock_dc.assert_not_called()


def test_run_deep_check_exception_does_not_fail_run(tmp_path):
    """A raising deep_check must not propagate out of run() — window close is primary."""
    manifest = _simple_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")

    with patch("lib.window.notify.notify"), \
         patch("lib.window.listmonk.fire_template_campaign", return_value=True), \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.run_upgrade_sweep", return_value={}), \
         patch("lib.window.kuma.monitors_status", return_value={}), \
         patch("lib.window.deep_check.run_deep_check", side_effect=RuntimeError("deep-check crash")):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        # must not raise
        summary = w.run()

    # run() returned normally despite deep_check crash
    assert summary is not None


# ---------------------------------------------------------------------------
# UCC app-upgrade sweep folded into the window (2026-06-28)
# ---------------------------------------------------------------------------

def test_run_upgrade_sweep_parses_results(tmp_path):
    """run_upgrade_sweep invokes the script (via injected runner) and returns the
    JSON the script wrote to last-upgrade.json."""
    from lib import window
    results = {
        "schema_version": 1,
        "summary": {"upgraded": 2, "failed": 0, "bailed": 0, "total": 2, "skipped": 5},
        "apps": {"sonarr": "upgraded", "radarr": "upgraded"},
        "upgraded": ["radarr", "sonarr"],
    }

    captured = {}

    def fake_runner(cmd, env=None, timeout=None, capture_output=False, text=False):
        captured["cmd"] = cmd
        captured["budget"] = env.get("MANITOBA_UPGRADE_BUDGET_S")
        Path(env["MANITOBA_UPGRADE_RESULTS"]).write_text(json.dumps(results), encoding="utf-8")
        class _R:
            returncode = 0; stdout = ""; stderr = ""
        return _R()

    out = window.run_upgrade_sweep(state_dir=tmp_path, runner=fake_runner)
    assert out == results
    assert (tmp_path / "last-upgrade.json").exists()
    assert captured["cmd"][0] == "bash"
    assert "--dry-run" not in captured["cmd"]
    assert captured["budget"]  # budget passed to the script


def test_run_upgrade_sweep_dry_run_passes_flag(tmp_path):
    from lib import window

    def fake_runner(cmd, env=None, timeout=None, capture_output=False, text=False):
        Path(env["MANITOBA_UPGRADE_RESULTS"]).write_text('{"summary":{}}', encoding="utf-8")
        class _R:
            returncode = 0; stdout = ""; stderr = ""
        return _R()

    out = window.run_upgrade_sweep(state_dir=tmp_path, dry_run=True, runner=fake_runner)
    assert out == {"summary": {}}


def test_run_upgrade_sweep_missing_script_returns_none(tmp_path, monkeypatch):
    """Script missing is a FAILURE to run — must return None, not {}, so it's
    never confused with a clean sweep that found zero upgrades (2026-07-29)."""
    from lib import window
    monkeypatch.setattr(window, "_upgrade_script_path", lambda: tmp_path / "nope.sh")
    called = []
    out = window.run_upgrade_sweep(
        state_dir=tmp_path,
        runner=lambda *a, **k: called.append(1),
    )
    assert out is None
    assert called == [], "runner must not be invoked when the script is missing"


def test_run_upgrade_sweep_runner_raises_returns_none(tmp_path):
    """A runner that raises (crash) must not propagate — but must return None
    (not {}) so a crash is distinguishable from a clean zero-upgrade run."""
    from lib import window

    def boom(*a, **k):
        raise RuntimeError("bash exploded")

    out = window.run_upgrade_sweep(state_dir=tmp_path, runner=boom)
    assert out is None


def test_run_upgrade_sweep_timeout_returns_none(tmp_path):
    """subprocess.TimeoutExpired (or any runner timeout) is the same failure
    class as a crash — must return None, not {}."""
    from lib import window
    import subprocess as _sp

    def times_out(*a, **k):
        raise _sp.TimeoutExpired(cmd="bash", timeout=1)

    out = window.run_upgrade_sweep(state_dir=tmp_path, runner=times_out)
    assert out is None


def test_run_upgrade_sweep_unreadable_results_returns_none(tmp_path):
    """The script runs (runner doesn't raise) but never writes a results file
    — e.g. it crashed internally after the subprocess call returned. Reading
    last-upgrade.json must fail, and that must return None, not {}."""
    from lib import window

    def fake_runner(cmd, env=None, timeout=None, capture_output=False, text=False):
        # Deliberately does NOT write MANITOBA_UPGRADE_RESULTS.
        class _R:
            returncode = 1; stdout = ""; stderr = "internal script failure"
        return _R()

    out = window.run_upgrade_sweep(state_dir=tmp_path, runner=fake_runner)
    assert out is None


def test_run_upgrade_sweep_malformed_results_returns_none(tmp_path):
    """A results file that exists but isn't valid JSON is unreadable —
    must return None, not {}."""
    from lib import window

    def fake_runner(cmd, env=None, timeout=None, capture_output=False, text=False):
        Path(env["MANITOBA_UPGRADE_RESULTS"]).write_text("{not json", encoding="utf-8")
        class _R:
            returncode = 0; stdout = ""; stderr = ""
        return _R()

    out = window.run_upgrade_sweep(state_dir=tmp_path, runner=fake_runner)
    assert out is None


def test_run_upgrade_sweep_clean_zero_returns_dict_not_none(tmp_path):
    """A sweep that ran fine and genuinely found zero upgrades must return the
    parsed dict (truthy-summary-or-not), never None — None means the run
    itself is untrustworthy, which is not the case here."""
    from lib import window
    clean_zero = {"schema_version": 1, "summary": {"upgraded": 0, "failed": 0, "bailed": 0}}

    def fake_runner(cmd, env=None, timeout=None, capture_output=False, text=False):
        Path(env["MANITOBA_UPGRADE_RESULTS"]).write_text(json.dumps(clean_zero), encoding="utf-8")
        class _R:
            returncode = 0; stdout = ""; stderr = ""
        return _R()

    out = window.run_upgrade_sweep(state_dir=tmp_path, runner=fake_runner)
    assert out == clean_zero
    assert out is not None


def test_run_records_upgrade_results_and_notes(tmp_path):
    """run() stores the sweep results on the summary and notes the counts."""
    manifest = _simple_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")
    sweep = {"summary": {"upgraded": 3, "failed": 0, "bailed": 0}, "upgraded": ["a", "b", "c"]}

    with patch("lib.window.notify.notify"), \
         patch("lib.window.listmonk.fire_template_campaign", return_value=True), \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.kuma.monitors_status", return_value={}), \
         patch("lib.window.deep_check.run_deep_check"), \
         patch("lib.window.run_upgrade_sweep", return_value=sweep):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        summary = w.run()

    assert summary.upgrade_results == sweep
    assert any("upgrade sweep" in n.lower() for n in summary.notes)


def test_run_dry_run_skips_upgrade_sweep(tmp_path):
    manifest = _simple_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")

    with patch("lib.window.notify.notify"), \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.run_upgrade_sweep") as mock_sweep:
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest, dry_run=True)
        summary = w.run()

    mock_sweep.assert_not_called()
    assert any("upgrade sweep skipped" in n.lower() for n in summary.notes)


def test_run_upgrade_sweep_exception_does_not_fail_run(tmp_path):
    """A raising run_upgrade_sweep must not propagate out of run()."""
    manifest = _simple_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")

    with patch("lib.window.notify.notify"), \
         patch("lib.window.listmonk.fire_template_campaign", return_value=True), \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.kuma.monitors_status", return_value={}), \
         patch("lib.window.deep_check.run_deep_check"), \
         patch("lib.window.run_upgrade_sweep", side_effect=RuntimeError("boom")):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        summary = w.run()  # must not raise

    assert summary is not None


# ---------------------------------------------------------------------------
# 2026-07-29 fix: a failed sweep must be distinguishable from a clean zero
# ---------------------------------------------------------------------------

def test_upgrade_sweep_records_failure_when_run_returns_none(tmp_path):
    """upgrade_sweep() must set upgrade_sweep_failed=True (and note it) when
    run_upgrade_sweep() reports None (crash/timeout/unreadable results) —
    this must NOT look like a clean run that found zero upgrades."""
    manifest = _simple_manifest()

    with patch("lib.window.run_upgrade_sweep", return_value=None):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        w.upgrade_sweep()
        summary = w.close()

    assert summary.upgrade_sweep_failed is True
    assert summary.upgrade_results == {}
    assert any("failed" in n.lower() for n in summary.notes)


def test_upgrade_sweep_clean_zero_does_not_set_failed_flag(tmp_path):
    """A sweep that ran cleanly and found zero upgrades must leave
    upgrade_sweep_failed False — only a genuine failure sets it."""
    manifest = _simple_manifest()
    clean_zero = {"summary": {"upgraded": 0, "failed": 0, "bailed": 0}}

    with patch("lib.window.run_upgrade_sweep", return_value=clean_zero):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        w.upgrade_sweep()
        summary = w.close()

    assert summary.upgrade_sweep_failed is False
    assert summary.upgrade_results == clean_zero


def test_window_log_line_marks_upgrade_sweep_failed(tmp_path):
    """Design law: every suppression goes to the audit log. A failed sweep
    must show up in the window-log line, not just vanish."""
    manifest = _simple_manifest()

    with patch("lib.window.run_upgrade_sweep", return_value=None):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        w.upgrade_sweep()
        w.close()

    log_dir = tmp_path / "window-log"
    log_content = next(log_dir.iterdir()).read_text(encoding="utf-8")
    assert "upgrade_sweep=FAILED" in log_content


def test_window_log_line_unchanged_shape_on_clean_run(tmp_path):
    """Happy-path window-log line format must be unchanged (no downstream
    parser breakage) — no upgrade_sweep segment appears at all."""
    manifest = _simple_manifest()
    clean_zero = {"summary": {"upgraded": 0, "failed": 0, "bailed": 0}}

    with patch("lib.window.run_upgrade_sweep", return_value=clean_zero):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        w.open()
        w.upgrade_sweep()
        w.close()

    log_dir = tmp_path / "window-log"
    log_content = next(log_dir.iterdir()).read_text(encoding="utf-8")
    assert "upgrade_sweep=FAILED" not in log_content


def test_run_notify_message_says_upgrade_sweep_failed(tmp_path):
    """The window-close Discord message must say the sweep failed explicitly,
    instead of silently omitting any upgrade mention (the original bug)."""
    manifest = _simple_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")

    with patch("lib.window.notify.notify") as mock_notify, \
         patch("lib.window.listmonk.fire_template_campaign", return_value=True), \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.kuma.monitors_status", return_value={}), \
         patch("lib.window.deep_check.run_deep_check"), \
         patch("lib.window.run_upgrade_sweep", return_value=None):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        summary = w.run()

    assert summary.upgrade_sweep_failed is True
    post_msg = mock_notify.call_args_list[1].args[0]
    assert "upgrade sweep FAILED" in post_msg


def test_run_notify_message_happy_path_format_unchanged(tmp_path):
    """A successful sweep with upgrades still uses the original
    ' · upgraded N' segment — the fix must not touch the happy path."""
    manifest = _simple_manifest()
    ok_health = HealthResult(ok=True, latency_ms=5, reason="ok")
    sweep = {"summary": {"upgraded": 2, "failed": 0, "bailed": 0}}

    with patch("lib.window.notify.notify") as mock_notify, \
         patch("lib.window.listmonk.fire_template_campaign", return_value=True), \
         patch("lib.window.health.probe", return_value=ok_health), \
         patch("lib.window.kuma.monitors_status", return_value={}), \
         patch("lib.window.deep_check.run_deep_check"), \
         patch("lib.window.run_upgrade_sweep", return_value=sweep):
        w = WindowOrchestrator(state_dir=tmp_path, manifest=manifest)
        summary = w.run()

    assert summary.upgrade_sweep_failed is False
    post_msg = mock_notify.call_args_list[1].args[0]
    assert "· upgraded 2" in post_msg
    assert "FAILED" not in post_msg
