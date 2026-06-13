"""Tests for service logging configuration (lib.cli._configure_service_logging).

WHY THIS EXISTS (2026-06-13): the long-running maint services (pusher, kuma
webhook) emit their auto-heal decisions via log.info() — "strike N/3",
"recovery=started", "[PAUSED: quiet hours]". With no handler configured,
Python's last-resort handler only emits WARNING+ to stderr, so those INFO
lines never reached journald. That is exactly why the 2026-06-12 tdarr
false-recovery bug was invisible in `journalctl` and had to be reconstructed
from notify.log. These tests pin the contract: after the service entry calls
_configure_service_logging(), INFO from a maint logger lands on stdout (which
systemd routes to journald), the level is env-overridable, and re-invocation
does not stack handlers.
"""
from __future__ import annotations

import io
import logging
import os
import sys
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "maint"))

from lib import cli


@pytest.fixture
def clean_root_logging():
    """Start each test with an empty root logger so the function's idempotency
    guard sees a clean slate, then restore the original handlers/level so we
    don't pollute pytest's own logging or other tests."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers[:] = []
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_emits_info_to_stdout(clean_root_logging, monkeypatch):
    """A bare log.info() from lib.pusher must reach stdout after configuration."""
    monkeypatch.delenv("MANITOBA_LOG_LEVEL", raising=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._configure_service_logging()
        logging.getLogger("lib.pusher").info("strike 2/3 for tdarr-node")
    out = buf.getvalue()
    assert "strike 2/3 for tdarr-node" in out
    assert "INFO" in out


def test_log_level_env_override_suppresses_info(clean_root_logging, monkeypatch):
    """MANITOBA_LOG_LEVEL=WARNING raises the bar: INFO is dropped, WARNING shows."""
    monkeypatch.setenv("MANITOBA_LOG_LEVEL", "WARNING")
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._configure_service_logging()
        log = logging.getLogger("lib.pusher")
        log.info("info should be suppressed")
        log.warning("warning should appear")
    out = buf.getvalue()
    assert "info should be suppressed" not in out
    assert "warning should appear" in out


def test_idempotent_no_duplicate_handlers(clean_root_logging, monkeypatch):
    """Re-invoking (service restart re-exec, double call) must not stack handlers
    and double-emit every line."""
    monkeypatch.delenv("MANITOBA_LOG_LEVEL", raising=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._configure_service_logging()
        cli._configure_service_logging()
        logging.getLogger("lib.pusher").info("single line")
    out = buf.getvalue()
    assert out.count("single line") == 1
