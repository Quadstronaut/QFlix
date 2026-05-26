"""tests/unit/test_suppression.py — TDD tests for lib/suppression.py.

Matrix:
  ucc class + ucc_active True  → recovery_suppressed True
  ucc class + ucc_active False → recovery_suppressed False
  systemd class (any)          → recovery_suppressed False
  cron class (any)             → recovery_suppressed False
  ucc.status() raises          → recovery_suppressed False (fail-safe)
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from lib.manifest import App, HealthConfig


# ---------------------------------------------------------------------------
# App factory helpers
# ---------------------------------------------------------------------------

def _make_app(class_: str, name: str = "sonarr") -> App:
    return App(
        name=name,
        class_=class_,
        kuma_monitor=f"{name.capitalize()}",
        health=HealthConfig(kind="http_api", raw={}),
        defaults={},
        raw={"class": class_},
    )


# ---------------------------------------------------------------------------
# ucc_active tests
# ---------------------------------------------------------------------------

class TestUccActive:
    def test_returns_true_when_state_active(self):
        from lib import suppression
        with patch("lib.ucc.status", return_value={"active": True}):
            assert suppression.ucc_active() is True

    def test_returns_false_when_state_inactive(self):
        from lib import suppression
        with patch("lib.ucc.status", return_value={"active": False}):
            result = suppression.ucc_active()
        assert result is False

    def test_returns_false_on_read_error(self):
        """If ucc.status() raises, ucc_active must return False (fail-safe)."""
        from lib import suppression
        with patch("lib.ucc.status", side_effect=OSError("disk full")):
            result = suppression.ucc_active()
        assert result is False

    def test_returns_false_when_active_key_missing(self):
        """State dict with no 'active' key → treat as inactive."""
        from lib import suppression
        with patch("lib.ucc.status", return_value={}):
            assert suppression.ucc_active() is False


# ---------------------------------------------------------------------------
# recovery_suppressed matrix
# ---------------------------------------------------------------------------

class TestRecoverySuppressed:
    def test_ucc_class_and_active_returns_true(self):
        """ucc-class app + ucc gate active → suppress recovery."""
        from lib import suppression
        app = _make_app("ucc")
        with patch("lib.suppression.ucc_active", return_value=True):
            assert suppression.recovery_suppressed(app) is True

    def test_ucc_class_and_inactive_returns_false(self):
        """ucc-class app + gate NOT active → do not suppress."""
        from lib import suppression
        app = _make_app("ucc")
        with patch("lib.suppression.ucc_active", return_value=False):
            assert suppression.recovery_suppressed(app) is False

    def test_systemd_class_always_false(self):
        """systemd apps use systemctl --user, unaffected by UCC gate."""
        from lib import suppression
        app = _make_app("systemd", name="listmonk")
        with patch("lib.suppression.ucc_active", return_value=True):
            assert suppression.recovery_suppressed(app) is False

    def test_cron_class_always_false(self):
        """cron apps also unaffected by UCC gate."""
        from lib import suppression
        app = _make_app("cron", name="recyclarr")
        with patch("lib.suppression.ucc_active", return_value=True):
            assert suppression.recovery_suppressed(app) is False

    def test_ucc_class_state_unreadable_returns_false(self):
        """If ucc.status() raises inside ucc_active, recovery_suppressed must
        return False (fail toward normal recovery, not silent suppression)."""
        from lib import suppression
        app = _make_app("ucc")
        with patch("lib.ucc.status", side_effect=OSError("perm denied")):
            # recovery_suppressed calls ucc_active which calls ucc.status
            result = suppression.recovery_suppressed(app)
        assert result is False

    def test_other_class_returns_false(self):
        """App with an unexpected class_ value — safe default, no exception."""
        from lib import suppression
        app = _make_app("ucc")
        app.class_ = "library"  # mutate to non-ucc
        with patch("lib.suppression.ucc_active", return_value=True):
            assert suppression.recovery_suppressed(app) is False


# ---------------------------------------------------------------------------
# push_suppressed registry
# ---------------------------------------------------------------------------

class TestPushSuppressed:
    def test_returns_none_when_no_file(self, tmp_path, monkeypatch):
        from lib import suppression
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        assert suppression.push_suppressed("flaresolverr") is None

    def test_returns_reason_when_listed(self, tmp_path, monkeypatch):
        from lib import suppression
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        (tmp_path / "push-suppress.json").write_text(
            json.dumps({"flaresolverr": {"reason": "awaiting UCC ticket", "since": "2026-05-25T23:00:00Z"}}),
            encoding="utf-8",
        )
        assert suppression.push_suppressed("flaresolverr") == "awaiting UCC ticket"

    def test_unlisted_app_returns_none(self, tmp_path, monkeypatch):
        from lib import suppression
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        (tmp_path / "push-suppress.json").write_text(
            json.dumps({"flaresolverr": {"reason": "x"}}), encoding="utf-8")
        assert suppression.push_suppressed("sonarr") is None

    def test_bare_string_entry_supported(self, tmp_path, monkeypatch):
        from lib import suppression
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        (tmp_path / "push-suppress.json").write_text(
            json.dumps({"flaresolverr": "quick mute"}), encoding="utf-8")
        assert suppression.push_suppressed("flaresolverr") == "quick mute"

    def test_corrupt_file_returns_none(self, tmp_path, monkeypatch):
        from lib import suppression
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        (tmp_path / "push-suppress.json").write_text("not json {{{", encoding="utf-8")
        assert suppression.push_suppressed("flaresolverr") is None

    def test_push_suppressed_forces_recovery_suppressed_any_class(self, tmp_path, monkeypatch):
        """A push-suppressed app has recovery suppressed regardless of class /
        UCC gate state."""
        from lib import suppression
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
        (tmp_path / "push-suppress.json").write_text(
            json.dumps({"flaresolverr": {"reason": "muted"}}), encoding="utf-8")
        app = _make_app("ucc", name="flaresolverr")
        with patch("lib.suppression.ucc_active", return_value=False):
            assert suppression.recovery_suppressed(app) is True
