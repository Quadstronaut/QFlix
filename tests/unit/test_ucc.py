"""tests/unit/test_ucc.py — TDD tests for lib/ucc.py.

All probe subprocess calls are mocked — no real SSH, no network.
conftest.py puts scripts/maint on sys.path so `from lib.ucc import ...` resolves.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from lib import ucc as ucc_mod
from lib.ucc import (
    classify,
    probe,
    detect,
    read_state,
    write_state,
    UCC_CLEAR_DEBOUNCE,
)


# ---------------------------------------------------------------------------
# Classification tests (pure, no filesystem)
# ---------------------------------------------------------------------------

GATED_OUTPUT = json.dumps({
    "data": {"message": "The 'start' operation is no longer available due to maintenance."},
    "result": False,
})

CLEAR_OUTPUT_TRUE = json.dumps({"result": True, "data": {"message": "Application started."}})
CLEAR_OUTPUT_RUNNING = json.dumps({"result": True, "data": {"message": "Application is already running."}})
CLEAR_OUTPUT_STARTED_OK = json.dumps({"result": True, "data": {"message": "started"}})

# Non-JSON output — classify as probe-error
ERROR_OUTPUT_NOJSON = "SSH connection refused"
# Unknown app response — should be probe-error, not clear
ERROR_OUTPUT_UNKNOWN_APP = "Unknown application: doesnotexist"
ERROR_OUTPUT_NOT_INSTALLED = "Application is not installed: fake-app"


class TestClassify:
    def test_gated_maintenance_message(self):
        assert classify(GATED_OUTPUT) == "gated"

    def test_gated_case_insensitive(self):
        out = json.dumps({"result": False, "data": {"message": "no longer available due to Maintenance."}})
        assert classify(out) == "gated"

    def test_gated_result_false_maintenance_text(self):
        # result==false AND maintenance substring
        out = json.dumps({"result": False, "data": {"message": "due to maintenance now"}})
        assert classify(out) == "gated"

    def test_result_false_without_maintenance_is_probe_error(self):
        # result==false but NOT maintenance message — treat as probe-error (unknown state)
        out = json.dumps({"result": False, "data": {"message": "Something unknown went wrong."}})
        assert classify(out) == "probe-error"

    def test_clear_result_true(self):
        assert classify(CLEAR_OUTPUT_TRUE) == "clear"

    def test_clear_result_already_running(self):
        assert classify(CLEAR_OUTPUT_RUNNING) == "clear"

    def test_clear_result_started_ok(self):
        assert classify(CLEAR_OUTPUT_STARTED_OK) == "clear"

    def test_probe_error_non_json(self):
        assert classify(ERROR_OUTPUT_NOJSON) == "probe-error"

    def test_probe_error_unknown_app(self):
        assert classify(ERROR_OUTPUT_UNKNOWN_APP) == "probe-error"

    def test_probe_error_not_installed(self):
        assert classify(ERROR_OUTPUT_NOT_INSTALLED) == "probe-error"

    def test_empty_output_rc0_is_clear(self):
        # app-manager >=2026.05.22 is silent on a successful write-op:
        # empty stdout + rc 0 = command accepted = NOT gated = clear.
        assert classify("", 0) == "clear"

    def test_empty_output_nonzero_rc_is_probe_error(self):
        # Empty stdout with a failure rc is a genuine error, not success.
        assert classify("", 1) == "probe-error"

    def test_whitespace_only_rc0_is_clear(self):
        assert classify("  \n ", 0) == "clear"

    def test_probe_error_partial_json(self):
        assert classify('{"result":') == "probe-error"


# ---------------------------------------------------------------------------
# probe() — subprocess mocking
# ---------------------------------------------------------------------------

class TestProbe:
    """probe() runs subprocess and returns (classification, probe_op, raw_output)."""

    def _run_probe(self, stdout_text, returncode=0, side_effect=None, probe_app="kavita"):
        cp = MagicMock()
        cp.stdout = stdout_text
        cp.returncode = returncode
        if side_effect:
            with patch("subprocess.run", side_effect=side_effect) as mock_run:
                result = probe(probe_app=probe_app)
        else:
            with patch("subprocess.run", return_value=cp) as mock_run:
                result = probe(probe_app=probe_app)
        return result

    def test_probe_gated(self):
        cp = MagicMock()
        cp.stdout = GATED_OUTPUT
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            classification, probe_op, raw = probe(probe_app="kavita")
        assert classification == "gated"
        assert "kavita" in probe_op
        assert "start" in probe_op

    def test_probe_clear(self):
        cp = MagicMock()
        cp.stdout = CLEAR_OUTPUT_TRUE
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            classification, probe_op, raw = probe(probe_app="kavita")
        assert classification == "clear"

    def test_probe_empty_stdout_rc0_is_clear(self):
        # Regression: app-manager v2026.05.22 returns empty stdout on a
        # successful `start`. probe() must read that as clear, not probe-error.
        cp = MagicMock()
        cp.stdout = ""
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            classification, probe_op, raw = probe(probe_app="kavita")
        assert classification == "clear"

    def test_probe_empty_stdout_nonzero_rc_is_probe_error(self):
        cp = MagicMock()
        cp.stdout = ""
        cp.returncode = 1
        with patch("subprocess.run", return_value=cp):
            classification, probe_op, raw = probe(probe_app="kavita")
        assert classification == "probe-error"

    def test_probe_timeout_is_probe_error(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="app-kavita start", timeout=15)):
            classification, probe_op, raw = probe(probe_app="kavita")
        assert classification == "probe-error"

    def test_probe_oserror_is_probe_error(self):
        with patch("subprocess.run", side_effect=OSError("command not found")):
            classification, probe_op, raw = probe(probe_app="kavita")
        assert classification == "probe-error"

    def test_probe_uses_probe_app_from_secret(self):
        """If secrets/ucc.probe_app exists, probe should use that app name."""
        cp = MagicMock()
        cp.stdout = CLEAR_OUTPUT_TRUE
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp) as mock_run, \
             patch("lib.secrets.read_secret", return_value="myapp"):
            classification, probe_op, raw = probe()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "myapp" in cmd or any("myapp" in str(a) for a in cmd)

    def test_probe_falls_back_to_kavita_when_no_secret(self):
        cp = MagicMock()
        cp.stdout = CLEAR_OUTPUT_TRUE
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp) as mock_run, \
             patch("lib.secrets.read_secret", side_effect=FileNotFoundError("no secret")):
            classification, probe_op, raw = probe()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "kavita" in cmd or any("kavita" in str(a) for a in cmd)


# ---------------------------------------------------------------------------
# State file read/write
# ---------------------------------------------------------------------------

class TestStateReadWrite:
    def test_write_and_read_roundtrip(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        data = {
            "active": True,
            "first_detected_at": "2026-05-24T21:30:00Z",
            "last_confirmed_at": "2026-05-24T23:55:00Z",
            "last_probe_at": "2026-05-24T23:55:00Z",
            "last_probe_result": "gated",
            "probe_op": "app-kavita start",
            "consecutive_clear": 0,
            "consecutive_error": 0,
        }
        write_state(state_path, data)
        result = read_state(state_path)
        assert result == data

    def test_read_missing_file_returns_empty(self, tmp_path):
        state_path = tmp_path / "nonexistent.json"
        result = read_state(state_path)
        assert result == {}

    def test_read_corrupt_file_returns_empty_no_crash(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        state_path.write_text("this is not valid json {{{}}}}", encoding="utf-8")
        result = read_state(state_path)
        assert result == {}

    def test_write_is_atomic(self, tmp_path):
        """Atomic write leaves no .tmp file behind."""
        state_path = tmp_path / "ucc-window.json"
        write_state(state_path, {"active": False, "consecutive_clear": 0, "consecutive_error": 0})
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_read_non_dict_json_returns_empty(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        state_path.write_text("[1, 2, 3]", encoding="utf-8")
        result = read_state(state_path)
        assert result == {}


# ---------------------------------------------------------------------------
# State machine: detect() — the core transition logic
# ---------------------------------------------------------------------------

class TestStateMachine:
    """detect() runs one probe cycle and updates state.
    Returns the new state dict.
    All notify and log side-effects are best-effort mocked.
    """

    def _detect(self, stdout_text, initial_state, state_path, returncode=0, side_effect=None):
        """Helper: write initial_state, run detect(), return new state."""
        write_state(state_path, initial_state)
        cp = MagicMock()
        cp.stdout = stdout_text
        cp.returncode = returncode
        with patch("subprocess.run", return_value=cp) if not side_effect \
                else patch("subprocess.run", side_effect=side_effect):
            with patch("lib.notify.notify", return_value=True) as mock_notify:
                new_state = detect(state_path=state_path, probe_app="kavita")
        return new_state, mock_notify

    # --- clear → active (single gated probe triggers immediately) ---

    def test_clear_to_active_on_single_gated_probe(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        initial = {"active": False, "consecutive_clear": 0, "consecutive_error": 0}
        new_state, mock_notify = self._detect(GATED_OUTPUT, initial, state_path)

        assert new_state["active"] is True
        assert new_state["last_probe_result"] == "gated"
        assert new_state["consecutive_clear"] == 0
        # Transition edge → notify called
        mock_notify.assert_called_once()

    def test_active_set_first_detected_at_on_first_flip(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        initial = {"active": False, "consecutive_clear": 0, "consecutive_error": 0}
        new_state, _ = self._detect(GATED_OUTPUT, initial, state_path)
        assert "first_detected_at" in new_state
        assert new_state["first_detected_at"] is not None

    # --- active → clear (requires UCC_CLEAR_DEBOUNCE consecutive clears) ---

    def test_active_clear_requires_debounce_count(self, tmp_path):
        """N-1 consecutive clears must NOT flip state."""
        state_path = tmp_path / "ucc-window.json"
        initial = {
            "active": True,
            "first_detected_at": "2026-05-24T21:30:00Z",
            "last_confirmed_at": "2026-05-24T23:55:00Z",
            "consecutive_clear": 0,
            "consecutive_error": 0,
        }
        # Feed UCC_CLEAR_DEBOUNCE-1 clear probes; state should remain active
        for i in range(UCC_CLEAR_DEBOUNCE - 1):
            write_state(state_path, {**initial, "consecutive_clear": i})
            cp = MagicMock()
            cp.stdout = CLEAR_OUTPUT_TRUE
            cp.returncode = 0
            with patch("subprocess.run", return_value=cp):
                with patch("lib.notify.notify", return_value=True):
                    state = detect(state_path=state_path, probe_app="kavita")
            assert state["active"] is True, f"Should still be active after {i+1} clear(s)"

    def test_active_to_clear_after_debounce(self, tmp_path):
        """Exactly UCC_CLEAR_DEBOUNCE consecutive clears flip active → False."""
        state_path = tmp_path / "ucc-window.json"
        initial = {
            "active": True,
            "first_detected_at": "2026-05-24T21:30:00Z",
            "last_confirmed_at": "2026-05-24T23:55:00Z",
            "consecutive_clear": UCC_CLEAR_DEBOUNCE - 1,  # one short of flip
            "consecutive_error": 0,
        }
        write_state(state_path, initial)
        cp = MagicMock()
        cp.stdout = CLEAR_OUTPUT_TRUE
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            with patch("lib.notify.notify", return_value=True) as mock_notify:
                state = detect(state_path=state_path, probe_app="kavita")
        assert state["active"] is False
        # Counter resets to 0 after the flip (matches test_clear_resets_consecutive_clear_counter).
        assert state["consecutive_clear"] == 0
        mock_notify.assert_called_once()

    def test_clear_resets_consecutive_clear_counter(self, tmp_path):
        """After active→clear flip, consecutive_clear should reset to 0."""
        state_path = tmp_path / "ucc-window.json"
        initial = {
            "active": True,
            "first_detected_at": "2026-05-24T21:30:00Z",
            "last_confirmed_at": "2026-05-24T23:55:00Z",
            "consecutive_clear": UCC_CLEAR_DEBOUNCE - 1,
            "consecutive_error": 0,
        }
        write_state(state_path, initial)
        cp = MagicMock()
        cp.stdout = CLEAR_OUTPUT_TRUE
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            with patch("lib.notify.notify", return_value=True):
                state = detect(state_path=state_path, probe_app="kavita")
        # After flip, counter resets
        assert state["consecutive_clear"] == 0

    def test_active_clears_via_empty_stdout_silent_success(self, tmp_path):
        """Regression for the 2026-05-25 stuck-gate bug: after maintenance,
        `app-X start` returns empty stdout + rc 0 (silent success). The gate
        must accumulate these as clears and flip active→clear after debounce,
        instead of treating them as probe-error forever."""
        state_path = tmp_path / "ucc-window.json"
        initial = {
            "active": True,
            "first_detected_at": "2026-05-25T02:38:00Z",
            "last_confirmed_at": "2026-05-25T12:01:00Z",
            "consecutive_clear": 0,
            "consecutive_error": 128,  # the real-world stuck count
        }
        write_state(state_path, initial)
        state = initial
        for i in range(UCC_CLEAR_DEBOUNCE):
            cp = MagicMock()
            cp.stdout = ""          # silent success
            cp.returncode = 0
            with patch("subprocess.run", return_value=cp):
                with patch("lib.notify.notify", return_value=True):
                    state = detect(state_path=state_path, probe_app="kavita")
            # error counter must reset on the first clear
            assert state["consecutive_error"] == 0
        assert state["active"] is False, "gate must clear after debounce of silent-success probes"

    # --- probe-error holds state, increments consecutive_error ---

    def test_probe_error_holds_state_when_active(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        initial = {
            "active": True,
            "first_detected_at": "2026-05-24T21:30:00Z",
            "last_confirmed_at": "2026-05-24T23:55:00Z",
            "consecutive_clear": 1,
            "consecutive_error": 0,
        }
        write_state(state_path, initial)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 15)):
            with patch("lib.notify.notify", return_value=True) as mock_notify:
                state = detect(state_path=state_path, probe_app="kavita")
        assert state["active"] is True
        assert state["consecutive_error"] == 1
        # No edge transition → notify NOT called
        mock_notify.assert_not_called()

    def test_probe_error_holds_state_when_inactive(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        initial = {"active": False, "consecutive_clear": 2, "consecutive_error": 0}
        write_state(state_path, initial)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 15)):
            with patch("lib.notify.notify", return_value=True) as mock_notify:
                state = detect(state_path=state_path, probe_app="kavita")
        assert state["active"] is False
        assert state["consecutive_error"] == 1
        mock_notify.assert_not_called()

    def test_probe_error_does_not_reset_consecutive_clear(self, tmp_path):
        """A probe-error must NOT reset the consecutive_clear counter."""
        state_path = tmp_path / "ucc-window.json"
        initial = {
            "active": True,
            "first_detected_at": "2026-05-24T21:30:00Z",
            "last_confirmed_at": "2026-05-24T23:55:00Z",
            "consecutive_clear": 2,  # in progress toward debounce
            "consecutive_error": 0,
        }
        write_state(state_path, initial)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 15)):
            with patch("lib.notify.notify", return_value=True):
                state = detect(state_path=state_path, probe_app="kavita")
        assert state["consecutive_clear"] == 2  # unchanged

    def test_gated_resets_consecutive_clear_counter(self, tmp_path):
        """A gated probe while active should reset consecutive_clear (still gated)."""
        state_path = tmp_path / "ucc-window.json"
        initial = {
            "active": True,
            "first_detected_at": "2026-05-24T21:30:00Z",
            "last_confirmed_at": "2026-05-24T23:55:00Z",
            "consecutive_clear": 2,
            "consecutive_error": 0,
        }
        write_state(state_path, initial)
        cp = MagicMock()
        cp.stdout = GATED_OUTPUT
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            with patch("lib.notify.notify", return_value=True) as mock_notify:
                state = detect(state_path=state_path, probe_app="kavita")
        assert state["active"] is True
        assert state["consecutive_clear"] == 0
        # No edge (still active) → no notify
        mock_notify.assert_not_called()

    # --- corrupt/missing state file → fresh start ---

    def test_corrupt_state_file_fresh_start_no_crash(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        state_path.write_text("not json at all!", encoding="utf-8")
        cp = MagicMock()
        cp.stdout = GATED_OUTPUT
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            with patch("lib.notify.notify", return_value=True):
                # Must not crash
                state = detect(state_path=state_path, probe_app="kavita")
        assert isinstance(state, dict)
        assert state["active"] is True

    def test_missing_state_file_fresh_start(self, tmp_path):
        state_path = tmp_path / "missing.json"
        cp = MagicMock()
        cp.stdout = CLEAR_OUTPUT_TRUE
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            with patch("lib.notify.notify", return_value=True):
                state = detect(state_path=state_path, probe_app="kavita")
        assert isinstance(state, dict)
        assert state["active"] is False

    # --- transitions log ---

    def test_edge_appends_transition_log(self, tmp_path, monkeypatch):
        """clear→active edge must append a record to the transitions log."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        state_path = tmp_path / "ucc-window.json"
        initial = {"active": False, "consecutive_clear": 0, "consecutive_error": 0}
        write_state(state_path, initial)
        cp = MagicMock()
        cp.stdout = GATED_OUTPUT
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            with patch("lib.notify.notify", return_value=True):
                detect(state_path=state_path, probe_app="kavita")
        log_path = tmp_path / "ucc-transitions.jsonl"
        assert log_path.exists(), "Transitions log must be created on edge"
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert "timestamp" in record
        assert "from" in record and "to" in record
        assert record["from"] == "clear"
        assert record["to"] == "active"
        assert "probe_op" in record

    def test_no_edge_no_transition_log_entry(self, tmp_path, monkeypatch):
        """No transition → no log entry appended."""
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        state_path = tmp_path / "ucc-window.json"
        initial = {"active": False, "consecutive_clear": 0, "consecutive_error": 0}
        write_state(state_path, initial)
        cp = MagicMock()
        cp.stdout = CLEAR_OUTPUT_TRUE  # stays clear, no edge
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            with patch("lib.notify.notify", return_value=True):
                detect(state_path=state_path, probe_app="kavita")
        log_path = tmp_path / "ucc-transitions.jsonl"
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8").strip()
            assert content == "", "No edge → no transition log entries"

    # --- notify is best-effort (failure must not abort state write) ---

    def test_notify_failure_does_not_abort_state_write(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        initial = {"active": False, "consecutive_clear": 0, "consecutive_error": 0}
        write_state(state_path, initial)
        cp = MagicMock()
        cp.stdout = GATED_OUTPUT
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            with patch("lib.notify.notify", side_effect=Exception("discord exploded")):
                # Must not crash even if notify raises
                state = detect(state_path=state_path, probe_app="kavita")
        # State was still written
        on_disk = read_state(state_path)
        assert on_disk["active"] is True

    # --- state fields ---

    def test_last_probe_at_is_updated(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        initial = {"active": False, "consecutive_clear": 0, "consecutive_error": 0}
        write_state(state_path, initial)
        cp = MagicMock()
        cp.stdout = CLEAR_OUTPUT_TRUE
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            with patch("lib.notify.notify", return_value=True):
                state = detect(state_path=state_path, probe_app="kavita")
        assert "last_probe_at" in state
        assert state["last_probe_at"].endswith("Z")

    def test_last_confirmed_at_updated_when_gated(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        initial = {
            "active": True,
            "first_detected_at": "2026-05-24T21:30:00Z",
            "last_confirmed_at": "2026-05-24T22:00:00Z",
            "consecutive_clear": 0,
            "consecutive_error": 0,
        }
        write_state(state_path, initial)
        cp = MagicMock()
        cp.stdout = GATED_OUTPUT
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            with patch("lib.notify.notify", return_value=True):
                state = detect(state_path=state_path, probe_app="kavita")
        # last_confirmed_at should be updated on a gated probe while active
        assert state["last_confirmed_at"] != "2026-05-24T22:00:00Z"

    def test_probe_op_stored_in_state(self, tmp_path):
        state_path = tmp_path / "ucc-window.json"
        initial = {"active": False, "consecutive_clear": 0, "consecutive_error": 0}
        write_state(state_path, initial)
        cp = MagicMock()
        cp.stdout = CLEAR_OUTPUT_TRUE
        cp.returncode = 0
        with patch("subprocess.run", return_value=cp):
            with patch("lib.notify.notify", return_value=True):
                state = detect(state_path=state_path, probe_app="kavita")
        assert "probe_op" in state
        assert "kavita" in state["probe_op"]
