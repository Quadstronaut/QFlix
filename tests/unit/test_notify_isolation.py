"""Regression pin for tests/unit/conftest.py's secrets isolation.

If this test ever fails, unit tests have regained access to real secrets —
meaning any test that reaches lib.notify.notify() will spam the operator's
actual Discord (2026-06-06 incident). Fix the conftest, not this test.
"""
from __future__ import annotations
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "maint"))


def test_notify_cannot_reach_a_real_webhook():
    from lib import notify, secrets

    # the autouse fixture must have redirected secrets away from <repo>/secrets
    assert not (secrets.secrets_dir() / "discord-webhook.url").exists()
    # and notify must therefore no-op (returns False, no HTTP)
    assert notify.notify("isolation self-test — must never reach Discord") is False
