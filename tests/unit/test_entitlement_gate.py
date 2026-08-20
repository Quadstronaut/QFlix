"""The decision table, and the two ways this system could evict somebody.

plan_for_share() is pure, so every branch that decides whether a real person
keeps their libraries is testable without a network, a token, or a live Plex.
That is the reason it is pure.

The tests are grouped by the harm they prevent:

  * NEVER SHRINK WHAT WE CANNOT NAME -- an accepted share with no roster
    household is a new arrival, not an intruder. Cutting it because a record is
    missing is how one typo evicts a real person.
  * NEVER ACT ON A NON-ANSWER -- an entitlement outage must move nothing.
  * NEVER WRITE AN EMPTY SECTION LIST -- plex.tv reads `[]` as "unshare the
    server", which deletes the share and evicts the person for real.
  * IDEMPOTENCE -- a plan that changes nothing must emit no mutation, or a
    15-minute cron rewrites fourteen shares 96 times a day.

NOTHING IN THIS FILE MAY NAME A REAL MEMBER.
"""
import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "maint" / "lib"))

import access_state as ST      # noqa: E402
import entitlement as ENT      # noqa: E402
import plexshare as PS         # noqa: E402
import seerrusers as SU        # noqa: E402


def _load_gate():
    p = ROOT / "scripts" / "maint" / "qflix-entitlement.py"
    spec = importlib.util.spec_from_file_location("qflix_entitlement", p)
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: @dataclass resolves annotations via
    # sys.modules[cls.__module__], which is None for a module that is still
    # being executed and not yet registered.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


G = _load_gate()

NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc)
AMNESTY = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)

WELCOME_ID = 999
FULL = [132919827, 132920523, 143790062, 143790063, WELCOME_ID]
MINIMUM = [WELCOME_ID]

SECTIONS = [
    PS.Section(id=132919827, key=2, title="QFlix - TV", type="show"),
    PS.Section(id=132920523, key=4, title="QFlix - Movies", type="movie"),
    PS.Section(id=143790062, key=6, title="QFlix - Anime", type="show"),
    PS.Section(id=143790063, key=5, title="QFlix - Anime Movies", type="movie"),
    PS.Section(id=WELCOME_ID, key=7, title="QFlix - Welcome", type="movie"),
]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def share(email="member@example.com", sections=None, accepted=True, user_id=7001):
    return PS.Share(
        shared_server_id=1, user_id=user_id, email=email, username="u",
        section_ids=set(FULL if sections is None else sections),
        all_libraries=False,
        accepted_at=(NOW - dt.timedelta(days=200)) if accepted else None,
        invited_at=NOW - dt.timedelta(days=201),
    )


def household(hid="h1", exempt=False, holder="pays@example.com", accounts=None,
              plex_only=()):
    billing = None if exempt else MEM_Billing(holder)
    return MEM_Household(hid, exempt, accounts or ["member@example.com"], billing,
                         plex_only=plex_only)


class MEM_Billing:
    def __init__(self, holder):
        self.holder = holder
        self.amount_usd = 5.0
        self.rail = "patreon"
        self.payer_ref = "x"


class MEM_Household:
    def __init__(self, hid, exempt, accounts, billing, plex_only=()):
        self.id = hid
        self.exempt = exempt
        self.accounts = accounts
        self.billing = billing
        self.reason = "test"
        self.display = hid
        self.plex_only_emails = frozenset(e.lower() for e in plex_only)

    def is_plex_only(self, email):
        return email.lower() in self.plex_only_emails


def answer(verdict, **kw):
    return ENT.Answer(verdict=verdict, email="pays@example.com", **kw)


def seerr_user(perms=SU.MEMBER_PERMISSIONS, uid=42):
    return SU.SeerrUser(id=uid, email="member@example.com", username="u",
                        permissions=perms, user_type=1, plex_id=7001)


def state_with(tmp_path, *, accepted_at=None, cohort=ST.COHORT_LAUNCH,
               ever_entitled=False, went_false=None, prior_perms=None):
    st = ST.AccessState.load(tmp_path / "s.json")
    # Realistic: production ALWAYS has first_run_at, because seed() sets it on
    # the first run and it is persisted before any early return. The
    # launch-cohort floor is measured from it, so a fixture without it does not
    # model anything that can actually occur.
    st.first_run_at = NOW - dt.timedelta(days=200)
    a = st.get("member@example.com")
    a.first_seen_accepted = accepted_at or (NOW - dt.timedelta(days=200))
    a.cohort = cohort
    if ever_entitled:
        a.last_entitled_at = NOW - dt.timedelta(days=30)
    a.went_false_at = went_false
    a.seerr_perms_prior = prior_perms
    return st


def plan(**over):
    kw = dict(
        share=share(), household=household(), answer=answer(ENT.YES),
        seerr_user=seerr_user(), state=None, full_ids=FULL, minimum_ids=MINIMUM,
        amnesty_until=AMNESTY, grace_days=7, new_arrival_days=30,
        member_permissions=SU.MEMBER_PERMISSIONS, now=NOW,
    )
    kw.update(over)
    return G.plan_for_share(**kw)


# ---------------------------------------------------------------------------
# NEVER SHRINK WHAT WE CANNOT NAME
# ---------------------------------------------------------------------------

def test_unnamed_share_is_never_shrunk_even_holding_every_library(tmp_path):
    """The out-of-band-share case. A share with four libraries and no roster row
    gets a loud page and ZERO mutation.

    The alternative -- shrink it -- closes the hole, and also cuts off any real
    member whose roster entry has a typo, was deleted, or has not been written
    yet. Since every new signup passes through this state by construction, the
    strict reading would evict every new member on their first day.
    """
    p = plan(household=None, state=state_with(tmp_path), answer=None,
             share=share(sections=FULL))
    assert p.state == G.S_UNNAMED
    assert p.plex_target is None, "an unnamed share must never be reduced"
    assert p.seerr_target is None
    assert p.alert and "unnamed share" in p.alert


def test_unnamed_share_at_the_floor_is_quiet_but_still_provisioned(tmp_path):
    p = plan(household=None, state=state_with(tmp_path), answer=None,
             share=share(sections=MINIMUM), seerr_user=None)
    assert p.state == G.S_UNNAMED
    assert p.alert is None, "a new arrival sitting at Welcome is not an incident"
    assert p.provision_plex_id == 7001, "but they still get a disabled Seerr account"


# ---------------------------------------------------------------------------
# NEVER ACT ON A NON-ANSWER
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ans", [
    None,
    ENT.Answer(verdict=ENT.UNKNOWN, email="p@example.com", error="timeout"),
    ENT.Answer(verdict=ENT.UNKNOWN, email="p@example.com", error="HTTP 429"),
    ENT.Answer(verdict=ENT.UNKNOWN, email="p@example.com",
               error="stale data reports not-entitled", stale=True),
])
def test_no_answer_moves_nothing_in_either_direction(tmp_path, ans):
    """An entitlement outage freezes the system. It does not drain it."""
    p = plan(answer=ans, state=state_with(tmp_path, ever_entitled=True,
                                          went_false=NOW - dt.timedelta(days=90)),
             share=share(sections=FULL))
    assert p.state == G.S_NO_ANSWER
    assert p.plex_target is None
    assert p.seerr_target is None


def test_no_answer_does_not_advance_the_lapse_clock(tmp_path):
    """Proved at the caller boundary: only a clean verdict writes to the clocks.

    record_not_entitled is what starts a countdown, and it is only ever reached
    via `answer.revokes`, which is False for UNKNOWN.
    """
    st = state_with(tmp_path, ever_entitled=True)
    unknown = ENT.Answer(verdict=ENT.UNKNOWN, email="p@example.com", error="boom")
    assert unknown.revokes is False
    assert st.get("member@example.com").went_false_at is None


# ---------------------------------------------------------------------------
# Exempt
# ---------------------------------------------------------------------------

def test_exempt_household_is_never_touched_even_when_not_entitled(tmp_path):
    """Operator directive: the owner, family, and comped households are never
    gated. Exempt must beat every clock and every verdict."""
    p = plan(household=household(exempt=True), answer=answer(ENT.NO),
             state=state_with(tmp_path), share=share(sections=FULL))
    assert p.state == G.S_EXEMPT
    assert p.plex_target is None and p.seerr_target is None


def test_exempt_household_is_not_even_provisioned(tmp_path):
    """Exempt is terminal, and that includes creating accounts.

    Caught by the first live run: the gate planned to create a Seerr account
    for the operator's own second Plex account, which deliberately has none.
    Exempt households are hand-managed by definition; a system that creates
    accounts for the people the operator explicitly carved out is doing
    something nobody asked for.
    """
    p = plan(household=household(exempt=True), answer=None, seerr_user=None,
             state=state_with(tmp_path))
    assert p.provision_plex_id is None
    assert not p.mutates


# ---------------------------------------------------------------------------
# Granting
# ---------------------------------------------------------------------------

def test_entitled_raises_to_every_section_that_exists(tmp_path):
    p = plan(answer=answer(ENT.YES), state=state_with(tmp_path),
             share=share(sections=MINIMUM), seerr_user=seerr_user(perms=0))
    assert p.state == G.S_ENTITLED
    assert p.plex_target == sorted(FULL)
    assert p.seerr_target == SU.MEMBER_PERMISSIONS


def test_entitled_restores_the_members_own_prior_permissions(tmp_path):
    """A hand-tuned account (extra quota, 4K rights) must not be flattened to
    the default just because they lapsed and came back."""
    custom = 1155539104
    p = plan(answer=answer(ENT.YES),
             state=state_with(tmp_path, prior_perms=custom),
             share=share(sections=MINIMUM), seerr_user=seerr_user(perms=0))
    assert p.seerr_target == custom


def test_stale_yes_still_grants(tmp_path):
    p = plan(answer=answer(ENT.YES, stale=True), state=state_with(tmp_path),
             share=share(sections=MINIMUM), seerr_user=seerr_user(perms=0))
    assert p.state == G.S_ENTITLED
    assert p.plex_target == sorted(FULL)


# ---------------------------------------------------------------------------
# Withholding, then reducing
# ---------------------------------------------------------------------------

def test_not_entitled_inside_the_amnesty_changes_nothing(tmp_path):
    """The launch cohort on 20 August: not entitled, amnesty runs to 1 September.
    Every existing member must be untouched on day one."""
    p = plan(answer=answer(ENT.NO, status="former_patron"),
             state=state_with(tmp_path), share=share(sections=FULL))
    assert p.state == G.S_PENDING
    assert p.plex_target is None and p.seerr_target is None
    assert 11 < p.days_remaining < 13


def test_expired_reduces_to_welcome_and_disables_seerr(tmp_path):
    after = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
    p = plan(answer=answer(ENT.NO, status="former_patron"),
             state=state_with(tmp_path), share=share(sections=FULL), now=after)
    assert p.state == G.S_EXPIRED
    assert p.plex_target == MINIMUM
    assert p.seerr_target == SU.PERMISSIONS_DISABLED


# ---------------------------------------------------------------------------
# Plex-only tagalongs (+1 accounts) — operator directive 2026-08-16:
# "the +1 accounts do not get access to anything but plex - they're just
# tagalongs", and "stays live as long as [the payer's] account does".
# ---------------------------------------------------------------------------

TAGALONG_HH = dict(accounts=["member@example.com"],
                   plex_only=["member@example.com"])


def test_tagalong_entitled_gets_plex_but_is_never_provisioned_in_seerr(tmp_path):
    """The Plex share rides the household's entitlement; the stage-1 Seerr
    account every other accepted share gets is withheld for tagalongs."""
    p = plan(household=household(**TAGALONG_HH), answer=answer(ENT.YES),
             state=state_with(tmp_path), share=share(sections=MINIMUM),
             seerr_user=None)
    assert p.state == G.S_ENTITLED
    assert p.plex_target == sorted(FULL), "the tagalong's Plex share IS raised"
    assert p.provision_plex_id is None, "no Seerr account is ever created"
    assert p.seerr_target is None
    assert "tagalong" in p.reason


def test_tagalong_with_an_existing_seerr_account_is_floored_not_raised(tmp_path):
    """If a Seerr account exists from before the flag, the grant branch pins it
    to DISABLED instead of raising it — plex_only withdraws exactly that."""
    p = plan(household=household(**TAGALONG_HH), answer=answer(ENT.YES),
             state=state_with(tmp_path), share=share(sections=FULL),
             seerr_user=seerr_user(perms=SU.MEMBER_PERMISSIONS))
    assert p.state == G.S_ENTITLED
    assert p.seerr_target == SU.PERMISSIONS_DISABLED


def test_tagalong_prior_perms_are_not_restored(tmp_path):
    """seerr_perms_prior can only predate the flag; restoring it would re-grant
    the exact access plex_only withdraws."""
    p = plan(household=household(**TAGALONG_HH), answer=answer(ENT.YES),
             state=state_with(tmp_path, prior_perms=SU.MEMBER_PERMISSIONS),
             share=share(sections=FULL),
             seerr_user=seerr_user(perms=SU.MEMBER_PERMISSIONS))
    assert p.seerr_target == SU.PERMISSIONS_DISABLED


def test_tagalong_expires_right_alongside_the_payer(tmp_path):
    """The coupling is the point: when the household lapses, the tagalong is
    reduced exactly like the payer — same clocks, same floor."""
    after = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
    p = plan(household=household(**TAGALONG_HH),
             answer=answer(ENT.NO, status="former_patron"),
             state=state_with(tmp_path), share=share(sections=FULL), now=after)
    assert p.state == G.S_EXPIRED
    assert p.plex_target == MINIMUM
    assert p.provision_plex_id is None, "expiry must not provision a tagalong either"


def test_a_full_member_in_the_same_household_is_unaffected_by_a_tagalong_sibling(tmp_path):
    """The flag is per-ACCOUNT: the payer keeps full treatment."""
    hh = household(accounts=["member@example.com", "plusone@example.com"],
                   plex_only=["plusone@example.com"])
    p = plan(household=hh, answer=answer(ENT.YES),
             state=state_with(tmp_path), share=share(sections=MINIMUM),
             seerr_user=seerr_user(perms=0))
    assert p.state == G.S_ENTITLED
    assert p.seerr_target == SU.MEMBER_PERMISSIONS, "non-tagalong sibling still raised"


def test_a_lookup_miss_is_frozen_as_unknown_payer_and_does_not_page(tmp_path):
    """`reason: unknown` means the service has no record of the address AT ALL,
    which is an absence of evidence, not evidence of absence.

    HISTORY. This grade used to page, on the theory it was overwhelmingly a
    billing.holder typo. 2026-08-17 dropped the page (the Patreon behind the
    service carries non-QFlix members, and QFlix carries rails the service
    cannot see, so a miss became an ordinary steady state) but LEFT the miss
    riding the lapse ladder as S_PENDING with a live countdown -- a permanent,
    unactionable pending that ends in a reduction taken on no evidence. Live on
    2026-08-20 that was five of twelve shares at "11.9 day(s) of grace remain".

    2026-08-19 gave it its own state. A miss is UNKNOWN, so it freezes exactly
    like S_NO_ANSWER: nothing granted, nothing reduced, no countdown -- the
    operator law against using missing data as an interlock, pointed at an
    absence that was DRIVING an action instead of blocking one.

    The old behaviour this still pins, unchanged: no page, no Plex write, no
    Seerr write. What is new is that the miss is VISIBLE (its own state, its
    own manifest roll-up) instead of hiding inside a `pending=5` count."""
    p = plan(answer=answer(ENT.NO, reason="unknown"), state=state_with(tmp_path),
             share=share(sections=FULL))
    assert p.state == G.S_UNKNOWN_PAYER
    assert p.never_seen is True
    assert "no record" in p.reason
    assert p.alert is None, "a lookup miss must not page from any branch"
    assert p.plex_target is None, "a miss may never plan a Plex reduction"
    assert p.seerr_target is None, "a miss may never plan a Seerr reduction"
    assert not p.mutates or p.provision_plex_id is not None, \
        "the only write a frozen household may carry is stage-1 provisioning"


def test_the_freeze_is_narrow_a_real_negative_verdict_still_counts_down(tmp_path):
    """The control for the test above, and the whole reason it is safe.

    "Freeze on unknown" is one careless widening away from "never revoke
    anyone", and that widening would be invisible: every unknown-payer test
    would stay green. So the same fixture with a REAL negative verdict must
    still land on the lapse ladder, still carry a countdown, and still be
    reducible when it runs out."""
    p = plan(answer=answer(ENT.NO, status="former_patron"),
             state=state_with(tmp_path), share=share(sections=FULL))
    assert p.state == G.S_PENDING
    assert p.never_seen is False
    assert p.days_remaining is not None and p.days_remaining > 0


def test_pending_for_a_lapsed_member_uses_the_seven_day_clock(tmp_path):
    """Past the amnesty, a member who was entitled and fell gets a full week."""
    fell = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    st = state_with(tmp_path, ever_entitled=True, went_false=fell)
    p = plan(answer=answer(ENT.NO), state=st, share=share(sections=FULL),
             now=fell + dt.timedelta(days=3))
    assert p.state == G.S_PENDING
    assert 3.9 < p.days_remaining < 4.1

    p2 = plan(answer=answer(ENT.NO), state=st, share=share(sections=FULL),
              now=fell + dt.timedelta(days=8))
    assert p2.state == G.S_EXPIRED


# ---------------------------------------------------------------------------
# Idempotence -- a 15-minute cron must not rewrite unchanged state
# ---------------------------------------------------------------------------

def test_entitled_and_already_full_emits_no_mutation(tmp_path):
    p = plan(answer=answer(ENT.YES), state=state_with(tmp_path),
             share=share(sections=FULL), seerr_user=seerr_user())
    assert p.state == G.S_ENTITLED
    assert not p.mutates, "96 no-op writes a day is how rate limits are found"


def test_expired_and_already_at_the_floor_emits_no_mutation(tmp_path):
    after = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
    p = plan(answer=answer(ENT.NO), state=state_with(tmp_path),
             share=share(sections=MINIMUM), seerr_user=seerr_user(perms=0),
             now=after)
    assert p.state == G.S_EXPIRED
    assert not p.mutates


def test_unaccepted_invite_is_left_completely_alone(tmp_path):
    p = plan(share=share(accepted=False), state=state_with(tmp_path),
             answer=answer(ENT.YES), seerr_user=None)
    assert p.state == G.S_NOT_ACCEPTED
    assert not p.mutates, "nothing exists yet to provision or restrict"


# ---------------------------------------------------------------------------
# NEVER WRITE AN EMPTY SECTION LIST
# ---------------------------------------------------------------------------

def test_set_sections_refuses_an_empty_list():
    """plex.tv reads [] as 'unshare this server' -- it DELETES the share and
    evicts the person, who then needs a fresh invite accepted from email. This
    is one typo away at all times, so the refusal is unconditional."""
    c = PS.PlexShareClient(token="t", machine_id="m",
                           opener=lambda *a, **k: pytest.fail("must not reach the network"))
    with pytest.raises(PS.PlexShareError) as e:
        c.set_sections(share(), [])
    assert "evicts" in str(e.value)


def test_minimum_access_raises_when_the_welcome_section_is_missing():
    """Catching it here names the cause (missing/renamed section) instead of
    letting the caller discover the symptom (an empty list) later."""
    without = [s for s in SECTIONS if s.id != WELCOME_ID]
    with pytest.raises(PS.PlexShareError) as e:
        PS.minimum_access_ids(without, "QFlix - Welcome")
    assert "unshares the server" in str(e.value)


def test_minimum_access_is_found_case_insensitively():
    assert PS.minimum_access_ids(SECTIONS, "qflix - welcome") == [WELCOME_ID]


def test_full_access_is_recomputed_from_the_live_catalogue():
    """Writing an explicit section list clears Plex's allLibraries flag, so a
    library added later would never reach anyone unless the full set is
    recomputed every run. This is the test that pins that behaviour.

    Updated 2026-08-08: full access EXCLUDES Welcome (master, 2026-08-07 --
    "Welcome is for the NON-entitled only, never both"), so the signature
    takes the welcome title and the expectation subtracts it.
    """
    welcome = "QFlix - Welcome"
    assert PS.full_access_ids(SECTIONS, welcome) == sorted(
        s.id for s in SECTIONS if s.title.lower() != welcome.lower())
    grown = SECTIONS + [PS.Section(id=555, key=8, title="QFlix - Docs", type="movie")]
    assert 555 in PS.full_access_ids(grown, welcome)


# ---------------------------------------------------------------------------
# Wire-format parsing
# ---------------------------------------------------------------------------

SHARED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="2">
<SharedServer id="37676882" username="u1" email="a@example.com" userID="284720148"
  accessToken="SECRET-DO-NOT-PARSE" name="QFlix" acceptedAt="1738445374"
  invitedAt="1738445300" allLibraries="1">
  <Section id="143790062" key="6" title="QFlix - Anime" type="show" shared="1"/>
  <Section id="132920523" key="4" title="QFlix - Movies" type="movie" shared="1"/>
</SharedServer>
<SharedServer id="37676895" username="u2" email="b@example.com" userID="435047761"
  accessToken="SECRET" name="QFlix" invitedAt="1738445399" allLibraries="0">
  <Section id="143790062" key="6" title="QFlix - Anime" type="show" shared="0"/>
</SharedServer>
</MediaContainer>"""


def test_parse_shares_reads_acceptance_and_ignores_unshared_sections():
    shares = PS.parse_shares(SHARED_XML)
    assert len(shares) == 2
    a, b = shares
    assert a.accepted is True
    assert a.accepted_at.year == 2025
    assert a.section_ids == {143790062, 132920523}
    assert a.all_libraries is True
    assert b.accepted is False, "invitedAt without acceptedAt is a pending invite"
    assert b.section_ids == set(), "shared='0' sections are not access"


def test_access_tokens_are_never_captured():
    """They grant server access as that user. Not in the dataclass, not in a log."""
    s = PS.parse_shares(SHARED_XML)[0]
    assert "SECRET-DO-NOT-PARSE" not in repr(s)
    assert not hasattr(s, "access_token")


def test_zero_sections_in_the_catalogue_is_refused():
    """An empty catalogue would compute an empty share set for everybody, which
    unshares the server for the entire membership in one run."""
    with pytest.raises(PS.PlexShareError):
        PS.parse_sections('<?xml version="1.0"?><MediaContainer size="0"/>')


def test_a_share_with_an_unparseable_id_is_skipped_not_guessed():
    xml = ('<MediaContainer><SharedServer id="notanint" userID="5" '
           'email="x@example.com"/></MediaContainer>')
    assert PS.parse_shares(xml) == []


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_digest_and_logs_never_leak_a_full_address():
    assert G.mask("someone@example.com") == "so***@example.com"
    p = plan(answer=answer(ENT.NO), state=state_with(Path(".")),
             share=share(sections=FULL))
    assert "member@example.com" not in json.dumps(p.to_json())


def test_digest_escalates_from_weekly_to_daily_in_the_final_week(tmp_path):
    far = plan(answer=answer(ENT.NO), state=state_with(tmp_path),
               share=share(sections=FULL))
    monday_5pm = dt.datetime(2026, 8, 17, 17, 0, tzinfo=dt.timezone.utc)
    tuesday_5pm = dt.datetime(2026, 8, 18, 17, 0, tzinfo=dt.timezone.utc)
    tuesday_6pm = dt.datetime(2026, 8, 18, 18, 0, tzinfo=dt.timezone.utc)

    far.days_remaining = 12.0
    assert G.should_send_digest([far], monday_5pm) is True
    assert G.should_send_digest([far], tuesday_5pm) is False, "weekly while far out"

    near = plan(answer=answer(ENT.NO), state=state_with(tmp_path),
                share=share(sections=FULL))
    near.days_remaining = 3.0
    assert G.should_send_digest([near], tuesday_5pm) is True, "daily in the last week"
    assert G.should_send_digest([near], tuesday_6pm) is False, \
        "one hour only -- a 15-minute cadence must not send four copies"


def test_digest_is_silent_when_nobody_is_counting_down(tmp_path):
    ok = plan(answer=answer(ENT.YES), state=state_with(tmp_path))
    assert G.should_send_digest([ok], dt.datetime(2026, 8, 17, 17, 0,
                                                  tzinfo=dt.timezone.utc)) is False


import json  # noqa: E402  (used by the masking test above)
