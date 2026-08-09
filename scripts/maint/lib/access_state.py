"""lib/access_state.py -- durable memory for the entitlement gate.

Pure stdlib. No network, no Plex, no Seerr, no roster parsing. This module
remembers three things across runs and computes one:

  * when each account was FIRST SEEN with an accepted Plex share, and which
    cohort that puts it in;
  * whether it has ever been entitled, and when it stopped being;
  * what its Seerr permissions were before this system zeroed them;
  * and from those, the DEADLINE after which reduced access is authorised.

WHY THIS IS A FILE AND NOT A DERIVED VALUE
------------------------------------------
Two of the three clocks are anchored to events with no record anywhere else.
The entitlement API is a projection of NOW with no history at all, so "when did
this person stop being entitled" exists only if something wrote it down. That
something is this file.

The arrival clock is the exception and is deliberately NOT trusted to this
file: Plex's shared_servers endpoint reports a real `acceptedAt` timestamp per
share, so `seed()` takes it and anchors to when the person actually accepted.
Anchoring to first-observation instead would have given a member who accepted
in February and one who accepted this morning the same clock on the gate's
first run.

If this file is lost, every account re-seeds with its true acceptance date and
only the lapse history is forgotten -- which is safe (a lapsed member gets a
fresh week) but wrong, and repeated loss would mean nobody is ever cut off. So
it is written atomically, and the loader treats corruption as "start over"
rather than as a crash, because a crash here stops the provisioning half of the
system too.

THE THREE CLOCKS, and why the LATEST one wins
---------------------------------------------
    launch amnesty      accounts already accepted when this system first ran.
                        One-time. The campaign had zero members on the day this
                        was built, so arming without an amnesty would have
                        shrunk every existing household on the first run.

    new-arrival grace   accounts first seen accepted after that. Counted from
                        acceptance, not from a fixed date, because a person
                        invited three days before a fixed deadline would
                        otherwise get a three-day window.

    lapse grace         an account that WAS entitled and went false. Counted
                        from the moment it went false.

`deadline = max(applicable)`. Taking the max rather than the min is a
deliberate choice to be slow: every clock is a promise of a minimum fair
window, and honouring all of them simultaneously means no promise is ever
broken by the existence of another. The cost is that somebody occasionally
keeps access a few weeks longer than the strictest reading would allow. That is
the cheap direction to be wrong in.

A NOTE ON THE LAPSE CLOCK AND PEOPLE WHO NEVER SUBSCRIBED
---------------------------------------------------------
`went_false_at` is only set for an account that had previously been entitled.
Someone who has never been entitled is not "lapsing", they are "pending", and
starting a 7-day lapse clock for them would be a second, shorter deadline
running underneath their real one. Since the max wins it would change no
outcome today -- but it would be a live trap for the first person who edits
these rules, so the distinction is enforced rather than merely commented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import json
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

SCHEMA = 1

COHORT_LAUNCH = "launch"      # already had an accepted share when we first ran
COHORT_ARRIVAL = "arrival"    # accepted after we started watching

# Defaults, overridden by members.yaml `defaults:`. Named here so the module is
# usable standalone and so a missing roster key has an explicit value rather
# than a None that propagates into date arithmetic.
DEFAULT_GRACE_DAYS = 7
DEFAULT_NEW_ARRIVAL_DAYS = 30

# Minimum days between first observing the launch cohort and being allowed to
# reduce any of it, regardless of what the amnesty date says. Exists for one
# case: the state file is lost and re-seeded AFTER the amnesty has expired, at
# which point a stale roster date would otherwise authorise reducing everybody
# immediately. A week is enough for the operator to notice a Kuma red and a
# Discord countdown before anything is taken away.
LAUNCH_FLOOR_DAYS = 7

# Minimum days between this system FIRST RECORDING an account and being allowed
# to reduce it -- whatever cohort it is in, whatever its clocks say.
#
# The bug this closes: `first_seen_accepted` is Plex's real `acceptedAt`, which
# is historical. An account row that goes missing from state.json while
# `first_run_at` survives re-seeds as COHORT_ARRIVAL anchored to that historical
# date, so `acceptedAt + new_arrival_days` can land MONTHS in the past and the
# account is reducible on the very first clean NO -- with zero of its promised
# thirty days. LAUNCH_FLOOR_DAYS did not cover it, because that guards the
# launch cohort only.
#
# The real invariant is not about cohorts at all: we must never reduce an
# account we have only just started tracking, because "just started tracking"
# means our clocks for it are reconstructions rather than observations. Keyed on
# `first_recorded_at` -- OUR wall clock -- not on anything Plex reports.
#
# Realistic triggers, none exotic: a member changes their Plex account email
# (state is keyed by email, so the old row is orphaned and a new one appears);
# a restore from a state.json backup predating that member; a partial write; a
# hand-edit.
TRACKING_FLOOR_DAYS = 7


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def to_iso(t: Optional[dt.datetime]) -> Optional[str]:
    if t is None:
        return None
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(s: Optional[str]) -> Optional[dt.datetime]:
    """Parse an ISO timestamp back to an aware UTC datetime. None on anything odd.

    Lenient on purpose: a malformed timestamp in the state file must not crash
    the run. It degrades that one account to "no recorded history", which the
    caller treats as a fresh arrival -- the safe direction.
    """
    if not s:
        return None
    try:
        txt = s.strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(txt)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def parse_amnesty(value) -> Optional[dt.datetime]:
    """Parse `defaults.amnesty_until` from the roster.

    Accepts a bare date (2026-09-01 -> midnight UTC) or a full timestamp. YAML
    parses an unquoted date into a datetime.date, so both shapes arrive here.
    Returns None if unset or unparseable; the caller then falls back to the
    new-arrival window, which is a real deadline rather than an infinite one --
    a typo in this field must not silently grant everybody permanent amnesty.
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc)
    return from_iso(str(value))


@dataclass
class AccountState:
    """Everything remembered about one Plex account email."""

    first_seen_accepted: Optional[dt.datetime] = None
    # When WE first wrote this account down. Distinct from first_seen_accepted,
    # which is Plex's historical acceptedAt. The difference is the whole point:
    # one is an observation of the past, the other is when our clocks for this
    # account started being real rather than reconstructed. See
    # TRACKING_FLOOR_DAYS.
    first_recorded_at: Optional[dt.datetime] = None
    cohort: str = COHORT_ARRIVAL
    last_entitled_at: Optional[dt.datetime] = None
    went_false_at: Optional[dt.datetime] = None
    seerr_user_id: Optional[int] = None
    seerr_perms_prior: Optional[int] = None
    last_action: Optional[str] = None
    last_action_at: Optional[dt.datetime] = None
    # Reporting only -- never used in a clock. See the module docstring.
    first_not_entitled_at: Optional[dt.datetime] = None
    # Notification throttle. The gate runs 96 times a day; an alert with no
    # memory is an alert sent 96 times, and a channel that fires 96 times a day
    # is a channel the operator mutes inside a week. Muting the unnamed-share
    # page is especially bad: that page IS the backstop for the one case the
    # system deliberately refuses to handle automatically.
    last_alert: Optional[str] = None
    last_alert_on: Optional[str] = None          # YYYY-MM-DD

    @property
    def ever_entitled(self) -> bool:
        return self.last_entitled_at is not None

    def to_json(self) -> dict:
        return {
            "first_seen_accepted": to_iso(self.first_seen_accepted),
            "first_recorded_at": to_iso(self.first_recorded_at),
            "cohort": self.cohort,
            "last_entitled_at": to_iso(self.last_entitled_at),
            "went_false_at": to_iso(self.went_false_at),
            "first_not_entitled_at": to_iso(self.first_not_entitled_at),
            "seerr_user_id": self.seerr_user_id,
            "seerr_perms_prior": self.seerr_perms_prior,
            "last_action": self.last_action,
            "last_action_at": to_iso(self.last_action_at),
            "last_alert": self.last_alert,
            "last_alert_on": self.last_alert_on,
        }

    @classmethod
    def from_json(cls, d: dict) -> "AccountState":
        if not isinstance(d, dict):
            d = {}
        cohort = d.get("cohort")
        if cohort not in (COHORT_LAUNCH, COHORT_ARRIVAL):
            cohort = COHORT_ARRIVAL
        uid = d.get("seerr_user_id")
        perms = d.get("seerr_perms_prior")
        return cls(
            first_seen_accepted=from_iso(d.get("first_seen_accepted")),
            first_recorded_at=from_iso(d.get("first_recorded_at")),
            cohort=cohort,
            last_entitled_at=from_iso(d.get("last_entitled_at")),
            went_false_at=from_iso(d.get("went_false_at")),
            first_not_entitled_at=from_iso(d.get("first_not_entitled_at")),
            seerr_user_id=uid if isinstance(uid, int) else None,
            seerr_perms_prior=perms if isinstance(perms, int) else None,
            last_action=d.get("last_action"),
            last_action_at=from_iso(d.get("last_action_at")),
            last_alert=d.get("last_alert"),
            last_alert_on=d.get("last_alert_on"),
        )


@dataclass
class AccessState:
    """The whole state file. `dirty` tracks whether save() has anything to do."""

    path: Path
    first_run_at: Optional[dt.datetime] = None
    accounts: Dict[str, AccountState] = field(default_factory=dict)
    dirty: bool = False
    last_digest_on: Optional[str] = None         # YYYY-MM-DD

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "AccessState":
        """Read state. A missing OR CORRUPT file yields empty state, not an error.

        Corruption is graded the same as absence deliberately. The alternative
        -- refusing to run -- stops provisioning as well as revocation, so a
        single bad byte would lock new members out of Seerr indefinitely.
        Losing the file is self-correcting in the safe direction: every account
        re-seeds as a fresh arrival and nobody is cut off early.
        """
        st = cls(path=Path(path))
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return st
        if not isinstance(raw, dict):
            return st
        st.first_run_at = from_iso(raw.get("first_run_at"))
        ld = raw.get("last_digest_on")
        st.last_digest_on = ld if isinstance(ld, str) else None
        accts = raw.get("accounts")
        if isinstance(accts, dict):
            for email, blob in accts.items():
                if isinstance(email, str) and email:
                    st.accounts[email.lower()] = AccountState.from_json(blob)

        # Heal a state file that has accounts but lost `first_run_at` (partial
        # write, hand-edit, schema drift). The launch-cohort floor is measured
        # from that timestamp, so a missing one makes the floor slide forward on
        # every run and NOBODY is ever reduced -- the protection half silently
        # stops working, which is the failure nobody reports.
        #
        # Guarded on `accounts` being non-empty so a genuinely first run is
        # still first: healing unconditionally would make is_first_run() false
        # before seed() had run, and the entire launch cohort would be tagged as
        # arrivals and lose the amnesty.
        if st.accounts and st.first_run_at is None:
            st.first_run_at = utcnow()
            st.dirty = True
        return st

    def save(self) -> None:
        """Atomic write. A half-written state file loses the prior-permissions
        record, which is precisely the data that makes a restore exact rather
        than a guess at what 'member default' used to be."""
        payload = {
            "schema": SCHEMA,
            "first_run_at": to_iso(self.first_run_at),
            "last_digest_on": self.last_digest_on,
            "accounts": {e: a.to_json() for e, a in sorted(self.accounts.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)
        self.dirty = False

    # -- observation --------------------------------------------------------

    def get(self, email: str) -> AccountState:
        return self.accounts.setdefault(email.lower(), AccountState())

    def is_first_run(self) -> bool:
        return self.first_run_at is None

    def seed(self, accepted, now: Optional[dt.datetime] = None) -> List[str]:
        """Record every currently-accepted share. Returns the newly-added ones.

        `accepted` is an iterable of either bare emails or (email, accepted_at)
        pairs. PREFER THE PAIR FORM. Plex's shared_servers endpoint reports a
        real `acceptedAt` unix timestamp for every share, so the clock can be
        anchored to when the person actually took the invite rather than to
        whenever this cron first happened to look at them. The difference is not
        cosmetic: without it, a member who accepted in February and a member who
        accepted this morning both anchor at "now" the first time the gate runs,
        and the second one silently inherits thirty days they did not earn.

        On the FIRST run everything present is tagged `launch` -- it predates
        this system entirely, so it gets the amnesty. On every later run a
        previously-unseen account is an `arrival` and its clock starts from its
        own acceptance.

        Seeding writes state even when the gate is disarmed, and that is
        deliberate: a system that only learns who existed once it is allowed to
        act cannot tell "pre-existing" from "appeared while I was disarmed", and
        would hand the launch amnesty to somebody who joined last week.
        """
        now = now or utcnow()
        first = self.is_first_run()
        added: List[str] = []
        for item in accepted:
            if isinstance(item, (tuple, list)):
                raw, accepted_at = (list(item) + [None])[:2]
            else:
                raw, accepted_at = item, None
            if not raw:
                continue
            email = raw.strip().lower()
            if not email:
                continue
            if email in self.accounts and self.accounts[email].first_seen_accepted:
                continue
            st = self.accounts.setdefault(email, AccountState())
            # A future-dated acceptance (clock skew, or a corrupt timestamp)
            # would push the deadline out forever, so it is clamped to now.
            if accepted_at is not None and accepted_at <= now:
                st.first_seen_accepted = accepted_at
            else:
                st.first_seen_accepted = now
            st.first_recorded_at = now
            st.cohort = COHORT_LAUNCH if first else COHORT_ARRIVAL
            added.append(email)
            self.dirty = True
        if first:
            self.first_run_at = now
            self.dirty = True
        return added

    def record_entitled(self, email: str, now: Optional[dt.datetime] = None) -> None:
        """A clean yes. Clears the lapse clock entirely.

        Clearing rather than pausing matters: a member who lapses, returns, and
        lapses again should get a full fresh week the second time, not the
        remainder of the first one.
        """
        now = now or utcnow()
        st = self.get(email)
        st.last_entitled_at = now
        if st.went_false_at is not None or st.first_not_entitled_at is not None:
            st.went_false_at = None
            st.first_not_entitled_at = None
        self.dirty = True

    def record_not_entitled(self, email: str, now: Optional[dt.datetime] = None) -> None:
        """A clean no. Starts the lapse clock ONLY for someone who had access.

        See the module docstring: a never-entitled account is pending, not
        lapsing, and giving it a lapse clock would put a second, shorter
        deadline underneath its real one.
        """
        now = now or utcnow()
        st = self.get(email)
        if st.first_not_entitled_at is None:
            st.first_not_entitled_at = now
            self.dirty = True
        if st.ever_entitled and st.went_false_at is None:
            st.went_false_at = now
            self.dirty = True

    def record_action(self, email: str, action: str,
                      now: Optional[dt.datetime] = None) -> None:
        st = self.get(email)
        st.last_action = action
        st.last_action_at = now or utcnow()
        self.dirty = True

    def remember_seerr(self, email: str, user_id: Optional[int] = None,
                       perms_prior: Optional[int] = None) -> None:
        """Persist the Seerr identity and the permissions to restore later.

        `perms_prior` is only ever recorded for a NON-ZERO value. Overwriting a
        remembered 1155539104 with the 0 this system just wrote would destroy
        the only record of what to put back, turning every restore into a guess.
        """
        st = self.get(email)
        if user_id is not None and st.seerr_user_id != user_id:
            st.seerr_user_id = user_id
            self.dirty = True
        if perms_prior is not None and perms_prior != 0 and st.seerr_perms_prior != perms_prior:
            st.seerr_perms_prior = perms_prior
            self.dirty = True

    # -- notification throttle ----------------------------------------------
    #
    # The gate runs 96 times a day. Anything it says without memory, it says 96
    # times. Both helpers below are keyed on the calendar day rather than on an
    # interval, so the operator gets at most one of each per day and the first
    # one arrives promptly instead of after a cooldown.

    def should_alert(self, email: str, text: str, now: Optional[dt.datetime] = None) -> bool:
        """One alert per account per day, and immediately if the TEXT changed.

        Re-alerting on a changed message matters: "unnamed share holds 1
        section" becoming "unnamed share holds 5 sections" is a new fact, not a
        repeat, and holding it for a day would hide an escalation.
        """
        now = now or utcnow()
        today = now.strftime("%Y-%m-%d")
        st = self.accounts.get(email.lower())
        if st is None:
            return True
        return not (st.last_alert == text and st.last_alert_on == today)

    def mark_alert(self, email: str, text: str, now: Optional[dt.datetime] = None) -> None:
        now = now or utcnow()
        st = self.get(email)
        st.last_alert = text
        st.last_alert_on = now.strftime("%Y-%m-%d")
        self.dirty = True

    def should_digest(self, now: Optional[dt.datetime] = None) -> bool:
        now = now or utcnow()
        return self.last_digest_on != now.strftime("%Y-%m-%d")

    def mark_digest(self, now: Optional[dt.datetime] = None) -> None:
        now = now or utcnow()
        self.last_digest_on = now.strftime("%Y-%m-%d")
        self.dirty = True

    # -- the clocks ---------------------------------------------------------

    def deadline_for(self, email: str, *, amnesty_until: Optional[dt.datetime],
                     grace_days: int = DEFAULT_GRACE_DAYS,
                     new_arrival_days: int = DEFAULT_NEW_ARRIVAL_DAYS,
                     now: Optional[dt.datetime] = None) -> dt.datetime:
        """The instant at or after which reduced access is authorised.

        Returns the LATEST applicable clock. Never returns None: an account with
        no recorded history is treated as arriving now, which yields a full
        new-arrival window rather than an immediate cut.
        """
        now = now or utcnow()
        st = self.accounts.get(email.lower())
        anchor = (st.first_seen_accepted if st and st.first_seen_accepted else now)
        cohort = st.cohort if st else COHORT_ARRIVAL

        candidates: List[dt.datetime] = []

        if cohort == COHORT_LAUNCH:
            # THE LAUNCH COHORT IS ANCHORED TO first_run_at, NEVER TO acceptedAt.
            #
            # This is the correction to a real defect. `anchor` for these
            # accounts is their genuine Plex acceptance date, which is MONTHS in
            # the past -- and an earlier version fell back to
            # `anchor + new_arrival_days` whenever the amnesty date was missing
            # or unparseable. That fallback is a deadline that has ALREADY
            # PASSED, so a single mistyped roster key (`amnesty_untill:`, or the
            # key nested one level wrong) would expire the entire launch cohort
            # on the first armed run -- precisely the mass reduction the amnesty
            # exists to prevent. The unit test missed it because it seeded the
            # bare-email form, which anchors at "now"; production seeds the
            # (email, acceptedAt) pair form, which anchors historically.
            run = self.first_run_at or now
            if amnesty_until is not None:
                # The amnesty date is POLICY and normally wins. The floor beside
                # it is an anti-footgun rail for the case where state is lost
                # and re-seeded AFTER the amnesty has already expired: without
                # it, a stale date in the roster would expire everybody the
                # instant the state file went missing.
                candidates.append(max(amnesty_until,
                                      run + dt.timedelta(days=LAUNCH_FLOOR_DAYS)))
            else:
                # No amnesty at all: a real, future window measured from when
                # this system first observed the cohort. Never from a date in
                # the past, and never "no deadline".
                candidates.append(run + dt.timedelta(days=new_arrival_days))
        else:
            candidates.append(anchor + dt.timedelta(days=new_arrival_days))

        if st and st.ever_entitled and st.went_false_at is not None:
            candidates.append(st.went_false_at + dt.timedelta(days=grace_days))

        # THE UNIVERSAL TRACKING FLOOR. Applies to every cohort, unconditionally.
        #
        # Every other candidate above is derived from a date this system did not
        # witness: Plex's historical acceptedAt, or a roster field. This one is
        # the only clock anchored to OUR observation, and it is what makes a
        # freshly-recorded account un-reducible regardless of how the others
        # compute. Without it, an account re-seeded from a historical acceptedAt
        # is reducible on its first clean NO with zero grace.
        #
        # Falls back to first_run_at then now, so an account that predates this
        # field (state written by an older version) is protected rather than
        # exposed by the upgrade.
        recorded = (st.first_recorded_at if st and st.first_recorded_at
                    else self.first_run_at or now)
        candidates.append(recorded + dt.timedelta(days=TRACKING_FLOOR_DAYS))

        return max(candidates)

    def is_expired(self, email: str, **kw) -> bool:
        now = kw.get("now") or utcnow()
        return now >= self.deadline_for(email, **kw)

    def days_remaining(self, email: str, **kw) -> float:
        now = kw.get("now") or utcnow()
        delta = self.deadline_for(email, **kw) - now
        return delta.total_seconds() / 86400.0


def default_state_path() -> Path:
    base = os.environ.get("QFLIX_ENTITLEMENT_STATE_DIR")
    if base:
        return Path(base) / "state.json"
    return Path.home() / ".opt" / "maint" / "entitlement" / "state.json"
