"""The NEVER-SEEN alert must survive into EXPIRED, not just PENDING.

WHY THIS EXISTS
---------------
`plan_for_share` grades a share and returns a Plan. When the entitlement
service has never heard of an address at all, that is overwhelmingly a typo in
`billing.holder` rather than a person who never subscribed — the address being
looked up is simply not the address they pay with.

That warning used to fire ONLY in the PENDING branch, on the reasoning that you
want to hear about it "while there is still time to fix it". That is backwards.
PENDING costs nobody anything; EXPIRED is the moment the person is actually
reduced to Welcome and has their Seerr disabled. So the one signal that
distinguishes "did not subscribe" from "we are asking about the wrong address"
went quiet at exactly the moment it started costing someone access — and stayed
quiet on every subsequent run, because a household does not leave EXPIRED on its
own.

The live shape of this, 2026-08-07: ten households sit PENDING with 24.2 days of
grace and amnesty expiring 2026-09-01, every one of them flagged NEVER SEEN. One
of them is expected to start paying via Patreon. If their Patreon address
differs from `billing.holder`, they get reduced as a non-payer WHILE PAYING, and
under the old behaviour the operator would have been told nothing.

These tests pin both halves: the alert fires in EXPIRED when never_seen, and it
stays absent when the service gave a real answer (so the alert keeps meaning
"look at the address", not "someone was reduced").
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MAINT = os.path.join(REPO_ROOT, "scripts", "maint")
LIB = os.path.join(MAINT, "lib")


@pytest.fixture(scope="module")
def gate():
    """Load qflix-entitlement.py by path (it is a script, not a package).

    It must be registered in sys.modules BEFORE exec_module: @dataclass resolves
    its own module out of sys.modules while processing annotations, and blows up
    with a bare AttributeError on None if it is absent.
    """
    for p in (LIB, MAINT):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(
        "qflix_entitlement_undertest", os.path.join(MAINT, "qflix-entitlement.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["qflix_entitlement_undertest"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def libs():
    for p in (LIB, MAINT):
        if p not in sys.path:
            sys.path.insert(0, p)
    import access_state as ST
    import entitlement as ENT
    import plexshare as PS
    from lib import members as MEM
    return ENT, PS, ST, MEM


def _plan(gate, libs, *, never_seen, days_past_deadline, tmp_path):
    ENT, PS, ST, MEM = libs
    now = dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc)
    # Deadline in the past by `days_past_deadline` => EXPIRED; in the future => PENDING.
    accepted = now - dt.timedelta(days=400)

    share = PS.Share(shared_server_id=1, user_id=2, email="t@example.com",
                     username="t", section_ids={4, 5}, all_libraries=False,
                     accepted_at=accepted, invited_at=accepted)
    # never_seen is a PROPERTY: verdict == NO and reason == "unknown". Build the
    # inputs it derives from rather than trying to set the derived value, so the
    # test exercises the real predicate.
    answer = ENT.Answer(verdict=ENT.NO, email="t@example.com", http_status=200,
                        error=None, stale=False, status="ok",
                        reason=("unknown" if never_seen else "not_entitled"),
                        tiers=(), amount_cents=None, synced_at=None, raw={})
    assert answer.never_seen is never_seen, "fixture did not produce the intended never_seen"

    hh = MEM.Household(id="t", display="T", exempt=False,
                       accounts=["t@example.com"],
                       billing=MEM.Billing(holder="t@example.com",
                                           amount_usd=0, rail="manual"))
    # An account with NO row is treated as arriving now, which yields a full
    # new-arrival window and lands in PENDING. To reach EXPIRED the account must
    # carry an old anchor, so seed one.
    anchor = now - dt.timedelta(days=365)
    acct = ST.AccountState(first_seen_accepted=anchor, cohort=ST.COHORT_ARRIVAL,
                           last_entitled_at=None, went_false_at=None,
                           seerr_user_id=None, seerr_perms_prior=None,
                           last_action=None, last_action_at=None,
                           first_not_entitled_at=anchor, last_alert=None,
                           last_alert_on=None)
    state = ST.AccessState(path=tmp_path / "state.json", first_run_at=anchor,
                           accounts={"t@example.com": acct}, dirty=False,
                           last_digest_on=None)
    return gate.plan_for_share(
        share=share, household=hh, answer=answer, seerr_user=None, state=state,
        full_ids=[4, 5], minimum_ids=[7],
        amnesty_until=now - dt.timedelta(days=days_past_deadline + 30),
        grace_days=7, new_arrival_days=30, member_permissions=0, now=now)


def test_expired_and_never_seen_raises_an_alert(gate, libs, tmp_path):
    """THE REGRESSION. Reducing someone the service has never heard of must say
    so — that is the difference between a non-payer and a wrong address."""
    plan = _plan(gate, libs, never_seen=True, days_past_deadline=14,
                 tmp_path=tmp_path)
    assert plan.state == gate.S_EXPIRED
    assert plan.alert, (
        "EXPIRED + never_seen produced no alert — the typo warning went silent "
        "at exactly the moment the reduction happens")
    assert "NEVER SEEN" in plan.alert.upper()


def test_expired_with_a_real_answer_stays_quiet(gate, libs, tmp_path):
    """The alert must keep meaning 'check the address'. If it fired on every
    expiry it would be noise, and an operator learns to scroll past noise."""
    plan = _plan(gate, libs, never_seen=False, days_past_deadline=14,
                 tmp_path=tmp_path)
    assert plan.state == gate.S_EXPIRED
    assert not plan.alert, (
        "a definitively not-entitled member produced a typo alert — that makes "
        "the signal meaningless")
