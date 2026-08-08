"""scripts/canaries/entitlement-service.sh -- structural + mutation-proof
guards (AC-09, AC-10).

The canary's remote body is bash (already gated for syntax by
test_canary_sshm_quoting.py). What THIS file adds:

  * AC-09: five legs are present, each failure path emits a STAGE= token,
    exit 1 is a real fault / exit 2 is could-not-assert, and the oracle leg
    delegates to lib/payer_oracle.judge() (via qflix-entitlement.py
    --oracle-check) rather than re-implementing the verdict table in bash.
  * AC-10: MUTATION PROOF. Feed the exact live-shaped inputs (3 declared
    payers, 0 ever entitled, bulk='no-scope') straight into
    lib/payer_oracle.judge() -- the same function the shell leg delegates to
    -- and assert the verdict is RED. A green canary result after the fix
    means something only if this can be shown to fail on today's real data.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(REPO_ROOT, "scripts", "maint", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

SCRIPT = os.path.join(REPO_ROOT, "scripts", "canaries", "entitlement-service.sh")

import payer_oracle as ORACLE  # noqa: E402


def _read():
    with open(SCRIPT, encoding="utf-8") as f:
        return f.read()


# --- AC-09: five legs, STAGE= tokens, exit-code convention, delegation -----


def test_script_declares_five_legs_in_the_header():
    src = _read()
    assert "FIVE LEGS" in src
    for leg in ("liveness", "auth-neg", "auth-pos", "contract", "oracle"):
        assert leg in src, "leg %r not named in the header" % leg


def test_leg2_auth_negative_control_probes_without_a_bearer_header():
    """AC-09's new leg: an unauthenticated lookup must 401/403."""
    src = _read()
    i = src.find("NOAUTH_CODE=$(curl")
    assert i != -1, "the unauthenticated probe (NOAUTH_CODE) was not found"
    call_line_end = src.find("\n", i)
    call_line = src[i:call_line_end]
    assert "Authorization" not in call_line, (
        "the auth-negative probe must NOT send a Bearer token: %r" % call_line)
    following = src[call_line_end:call_line_end + 400]
    assert "401" in following and "403" in following


def test_every_failure_path_emits_a_stage_token():
    src = _read()
    # Every `exit 1` or `exit 2` line must be preceded (within a few lines) by
    # a STAGE= printf. Cheap structural proxy: count STAGE= occurrences vs
    # `exit 1`/`exit 2` occurrences inside the sshm body (excluding the final
    # `exit $RC` outside it, and the `exit 0` PASS paths).
    body_start = src.index("sshm '")
    body = src[body_start:]
    fault_exits = len(re.findall(r"exit 1\b", body))
    assert_exits = len(re.findall(r"exit 2\b", body))
    stage_tokens = len(re.findall(r"STAGE=[a-zA-Z0-9-]+", body))
    assert stage_tokens >= fault_exits + assert_exits, (
        "fewer STAGE= tokens (%d) than fault/could-not-assert exits (%d) -- "
        "some failure path is silent" % (stage_tokens, fault_exits + assert_exits))


def test_exit_1_is_real_fault_exit_2_is_could_not_assert_per_docstring():
    src = _read()
    assert "1 - service down" in src or "1 -   service down" in src or \
        re.search(r"1 - .*(down|failed|violat|red)", src)
    assert re.search(r"2 - could not assert", src)


def test_oracle_leg_delegates_to_payer_oracle_judge_not_reimplemented():
    """The load-bearing AC-09 requirement: no bash re-implementation of the
    verdict table. The leg must invoke qflix-entitlement.py --oracle-check
    (which itself calls lib.payer_oracle.judge()) rather than parsing
    reason=unknown counts and thresholds directly in the shell body."""
    src = _read()
    i = src.find("leg 5: the payer oracle")
    assert i != -1, "oracle leg not found"
    block = src[i:]
    assert "--oracle-check" in block
    assert "qflix-entitlement.py" in block
    # The OLD implementation looped over addresses and counted FORGOTTEN vs
    # CHECKED directly in bash. That pattern must be gone.
    assert "FORGOTTEN=0" not in block
    assert "CHECKED=0" not in block


def test_oracle_leg_never_prints_an_unmasked_address():
    """Leg 5's own text must carry no '@' literal that could smuggle a real
    address into the script (the probe addresses used elsewhere are already
    RFC 2606 .invalid, checked by test_no_pii_in_repo.py for the whole repo;
    this pins that the oracle leg specifically adds none of its own)."""
    src = _read()
    i = src.find("leg 5: the payer oracle")
    block = src[i:src.find("RC=$?")]
    # Only the shell-safe DETAIL_LINE plumbing may reference addresses, and it
    # only ever forwards an ALREADY-MASKED string produced by
    # qflix-entitlement.py --oracle-check (which itself only ever emits
    # payer_oracle.mask()ed text). No literal @ appears in this leg's source.
    assert "@" not in block


# --- AC-10: mutation proof --------------------------------------------------


def test_mutation_proof_live_shaped_inputs_yield_a_red_verdict():
    """The exact live shape from SPEC section 1: 3 declared payers, 0 ever
    entitled, bulk='no-scope'. Feeding this into the SAME function the canary
    leg delegates to must produce a RED verdict -- proving a green canary
    result after the fix is not vacuous."""
    now = dt.datetime(2026, 8, 7, tzinfo=dt.timezone.utc)
    declared = [
        ORACLE.DeclaredPayer(household_id="h%d" % i, holder="payer%d@example.com" % i,
                            first_declared_at=now - dt.timedelta(days=10))
        for i in range(3)
    ]
    bulk = ORACLE.BulkFacts(state=ORACLE.BULK_NO_SCOPE)
    v = ORACLE.judge(declared=declared, bulk=bulk, now=now, settle_days=2)
    assert v.is_red is True
    assert v.canary_exit == 1
    assert v.verdict == ORACLE.UNPROVEN_BLIND


def test_mutation_proof_a_healthy_shape_would_pass():
    """The other half of the proof: a genuinely healthy shape (one declared
    payer currently reading entitled) must NOT be red, so the guard above is
    discriminating and not just always-red."""
    now = dt.datetime(2026, 8, 7, tzinfo=dt.timezone.utc)
    declared = [ORACLE.DeclaredPayer(household_id="h1", holder="a@example.com",
                                    currently_yes=True)]
    bulk = ORACLE.BulkFacts(state=ORACLE.BULK_NO_SCOPE)
    v = ORACLE.judge(declared=declared, bulk=bulk, now=now, settle_days=2)
    assert v.is_red is False
