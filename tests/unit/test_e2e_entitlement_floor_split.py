"""The live e2e harness must expect the same floor/content split the gate writes.

tests/e2e-entitlement-live.py is run by hand against the live stack, so CI never
executes it -- which is how its expectations rotted twice: "entitled = every
section" stayed in it after Welcome left full access (2026-08-17), and "expired =
[Welcome]" would break the moment QFlix - Test joined the floor (QFLX-4). This
pins its split helper against the gate's own lib so the two cannot drift again.
"""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "maint" / "lib"))
import plexshare as PS  # noqa: E402


def _load_e2e():
    os.environ.setdefault("QFLIX_E2E_SUBJECT", "nobody@example.invalid")
    spec = importlib.util.spec_from_file_location(
        "e2e_entitlement_live", ROOT / "tests" / "e2e-entitlement-live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


E2E = _load_e2e()
CAT = {10: "QFlix - Movies", 11: "QFlix - TV", 20: "QFlix - Welcome",
       21: "QFlix - Test"}


def test_e2e_floor_includes_test_and_content_excludes_the_floor():
    floor, content = E2E.split_catalogue(CAT)
    assert floor == [20, 21]
    assert content == [10, 11]


def test_e2e_split_matches_the_gate_library():
    secs = [PS.Section(id=i, key=i, title=t, type="movie") for i, t in CAT.items()]
    floor, content = E2E.split_catalogue(CAT)
    assert floor == PS.minimum_access_ids(secs, E2E.WELCOME_TITLE, E2E.FLOOR_EXTRA)
    assert content == PS.full_access_ids(secs, E2E.WELCOME_TITLE, E2E.FLOOR_EXTRA)
