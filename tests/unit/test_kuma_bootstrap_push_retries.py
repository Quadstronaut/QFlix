"""QFLX-104: push monitors must tolerate one Kuma stall without paging.

WHY: Kuma's clear-old-data job (cron `14 03 * * *`) blocks /api/push ~70s every
night. Every live PUSH monitor had maxretries=0, so one missed 90s window was a
DOWN + Discord page, and ~32 monitors paged together on each of 2026-11-01..07.
The fix is two-sided and both halves are pinned here:
  1. new monitors are CREATED with the retry tolerance;
  2. EXISTING monitors are RECONCILED to it on every bootstrap run -- a value
     applied only at creation would have left all 85 live monitors unfixed.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "scripts" / "maint" / "bootstrap-kuma-monitors.py"
SRC = BOOTSTRAP.read_text(encoding="utf-8")
MAIN = SRC[SRC.index("def main("):]


@pytest.fixture(scope="module")
def mod():
    sys.path.insert(0, str(REPO / "scripts" / "maint"))
    spec = importlib.util.spec_from_file_location("kuma_bootstrap_retries", BOOTSTRAP)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_budget_covers_observed_silence(mod):
    """DOWN after interval + retries*retryInterval must exceed the worst
    observed silence (~70s stall + ~65s pusher cadence = ~140s) with margin."""
    budget = mod.APP_HEARTBEAT_S + mod.PUSH_MAXRETRIES * mod.PUSH_RETRY_INTERVAL_S
    assert budget >= 180, budget


def test_create_uses_retry_tolerance(mod):
    api = MagicMock()
    api._build_monitor_data.return_value = {}
    api.get_monitors.return_value = []
    import uptime_kuma_api.api as kapi
    orig = (kapi._convert_monitor_input, kapi._check_arguments_monitor)
    kapi._convert_monitor_input = lambda d: None
    kapi._check_arguments_monitor = lambda d: None
    try:
        mod._add_push_monitor(api, "X")
    finally:
        kapi._convert_monitor_input, kapi._check_arguments_monitor = orig
    kw = api._build_monitor_data.call_args.kwargs
    assert kw["maxretries"] == mod.PUSH_MAXRETRIES
    assert kw["retryInterval"] == mod.PUSH_RETRY_INTERVAL_S


def test_reconcile_edits_only_drifted_managed_push_monitors(mod):
    api = MagicMock()
    want = (mod.PUSH_MAXRETRIES, mod.PUSH_RETRY_INTERVAL_S)
    api.get_monitors.return_value = [
        {"id": 1, "name": "Sonarr", "type": "push", "maxretries": 0, "retryInterval": 60},
        {"id": 2, "name": "Radarr", "type": "MonitorType.PUSH",
         "maxretries": want[0], "retryInterval": want[1]},
        {"id": 3, "name": "Node1", "type": "http", "maxretries": 0, "retryInterval": 60},
        {"id": 4, "name": "QFlix Collect (seedbox)", "type": "push",
         "maxretries": 0, "retryInterval": 60},
    ]
    n = mod._ensure_push_retries(api, skip_names={"QFlix Collect (seedbox)"})
    assert n == 1
    api.edit_monitor.assert_called_once_with(
        1, maxretries=want[0], retryInterval=want[1])


def test_reconcile_failure_is_reported_not_raised(mod, capsys):
    api = MagicMock()
    api.get_monitors.return_value = [
        {"id": 1, "name": "Sonarr", "type": "push", "maxretries": 0, "retryInterval": 60}]
    api.edit_monitor.side_effect = RuntimeError("socket closed")
    assert mod._ensure_push_retries(api) == 0
    assert "edit-FAIL" in capsys.readouterr().out


def test_main_runs_the_reconcile(mod):
    """Structural pin: main() must call the reconcile, or existing monitors
    keep maxretries=0 forever (the creation-only trap QFLX-81 hit for
    heartbeat windows)."""
    assert "_ensure_push_retries(api" in MAIN
