"""lib/entitlement.py -- ask entitlements.quadstronix.dev about one email.

Pure stdlib. No Plex, no Seerr, no roster, no mutation. This module answers
exactly one question -- "what does the entitlement service currently say about
this address?" -- and it is deliberately incapable of doing anything with the
answer.

THE THREE-VALUED ANSWER, and why there is no boolean
----------------------------------------------------
The obvious API here is `is_entitled(email) -> bool`, and it is wrong. A bool
has two values but this problem has three:

    YES      the service answered, and the person is entitled
    NO       the service answered, and the person is not entitled
    UNKNOWN  the service did not answer

A bool forces UNKNOWN to collapse into one of the other two at the moment of
return, before the caller knows which direction it is about to move. The
integration guide says to collapse it into "not entitled" -- fail closed -- and
for a system that only ever GRANTS access, that is correct and safe.

This system also REVOKES. Collapsing UNKNOWN into NO in a revoking system means
a DNS blip, an expired TLS certificate, a 429 from a chatty neighbour, or the
Starhold box rebooting is indistinguishable from "every member cancelled at the
same instant". Fail-closed, applied to revocation, is a mass-eviction button
wired to the least reliable component in the stack.

So the third value is preserved all the way to the decision, and the caller is
handed two separate, non-negotiable predicates instead of one bool:

    answer.grants   -> True only for YES.  Errors never grant.
    answer.revokes  -> True only for NO.   Errors never revoke.

Both are False for UNKNOWN. That is the point: an outage makes the system do
NOTHING, in either direction, which is the only safe behaviour for a component
that can take away the thing people watch in the evening. An entitlement API
outage freezes this system; it does not drain it.

This is the same law patreon-report.py already encodes for its own auth
failures -- "empty-because-clean must never look like empty-because-broken" --
but the stakes are higher here, because the wrong answer does not merely print
a misleading report. It removes access.

`stale`
-------
The service sets `stale: true` when its local projection is older than ~2 sync
cycles. It is graded asymmetrically for the same reason:

    stale + entitled:true   -> YES.  A stale yes was a real yes, and acting on
                                     it only grants. The cost of being wrong is
                                     that somebody keeps access slightly too
                                     long, which is not an incident.
    stale + entitled:false  -> UNKNOWN. A stale no may simply predate the
                                     person's renewal. Acting on it revokes a
                                     paying member.

WHY `reason: "unknown"` IS REPORTED SEPARATELY
----------------------------------------------
The service returns HTTP 200 with `{"entitled": false, "reason": "unknown"}`
for an address it has never seen. That IS a definitive no, and it is graded NO,
not UNKNOWN -- the guide is explicit that this is an answer rather than an
error, and treating it as UNKNOWN would mean nobody could ever be revoked for
simply never having subscribed.

But it is a different FACT than `status: former_patron`, and it demands a
different human response. "They stopped paying" needs no action. "We have never
seen this address" is very often a typo in the roster's `billing.holder`, and
the fix is to correct the address, not to let the clock run out on a paying
member whose email we wrote down wrong. So `Answer.never_seen` is exposed and
the reporter separates the two populations. The clocks give weeks of warning;
this is what makes those weeks actionable instead of merely long.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import List, Optional
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://entitlements.quadstronix.dev"
DEFAULT_TIMEOUT = 15
UA = "qflix-entitlement-gate/1.0"

# Verdicts. Strings rather than an Enum so they survive a JSON round-trip into
# the audit manifest unchanged and read correctly in a log line.
YES = "yes"
NO = "no"
UNKNOWN = "unknown"

# Bulk states. A fourth, distinct vocabulary from the per-lookup verdicts above
# on purpose: /v1/entitlements answers a DIFFERENT question ("what does the
# whole projection contain") and fails in a mode the single-lookup endpoint
# never can -- "this key exists and works but is not SCOPED for this call".
# Collapsing that into UNKNOWN would erase the one distinction payer_oracle.py
# needs to tell the operator the exact one-line Starhold-side fix instead of a
# generic outage message.
BULK_OK = "ok"
BULK_NO_SCOPE = "no-scope"
BULK_UNREACHABLE = "unreachable"
BULK_UNPARSEABLE = "unparseable"

# /v1/entitlements paging. The server clamps `limit` to [1, 1000] (default
# 200) and orders rows by patreon_user_id; a page shorter than the echoed
# limit is the last one. The page cap is a runaway guard, not a size budget:
# 50 x 1000 rows is orders of magnitude past any real projection, so hitting
# it means the server is not honouring `offset` and we would loop forever.
BULK_PAGE_LIMIT = 1000
BULK_MAX_PAGES = 50


@dataclass
class Answer:
    """One entitlement lookup. Immutable-by-convention; never mutate in place.

    `verdict` is the only thing a decision may branch on, and the two
    predicates below are the only supported way to branch on it. Reading
    `.raw["entitled"]` directly re-introduces the boolean this class exists to
    prevent -- if you find yourself doing that, the bug is upstream of here.
    """

    verdict: str
    email: str = field(repr=False)      # real address; mask() before any surface
    http_status: Optional[int] = None
    error: Optional[str] = None
    stale: bool = False
    status: Optional[str] = None        # active_patron / declined_patron / former_patron
    reason: Optional[str] = None        # "unknown" for an address never seen
    tiers: List[str] = field(default_factory=list)
    amount_cents: Optional[int] = None
    synced_at: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def grants(self) -> bool:
        """May the caller EXPAND access on this answer? Only ever on a clean yes."""
        return self.verdict == YES

    @property
    def revokes(self) -> bool:
        """May the caller REDUCE access on this answer? Only ever on a clean no.

        Note this is not `not grants`. UNKNOWN is False for both, and that
        asymmetry is the entire safety property of this module.
        """
        return self.verdict == NO

    @property
    def answered(self) -> bool:
        return self.verdict in (YES, NO)

    @property
    def never_seen(self) -> bool:
        """A definitive no for an address the service has never heard of.

        Almost always one of: they never subscribed, or the roster's
        billing.holder is a typo. The second is an operator error that this
        system must surface loudly rather than silently time out on.
        """
        return self.verdict == NO and self.reason == "unknown"

    def describe(self) -> str:
        """One short human line for a log or a Discord digest."""
        if self.verdict == YES:
            bits = ["entitled"]
            if self.stale:
                bits.append("(stale)")
            if self.tiers:
                bits.append("tiers=" + ",".join(self.tiers))
            return " ".join(bits)
        if self.verdict == NO:
            if self.never_seen:
                return "NOT ENTITLED (address never seen by the service)"
            return "not entitled (status=%s)" % (self.status or "?")
        return "NO ANSWER (%s)" % (self.error or "unspecified")


def _unknown(email: str, error: str, http_status: Optional[int] = None,
             raw: Optional[dict] = None) -> Answer:
    return Answer(verdict=UNKNOWN, email=email, error=error,
                  http_status=http_status, raw=raw or {})


@dataclass
class BulkAnswer:
    """One call to GET /v1/entitlements. NEVER raises -- see EntitlementClient.bulk().

    `state` is one of the four BULK_* strings above and is the only thing a
    caller may branch on, same law as `Answer.verdict`. `count` is deliberately
    None in every state except 'ok': a caller that reads `count or 0` on a
    'no-scope' or 'unreachable' answer would silently treat "I could not ask"
    as "zero people are entitled", which is exactly the UNPROVEN_BLIND vs
    UNPROVEN_EMPTY distinction payer_oracle.py exists to keep apart.

    In the 'ok' state `count == len(entitled)`: the number of DISTINCT
    addresses whose member row reads `entitled: true`, across every page. It
    is NOT the server's per-page `count` field (that is just the page length
    and includes former/declined patrons) -- reading that number as "entitled
    accounts" is the QFLX-119 bug that let oracle row 5 go green on a
    projection containing nobody who pays.

    `entitled` holds the real (unmasked) addresses the service reports, kept
    in memory only for the duration of one run so payer_oracle.judge() can
    cross-check them against declared billing.holder values. Per SPEC section
    4 this list must NEVER be logged, persisted, or sent to Kuma/Discord
    unmasked -- every caller that prints from it must call mask() first.
    """

    state: str
    count: Optional[int] = None
    entitled: List[str] = field(default_factory=list, repr=False)
    error: Optional[str] = None
    http_status: Optional[int] = None

    @property
    def supported(self) -> bool:
        """True only when the projection is actually readable this run."""
        return self.state == BULK_OK


class EntitlementClient:
    """Thin, fail-safe client for the Starhold entitlement API.

    NEVER raises for anything the network or the remote service can do. Every
    such condition becomes an UNKNOWN answer, because a raised exception in a
    per-household loop would either abort the run (leaving earlier households
    half-processed) or get swallowed by a bare `except` somewhere upstream and
    quietly become a bool. Both are worse than an explicit UNKNOWN.

    It DOES raise ValueError for programmer error -- an empty email, a missing
    key -- because those are bugs that must not be retried against a live
    service 96 times a day.
    """

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 timeout: int = DEFAULT_TIMEOUT, opener=None):
        if not api_key or not api_key.strip():
            raise ValueError(
                "entitlement API key is empty. Refusing to construct a client "
                "that would 401 on every lookup and report every member as "
                "unanswerable -- that failure must be loud at startup, not "
                "spread across a day of silent UNKNOWNs.")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Injectable for tests. Signature matches urllib.request.urlopen.
        self._opener = opener or urllib.request.urlopen

    # -- the only public call ------------------------------------------------

    def lookup(self, email: str) -> Answer:
        """Current entitlement for one email. Never raises on I/O."""
        if not email or "@" not in email:
            raise ValueError("lookup() needs an email address, got %r" % (email,))
        email = email.strip()

        url = (self.base_url + "/v1/entitlement?"
               + urllib.parse.urlencode({"email": email}))
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + self.api_key,
            "Accept": "application/json",
            "User-Agent": UA,
        })

        try:
            with self._opener(req, timeout=self.timeout) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                body = resp.read()
        except urllib.error.HTTPError as e:
            # 401/403/429/5xx all land here. Every one is UNKNOWN: none of them
            # is the service telling us something about this person.
            return _unknown(email, "HTTP %s" % e.code, http_status=e.code)
        except urllib.error.URLError as e:
            return _unknown(email, "unreachable: %s" % (e.reason,))
        except Exception as e:                      # socket timeouts, TLS, ...
            return _unknown(email, "%s: %s" % (type(e).__name__, e))

        if code != 200:
            # Defensive: a non-raising opener (or a test double) could return a
            # non-200 without an HTTPError. Same grading either way.
            return _unknown(email, "HTTP %s" % code, http_status=code)

        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            return _unknown(email, "malformed JSON: %s" % e, http_status=code)

        if not isinstance(data, dict):
            return _unknown(email, "body is %s, expected object"
                            % type(data).__name__, http_status=code)

        if "entitled" not in data:
            # A 200 with no verdict field is a contract violation, not a "no".
            # Grading it NO would let a future server-side refactor silently
            # revoke everybody.
            return _unknown(email, "200 response has no 'entitled' field",
                            http_status=code, raw=data)

        entitled = data.get("entitled")
        if not isinstance(entitled, bool):
            return _unknown(email, "'entitled' is %s, expected bool"
                            % type(entitled).__name__, http_status=code, raw=data)

        stale = bool(data.get("stale"))
        common = dict(
            email=email,
            http_status=code,
            stale=stale,
            status=data.get("status"),
            reason=data.get("reason"),
            tiers=list(data.get("tiers") or []),
            amount_cents=data.get("amount_cents"),
            synced_at=data.get("synced_at"),
            raw=data,
        )

        if entitled:
            # Stale yes is still a yes -- acting on it only ever grants.
            return Answer(verdict=YES, **common)

        if stale:
            # Stale no may predate a renewal. Refuse to call it an answer.
            return Answer(verdict=UNKNOWN,
                          error="stale data reports not-entitled; refusing to "
                                "treat a possibly-outdated no as grounds to revoke",
                          **common)

        return Answer(verdict=NO, **common)

    def bulk(self) -> BulkAnswer:
        """GET /v1/entitlements -- the whole-projection cross-check.

        Requires the 'bulk' scope on the key. The response contract (server
        source: QuadstroNot patreon_entitlement/app.py, `bulk`) is one PAGE::

            200 {"count": <rows on THIS page>, "limit": L, "offset": O,
                 "members": [{"email": ..., "entitled": bool, "stale": bool,
                              "status": ..., ...}, ...]}

        Every page is walked (offset += limit until a page is shorter than
        its echoed limit) and the entitled set is derived row by row from
        each member's own `entitled` flag. A stale `entitled: true` still
        counts -- same asymmetry as lookup(): a stale yes was a real yes. An
        entitled row with a null email is skipped: it cannot be compared to a
        billing.holder either way.

        ANY page that is not exactly that shape -- no `members` list, a row
        without a boolean `entitled`, a `count` that disagrees with the rows,
        an offset the server did not honour, the pre-QFLX-119 top-level
        `entitled` list -- makes the WHOLE answer 'unparseable'. Never a
        partial or empty 'ok': empty-because-clean must never look like
        empty-because-broken, and judge() reads an empty 'ok' as row 8.

        A key without the scope gets::

            403 {"error": "this key lacks the 'bulk' scope"}

        That is graded 'no-scope' specifically -- not folded into the generic
        'unreachable' bucket -- because payer_oracle.judge() needs to tell the
        operator the EXACT one-line Starhold-side fix (grant the scope)
        instead of a message that reads like a network outage. Every other
        4xx/5xx, every network failure and every timeout is 'unreachable':
        none of those tells the caller anything actionable beyond "try again
        later" or "escalate to Starhold". A failure on any page fails the
        whole call the same way. NEVER raises -- see the class docstring for
        why a per-household loop cannot tolerate an exception from this
        client.
        """
        addrs = set()
        offset = 0
        for _ in range(BULK_MAX_PAGES):
            page = self._bulk_page(offset)
            if isinstance(page, BulkAnswer):
                return page                         # failure: whole call fails
            rows, limit, code = page
            for row in rows:
                email = row.get("email")
                if row["entitled"] and isinstance(email, str) and "@" in email:
                    addrs.add(email.strip().lower())
            if len(rows) < limit:
                clean = sorted(addrs)
                return BulkAnswer(state=BULK_OK, count=len(clean),
                                  entitled=clean, http_status=code)
            offset += limit
        return BulkAnswer(
            state=BULK_UNPARSEABLE,
            error="still paging after %d pages - the server is not honouring "
                  "offset" % BULK_MAX_PAGES)

    def _bulk_page(self, offset: int):
        """Fetch and validate ONE /v1/entitlements page.

        Returns (rows, echoed_limit, http_status) on a well-formed page, or a
        non-ok BulkAnswer describing why not. Never raises.
        """
        url = (self.base_url + "/v1/entitlements?"
               + urllib.parse.urlencode({"limit": BULK_PAGE_LIMIT,
                                         "offset": offset}))
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + self.api_key,
            "Accept": "application/json",
            "User-Agent": UA,
        })

        try:
            with self._opener(req, timeout=self.timeout) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                body = resp.read()
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 403 and "scope" in text.lower():
                return BulkAnswer(state=BULK_NO_SCOPE,
                                  error=(text.strip() or "HTTP 403"),
                                  http_status=e.code)
            return BulkAnswer(state=BULK_UNREACHABLE, error="HTTP %s" % e.code,
                              http_status=e.code)
        except urllib.error.URLError as e:
            return BulkAnswer(state=BULK_UNREACHABLE,
                              error="unreachable: %s" % (e.reason,))
        except Exception as e:                      # socket timeouts, TLS, ...
            return BulkAnswer(state=BULK_UNREACHABLE,
                              error="%s: %s" % (type(e).__name__, e))

        if code != 200:
            # Defensive: a non-raising opener (or a test double) could return a
            # non-200 without an HTTPError. Same grading either way.
            return BulkAnswer(state=BULK_UNREACHABLE, error="HTTP %s" % code,
                              http_status=code)

        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            return BulkAnswer(state=BULK_UNPARSEABLE,
                              error="malformed JSON: %s" % e, http_status=code)

        if not isinstance(data, dict):
            return BulkAnswer(state=BULK_UNPARSEABLE,
                              error="body is %s, expected object"
                                    % type(data).__name__, http_status=code)

        def bad(why: str) -> BulkAnswer:
            return BulkAnswer(state=BULK_UNPARSEABLE, error=why, http_status=code)

        rows = data.get("members")
        if not isinstance(rows, list):
            # Includes the pre-QFLX-119 assumption of a top-level `entitled`
            # list, and a bare `count`: neither carries per-member verdicts,
            # so neither may be read as "these are all the entitled people".
            return bad("200 body has no 'members' list")
        limit, count, echoed = data.get("limit"), data.get("count"), data.get("offset")
        # bool is an int subclass; True must not pass as limit=1.
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            return bad("'limit' is %r, expected a positive integer" % (limit,))
        if echoed != offset:
            return bad("asked for offset %d, page says %r" % (offset, echoed))
        if count != len(rows):
            return bad("'count' is %r but the page holds %d member rows"
                       % (count, len(rows)))
        if len(rows) > limit:
            return bad("page holds %d rows, more than its limit %d"
                       % (len(rows), limit))
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("entitled"), bool):
                # Never guess a missing verdict as "not entitled": that is the
                # silent-empty failure this whole function exists to refuse.
                return bad("member row %d has no boolean 'entitled'" % (offset + i))
            email = row.get("email")
            if email is not None and not isinstance(email, str):
                return bad("member row %d 'email' is %s, expected string or null"
                           % (offset + i, type(email).__name__))
        return rows, limit, code

    def healthz(self) -> bool:
        """Liveness probe. No auth required. Never raises."""
        try:
            req = urllib.request.Request(self.base_url + "/healthz",
                                         headers={"User-Agent": UA})
            with self._opener(req, timeout=self.timeout) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                return code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

def _secrets_dir() -> Path:
    env = os.environ.get("MANITOBA_SECRETS")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[3] / "secrets"
    if repo.exists():
        return repo
    return Path.home() / "secrets"


def load_key(explicit: Optional[str] = None) -> str:
    """Resolve the API key: argument, then $QFLIX_ENTITLEMENT_KEY, then secrets/.

    Raises rather than returning "" -- see EntitlementClient.__init__ for why an
    empty key must be fatal at startup instead of becoming a day of UNKNOWNs.
    """
    if explicit:
        return explicit.strip()
    env = os.environ.get("QFLIX_ENTITLEMENT_KEY")
    if env and env.strip():
        return env.strip()
    p = _secrets_dir() / "entitlement.key"
    try:
        val = p.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ValueError(
            "no entitlement API key: %s is unreadable (%s). Write the "
            "QFlix-scoped key there, or set $QFLIX_ENTITLEMENT_KEY." % (p, e))
    if not val:
        raise ValueError("%s is empty -- write the QFlix-scoped key there" % p)
    return val


def load_base_url() -> str:
    """Base URL: $QFLIX_ENTITLEMENT_URL, then secrets/entitlement.url, then default."""
    env = os.environ.get("QFLIX_ENTITLEMENT_URL")
    if env and env.strip():
        return env.strip().rstrip("/")
    p = _secrets_dir() / "entitlement.url"
    try:
        val = p.read_text(encoding="utf-8").strip()
        if val:
            return val.rstrip("/")
    except OSError:
        pass
    return DEFAULT_BASE_URL


def client_from_secrets(timeout: int = DEFAULT_TIMEOUT,
                        opener=None) -> EntitlementClient:
    return EntitlementClient(api_key=load_key(), base_url=load_base_url(),
                             timeout=timeout, opener=opener)
