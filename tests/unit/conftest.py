"""tests/unit/conftest.py — hard isolation: unit tests must NEVER touch real
secrets or send real notifications.

WHY THIS EXISTS (2026-06-06): lib/secrets.py resolves the secrets dir as
env override -> <repo>/secrets/ -> ~/secrets. On a workstation checkout,
<repo>/secrets/ holds the REAL discord-webhook.url — so any unit test that
reached lib.notify.notify() posted to the operator's actual Discord. Every
full-suite run spammed the channel with test-fixture messages ("[radarr]
fallback stage 1 (HDTV): Movie", "UCC upstream maintenance detected", ...).

The autouse fixture below points every secrets/state env var at an empty
per-test tmp dir BEFORE the test body runs:
  - lib.secrets.secrets_dir() finds no discord-webhook.url -> notify()
    becomes a no-op (logs to the tmp state dir and returns False).
  - Anything else that resolves secrets (listmonk, kuma, ArrClient via
    MANITOBA_SECRETS) sees an empty dir and fails fast/skips instead of
    hitting production services.

Tests that need specific secrets keep working: they either pass an explicit
secrets_dir/monkeypatch.setenv (which runs AFTER this fixture and wins) or
write files into the dir these env vars point at.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# --- IMPORT-TIME isolation (2026-09-17) -----------------------------------
# The autouse fixture below is per-TEST, which is too late for any module that
# binds its state path at IMPORT time. lib/recovery.py:132 does exactly that
# (`_ESCALATION_PAGE_LEDGER = _state_dir() / "escalation-pages.json"`), so the
# fixture never redirected it and the suite read and WROTE the developer's real
# ~/.opt/maint/escalation-pages.json. Consequences seen on 2026-09-17: fixture
# rows in the real arr-regrab-ledger.json and arr-unstick-pages.json, a real
# arr-unstick.log appended to, and test_recovery_three_failures_escalate
# failing depending on what an EARLIER run had left on disk -- an
# order-dependent failure whose cause is invisible from the test.
#
# pytest imports conftest before it imports any test module, so setting the env
# here is the earliest point that still precedes every `import lib.<x>` in the
# suite. The per-test fixture below keeps function-scoped isolation for modules
# that resolve lazily; this only guarantees that the IMPORT-time fallback can
# never be the operator's real state dir.
_SESSION_STATE_DIR = tempfile.mkdtemp(prefix="qflix-unit-state-")
_SESSION_SECRETS_DIR = tempfile.mkdtemp(prefix="qflix-unit-secrets-")
os.environ["MANITOBA_STATE_DIR"] = _SESSION_STATE_DIR
os.environ["MANITOBA_SECRETS_DIR"] = _SESSION_SECRETS_DIR
os.environ["MANITOBA_SECRETS"] = _SESSION_SECRETS_DIR


@atexit.register
def _drop_session_dirs() -> None:
    for d in (_SESSION_STATE_DIR, _SESSION_SECRETS_DIR):
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_real_secrets_or_notifications(tmp_path, monkeypatch):
    isolated = tmp_path / "secrets-isolated"
    isolated.mkdir()
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(isolated))
    monkeypatch.setenv("MANITOBA_SECRETS", str(isolated))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path / "maint-state-isolated"))
