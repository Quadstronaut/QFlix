"""tests/unit/test_ucc_response.py — TDD tests for lib/ucc_response.py.

Covers:
  - Both edges (clear→active and active→clear) fire the right side-effects.
  - No-op when state matches cursor (idempotent).
  - A failing side-effect doesn't block others or the cursor write.
  - The active→clear edge calls deep_check.run_deep_check(reason="ucc-clear")
    via sys.modules injection.
  - Cursor is written after effects even if one effect fails.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state(active: bool) -> dict:
    return {"active": active, "last_probe_result": "gated" if active else "clear"}


def _read_cursor(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Edge detection
# ---------------------------------------------------------------------------

class TestRespond:

    def test_clear_to_active_edge_detected(self, tmp_path):
        """Cursor absent (False) + state active=True → clear_to_active edge."""
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"

        with patch("lib.ucc_response._fire_clear_to_active") as mock_c2a, \
             patch("lib.ucc_response._fire_active_to_clear") as mock_a2c:
            result = ucc_response.respond(_state(True), response_state_path=cursor_path)

        assert result["edge"] == "clear_to_active"
        mock_c2a.assert_called_once()
        mock_a2c.assert_not_called()

    def test_active_to_clear_edge_detected(self, tmp_path):
        """Cursor active=True + state active=False → active_to_clear edge."""
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"
        cursor_path.write_text(json.dumps({"active": True}))

        with patch("lib.ucc_response._fire_clear_to_active") as mock_c2a, \
             patch("lib.ucc_response._fire_active_to_clear") as mock_a2c:
            result = ucc_response.respond(_state(False), response_state_path=cursor_path)

        assert result["edge"] == "active_to_clear"
        mock_a2c.assert_called_once()
        mock_c2a.assert_not_called()

    def test_no_op_when_already_active(self, tmp_path):
        """Cursor active=True + state active=True → no-op."""
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"
        cursor_path.write_text(json.dumps({"active": True}))

        with patch("lib.ucc_response._fire_clear_to_active") as mock_c2a, \
             patch("lib.ucc_response._fire_active_to_clear") as mock_a2c:
            result = ucc_response.respond(_state(True), response_state_path=cursor_path)

        assert result["edge"] == "none"
        mock_c2a.assert_not_called()
        mock_a2c.assert_not_called()

    def test_no_op_when_already_inactive(self, tmp_path):
        """Cursor absent (False) + state active=False → no-op."""
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"

        with patch("lib.ucc_response._fire_clear_to_active") as mock_c2a, \
             patch("lib.ucc_response._fire_active_to_clear") as mock_a2c:
            result = ucc_response.respond(_state(False), response_state_path=cursor_path)

        assert result["edge"] == "none"
        mock_c2a.assert_not_called()
        mock_a2c.assert_not_called()

    def test_idempotent_repeated_active(self, tmp_path):
        """Calling respond twice with active=True only fires clear_to_active once."""
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"

        calls = []
        def record_call():
            calls.append(1)

        with patch("lib.ucc_response._fire_clear_to_active", side_effect=record_call), \
             patch("lib.ucc_response._fire_active_to_clear"):
            ucc_response.respond(_state(True), response_state_path=cursor_path)
            ucc_response.respond(_state(True), response_state_path=cursor_path)

        assert len(calls) == 1  # second call is a no-op

    def test_cursor_written_after_effects(self, tmp_path):
        """Cursor file is written with the new active value."""
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"

        with patch("lib.ucc_response._fire_clear_to_active"), \
             patch("lib.ucc_response._fire_active_to_clear"):
            ucc_response.respond(_state(True), response_state_path=cursor_path)

        data = _read_cursor(cursor_path)
        assert data["active"] is True

    def test_cursor_written_even_if_effect_fails(self, tmp_path):
        """A raising side-effect must not abort the cursor write."""
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"

        with patch("lib.ucc_response._fire_clear_to_active",
                   side_effect=RuntimeError("email server down")), \
             patch("lib.ucc_response._fire_active_to_clear"):
            # Should not raise
            ucc_response.respond(_state(True), response_state_path=cursor_path)

        # Cursor must still be written
        data = _read_cursor(cursor_path)
        assert "active" in data


# ---------------------------------------------------------------------------
# clear→active side-effects
# ---------------------------------------------------------------------------

class TestFireClearToActive:

    def test_pins_incident(self, tmp_path):
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"

        with patch("lib.ucc_incident.pin_maintenance_incident") as mock_pin, \
             patch("lib.listmonk.fire_template_campaign"), \
             patch("lib.notify.notify"):
            ucc_response.respond(_state(True), response_state_path=cursor_path)

        mock_pin.assert_called_once()

    def test_fires_start_email(self, tmp_path):
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"

        with patch("lib.ucc_incident.pin_maintenance_incident"), \
             patch("lib.listmonk.fire_template_campaign") as mock_email, \
             patch("lib.notify.notify"):
            ucc_response.respond(_state(True), response_state_path=cursor_path)

        mock_email.assert_called_once()
        kw = mock_email.call_args.kwargs
        assert kw["template_title"] == "Upstream Maintenance Start"
        assert "maintenance" in kw["subject"].lower()

    def test_notifies_discord(self, tmp_path):
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"

        with patch("lib.ucc_incident.pin_maintenance_incident"), \
             patch("lib.listmonk.fire_template_campaign"), \
             patch("lib.notify.notify") as mock_notify:
            ucc_response.respond(_state(True), response_state_path=cursor_path)

        mock_notify.assert_called_once()
        _, kwargs = mock_notify.call_args
        assert kwargs.get("level") == "warning"

    def test_failing_pin_does_not_block_email_or_notify(self, tmp_path):
        """If pin_maintenance_incident raises, email and notify still fire."""
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"
        email_calls = []
        notify_calls = []

        with patch("lib.ucc_incident.pin_maintenance_incident",
                   side_effect=ConnectionError("kuma unreachable")), \
             patch("lib.listmonk.fire_template_campaign",
                   side_effect=lambda **kw: email_calls.append(kw) or True), \
             patch("lib.notify.notify",
                   side_effect=lambda msg, **kw: notify_calls.append(msg)):
            ucc_response.respond(_state(True), response_state_path=cursor_path)

        assert len(email_calls) == 1
        assert len(notify_calls) == 1


# ---------------------------------------------------------------------------
# active→clear side-effects
# ---------------------------------------------------------------------------

class TestFireActiveToClear:

    def _setup_cursor(self, path: Path) -> None:
        path.write_text(json.dumps({"active": True}))

    def test_unpin_incident(self, tmp_path):
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"
        self._setup_cursor(cursor_path)

        with patch("lib.ucc_incident.clear_maintenance_incident") as mock_clear, \
             patch("lib.listmonk.fire_template_campaign"), \
             patch("lib.notify.notify"), \
             patch("lib.ucc_response._trigger_deep_check"):
            ucc_response.respond(_state(False), response_state_path=cursor_path)

        mock_clear.assert_called_once()

    def test_fires_complete_email(self, tmp_path):
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"
        self._setup_cursor(cursor_path)

        with patch("lib.ucc_incident.clear_maintenance_incident"), \
             patch("lib.listmonk.fire_template_campaign") as mock_email, \
             patch("lib.notify.notify"), \
             patch("lib.ucc_response._trigger_deep_check"):
            ucc_response.respond(_state(False), response_state_path=cursor_path)

        mock_email.assert_called_once()
        kw = mock_email.call_args.kwargs
        assert kw["template_title"] == "Upstream Maintenance Complete"

    def test_notifies_discord_info(self, tmp_path):
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"
        self._setup_cursor(cursor_path)

        with patch("lib.ucc_incident.clear_maintenance_incident"), \
             patch("lib.listmonk.fire_template_campaign"), \
             patch("lib.notify.notify") as mock_notify, \
             patch("lib.ucc_response._trigger_deep_check"):
            ucc_response.respond(_state(False), response_state_path=cursor_path)

        mock_notify.assert_called_once()
        _, kwargs = mock_notify.call_args
        assert kwargs.get("level") == "info"

    def test_triggers_deep_check_on_clear(self, tmp_path):
        """B→D seam: active→clear calls deep_check.run_deep_check(reason='ucc-clear').

        D's lib.deep_check is now merged, so patch the real symbol that the
        responder's `from lib import deep_check` resolves to.
        """
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"
        self._setup_cursor(cursor_path)

        with patch("lib.ucc_incident.clear_maintenance_incident"), \
             patch("lib.listmonk.fire_template_campaign"), \
             patch("lib.notify.notify"), \
             patch("lib.deep_check.run_deep_check") as mock_dc:
            ucc_response.respond(_state(False), response_state_path=cursor_path)

        mock_dc.assert_called_once_with(reason="ucc-clear")

    def test_deep_check_absent_does_not_abort(self, tmp_path):
        """If lib.deep_check doesn't exist (D not merged), active→clear still completes."""
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"
        self._setup_cursor(cursor_path)

        # Ensure lib.deep_check is NOT in sys.modules.
        sys.modules.pop("lib.deep_check", None)

        with patch("lib.ucc_incident.clear_maintenance_incident"), \
             patch("lib.listmonk.fire_template_campaign"), \
             patch("lib.notify.notify"):
            # Should not raise even with deep_check absent.
            result = ucc_response.respond(_state(False), response_state_path=cursor_path)

        assert result["edge"] == "active_to_clear"
        data = _read_cursor(cursor_path)
        assert data["active"] is False

    def test_failing_unpin_does_not_block_email_notify_deep_check(self, tmp_path):
        """If clear_maintenance_incident raises, other effects still run."""
        from lib import ucc_response

        cursor_path = tmp_path / "ucc-response-state.json"
        self._setup_cursor(cursor_path)

        email_calls = []
        notify_calls = []
        deep_check_calls = []

        with patch("lib.ucc_incident.clear_maintenance_incident",
                   side_effect=ConnectionError("kuma unreachable")), \
             patch("lib.listmonk.fire_template_campaign",
                   side_effect=lambda **kw: email_calls.append(kw) or True), \
             patch("lib.notify.notify",
                   side_effect=lambda msg, **kw: notify_calls.append(msg)), \
             patch("lib.deep_check.run_deep_check",
                   side_effect=lambda *, reason: deep_check_calls.append(reason)):
            ucc_response.respond(_state(False), response_state_path=cursor_path)

        assert len(email_calls) == 1
        assert len(notify_calls) == 1
        assert deep_check_calls == ["ucc-clear"]
