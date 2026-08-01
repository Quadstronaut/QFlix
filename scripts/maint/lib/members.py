"""lib/members.py -- load and validate the membership roster (secrets/members.yaml).

Pure stdlib + pyyaml. No SSH, no network, no secrets, no payment rail. This
module answers exactly one question -- "what does the roster say?" -- and is
deliberately ignorant of who has actually paid. That separation is the point:
the roster is operator intent, the rail is external fact, and conflating them
is how a payment outage turns into a mass revocation.

THE INVARIANTS, and why each one exists
---------------------------------------
Every rule below is a refusal. This file decides whether real people keep
access to the thing they watch in the evening, so every ambiguity resolves to
"stop and ask a human", never to "assume and proceed".

1. Exempt households carry no billing block, and billing households carry no
   `exempt: true`. A record that says both is not a preference, it is a
   contradiction, and silently picking one reading is how somebody's dad gets
   billed.

2. A non-exempt household must state `amount_usd`. `null` means UNSET, not
   free -- there is no way to tell those apart later, and "free" is what a
   forgotten field looks like.

3. No email may appear in two households. Otherwise one household's lapse and
   another's good standing both apply to the same person, and which one wins
   depends on dict iteration order.

4. Household ids are unique. They key the state file that remembers what a
   paused account looked like before it was paused; a collision silently
   restores one household to another household's settings.

`armed` is separate from all of this. A roster can be structurally valid and
still not authorised to touch anything -- see gate_is_armed().
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import yaml


class MembersError(Exception):
    """Raised when members.yaml fails validation. Always fatal to the gate."""


# Where the roster lives, in resolution order.
#
# NOT in the repo. On 2026-08-01 this file was born under manifest/ next to the
# app config and was committed and pushed to a public repo with fourteen real
# addresses in it. The roster is operator data -- it belongs beside the API keys
# in the gitignored secrets directory, on both the workstation and the box.
#
# find_roster() is the only supported way to locate it, and it REFUSES a path
# inside a tracked directory even if one is handed to it. Convention is what
# you follow when you are paying attention; this is for when you are not.
_ROSTER_BASENAME = "members.yaml"
_TRACKED_DIRS = ("manifest", "docs", "scripts", "tests", "apps")


def find_roster(explicit: Optional[Path] = None) -> Path:
    """Resolve the roster path. $QFLIX_MEMBERS wins, then secrets/, then ~/secrets/."""
    if explicit is not None:
        return _reject_if_tracked(Path(explicit))
    env = os.environ.get("QFLIX_MEMBERS")
    if env:
        return _reject_if_tracked(Path(env))
    repo_secrets = Path(__file__).resolve().parents[3] / "secrets" / _ROSTER_BASENAME
    if repo_secrets.exists():
        return repo_secrets
    return Path.home() / "secrets" / _ROSTER_BASENAME


def _reject_if_tracked(p: Path) -> Path:
    """Refuse a roster sitting in a directory git tracks.

    This is a guard against a specific mistake that has already happened once,
    not a hypothetical. Loading is where it gets caught because that is the step
    nobody skips.
    """
    parts = {x.lower() for x in p.resolve().parts}
    bad = parts & set(_TRACKED_DIRS)
    if bad:
        raise MembersError(
            "refusing to read the roster from %s -- %r is a tracked directory "
            "and this file holds real names and addresses. Put it in secrets/ "
            "(gitignored) or point $QFLIX_MEMBERS somewhere outside the repo."
            % (p, sorted(bad)[0]))
    return p


@dataclass
class Billing:
    holder: str
    amount_usd: Optional[float]

    @property
    def resolved(self) -> bool:
        return self.amount_usd is not None


@dataclass
class Household:
    id: str
    display: str
    exempt: bool
    accounts: List[str]
    reason: Optional[str] = None
    billing: Optional[Billing] = None
    provisional: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def resolved(self) -> bool:
        """Is this household ready to be enforced against?

        `provisional` beats everything. It is the flag for "the operator has
        not decided yet", and it exists because `exempt` was the wrong place to
        park an open question: an exempt household is resolved by definition,
        so a TODO written into `reason` would have let a placeholder exemption
        sit there free forever with the gate perfectly content. Undecided has
        to be its own state, and it has to jam the gate, or it is not really
        undecided -- it is just permanently yes.

        Otherwise: exempt households are resolved (nothing left to decide) and
        billing households need a real amount.
        """
        if self.provisional:
            return False
        if self.exempt:
            return True
        return self.billing is not None and self.billing.resolved


@dataclass
class Roster:
    version: int
    armed: bool
    grace_days: int
    paused_sections: List[str]
    households: List[Household]

    def by_email(self) -> Dict[str, Household]:
        """email (lowercased) -> owning household. Every listed account."""
        out: Dict[str, Household] = {}
        for h in self.households:
            for a in h.accounts:
                out[a.lower()] = h
        return out

    def unresolved(self) -> List[Household]:
        return [h for h in self.households if not h.resolved]

    def __iter__(self) -> Iterator[Household]:
        return iter(self.households)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise MembersError(msg)


def load(path: Path) -> Roster:
    """Parse and validate the roster. Raises MembersError on any violation."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MembersError("members.yaml not found at %s" % path)
    except yaml.YAMLError as e:
        raise MembersError("members.yaml is not valid YAML: %s" % e)

    _require(isinstance(raw, dict), "members.yaml must be a mapping at the top level")
    _require(raw.get("version") == 1, "members.yaml: unsupported version %r (expected 1)" % raw.get("version"))

    armed = raw.get("armed")
    _require(isinstance(armed, bool),
             "members.yaml: `armed` must be an explicit true or false, got %r. "
             "A missing value is NOT treated as false -- an absent switch on a "
             "roster that can cut off access is a typo, not a default." % armed)

    defaults = raw.get("defaults") or {}
    grace_days = defaults.get("grace_days", 3)
    _require(isinstance(grace_days, int) and grace_days >= 0,
             "members.yaml: defaults.grace_days must be a non-negative integer, got %r" % grace_days)
    paused_sections = defaults.get("paused_sections") or []
    _require(isinstance(paused_sections, list),
             "members.yaml: defaults.paused_sections must be a list, got %r" % type(paused_sections).__name__)

    rows = raw.get("households")
    _require(isinstance(rows, list) and rows, "members.yaml: `households` must be a non-empty list")

    households: List[Household] = []
    seen_ids = set()
    seen_emails: Dict[str, str] = {}

    for i, row in enumerate(rows):
        _require(isinstance(row, dict), "members.yaml: household #%d is not a mapping" % i)
        hid = row.get("id")
        _require(isinstance(hid, str) and hid.strip(), "members.yaml: household #%d has no id" % i)
        _require(hid not in seen_ids,
                 "members.yaml: duplicate household id %r. Ids key the pause-state "
                 "file; a collision restores one household to another's settings." % hid)
        seen_ids.add(hid)

        exempt = row.get("exempt")
        _require(isinstance(exempt, bool),
                 "members.yaml: household %r must set `exempt` to an explicit "
                 "true or false" % hid)

        provisional = row.get("provisional", False)
        _require(isinstance(provisional, bool),
                 "members.yaml: household %r `provisional` must be true or "
                 "false, got %r" % (hid, provisional))

        accounts = row.get("accounts")
        _require(isinstance(accounts, list) and accounts,
                 "members.yaml: household %r has no accounts" % hid)
        norm: List[str] = []
        for a in accounts:
            _require(isinstance(a, str) and "@" in a,
                     "members.yaml: household %r has a non-email account %r" % (hid, a))
            low = a.lower()
            _require(low not in seen_emails,
                     "members.yaml: %s appears in both %r and %r. One person cannot "
                     "be in two households -- whichever is read last would win, "
                     "which is a coin flip on somebody's access."
                     % (a, seen_emails.get(low), hid))
            seen_emails[low] = hid
            norm.append(a)

        billing_raw = row.get("billing")
        billing = None
        if billing_raw is not None:
            _require(isinstance(billing_raw, dict),
                     "members.yaml: household %r has a malformed billing block" % hid)
            holder = billing_raw.get("holder")
            _require(isinstance(holder, str) and "@" in holder,
                     "members.yaml: household %r billing.holder must be an email" % hid)
            amt = billing_raw.get("amount_usd")
            _require(amt is None or (isinstance(amt, (int, float)) and amt >= 0),
                     "members.yaml: household %r billing.amount_usd must be a "
                     "non-negative number or null, got %r" % (hid, amt))
            billing = Billing(holder=holder, amount_usd=amt)

        # The contradiction check. Both readings of "exempt AND billed" are
        # defensible, which is exactly why the file may not say it.
        _require(not (exempt and billing is not None),
                 "members.yaml: household %r is marked exempt AND carries a "
                 "billing block. Pick one -- an exempt household that is also "
                 "billed has two valid readings and no way to choose." % hid)
        _require(exempt or billing is not None,
                 "members.yaml: household %r is not exempt and has no billing "
                 "block. Every household is either comped or paying; there is "
                 "no third state." % hid)

        households.append(Household(
            id=hid,
            display=row.get("display") or hid,
            exempt=exempt,
            accounts=norm,
            reason=row.get("reason"),
            billing=billing,
            provisional=bool(provisional),
            raw=row,
        ))

    return Roster(
        version=1,
        armed=bool(armed),
        grace_days=grace_days,
        paused_sections=list(paused_sections),
        households=households,
    )


def gate_is_armed(roster: Roster) -> tuple:
    """(armed, reason). Armed requires BOTH the switch and a clean roster.

    Returned rather than raised: "not armed" is a normal reporting state the
    gate runs in every day, not an error. The reason is what gets logged and
    pushed to Kuma, so it has to name the specific blocker -- "not armed" with
    no cause is the kind of message an operator learns to scroll past.
    """
    if not roster.armed:
        return False, "roster armed: false"
    pending = roster.unresolved()
    if pending:
        return False, ("roster has %d unresolved household(s): %s"
                       % (len(pending), ", ".join(h.id for h in pending)))
    return True, "armed"


def reconcile_shares(roster: Roster, live_emails) -> tuple:
    """Compare the roster against the live Plex share list.

    Returns (missing_from_roster, missing_from_plex).

    `missing_from_roster` is the one that stops the gate. A live share nobody
    wrote down is an unbilled share, and a gate that quietly skips what it does
    not recognise is a revenue leak that never files a bug.

    `missing_from_plex` is only informational -- a household can legitimately be
    listed before their invite is accepted, or after they have been removed by
    hand. It is reported, never fatal.
    """
    listed = set(roster.by_email())
    live = {e.lower() for e in live_emails if e}
    return sorted(live - listed), sorted(listed - live)
