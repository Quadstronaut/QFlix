"""lib/entitlement.py -- EntitlementClient.bulk() (AC-01) and the grants/revokes
asymmetry (AC-08).

The opener is injected (matches urllib.request.urlopen's call signature), so
these tests never touch the network -- same convention the module's own
docstring promises callers: bulk() NEVER raises for anything the network or
the remote service can do.
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(REPO_ROOT, "scripts", "maint", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import entitlement as ENT  # noqa: E402


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener_returning(body: bytes, status: int = 200):
    def opener(req, timeout=None):
        return _FakeResp(body, status)
    return opener


def _opener_raising_http_error(code: int, body: bytes):
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(body))
    return opener


def _opener_raising_url_error(reason="connection refused"):
    def opener(req, timeout=None):
        raise urllib.error.URLError(reason)
    return opener


def _opener_raising_timeout():
    def opener(req, timeout=None):
        raise TimeoutError("timed out")
    return opener


def _client(opener):
    return ENT.EntitlementClient(api_key="k", opener=opener)


# --- AC-01: bulk() grading ---------------------------------------------


def test_bulk_403_with_scope_in_body_is_no_scope():
    """The LIVE-OBSERVED body, verbatim."""
    body = json.dumps({"error": "this key lacks the 'bulk' scope"}).encode()
    r = _client(_opener_raising_http_error(403, body)).bulk()
    assert r.state == ENT.BULK_NO_SCOPE
    assert r.count is None
    assert not r.supported


def test_bulk_403_without_scope_wording_is_unreachable():
    body = json.dumps({"error": "forbidden"}).encode()
    r = _client(_opener_raising_http_error(403, body)).bulk()
    assert r.state == ENT.BULK_UNREACHABLE
    assert r.count is None


def test_bulk_401_is_unreachable():
    r = _client(_opener_raising_http_error(401, b"{}")).bulk()
    assert r.state == ENT.BULK_UNREACHABLE
    assert r.count is None


def test_bulk_500_is_unreachable():
    r = _client(_opener_raising_http_error(500, b"boom")).bulk()
    assert r.state == ENT.BULK_UNREACHABLE
    assert r.count is None


def test_bulk_network_error_is_unreachable():
    r = _client(_opener_raising_url_error()).bulk()
    assert r.state == ENT.BULK_UNREACHABLE
    assert r.count is None


def test_bulk_timeout_is_unreachable():
    r = _client(_opener_raising_timeout()).bulk()
    assert r.state == ENT.BULK_UNREACHABLE
    assert r.count is None


def test_bulk_200_non_json_is_unparseable():
    r = _client(_opener_returning(b"not json")).bulk()
    assert r.state == ENT.BULK_UNPARSEABLE
    assert r.count is None


def test_bulk_200_json_array_not_object_is_unparseable():
    r = _client(_opener_returning(b"[1, 2, 3]")).bulk()
    assert r.state == ENT.BULK_UNPARSEABLE
    assert r.count is None


def test_bulk_200_object_with_neither_entitled_nor_count_is_unparseable():
    r = _client(_opener_returning(json.dumps({"foo": "bar"}).encode())).bulk()
    assert r.state == ENT.BULK_UNPARSEABLE
    assert r.count is None


def test_bulk_200_with_entitled_list_is_ok_with_count_set():
    body = json.dumps({"entitled": ["a@example.com", "b@example.com"]}).encode()
    r = _client(_opener_returning(body)).bulk()
    assert r.state == ENT.BULK_OK
    assert r.supported
    assert r.count == 2
    assert r.entitled == ["a@example.com", "b@example.com"]


def test_bulk_200_with_count_only_is_ok():
    body = json.dumps({"count": 0}).encode()
    r = _client(_opener_returning(body)).bulk()
    assert r.state == ENT.BULK_OK
    assert r.count == 0
    assert r.entitled == []


def test_bulk_200_explicit_count_overrides_list_length_if_valid():
    body = json.dumps({"count": 5, "entitled": ["a@example.com"]}).encode()
    r = _client(_opener_returning(body)).bulk()
    assert r.state == ENT.BULK_OK
    assert r.count == 5


def test_bulk_never_raises_on_garbage_status():
    """Defensive: even a non-raising opener returning a weird status must not
    raise out of bulk()."""
    r = _client(_opener_returning(b"{}", status=599)).bulk()
    assert r.state == ENT.BULK_UNREACHABLE
    assert r.count is None


@pytest.mark.parametrize("state", [
    ENT.BULK_NO_SCOPE, ENT.BULK_UNREACHABLE, ENT.BULK_UNPARSEABLE,
])
def test_count_is_none_in_every_non_ok_state(state):
    """AC-01, restated as its own explicit assertion across all three."""
    assert ENT.BulkAnswer(state=state).count is None


# --- AC-08: grants/revokes are not complements ------------------------


def test_yes_grants_and_does_not_revoke():
    body = json.dumps({"entitled": True}).encode()
    a = _client(_opener_returning(body)).lookup("t@example.com")
    assert a.verdict == ENT.YES
    assert a.grants is True
    assert a.revokes is False


def test_no_revokes_and_does_not_grant():
    body = json.dumps({"entitled": False, "status": "former_patron"}).encode()
    a = _client(_opener_returning(body)).lookup("t@example.com")
    assert a.verdict == ENT.NO
    assert a.grants is False
    assert a.revokes is True


def test_stale_false_is_unknown_not_no():
    """THE case AC-08 calls out by name: stale + entitled:false must not
    collapse to a revoke."""
    body = json.dumps({"entitled": False, "stale": True}).encode()
    a = _client(_opener_returning(body)).lookup("t@example.com")
    assert a.verdict == ENT.UNKNOWN
    assert a.grants is False
    assert a.revokes is False


def test_http_error_is_unknown_and_neither_grants_nor_revokes():
    a = _client(_opener_raising_http_error(500, b"boom")).lookup("t@example.com")
    assert a.verdict == ENT.UNKNOWN
    assert a.grants is False
    assert a.revokes is False


def test_grants_and_revokes_are_not_complements_across_all_three_verdicts():
    """THE asymmetry, proven directly: for UNKNOWN, `revokes != (not grants)`.
    A boolean collapse would make these complements; the three-valued design
    exists precisely so they are not."""
    cases = {
        ENT.YES: json.dumps({"entitled": True}).encode(),
        ENT.NO: json.dumps({"entitled": False, "status": "former_patron"}).encode(),
    }
    answers = {v: _client(_opener_returning(b)).lookup("t@example.com")
              for v, b in cases.items()}
    # UNKNOWN via an outage rather than a lookup body.
    answers[ENT.UNKNOWN] = _client(_opener_raising_url_error()).lookup("t@example.com")

    for verdict, a in answers.items():
        if verdict == ENT.UNKNOWN:
            # THE asymmetry: both are False simultaneously. A complement would
            # require revokes == (not grants), i.e. exactly one True -- here
            # BOTH are False, so revokes is provably NOT the complement of
            # grants for this verdict.
            assert a.grants is False
            assert a.revokes is False
            assert a.revokes != (not a.grants), (
                "UNKNOWN must not behave as the complement of grants -- "
                "collapsing it that way is the fail-closed bug this system "
                "exists to avoid on the revoking side")
        else:
            assert a.grants != a.revokes, "%s must be exactly one of grants/revokes" % verdict
