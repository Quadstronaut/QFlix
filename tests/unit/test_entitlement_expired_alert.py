"""NEVER-SEEN in the EXPIRED branch: recorded on the plan, never paged.

WHY THIS EXISTS
---------------
`plan_for_share` grades a share and returns a Plan. "Never seen" means the
entitlement service has no record of the household's `billing.holder` AT ALL,
as distinct from having a record that says no.

The original 2026-08-07 version of this file pinned an ALERT here. The reasoning
was sound for its moment: never-seen was overwhelmingly a typo in
`billing.holder`, and it used to fire only in PENDING — which costs nobody
anything — while going quiet in EXPIRED, the moment the member is actually
reduced to Welcome and has Seerr disabled. So the one signal distinguishing "did
not subscribe" from "we are asking about the wrong address" was silent exactly
when it started costing someone access.

WHAT CHANGED 2026-08-17 (operator directive)
--------------------------------------------
The base rate flipped. The Patreon behind the entitlement service now carries
non-QFlix members, and QFlix carries households paying on rails the service
cannot see at all, so never-seen became an ordinary steady state for a growing
slice of the roster rather than an anomaly. Combined with EXPIRED being terminal
— a household does not leave it on its own — the alert became a permanent daily
page, per household, on a fact that was not going to change. A channel that
fires on a steady state is a channel that gets muted, and muting THIS one would
also bury the unnamed-share page and the arm-check.

So the fact is kept and the page is dropped: `Plan.never_seen` carries it into
the --json plan, `Plan.reason` says it in words, and the rare genuinely
actionable variant — an EVER-ENTITLED declared payer going never-seen, meaning
the sync projection died — still pages from `payer_oracle.judge()` row 3.

WHAT CHANGED 2026-08-19 (this file's assertions moved with it)
-------------------------------------------------------------
Dropping the page finished half the thought. The miss kept walking the lapse
ladder: PENDING with a live countdown, then EXPIRED, then a real reduction of a
real person's libraries -- decided by the entitlement service's failure to have
heard of them. Live on 2026-08-20 that was five of twelve shares sitting at
"11.9 day(s) of grace remain", every one of them a miss.

An absence of evidence is not a verdict. A miss is now graded UNKNOWN and
frozen in its own state, S_UNKNOWN_PAYER -- nothing granted, nothing reduced,
no countdown -- the same freeze S_NO_ANSWER already got for the same reason.
So never-seen NEVER REACHES EXPIRED at all, and the question this file used to
ask ("is the reduction of a never-seen household still recorded?") is answered
by there being no such reduction.

These tests pin all of it: a miss past its deadline is frozen rather than
reduced, the fact still reaches the plan and --json, nothing pages, and a REAL
not-entitled answer still expires and still reduces.
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


def test_a_miss_past_its_deadline_is_frozen_not_reduced_and_never_pages(gate, libs,
                                                                       tmp_path):
    """The strongest form of the statement: fourteen days PAST the deadline,
    with an anchor old enough that every clock has run out, a household the
    service has no record of is still not reduced.

    The old assertion here was `state == EXPIRED, alert is None` -- the harm it
    guarded (a silent reduction of a wrong address) is now impossible by
    construction rather than merely unpaged, so the assertion moves up to the
    property instead of the state name. The distinction the old test cared
    about is still legible, and better than before: a miss is its own state
    rather than a boolean buried in a reason string.

    Losing the reduction is the POINT, not a side effect. Nothing here grants
    anything either -- the household keeps exactly what it already holds."""
    plan = _plan(gate, libs, never_seen=True, days_past_deadline=14,
                 tmp_path=tmp_path)
    assert plan.state == gate.S_UNKNOWN_PAYER
    assert plan.never_seen is True, "the fact must survive on the plan"
    assert plan.to_json()["never_seen"] is True, "and reach the --json surface"
    assert plan.plex_target is None, "a miss may never take libraries away"
    assert plan.seerr_target is None, "nor disable Seerr"
    assert "billing.rail" in plan.reason, "and must name the operator's lever"
    assert not plan.alert, (
        "a lookup miss paged -- that is a permanent daily alert per household "
        "on a fact that does not change (operator directive 2026-08-17)")


def test_the_freeze_is_not_a_grant(gate, libs, tmp_path):
    """Freezing an UNKNOWN must not be mistaken for resolving it upward. The
    household keeps what it holds; the only write allowed to ride along is the
    stage-1 Seerr provisioning every accepted share gets."""
    plan = _plan(gate, libs, never_seen=True, days_past_deadline=14,
                 tmp_path=tmp_path)
    assert plan.plex_target is None and plan.seerr_target is None
    assert not plan.mutates or plan.provision_plex_id is not None


def test_expired_with_a_real_answer_records_nothing_extra(gate, libs, tmp_path):
    """never_seen must keep meaning 'the service has no record', not 'someone
    was reduced' — otherwise the JSON field is as useless as the old alert."""
    plan = _plan(gate, libs, never_seen=False, days_past_deadline=14,
                 tmp_path=tmp_path)
    assert plan.state == gate.S_EXPIRED
    assert plan.never_seen is False
    assert "billing.holder" not in plan.reason
    assert not plan.alert
    # The revoke rail is UNCHANGED by the freeze above. Same fixture, same
    # expired clock, only a real verdict instead of a miss -- and the reduction
    # still happens. Without this, "freeze on unknown" could quietly become
    # "never reduce anyone" and every other test in this file stays green.
    assert plan.plex_target == [7], "a real lapse must still drop to the floor"
