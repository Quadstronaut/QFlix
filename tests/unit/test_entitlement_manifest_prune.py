"""Retention for the entitlement gate's audit manifests.

WHY THIS FILE EXISTS (2026-08-19)
---------------------------------
Live on the box: 1259 manifest-*.json, 14 MB, 1275 dirents in one flat
~/.opt/maint/entitlement/, every one of them written by a timer that fires 96
times a day and back to the first armed run on 2026-08-07. The 30-day age rule
that shipped alongside them had never fired -- and would not have helped when it
did, because 30 days at 96 runs a day is ~2880 files. An age rule bounds the
DAILY-rotated log; only a count bounds a PER-RUN artefact.

Each manifest is also a per-member decision record, so this is not only a disk
leak on a shared slot: it is member data accumulating with no chosen lifetime.

The four properties below are the whole contract, and each one is a real way a
prune goes wrong:

  * KEEPS THE NEWEST -- a prune that can delete the newest manifest can leave a
    state dir that cannot answer anything about the run that just happened.
    This is the only invariant that holds under EVERY rule, including the age
    rule, including a restored backup whose mtimes are all ancient.
  * PRUNES TO N -- the actual bound. Off-by-one here is how "bounded" quietly
    becomes "bounded plus one per run".
  * IDEMPOTENT -- it runs 96 times a day. A prune that deletes something on the
    second pass over an unchanged directory is a prune that eventually reaches
    zero.
  * NEVER PRUNES BELOW THE CAP -- deleting history from a directory that is
    already under budget is pure loss with no benefit.

NOTHING IN THIS FILE MAY NAME A REAL MEMBER. The fixtures write empty manifests
on purpose: retention is a property of the FILE SET, and putting plausible
member data in a test fixture is how a real address eventually gets pasted in.
"""
import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "maint" / "lib"))


def _load_gate():
    """Import qflix-entitlement.py by path (the hyphen makes it un-importable
    by name). Registered in sys.modules BEFORE exec so @dataclass can resolve
    its own annotations."""
    p = ROOT / "scripts" / "maint" / "qflix-entitlement.py"
    spec = importlib.util.spec_from_file_location("qflix_entitlement_prune", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


def _seed(d: Path, n: int, start_day: int = 1) -> list:
    """Write `n` manifests with ascending in-name UTC stamps.

    The names are what the prune sorts on, so they carry the ordering the test
    asserts. mtimes are left at "now" unless a test moves them: that is the
    normal case, and it also proves the ordering does not secretly depend on
    mtime.
    """
    base = dt.datetime(2026, 8, start_day, tzinfo=dt.timezone.utc)
    made = []
    for i in range(n):
        stamp = (base + dt.timedelta(minutes=15 * i)).strftime("%Y%m%dT%H%M%SZ")
        p = d / ("manifest-%s.json" % stamp)
        p.write_text("{}\n", encoding="utf-8")
        made.append(p)
    return made


def _names(d: Path) -> list:
    return sorted(p.name for p in d.glob("manifest-*.json"))


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------
def test_prunes_down_to_keep(gate, tmp_path):
    _seed(tmp_path, 20)
    removed = gate.prune_manifests(tmp_path, keep=5)
    assert removed == 15
    assert len(_names(tmp_path)) == 5


def test_the_survivors_are_the_newest_ones(gate, tmp_path):
    made = _seed(tmp_path, 20)
    gate.prune_manifests(tmp_path, keep=5)
    assert _names(tmp_path) == sorted(p.name for p in made[-5:])


def test_the_newest_manifest_is_never_deleted(gate, tmp_path):
    """The one invariant that survives every rule. Asserted at keep=1, the
    tightest possible cap, because that is where an off-by-one would take the
    last record with it."""
    made = _seed(tmp_path, 12)
    gate.prune_manifests(tmp_path, keep=1)
    assert _names(tmp_path) == [made[-1].name]


def test_a_keep_of_zero_still_leaves_the_newest(gate, tmp_path):
    """keep=0 is a caller bug (a mis-set constant, an env override). It must
    degrade to 'keep exactly one', never to 'empty the directory'."""
    made = _seed(tmp_path, 6)
    gate.prune_manifests(tmp_path, keep=0)
    assert _names(tmp_path) == [made[-1].name]


# ---------------------------------------------------------------------------
# Under budget
# ---------------------------------------------------------------------------
def test_never_prunes_when_count_equals_keep(gate, tmp_path):
    _seed(tmp_path, 5)
    before = _names(tmp_path)
    assert gate.prune_manifests(tmp_path, keep=5) == 0
    assert _names(tmp_path) == before


def test_never_prunes_when_count_is_below_keep(gate, tmp_path):
    _seed(tmp_path, 3)
    before = _names(tmp_path)
    assert gate.prune_manifests(tmp_path, keep=5) == 0
    assert _names(tmp_path) == before


def test_a_single_manifest_is_left_alone(gate, tmp_path):
    _seed(tmp_path, 1)
    assert gate.prune_manifests(tmp_path, keep=1) == 0
    assert len(_names(tmp_path)) == 1


def test_an_empty_state_dir_is_not_an_error(gate, tmp_path):
    assert gate.prune_manifests(tmp_path, keep=5) == 0


def test_a_missing_state_dir_is_not_an_error(gate, tmp_path):
    """Called on every run, including the first one on a fresh box. A prune
    that raises here takes the gate down before it has decided anything."""
    assert gate.prune_manifests(tmp_path / "does-not-exist", keep=5) == 0


# ---------------------------------------------------------------------------
# Idempotence -- it runs 96 times a day
# ---------------------------------------------------------------------------
def test_is_idempotent(gate, tmp_path):
    _seed(tmp_path, 30)
    first = gate.prune_manifests(tmp_path, keep=7)
    after_first = _names(tmp_path)
    assert first == 23
    for _ in range(3):
        assert gate.prune_manifests(tmp_path, keep=7) == 0
        assert _names(tmp_path) == after_first


def test_steady_state_holds_the_cap_across_many_runs(gate, tmp_path):
    """Write-then-prune, the real loop. The count must sit ON the cap forever,
    not drift up by one per run (prune-before-write) or down toward zero."""
    for day in range(1, 13):
        _seed(tmp_path, 4, start_day=day)
        gate.prune_manifests(tmp_path, keep=10)
        assert len(_names(tmp_path)) <= 10
    assert len(_names(tmp_path)) == 10


# ---------------------------------------------------------------------------
# The age rule that rides along
# ---------------------------------------------------------------------------
def test_manifests_older_than_the_log_retention_go_even_under_the_cap(gate, tmp_path):
    """Member decision records must not outlive the lifetime the logs beside
    them declare. Only matters if the timer cadence ever slows -- at 96 runs a
    day the count cap always bites first -- but the data-lifetime promise is
    the reason the rule is here, not the disk."""
    made = _seed(tmp_path, 4)
    ancient = (dt.datetime.now(dt.timezone.utc).timestamp()
               - (gate.LOG_RETENTION_DAYS + 5) * 86400)
    for p in made[:2]:
        import os
        os.utime(p, (ancient, ancient))
    removed = gate.prune_manifests(tmp_path, keep=100)
    assert removed == 2
    assert _names(tmp_path) == sorted(p.name for p in made[2:])


def test_the_newest_survives_even_if_every_file_is_ancient(gate, tmp_path):
    """A restored backup or a clock step makes every mtime old at once. The
    age rule must not be able to empty the directory."""
    import os
    made = _seed(tmp_path, 5)
    ancient = (dt.datetime.now(dt.timezone.utc).timestamp()
               - (gate.LOG_RETENTION_DAYS + 400) * 86400)
    for p in made:
        os.utime(p, (ancient, ancient))
    gate.prune_manifests(tmp_path, keep=100)
    assert _names(tmp_path) == [made[-1].name]


# ---------------------------------------------------------------------------
# Blast radius
# ---------------------------------------------------------------------------
def test_only_manifests_are_touched(gate, tmp_path):
    """state.json, oracle-state.json and the durable logs live in the same
    directory. A glob that widened by one character would delete the clocks
    the whole gate depends on."""
    _seed(tmp_path, 12)
    keepers = ["state.json", "oracle-state.json", "entitlement-2026-08-19.log",
               "lock", "manifest-README.txt"]
    for name in keepers:
        (tmp_path / name).write_text("x", encoding="utf-8")
    gate.prune_manifests(tmp_path, keep=2)
    for name in keepers:
        assert (tmp_path / name).exists(), name
    assert len(_names(tmp_path)) == 2


def test_default_keep_covers_the_grace_window_at_full_resolution(gate, tmp_path):
    """The constant is not arbitrary and the header says why: grace_days (7 in
    the live roster) x 96 runs a day. Pinned so a future 'let's trim it' has to
    argue with the reconstructability requirement instead of just editing a
    number."""
    assert gate.MANIFEST_RETENTION_RUNS == 7 * 96
    _seed(tmp_path, 3)
    assert gate.prune_manifests(tmp_path) == 0


# ---------------------------------------------------------------------------
# The lapse clock under a lookup MISS
#
# Rides along in this file because retention and this share one root cause --
# both are the gate keeping a durable record whose lifetime or truth nobody
# checked -- and because the repo's file ownership put both fixes in one place.
# A miss is UNKNOWN. The DECISION to freeze it lives in plan_for_share()
# (S_UNKNOWN_PAYER); this covers the half underneath, in compute_plans(), where
# a miss used to be written into state as a clean negative verdict.
# ---------------------------------------------------------------------------
import types

import access_state as ST
import entitlement as ENT

MEMBER = "member@example.invalid"          # RFC 2606 - never a real address
HOLDER = "holder@example.invalid"


def _answer(*, reason=None, status=None):
    """A clean NO. reason='unknown' is what the service returns for an address
    it has no record of; anything else is a real negative verdict."""
    return ENT.Answer(verdict=ENT.NO, email=HOLDER, http_status=200,
                      reason=reason, status=status)


def _world(state, answer):
    """The minimum compute_plans() needs: one non-exempt household, one
    accepted share, one canned entitlement answer."""
    household = types.SimpleNamespace(
        id="hh-test", exempt=False, provisional=False, accounts=[MEMBER],
        billing=types.SimpleNamespace(holder=HOLDER, rail="patreon",
                                      amount_usd=5, payer_ref="ref"),
        is_plex_only=lambda email: False)
    roster = types.SimpleNamespace(
        households=[household], grace_days=7,
        by_email=lambda: {MEMBER: household})
    share = types.SimpleNamespace(email=MEMBER, accepted=True, user_id=1,
                                  section_ids=[9], accepted_at=None)
    client = types.SimpleNamespace(lookup=lambda holder: answer)
    return dict(roster=roster, state=state, ent_client=client, shares=[share],
                full_ids=[1, 2], minimum_ids=[9], amnesty=None, grace_days=7,
                new_arrival_days=14, member_permissions=32,
                by_plex_id={}, by_email={}, now=dt.datetime.now(dt.timezone.utc))


def _entitled_then(gate, tmp_path, answer):
    """Make MEMBER ever-entitled, then run one cycle against `answer`. Returns
    (plans, account-state). ever_entitled is the precondition that arms
    went_false_at -- a never-entitled account cannot show this defect."""
    state = ST.AccessState.load(tmp_path / "state.json")
    state.record_entitled(MEMBER)
    plans, _, _ = gate.compute_plans(**_world(state, answer))
    return plans, state.accounts.get(MEMBER.lower())


def test_a_miss_is_graded_NO_by_the_client(gate):
    """Pins the trap this fix exists for. lib/entitlement.py deliberately
    grades a miss as a clean NO, so `revokes` is True and any caller that
    branches on it alone treats an absence of evidence as a verdict."""
    miss = _answer(reason="unknown")
    assert miss.never_seen is True
    assert miss.revokes is True          # <- why the guard cannot be implicit
    assert miss.grants is False


def test_a_miss_does_not_start_the_lapse_clock(gate, tmp_path):
    """The freeze S_UNKNOWN_PAYER promises, asserted on the state file. If
    went_false_at is stamped here, a member whose entitlement projection died
    burns their whole grace during the outage and is reducible the moment the
    service answers again."""
    plans, acct = _entitled_then(gate, tmp_path, _answer(reason="unknown"))
    assert acct.went_false_at is None
    assert acct.first_not_entitled_at is None
    assert [p.state for p in plans] == [gate.S_UNKNOWN_PAYER]


def test_a_real_negative_verdict_still_starts_the_lapse_clock(gate, tmp_path):
    """The control. The guard must be narrow: it exempts a MISS, not every
    no. Without this, 'freeze on unknown' could be silently widened into
    'never revoke anyone' and the tests would still be green."""
    plans, acct = _entitled_then(gate, tmp_path,
                                 _answer(status="former_patron"))
    assert acct.went_false_at is not None
    assert acct.first_not_entitled_at is not None
    assert [p.state for p in plans] == [gate.S_PENDING]


def test_a_miss_never_reaches_a_reducing_state(gate, tmp_path):
    """The whole point, stated as the property an operator cares about: no
    plan built from a miss may carry a Plex or Seerr reduction."""
    plans, _ = _entitled_then(gate, tmp_path, _answer(reason="unknown"))
    p = plans[0]
    assert p.plex_target is None
    assert p.seerr_target is None
    assert p.never_seen is True
    assert gate.unknown_payers(plans) == [gate.mask(MEMBER)]
