"""Shared, session-scoped fixtures for the detector tests.

Every detector walks the whole checkout. Re-reading ~575 files per test would
make the suite slow enough that people stop running it, which is how a guard
dies. One Repo, one text cache, one report — reused.
"""
from __future__ import annotations

import os

import pytest

from lib.audit import detectors as _detectors
from lib.audit.engine import Ctx, run
from lib.audit.ledger import load as load_ledgers
from lib.audit.repo import Repo

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@pytest.fixture(scope="session")
def repo() -> Repo:
    return Repo(REPO_ROOT)


@pytest.fixture(scope="session")
def ledgers(repo):
    return load_ledgers(repo)


@pytest.fixture(scope="session")
def ctx(repo, ledgers) -> Ctx:
    return Ctx(repo=repo, ledgers=ledgers)


@pytest.fixture(scope="session")
def available_detectors():
    return _detectors.available()


@pytest.fixture(scope="session")
def report(repo):
    """The real audit against the real checkout, meta-checks included."""
    return run(repo)
