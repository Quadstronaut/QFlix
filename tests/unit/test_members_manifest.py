"""The roster decides whether real people keep access. Every rule is a refusal.

The roster is the only file in this system whose contents can black out
somebody's television. That earns it a validator whose every rule fails loudly
rather than picking a reading, and a test file that proves each refusal actually
refuses -- a guard nobody tested is a guard nobody has.

Two classes of test here:

  * INVARIANT tests build a deliberately broken roster and assert it is
    rejected. Each one names the specific way a real person gets hurt if the
    rule is ever relaxed.

  * LIVE tests load the operator's real roster from gitignored secrets/, and
    SKIP when it is absent (CI). A validator that has only ever seen fixtures
    is decoration.

NOTHING IN THIS FILE MAY NAME A MEMBER. Not an address, not a username, not a
relationship. The roster itself is operator data and lives outside the repo;
this file is public and there is a test at the bottom that enforces it.
"""
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "maint"))

from lib import members as M  # noqa: E402

# The real roster is operator data and lives in gitignored secrets/, so CI will
# not have it. These tests SKIP rather than fail when it is absent -- a red
# build on a machine that legitimately has no roster teaches people to ignore
# red builds. When the file IS present (the operator's workstation, the box)
# they run for real.
LIVE = M.find_roster()
_live = pytest.mark.skipif(not LIVE.exists(),
                           reason="no roster at %s (expected in CI)" % LIVE)


def _write(tmp_path, doc) -> Path:
    p = tmp_path / "members.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p


def _base():
    """A minimal roster that PASSES. Every invariant test breaks exactly one
    thing about this, so a failure names one cause rather than a soup."""
    return {
        "version": 1,
        "armed": False,
        "defaults": {"grace_days": 3, "paused_sections": []},
        "households": [
            {"id": "comped", "display": "Comped", "exempt": True,
             "accounts": ["free@example.com"]},
            # Fully resolved on purpose: an amount, a rail, and the payer
            # identifier the receipt will carry. Anything less does not resolve,
            # so a baseline missing one of them would make every arming test
            # below pass for the wrong reason.
            {"id": "payer", "display": "Payer", "exempt": False,
             "billing": {"holder": "pay@example.com", "amount_usd": 50,
                         "rail": "venmo", "payer_ref": "Payer Person"},
             "accounts": ["pay@example.com"]},
        ],
    }


def test_the_baseline_actually_loads(tmp_path):
    """If this ever fails, every other test in the file is vacuous."""
    r = M.load(_write(tmp_path, _base()))
    assert len(r.households) == 2
    assert r.by_email()["pay@example.com"].id == "payer"


# --- invariants ----------------------------------------------------------

def test_exempt_and_billed_is_a_contradiction_not_a_preference(tmp_path):
    """Both readings are defensible, which is why the file may not say it.

    'Exempt but has a card on file' could mean comped-with-history, or could
    mean a stale exemption on someone who now pays. Guessing bills somebody's
    parent or comps somebody who agreed to pay.
    """
    doc = _base()
    doc["households"][0]["billing"] = {"holder": "free@example.com", "amount_usd": 50}
    with pytest.raises(M.MembersError, match="exempt AND carries a billing"):
        M.load(_write(tmp_path, doc))


def test_a_household_that_is_neither_exempt_nor_billed_is_rejected(tmp_path):
    doc = _base()
    del doc["households"][1]["billing"]
    with pytest.raises(M.MembersError, match="not exempt and has no billing"):
        M.load(_write(tmp_path, doc))


def test_one_email_cannot_be_in_two_households(tmp_path):
    """Otherwise one household's lapse and another's good standing both apply to
    the same person, and the winner is dict iteration order."""
    doc = _base()
    doc["households"][1]["accounts"].append("free@example.com")
    with pytest.raises(M.MembersError, match="cannot be in two households"):
        M.load(_write(tmp_path, doc))


# --- plex_only tagalongs (mapping-form accounts, 2026-08-16) ---------------

def test_mapping_account_with_plex_only_parses_and_flags(tmp_path):
    """The +1 form: email still lands in accounts (by_email, dedupe, oracle all
    see it) and the flag is queryable per account."""
    doc = _base()
    doc["households"][1]["accounts"].append(
        {"email": "PlusOne@example.com", "plex_only": True, "note": "tagalong"})
    r = M.load(_write(tmp_path, doc))
    h = r.by_email()["plusone@example.com"]
    assert h.id == "payer"
    assert h.is_plex_only("PLUSONE@example.com"), "flag is case-insensitive"
    assert not h.is_plex_only("pay@example.com"), "the payer is untouched"
    assert "PlusOne@example.com" in h.accounts


def test_mapping_account_without_plex_only_is_a_plain_member(tmp_path):
    doc = _base()
    doc["households"][1]["accounts"].append({"email": "kid@example.com"})
    r = M.load(_write(tmp_path, doc))
    assert not r.by_email()["kid@example.com"].is_plex_only("kid@example.com")


def test_mapping_account_with_unknown_keys_is_refused(tmp_path):
    """A typo like `plexonly:` must fail loudly, not silently grant the
    full-member treatment the mapping form exists to withhold."""
    doc = _base()
    doc["households"][1]["accounts"].append(
        {"email": "x@example.com", "plexonly": True})
    with pytest.raises(M.MembersError, match="unknown key"):
        M.load(_write(tmp_path, doc))


def test_mapping_account_plex_only_must_be_boolean(tmp_path):
    doc = _base()
    doc["households"][1]["accounts"].append(
        {"email": "x@example.com", "plex_only": "yes"})
    with pytest.raises(M.MembersError, match="must be true or false"):
        M.load(_write(tmp_path, doc))


def test_the_billing_holder_cannot_be_a_tagalong_on_their_own_bill(tmp_path):
    """Both readings of a plex_only payer are defensible, so the file may not
    say it — the same rule as exempt-and-billed."""
    doc = _base()
    doc["households"][1]["accounts"] = [
        {"email": "pay@example.com", "plex_only": True}]
    with pytest.raises(M.MembersError, match="billing.holder"):
        M.load(_write(tmp_path, doc))


def test_an_exempt_household_cannot_carry_plex_only_accounts(tmp_path):
    """The exempt branch returns before the flag is ever consulted, so the
    flag would be a silent no-op — and a written-down no-op with two readings
    is refused, same rule as exempt-and-billed."""
    doc = _base()
    doc["households"][0]["accounts"].append(
        {"email": "tag@example.com", "plex_only": True})
    with pytest.raises(M.MembersError, match="exempt AND marks"):
        M.load(_write(tmp_path, doc))


def test_mapping_form_still_hits_the_two_household_dedupe(tmp_path):
    doc = _base()
    doc["households"][1]["accounts"].append(
        {"email": "free@example.com", "plex_only": True})
    with pytest.raises(M.MembersError, match="cannot be in two households"):
        M.load(_write(tmp_path, doc))


def test_duplicate_household_ids_are_rejected(tmp_path):
    """Ids key the pause-state file. A collision restores one household to
    another household's pre-pause settings."""
    doc = _base()
    doc["households"][1]["id"] = "comped"
    with pytest.raises(M.MembersError, match="duplicate household id"):
        M.load(_write(tmp_path, doc))


def test_armed_must_be_explicit_and_a_missing_switch_is_not_false(tmp_path):
    """A missing arming switch is a typo, not a default. Defaulting it to false
    would be 'safe' today and would silently swallow the day someone deletes the
    line while meaning to set it true."""
    doc = _base()
    del doc["armed"]
    with pytest.raises(M.MembersError, match="explicit true or false"):
        M.load(_write(tmp_path, doc))


def test_null_amount_is_unset_and_blocks_arming_rather_than_meaning_free(tmp_path):
    """THE ONE THAT MATTERS. `amount_usd: null` and 'this person pays nothing'
    are indistinguishable downstream, and null is what a forgotten field looks
    like. So null must never resolve -- it must jam the gate."""
    doc = _base()
    doc["armed"] = True
    doc["households"][1]["billing"]["amount_usd"] = None
    r = M.load(_write(tmp_path, doc))          # structurally fine...
    assert [h.id for h in r.unresolved()] == ["payer"]
    armed, why = M.gate_is_armed(r)
    assert armed is False
    assert "payer" in why, "the blocker must be NAMED, not just counted: " + why


def test_arming_needs_both_the_switch_and_a_clean_roster(tmp_path):
    doc = _base()
    doc["armed"] = True
    r = M.load(_write(tmp_path, doc))
    assert M.gate_is_armed(r) == (True, "armed")

    doc["armed"] = False
    r2 = M.load(_write(tmp_path, doc))
    assert M.gate_is_armed(r2)[0] is False


def test_exempt_households_are_always_resolved(tmp_path):
    """An exempt household has nothing left to decide, so it must never be the
    thing that keeps the gate disarmed."""
    r = M.load(_write(tmp_path, _base()))
    assert r.households[0].resolved is True


def test_provisional_beats_exempt_and_jams_the_gate(tmp_path):
    """'I have not decided yet' must be a state that STOPS things.

    The failure this prevents: two households were once exempt with their terms
    unconfirmed, recorded as a TODO in a comment. Since an exempt household is
    resolved by definition, that placeholder would have sat there free forever
    with the gate perfectly happy and nothing ever surfacing it. Undecided has
    to jam the gate, or it is not undecided -- it is permanently yes.
    """
    doc = _base()
    doc["armed"] = True
    doc["households"][0]["provisional"] = True
    r = M.load(_write(tmp_path, doc))
    assert r.households[0].exempt is True
    assert r.households[0].resolved is False, "provisional must beat exempt"
    armed, why = M.gate_is_armed(r)
    assert armed is False
    assert "comped" in why, "the undecided household must be named: " + why


def test_provisional_defaults_to_false_and_must_be_boolean(tmp_path):
    r = M.load(_write(tmp_path, _base()))
    assert all(h.provisional is False for h in r)

    doc = _base()
    doc["households"][0]["provisional"] = "yes"
    with pytest.raises(M.MembersError, match="provisional"):
        M.load(_write(tmp_path, doc))


# --- reconciliation against live Plex ------------------------------------

def test_a_live_share_missing_from_the_roster_is_surfaced(tmp_path):
    """An unlisted share is an unbilled share. A gate that skips what it does
    not recognise is a revenue leak that never files a bug."""
    r = M.load(_write(tmp_path, _base()))
    missing, absent = M.reconcile_shares(
        r, ["pay@example.com", "free@example.com", "ghost@example.com"])
    assert missing == ["ghost@example.com"]
    assert absent == []


def test_a_rostered_household_not_yet_on_plex_is_informational_only(tmp_path):
    """Legitimate during the window between listing someone and their accepting
    the invite. Reported, never fatal."""
    r = M.load(_write(tmp_path, _base()))
    missing, absent = M.reconcile_shares(r, ["pay@example.com"])
    assert missing == []
    assert absent == ["free@example.com"]


def test_reconcile_is_case_insensitive_on_both_sides(tmp_path):
    """Plex returns whatever case the user typed at signup. Treating
    Pay@Example.com as a stranger would revoke a paying member."""
    r = M.load(_write(tmp_path, _base()))
    missing, _ = M.reconcile_shares(r, ["PAY@Example.COM", "Free@example.com"])
    assert missing == []


# --- the real file --------------------------------------------------------

@_live
def test_the_checked_in_roster_is_valid():
    """A validator that has only ever seen fixtures is decoration."""
    M.load(LIVE)


@_live
def test_arming_preconditions_hold():
    """Armed only ever with a fully-resolved roster.

    Until 2026-08-08 this test asserted `armed is False` outright: arming is an
    operator act performed with intent, never something that rides along in a
    commit. That intent was then exercised -- the operator armed the gate
    deliberately after a live end-to-end test (a real subscription synced to
    entitled:true and the first execute run applied exactly the planned
    writes), so a permanent disarmed-assertion would now demand the gate be
    switched off to make CI pass, which is the tail wagging the dog.

    What must survive that transition is the roster's own documented
    precondition: 'Flip to true ONLY when every household below is resolved.'
    An armed roster with an unresolved household is the state where the gate
    can act on a household nobody finished deciding about -- that is still a
    bug, and it is the one this guard now catches. A disarmed roster is always
    acceptable; report-only is safe in every state."""
    r = M.load(LIVE)
    if r.armed:
        unresolved = r.unresolved()
        assert not unresolved, (
            "members.yaml is ARMED with %d unresolved household(s). Arming "
            "requires every household resolved; resolve them or disarm."
            % len(unresolved)
        )


# Households that must never be gated, as a COUNT rather than a list.
#
# The obvious version of this guard named each protected account. That put real
# addresses and family relationships into a test file, which is public. The
# count protects the same thing -- "tidying up the roster" silently dropping an
# exemption -- while naming nobody. Changing this number should be a deliberate
# edit with a reason in the commit message, never a drive-by.
#
# 4 -> 3 on 2026-08-08: one household's exemption was removed by operator
# directive (the person does not use the service and holds no share; the row
# was kept, un-exempted, so any future share lands named and graded). This is
# exactly the deliberate-edit-with-a-reason this guard exists to force.
EXPECTED_EXEMPT_HOUSEHOLDS = 3


@_live
def test_the_protected_exemptions_are_still_exempt():
    """Regression guard on accounts that must never be gated.

    Deliberately anonymous. Asserting a count catches an exemption being
    dropped, reordered, or flipped to billing, without this file having to know
    who any of those people are.
    """
    r = M.load(LIVE)
    exempt = [h for h in r if h.exempt]
    assert len(exempt) == EXPECTED_EXEMPT_HOUSEHOLDS, (
        "exempt-household count changed (%d -> %d). If that was intentional, "
        "update EXPECTED_EXEMPT_HOUSEHOLDS and say why in the commit. If it "
        "was not, somebody just lost their free access."
        % (EXPECTED_EXEMPT_HOUSEHOLDS, len(exempt))
    )


@_live
def test_no_household_is_missing_an_account():
    """A household with no accounts cannot be enforced either way -- it is
    invisible to the gate and to the Plex reconcile, so it silently pays
    nothing forever."""
    r = M.load(LIVE)
    assert all(h.accounts for h in r)


def test_this_test_file_contains_no_personal_data():
    """The repo is public. On 2026-08-01 a roster of real addresses was
    committed and pushed, and the fix is worth an assertion rather than a
    resolution: nothing in this file may name a member.

    Scoped to this file on purpose -- it is the one that has a standing
    temptation to hardcode a real account to make an assertion concrete.
    """
    import re
    src = Path(__file__).read_text(encoding="utf-8")
    # Strip the pattern definition below so the test does not match itself.
    body = src.replace("PERSONAL", "")
    hits = re.findall(
        r"[A-Za-z0-9._%+-]+@(?:gmail|hotmail|outlook|yahoo|icloud|aol|live|protonmail)\.[a-z.]+",
        body)
    assert hits == [], "real address(es) present in a public test file"


@_live
def test_no_exemption_is_left_provisional():
    """A provisional exemption must never survive into a merge.

    Scoped to `provisional` on purpose, NOT to full resolution. The paying
    households legitimately carry `amount_usd: null` right now -- no rail is
    chosen, so no amount is real yet -- and gate_is_armed() already refuses to
    arm while that is true. Asserting full resolution here would just be a red
    build describing a decision that has not been made yet.

    A provisional EXEMPTION is different: it is free access with an open
    question attached, it costs money silently, and nothing else in the system
    would ever surface it. That is the one that has to fail the build. If this
    goes red, go make the decision -- do not delete the flag.
    """
    r = M.load(LIVE)
    pending = [h.id for h in r if h.provisional]
    assert pending == [], "provisional exemptions still open: " + ", ".join(pending)


# --- multi-rail billing ---------------------------------------------------

def _billing(**kw):
    doc = _base()
    doc["households"][1]["billing"] = dict(
        {"holder": "pay@example.com", "amount_usd": 50}, **kw)
    return doc


def test_a_resolved_household_needs_amount_rail_and_payer_ref(tmp_path):
    """All three or the reconciler cannot decide, and a reconciler that cannot
    decide must not act. Each is checked separately so a failure names one
    cause."""
    # amount only
    r = M.load(_write(tmp_path, _billing()))
    assert not r.households[1].resolved, "no rail should not resolve"
    # amount + rail, but a rail that reports by email and no ref
    r = M.load(_write(tmp_path, _billing(rail="venmo")))
    assert not r.households[1].resolved, "email rail without payer_ref must not resolve"
    # complete
    r = M.load(_write(tmp_path, _billing(rail="venmo", payer_ref="Some Payer")))
    assert r.households[1].resolved


def test_manual_rail_needs_no_payer_ref(tmp_path):
    """`manual` means the operator is the matcher -- cash, a bank transfer with
    no parseable receipt. Demanding a ref there would be busywork."""
    r = M.load(_write(tmp_path, _billing(rail="manual")))
    assert r.households[1].resolved


def test_an_unknown_rail_is_rejected(tmp_path):
    """A typo silently orphans the household: no ingester claims it, no receipt
    matches, and it reads as a permanent lapse."""
    with pytest.raises(M.MembersError, match="valid rails are"):
        M.load(_write(tmp_path, _billing(rail="vemno", payer_ref="x")))


def test_two_households_cannot_share_a_payer_ref_on_one_rail(tmp_path):
    """THE MATCHING DISASTER. One receipt matching two households credits
    whichever is read first: one person pays, someone else's access renews, and
    the payer lapses."""
    doc = _billing(rail="venmo", payer_ref="Same Name")
    doc["households"][0] = {
        "id": "other", "display": "Other", "exempt": False,
        "billing": {"holder": "o@example.com", "amount_usd": 20,
                    "rail": "venmo", "payer_ref": "same name"},
        "accounts": ["o@example.com"]}
    with pytest.raises(M.MembersError, match="share the payer_ref"):
        M.load(_write(tmp_path, doc))


def test_the_same_name_on_two_different_rails_is_fine(tmp_path):
    """Scoped per rail on purpose -- one display name on Venmo and the same on
    Patreon are unrelated facts, and forbidding it would be a false positive."""
    doc = _billing(rail="venmo", payer_ref="Same Name")
    doc["households"][0] = {
        "id": "other", "display": "Other", "exempt": False,
        "billing": {"holder": "o@example.com", "amount_usd": 20,
                    "rail": "patreon", "payer_ref": "Same Name"},
        "accounts": ["o@example.com"]}
    r = M.load(_write(tmp_path, doc))
    assert len(r.by_payer_ref()) == 2


def test_payer_ref_lookup_is_case_and_space_insensitive(tmp_path):
    """Receipts render names inconsistently. Treating 'jane d' as a stranger
    would revoke someone who paid."""
    r = M.load(_write(tmp_path, _billing(rail="venmo", payer_ref="  Jane D  ")))
    assert ("venmo", "jane d") in r.by_payer_ref()


def test_rails_in_use_reports_the_spread(tmp_path):
    r = M.load(_write(tmp_path, _billing(rail="venmo", payer_ref="x")))
    assert r.rails_in_use() == {"venmo": 1}
