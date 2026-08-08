"""lib/payer_oracle.py -- the SPEC section 3 verdict table, one row per test.

Every test below asserts BOTH the verdict AND that `detail` carries no
unmasked address and no household id (AC-02). Detail text is inspected for
the literal household id strings and for the full holder email string -- if
either survives, the test fails loudly rather than trusting `mask()` blindly.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(REPO_ROOT, "scripts", "maint", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import payer_oracle as ORACLE  # noqa: E402


NOW = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.timezone.utc)
OLD = NOW - dt.timedelta(days=30)          # well past any settle window
RECENT = NOW - dt.timedelta(hours=6)       # well inside a 2-day settle window


def _payer(hid, holder, *, declared_at=OLD, ever_entitled=False,
          currently_yes=False, currently_never_seen=False):
    return ORACLE.DeclaredPayer(
        household_id=hid, holder=holder, first_declared_at=declared_at,
        ever_entitled=ever_entitled, currently_yes=currently_yes,
        currently_never_seen=currently_never_seen)


def _bulk(state, count=None, entitled=()):
    return ORACLE.BulkFacts(state=state, count=count, entitled=tuple(entitled))


def _assert_no_pii(v, *forbidden_strings):
    """Common assertion body: detail carries none of the raw values passed."""
    for s in forbidden_strings:
        assert s not in v.detail, (
            "unmasked value %r leaked into oracle detail: %r" % (s, v.detail))


# --- row 1: DORMANT ----------------------------------------------------------


def test_row1_zero_declared_payers_is_dormant_and_green():
    v = ORACLE.judge(declared=[], bulk=_bulk(ORACLE.BULK_NO_SCOPE), now=NOW)
    assert v.verdict == ORACLE.DORMANT
    assert v.is_red is False
    assert v.canary_exit == 0


# --- row 2: PROVEN -------------------------------------------------------


def test_row2_any_current_yes_is_proven_and_green():
    payers = [
        _payer("h1", "payer1@example.com", currently_yes=True),
        _payer("h2", "payer2@example.com"),
    ]
    v = ORACLE.judge(declared=payers, bulk=_bulk(ORACLE.BULK_NO_SCOPE), now=NOW)
    assert v.verdict == ORACLE.PROVEN
    assert v.is_red is False
    _assert_no_pii(v, "h1", "h2", "payer1@example.com", "payer2@example.com")


# --- row 3: DEAD (AC-04 -- ANY forgotten, not ALL) --------------------------


def test_row3_any_forgotten_among_two_ever_entitled_is_dead():
    """AC-04. Two ever-entitled accounts, ONE forgotten -- must be DEAD, not
    a pass because 'only' one of two was lost."""
    payers = [
        _payer("h1", "still-good@example.com", ever_entitled=True,
              currently_never_seen=False),
        _payer("h2", "forgotten@example.com", ever_entitled=True,
              currently_never_seen=True),
    ]
    v = ORACLE.judge(declared=payers, bulk=_bulk(ORACLE.BULK_NO_SCOPE), now=NOW)
    assert v.verdict == ORACLE.DEAD
    assert v.is_red is True
    assert v.canary_exit == 1
    _assert_no_pii(v, "h1", "h2", "forgotten@example.com", "still-good@example.com")


def test_row3_all_forgotten_is_still_dead():
    """The original (weaker) draft threshold must still be caught."""
    payers = [
        _payer("h1", "a@example.com", ever_entitled=True, currently_never_seen=True),
        _payer("h2", "b@example.com", ever_entitled=True, currently_never_seen=True),
    ]
    v = ORACLE.judge(declared=payers, bulk=_bulk(ORACLE.BULK_NO_SCOPE), now=NOW)
    assert v.verdict == ORACLE.DEAD


def test_row3_zero_forgotten_among_ever_entitled_does_not_fire():
    payers = [_payer("h1", "a@example.com", ever_entitled=True,
                     currently_never_seen=False)]
    v = ORACLE.judge(declared=payers, bulk=_bulk(ORACLE.BULK_OK, count=1,
                                                entitled=["a@example.com"]), now=NOW)
    assert v.verdict != ORACLE.DEAD


# --- row 4: MISMATCH (AC-05) ----------------------------------------------


def test_row4_bulk_nonempty_no_declared_holder_present_is_mismatch():
    payers = [_payer("h1", "roster-address@example.com")]
    bulk = _bulk(ORACLE.BULK_OK, count=1, entitled=["different-address@example.com"])
    v = ORACLE.judge(declared=payers, bulk=bulk, now=NOW)
    assert v.verdict == ORACLE.MISMATCH
    assert v.is_red is True
    assert v.canary_exit == 1
    # A masked EXAMPLE must be present (per SPEC: "money-losing, names masked
    # example") but the real address must not.
    assert "di***@example.com" in v.detail
    _assert_no_pii(v, "h1", "roster-address@example.com", "different-address@example.com")


def test_row4_does_not_fire_when_a_declared_holder_is_present():
    payers = [_payer("h1", "matches@example.com")]
    bulk = _bulk(ORACLE.BULK_OK, count=1, entitled=["matches@example.com"])
    v = ORACLE.judge(declared=payers, bulk=bulk, now=NOW)
    assert v.verdict == ORACLE.PROVEN_UPSTREAM


def test_row4_match_is_case_insensitive():
    payers = [_payer("h1", "Matches@Example.com")]
    bulk = _bulk(ORACLE.BULK_OK, count=1, entitled=["matches@example.com"])
    v = ORACLE.judge(declared=payers, bulk=bulk, now=NOW)
    assert v.verdict == ORACLE.PROVEN_UPSTREAM


# --- row 5: PROVEN_UPSTREAM --------------------------------------------


def test_row5_bulk_nonempty_with_a_match_is_proven_upstream_and_green():
    payers = [_payer("h1", "a@example.com")]
    bulk = _bulk(ORACLE.BULK_OK, count=3, entitled=["a@example.com", "x@example.com"])
    v = ORACLE.judge(declared=payers, bulk=bulk, now=NOW)
    assert v.verdict == ORACLE.PROVEN_UPSTREAM
    assert v.is_red is False
    _assert_no_pii(v, "h1", "a@example.com", "x@example.com")


# --- row 6: SETTLING (AC-06) -----------------------------------------------


def test_row6_young_declaration_is_settling_and_green_with_hours_remaining():
    payers = [_payer("h1", "a@example.com", declared_at=RECENT)]
    v = ORACLE.judge(declared=payers, bulk=_bulk(ORACLE.BULK_NO_SCOPE), now=NOW,
                     settle_days=2)
    assert v.verdict == ORACLE.SETTLING
    assert v.is_red is False
    assert "hour" in v.detail.lower()
    # 6h old, 2-day (48h) window -> 42h remaining.
    assert "42.0 hour" in v.detail
    _assert_no_pii(v, "h1", "a@example.com")


def test_row6_zero_declared_payers_takes_row1_not_row6():
    """Zero declared payers must yield DORMANT (row 1), never SETTLING."""
    v = ORACLE.judge(declared=[], bulk=_bulk(ORACLE.BULK_NO_SCOPE), now=NOW)
    assert v.verdict == ORACLE.DORMANT


# --- row 7: UNPROVEN_BLIND (AC-03) -----------------------------------------


def test_row7_todays_live_shape_is_unproven_blind_and_red():
    """AC-03. The exact live shape: 3 declared payers, 0 ever entitled,
    bulk='no-scope', age >= settle_days."""
    payers = [_payer("h%d" % i, "payer%d@example.com" % i) for i in range(3)]
    v = ORACLE.judge(declared=payers, bulk=_bulk(ORACLE.BULK_NO_SCOPE), now=NOW,
                     settle_days=2)
    assert v.verdict == ORACLE.UNPROVEN_BLIND
    assert v.is_red is True
    assert v.canary_exit == 1
    low = v.detail.lower()
    assert "grant" in low
    assert "bulk" in low
    assert "scope" in low
    _assert_no_pii(v, "h0", "h1", "h2",
                   "payer0@example.com", "payer1@example.com", "payer2@example.com")


def test_row7_unreachable_bulk_also_yields_blind():
    payers = [_payer("h1", "a@example.com")]
    v = ORACLE.judge(declared=payers, bulk=_bulk(ORACLE.BULK_UNREACHABLE), now=NOW)
    assert v.verdict == ORACLE.UNPROVEN_BLIND


def test_row7_unparseable_bulk_also_yields_blind():
    payers = [_payer("h1", "a@example.com")]
    v = ORACLE.judge(declared=payers, bulk=_bulk(ORACLE.BULK_UNPARSEABLE), now=NOW)
    assert v.verdict == ORACLE.UNPROVEN_BLIND


# --- row 8: UNPROVEN_EMPTY --------------------------------------------------


def test_row8_bulk_supported_zero_count_zero_ever_entitled_is_unproven_empty():
    payers = [_payer("h1", "a@example.com")]
    v = ORACLE.judge(declared=payers, bulk=_bulk(ORACLE.BULK_OK, count=0), now=NOW)
    assert v.verdict == ORACLE.UNPROVEN_EMPTY
    assert v.is_red is True
    assert v.canary_exit == 1
    _assert_no_pii(v, "h1", "a@example.com")


# --- AC-07: the oracle module cannot reach access decisions -----------------
# (the I/O-purity half of AC-07; the plan_for_share half lives in
# tests/unit/test_entitlement_arm_check.py, which needs the gate module)


def test_judge_has_no_network_or_file_attributes_reachable():
    """Cheap static guard: the module imports neither urllib nor pathlib/os
    file-opening primitives at all, so there is nothing judge() COULD do I/O
    with even if a future edit tried."""
    import inspect
    src = inspect.getsource(sys.modules["payer_oracle"])
    for banned in ("import urllib", "import socket", "open(", "requests."):
        assert banned not in src, "payer_oracle.py must stay I/O-free: found %r" % banned


# --- fallback row: previously proven, no fresh signal this run -------------


def test_fallback_previously_proven_lapsed_member_bulk_down_stays_green():
    """Not one of the 8 numbered rows: a declared payer who WAS entitled and
    has since lapsed (not forgotten), nobody currently reads yes, and bulk is
    down. History already proved the path works once; this must not read as
    a fresh fault."""
    payers = [_payer("h1", "a@example.com", ever_entitled=True,
                     currently_never_seen=False, currently_yes=False)]
    v = ORACLE.judge(declared=payers, bulk=_bulk(ORACLE.BULK_UNREACHABLE), now=NOW)
    assert v.is_red is False
    assert v.verdict not in ORACLE.RED_VERDICTS


# --- vocabulary pin: payer_oracle's BULK_* strings must match entitlement.py


def test_bulk_state_vocabulary_matches_lib_entitlement():
    MAINT = os.path.join(REPO_ROOT, "scripts", "maint")
    if MAINT not in sys.path:
        sys.path.insert(0, MAINT)
    import entitlement as ENT
    assert ORACLE.BULK_OK == ENT.BULK_OK
    assert ORACLE.BULK_NO_SCOPE == ENT.BULK_NO_SCOPE
    assert ORACLE.BULK_UNREACHABLE == ENT.BULK_UNREACHABLE
    assert ORACLE.BULK_UNPARSEABLE == ENT.BULK_UNPARSEABLE


def test_bulk_facts_from_bulk_answer_adapter():
    MAINT = os.path.join(REPO_ROOT, "scripts", "maint")
    if MAINT not in sys.path:
        sys.path.insert(0, MAINT)
    import entitlement as ENT
    ans = ENT.BulkAnswer(state=ENT.BULK_OK, count=2, entitled=["a@example.com"])
    bf = ORACLE.BulkFacts.from_bulk_answer(ans)
    assert bf.supported is True
    assert bf.count == 2
    assert bf.entitled == ("a@example.com",)


# --- mask() ------------------------------------------------------------


@pytest.mark.parametrize("addr,expected_prefix", [
    # local-part <= 2 chars keeps only 1 -- same convention as
    # qflix-entitlement.py's mask() and patreon-report.py's mask().
    ("ab@example.com", "a***@example.com"),
    ("abc@example.com", "ab***@example.com"),
    ("a@example.com", "a***@example.com"),
    ("", "?"),
    (None, "?"),
])
def test_mask(addr, expected_prefix):
    assert ORACLE.mask(addr) == expected_prefix
