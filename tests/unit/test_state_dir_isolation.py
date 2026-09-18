"""The unit suite must never read or write the operator's real ~/.opt/maint.

Guards the import-time hole found 2026-09-17: conftest's autouse fixture is
per-test, so a module binding its state path at import time (lib/recovery.py)
escaped isolation entirely and used the real home directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "maint"))

REAL_STATE_DIR = Path.home() / ".opt" / "maint"


def test_state_dir_env_is_not_the_real_one():
    assert Path(os.environ["MANITOBA_STATE_DIR"]).resolve() != REAL_STATE_DIR.resolve()


def test_import_time_bound_ledger_is_isolated():
    """lib/recovery.py binds its escalation ledger at import time."""
    import lib.recovery as recovery  # type: ignore
    bound = Path(recovery._ESCALATION_PAGE_LEDGER).resolve()
    assert bound.parent != REAL_STATE_DIR.resolve(), (
        f"recovery bound its ledger to the REAL state dir ({bound}); "
        "conftest import-time isolation regressed"
    )


def test_every_module_that_reads_the_env_sees_the_isolated_dir():
    import lib.regrab_ledger as rl  # type: ignore
    assert Path(rl.ledger_path()).parent.resolve() != REAL_STATE_DIR.resolve()
