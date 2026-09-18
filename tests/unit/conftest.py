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

import sys

import pytest


@pytest.fixture(autouse=True)
def _no_real_secrets_or_notifications(tmp_path, monkeypatch):
    isolated = tmp_path / "secrets-isolated"
    isolated.mkdir()
    state = tmp_path / "maint-state-isolated"
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(isolated))
    monkeypatch.setenv("MANITOBA_SECRETS", str(isolated))
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state))

    # The env var above is read at IMPORT time by lib/recovery.py, which caches
    # two module-level Paths from it. pytest imports that module once, before
    # this fixture ever runs, so those two Paths still point at the operator's
    # REAL ~/.opt/maint — and the recovery tests then wrote page stamps there.
    #
    # Found 2026-09-17: a second, same-day run of the suite on the same
    # workstation went RED on test_recovery_three_failures_escalate, because
    # the FIRST run had left a real `sonarr` stamp in
    # ~/.opt/maint/escalation-pages.json and the 24h cooldown correctly muted
    # the page the test was asserting on. CI never saw it (fresh HOME every
    # run), which is exactly the shape of bug that burns an operator and not a
    # pipeline. Same reasoning as the secrets isolation above: a unit test may
    # not touch live state.
    recovery = sys.modules.get("lib.recovery")
    if recovery is not None:
        monkeypatch.setattr(recovery, "_ESCALATION_PAGE_LEDGER",
                            state / "escalation-pages.json", raising=False)
        monkeypatch.setattr(recovery, "_STATE_PATH", state / "state.json",
                            raising=False)
