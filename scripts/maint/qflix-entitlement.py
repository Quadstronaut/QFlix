#!/usr/bin/env python3
"""qflix-entitlement.py -- keep Plex and Seerr access in step with Starhold entitlement.

A PROVISIONING and PROTECTION system. The operator invites friends by hand off
the form at qflix.starhold.dev; this observes the Plex share flipping to
accepted, provisions a disabled Seerr account, and thereafter grants or
withdraws access to match https://entitlements.starhold.app.

Access is an AND:

    invited by the operator (an accepted Plex share + a roster household)
        AND
    currently entitled      (the entitlement API says so)

which is why this gates on the bare `entitled` boolean rather than a pledge
amount. The roster is already the allowlist; a price rule on the entitlement
server would be a second, weaker allowlist that can silently disagree with it.

    stage 1  accepted, not entitled  -> Welcome library only, Seerr perms 0
    stage 2  entitled                -> every library, Seerr perms restored
    stage 3  revoked, past grace     -> back to stage 1, share object KEPT

Stage 3 is deliberately identical to stage 1: a revoked member is returned to
the pitch, not evicted. Deleting the Plex share would force a fresh invite they
must accept out of their email.

SHIPS INERT
-----------
Dry-run is the default. Four interlocks must ALL be satisfied to mutate:

    1. members.yaml `armed: true`
    2. --execute (armed on the box via a systemd drop-in, never in the repo)
    3. not inside the Monday maintenance window / window lock
    4. under --max-mutations for this run (overflow DEFERS to the next run)

THE LAW THIS FILE OBEYS (lib/entitlement.py, section 5.3 of the spec)
---------------------------------------------------------------------
Granting needs a clean 200 + entitled:true. Revoking needs a clean 200 +
entitled:false. EVERY other outcome -- timeout, 429, 401, malformed body, a
stale projection reporting not-entitled -- is *no answer*, and no answer moves
nothing in either direction and does not advance the lapse clock.

An entitlement API outage therefore FREEZES this system rather than draining
it. That asymmetry is not a policy this file applies; it is structural, because
the client returns three values and this file only ever branches on
`answer.grants` / `answer.revokes`, which are not complements.

Exit codes are distinct so a cron wrapper cannot conflate "nobody is entitled"
with "I could not ask" -- see EXIT_* below.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "lib"))

import access_state as ST                                    # noqa: E402
import entitlement as ENT                                    # noqa: E402
import members as MEM                                        # noqa: E402
import plexshare as PS                                       # noqa: E402
import seerrusers as SU                                      # noqa: E402

TOOL = "qflix-entitlement"
KUMA_PUSH_KEY = "qflix-entitlement"
KUMA_BASE = "http://127.0.0.1"
DEFAULT_WELCOME_SECTION = "QFlix - Welcome"
DEFAULT_MAX_MUTATIONS = 10
LOG_RETENTION_DAYS = 30

# Blast-radius tripwire: the fraction of governed households that may be REDUCED
# in a single run before the run refuses to reduce anyone at all.
#
# This is defence in depth against a class rather than a cause. Both mass-shrink
# defects found by the adversarial review -- a missing amnesty key, and a lost
# state file -- were different bugs with an identical SHAPE: many accounts
# expiring in the same run. A tripwire on the shape would have stopped both
# without knowing anything about clocks, and will stop the next one too.
#
# A third is a deliberate threshold, not a round number. Real lapses are
# independent events: with ten governed households, one lapsing is 10% and two
# in the same 15-minute window is already unusual. Four at once is not a
# coincidence, it is a bug or a billing-rail outage upstream -- and either way a
# human should look before ten people lose their libraries.
#
# It gates REDUCTIONS ONLY. Grants are never withheld by it: granting access to
# too many people is not a harm this system needs to defend against, and
# withholding a grant during an anomaly would punish members for a bug.
DEFAULT_MAX_REDUCE_PCT = 34

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_ENTITLEMENT_UNAVAILABLE = 3
EXIT_MEDIA_STACK_UNAVAILABLE = 4
EXIT_CONFIG = 5

# Plan states. Strings so they survive into the audit manifest unchanged.
S_EXEMPT = "exempt"
S_ENTITLED = "entitled"
S_PENDING = "pending"
S_EXPIRED = "expired"
S_NO_ANSWER = "no-answer"
S_UNNAMED = "unnamed-share"
S_NOT_ACCEPTED = "not-accepted"


# ===========================================================================
# Logging -- durable file plus stdout, matching the reaper/janitor convention.
# Trust the durable log over journald; that lesson is already paid for twice.
# ===========================================================================
_LOG_FH = None


def _open_log(state_dir: Path) -> None:
    global _LOG_FH
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        _LOG_FH = open(state_dir / ("entitlement-%s.log" % stamp), "a", encoding="utf-8")
        cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - LOG_RETENTION_DAYS * 86400
        # Manifests are pruned on the SAME retention as the logs. They were
        # previously written and never removed: 96 a day, forever, each one a
        # per-member decision record. That is both an unbounded disk leak on a
        # shared slot and a growing pile of member data whose lifetime nobody
        # chose -- the logs beside them already had an answer, and there is no
        # reason for the two to disagree.
        for pattern in ("entitlement-*.log", "manifest-*.json"):
            for old in state_dir.glob(pattern):
                try:
                    if old.stat().st_mtime < cutoff:
                        old.unlink()
                except OSError:
                    pass
    except Exception:
        _LOG_FH = None


def _file_log(line: str) -> None:
    if _LOG_FH is None:
        return
    try:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _LOG_FH.write(stamp + " " + line + "\n")
        _LOG_FH.flush()
    except Exception:
        pass


def log(msg: str) -> None:
    line = "[%s] %s" % (TOOL, msg)
    print(line, flush=True)
    _file_log(line)


def warn(msg: str) -> None:
    line = "[%s] WARNING: %s" % (TOOL, msg)
    print(line, file=sys.stderr, flush=True)
    _file_log(line)


# ===========================================================================
# Run lock. Released via atexit so every one of main()'s return paths -- and a
# crash -- drops it, rather than each early return needing to remember.
# ===========================================================================
def _take_lock(path: Path) -> bool:
    """Acquire an exclusive run lock. False if another live run holds it.

    A lock whose PID is gone is stale and is taken over: the alternative is that
    one SIGKILL wedges the gate permanently, which turns a crash into a silent
    outage of both provisioning and protection.
    """
    import atexit
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                pid = int(path.read_text(encoding="utf-8").split()[0])
            except (ValueError, IndexError, OSError):
                pid = None
            if pid and os.name == "posix":
                try:
                    os.kill(pid, 0)
                    return False                      # a live run holds it
                except ProcessLookupError:
                    pass                              # stale, take it over
                except PermissionError:
                    return False                      # someone else's live pid
        path.write_text("%d\n" % os.getpid(), encoding="utf-8")
    except OSError as e:
        # A lock we cannot write is not a reason to refuse to run; it is a
        # reason to say so. Refusing would let a read-only state directory
        # silently stop all provisioning and all protection.
        warn("could not take the run lock (%s); continuing unlocked" % e)
        return True

    def _release():
        try:
            if path.exists() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                path.unlink()
        except OSError:
            pass

    atexit.register(_release)
    return True


# ===========================================================================
# PII discipline. Member addresses go in the durable log (operator-only, on the
# box, outside the repo) but NEVER into Discord or Kuma, which are chat and a
# public status page respectively.
# ===========================================================================
def mask(email: str) -> str:
    if not email or "@" not in email:
        return "?"
    local, _, domain = email.partition("@")
    keep = local[:2] if len(local) > 2 else local[:1]
    return "%s***@%s" % (keep, domain)


# ===========================================================================
# Kuma + notify. Both best-effort; neither may abort a run.
# ===========================================================================
def _secrets_dir() -> Path:
    env = os.environ.get("MANITOBA_SECRETS")
    if env:
        return Path(env)
    repo = _HERE.parents[1] / "secrets"
    return repo if repo.exists() else Path.home() / "secrets"


def _push_kuma(status: str, msg: str) -> None:
    import urllib.parse
    import urllib.request
    token = os.environ.get("QFLIX_ENTITLEMENT_KUMA_TOKEN", "")
    if not token:
        try:
            data = json.loads((_secrets_dir() / "kuma-push-tokens.json")
                              .read_text(encoding="utf-8"))
            token = data.get(KUMA_PUSH_KEY, "") or ""
        except Exception:
            token = ""
    if not token:
        warn("no Kuma push token under '%s' - heartbeat NOT pushed. A monitor "
             "with no token sits DOWN forever while the push silently exits 0."
             % KUMA_PUSH_KEY)
        return
    try:
        port = (_secrets_dir() / "uptimekuma.port").read_text(encoding="utf-8").strip()
    except Exception:
        port = "17091"
    qs = urllib.parse.urlencode({"status": status, "msg": msg[:200]})
    try:
        urllib.request.urlopen("%s:%s/api/push/%s?%s" % (KUMA_BASE, port, token, qs),
                               timeout=5).read()
    except Exception as exc:
        warn("Kuma push failed (non-fatal): %s" % exc)


def _notify(msg: str, level: str = "info") -> None:
    try:
        from lib.notify import notify
        notify(msg, level)
    except Exception as exc:
        warn("notify failed (non-fatal): %s" % exc)


def in_maintenance_window(now: Optional[dt.datetime] = None) -> bool:
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.weekday() == 0 and 11 <= now.hour < 15:
        return True
    try:
        lock = Path(os.environ.get("MANITOBA_STATE_DIR",
                                   str(Path.home() / ".opt" / "maint"))) / "lock"
        if lock.exists():
            pid = int(lock.read_text(encoding="utf-8").splitlines()[0].strip())
            if os.name == "posix":
                try:
                    os.kill(pid, 0)
                    return True
                except ProcessLookupError:
                    return False
                except PermissionError:
                    return True
    except Exception:
        pass
    return False


# ===========================================================================
# The plan. PURE -- no I/O below this line until apply_plan().
# ===========================================================================
@dataclass
class Plan:
    email: str
    state: str
    reason: str
    household_id: Optional[str] = None
    holder: Optional[str] = None
    plex_target: Optional[List[int]] = None      # None = leave Plex alone
    seerr_target: Optional[int] = None           # None = leave Seerr alone
    provision_plex_id: Optional[int] = None      # set = create a Seerr account
    deadline: Optional[dt.datetime] = None
    days_remaining: Optional[float] = None
    alert: Optional[str] = None                  # something a human must see now

    @property
    def mutates(self) -> bool:
        return (self.plex_target is not None
                or self.seerr_target is not None
                or self.provision_plex_id is not None)

    def to_json(self) -> dict:
        return {
            "email": mask(self.email),
            "state": self.state,
            "reason": self.reason,
            "household": self.household_id,
            "plex_target": self.plex_target,
            "seerr_target": self.seerr_target,
            "provision": self.provision_plex_id is not None,
            "deadline": ST.to_iso(self.deadline),
            "days_remaining": (round(self.days_remaining, 2)
                               if self.days_remaining is not None else None),
            "alert": self.alert,
        }


def plan_for_share(
    *,
    share: "PS.Share",
    household,
    answer: Optional["ENT.Answer"],
    seerr_user: Optional["SU.SeerrUser"],
    state: "ST.AccessState",
    full_ids: Sequence[int],
    minimum_ids: Sequence[int],
    amnesty_until: Optional[dt.datetime],
    grace_days: int,
    new_arrival_days: int,
    member_permissions: int,
    now: dt.datetime,
) -> Plan:
    """Decide what should happen to one accepted Plex share. No I/O.

    Every branch that does nothing says WHY it does nothing, because "no
    action" is the majority outcome and an unexplained no-op is
    indistinguishable from a bug that skipped somebody.
    """
    email = share.email
    clock = dict(amnesty_until=amnesty_until, grace_days=grace_days,
                 new_arrival_days=new_arrival_days, now=now)

    if not share.accepted:
        return Plan(email=email, state=S_NOT_ACCEPTED,
                    reason="invite sent but not yet accepted; nothing exists to "
                           "provision or restrict")

    # --- provisioning is independent of entitlement -----------------------
    # A person who accepted gets a Seerr account whether or not they are
    # entitled, because stage 1 IS "has an account, disabled". Gating
    # provisioning on entitlement would leave new arrivals unable to log in at
    # all, which reads as a broken invite rather than as a pending upgrade.
    provision = None
    if seerr_user is None:
        provision = share.user_id

    # --- exempt: never touched, and never even looked up ------------------
    #
    # Exempt means EXEMPT -- no lookup, no clock, and no provisioning either.
    # An earlier draft provisioned exempt households too, on the theory that a
    # comped member still wants to be able to request things. The first live
    # run showed why that is wrong: it planned to create a Seerr account for
    # the operator's own second Plex account, which deliberately has none.
    # Exempt households are hand-managed by definition, and a system that
    # creates accounts for people the operator has explicitly carved out is
    # doing something nobody asked for. If a comped member needs Seerr, the
    # operator adds them in Seerr.
    if household is not None and household.exempt:
        return Plan(email=email, state=S_EXEMPT, household_id=household.id,
                    reason="household is exempt (%s); access is never gated and "
                           "nothing is provisioned"
                           % (household.reason or "no reason recorded"))

    # --- a share we cannot name -------------------------------------------
    # Never shrunk. Revoking access because a RECORD is missing is how one
    # roster typo evicts a real person; the hole is closed by paging a human.
    if household is None:
        over = sorted(set(share.section_ids) - set(minimum_ids))
        alert = None
        if over:
            alert = ("unnamed share %s holds %d section(s) beyond Welcome and "
                     "is in no members.yaml household - add it, or remove the "
                     "share by hand" % (mask(email), len(over)))
        return Plan(email=email, state=S_UNNAMED, provision_plex_id=provision,
                    alert=alert,
                    reason="accepted share with no roster household; provisioned "
                           "at the floor and reported, never shrunk")

    hid = household.id
    holder = household.billing.holder if household.billing else None
    deadline = state.deadline_for(email, **clock)
    remaining = state.days_remaining(email, **clock)

    # --- no answer: freeze -------------------------------------------------
    if answer is None or not answer.answered:
        why = answer.error if answer is not None else "no lookup performed"
        return Plan(email=email, state=S_NO_ANSWER, household_id=hid, holder=holder,
                    provision_plex_id=provision, deadline=deadline,
                    days_remaining=remaining,
                    reason="entitlement service gave no usable answer (%s); "
                           "neither granting nor revoking, and the lapse clock "
                           "does not advance" % why)

    # --- entitled ----------------------------------------------------------
    if answer.grants:
        acct = state.accounts.get(email.lower())
        want_perms = (acct.seerr_perms_prior if acct and acct.seerr_perms_prior
                      else member_permissions)
        plex_target = (sorted(full_ids)
                       if set(share.section_ids) != set(full_ids) else None)
        seerr_target = None
        if seerr_user is not None and seerr_user.permissions != want_perms:
            seerr_target = want_perms
        return Plan(email=email, state=S_ENTITLED, household_id=hid, holder=holder,
                    plex_target=plex_target, seerr_target=seerr_target,
                    provision_plex_id=provision,
                    reason="entitled; %s" % (
                        "already at full access" if not (plex_target or seerr_target)
                        else "raising to full access"))

    # --- not entitled: pending or expired ----------------------------------
    if now < deadline:
        note = ""
        if answer.never_seen:
            # Overwhelmingly this is a typo in billing.holder, not a person who
            # never subscribed. Say so while there is still time to fix it.
            note = (" -- the entitlement service has NEVER SEEN %s; check "
                    "billing.holder for a typo" % mask(holder or "?"))
        return Plan(email=email, state=S_PENDING, household_id=hid, holder=holder,
                    provision_plex_id=provision, deadline=deadline,
                    days_remaining=remaining,
                    alert=(note.strip(" -") or None) if answer.never_seen else None,
                    reason="not entitled, %.1f day(s) of grace remain%s"
                           % (remaining, note))

    plex_target = (sorted(minimum_ids)
                   if set(share.section_ids) != set(minimum_ids) else None)
    seerr_target = None
    if seerr_user is not None and seerr_user.permissions != SU.PERMISSIONS_DISABLED:
        seerr_target = SU.PERMISSIONS_DISABLED
    # THE NEVER-SEEN ALERT MUST SURVIVE INTO EXPIRED (2026-08-07).
    # It used to fire only in the PENDING branch above, on the reasoning that
    # you want to hear about a typo "while there is still time to fix it". That
    # is backwards: PENDING costs nobody anything, and EXPIRED is the moment the
    # person is actually reduced. So the one warning that distinguishes "did not
    # subscribe" from "we are looking up the wrong address" went silent at
    # exactly the moment it mattered, and stayed silent on every subsequent run
    # because the state never leaves EXPIRED on its own.
    #
    # Concretely: a member subscribes on Patreon under a different address than
    # billing.holder, the entitlement service is never asked about the address
    # they actually pay with, and they are reduced to Welcome as a non-payer —
    # while paying. Louder here than in PENDING, not quieter, because here it is
    # already costing someone their access.
    expired_alert = None
    if answer is not None and answer.never_seen:
        expired_alert = (
            "REDUCING %s but the entitlement service has NEVER SEEN that "
            "address — if they pay under a different one, billing.holder is "
            "wrong and this reduction is a false positive"
            % mask(holder or "?"))
    return Plan(email=email, state=S_EXPIRED, household_id=hid, holder=holder,
                plex_target=plex_target, seerr_target=seerr_target,
                provision_plex_id=provision, deadline=deadline,
                days_remaining=remaining, alert=expired_alert,
                reason="not entitled and grace expired %.1f day(s) ago; %s"
                       % (-remaining,
                          "already at the floor" if not (plex_target or seerr_target)
                          else "reducing to Welcome + Seerr disabled"))


# ===========================================================================
# Reporting
# ===========================================================================
def digest_lines(plans: Sequence[Plan], now: dt.datetime) -> List[str]:
    """The countdown digest. Masked -- this goes to Discord."""
    pending = [p for p in plans if p.state == S_PENDING and p.days_remaining is not None]
    if not pending:
        return []
    pending.sort(key=lambda p: p.days_remaining)
    soonest = pending[0].days_remaining
    header = ("%d household(s) not entitled; soonest reduction in %.1f day(s)"
              % (len(pending), soonest))
    rows = ["  %s (%s) - %.1fd" % (p.household_id or "?", mask(p.email), p.days_remaining)
            for p in pending[:20]]
    if len(pending) > 20:
        rows.append("  ... and %d more" % (len(pending) - 20))
    return [header] + rows


def should_send_digest(plans: Sequence[Plan], now: dt.datetime) -> bool:
    """Weekly while the deadline is far, daily inside the last seven days.

    This answers "is now a sending WINDOW", not "has it already been sent".
    Restricting to one hour is not sufficient on its own: the timer fires at
    :07, :22, :37 and :52, so an hour-keyed test is true four times and sends
    four identical copies -- exactly what an earlier version of this docstring
    claimed it prevented. Deduplication is AccessState.should_digest(), which is
    keyed on the calendar day and persisted.

    Kept as a separate pure function because the two questions have different
    failure modes: this one being wrong sends at the wrong time, the other being
    wrong sends the right thing repeatedly.
    """
    pending = [p for p in plans if p.state == S_PENDING and p.days_remaining is not None]
    if not pending:
        return False
    if now.hour != 17:
        return False
    if min(p.days_remaining for p in pending) <= 7:
        return True
    return now.weekday() == 0


# ===========================================================================
# Apply
# ===========================================================================
@dataclass
class Outcome:
    applied: List[str] = field(default_factory=list)
    deferred: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)


def apply_plan(plan: Plan, *, plex: "PS.PlexShareClient", share: "PS.Share",
               seerr: "SU.SeerrClient", seerr_user: Optional["SU.SeerrUser"],
               state: "ST.AccessState", execute: bool) -> Tuple[List[str], List[str]]:
    """Perform one plan's mutations. Returns (applied, failed) descriptions."""
    applied: List[str] = []
    failed: List[str] = []
    who = mask(plan.email)

    if plan.provision_plex_id is not None:
        desc = "provision Seerr account for %s (disabled)" % who
        if not execute:
            applied.append("DRY-RUN " + desc)
        else:
            try:
                created = seerr.import_from_plex([plan.provision_plex_id])
                made = next((u for u in created
                             if u.email.lower() == plan.email.lower()
                             or u.plex_id == plan.provision_plex_id), None)
                if made is None:
                    failed.append(desc + " - import returned no matching user")
                else:
                    # Seerr grants defaultPermissions on import. Verify and
                    # force to 0 rather than trusting the setting, because the
                    # whole point of stage 1 is that a new arrival cannot
                    # request before they are entitled.
                    if made.permissions != SU.PERMISSIONS_DISABLED:
                        made = seerr.disable(made)
                    state.remember_seerr(plan.email, user_id=made.id)
                    applied.append(desc)
                    seerr_user = made
            except Exception as e:
                failed.append("%s - %s" % (desc, e))

    if plan.seerr_target is not None and seerr_user is not None:
        desc = "Seerr %s permissions %d -> %d" % (who, seerr_user.permissions,
                                                  plan.seerr_target)
        if not execute:
            applied.append("DRY-RUN " + desc)
        else:
            try:
                # Save what they had BEFORE zeroing it. Doing this after the
                # write would record the 0 we just set and destroy the only
                # record of what to restore.
                if plan.seerr_target == SU.PERMISSIONS_DISABLED:
                    state.remember_seerr(plan.email, user_id=seerr_user.id,
                                         perms_prior=seerr_user.permissions)
                seerr.set_permissions(seerr_user, plan.seerr_target)
                applied.append(desc)
            except Exception as e:
                failed.append("%s - %s" % (desc, e))

    if plan.plex_target is not None:
        desc = "Plex %s sections %d -> %d" % (who, len(share.section_ids),
                                              len(plan.plex_target))
        if not execute:
            applied.append("DRY-RUN " + desc)
        else:
            try:
                plex.set_sections(share, plan.plex_target)
                applied.append(desc)
            except Exception as e:
                failed.append("%s - %s" % (desc, e))

    if applied and execute:
        state.record_action(plan.email, plan.state)
    return applied, failed


# ===========================================================================
# Main
# ===========================================================================
def build_args(argv=None):
    p = argparse.ArgumentParser(
        description="Reconcile QFlix Plex/Seerr access against Starhold entitlement.")
    p.add_argument("--execute", action="store_true",
                   help="actually mutate. Absent = dry run. Arm on the box via a "
                        "systemd drop-in, never in the repo.")
    p.add_argument("--members", default=None, help="roster path (default: secrets/members.yaml)")
    p.add_argument("--state-dir", default=None, help="durable state directory")
    p.add_argument("--welcome-section", default=DEFAULT_WELCOME_SECTION)
    p.add_argument("--max-mutations", type=int, default=DEFAULT_MAX_MUTATIONS,
                   help="per-run cap; overflow DEFERS to the next run")
    p.add_argument("--max-reduce-pct", type=int, default=DEFAULT_MAX_REDUCE_PCT,
                   help="refuse ALL reductions if more than this %% of governed "
                        "households would be reduced in one run (0 disables)")
    p.add_argument("--member-permissions", type=int, default=SU.MEMBER_PERMISSIONS,
                   help="Seerr bitfield granted to an entitled member with no saved prior")
    p.add_argument("--machine-id", default=None, help="Plex machineIdentifier")
    p.add_argument("--ignore-window", action="store_true",
                   help="run inside the maintenance window (testing only)")
    p.add_argument("--json", action="store_true", help="emit the plan as JSON")
    p.add_argument("--no-kuma", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    return p.parse_args(argv)


def _plex_machine_id(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    env = os.environ.get("QFLIX_PLEX_MACHINE_ID")
    if env:
        return env
    d = _secrets_dir()
    for name in ("plex.machine_id", "plex.machineIdentifier"):
        try:
            v = (d / name).read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            continue
    # Last resort: ask the local PMS.
    import urllib.request
    import xml.etree.ElementTree as ET
    try:
        port = (d / "plex.port").read_text(encoding="utf-8").strip()
        host = (d / "plex.host").read_text(encoding="utf-8").strip() or "127.0.0.1"
        with urllib.request.urlopen("http://%s:%s/identity" % (host, port), timeout=10) as r:
            return ET.fromstring(r.read().decode("utf-8")).get("machineIdentifier") or ""
    except Exception as e:
        # ValueError, not SystemExit. SystemExit bypasses main()'s except clause
        # and exits 1 -- the code reserved for "partial failure, some accounts
        # were processed" -- while skipping the Kuma push entirely. A cron
        # wrapper would read that as a normal partial run and the monitor would
        # stay green through a total inability to reach Plex at all.
        raise ValueError("cannot determine Plex machineIdentifier: %s" % e)


def main(argv=None) -> int:
    args = build_args(argv)
    now = dt.datetime.now(dt.timezone.utc)

    state_dir = (Path(args.state_dir) if args.state_dir
                 else ST.default_state_path().parent)
    _open_log(state_dir)

    # ---- run lock --------------------------------------------------------
    # Two concurrent runs would each read the pre-mutation Seerr permissions and
    # each save them as `seerr_perms_prior`. If one has already written 0, the
    # other saves 0 as the value to restore, and the member is permanently
    # downgraded to no permissions with the log reporting a clean restore.
    # A manual run overlapping the timer is the realistic way this happens.
    lock = state_dir / "run.lock"
    if not _take_lock(lock):
        log("another run holds %s - exiting without doing anything" % lock)
        return EXIT_OK

    # ---- roster ----------------------------------------------------------
    try:
        roster_path = MEM.find_roster(Path(args.members) if args.members else None)
        roster = MEM.load(roster_path)
    except MEM.MembersError as e:
        # The detail goes to the durable log (operator-only, on the box). It does
        # NOT go to Kuma: members.py's validation messages quote the offending
        # rows verbatim -- "%s appears in both %r and %r" -- so the text embeds
        # real member addresses and payer references, and the Kuma status page
        # is a surface this repo has already leaked member data through once.
        warn("roster invalid: %s" % e)
        if not args.no_kuma:
            _push_kuma("down", "roster failed validation - see the durable log "
                               "at ~/.opt/maint/entitlement/")
        return EXIT_CONFIG

    armed_roster, arm_reason = MEM.gate_is_armed(roster)
    amnesty = ST.parse_amnesty(_roster_default(roster_path, "amnesty_until"))
    new_arrival_days = int(_roster_default(roster_path, "new_arrival_days")
                           or ST.DEFAULT_NEW_ARRIVAL_DAYS)
    grace_days = roster.grace_days

    window = in_maintenance_window(now) and not args.ignore_window
    execute = bool(args.execute) and armed_roster and not window

    log("roster=%s households=%d armed=%s (%s) window=%s execute=%s"
        % (roster_path.name, len(roster.households), armed_roster, arm_reason,
           window, execute))
    log("clocks: amnesty_until=%s grace_days=%d new_arrival_days=%d"
        % (ST.to_iso(amnesty) or "unset", grace_days, new_arrival_days))

    # ---- media stack -----------------------------------------------------
    try:
        token = (_secrets_dir() / "plex.token").read_text(encoding="utf-8").strip()
        plex = PS.PlexShareClient(token=token, machine_id=_plex_machine_id(args.machine_id))
        sections = plex.sections()
        shares = plex.shares()
    except (PS.PlexShareError, OSError, ValueError) as e:
        warn("Plex unavailable: %s" % e)
        if not args.no_kuma:
            _push_kuma("down", "Plex unavailable: %s" % e)
        return EXIT_MEDIA_STACK_UNAVAILABLE

    try:
        minimum_ids = PS.minimum_access_ids(sections, args.welcome_section)
    except PS.PlexShareError as e:
        warn(str(e))
        if not args.no_kuma:
            _push_kuma("down", "welcome section missing")
        return EXIT_CONFIG
    full_ids = PS.full_access_ids(sections)

    try:
        seerr = SU.client_from_secrets()
        seerr_users = seerr.users()
    except SU.SeerrError as e:
        warn("Seerr unavailable: %s" % e)
        if not args.no_kuma:
            _push_kuma("down", "Seerr unavailable: %s" % e)
        return EXIT_MEDIA_STACK_UNAVAILABLE

    drift = SU.check_default_permissions(seerr)
    if drift:
        warn(drift)

    by_plex_id = {u.plex_id: u for u in seerr_users if u.plex_id}
    by_email = {u.email.lower(): u for u in seerr_users if u.email}

    # ---- state + cohort seeding (happens even when disarmed) -------------
    state = ST.AccessState.load(state_dir / "state.json")
    first_run = state.is_first_run()
    accepted = [(s.email, s.accepted_at) for s in shares if s.accepted and s.email]
    added = state.seed(accepted, now=now)
    if first_run:
        log("FIRST RUN: seeded %d accepted share(s) into the launch cohort "
            "(amnesty applies)" % len(added))
    elif added:
        log("new arrival(s): %s" % ", ".join(mask(e) for e in added))

    # PERSIST THE COHORT NOW, before anything below can return early.
    #
    # Every exit path after this point -- entitlement client unbuildable, all
    # lookups failed -- used to return without saving, so a first run during an
    # outage discarded first_run_at and the cohort tags. The next successful run
    # would then be "first" again, and any share that had genuinely arrived in
    # between would be back-dated into the LAUNCH cohort and handed an amnesty
    # it never earned. Seeding is an observation, not a decision; it is valid
    # whether or not the rest of the run succeeds.
    if state.dirty:
        try:
            state.save()
        except OSError as e:
            warn("could not persist the seeded cohort: %s" % e)

    # ---- entitlement lookups, one per household --------------------------
    ent_client = None
    try:
        ent_client = ENT.client_from_secrets()
    except ValueError as e:
        warn("entitlement client unavailable: %s" % e)
        if not args.no_kuma:
            _push_kuma("down", str(e))
        return EXIT_ENTITLEMENT_UNAVAILABLE

    roster_by_email = roster.by_email()
    answers: Dict[str, ENT.Answer] = {}
    for h in roster.households:
        if h.exempt or not h.billing or not h.billing.holder:
            continue
        holder = h.billing.holder.lower()
        if holder not in answers:
            answers[holder] = ent_client.lookup(holder)

    unanswered = sum(1 for a in answers.values() if not a.answered)
    if answers and unanswered == len(answers):
        # Every single lookup failed. That is an outage, not a mass lapse.
        warn("all %d entitlement lookup(s) failed - treating as an outage and "
             "changing nothing" % len(answers))
        if not args.no_kuma:
            _push_kuma("down", "entitlement API unreachable for all %d lookups"
                       % len(answers))
        return EXIT_ENTITLEMENT_UNAVAILABLE

    # ---- record the clean answers into the clocks, BEFORE planning --------
    #
    # ORDER IS LOAD-BEARING. An earlier version recorded after planning, on the
    # theory that a plan should be computed against the state that produced its
    # deadline. That reasoning quietly cancelled the lapse grace: on the very
    # run that first observes a member going false, `went_false_at` was still
    # None at plan time, so the 7-day clock contributed nothing and the deadline
    # fell back to the cohort clock alone. Once the amnesty date has passed --
    # which the roster tells the operator to delete -- that cohort clock is in
    # the past, so the member was reduced on the same run that first noticed
    # they had lapsed. They got zero days of the week they were promised.
    #
    # Recording first means the clock exists before it is read, which is the
    # only order in which a grace period can be granted at all.
    for share in shares:
        if not share.accepted or not share.email:
            continue
        hh = roster_by_email.get(share.email.lower())
        if hh is None or hh.exempt or not hh.billing:
            continue
        a = answers.get((hh.billing.holder or "").lower())
        if a is None:
            continue
        if a.grants:
            state.record_entitled(share.email, now=now)
        elif a.revokes:
            state.record_not_entitled(share.email, now=now)

    # ---- plan ------------------------------------------------------------
    plans: List[Plan] = []
    nameless = 0
    for share in shares:
        if not share.email:
            # A share with no email cannot be matched to a household, a Seerr
            # account, or a state row. Counted and reported rather than silently
            # dropped: an invisible share is one the operator cannot audit, and
            # "14 shares, 14 planned" is the only arithmetic that proves nothing
            # was skipped.
            nameless += 1
            continue
        hh = roster_by_email.get(share.email.lower())
        holder = (hh.billing.holder.lower()
                  if hh and hh.billing and hh.billing.holder else None)
        answer = answers.get(holder) if holder else None
        su = by_plex_id.get(share.user_id) or by_email.get(share.email.lower())
        plans.append(plan_for_share(
            share=share, household=hh, answer=answer, seerr_user=su, state=state,
            full_ids=full_ids, minimum_ids=minimum_ids, amnesty_until=amnesty,
            grace_days=grace_days, new_arrival_days=new_arrival_days,
            member_permissions=args.member_permissions, now=now))
    if nameless:
        warn("%d Plex share(s) carry no email address and were not planned; "
             "they cannot be matched to a household or a Seerr account" % nameless)

    # ---- audit manifest BEFORE any mutation ------------------------------
    mutating = [p for p in plans if p.mutates]
    manifest = {
        "tool": TOOL,
        "at": ST.to_iso(now),
        "execute": execute,
        "armed_roster": armed_roster,
        "window": window,
        "welcome_section": args.welcome_section,
        "full_section_ids": full_ids,
        "minimum_section_ids": minimum_ids,
        "seerr_default_permissions_drift": drift,
        "plans": [p.to_json() for p in plans],
    }
    try:
        mpath = state_dir / ("manifest-%s.json" % now.strftime("%Y%m%dT%H%M%SZ"))
        mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        warn("could not write audit manifest (continuing): %s" % e)

    if args.json:
        print(json.dumps(manifest, indent=2))

    # ---- summary ---------------------------------------------------------
    counts: Dict[str, int] = {}
    for p in plans:
        counts[p.state] = counts.get(p.state, 0) + 1
    log("shares=%d %s" % (len(plans), " ".join("%s=%d" % kv for kv in sorted(counts.items()))))
    for p in plans:
        log("  %-14s %-22s %s" % (p.state, mask(p.email), p.reason))

    # ---- blast-radius tripwire -------------------------------------------
    reducing = [p for p in mutating if p.state == S_EXPIRED]
    governed = [p for p in plans
                if p.state in (S_ENTITLED, S_PENDING, S_EXPIRED, S_NO_ANSWER)]
    tripped = False
    if args.max_reduce_pct > 0 and reducing and governed:
        pct = 100.0 * len(reducing) / len(governed)
        if pct > args.max_reduce_pct:
            tripped = True
            msg = ("REFUSING to reduce: %d of %d governed household(s) (%.0f%%) "
                   "would lose access in one run, over the %d%% tripwire. Real "
                   "lapses are independent events; this many at once is a bug or "
                   "an upstream billing outage. Grants still applied. Override "
                   "with --max-reduce-pct 0 after looking."
                   % (len(reducing), len(governed), pct, args.max_reduce_pct))
            warn(msg)
            _notify("QFlix entitlement: " + msg, "warn")
            mutating = [p for p in mutating if p.state != S_EXPIRED]

    # ---- apply -----------------------------------------------------------
    out = Outcome()
    share_by_email = {s.email.lower(): s for s in shares if s.email}
    budget = max(0, int(args.max_mutations))
    for p in mutating:
        if budget <= 0:
            out.deferred.append(mask(p.email))
            continue
        share = share_by_email.get(p.email.lower())
        if share is None:
            continue
        su = by_plex_id.get(share.user_id) or by_email.get(p.email.lower())
        applied, failed = apply_plan(p, plex=plex, share=share, seerr=seerr,
                                     seerr_user=su, state=state, execute=execute)
        out.applied.extend(applied)
        out.failed.extend(failed)
        if applied or failed:
            budget -= 1

    for line in out.applied:
        log("APPLIED " + line)
    for line in out.failed:
        warn("FAILED  " + line)
    if out.deferred:
        log("DEFERRED to next run (max-mutations=%d): %s"
            % (args.max_mutations, ", ".join(out.deferred)))

    try:
        if state.dirty:
            state.save()
    except OSError as e:
        warn("could not persist state (clocks may repeat): %s" % e)

    # ---- notify ----------------------------------------------------------
    if not args.no_notify:
        # THROTTLED. This runs 96 times a day, so anything sent without memory
        # is sent 96 times, and a channel that fires 96 times a day is a channel
        # the operator mutes inside a week. Muting THIS one is particularly
        # costly: the unnamed-share page is the designed backstop for the single
        # case the system deliberately refuses to handle automatically, so its
        # value is entirely in being read.
        #
        # At most one alert per account per day, and immediately again if the
        # text CHANGES -- "holds 1 section" becoming "holds 5 sections" is an
        # escalation, not a repeat.
        for p in plans:
            if p.alert and state.should_alert(p.email, p.alert, now):
                _notify("QFlix entitlement: %s" % p.alert, "warn")
                state.mark_alert(p.email, p.alert, now)
        if execute:
            # Mutations are rare and each one is a distinct event, so these are
            # not throttled. If they ever become chatty, that is itself the
            # signal something is looping.
            for line in out.applied:
                _notify("QFlix entitlement: %s" % line, "info")
        if should_send_digest(plans, now) and state.should_digest(now):
            body = digest_lines(plans, now)
            if body:
                _notify("QFlix entitlement countdown\n" + "\n".join(body), "info")
                state.mark_digest(now)
        if state.dirty:
            try:
                state.save()
            except OSError as e:
                warn("could not persist notification throttle state: %s" % e)

    # ---- heartbeat -------------------------------------------------------
    status = "up"
    rc = EXIT_OK

    summary = "%d share(s); %s" % (
        len(plans), " ".join("%s=%d" % kv for kv in sorted(counts.items())))
    if tripped:
        # RED, not a warning. A tripped blast-radius rail means the system
        # believes something is badly wrong with its own inputs, and it has
        # just declined to act on that belief. That is precisely the state a
        # human must be pulled into -- a green monitor would let it sit.
        status, rc = "down", EXIT_PARTIAL
        summary = ("BLAST-RADIUS TRIPWIRE: refused to reduce %d of %d governed; %s"
                   % (len(reducing), len(governed), summary))
    elif out.failed:
        status, rc = "down", EXIT_PARTIAL
        summary = "%d failure(s); %s" % (len(out.failed), summary)
    elif not execute:
        summary = ("report-only (%s); " % ("disarmed" if not armed_roster
                                           else "window" if window else "no --execute")) + summary
    if not args.no_kuma:
        _push_kuma(status, summary)
    log("done: %s" % summary)
    return rc


def _roster_default(path: Path, key: str):
    """Read one key out of the roster's `defaults:` block.

    lib/members.py's Roster deliberately exposes only the fields it validates,
    and adding fields to it from this branch would collide with master's own
    work on the same file. Reading the raw YAML for the two clock knobs keeps
    this system's additions entirely on this side of the boundary.
    """
    try:
        import yaml
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return ((raw.get("defaults") or {}) or {}).get(key)
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
