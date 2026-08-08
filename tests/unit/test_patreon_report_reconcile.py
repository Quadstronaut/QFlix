"""reconcile() must survive contact with a real Roster.

WHY THIS FILE EXISTS
patreon-report.py read `hid` in three places while the dataclass field has
always been `Household.id` (lib/members.py:135). Two of the sites hid the bug
-- `getattr(hh, "hid", None)` politely returns None, so every matched patron
reported `household_id: null` and the not-in-seen dedupe was always true -- and
the third crashed outright: `h.hid` raised AttributeError on the FIRST patreon
household the roster-side sweep touched. AttributeError is not a PatreonError,
so main()'s handler let it traceback.

Nothing caught it because no test had ever called reconcile() with the real
Roster type; the tool's own runs died earlier, at the dead OAuth token, which
is a LOUD failure -- so the crash behind it stayed unreachable and unreported
(found by the 2026-08-08 money-path council, DEFECT-A).

The fixtures here build the roster through M.load() on real YAML rather than a
stub, so the field names asserted are the loader's own contract -- a stub with
an `.id` attribute would have passed against the `hid` bug just as silently as
no test at all.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "maint"))

from lib import members as M  # noqa: E402


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "patreon_report", REPO / "scripts" / "maint" / "patreon-report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


@pytest.fixture()
def roster(tmp_path):
    """A real Roster with one patreon household, via the real loader."""
    y = tmp_path / "members.yaml"
    y.write_text(
        "version: 1\n"
        "armed: false\n"
        "households:\n"
        "  - id: payer-one\n"
        "    display: \"Payer One\"\n"
        "    exempt: false\n"
        "    billing:\n"
        "      holder: payer1@example.com\n"
        "      amount_usd: 10\n"
        "      rail: patreon\n"
        "      payer_ref: payer1@example.com\n"
        "    accounts: [payer1@example.com]\n",
        encoding="utf-8")
    return M.load(y)


def _member(email, name="Payer One", status="active_patron"):
    return {"id": "m-1", "attributes": {
        "email": email, "full_name": name, "patron_status": status,
        "currently_entitled_amount_cents": 1000}}


def test_roster_sweep_does_not_crash_and_names_the_household(tool, roster):
    """The DEFECT-A crash site: a patreon household with no patron record."""
    rep = tool.reconcile([], roster)
    rail = rep["roster_on_patreon_without_patron"]
    assert [r["household_id"] for r in rail] == ["payer-one"]


def test_matched_patron_carries_the_household_id(tool, roster):
    """The silent site: getattr(hh, 'hid') returned None for every match, so
    the report said a patron matched but could not say to WHOM."""
    rep = tool.reconcile([_member("payer1@example.com")], roster)
    assert len(rep["matched"]) == 1
    assert rep["matched"][0]["household_id"] == "payer-one"
    # ...and a matched household must NOT also be reported as missing its
    # patron: the None ids made `not in seen` vacuously true for everyone.
    assert rep["roster_on_patreon_without_patron"] == []


def test_unmatched_patron_stays_unmatched(tool, roster):
    rep = tool.reconcile([_member("stranger@example.com", "A Stranger")], roster)
    assert rep["matched"] == []
    assert len(rep["unmatched_patrons"]) == 1
    assert [r["household_id"] for r in rep["roster_on_patreon_without_patron"]] == ["payer-one"]
