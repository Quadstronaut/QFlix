"""The asymmetric failure law, proved by fault injection.

This file exists because of one class of incident: a monitoring or gating
system that cannot tell "the answer is no" from "I did not get an answer", and
therefore treats an outage in its own dependency as a fact about its users. In
a system that only grants access, collapsing those two is safe and is exactly
what the integration guide prescribes -- fail closed. In a system that also
REVOKES, the same collapse turns a DNS blip into a mass eviction.

So lib/entitlement.py returns three values, not two, and the two predicates
that decisions branch on are deliberately not complements:

    grants   True only on a clean HTTP 200 + entitled:true
    revokes  True only on a clean HTTP 200 + entitled:false
    both False for every failure, malformation, and stale no

The tests below are the enforcement. THE CENTRAL ONE is
test_no_failure_mode_can_ever_revoke: it enumerates every way the network or
the remote service can misbehave and asserts that not one of them produces
`revokes`. If someone later "simplifies" the client to return a bool, that test
is what fails, and its failure message says why the shape matters.

A guard nobody tested is a guard nobody has -- so the guard here is tested
against the faults, not against a mock that only ever succeeds.

NOTHING IN THIS FILE MAY NAME A REAL MEMBER. Every address is example.com.
"""
import io
import json
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "maint" / "lib"))

import entitlement as ent  # noqa: E402


KEY = "test-key-not-a-real-credential"
WHO = "person@example.com"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _Resp:
    """Minimal urlopen-context-manager stand-in."""

    def __init__(self, body, status=200):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener_returning(body, status=200):
    def _open(req, timeout=None):
        return _Resp(body, status)
    return _open


def _opener_raising(exc):
    def _open(req, timeout=None):
        raise exc
    return _open


def _client(opener):
    return ent.EntitlementClient(api_key=KEY, base_url="https://example.invalid",
                                 opener=opener)


def _body(**over):
    d = {
        "entitled": True,
        "email": WHO,
        "status": "active_patron",
        "tiers": ["Q Support: Gold"],
        "amount_cents": 500,
        "synced_at": "2026-08-06T12:00:00+00:00",
        "stale": False,
    }
    d.update(over)
    return json.dumps(d)


# ---------------------------------------------------------------------------
# THE CENTRAL INVARIANT
# ---------------------------------------------------------------------------

# Every way this call can fail to produce a trustworthy answer. Each entry is
# (label, opener). The label is what shows up in the assertion message, so it
# has to name the real-world event, not the Python exception.
FAULTS = [
    ("entitlement box is down (connection refused)",
     _opener_raising(urllib.error.URLError(ConnectionRefusedError(111, "refused")))),
    ("DNS failure",
     _opener_raising(urllib.error.URLError(socket.gaierror(-2, "Name or service not known")))),
    ("socket timeout",
     _opener_raising(socket.timeout("timed out"))),
    ("TLS handshake failure",
     _opener_raising(OSError("certificate verify failed"))),
    ("API key revoked (401)",
     _opener_raising(urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"")))),
    ("key lacks scope (403)",
     _opener_raising(urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"")))),
    ("rate limited (429)",
     _opener_raising(urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b"")))),
    ("upstream 500",
     _opener_raising(urllib.error.HTTPError("u", 500, "Server Error", {}, io.BytesIO(b"")))),
    ("bad gateway from Caddy (502)",
     _opener_raising(urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(b"")))),
    ("non-200 without an exception",
     _opener_returning(_body(), status=503)),
    ("HTML error page instead of JSON",
     _opener_returning("<html><body>502 Bad Gateway</body></html>")),
    ("truncated JSON body",
     _opener_returning('{"entitled": tr')),
    ("empty body",
     _opener_returning("")),
    ("JSON array instead of object",
     _opener_returning("[]")),
    ("200 with no 'entitled' field (contract drift)",
     _opener_returning(json.dumps({"email": WHO, "status": "former_patron"}))),
    ("'entitled' is a string, not a bool",
     _opener_returning(json.dumps({"entitled": "false", "email": WHO}))),
    ("'entitled' is null",
     _opener_returning(json.dumps({"entitled": None, "email": WHO}))),
    ("stale projection reporting not-entitled",
     _opener_returning(_body(entitled=False, stale=True, status="former_patron"))),
]


@pytest.mark.parametrize("label,opener", FAULTS, ids=[f[0] for f in FAULTS])
def test_no_failure_mode_can_ever_revoke(label, opener):
    """No fault may authorise a reduction in access.

    This is the whole safety property. If this test fails, some failure mode has
    become indistinguishable from the service saying "this person is not a
    supporter", and an outage in a dependency will start taking people's access
    away.
    """
    answer = _client(opener).lookup(WHO)
    assert answer.revokes is False, (
        "%s produced revokes=True. A failure to reach the entitlement service "
        "must never be readable as 'this person is not entitled' -- that is "
        "how an outage becomes a mass revocation." % label)
    assert answer.verdict == ent.UNKNOWN, (
        "%s should grade UNKNOWN, got %r" % (label, answer.verdict))


@pytest.mark.parametrize("label,opener", FAULTS, ids=[f[0] for f in FAULTS])
def test_no_failure_mode_can_ever_grant(label, opener):
    """The guide's own law: never grant on an error. Same enumeration.

    Weaker stakes than revocation (the worst case is somebody keeps access they
    should not) but it is the documented contract and it is free to hold.
    """
    answer = _client(opener).lookup(WHO)
    assert answer.grants is False, (
        "%s produced grants=True -- access must never be granted on a "
        "response the service did not actually give." % label)


def test_grants_and_revokes_are_not_complements():
    """The shape itself. `revokes` must not be `not grants`.

    Written as its own test because this is the property a future refactor is
    most likely to destroy while all the other tests keep passing: collapsing
    the three-valued answer back into a bool makes every one of the fault tests
    above pass in the granting direction and fail catastrophically in
    production in the revoking direction.
    """
    unknown = _client(_opener_raising(socket.timeout("x"))).lookup(WHO)
    assert unknown.grants is False
    assert unknown.revokes is False
    assert not unknown.answered, (
        "UNKNOWN must not report itself as answered -- callers use `answered` "
        "to decide whether a clock may advance at all.")


# ---------------------------------------------------------------------------
# The happy paths, so the safety rails are not merely a very safe no-op
# ---------------------------------------------------------------------------

def test_clean_yes_grants():
    a = _client(_opener_returning(_body())).lookup(WHO)
    assert a.verdict == ent.YES
    assert a.grants is True and a.revokes is False
    assert a.status == "active_patron"
    assert a.tiers == ["Q Support: Gold"]


def test_clean_no_revokes():
    a = _client(_opener_returning(
        _body(entitled=False, status="former_patron", tiers=[]))).lookup(WHO)
    assert a.verdict == ent.NO
    assert a.revokes is True and a.grants is False


def test_stale_yes_still_grants():
    """A stale yes was a real yes, and acting on it only ever adds access.

    Grading it UNKNOWN would mean a member who renewed during a sync hiccup
    sits locked out until the projection catches up -- a real harm, traded
    against no risk, since the failure mode of a wrong yes is that somebody
    keeps access slightly too long.
    """
    a = _client(_opener_returning(_body(stale=True))).lookup(WHO)
    assert a.verdict == ent.YES
    assert a.grants is True
    assert a.stale is True


def test_stale_no_is_not_an_answer():
    """The mirror of the test above, and the reason `stale` is graded at all.

    A stale no may simply predate the person's renewal. Revoking on it cuts off
    somebody who has already paid.
    """
    a = _client(_opener_returning(_body(entitled=False, stale=True))).lookup(WHO)
    assert a.verdict == ent.UNKNOWN
    assert a.revokes is False
    assert "stale" in (a.error or "").lower()


def test_unknown_address_is_a_definitive_no_and_is_flagged():
    """`reason: "unknown"` is an ANSWER, not an error -- but a distinct one.

    The guide is explicit that an unrecognised address returns 200 with a
    definitive false rather than a 404. Grading it UNKNOWN would mean nobody
    could ever be revoked for simply never having subscribed.

    It is still separated out, because the overwhelmingly common cause in this
    system is a typo in the roster's billing.holder -- and "we have never seen
    this address" needs a human to fix an address, whereas "they stopped
    paying" needs nobody to do anything.
    """
    a = _client(_opener_returning(json.dumps(
        {"entitled": False, "reason": "unknown"}))).lookup(WHO)
    assert a.verdict == ent.NO
    assert a.revokes is True
    assert a.never_seen is True
    assert "never seen" in a.describe()


def test_lapsed_patron_is_not_flagged_as_never_seen():
    a = _client(_opener_returning(
        _body(entitled=False, status="declined_patron"))).lookup(WHO)
    assert a.never_seen is False


# ---------------------------------------------------------------------------
# Programmer errors stay loud
# ---------------------------------------------------------------------------

def test_empty_key_is_fatal_at_construction():
    """An empty key must not become a day of silent UNKNOWNs.

    With a blank key every lookup 401s, every household grades UNKNOWN, and the
    system correctly does nothing -- forever, quietly, while looking healthy.
    That is a worse failure than a crash, so it is a crash.
    """
    with pytest.raises(ValueError):
        ent.EntitlementClient(api_key="")
    with pytest.raises(ValueError):
        ent.EntitlementClient(api_key="   ")


def test_bad_email_is_a_programmer_error_not_an_unknown():
    c = _client(_opener_returning(_body()))
    for bad in ("", "not-an-email", None):
        with pytest.raises(ValueError):
            c.lookup(bad)


def test_key_is_sent_as_a_bearer_token_and_never_in_the_query():
    """Guards against the key leaking into request logs on the Starhold box."""
    seen = {}

    def _open(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        seen["url"] = req.full_url
        return _Resp(_body())

    _client(_open).lookup(WHO)
    assert seen["auth"] == "Bearer " + KEY
    assert KEY not in seen["url"]


def test_healthz_never_raises():
    assert _client(_opener_raising(socket.timeout("x"))).healthz() is False
    assert _client(_opener_returning('{"status":"ok"}')).healthz() is True
