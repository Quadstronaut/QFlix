"""lib/mail_members.py -- keep the QFlix mailing list equal to the entitled members (QFLX-125).

Question this module answers: "who should receive QFlix email right now?"
The answer is the same people the entitlement gate lets in: an accepted Plex
share whose plan state is `entitled` or `exempt`, minus plex-only tagalongs
(they ride a household's share and never got newsletters).

WHAT IT DOES TO LISTMONK, and what it refuses to do
---------------------------------------------------
* Only ever touches ONE list (listmonk.list_id, the "Subscribers" list the
  campaigns target). The Stage list is never read or written.
* Removal means "unsubscribe from that list". It NEVER deletes a subscriber,
  never blocklists, never edits attributes. Everything is reversible.
* Re-adding only happens for subscribers THIS module unsubscribed (their ids
  are remembered in mail-sync-state.json). Somebody who unsubscribed
  themselves, or was blocklisted by a bounce, is left exactly as they are.
* It sends nothing. Listmonk only mails when a campaign runs; changing list
  membership through the API sends no message (the list is double opt-in, and
  members are added already `confirmed`).
* Fails closed. Any error before a change is made changes nothing. No answer
  from the entitlement API (state `no-answer`) is a HOLD for that member: not
  removed, not added. An empty eligible set, or more removals than the cap in
  one run, aborts the run untouched (a bug or an outage, never a mass lapse).
* Logs counts only, never an address.

Before the first change of a run it writes a full backup of the subscribers
and their list memberships (mode 0600, newest BACKUP_KEEP kept).
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

from lib.secrets import secrets_dir as _secrets_dir

# Plan states (mirrors qflix-entitlement.py; strings so this module need not import it).
STATE_ENTITLED = "entitled"
STATE_EXEMPT = "exempt"
STATE_NO_ANSWER = "no-answer"

ELIGIBLE_FILE = "mail-eligible.json"
STATE_FILE = "mail-sync-state.json"
BACKUP_PREFIX = "mail-backup-"
BACKUP_KEEP = 14
# Largest number of people one run may take off the list. The first fix removes
# about nine; more than this at once is treated as a broken input, not a lapse.
DEFAULT_MAX_REMOVALS = 12
ELIGIBLE_MAX_AGE_HOURS = 48


class MailSyncError(Exception):
    """Anything that must stop the run before it changes Listmonk."""


# --------------------------------------------------------------------------
# Pure decision logic (no I/O). Everything below is unit tested.
# --------------------------------------------------------------------------

@dataclass
class Eligibility:
    eligible: Set[str] = field(default_factory=set)   # lowercased emails that should receive mail
    hold: Set[str] = field(default_factory=set)        # no answer: change nothing for them
    # Operator allowlist bookkeeping (counts only ever leave this module).
    unlisted_active: int = 0    # gate says active, operator's list does not name them (kept OFF the list)
    listed_inactive: int = 0    # operator's list names them, gate says not active
    # While the two sources disagree the sync may only REMOVE, never add, so it
    # cannot undo the operator's list before he rules on the mismatch.
    remove_only: bool = False


def read_allowlist(secrets: Path) -> Optional[Set[str]]:
    """Household ids the operator named as the only possible recipients
    (secrets/mail-allowlist.txt, one per line, # comments). None = no file, no
    restriction. An unreadable file raises (fail closed)."""
    try:
        text = (secrets / "mail-allowlist.txt").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        raise MailSyncError("mail-allowlist.txt unreadable")
    ids = {ln.split("#")[0].strip() for ln in text.splitlines()}
    ids.discard("")
    if not ids:
        raise MailSyncError("mail-allowlist.txt is empty")
    return ids


def eligibility_from_plans(plans: Iterable, is_plex_only: Callable[[str], bool],
                           household_of: Optional[Callable[[str], Optional[str]]] = None,
                           allow: Optional[Set[str]] = None) -> Eligibility:
    """Turn the gate's per-share plans into who may receive mail.

    `plans` need `.email` and `.state`. `is_plex_only(email)` is True for a
    tagalong account. Anything that is not entitled or exempt is simply absent
    from `eligible`, so it comes off the list. Only accounts with an accepted
    Plex share appear in plans, so a household's other, share-less addresses
    are never eligible (one subscription per member). With `allow` (household
    ids) only those households can be eligible; disagreements between the
    allowlist and the gate are counted and force remove_only.
    """
    out = Eligibility()
    listed_active: Set[str] = set()
    for p in plans:
        email = (p.email or "").strip().lower()
        if not email:
            continue
        if p.state == STATE_NO_ANSWER:
            out.hold.add(email)
            continue
        if is_plex_only(email):
            continue
        hh = household_of(email) if household_of else None
        if p.state not in (STATE_ENTITLED, STATE_EXEMPT):
            # The operator named this household but the gate says inactive: he
            # decides, so leave their subscription exactly as it is (hold).
            if allow is not None and hh in allow:
                out.hold.add(email)
                listed_active.add(hh)
                out.listed_inactive += 1
            continue
        if allow is not None and hh not in allow:
            out.unlisted_active += 1
            continue
        out.eligible.add(email)
        if hh is not None:
            listed_active.add(hh)
    if allow is not None:
        out.listed_inactive += len(allow - listed_active)   # named, but no share/plan to match
        out.remove_only = bool(out.unlisted_active or out.listed_inactive)
    return out


@dataclass
class Actions:
    unsubscribe: List[int] = field(default_factory=list)   # subscriber ids to take off the list
    add: List[int] = field(default_factory=list)           # subscriber ids to put (back) on the list
    removed_state: Set[int] = field(default_factory=set)   # new value of the "we removed these" memory

    @property
    def changes(self) -> int:
        return len(self.unsubscribe) + len(self.add)


def _membership(sub: dict, list_id: int) -> Optional[str]:
    for l in sub.get("lists") or []:
        if int(l["id"]) == list_id:
            return l.get("subscription_status")
    return None


def plan_actions(subscribers: List[dict], elig: Eligibility, list_id: int,
                 removed_ids: Set[int], remove_only: bool = False) -> Actions:
    """Decide what to change. Pure.

    Rules, per subscriber record (matched on lowercased email):
      * blocklisted record: never touched.
      * two records with the same normalised email: the lowest id is canonical,
        the others are taken off the list (duplicate memberships collapse).
      * eligible and on the list (confirmed/unconfirmed): nothing.
      * eligible, unsubscribed, and we are the ones who unsubscribed it: re-add.
      * eligible, unsubscribed by themselves: left alone.
      * not eligible, not on hold, on the list: unsubscribe and remember the id.
      * on hold: nothing.
    """
    act = Actions(removed_state=set(removed_ids))
    by_email: Dict[str, List[dict]] = {}
    for s in subscribers:
        by_email.setdefault((s.get("email") or "").strip().lower(), []).append(s)

    for email, group in by_email.items():
        group.sort(key=lambda s: int(s["id"]))
        for idx, s in enumerate(group):
            sid = int(s["id"])
            if s.get("status") == "blocklisted":
                continue
            status = _membership(s, list_id)           # None = not a member at all
            on_list = status in ("confirmed", "unconfirmed")
            if email in elig.hold:
                continue
            if idx > 0 and on_list:                    # duplicate record of one address
                act.unsubscribe.append(sid)
                act.removed_state.discard(sid)
                continue
            if email in elig.eligible and idx == 0:
                if status == "unsubscribed" and sid in removed_ids and not remove_only:
                    act.add.append(sid)
                    act.removed_state.discard(sid)
                continue
            if on_list:                                 # not eligible, still receiving
                act.unsubscribe.append(sid)
                act.removed_state.add(sid)
    return act


# --------------------------------------------------------------------------
# Listmonk access (small, urllib only; same secrets as lib/listmonk.py).
# --------------------------------------------------------------------------

class Listmonk:
    def __init__(self, base: str, user: str, token: str, timeout: int = 20):
        self.base = base.rstrip("/")
        self._auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
        self.timeout = timeout

    @classmethod
    def from_secrets(cls) -> "Listmonk":
        d = _secrets_dir()

        def rd(n: str) -> str:
            try:
                return (d / n).read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                raise MailSyncError("missing secret %s" % n)
        return cls("http://127.0.0.1:%s" % rd("listmonk.port"), rd("listmonk.api_user"),
                   rd("listmonk.api_token"))

    def _req(self, method: str, path: str, body: Optional[dict] = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, method=method, data=data, headers={
            "Content-Type": "application/json", "Authorization": self._auth})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise MailSyncError("listmonk %s %s failed: %s" % (method, path, type(e).__name__))

    def subscribers(self) -> List[dict]:
        out, page = [], 1
        while True:
            res = self._req("GET", "/api/subscribers?per_page=200&page=%d" % page).get("data") or {}
            rows = res.get("results") or []
            out.extend(rows)
            if not rows or page * 200 >= int(res.get("total", 0)):
                return out
            page += 1

    def lists(self) -> List[dict]:
        return (self._req("GET", "/api/lists?per_page=all").get("data") or {}).get("results") or []

    def set_membership(self, ids: List[int], list_id: int, action: str) -> None:
        body = {"ids": ids, "action": action, "target_list_ids": [list_id]}
        if action == "add":
            body["status"] = "confirmed"
        self._req("PUT", "/api/subscribers/lists", body)

    def recipient_count(self, list_id: int) -> int:
        """Listmonk's own count of who a campaign on this list reaches (confirmed, enabled)."""
        res = self._req("GET", "/api/subscribers?list_id=%d&subscription_status=confirmed&per_page=1" % list_id)
        return int((res.get("data") or {}).get("total", 0))


# --------------------------------------------------------------------------
# Files: eligible snapshot, memory of what we removed, backup.
# --------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_eligible(state_dir: Path, elig: Eligibility, now: dt.datetime) -> None:
    """Snapshot for the nightly subscriber sync, which must only add these people."""
    _atomic_write(state_dir / ELIGIBLE_FILE, json.dumps({
        "at": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "emails": sorted(elig.eligible), "hold": sorted(elig.hold)}) + "\n")


def read_eligible(state_dir: Path, now: Optional[dt.datetime] = None,
                  max_age_hours: int = ELIGIBLE_MAX_AGE_HOURS) -> Eligibility:
    """Raises MailSyncError when missing, unreadable or stale (fail closed)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        d = json.loads((state_dir / ELIGIBLE_FILE).read_text(encoding="utf-8"))
        at = dt.datetime.strptime(d["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        emails = {str(e).strip().lower() for e in d["emails"]}
        hold = {str(e).strip().lower() for e in d.get("hold", [])}
    except (OSError, ValueError, KeyError, TypeError):
        raise MailSyncError("%s missing or unreadable" % ELIGIBLE_FILE)
    if now - at > dt.timedelta(hours=max_age_hours):
        raise MailSyncError("%s is older than %dh" % (ELIGIBLE_FILE, max_age_hours))
    return Eligibility(eligible=emails, hold=hold)


def _load_removed(state_dir: Path) -> Set[int]:
    try:
        d = json.loads((state_dir / STATE_FILE).read_text(encoding="utf-8"))
        return {int(i) for i in d.get("removed_ids", [])}
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, TypeError):
        # A corrupt memory file must not guess: with no memory nobody is re-added
        # and nobody un-removed. Stop and let a human look.
        raise MailSyncError("%s unreadable" % STATE_FILE)


def _save_removed(state_dir: Path, ids: Set[int]) -> None:
    _atomic_write(state_dir / STATE_FILE, json.dumps({"removed_ids": sorted(ids)}) + "\n")


def write_backup(state_dir: Path, lists: List[dict], subscribers: List[dict],
                 now: dt.datetime) -> Path:
    p = state_dir / ("%s%s.json" % (BACKUP_PREFIX, now.strftime("%Y%m%dT%H%M%SZ")))
    _atomic_write(p, json.dumps({"ticket": "QFLX-125", "lists": lists,
                                 "subscribers": subscribers}, indent=1) + "\n")
    # Keep the newest BACKUP_KEEP timestamped files; the hand-named pre-fix one is never pruned.
    stamped = sorted(q for q in state_dir.glob(BACKUP_PREFIX + "2*T*Z.json"))
    for old in stamped[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return p


# --------------------------------------------------------------------------
# The run.
# --------------------------------------------------------------------------

@dataclass
class Result:
    ok: bool
    unsubscribed: int = 0
    added: int = 0
    recipients: Optional[int] = None
    detail: str = ""
    mismatch: str = ""

    def line(self) -> str:
        return ("mail-sync: %s unsubscribed=%d added=%d recipients=%s%s"
                % ("ok" if self.ok else "REFUSED", self.unsubscribed, self.added,
                   self.recipients if self.recipients is not None else "?",
                   (" (%s)" % self.detail) if self.detail else ""))


def reconcile(client, list_id: int, elig: Eligibility, state_dir: Path, *,
              now: Optional[dt.datetime] = None, execute: bool = True,
              max_removals: int = DEFAULT_MAX_REMOVALS) -> Result:
    """One idempotent pass. Never raises: any problem returns Result(ok=False)
    having changed nothing that was not already changed."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        if not elig.eligible:
            raise MailSyncError("no eligible members computed; refusing to empty the list")
        removed_ids = _load_removed(state_dir)
        subscribers = client.subscribers()
        act = plan_actions(subscribers, elig, list_id, removed_ids, elig.remove_only)
        if len(act.unsubscribe) > max_removals:
            raise MailSyncError("would remove %d (cap %d); refusing" % (len(act.unsubscribe), max_removals))
        if act.changes and execute:
            write_backup(state_dir, client.lists(), subscribers, now)
            # Unsubscribe first, remember it, then add: the memory is saved as soon as
            # the removal succeeded so a later failure cannot lose track of it.
            if act.unsubscribe:
                client.set_membership(sorted(set(act.unsubscribe)), list_id, "unsubscribe")
                _save_removed(state_dir, removed_ids | set(act.unsubscribe))
            if act.add:
                client.set_membership(sorted(set(act.add)), list_id, "add")
            _save_removed(state_dir, act.removed_state)
        recipients = client.recipient_count(list_id) if execute else None
        return Result(True, len(act.unsubscribe) if execute else 0,
                      len(act.add) if execute else 0, recipients,
                      "" if execute else "dry-run, would unsubscribe=%d add=%d"
                      % (len(act.unsubscribe), len(act.add)))
    except MailSyncError as e:
        return Result(False, detail=str(e))
    except Exception as e:                                   # noqa: BLE001 - fail closed, log class only
        return Result(False, detail="unexpected %s" % type(e).__name__)
