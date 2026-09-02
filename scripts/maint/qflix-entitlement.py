#!/usr/bin/env python3
"""qflix-entitlement.py -- keep Plex and Seerr access in step with Starhold entitlement.

A PROVISIONING and PROTECTION system. The operator invites friends by hand off
the form at qflix.starhold.dev; this observes the Plex share flipping to
accepted, provisions a disabled Seerr account, and thereafter grants or
withdraws access to match https://entitlements.quadstronix.dev.

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

There is a fourth outcome that is not a stage because it is not a position on
that ladder: a household the entitlement service has NO RECORD of. That is a
lookup MISS, not a verdict, and it is frozen in place (S_UNKNOWN_PAYER) rather
than walked down the ladder -- ten of the live roster's households are on
`rail: manual`, which the service cannot see by construction.

Stage 3 is deliberately identical to stage 1: a revoked member is returned to
the pitch, not evicted. Deleting the Plex share would force a fresh invite they
must accept out of their email.

SHIPS INERT
-----------
Dry-run is the default. FIVE conditions must ALL hold before anything mutates:

    1. members.yaml `armed: true`
    2. members.yaml has ZERO UNRESOLVED HOUSEHOLDS -- every non-exempt household
       needs amount_usd, rail, and (for email-reporting rails) payer_ref.
       lib/members.gate_is_armed() requires this AND condition 1; flipping
       `armed: true` while any household is unresolved leaves the gate inert.
       It is listed separately because it is invisible: the switch is on, the
       log says armed=False, and the natural next move -- resolving every
       household in one edit -- arms all of them simultaneously. Resolve them
       ONE AT A TIME.
    3. --execute (armed on the box via a systemd drop-in, never in the repo)
    4. not inside the Monday maintenance window / window lock
    5. under --max-mutations for this run (overflow DEFERS to the next run)

Plus one rail that cannot be satisfied, only tripped: --max-reduce-pct. If more
than a third of governed households would be REDUCED in a single run, the run
reduces nobody, pages Discord, and goes RED.

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
import oracle_state as OSTATE                                 # noqa: E402
import payer_oracle as ORACLE                                 # noqa: E402
import plexshare as PS                                       # noqa: E402
import seerrusers as SU                                      # noqa: E402

TOOL = "qflix-entitlement"
KUMA_PUSH_KEY = "qflix-entitlement"
KUMA_BASE = "http://127.0.0.1"
DEFAULT_WELCOME_SECTION = "QFlix - Welcome"
DEFAULT_MAX_MUTATIONS = 10
LOG_RETENTION_DAYS = 30

# How many audit manifests survive. See prune_manifests() for the whole WHY;
# 672 is grace_days(7) x 96 runs/day, i.e. full-resolution coverage of the
# entire grace window a disputed reduction can live inside.
MANIFEST_RETENTION_RUNS = 672

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
EXIT_ARM_CHECK_RED = 2        # --arm-check / --oracle-check: red verdict or a would-be reduction
EXIT_ENTITLEMENT_UNAVAILABLE = 3
EXIT_MEDIA_STACK_UNAVAILABLE = 4
EXIT_CONFIG = 5

DEFAULT_SETTLE_DAYS = ORACLE.DEFAULT_SETTLE_DAYS

# Plan states. Strings so they survive into the audit manifest unchanged.
S_EXEMPT = "exempt"
S_ENTITLED = "entitled"
S_PENDING = "pending"
S_EXPIRED = "expired"
S_NO_ANSWER = "no-answer"
# A LOOKUP MISS. The service answered cleanly and its answer was "I have no
# record of this address at all" (HTTP 200, entitled:false, reason:"unknown").
# That is an absence of evidence, not evidence of absence -- see the branch in
# plan_for_share() for the whole argument. Frozen exactly like S_NO_ANSWER:
# nothing is granted, nothing is reduced, and the lapse clock does not advance.
S_UNKNOWN_PAYER = "unknown-payer"
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
        # Logs rotate DAILY, so an age rule alone bounds them at 30 files.
        # Manifests do not -- they are per-RUN, 96 a day -- so they are pruned
        # by COUNT in prune_manifests(), called after the manifest is written.
        for old in state_dir.glob("entitlement-*.log"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    except Exception:
        _LOG_FH = None


def prune_manifests(state_dir: Path, keep: int = MANIFEST_RETENTION_RUNS) -> int:
    """Retain the `keep` newest manifest-*.json. Returns how many were removed.

    WHY THIS EXISTS
    ---------------
    Observed live 2026-08-19: 1259 manifest-*.json / 14 MB / 1275 dirents in one
    flat ~/.opt/maint/entitlement/, back to the first armed run on 2026-08-07 --
    thirteen days of a writer that never stops. The 30-day age rule that shipped
    with these files had simply never fired yet, and would not have saved the
    directory when it did: at 96 runs a day a 30-day age rule settles at ~2880
    files. An age rule is the right shape for the daily-rotated LOG beside it
    and the wrong shape for a per-run artefact.

    The house pattern for a per-run artefact is a COUNT (prune-app-backups.sh
    keeps 3 per app, for the same reason: an age rule on a bursty writer either
    keeps nothing or keeps everything, depending on the burst).

    WHY 672 AND NOT SOME ROUND NUMBER
    ---------------------------------
    The thing a manifest has to be able to settle is a disputed grant or
    revoke: "why did this household lose their libraries on the 14th". Every
    reduction is the end of a grace clock (defaults.grace_days = 7 in the live
    roster), so the decision record that matters is every run between the clock
    starting and access changing. 7 days x 96 runs/day = 672 manifests keeps
    that entire window at full resolution -- not a sample of it.

    Older than that is not lost, it is just coarser: the durable log beside
    these files carries the same per-member `state / masked-address / reason`
    line for every run and is kept 30 days. So the pair is deliberate --
    machine-readable snapshots for the disputable window, human-readable text
    for the month. Steady state is ~7.5 MB and 672 dirents instead of unbounded.

    Both rules apply, whichever bites first: past the count, or past
    LOG_RETENTION_DAYS. The second only matters if the timer cadence ever
    slows -- 672 hourly manifests would be 28 days of history and the member
    decision records inside them would outlive the lifetime the logs declare.

    THE NEWEST FILE IS NEVER DELETED, under any rule, even if it is somehow
    older than the age cutoff (a restored backup, a clock step). A state dir
    with zero manifests cannot answer anything.

    Deleting is done from ONE snapshot listing, sorted by filename -- the name
    carries a fixed-width UTC stamp, so lexicographic order IS chronological
    and it survives the mtime rewrite a copy or a restore would cause. A
    manifest written concurrently sorts newest and can therefore never appear
    in a delete list computed from the older snapshot. unlink() is the atomic
    primitive; nothing is moved, truncated, or rewritten in place, so an
    interrupted prune leaves a strictly smaller valid set of manifests.
    """
    try:
        found = sorted(state_dir.glob("manifest-*.json"), key=lambda p: p.name,
                       reverse=True)
    except OSError:
        return 0
    if len(found) <= 1:
        return 0
    keep = max(1, int(keep))
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - LOG_RETENTION_DAYS * 86400
    doomed = list(found[keep:])                       # past the count cap
    for p in found[1:keep]:                           # index 0 is never touched
        try:
            if p.stat().st_mtime < cutoff:
                doomed.append(p)
        except OSError:
            pass
    removed = 0
    for p in doomed:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


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

    def _create_exclusive() -> bool:
        """Atomically create the lock. O_CREAT|O_EXCL is the whole fix
        (council 2026-08-18): the old exists()->read->write sequence was
        check-then-act, so the 15-minute timer racing a manual run could BOTH
        pass the check and BOTH mutate state. With O_EXCL the kernel picks
        exactly one winner."""
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, ("%d\n" % os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)
        return True

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return _create_exclusive() and _register_release(path, atexit)
        except FileExistsError:
            pass
        # Lock exists. Decide live vs stale, then RETRY EXCLUSIVELY - the
        # unlink+create window is itself a race, and losing that race must
        # mean standing down, not overwriting the winner.
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
        # Stale (dead pid / unreadable), or non-posix where liveness cannot
        # be asked (dev workstation - the gate's production home is the box).
        try:
            path.unlink()
        except OSError:
            pass
        try:
            return _create_exclusive() and _register_release(path, atexit)
        except FileExistsError:
            return False                          # lost the takeover race
    except OSError as e:
        # A lock we cannot write is not a reason to refuse to run; it is a
        # reason to say so. Refusing would let a read-only state directory
        # silently stop all provisioning and all protection.
        warn("could not take the run lock (%s); continuing unlocked" % e)
        return True


def _register_release(path: Path, atexit) -> bool:
    """Arm the atexit release for a lock we now own. Split out of _take_lock
    so both exclusive-create sites arm it identically. Always True, so the
    call composes as `_create_exclusive() and _register_release(...)`."""
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
    # The entitlement service has no record of this household's billing.holder
    # at all (as opposed to "has a record, and it says no"). REPORTED, NEVER
    # PAGED -- see the never-seen note in the PENDING branch for why.
    never_seen: bool = False

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
            "never_seen": self.never_seen,
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

    # Tagalong (+1) accounts ride the household's entitlement for the Plex
    # share ONLY (operator directive 2026-08-16). Killing `provision` here
    # cancels the provisioning-is-independent-of-entitlement rule above for
    # exactly this class: a tagalong never gets a Seerr account, not even a
    # disabled stage-1 one. Revocation branches below are deliberately
    # UNCHANGED — when the household lapses, the tagalong is reduced right
    # alongside the payer, which is the coupling the flag exists to express.
    tagalong = household.is_plex_only(email)
    if tagalong:
        provision = None

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
        # Tagalongs are pinned to DISABLED regardless of any recorded prior:
        # a perms_prior can only exist from before the account was marked
        # plex_only, and restoring it would re-grant the exact access the
        # flag withdraws.
        want_perms = (SU.PERMISSIONS_DISABLED if tagalong
                      else acct.seerr_perms_prior if acct and acct.seerr_perms_prior
                      else member_permissions)
        plex_target = (sorted(full_ids)
                       if set(share.section_ids) != set(full_ids) else None)

        # A GRANT MAY NEVER TAKE A *CONTENT* SECTION AWAY.
        #
        # `full_ids` is "every section that exists right now, MINUS Welcome",
        # read fresh from plex.tv on every run. parse_sections() refuses only a
        # ZERO-section catalogue, so a poll that returns 2 of 5 sections is
        # accepted as truth -- and an entitled member holding all 5 is then
        # planned down to 2, with the log calling it "raising to full access".
        # Against a third-party API polled 96 times a day, that is not a
        # hypothetical.
        #
        # The rail is a shape check, not a freshness check: while the answer
        # GRANTS, the target must never be a strict subset of the content the
        # person already holds. Growing is fine, identical is fine, shrinking is
        # a catalogue problem and never a decision this branch is allowed to
        # make -- reduction lives in the expiry branch, behind the clocks, alone.
        #
        # WELCOME IS EXCLUDED FROM THE COMPARISON (operator directive
        # 2026-08-17). full_access_ids() subtracts Welcome by construction, so
        # an entitled member who still carries Welcome from a previous lapse
        # holds full+1 -- and a naive `full_ids < share.section_ids` reads that
        # one extra floor section as a truncated catalogue, fires the alert, and
        # cancels the write. That is self-sealing: the write it cancels is the
        # very write that would have dropped Welcome, so the member keeps being
        # shown the "go activate your subscription" video forever WHILE PAYING,
        # and the alert re-fires every day. Removing Welcome from an entitled
        # member is not a reduction, it is the disjointness rule in
        # plexshare.full_access_ids() being enforced. Compare content to
        # content: subtract the floor from both sides first.
        held_content = set(share.section_ids) - set(minimum_ids)
        drops_only_welcome = (plex_target is not None
                              and set(full_ids) == held_content)
        # ANY-REMOVAL, NOT STRICT-SUBSET (council 2026-08-18, arbiter-verified).
        # This guard used to test `set(full_ids) < held_content`. Strict subset
        # has a hole: a truncated poll that BOTH drops held sections AND
        # carries one the member does not hold (a library created in the same
        # 15-minute window as the short read) is not a subset of anything, so
        # the guard stayed False and the member was silently reduced with
        # alert=None. The correct question was never "is the target smaller" -
        # it is "does the target REMOVE any content this member holds". Growing
        # is fine, identical is fine, reshuffling that keeps every held section
        # is fine; a plan that drops even one held content section while the
        # answer GRANTS is a catalogue problem, refused and paged.
        removed = held_content - set(full_ids)
        grant_alert = None
        if plex_target is not None and removed:
            grant_alert = (
                "plex.tv catalogue read would drop %d content section(s) %s "
                "already holds (reported %d, held %d); refusing to reduce an "
                "ENTITLED member on a short catalogue read (truncated poll, or "
                "a stale section id in the share - never a grant's decision)"
                % (len(removed), mask(email), len(full_ids), len(held_content)))
            plex_target = None

        seerr_target = None
        if seerr_user is not None and seerr_user.permissions != want_perms:
            seerr_target = want_perms
        return Plan(email=email, state=S_ENTITLED, household_id=hid, holder=holder,
                    plex_target=plex_target, seerr_target=seerr_target,
                    provision_plex_id=provision, alert=grant_alert,
                    reason="entitled%s; %s" % (
                        " (plex-only tagalong)" if tagalong else "",
                        grant_alert if grant_alert else
                        "already at full access" if not (plex_target or seerr_target)
                        else "dropping the Welcome library (entitled members are "
                        "not shown the activate-your-subscription video)"
                        if drops_only_welcome
                        else "raising to full access"))

    # --- the service has NO RECORD of this address: freeze -----------------
    #
    # A LOOKUP MISS IS NOT A NEGATIVE VERDICT (2026-08-19).
    #
    # entitlement.Answer grades `{"entitled": false, "reason": "unknown"}` as a
    # clean NO, and that grading is deliberately left alone here: it was
    # correct while the service's universe and the QFlix roster were the same
    # set of people, and lib/entitlement.py is right that "never subscribed"
    # has to be revocable or nobody could ever be revoked.
    #
    # That premise is gone, and the file already says so twice below. The
    # Patreon behind the service carries non-QFlix members, and -- the half
    # that matters here -- the live roster carries TEN households on
    # `rail: manual` with `amount_usd: 0`, an operator-encoded "no arrangement
    # in place". The entitlement service cannot see a manual rail. It is not
    # the authority for those households and never was, so its "no record"
    # is not a report about their entitlement; it is a report about its own
    # coverage. Grading coverage as a verdict is exactly the operator law
    # against using missing data as an interlock, pointed the other way:
    # instead of an absence blocking an action, an absence is DRIVING one.
    #
    # What it was driving, live on 2026-08-19: five of twelve shares sat
    # `pending` with a 12-day countdown to reduction, every one of them
    # never-seen. On day twelve all five would have gone EXPIRED together --
    # 5 of 9 governed households, 55%, straight through the 34% blast-radius
    # tripwire. So the miss was never going to mass-revoke; it was going to
    # spend the tripwire on a non-event and then page Discord daily forever
    # about a state that cannot change. A tripwire that fires on a steady
    # state is a tripwire that gets muted, and it is the same channel the
    # unnamed-share page depends on.
    #
    # So the answer is not re-graded, the DECISION is. Same freeze as
    # S_NO_ANSWER: nothing granted, nothing reduced, clock does not advance.
    #
    # DELIBERATELY NOT AN AUTO-GRANT. These households keep exactly what they
    # already hold -- Welcome only, for anyone who was never entitled. The
    # freeze changes nobody's access today; it removes a scheduled reduction
    # that would have been taken on no evidence.
    #
    # And deliberately NOT PAGED (operator directive 2026-08-17): never-seen is
    # an ordinary steady state now. It is REPORTED -- its own plan state so it
    # stops hiding inside `pending=5`, its own masked roll-up line in the run
    # summary, and `unknown_payers` in the audit manifest and --json.
    #
    # The two operator levers, both in members.yaml, both explicit:
    #   * they do pay, on a rail the service sees -> set billing.rail +
    #     payer_ref and they resume normal grading on the next run;
    #   * they are comped or hand-managed -> mark the household exempt.
    # Neither is a switch this file may throw on their behalf.
    if answer.never_seen:
        return Plan(email=email, state=S_UNKNOWN_PAYER, household_id=hid,
                    holder=holder, provision_plex_id=provision,
                    deadline=deadline, days_remaining=remaining,
                    never_seen=True,
                    reason="the entitlement service has no record of %s at all "
                           "(unknown address, or a rail it cannot see); a lookup "
                           "MISS is not a no, so nothing is granted, nothing is "
                           "reduced and the lapse clock is frozen. Set "
                           "billing.rail + payer_ref if they pay on a visible "
                           "rail, or mark the household exempt"
                           % mask(holder or "?"))

    # --- not entitled: pending or expired ----------------------------------
    if now < deadline:
        # PENDING is now only ever a REAL negative verdict -- the service has a
        # record and it says not entitled (status=former_patron / declined /
        # lapsed). The never-seen population that used to share this branch
        # returned above as S_UNKNOWN_PAYER, so `never_seen` is False here by
        # construction and the countdown in the digest counts only households
        # the service can actually see.
        return Plan(email=email, state=S_PENDING, household_id=hid, holder=holder,
                    provision_plex_id=provision, deadline=deadline,
                    days_remaining=remaining, never_seen=False,
                    reason="not entitled (%s), %.1f day(s) of grace remain"
                           % (answer.status or "no status reported", remaining))

    plex_target = (sorted(minimum_ids)
                   if set(share.section_ids) != set(minimum_ids) else None)
    seerr_target = None
    if seerr_user is not None and seerr_user.permissions != SU.PERMISSIONS_DISABLED:
        seerr_target = SU.PERMISSIONS_DISABLED
    # NEVER-SEEN NO LONGER REACHES EXPIRED AT ALL.
    #
    # History, because this line has moved three times. It was PENDING-only,
    # then 2026-08-07 added a never-seen note here too on the reasoning that
    # EXPIRED is the moment the reduction actually costs someone access, then
    # 2026-08-17 dropped the page and kept the note because never-seen had
    # become an ordinary steady state for manual-rail households.
    #
    # 2026-08-19 finished the thought. If never-seen is an ordinary steady
    # state, it is not a verdict about the member, and a state whose whole job
    # is to REDUCE somebody must never be entered on one -- see the
    # S_UNKNOWN_PAYER branch above. The note is gone from here because the
    # condition is: this branch is now reached only when the service HAS a
    # record and that record says no. The one genuinely actionable never-seen
    # signal -- an EVER-ENTITLED declared payer going never-seen, i.e. the sync
    # projection dying -- is caught by payer_oracle.judge() row 3 and still
    # pages, and is unaffected by any of this.
    return Plan(email=email, state=S_EXPIRED, household_id=hid, holder=holder,
                plex_target=plex_target, seerr_target=seerr_target,
                provision_plex_id=provision, deadline=deadline,
                days_remaining=remaining, never_seen=False,
                reason="not entitled (%s) and grace expired %.1f day(s) ago; %s"
                       % (answer.status or "no status reported", -remaining,
                          "already at the floor" if not (plex_target or seerr_target)
                          else "reducing to Welcome + Seerr disabled"))


# ===========================================================================
# The payer oracle -- SPEC section 3. Every function below is PURE (given
# already-fetched facts); the only I/O is in main()'s --oracle-check /
# --arm-check branches and in the periodic observe()/save() of the declared-
# payer clock. lib/payer_oracle.judge() is the single implementation of the
# verdict table; this section only ASSEMBLES its inputs from this file's own
# data model (Roster, AccessState, Answer) so the gate and the canary never
# carry two copies of the table itself.
# ===========================================================================

def declared_payer_households(roster: "MEM.Roster") -> List["MEM.Household"]:
    """SPEC section 3, L1: non-exempt, non-provisional, billing.rail set, and
    billing.amount_usd > 0. `provisional` and `exempt` households are excluded
    even if they carry a stray amount -- both mean "not yet a real bill"."""
    out = []
    for h in roster.households:
        if h.exempt or h.provisional or not h.billing:
            continue
        if h.billing.rail and h.billing.amount_usd and h.billing.amount_usd > 0:
            out.append(h)
    return out


def household_ever_entitled(hh: "MEM.Household", state: "ST.AccessState") -> bool:
    """True if ANY Plex account in this household has ever been entitled,
    per lib/access_state.py's per-account memory."""
    for email in hh.accounts:
        acct = state.accounts.get(email.lower())
        if acct is not None and acct.ever_entitled:
            return True
    return False


def build_declared_payers(
    roster: "MEM.Roster", state: "ST.AccessState",
    declared_at: Dict[str, dt.datetime],
    holder_answers: Dict[str, "ENT.Answer"],
) -> List["ORACLE.DeclaredPayer"]:
    """Assemble payer_oracle.DeclaredPayer rows. Pure -- every input was
    already fetched by the caller (declared_at from oracle_state.observe(),
    holder_answers from ent_client.lookup() per unique billing.holder)."""
    out = []
    for hh in declared_payer_households(roster):
        holder = (hh.billing.holder or "").lower()
        answer = holder_answers.get(holder)
        out.append(ORACLE.DeclaredPayer(
            household_id=hh.id,
            holder=hh.billing.holder,
            first_declared_at=declared_at.get(hh.id),
            ever_entitled=household_ever_entitled(hh, state),
            currently_yes=bool(answer and answer.grants),
            currently_never_seen=bool(answer and answer.never_seen),
        ))
    return out


def oracle_verdict(
    roster: "MEM.Roster", state: "ST.AccessState",
    declared_at: Dict[str, dt.datetime],
    holder_answers: Dict[str, "ENT.Answer"],
    bulk_answer: "ENT.BulkAnswer", now: dt.datetime,
    settle_days: int = DEFAULT_SETTLE_DAYS,
) -> "ORACLE.Verdict":
    """The one call site both --oracle-check and --arm-check use. Pure."""
    payers = build_declared_payers(roster, state, declared_at, holder_answers)
    bulk_facts = ORACLE.BulkFacts.from_bulk_answer(bulk_answer)
    return ORACLE.judge(declared=payers, bulk=bulk_facts, now=now,
                        settle_days=settle_days)


def arm_check_should_block(verdict: "ORACLE.Verdict", plans: Sequence[Plan]) -> bool:
    """True iff `--arm-check` must exit red (EXIT_ARM_CHECK_RED): either the
    oracle verdict itself is red, or the plan set contains at least one
    account that would actually be REDUCED (state EXPIRED and mutating) if
    this run executed. Both are 'do not arm' signals; a red oracle with zero
    pending reductions is still a reason to hold, and vice versa."""
    if verdict.is_red:
        return True
    return any(p.state == S_EXPIRED and p.mutates for p in plans)


def unknown_payers(plans: Sequence[Plan]) -> List[str]:
    """The masked addresses the entitlement service has no record of.

    Sorted and masked because it lands in the audit manifest, in --json and in
    a log line, all of which are durable and forwardable surfaces. This is the
    ANSWER to "which five are stuck": before S_UNKNOWN_PAYER existed they were
    indistinguishable from real lapses inside a `pending=5` count, and the only
    way to find them was to read every reason string.
    """
    return sorted(mask(p.email) for p in plans if p.state == S_UNKNOWN_PAYER)


def would_be_reduced(plans: Sequence[Plan]) -> List[str]:
    """The masked set --arm-check prints: exactly the accounts EXPIRED-and-
    mutating would touch. Masked because this is diagnostic output that may
    be read over someone's shoulder or pasted into a ticket."""
    return sorted(mask(p.email) for p in plans if p.state == S_EXPIRED and p.mutates)


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
    # L-1 (council ledger): household_id was emitted here raw. Discord is a
    # durable forwarded surface, so the digest names members ONLY by masked
    # address -- the id adds nothing the operator can't get from the manifest.
    rows = ["  %s - %.1fd" % (mask(p.email), p.days_remaining)
            for p in pending[:20]]
    if len(pending) > 20:
        rows.append("  ... and %d more" % (len(pending) - 20))
    # Unknown-payer households ride along as an APPENDIX and never trigger a
    # send on their own -- should_send_digest() is deliberately left keyed on
    # the pending countdown. They have no countdown to report and the state
    # does not change on its own, so making them a send reason would turn the
    # weekly digest into a weekly reminder of a fact the operator already
    # knows. Attached to a digest that was going out anyway, it costs nothing.
    unknown = unknown_payers(plans)
    if unknown:
        rows.append("also frozen (service has no record, nothing reduced): "
                    + ", ".join(unknown[:20]))
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


class AllLookupsFailed(Exception):
    """Every entitlement lookup this run attempted came back UNKNOWN. That is
    an outage, not a mass lapse -- see the call site for why the caller must
    change nothing rather than plan against it."""

    def __init__(self, n: int):
        self.n = n
        super().__init__("all %d entitlement lookup(s) failed" % n)


def compute_plans(
    *, roster: "MEM.Roster", state: "ST.AccessState", ent_client: "ENT.EntitlementClient",
    shares: Sequence["PS.Share"], full_ids: Sequence[int], minimum_ids: Sequence[int],
    amnesty: Optional[dt.datetime], grace_days: int, new_arrival_days: int,
    member_permissions: int, by_plex_id: Dict[int, "SU.SeerrUser"],
    by_email: Dict[str, "SU.SeerrUser"], now: dt.datetime,
) -> Tuple[List[Plan], Dict[str, "ENT.Answer"], int]:
    """Look up entitlement for every billed household, record the clean
    answers into `state`'s clocks, and build one Plan per Plex share.

    SHARED by main()'s real run and --arm-check's read-only preview, so the
    two can never compute a different answer to "what would happen" -- the
    exact two-implementations-drift failure this whole change exists to
    close for the oracle table, applied here too. Mutates `state` IN MEMORY
    (record_entitled/record_not_entitled); the caller decides whether to
    persist that by calling state.save() or not.

    Raises AllLookupsFailed if every lookup failed (an outage, not a mass
    lapse) -- the caller must change nothing on that path.

    ORDER IS LOAD-BEARING: the clean answers are recorded into `state`
    BEFORE any Plan is built. See qflix-entitlement.py's git history
    (2026-08-07) for the defect this guards against: recording after
    planning silently cancelled the lapse grace for the very run that first
    observed a member going false.
    """
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
        raise AllLookupsFailed(len(answers))

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
        elif a.revokes and not a.never_seen:
            # THE CLOCK MUST FREEZE ON A MISS TOO, NOT JUST THE DECISION.
            #
            # plan_for_share() returns S_UNKNOWN_PAYER for a never-seen answer
            # and its reason string promises "the lapse clock is frozen". This
            # line was where that promise leaked: `never_seen` is graded
            # verdict=NO (lib/entitlement.py:155 -- NO plus reason=="unknown"),
            # so `a.revokes` was True and a lookup MISS was recorded here as a
            # clean negative verdict. The decision was frozen upstream; the
            # clock underneath it kept running. Half a freeze is not a freeze.
            #
            # Harmless today by luck, not by design: deadline_for() never reads
            # first_not_entitled_at, and it returns max(candidates), so an extra
            # candidate can only push a reduction LATER. Both of those are
            # access_state's business and either could change without anyone
            # thinking about this file.
            #
            # Not harmless in the one case that matters. For an EVER-ENTITLED
            # account, record_not_entitled() also stamps went_false_at, which
            # deadline_for() DOES read as `went_false_at + grace_days`. So a
            # paying member whose entitlement projection dies -- the service
            # stops knowing them at all, payer_oracle.judge() row 3, the exact
            # upstream failure this system is built to survive -- silently burns
            # their entire 7-day grace while frozen, and is reducible on the
            # first run after the projection comes back with a real NO. An
            # outage would have spent the grace it was supposed to suspend.
            # That is the section-5.3 asymmetry ("no answer advances nothing")
            # defeated by a miss being spelled NO rather than UNKNOWN.
            #
            # Costs nothing to be right: a household the service can see is
            # unaffected, and a miss that later becomes a genuine NO starts its
            # grace from THAT run -- the first moment there was a verdict to
            # start it from. Verified against the live state.json 2026-08-20:
            # every account reads went_false_at=null, so no clock is stale today
            # and this fix is purely forward-looking. It stays because the next
            # never-seen account will be one that used to pay.
            state.record_not_entitled(share.email, now=now)

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
            member_permissions=member_permissions, now=now))
    return plans, answers, nameless


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
    p.add_argument("--arm-check", action="store_true",
                   help="read-only: report the payer-oracle verdict and the exact "
                        "set that would be reduced if this run executed. Mutates "
                        "nothing, pushes no Kuma beat. Exits %d if the verdict is "
                        "red OR any account would be reduced." % EXIT_ARM_CHECK_RED)
    p.add_argument("--oracle-check", action="store_true",
                   help="read-only: report ONLY the payer-oracle verdict (SPEC "
                        "section 3), no Plex/Seerr I/O at all. Used by the "
                        "entitlement-service canary's oracle leg. Exits %d on a "
                        "red verdict." % EXIT_ARM_CHECK_RED)
    p.add_argument("--settle-days", type=int, default=DEFAULT_SETTLE_DAYS,
                   help="payer_oracle settle window in days (default %d)"
                        % DEFAULT_SETTLE_DAYS)
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


def _oracle_check(args, now: dt.datetime) -> int:
    """`--oracle-check`: report ONLY the payer-oracle verdict (SPEC section 3).
    No Plex, no Seerr, no mutation of any state file, no Kuma push, no
    notify(). This is what the entitlement-service canary's oracle leg
    invokes on the box. Exits EXIT_ARM_CHECK_RED on a red verdict, EXIT_OK
    otherwise -- anything else (EXIT_CONFIG, EXIT_ENTITLEMENT_UNAVAILABLE)
    means the canary could not assert and must map that to its own exit 2."""
    state_dir = (Path(args.state_dir) if args.state_dir
                else ST.default_state_path().parent)
    _open_log(state_dir)
    try:
        roster_path = MEM.find_roster(Path(args.members) if args.members else None)
        roster = MEM.load(roster_path)
    except MEM.MembersError as e:
        # The detail goes to the durable log only: members.py's validation
        # messages quote offending rows verbatim (real addresses / payer refs),
        # and this read-only path pushes nothing to Kuma by design.
        warn("roster invalid: %s" % e)
        return EXIT_CONFIG

    state = ST.AccessState.load(state_dir / "state.json")
    oracle_state = OSTATE.OracleState.load(state_dir / "oracle-state.json")

    try:
        ent_client = ENT.client_from_secrets()
    except ValueError as e:
        warn("entitlement client unavailable: %s" % e)
        return EXIT_ENTITLEMENT_UNAVAILABLE

    holder_answers: Dict[str, ENT.Answer] = {}
    for hh in declared_payer_households(roster):
        holder = (hh.billing.holder or "").lower()
        if holder and holder not in holder_answers:
            holder_answers[holder] = ent_client.lookup(holder)

    bulk_answer = ent_client.bulk()
    verdict = oracle_verdict(roster, state, oracle_state.first_declared_at,
                             holder_answers, bulk_answer, now,
                             settle_days=args.settle_days)

    detail = verdict.detail.replace("\n", " ")
    log("oracle-check: VERDICT=%s RED=%d DETAIL=%s"
        % (verdict.verdict, int(verdict.is_red), detail))
    print("VERDICT=%s" % verdict.verdict)
    print("RED=%d" % int(verdict.is_red))
    print("DETAIL=%s" % detail)
    return EXIT_ARM_CHECK_RED if verdict.is_red else EXIT_OK


def _arm_check(args, now: dt.datetime) -> int:
    """`--arm-check`: read-only preview of a real run. Reports the payer-
    oracle verdict AND the exact (masked) set of accounts that would be
    REDUCED if this run executed with --execute.

    MUTATES NOTHING: state.json and oracle-state.json are loaded but their
    in-memory copies are never saved (compute_plans() and observe() mutate
    those copies exactly as a real run would, so the preview is accurate --
    the mutation is simply discarded when the process exits instead of
    written to disk). No manifest file is written, no Kuma beat is pushed,
    no notify() call is made.
    """
    state_dir = (Path(args.state_dir) if args.state_dir
                else ST.default_state_path().parent)
    _open_log(state_dir)
    try:
        roster_path = MEM.find_roster(Path(args.members) if args.members else None)
        roster = MEM.load(roster_path)
    except MEM.MembersError as e:
        # The detail goes to the durable log only: members.py's validation
        # messages quote offending rows verbatim (real addresses / payer refs),
        # and this read-only path pushes nothing to Kuma by design.
        warn("roster invalid: %s" % e)
        return EXIT_CONFIG

    # The preview must refuse the same configs the real run refuses (H4), or
    # --arm-check happily rehearses a run that would exit EXIT_CONFIG -- or
    # worse, previews with a different deadline than the run would compute.
    knobs = _validated_knobs(roster_path, roster)
    if knobs is None:
        return EXIT_CONFIG
    amnesty, new_arrival_days, grace_days = knobs

    try:
        token = (_secrets_dir() / "plex.token").read_text(encoding="utf-8").strip()
        plex = PS.PlexShareClient(token=token, machine_id=_plex_machine_id(args.machine_id))
        sections = plex.sections()
        shares = plex.shares()
    except (PS.PlexShareError, OSError, ValueError) as e:
        warn("Plex unavailable: %s" % e)
        return EXIT_MEDIA_STACK_UNAVAILABLE

    try:
        minimum_ids = PS.minimum_access_ids(sections, args.welcome_section)
    except PS.PlexShareError as e:
        warn(str(e))
        return EXIT_CONFIG
    full_ids = PS.full_access_ids(sections, args.welcome_section)

    try:
        seerr = SU.client_from_secrets()
        seerr_users = seerr.users()
    except SU.SeerrError as e:
        warn("Seerr unavailable: %s" % e)
        return EXIT_MEDIA_STACK_UNAVAILABLE

    by_plex_id = {u.plex_id: u for u in seerr_users if u.plex_id}
    by_email = {u.email.lower(): u for u in seerr_users if u.email}

    state = ST.AccessState.load(state_dir / "state.json")               # never .save()d
    oracle_state = OSTATE.OracleState.load(state_dir / "oracle-state.json")  # never .save()d

    try:
        ent_client = ENT.client_from_secrets()
    except ValueError as e:
        warn("entitlement client unavailable: %s" % e)
        return EXIT_ENTITLEMENT_UNAVAILABLE

    try:
        plans, answers, nameless = compute_plans(
            roster=roster, state=state, ent_client=ent_client, shares=shares,
            full_ids=full_ids, minimum_ids=minimum_ids, amnesty=amnesty,
            grace_days=grace_days, new_arrival_days=new_arrival_days,
            member_permissions=args.member_permissions, by_plex_id=by_plex_id,
            by_email=by_email, now=now)
    except AllLookupsFailed as e:
        warn("all %d entitlement lookup(s) failed" % e.n)
        return EXIT_ENTITLEMENT_UNAVAILABLE
    if nameless:
        warn("%d Plex share(s) carry no email address and were not planned"
             % nameless)

    declared_ids = [h.id for h in declared_payer_households(roster)]
    # observe() mutates the in-memory OracleState only -- .save() is never
    # called, so this preview cannot arm a settle-window clock the real gate
    # has not itself started yet.
    declared_at = oracle_state.observe(declared_ids, now=now)
    bulk_answer = ent_client.bulk()
    verdict = oracle_verdict(roster, state, declared_at, answers, bulk_answer,
                             now, settle_days=args.settle_days)

    reduced = would_be_reduced(plans)
    blocked = arm_check_should_block(verdict, plans)

    detail = verdict.detail.replace("\n", " ")
    log("arm-check: VERDICT=%s RED=%d DETAIL=%s would_reduce=%d"
        % (verdict.verdict, int(verdict.is_red), detail, len(reduced)))
    print("VERDICT=%s" % verdict.verdict)
    print("RED=%d" % int(verdict.is_red))
    print("DETAIL=%s" % detail)
    print("WOULD_REDUCE=%d" % len(reduced))
    for who in reduced:
        print("  " + who)
    if blocked:
        print("ARM: DO NOT ARM - %s"
             % ("red verdict" if verdict.is_red
                else "%d account(s) would be reduced" % len(reduced)))
    else:
        print("ARM: safe to arm (oracle not red, nothing would be reduced)")
    return EXIT_ARM_CHECK_RED if blocked else EXIT_OK


def main(argv=None) -> int:
    args = build_args(argv)
    now = dt.datetime.now(dt.timezone.utc)

    if args.oracle_check:
        return _oracle_check(args, now)
    if args.arm_check:
        return _arm_check(args, now)

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

    # VALIDATE THE TWO CLOCK KNOBS THAT members.py DOES NOT.
    #
    # `grace_days` is validated in the roster loader. `new_arrival_days` and
    # `amnesty_until` are read straight out of the YAML by _roster_default() and
    # bypass every check -- which is backwards, because these two move a
    # reduction EARLIER and grace_days only moves it later. A negative
    # new_arrival_days yields a deadline in the past; a non-integer raised
    # inside main() and exited 1 with no Kuma push, the same silent shape that
    # was already fixed once for machineIdentifier.
    raw_nad = _roster_default(roster_path, "new_arrival_days")
    if raw_nad is None:
        new_arrival_days = ST.DEFAULT_NEW_ARRIVAL_DAYS
    elif isinstance(raw_nad, bool) or not isinstance(raw_nad, int) or raw_nad < 1:
        warn("members.yaml defaults.new_arrival_days is %r; it must be an "
             "integer >= 1. A zero or negative window puts a new member's "
             "deadline at or before the moment they accepted." % (raw_nad,))
        if not args.no_kuma:
            _push_kuma("down", "invalid defaults.new_arrival_days in the roster")
        return EXIT_CONFIG
    else:
        new_arrival_days = raw_nad

    raw_amnesty = _roster_default(roster_path, "amnesty_until")
    amnesty = ST.parse_amnesty(raw_amnesty)
    if raw_amnesty is not None and amnesty is None:
        # Present but unparseable is a typo, not an intent to remove it. Removing
        # the key is how you retire the amnesty; a mistyped date must not read as
        # the same thing.
        warn("members.yaml defaults.amnesty_until is %r, which is not a date. "
             "Delete the key to retire the amnesty; do not leave it malformed."
             % (raw_amnesty,))
        if not args.no_kuma:
            _push_kuma("down", "invalid defaults.amnesty_until in the roster")
        return EXIT_CONFIG

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

    # Both sets are computed from the same live section list and both refuse to
    # be empty, so a missing/renamed Welcome section fails the run rather than
    # silently unsharing anybody. They are DISJOINT: Welcome is the floor for
    # the non-entitled and is deliberately absent from full access, because its
    # only content tells the viewer to go and subscribe.
    try:
        minimum_ids = PS.minimum_access_ids(sections, args.welcome_section)
        full_ids = PS.full_access_ids(sections, args.welcome_section)
    except PS.PlexShareError as e:
        warn(str(e))
        if not args.no_kuma:
            _push_kuma("down", "welcome section missing")
        return EXIT_CONFIG

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

    try:
        plans, answers, nameless = compute_plans(
            roster=roster, state=state, ent_client=ent_client, shares=shares,
            full_ids=full_ids, minimum_ids=minimum_ids, amnesty=amnesty,
            grace_days=grace_days, new_arrival_days=new_arrival_days,
            member_permissions=args.member_permissions, by_plex_id=by_plex_id,
            by_email=by_email, now=now)
    except AllLookupsFailed as e:
        # Every single lookup failed. That is an outage, not a mass lapse.
        warn("all %d entitlement lookup(s) failed - treating as an outage and "
             "changing nothing" % e.n)
        if not args.no_kuma:
            _push_kuma("down", "entitlement API unreachable for all %d lookups"
                       % e.n)
        return EXIT_ENTITLEMENT_UNAVAILABLE
    if nameless:
        warn("%d Plex share(s) carry no email address and were not planned; "
             "they cannot be matched to a household or a Seerr account" % nameless)

    # ---- payer-oracle declared-payer clock (SPEC section 3, L1) ----------
    # Observed on EVERY run, armed or not -- same law as cohort seeding
    # above: a clock that only starts once the gate is armed cannot tell
    # "declared last week" from "declared five minutes before arming", and
    # the settle window (payer_oracle.judge() row 6) exists precisely to give
    # a fresh declaration a couple of quiet days before anything red is said
    # about it.
    oracle_state = OSTATE.OracleState.load(state_dir / "oracle-state.json")
    declared_ids = [h.id for h in declared_payer_households(roster)]
    oracle_state.observe(declared_ids, now=now)
    if oracle_state.dirty:
        try:
            oracle_state.save()
        except OSError as e:
            warn("could not persist the declared-payer clock: %s" % e)

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
        # The masked roll-up of every household the entitlement service has no
        # record of. Denormalised out of `plans` on purpose: this is the one
        # list that carries an operator TODO (fix billing.rail, or mark the
        # household exempt), and it must be greppable across manifests without
        # reparsing every plan to find which ones were frozen.
        "unknown_payers": unknown_payers(plans),
        "plans": [p.to_json() for p in plans],
    }
    try:
        mpath = state_dir / ("manifest-%s.json" % now.strftime("%Y%m%dT%H%M%SZ"))
        mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        warn("could not write audit manifest (continuing): %s" % e)
    # AFTER the write, not before: pruning here makes the cap exact (the file
    # this run just added is already counted, and is the one file the sort
    # guarantees is kept). Never fatal -- a full or read-only state dir must
    # not take the gate down, it is a housekeeping chore, not a decision.
    try:
        gone = prune_manifests(state_dir)
        if gone:
            log("pruned %d old audit manifest(s) (keeping the newest %d)"
                % (gone, MANIFEST_RETENTION_RUNS))
    except Exception as e:                                   # noqa: BLE001
        warn("manifest prune failed (continuing): %s" % e)

    if args.json:
        print(json.dumps(manifest, indent=2))

    # ---- summary ---------------------------------------------------------
    counts: Dict[str, int] = {}
    for p in plans:
        counts[p.state] = counts.get(p.state, 0) + 1
    log("shares=%d %s" % (len(plans), " ".join("%s=%d" % kv for kv in sorted(counts.items()))))
    for p in plans:
        log("  %-14s %-22s %s" % (p.state, mask(p.email), p.reason))
    # One roll-up line naming the frozen households, so the operator TODO is
    # visible in the log without reading twelve per-member rows. LOG ONLY --
    # deliberately not _notify()'d: never-seen is a steady state and a channel
    # that fires 96 times a day on a steady state gets muted (2026-08-17).
    unknown = unknown_payers(plans)
    if unknown:
        log("unknown to the entitlement service (frozen, no clock, nothing "
            "reduced): %s -- set billing.rail + payer_ref if they pay on a "
            "visible rail, or mark the household exempt" % ", ".join(unknown))

    # ---- blast-radius tripwire -------------------------------------------
    reducing = [p for p in mutating if p.state == S_EXPIRED]
    # S_UNKNOWN_PAYER counts as GOVERNED even though it is frozen, exactly as
    # S_NO_ANSWER does: the tripwire denominator is "households this gate is
    # responsible for", not "households it moved this run". Leaving the frozen
    # ones out would shrink the denominator and make the 34% rail hair-trigger
    # -- with 4 visible households, a single ordinary lapse is 25% and two are
    # 50%, so the rail would start refusing routine reductions.
    governed = [p for p in plans
                if p.state in (S_ENTITLED, S_PENDING, S_EXPIRED, S_NO_ANSWER,
                               S_UNKNOWN_PAYER)]
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


def _validated_knobs(roster_path: Path, roster: "MEM.Roster"):
    """The H4 clock-knob validation, for the READ-ONLY paths (--arm-check).

    main() keeps its own inline copy because a refusal there must also push
    Kuma down; the preview paths push nothing, so a refusal here just warn()s
    and the caller exits EXIT_CONFIG. Same rules as main(): a zero/negative/
    boolean new_arrival_days is refused, and an amnesty_until that is present
    but unparseable is a typo, never an intent to retire the amnesty.

    Returns (amnesty, new_arrival_days, grace_days) or None on refusal.
    """
    nad = _roster_default(roster_path, "new_arrival_days")
    if nad is None:
        nad_days = ST.DEFAULT_NEW_ARRIVAL_DAYS
    elif isinstance(nad, bool) or not isinstance(nad, int) or nad < 1:
        warn("members.yaml defaults.new_arrival_days is %r; it must be an "
             "integer >= 1 (same refusal as the real run)." % (nad,))
        return None
    else:
        nad_days = nad
    am_raw = _roster_default(roster_path, "amnesty_until")
    am = ST.parse_amnesty(am_raw)
    if am_raw is not None and am is None:
        warn("members.yaml defaults.amnesty_until is %r, which is not a "
             "date; delete the key to retire the amnesty (same refusal as "
             "the real run)." % (am_raw,))
        return None
    return am, nad_days, roster.grace_days


if __name__ == "__main__":
    sys.exit(main())
