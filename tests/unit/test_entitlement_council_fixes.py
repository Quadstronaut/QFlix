"""Second review wave: hazards from the council's Stage-0 arbiter (2026-08-07).

The council's generators died on a session limit, so it never reached a verdict.
Its Stage-0 arbiter finished first and reproduced five hazards BY EXECUTION,
which is worth more than a verdict reached by argument. Each is fixed here and
pinned by a test that names the harm.

The highest-severity one is not in the code at all: `armed: true` cannot arm the
gate on its own, because `gate_is_armed()` also requires zero unresolved
households and every non-exempt household carries `rail: null`. The danger is
not the inert gate -- it is what an operator does after flipping the switch and
seeing nothing happen.

NOTHING IN THIS FILE MAY NAME A REAL MEMBER.
"""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "maint" / "lib"))

import access_state as ST      # noqa: E402
import entitlement as ENT      # noqa: E402
import plexshare as PS         # noqa: E402
import seerrusers as SU        # noqa: E402

NOW = dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.timezone.utc)
LONG_AGO = dt.datetime(2026, 2, 10, tzinfo=dt.timezone.utc)
GATE_SRC = (ROOT / "scripts" / "maint" / "qflix-entitlement.py").read_text(encoding="utf-8")


def _gate(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "maint" / "qflix-entitlement.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _kw(**over):
    base = dict(amnesty_until=None, grace_days=7, new_arrival_days=30, now=NOW)
    base.update(over)
    return base


class _Billing:
    holder = "payer@example.com"
    amount_usd = 5.0
    rail = "patreon"
    payer_ref = "x"


class _Household:
    id = "h"
    exempt = False
    accounts = ["member@example.com"]
    billing = _Billing()
    reason = "test"
    display = "h"
    plex_only_emails = frozenset()

    def is_plex_only(self, email):
        return email.lower() in self.plex_only_emails


# ---------------------------------------------------------------------------
# H1 (HIGH) -- zero-grace re-seed
# ---------------------------------------------------------------------------

def test_a_freshly_recorded_account_cannot_be_reduced_however_old_its_acceptance(tmp_path):
    """`first_seen_accepted` is Plex's HISTORICAL acceptedAt.

    An account row that goes missing from state.json while `first_run_at`
    survives re-seeds as ARRIVAL anchored to that historical date, so
    acceptedAt + 30d lands MONTHS in the past and the account is reducible on
    its first clean NO -- with zero of its promised thirty days.
    LAUNCH_FLOOR_DAYS did not cover it: that guards the launch cohort only.

    The trigger is mundane, not exotic: a member changes their Plex account
    email. State is keyed by email, so the old row is orphaned and a new one
    appears with a months-old acceptance date.
    """
    st = ST.AccessState.load(tmp_path / "s.json")
    st.first_run_at = NOW - dt.timedelta(days=400)          # long-established gate
    st.seed([("moved@example.com", LONG_AGO)], now=NOW)     # recorded TODAY
    acct = st.get("moved@example.com")
    assert acct.cohort == ST.COHORT_ARRIVAL
    assert acct.first_seen_accepted == LONG_AGO

    deadline = st.deadline_for("moved@example.com", **_kw())
    assert deadline > NOW, (
        "an account first RECORDED today was immediately reducible because its "
        "acceptedAt is historical (deadline %s)" % deadline)
    assert deadline == NOW + dt.timedelta(days=ST.TRACKING_FLOOR_DAYS)
    assert not st.is_expired("moved@example.com", **_kw())


def test_the_tracking_floor_never_shortens_a_longer_clock(tmp_path):
    """A floor is a floor. A genuine new arrival keeps its full thirty days."""
    st = ST.AccessState.load(tmp_path / "s.json")
    st.first_run_at = NOW - dt.timedelta(days=400)
    st.seed([("new@example.com", NOW)], now=NOW)
    assert st.deadline_for("new@example.com", **_kw()) == NOW + dt.timedelta(days=30)


def test_state_from_an_older_version_upgrades_safely(tmp_path):
    """A state file written before `first_recorded_at` existed must be protected
    by the upgrade, not exposed by it."""
    st = ST.AccessState.load(tmp_path / "s.json")
    st.seed([("a@example.com", LONG_AGO)], now=NOW)
    st.save()
    assert ST.AccessState.load(tmp_path / "s.json").get("a@example.com").first_recorded_at == NOW

    p = tmp_path / "old.json"
    p.write_text(json.dumps({
        "schema": 1,
        "first_run_at": "2026-08-06T00:00:00Z",
        "accounts": {"a@example.com": {"first_seen_accepted": "2026-02-10T00:00:00Z",
                                       "cohort": "arrival"}},
    }), encoding="utf-8")
    old = ST.AccessState.load(p)
    assert old.get("a@example.com").first_recorded_at is None
    assert not old.is_expired("a@example.com", **_kw(now=NOW)), \
        "the fallback to first_run_at must still yield a future deadline"


# ---------------------------------------------------------------------------
# H3 (HIGH) -- a grant that demotes
# ---------------------------------------------------------------------------

def _plan_with(full_ids, holds, tmp_path, name):
    m = _gate(name)
    share = PS.Share(shared_server_id=1, user_id=7, email="member@example.com",
                     username="u", section_ids=set(holds), all_libraries=False,
                     accepted_at=LONG_AGO)
    st = ST.AccessState.load(tmp_path / "s.json")
    st.first_run_at = NOW - dt.timedelta(days=400)
    st.seed([("member@example.com", LONG_AGO)], now=NOW - dt.timedelta(days=300))
    return m, m.plan_for_share(
        share=share, household=_Household(),
        answer=ENT.Answer(verdict=ENT.YES, email="payer@example.com"),
        seerr_user=SU.SeerrUser(id=1, email="member@example.com", username="u",
                                permissions=SU.MEMBER_PERMISSIONS,
                                user_type=1, plex_id=7),
        state=st, full_ids=full_ids, minimum_ids=[full_ids[0]],
        amnesty_until=None, grace_days=7, new_arrival_days=30,
        member_permissions=SU.MEMBER_PERMISSIONS, now=NOW)


def test_a_grant_may_never_remove_a_section(tmp_path):
    """`full_ids` is re-read from plex.tv on every run, and parse_sections()
    refuses only a ZERO-section catalogue -- so a poll returning 2 of 5 is
    accepted as truth, and an ENTITLED member holding 5 is planned down to 2
    with the log calling it "raising to full access".

    Against a third-party API polled 96 times a day, a short read is not a
    hypothetical. Reduction belongs to the expiry branch, behind the clocks,
    alone; the grant branch must never be able to take anything away.
    """
    m, p = _plan_with([101, 105], [101, 102, 103, 104, 105], tmp_path, "qe_h3a")
    assert p.state == m.S_ENTITLED
    assert p.plex_target is None, "a GRANT planned a reduction from 5 sections to 2"
    assert p.alert and "refusing to reduce an ENTITLED member" in p.alert
    assert "raising to full access" not in p.reason, \
        "a demotion must never be logged as a promotion"


def test_a_grant_may_still_add_sections(tmp_path):
    """The rail is a shape check, not a freeze. Growing must still work, or a
    new library would never reach anybody."""
    m, p = _plan_with([101, 102, 103], [101], tmp_path, "qe_h3b")
    assert p.plex_target == [101, 102, 103]
    assert p.alert is None


def test_a_grant_with_an_identical_set_still_emits_nothing(tmp_path):
    m, p = _plan_with([101, 102], [101, 102], tmp_path, "qe_h3c")
    assert p.plex_target is None
    assert p.alert is None


# ---------------------------------------------------------------------------
# H5 (MEDIUM) -- the backstop went silent at the moment of harm
# ---------------------------------------------------------------------------

def test_never_seen_is_still_shouted_when_the_reduction_happens():
    """The typo backstop alerted only while PENDING, so it fell silent on
    exactly the run that took the member's libraries away -- the one run where
    "we may have written their address down wrong" is worth saying out loud."""
    seg = GATE_SRC[GATE_SRC.index("expired_alert = None"):
                   GATE_SRC.index("return Plan(email=email, state=S_EXPIRED")]
    assert "answer.never_seen" in seg
    assert "NEVER SEEN" in seg
    assert "billing.holder" in seg, "say what to fix, not just that something is wrong"


# ---------------------------------------------------------------------------
# H4 (MED-HIGH) -- the unvalidated knobs
# ---------------------------------------------------------------------------

def test_clock_knobs_are_validated_before_they_can_move_a_deadline_earlier():
    """grace_days is validated by the roster loader. new_arrival_days and
    amnesty_until were read as raw YAML and bypassed every check -- backwards,
    because those two move a reduction EARLIER and grace_days only moves it
    later. A non-integer also raised inside main(), exiting 1 with no Kuma push:
    the same silent shape already fixed once for machineIdentifier.
    """
    seg = GATE_SRC[GATE_SRC.index("raw_nad = _roster_default"):
                   GATE_SRC.index("grace_days = roster.grace_days")]
    assert "raw_nad < 1" in seg, "zero and negative windows must be rejected"
    assert "isinstance(raw_nad, bool)" in seg, "True is an int in Python; reject it"
    assert seg.count("return EXIT_CONFIG") == 2, "both knobs must refuse the run"
    assert seg.count("_push_kuma") == 2, "a config refusal must never be silent"


def test_a_malformed_amnesty_is_refused_rather_than_treated_as_absent():
    """Deleting the key is how you retire the amnesty. A mistyped date must not
    read as the same intent -- that is the difference between a deliberate
    retirement and a typo that removes everyone's protection."""
    seg = GATE_SRC[GATE_SRC.index("raw_amnesty = _roster_default"):
                   GATE_SRC.index("grace_days = roster.grace_days")]
    assert "raw_amnesty is not None and amnesty is None" in seg
    assert "Delete the key to retire the amnesty" in seg


# ---------------------------------------------------------------------------
# G-2 (HIGH) -- the documentation defect, which is the worst of the five
# ---------------------------------------------------------------------------

def test_the_docstring_no_longer_claims_four_interlocks():
    """`armed: true` ALONE cannot arm the gate: gate_is_armed() also requires
    zero unresolved households, and every non-exempt household currently carries
    rail:null / amount_usd:null.

    The hazard is not the inert gate. It is what the operator does next after
    flipping the switch and seeing nothing happen: resolve all ten households in
    one edit, arming all ten simultaneously.
    """
    head = GATE_SRC[:GATE_SRC.index('"""', GATE_SRC.index('"""') + 3)]
    assert "Four interlocks" not in head
    assert "ZERO UNRESOLVED HOUSEHOLDS" in head
    assert "ONE AT A TIME" in head, "the mitigation must be stated, not implied"
    assert "--max-reduce-pct" in head, "the rail that catches it must be named too"


def test_the_runbook_agrees_with_the_code():
    """A runbook is executable instructions. One that understates the arming
    conditions is a defect of the same class as a code defect, because the
    operator executes it."""
    rb = (ROOT / "docs" / "entitlement-gate-runbook.md").read_text(encoding="utf-8")
    assert "unresolved" in rb.lower(), \
        "the runbook must state that armed:true alone does not arm the gate"
    assert "one at a time" in rb.lower(), \
        "the runbook must warn against resolving every household in one edit"
