"""The ledger infers "paid up" from arrivals. Every test here is a way that goes wrong.

There is no subscription status to read on any rail that does not require a
merchant contract, so paid-up is an inference from a credit plus a date window.
That inversion moves the risk: instead of trusting a provider's field, we trust
our own reading of a stream of arrivals -- and the ways THAT fails all end with
somebody who paid losing access.

The single most dangerous property: a broken ingester and "nobody paid this
month" produce identical evidence. An IMAP password change would read as
thirteen simultaneous lapses. gate_inputs() is the interlock and it gets the
hardest tests in this file.

No real member data appears here. Households are opaque ids and amounts are
made up -- this file is public.
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "maint"))

from lib import ledger as L  # noqa: E402


def C(household, day, amount=50.0, source="venmo", ext=None, note=""):
    return L.Credit(household=household, source=source,
                    external_id=ext or ("%s-%s-%s" % (source, household, day)),
                    amount_usd=amount, at=str(day), note=note)


D = date(2026, 8, 1)


# --- persistence ---------------------------------------------------------

def test_a_credit_round_trips(tmp_path):
    p = tmp_path / "ledger.jsonl"
    L.append(p, C("alpha", "2026-07-01"))
    got = L.read(p)
    assert len(got) == 1
    assert got[0].household == "alpha"
    assert got[0].amount_usd == 50.0
    assert got[0].day == date(2026, 7, 1)


def test_a_missing_ledger_is_empty_not_an_error(tmp_path):
    assert L.read(tmp_path / "nope.jsonl") == []


def test_the_same_payment_written_twice_counts_once(tmp_path):
    """Re-reading an inbox, replaying a webhook, or re-running after a crash
    must not extend anybody's access twice."""
    p = tmp_path / "ledger.jsonl"
    c = C("alpha", "2026-07-01", ext="venmo-txn-99")
    L.append(p, c)
    L.append(p, c)
    L.append(p, c)
    assert len(L.read(p)) == 1


def test_dedup_is_per_source_not_global(tmp_path):
    """Two rails can legitimately mint the same id. Collapsing them would
    silently drop a real payment."""
    p = tmp_path / "ledger.jsonl"
    L.append(p, C("alpha", "2026-07-01", ext="1001", source="venmo"))
    L.append(p, C("alpha", "2026-07-02", ext="1001", source="paypal"))
    assert len(L.read(p)) == 2


def test_a_malformed_line_is_fatal_not_skipped(tmp_path):
    """THE ONE THAT MATTERS FOR PERSISTENCE.

    Skipping unparseable lines under-reports payments, and under-reporting
    payments cuts off someone who paid. Loud beats lossy.
    """
    p = tmp_path / "ledger.jsonl"
    L.append(p, C("alpha", "2026-07-01"))
    with open(p, "a", encoding="utf-8") as f:
        f.write("{this is not json\n")
    with pytest.raises(L.LedgerError, match="malformed"):
        L.read(p)


def test_a_line_missing_a_required_field_is_also_fatal(tmp_path):
    p = tmp_path / "ledger.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"household": "alpha", "source": "venmo"}) + "\n")
    with pytest.raises(L.LedgerError):
        L.read(p)


def test_an_unparseable_date_is_caught_at_read_not_at_compare(tmp_path):
    """Otherwise the failure surfaces deep inside the gate, mid-reconcile,
    after some households have already been acted on."""
    p = tmp_path / "ledger.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"household": "a", "source": "venmo", "external_id": "1",
                            "amount_usd": 50, "at": "last tuesday"}) + "\n")
    with pytest.raises(L.LedgerError):
        L.read(p)


# --- the inference -------------------------------------------------------

def test_a_fresh_payment_is_paid():
    s = L.standing([C("alpha", "2026-07-20")], "alpha", 50.0, grace_days=3, on=D)
    assert s.state == "paid"
    assert s.paid_through == date(2026, 8, 20)


def test_the_day_the_cycle_ends_is_still_paid():
    """Boundary. Judging someone late on the exact day their cycle runs out is
    a rounding error that reads to them as being cut off early."""
    last = D - timedelta(days=L.CYCLE_DAYS)
    s = L.standing([C("alpha", last)], "alpha", 50.0, grace_days=3, on=D)
    assert s.state == "paid"


def test_one_day_past_the_cycle_enters_grace_not_lapsed():
    last = D - timedelta(days=L.CYCLE_DAYS + 1)
    s = L.standing([C("alpha", last)], "alpha", 50.0, grace_days=3, on=D)
    assert s.state == "grace"


def test_grace_expiry_is_inclusive_then_lapses():
    last = D - timedelta(days=L.CYCLE_DAYS + 3)
    assert L.standing([C("alpha", last)], "alpha", 50.0, 3, on=D).state == "grace"
    last = D - timedelta(days=L.CYCLE_DAYS + 4)
    assert L.standing([C("alpha", last)], "alpha", 50.0, 3, on=D).state == "lapsed"


def test_a_household_with_no_credits_is_never_paid_not_lapsed():
    """Distinct states on purpose. 'Never paid' is an onboarding problem;
    'lapsed' is a billing problem. Collapsing them hides which one you have."""
    s = L.standing([], "ghost", 50.0, 3, on=D)
    assert s.state == "never_paid"
    assert s.paid_through is None


def test_the_most_recent_credit_wins_regardless_of_write_order():
    creds = [C("alpha", "2026-05-01"), C("alpha", "2026-07-25"), C("alpha", "2026-06-01")]
    s = L.standing(creds, "alpha", 50.0, 3, on=D)
    assert s.last_credit == date(2026, 7, 25)
    assert s.state == "paid"


def test_credits_from_different_rails_both_count():
    """Multi-rail is the point: someone paying by Venmo one month and PayPal
    the next must stay continuously paid."""
    creds = [C("alpha", "2026-06-25", source="venmo"),
             C("alpha", "2026-07-26", source="paypal")]
    s = L.standing(creds, "alpha", 50.0, 3, on=D)
    assert s.state == "paid"
    assert s.last_credit == date(2026, 7, 26)


def test_another_households_payments_never_count():
    s = L.standing([C("beta", "2026-07-25")], "alpha", 50.0, 3, on=D)
    assert s.state == "never_paid"


# --- shortfall -----------------------------------------------------------

def test_a_short_payment_is_flagged_but_does_not_revoke():
    """$45 against a $50 agreement is a conversation, not a cut-off. The gate
    surfaces it; a human decides."""
    s = L.standing([C("alpha", "2026-07-25", amount=45.0)], "alpha", 50.0, 3, on=D)
    assert s.shortfall is True
    assert s.state == "paid"


def test_exact_payment_is_not_a_shortfall_despite_float_math():
    s = L.standing([C("alpha", "2026-07-25", amount=49.99 + 0.01)], "alpha", 50.0, 3, on=D)
    assert s.shortfall is False


def test_an_unset_expected_amount_never_flags_a_shortfall():
    s = L.standing([C("alpha", "2026-07-25", amount=5.0)], "alpha", None, 3, on=D)
    assert s.shortfall is False
    assert s.state == "paid"


# --- the interlock -------------------------------------------------------

def test_an_empty_ledger_refuses_to_authorise_revocation():
    """THE BIG ONE. An empty ledger means either nobody has ever paid or
    ingestion has never worked, and those are indistinguishable. Acting on that
    revokes everybody."""
    ok, why = L.gate_inputs([], stale_after_days=40, on=D)
    assert ok is False
    assert "ingestion" in why


def test_a_stale_ledger_refuses_to_authorise_revocation():
    """An IMAP password change, a Gmail filter edit, or a rail changing its
    receipt wording all look exactly like everyone lapsing at once."""
    old = [C("alpha", D - timedelta(days=60))]
    ok, why = L.gate_inputs(old, stale_after_days=40, on=D)
    assert ok is False
    assert "broken ingester" in why


def test_a_fresh_ledger_authorises_revocation():
    fresh = [C("alpha", D - timedelta(days=2))]
    ok, why = L.gate_inputs(fresh, stale_after_days=40, on=D)
    assert ok is True
    assert "fresh" in why


def test_one_recent_credit_from_anyone_is_enough_to_prove_ingest_alive():
    """Deliberately ANY household, not each. One arrival proves the pipe works;
    requiring all of them would jam the gate on a single genuine lapse."""
    creds = [C("alpha", D - timedelta(days=1)),
             C("beta", D - timedelta(days=200))]
    ok, _ = L.gate_inputs(creds, stale_after_days=40, on=D)
    assert ok is True
    assert L.standing(creds, "beta", 50.0, 3, on=D).state == "lapsed"


def test_the_staleness_boundary_is_not_off_by_one():
    exactly = [C("alpha", D - timedelta(days=40))]
    assert L.gate_inputs(exactly, stale_after_days=40, on=D)[0] is True
    over = [C("alpha", D - timedelta(days=41))]
    assert L.gate_inputs(over, stale_after_days=40, on=D)[0] is False


# --- reporting -----------------------------------------------------------

def test_summarise_counts_per_rail():
    creds = [C("a", "2026-07-01", source="venmo"),
             C("b", "2026-07-02", source="venmo"),
             C("c", "2026-07-03", source="paypal")]
    assert L.summarise(creds) == {"venmo": 2, "paypal": 1}


def test_last_ingest_is_none_on_empty():
    assert L.last_ingest([]) is None
