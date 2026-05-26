"""tests/unit/test_flaresolverr_canary.py — push-suppression honoring for the
standalone FlareSolverr restart-bot canary.

The canary runs on its own 5-min systemd timer and notifies Discord directly
(it does NOT go through the pusher). Regression guard for the gap where a
crash-looping flaresolverr kept paging "restart REFUSED" even though its Kuma
monitor was already muted in push-suppress.json: run() must short-circuit to a
clean no-op (no probe, no restart, no notify) when the app is suppressed.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# The module file has a hyphen in its name, so it can't be imported with a
# normal `import`. Load it from its path on disk.
_CANARY_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "maint" / "flaresolverr-canary.py"
)


@pytest.fixture
def canary():
    spec = importlib.util.spec_from_file_location("flaresolverr_canary", _CANARY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_suppress(state_dir: Path, payload: dict) -> None:
    (state_dir / "push-suppress.json").write_text(
        json.dumps(payload), encoding="utf-8")


class TestSuppressedRunIsNoOp:
    def test_suppressed_skips_probe_restart_notify(self, canary, tmp_path, monkeypatch):
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        _write_suppress(tmp_path, {"flaresolverr": {"reason": "awaiting UCC ticket"}})

        # Any of these being called means suppression didn't short-circuit.
        probe_root = MagicMock(return_value=(True, "ready"))
        probe_v1 = MagicMock(return_value=(True, "ok"))
        restart = MagicMock(return_value=(True, ""))
        notify = MagicMock()
        monkeypatch.setattr(canary, "_probe_root", probe_root)
        monkeypatch.setattr(canary, "_probe_v1", probe_v1)
        monkeypatch.setattr(canary, "_restart", restart)
        monkeypatch.setattr(canary, "_notify", notify)

        rc = canary.run(dry_run=False)

        assert rc == 0
        probe_root.assert_not_called()
        probe_v1.assert_not_called()
        restart.assert_not_called()
        notify.assert_not_called()

    def test_bare_string_suppress_entry_also_silences(self, canary, tmp_path, monkeypatch):
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        _write_suppress(tmp_path, {"flaresolverr": "quick mute"})
        notify = MagicMock()
        monkeypatch.setattr(canary, "_notify", notify)

        assert canary.run(dry_run=False) == 0
        notify.assert_not_called()

    def test_respects_custom_suppress_key(self, canary, tmp_path, monkeypatch):
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        _write_suppress(tmp_path, {"fs-prod": {"reason": "muted"}})
        # FS_SUPPRESS_KEY is a module-level constant read inside _suppress_reason;
        # override it on the loaded module to simulate the env-var default.
        canary.FS_SUPPRESS_KEY = "fs-prod"
        notify = MagicMock()
        monkeypatch.setattr(canary, "_notify", notify)

        assert canary.run(dry_run=False) == 0
        notify.assert_not_called()


class TestNotSuppressedProceeds:
    def test_unsuppressed_runs_probes(self, canary, tmp_path, monkeypatch):
        # No suppress file at all → must proceed to the normal probe path.
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        # SECRETS_DIR is read at module import; override it on the loaded module.
        canary.SECRETS_DIR = tmp_path
        (tmp_path / "flaresolverr.port").write_text("17011")

        probe_root = MagicMock(return_value=(True, "ready"))
        probe_v1 = MagicMock(return_value=(True, "ok"))
        notify = MagicMock()
        monkeypatch.setattr(canary, "_probe_root", probe_root)
        monkeypatch.setattr(canary, "_probe_v1", probe_v1)
        monkeypatch.setattr(canary, "_notify", notify)

        rc = canary.run(dry_run=False)

        assert rc == 0           # healthy
        probe_root.assert_called_once()
        probe_v1.assert_called_once()

    def test_corrupt_suppress_file_fails_open_to_probing(self, canary, tmp_path, monkeypatch):
        """A corrupt registry must NOT silently suppress — fail toward alerting."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        canary.SECRETS_DIR = tmp_path
        (tmp_path / "flaresolverr.port").write_text("17011")
        (tmp_path / "push-suppress.json").write_text("not json {{{", encoding="utf-8")

        probe_root = MagicMock(return_value=(True, "ready"))
        probe_v1 = MagicMock(return_value=(True, "ok"))
        monkeypatch.setattr(canary, "_probe_root", probe_root)
        monkeypatch.setattr(canary, "_probe_v1", probe_v1)

        canary.run(dry_run=False)
        probe_root.assert_called_once()
