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
import urllib.parse

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


# --- QFLX-119: the REAL response shape -----------------------------------
#
# Server contract (QuadstroNot patreon_entitlement/app.py `bulk`): one page of
# {count: <rows on this page>, limit, offset, members: [{email, entitled,
# stale, status, ...}]}, ordered by patreon_user_id, limit clamped to
# [1, 1000]. Every address below is synthetic (example.com).


def _member(email, entitled, *, stale=False, status=None):
    return {
        "entitled": entitled,
        "patreon_user_id": "synthetic",
        "email": email,
        "status": status or ("active_patron" if entitled else "former_patron"),
        "tiers": [], "tier_ids": [], "amount_cents": 500 if entitled else 0,
        "next_charge_date": None, "rewards": [],
        "synced_at": "2026-11-11T00:00:00+00:00", "stale": stale,
    }


def _page(members, *, offset=0, limit=1000):
    return json.dumps({"count": len(members), "limit": limit,
                       "offset": offset, "members": members}).encode()


def _paged_opener(all_members, page_size, seen_urls=None):
    """A fake server that honours ?offset= exactly like the real one, with a
    server-side limit of `page_size` regardless of what the client asks."""
    def opener(req, timeout=None):
        if seen_urls is not None:
            seen_urls.append(req.full_url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        off = int(q.get("offset", ["0"])[0])
        return _FakeResp(_page(all_members[off:off + page_size],
                               offset=off, limit=page_size))
    return opener


def test_bulk_mixed_entitled_and_not_counts_only_entitled():
    """THE bug: count must be the ENTITLED addresses, not the page length."""
    body = _page([
        _member("Paying.One@Example.com ", True),
        _member("lapsed@example.com", False),
        _member("declined@example.com", False, status="declined_patron"),
        _member("paying.two@example.com", True),
    ])
    r = _client(_opener_returning(body)).bulk()
    assert r.state == ENT.BULK_OK
    assert r.supported
    assert r.count == 2
    assert r.entitled == ["paying.one@example.com", "paying.two@example.com"]


def test_bulk_projection_with_nobody_entitled_is_ok_zero_not_ok_n():
    """Pre-fix this read as count=3 and greened oracle row 5."""
    body = _page([_member("a@example.com", False), _member("b@example.com", False),
                  _member("c@example.com", False)])
    r = _client(_opener_returning(body)).bulk()
    assert r.state == ENT.BULK_OK
    assert r.count == 0
    assert r.entitled == []


def test_bulk_empty_projection_is_ok_zero():
    r = _client(_opener_returning(_page([]))).bulk()
    assert r.state == ENT.BULK_OK
    assert r.count == 0


def test_bulk_stale_yes_still_counts():
    """Same asymmetry as lookup(): a stale yes was a real yes."""
    body = _page([_member("stale-yes@example.com", True, stale=True),
                  _member("stale-no@example.com", False, stale=True)])
    r = _client(_opener_returning(body)).bulk()
    assert r.state == ENT.BULK_OK
    assert r.entitled == ["stale-yes@example.com"]


def test_bulk_entitled_row_with_null_email_is_skipped_not_fatal():
    body = _page([_member(None, True), _member("a@example.com", True)])
    r = _client(_opener_returning(body)).bulk()
    assert r.state == ENT.BULK_OK
    assert r.entitled == ["a@example.com"]
    assert r.count == 1


def test_bulk_first_request_asks_for_max_page_from_offset_zero():
    urls = []
    _client(_paged_opener([], 1000, urls)).bulk()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(urls[0]).query)
    assert q == {"limit": ["1000"], "offset": ["0"]}
    assert len(urls) == 1


def test_bulk_follows_pagination_and_merges_pages():
    members = [
        _member("p1@example.com", True), _member("n1@example.com", False),
        _member("p2@example.com", True), _member("n2@example.com", False),
        _member("p3@example.com", True),
    ]
    urls = []
    r = _client(_paged_opener(members, 2, urls)).bulk()
    assert r.state == ENT.BULK_OK
    assert r.entitled == ["p1@example.com", "p2@example.com", "p3@example.com"]
    assert r.count == 3
    offsets = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["offset"][0]
               for u in urls]
    assert offsets == ["0", "2", "4"]


def test_bulk_exactly_full_last_page_fetches_one_empty_page_then_stops():
    members = [_member("p%d@example.com" % i, True) for i in range(4)]
    urls = []
    r = _client(_paged_opener(members, 2, urls)).bulk()
    assert r.state == ENT.BULK_OK
    assert r.count == 4
    assert len(urls) == 3          # 2 + 2 + 0


def test_bulk_dedups_an_address_seen_on_two_pages():
    """A row inserted mid-walk shifts one row onto the next page too."""
    members = [_member("a@example.com", True), _member("b@example.com", True),
               _member("b@example.com", True)]
    r = _client(_paged_opener(members, 2)).bulk()
    assert r.count == 2
    assert r.entitled == ["a@example.com", "b@example.com"]


def test_bulk_failure_on_a_later_page_fails_the_whole_call():
    """Never a partial 'ok': half a projection is not the projection."""
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            return _FakeResp(_page([_member("a@example.com", True)] * 2, limit=2))
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, io.BytesIO(b""))
    r = _client(opener).bulk()
    assert r.state == ENT.BULK_UNREACHABLE
    assert r.count is None
    assert r.entitled == []


def test_bulk_malformed_later_page_is_unparseable():
    calls = []

    def opener(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return _FakeResp(_page([_member("a@example.com", True)] * 2, limit=2))
        return _FakeResp(json.dumps({"entitled": []}).encode())
    r = _client(opener).bulk()
    assert r.state == ENT.BULK_UNPARSEABLE
    assert r.count is None
    assert r.entitled == []


def test_bulk_server_ignoring_offset_is_unparseable_not_an_infinite_loop():
    """Always echoes offset 0 with a full page."""
    full = _page([_member("a@example.com", True)] * 2, offset=0, limit=2)
    r = _client(_opener_returning(full)).bulk()
    assert r.state == ENT.BULK_UNPARSEABLE
    assert "offset" in r.error


def test_bulk_server_that_never_ends_is_capped():
    """Echoes the right offset but every page is full: stop at the cap."""
    def opener(req, timeout=None):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        off = int(q["offset"][0])
        return _FakeResp(_page([_member("a@example.com", True)], offset=off, limit=1))
    r = _client(opener).bulk()
    assert r.state == ENT.BULK_UNPARSEABLE
    assert r.count is None


@pytest.mark.parametrize("body", [
    {"count": 1, "limit": 1000, "offset": 0, "members": "nope"},
    {"count": 1, "limit": 1000, "offset": 0, "members": [{"email": "a@example.com"}]},
    {"count": 1, "limit": 1000, "offset": 0,
     "members": [{"email": "a@example.com", "entitled": "true"}]},
    {"count": 1, "limit": 1000, "offset": 0,
     "members": [{"email": "a@example.com", "entitled": None}]},
    {"count": 1, "limit": 1000, "offset": 0, "members": ["a@example.com"]},
    {"count": 1, "limit": 1000, "offset": 0,
     "members": [{"email": 7, "entitled": True}]},
    {"count": 5, "limit": 1000, "offset": 0,
     "members": [{"email": "a@example.com", "entitled": True}]},
    {"count": 1, "offset": 0, "members": [{"email": "a@example.com", "entitled": True}]},
    {"count": 1, "limit": 0, "offset": 0, "members": [{"email": "a@example.com", "entitled": True}]},
    {"count": 1, "limit": True, "offset": 0, "members": [{"email": "a@example.com", "entitled": True}]},
    {"count": 1, "limit": 1000, "members": [{"email": "a@example.com", "entitled": True}]},
    {"count": 2, "limit": 1, "offset": 0,
     "members": [{"email": "a@example.com", "entitled": True}] * 2},
], ids=[
    "members-not-list", "row-missing-entitled", "entitled-is-string",
    "entitled-is-null", "row-not-object", "email-not-string",
    "count-disagrees", "limit-missing", "limit-zero", "limit-bool",
    "offset-missing", "rows-exceed-limit",
])
def test_bulk_malformed_real_shape_is_unparseable_never_empty_ok(body):
    r = _client(_opener_returning(json.dumps(body).encode())).bulk()
    assert r.state == ENT.BULK_UNPARSEABLE
    assert not r.supported
    assert r.count is None
    assert r.entitled == []
    assert "@" not in (r.error or ""), "error text must never carry an address"


@pytest.mark.parametrize("body", [
    {"entitled": ["a@example.com", "b@example.com"]},
    {"count": 5, "entitled": ["a@example.com"]},
    {"count": 0},
    {"count": 3},
], ids=["entitled-list", "count-plus-list", "count-zero-only", "count-only"])
def test_bulk_old_assumed_shape_is_unparseable(body):
    """The pre-QFLX-119 client's assumed contract. The server never sends it;
    if something does, it carries no per-member verdicts and must not be
    graded 'ok' -- least of all as an empty 'ok', which judge() reads as
    UNPROVEN_EMPTY rather than as a broken cross-check."""
    r = _client(_opener_returning(json.dumps(body).encode())).bulk()
    assert r.state == ENT.BULK_UNPARSEABLE
    assert r.count is None
    assert r.entitled == []


# --- QFLX-119: what the oracle now sees from a real-shape answer ----------


def _oracle_on(body, *holders):
    import datetime as dt
    import payer_oracle as ORACLE
    now = dt.datetime(2026, 11, 11, 12, 0, tzinfo=dt.timezone.utc)
    payers = [ORACLE.DeclaredPayer(household_id="h%d" % i, holder=h,
                                   first_declared_at=now - dt.timedelta(days=30))
              for i, h in enumerate(holders)]
    ans = _client(_opener_returning(body)).bulk()
    return ORACLE.judge(declared=payers, bulk=ORACLE.BulkFacts.from_bulk_answer(ans),
                        now=now)


def test_oracle_row4_mismatch_can_now_fire_on_the_real_shape():
    body = _page([_member("paying-elsewhere@example.com", True),
                  _member("roster@example.com", False)])
    v = _oracle_on(body, "roster@example.com")
    assert v.verdict == "MISMATCH"
    assert v.is_red


def test_oracle_row5_needs_an_entitled_match_not_just_rows():
    """Pre-fix: any non-empty page greened row 5. Now a projection where the
    declared holder exists but is NOT entitled is row 8, not row 5."""
    body = _page([_member("roster@example.com", False)])
    v = _oracle_on(body, "roster@example.com")
    assert v.verdict == "UNPROVEN_EMPTY"
    assert v.is_red


def test_oracle_row5_still_greens_on_a_real_entitled_match():
    body = _page([_member("roster@example.com", True),
                  _member("someone-else@example.com", False)])
    v = _oracle_on(body, "roster@example.com")
    assert v.verdict == "PROVEN_UPSTREAM"
    assert not v.is_red


def test_oracle_on_unknown_shape_is_blind_not_empty():
    """Unknown shape -> row 7 (cannot cross-check), never row 8 (checked,
    found nobody)."""
    v = _oracle_on(json.dumps({"count": 0}).encode(), "roster@example.com")
    assert v.verdict == "UNPROVEN_BLIND"


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
