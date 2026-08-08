"""qflix-entitlement.py's payer-oracle plumbing: declared_payer_households(),
build_declared_payers(), arm_check_should_block(), would_be_reduced(), and
the AC-07 inertness proof.

Loaded by path, same convention as test_entitlement_expired_alert.py.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import inspect
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MAINT = os.path.join(REPO_ROOT, "scripts", "maint")
LIB = os.path.join(MAINT, "lib")


@pytest.fixture(scope="module")
def gate():
    for p in (LIB, MAINT):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(
        "qflix_entitlement_armcheck_undertest", os.path.join(MAINT, "qflix-entitlement.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["qflix_entitlement_armcheck_undertest"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def libs():
    for p in (LIB, MAINT):
        if p not in sys.path:
            sys.path.insert(0, p)
    import access_state as ST
    import entitlement as ENT
    import payer_oracle as ORACLE
    import plexshare as PS
    from lib import members as MEM
    return ENT, ORACLE, PS, ST, MEM


NOW = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.timezone.utc)


def _hh(MEM, hid, *, exempt=False, provisional=False, rail="patreon",
       amount=5.0, holder="a@example.com", accounts=None):
    billing = None if exempt else MEM.Billing(holder=holder, amount_usd=amount,
                                              rail=rail, payer_ref="ref")
    return MEM.Household(id=hid, display=hid, exempt=exempt,
                         accounts=accounts or [holder], billing=billing,
                         provisional=provisional)


# --- declared_payer_households ---------------------------------------------


def test_declared_payer_excludes_exempt(gate, libs):
    _, _, _, _, MEM = libs
    hh = _hh(MEM, "h1", exempt=True)
    roster = MEM.Roster(version=1, armed=False, grace_days=3, paused_sections=[],
                        households=[hh])
    assert gate.declared_payer_households(roster) == []


def test_declared_payer_excludes_provisional(gate, libs):
    _, _, _, _, MEM = libs
    hh = _hh(MEM, "h1", provisional=True)
    roster = MEM.Roster(version=1, armed=False, grace_days=3, paused_sections=[],
                        households=[hh])
    assert gate.declared_payer_households(roster) == []


def test_declared_payer_excludes_zero_amount(gate, libs):
    _, _, _, _, MEM = libs
    hh = _hh(MEM, "h1", amount=0)
    roster = MEM.Roster(version=1, armed=False, grace_days=3, paused_sections=[],
                        households=[hh])
    assert gate.declared_payer_households(roster) == []


def test_declared_payer_includes_a_real_billed_household(gate, libs):
    _, _, _, _, MEM = libs
    hh = _hh(MEM, "h1")
    roster = MEM.Roster(version=1, armed=False, grace_days=3, paused_sections=[],
                        households=[hh])
    out = gate.declared_payer_households(roster)
    assert [h.id for h in out] == ["h1"]


# --- household_ever_entitled -------------------------------------------


def test_household_ever_entitled_true_if_any_account_was(gate, libs, tmp_path):
    _, _, _, ST, MEM = libs
    hh = _hh(MEM, "h1", accounts=["a@example.com", "b@example.com"])
    state = ST.AccessState(path=tmp_path / "s.json")
    state.record_entitled("b@example.com", now=NOW)
    assert gate.household_ever_entitled(hh, state) is True


def test_household_ever_entitled_false_if_none_was(gate, libs, tmp_path):
    _, _, _, ST, MEM = libs
    hh = _hh(MEM, "h1", accounts=["a@example.com"])
    state = ST.AccessState(path=tmp_path / "s.json")
    assert gate.household_ever_entitled(hh, state) is False


# --- build_declared_payers / oracle_verdict -----------------------------


def test_build_declared_payers_wires_answers_and_ever_entitled(gate, libs, tmp_path):
    ENT, ORACLE, _, ST, MEM = libs
    hh = _hh(MEM, "h1", holder="pay@example.com", accounts=["pay@example.com"])
    roster = MEM.Roster(version=1, armed=False, grace_days=3, paused_sections=[],
                        households=[hh])
    state = ST.AccessState(path=tmp_path / "s.json")
    state.record_entitled("pay@example.com", now=NOW)
    answer = ENT.Answer(verdict=ENT.YES, email="pay@example.com", http_status=200)
    payers = gate.build_declared_payers(roster, state, {"h1": NOW},
                                        {"pay@example.com": answer})
    assert len(payers) == 1
    p = payers[0]
    assert p.household_id == "h1"
    assert p.ever_entitled is True
    assert p.currently_yes is True
    assert p.currently_never_seen is False


def test_oracle_verdict_end_to_end_matches_direct_judge_call(gate, libs, tmp_path):
    ENT, ORACLE, _, ST, MEM = libs
    hh = _hh(MEM, "h1", holder="pay@example.com", accounts=["pay@example.com"])
    roster = MEM.Roster(version=1, armed=False, grace_days=3, paused_sections=[],
                        households=[hh])
    state = ST.AccessState(path=tmp_path / "s.json")
    bulk = ENT.BulkAnswer(state=ENT.BULK_NO_SCOPE)
    v = gate.oracle_verdict(roster, state, {}, {}, bulk, NOW, settle_days=2)
    assert v.verdict == ORACLE.UNPROVEN_BLIND
    assert v.is_red is True


# --- arm_check_should_block (AC-19) -----------------------------------


def _plan(gate, state, *, mutates=False):
    return gate.Plan(email="x@example.com", state=state,
                     reason="test",
                     plex_target=([1] if mutates else None))


def test_arm_check_blocks_on_red_verdict_even_with_no_reductions(gate, libs):
    _, ORACLE, _, _, _ = libs
    red = ORACLE.Verdict(ORACLE.UNPROVEN_BLIND, "blind", True)
    assert gate.arm_check_should_block(red, []) is True


def test_arm_check_blocks_on_a_would_be_reduction_even_with_green_verdict(gate, libs):
    _, ORACLE, _, _, _ = libs
    green = ORACLE.Verdict(ORACLE.PROVEN, "fine", False)
    plans = [_plan(gate, gate.S_EXPIRED, mutates=True)]
    assert gate.arm_check_should_block(green, plans) is True


def test_arm_check_does_not_block_on_green_with_no_reductions(gate, libs):
    _, ORACLE, _, _, _ = libs
    green = ORACLE.Verdict(ORACLE.DORMANT, "nothing to prove", False)
    plans = [_plan(gate, gate.S_ENTITLED, mutates=False),
            _plan(gate, gate.S_PENDING, mutates=False)]
    assert gate.arm_check_should_block(green, plans) is False


def test_would_be_reduced_only_lists_expired_mutating_plans(gate):
    entitled_plan = gate.Plan(email="ok@example.com", state=gate.S_ENTITLED,
                              reason="t", plex_target=[1])          # mutates but not EXPIRED
    expired_plan = gate.Plan(email="bye@example.com", state=gate.S_EXPIRED,
                             reason="t", plex_target=[7])            # the real thing
    expired_noop = gate.Plan(email="already@example.com", state=gate.S_EXPIRED,
                             reason="t")                              # already at floor
    out = gate.would_be_reduced([entitled_plan, expired_plan, expired_noop])
    assert out == [gate.mask("bye@example.com")]


# --- AC-07: the oracle is INERT -----------------------------------------


def test_deadline_for_is_identical_with_and_without_oracle_state(libs, tmp_path):
    """AC-07, half one. AccessState.deadline_for()'s signature carries no
    oracle/bulk parameter at all -- assert that structurally, then prove it
    behaviourally: the same AccessState produces the same deadline whether or
    not an OracleState file exists on disk (this AccessState instance never
    reads it either way, but the behavioural check is the one the AC asks
    for)."""
    _, _, _, ST, _ = libs
    assert "oracle" not in inspect.signature(ST.AccessState.deadline_for).parameters

    state = ST.AccessState(path=tmp_path / "s.json")
    state.record_entitled("m@example.com", now=NOW - dt.timedelta(days=10))
    state.record_not_entitled("m@example.com", now=NOW - dt.timedelta(days=3))

    kw = dict(amnesty_until=None, grace_days=7, new_arrival_days=30, now=NOW)
    d1 = state.deadline_for("m@example.com", **kw)

    # "Without oracle state present" -- no oracle-state.json exists anywhere
    # near this state; "with" -- an OracleState is loaded and populated
    # alongside it. Neither changes deadline_for's inputs or output.
    import oracle_state as OSTATE
    osfile = tmp_path / "oracle-state.json"
    ostate = OSTATE.OracleState.load(osfile)
    ostate.observe(["some-household"], now=NOW)
    ostate.save()
    assert osfile.exists()

    d2 = state.deadline_for("m@example.com", **kw)
    assert d1 == d2


# --- CLI wiring -----------------------------------------------------------


def test_build_args_declares_arm_check_and_oracle_check_flags(gate):
    args = gate.build_args(["--arm-check"])
    assert args.arm_check is True
    assert args.oracle_check is False
    assert args.settle_days == gate.DEFAULT_SETTLE_DAYS

    args2 = gate.build_args(["--oracle-check", "--settle-days", "5"])
    assert args2.oracle_check is True
    assert args2.arm_check is False
    assert args2.settle_days == 5


def test_exit_arm_check_red_is_a_distinct_unused_code(gate):
    """Value 2 was unused by main()'s existing exit codes before this change
    (0, 1, 3, 4, 5) -- confirms --arm-check / --oracle-check did not collide
    with an existing meaning."""
    existing = {gate.EXIT_OK, gate.EXIT_PARTIAL, gate.EXIT_ENTITLEMENT_UNAVAILABLE,
               gate.EXIT_MEDIA_STACK_UNAVAILABLE, gate.EXIT_CONFIG}
    assert gate.EXIT_ARM_CHECK_RED == 2
    assert gate.EXIT_ARM_CHECK_RED not in existing


def test_oracle_check_and_arm_check_are_dispatched_before_any_plex_seerr_setup(gate):
    """Structural guard: main() must check args.oracle_check / args.arm_check
    before it ever builds a PlexShareClient or SeerrClient for the NORMAL
    (non-flag) path, so --oracle-check truly never touches Plex/Seerr."""
    src = inspect.getsource(gate.main)
    i_oracle = src.index("args.oracle_check")
    i_arm = src.index("args.arm_check")
    i_plex = src.index("PlexShareClient(")
    assert i_oracle < i_plex and i_arm < i_plex


def test_plan_for_share_signature_has_no_oracle_or_bulk_parameter(gate):
    """AC-07, half two, part A: plan_for_share cannot reach into oracle/bulk
    data it was never handed."""
    params = set(inspect.signature(gate.plan_for_share).parameters)
    for banned in ("oracle", "bulk", "verdict", "payer_oracle"):
        assert not any(banned in p.lower() for p in params), (
            "plan_for_share gained an oracle/bulk parameter: %s" % params)


@pytest.mark.parametrize("oracle_verdict_name", [
    "DORMANT", "PROVEN", "DEAD", "MISMATCH", "PROVEN_UPSTREAM",
    "SETTLING", "UNPROVEN_BLIND", "UNPROVEN_EMPTY",
])
def test_plan_outputs_are_identical_across_the_full_verdict_range(
        gate, libs, tmp_path, oracle_verdict_name):
    """AC-07, half two, part B. Compute a DIFFERENT oracle verdict on the
    side for each parametrized case (using unrelated, fabricated
    DeclaredPayer/BulkFacts inputs -- deliberately NOT derived from the share
    under test) and assert plan_for_share's mutation-relevant outputs
    (plex_target, seerr_target, provision_plex_id) never move. Since
    plan_for_share's signature (asserted above) has no oracle parameter, this
    is the behavioural half of the same proof: no side-channel exists either
    (e.g. a shared global), because computing an oracle verdict never mutates
    anything plan_for_share reads.
    """
    ENT, ORACLE, PS, ST, MEM = libs

    # An unrelated household proves whatever verdict this parametrization
    # names -- it shares no data with the share/household under test below.
    if oracle_verdict_name in ("DORMANT",):
        side_declared = []
    elif oracle_verdict_name == "PROVEN":
        side_declared = [ORACLE.DeclaredPayer("side", "side@example.com",
                                             currently_yes=True)]
    elif oracle_verdict_name == "DEAD":
        side_declared = [ORACLE.DeclaredPayer("side", "side@example.com",
                                             ever_entitled=True,
                                             currently_never_seen=True)]
    else:
        side_declared = [ORACLE.DeclaredPayer("side", "side@example.com",
                                             first_declared_at=NOW - dt.timedelta(days=30))]
    if oracle_verdict_name in ("MISMATCH", "PROVEN_UPSTREAM"):
        side_bulk = ORACLE.BulkFacts(ORACLE.BULK_OK, count=1,
                                     entitled=(("other@example.com",)
                                              if oracle_verdict_name == "MISMATCH"
                                              else ("side@example.com",)))
    elif oracle_verdict_name == "UNPROVEN_EMPTY":
        side_bulk = ORACLE.BulkFacts(ORACLE.BULK_OK, count=0)
    elif oracle_verdict_name == "SETTLING":
        side_declared = [ORACLE.DeclaredPayer("side", "side@example.com",
                                             first_declared_at=NOW - dt.timedelta(hours=1))]
        side_bulk = ORACLE.BulkFacts(ORACLE.BULK_NO_SCOPE)
    else:
        side_bulk = ORACLE.BulkFacts(ORACLE.BULK_NO_SCOPE)

    side_verdict = ORACLE.judge(declared=side_declared, bulk=side_bulk, now=NOW,
                               settle_days=2)
    assert side_verdict.verdict == oracle_verdict_name, (
        "test fixture for %s produced %s -- fix the fixture, not the assertion"
        % (oracle_verdict_name, side_verdict.verdict))

    # The REAL share/household/answer under test, computed identically
    # regardless of `side_verdict` above (which is never passed in).
    share = PS.Share(shared_server_id=1, user_id=2, email="real@example.com",
                     username="real", section_ids={4, 5}, all_libraries=False,
                     accepted_at=NOW - dt.timedelta(days=400),
                     invited_at=NOW - dt.timedelta(days=400))
    answer = ENT.Answer(verdict=ENT.YES, email="real@example.com", http_status=200)
    hh = MEM.Household(id="real", display="Real", exempt=False,
                       accounts=["real@example.com"],
                       billing=MEM.Billing(holder="real@example.com",
                                           amount_usd=5, rail="manual"))
    state = ST.AccessState(path=tmp_path / "s.json")

    plan = gate.plan_for_share(
        share=share, household=hh, answer=answer, seerr_user=None, state=state,
        full_ids=[4, 5], minimum_ids=[7], amnesty_until=None,
        grace_days=7, new_arrival_days=30, member_permissions=0, now=NOW)

    # Pin the expected outputs once, independent of the loop variable: the
    # share already carries the full section set, so nothing needs writing.
    assert plan.state == gate.S_ENTITLED
    assert plan.plex_target is None
    assert plan.provision_plex_id == 2
    assert plan.seerr_target is None
