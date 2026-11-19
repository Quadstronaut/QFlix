"""tests/unit/test_mail_members.py -- QFLX-125: the QFlix mailing list follows entitlement."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace as NS

import pytest

from lib import mail_members as MM

NOW = dt.datetime(2026, 11, 19, 12, 0, tzinfo=dt.timezone.utc)
LIST = 4


def plan(email, state):
    return NS(email=email, state=state)


def sub(i, email, status="confirmed", extra_lists=(), enabled="enabled"):
    lists = [{"id": LIST, "subscription_status": status}] if status else []
    lists += [{"id": x, "subscription_status": "confirmed"} for x in extra_lists]
    return {"id": i, "email": email, "status": enabled, "lists": lists}


class Fake:
    """In-memory Listmonk. `fail` makes every call raise."""

    def __init__(self, subs, fail=False):
        self.subs, self.fail, self.calls = subs, fail, []

    def subscribers(self):
        if self.fail:
            raise MM.MailSyncError("down")
        return self.subs

    def lists(self):
        return [{"id": LIST}]

    def set_membership(self, ids, list_id, action):
        if self.fail:
            raise MM.MailSyncError("down")
        self.calls.append((action, list(ids)))
        for s in self.subs:
            if s["id"] in ids:
                for l in s["lists"]:
                    if l["id"] == list_id:
                        l["subscription_status"] = "confirmed" if action == "add" else "unsubscribed"

    def recipient_count(self, list_id):
        return sum(1 for s in self.subs for l in s["lists"]
                   if l["id"] == list_id and l["subscription_status"] == "confirmed"
                   and s["status"] == "enabled")


def run(fake, elig, tmp_path, **kw):
    return MM.reconcile(fake, LIST, elig, tmp_path, now=NOW, **kw)


def elig_of(*emails, hold=()):
    return MM.Eligibility(eligible=set(emails), hold=set(hold))


def test_active_stays_deactivated_removed(tmp_path):
    f = Fake([sub(1, "a@x"), sub(2, "b@x")])
    r = run(f, elig_of("a@x"), tmp_path)
    assert r.ok and r.unsubscribed == 1 and r.recipients == 1
    assert [s["lists"][0]["subscription_status"] for s in f.subs] == ["confirmed", "unsubscribed"]
    assert len(f.subs) == 2                       # never deleted


def test_idempotent(tmp_path):
    f = Fake([sub(1, "a@x"), sub(2, "b@x")])
    run(f, elig_of("a@x"), tmp_path)
    f.calls.clear()
    r = run(f, elig_of("a@x"), tmp_path)
    assert r.ok and r.unsubscribed == 0 and f.calls == []


def test_re_entitled_is_re_added_but_self_unsubscribed_is_not(tmp_path):
    f = Fake([sub(1, "a@x"), sub(2, "b@x", status="unsubscribed")])
    run(f, elig_of("a@x"), tmp_path)                       # a stays; b was never ours
    run(f, elig_of("b@x"), tmp_path)                       # a leaves, b is eligible but self-unsubscribed
    states = {s["id"]: s["lists"][0]["subscription_status"] for s in f.subs}
    assert states == {1: "unsubscribed", 2: "unsubscribed"}
    r = run(f, elig_of("a@x", "b@x"), tmp_path)            # a re-entitled
    states = {s["id"]: s["lists"][0]["subscription_status"] for s in f.subs}
    assert r.added == 1 and states == {1: "confirmed", 2: "unsubscribed"}


def test_duplicate_records_collapse_to_one(tmp_path):
    f = Fake([sub(1, "A@x"), sub(2, "a@x ")])
    r = run(f, elig_of("a@x"), tmp_path)
    assert r.ok and r.recipients == 1
    assert {s["id"]: s["lists"][0]["subscription_status"] for s in f.subs} == {1: "confirmed", 2: "unsubscribed"}


def test_error_leaves_state_unchanged(tmp_path):
    f = Fake([sub(1, "a@x"), sub(2, "b@x")], fail=True)
    r = run(f, elig_of("a@x"), tmp_path)
    assert not r.ok and f.calls == []
    assert not (tmp_path / MM.STATE_FILE).exists()
    assert not list(tmp_path.glob(MM.BACKUP_PREFIX + "*"))


def test_empty_eligible_refuses(tmp_path):
    f = Fake([sub(1, "a@x")])
    r = run(f, elig_of(), tmp_path)
    assert not r.ok and f.calls == []


def test_cap_refuses_mass_removal(tmp_path):
    f = Fake([sub(i, "u%d@x" % i) for i in range(1, 6)])
    r = run(f, elig_of("u1@x"), tmp_path, max_removals=2)
    assert not r.ok and f.calls == []


def test_no_answer_holds_and_blocklisted_untouched(tmp_path):
    f = Fake([sub(1, "a@x"), sub(2, "b@x"), sub(3, "c@x", enabled="blocklisted")])
    r = run(f, elig_of("a@x", hold=("b@x",)), tmp_path)
    assert r.ok and r.unsubscribed == 0 and f.calls == []


def test_backup_written_before_change_and_other_lists_kept(tmp_path):
    f = Fake([sub(1, "a@x"), sub(2, "b@x", extra_lists=(19,))])
    run(f, elig_of("a@x"), tmp_path)
    b = list(tmp_path.glob(MM.BACKUP_PREFIX + "*"))
    assert len(b) == 1 and len(json.loads(b[0].read_text())["subscribers"]) == 2
    assert f.subs[1]["lists"][1] == {"id": 19, "subscription_status": "confirmed"}


def test_dry_run_changes_nothing(tmp_path):
    f = Fake([sub(1, "a@x"), sub(2, "b@x")])
    r = run(f, elig_of("a@x"), tmp_path, execute=False)
    assert r.ok and f.calls == []


# ---- eligibility from the gate's plans -------------------------------------

def test_eligibility_states_and_tagalong():
    plans = [plan("p@x", "entitled"), plan("e@x", "exempt"), plan("t@x", "entitled"),
             plan("u@x", "unknown-payer"), plan("x@x", "expired"), plan("n@x", "no-answer")]
    el = MM.eligibility_from_plans(plans, lambda a: a == "t@x")
    assert el.eligible == {"p@x", "e@x"} and el.hold == {"n@x"}


HH = {"a@x": "hh-a", "b@x": "hh-b", "o@x": "operator"}


def elig_allow(plans, allow):
    return MM.eligibility_from_plans(plans, lambda a: False, HH.get, allow)


def test_allowlist_matches_gate_no_mismatch():
    el = elig_allow([plan("a@x", "entitled"), plan("o@x", "exempt")], {"hh-a", "operator"})
    assert el.eligible == {"a@x", "o@x"} and not el.remove_only


def test_allowlist_blocks_active_unlisted_and_goes_remove_only(tmp_path):
    el = elig_allow([plan("a@x", "entitled"), plan("b@x", "exempt")], {"hh-a"})
    assert el.eligible == {"a@x"} and el.unlisted_active == 1 and el.remove_only
    # remove-only: a previously removed listed member is NOT re-added, extras still go
    (tmp_path / MM.STATE_FILE).write_text(json.dumps({"removed_ids": [1]}))
    f = Fake([sub(1, "a@x", status="unsubscribed"), sub(2, "b@x")])
    r = run(f, el, tmp_path)
    assert r.ok and r.added == 0 and r.unsubscribed == 1


def test_allowlist_listed_but_inactive_keeps_subscription():
    el = elig_allow([plan("a@x", "entitled"), plan("b@x", "expired")], {"hh-a", "hh-b"})
    assert "b@x" in el.hold and el.listed_inactive == 1 and el.remove_only


def test_allowlist_named_without_a_share_is_reported():
    el = elig_allow([plan("a@x", "entitled")], {"hh-a", "hh-b"})
    assert el.listed_inactive == 1 and el.remove_only


# ---- files -------------------------------------------------------------------

def test_eligible_snapshot_roundtrip_and_stale(tmp_path):
    MM.write_eligible(tmp_path, elig_of("A@x"), NOW)
    assert MM.read_eligible(tmp_path, NOW).eligible == {"a@x"}
    with pytest.raises(MM.MailSyncError):
        MM.read_eligible(tmp_path, NOW + dt.timedelta(hours=49))
    with pytest.raises(MM.MailSyncError):
        MM.read_eligible(tmp_path / "nope", NOW)


def test_allowlist_file(tmp_path):
    assert MM.read_allowlist(tmp_path) is None
    (tmp_path / "mail-allowlist.txt").write_text("# five\nhh-a\n\noperator  # me\n")
    assert MM.read_allowlist(tmp_path) == {"hh-a", "operator"}
    (tmp_path / "mail-allowlist.txt").write_text("# nothing\n")
    with pytest.raises(MM.MailSyncError):
        MM.read_allowlist(tmp_path)
