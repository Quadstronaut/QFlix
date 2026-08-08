"""lib/payer_oracle.py -- "has the money path ever demonstrated a success?"

Pure stdlib. `judge()` performs NO network I/O and NO file I/O -- every fact
it needs is handed to it by the caller as plain dataclasses. That purity is
the whole point: SPEC section 3 requires ONE implementation of the verdict
table consumed by BOTH the gate (qflix-entitlement.py --oracle-check /
--arm-check) and the canary (scripts/canaries/entitlement-service.sh, via a
python3 invocation on the box). The REA lesson this project already paid for
once is that a policy replicated across two surfaces drifts by default; the
fix is not "keep them in sync", it is "there is exactly one copy of the
table, and both surfaces call it".

THE THREE ORACLE LAYERS THIS TABLE ARBITRATES BETWEEN
-------------------------------------------------------
  L1  declared-payer clock   zero new credentials, works from day one.
  L2  bulk cross-check       the real oracle, once the 'bulk' scope lands.
  L3  forgotten-patron check the original canary leg, threshold corrected
                             from ALL-forgotten to ANY-forgotten (AC-04).

WHY judge() NEVER TOUCHES THE NETWORK
--------------------------------------
Every fact below -- who is a declared payer, whether they have ever been
entitled, whether the CURRENT lookup says yes or "never seen", and what the
bulk endpoint currently reports -- was already fetched by the caller for its
own purposes (the gate already looks up every declared payer every 15
minutes; the canary already probes /v1/entitlements for its contract leg).
Handing judge() live objects instead of pre-computed facts would make it
untestable without a fake HTTP server and would silently reintroduce the
two-surfaces problem the moment somebody "simplified" one call site to skip
a step the other one still does.

PII DISCIPLINE
---------------
`DeclaredPayer.holder` and `BulkFacts.entitled` carry REAL, unmasked
addresses -- judge() needs the real values to do an accurate set comparison
for MISMATCH (row 4). What judge() RETURNS never does: `Verdict.detail` is
built exclusively through `mask()` below, and every test in
tests/unit/test_payer_oracle.py asserts that no full address and no
household id survives into it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from typing import List, Optional, Sequence

# --- verdicts, per SPEC section 3's table, first-match-wins -----------------
DORMANT = "DORMANT"
PROVEN = "PROVEN"
DEAD = "DEAD"
MISMATCH = "MISMATCH"
PROVEN_UPSTREAM = "PROVEN_UPSTREAM"
SETTLING = "SETTLING"
UNPROVEN_BLIND = "UNPROVEN_BLIND"
UNPROVEN_EMPTY = "UNPROVEN_EMPTY"

# The canary's fault legs (rows 3, 4, 7, 8). Everything else is a PASS.
RED_VERDICTS = frozenset({DEAD, MISMATCH, UNPROVEN_BLIND, UNPROVEN_EMPTY})

DEFAULT_SETTLE_DAYS = 2

# Must track lib.entitlement.BulkAnswer.state's vocabulary exactly. Not
# imported from there on purpose -- this module stays a standalone, dependency
# -free unit so it can be copied to the box beside entitlement.py without
# pulling in urllib request machinery it never uses. test_payer_oracle.py pins
# the two vocabularies against each other so they cannot silently diverge.
BULK_OK = "ok"
BULK_NO_SCOPE = "no-scope"
BULK_UNREACHABLE = "unreachable"
BULK_UNPARSEABLE = "unparseable"


def mask(value: Optional[str]) -> str:
    """Same masking law as everywhere else: two local-part characters and the
    domain. Never enough to identify the person from the log line alone."""
    if not value or "@" not in value:
        return "?"
    local, _, domain = value.partition("@")
    keep = local[:2] if len(local) > 2 else local[:1]
    return "%s***@%s" % (keep, domain)


@dataclass
class DeclaredPayer:
    """One L1 declared payer: non-exempt, non-provisional, billing.rail set,
    billing.amount_usd > 0 (SPEC section 3, L1).

    `household_id` is carried for the caller's own bookkeeping but judge()
    never emits it -- AC-02 requires the detail line to name no household id,
    the same PII law as everywhere else in this repo (see the L-1 ledger
    entry: `digest_lines()` already leaks household_id into Discord and that
    is a known, scope-fenced defect this module does not repeat).
    """

    household_id: str
    holder: str = field(repr=False)      # real address; never leaves judge() unmasked
    first_declared_at: Optional[dt.datetime] = None
    ever_entitled: bool = False          # access_state.AccountState.ever_entitled, for this holder
    currently_yes: bool = False          # THIS run's Answer.grants for this holder
    currently_never_seen: bool = False   # THIS run's Answer.never_seen for this holder


@dataclass
class BulkFacts:
    """The graded result of EntitlementClient.bulk(), reduced to what judge()
    needs. Build with `BulkFacts.from_bulk_answer()` if you have a real
    lib.entitlement.BulkAnswer; the fields are duplicated by hand instead of
    imported so this module has zero dependency on entitlement.py.
    """

    state: str
    count: Optional[int] = None
    entitled: Sequence[str] = field(default_factory=tuple, repr=False)   # real addresses

    @property
    def supported(self) -> bool:
        return self.state == BULK_OK

    @classmethod
    def from_bulk_answer(cls, answer) -> "BulkFacts":
        """Adapter for a real lib.entitlement.BulkAnswer. Kept here rather than
        in entitlement.py so entitlement.py never needs to import this module
        (the dependency runs one way: gate -> both leaves, never leaf -> leaf).
        """
        return cls(state=answer.state, count=answer.count,
                   entitled=tuple(answer.entitled or ()))


@dataclass
class Verdict:
    verdict: str
    detail: str
    is_red: bool

    @property
    def canary_exit(self) -> int:
        """0 for a PASS, 1 for a real fault -- matches the house convention
        (exit 2 is reserved for could-not-assert, which is a property of the
        CALLER's I/O, not of this pure decision)."""
        return 1 if self.is_red else 0


def _hours_remaining(age: dt.timedelta, settle_days: int) -> float:
    remaining = dt.timedelta(days=settle_days) - age
    return max(0.0, remaining.total_seconds() / 3600.0)


def judge(*, declared: Sequence[DeclaredPayer], bulk: BulkFacts,
         now: dt.datetime, settle_days: int = DEFAULT_SETTLE_DAYS) -> Verdict:
    """The SPEC section 3 verdict table. First match wins. Pure: no I/O."""
    D = list(declared)

    # --- row 1 --------------------------------------------------------------
    if not D:
        return Verdict(DORMANT,
                       "no declared payers yet (0 households with a billing "
                       "rail and amount_usd > 0) - nothing to prove", False)

    # --- row 2 ---------------------------------------------------------------
    yes_now = [d for d in D if d.currently_yes]
    if yes_now:
        return Verdict(PROVEN,
                       "%d of %d declared payer(s) currently read entitled - "
                       "the money path works" % (len(yes_now), len(D)),
                       False)

    E = [d for d in D if d.ever_entitled]

    # --- row 3 -----------------------------------------------------------
    forgotten = sorted((d for d in E if d.currently_never_seen),
                       key=lambda d: d.holder)
    if E and forgotten:
        example = mask(forgotten[0].holder)
        return Verdict(
            DEAD,
            "%d of %d ever-entitled declared payer(s) now read as NEVER SEEN "
            "by the service (e.g. %s) - the sync projection lost them, this "
            "is not an ordinary lapse" % (len(forgotten), len(E), example),
            True)

    # --- row 4 -----------------------------------------------------------
    if bulk.supported and (bulk.count or 0) > 0:
        declared_holders = {d.holder.strip().lower() for d in D if d.holder}
        bulk_addrs = {a.strip().lower() for a in bulk.entitled if a}
        if bulk_addrs and not (declared_holders & bulk_addrs):
            example = mask(sorted(bulk_addrs)[0])
            return Verdict(
                MISMATCH,
                "the service knows %d entitled account(s) and NONE matches a "
                "declared billing.holder (e.g. %s) - a member may be paying "
                "under a different address and is on a clock to be reduced "
                "while paying" % (bulk.count, example),
                True)

        # --- row 5 -------------------------------------------------------
        return Verdict(
            PROVEN_UPSTREAM,
            "the bulk endpoint carries %d entitled account(s) and at least "
            "one matches a declared billing.holder - proven upstream"
            % bulk.count,
            False)

    # --- row 6 -----------------------------------------------------------
    declared_ats = [d.first_declared_at for d in D if d.first_declared_at is not None]
    if declared_ats:
        oldest = min(declared_ats)
        age = now - oldest
    else:
        # No declaration timestamp recorded at all (fresh install, or the
        # declared-payer clock has not observed a run yet). Treat as maximally
        # old rather than maximally young: the SAFE direction for a settle
        # window is to let it lapse, not to hold every red verdict open
        # forever because a clock never got a chance to start.
        age = dt.timedelta(days=settle_days + 1)
    if age < dt.timedelta(days=settle_days):
        return Verdict(
            SETTLING,
            "oldest declaration is %.1f hour(s) old; %.1f hour(s) remain "
            "before the %d-day settle window closes"
            % (age.total_seconds() / 3600.0, _hours_remaining(age, settle_days),
               settle_days),
            False)

    # --- row 7 -----------------------------------------------------------
    if not bulk.supported and not E:
        if bulk.state == BULK_NO_SCOPE:
            why = "the QFlix entitlement key lacks the 'bulk' scope"
        elif bulk.state == BULK_UNPARSEABLE:
            why = "the bulk endpoint returned an unparseable body"
        else:
            why = "the bulk endpoint is unreachable"
        return Verdict(
            UNPROVEN_BLIND,
            "the money path has never demonstrated a success and cannot "
            "currently be cross-checked (%s). Fix: grant the QFlix "
            "entitlement key the 'bulk' scope on Starhold - GET "
            "/v1/entitlements today answers 403 \"this key lacks the "
            "'bulk' scope\"" % why,
            True)

    # --- row 8 -----------------------------------------------------------
    if bulk.supported and (bulk.count or 0) == 0 and not E:
        return Verdict(
            UNPROVEN_EMPTY,
            "the bulk endpoint answers but currently lists 0 entitled "
            "account(s), and no declared payer has ever been entitled - "
            "either nobody has completed payment yet, or billing.holder "
            "does not match what patrons actually pay with",
            True)

    # --- fallback: previously proven, nothing new to report this run --------
    # Not one of the 8 numbered rows. Reachable only when SOME declared payer
    # was entitled at some point (E non-empty), none of them is currently
    # forgotten (row 3 did not fire), nobody currently reads YES (row 2 did
    # not fire -- they have since lapsed, which is normal), and the bulk
    # cross-check happens to be unavailable or empty on this one run. History
    # already proved the path works; a single blind/empty cross-check on a
    # run where the last known state was healthy is not itself new evidence
    # of a fault, so this stays green rather than joining rows 7/8, which are
    # about a path that has NEVER been proven.
    return Verdict(
        PROVEN,
        "no fresh signal this run, but %d declared payer(s) were entitled at "
        "some point and none reads as forgotten - the money path has already "
        "demonstrated it works" % len(E),
        False)
