"""lib/plexshare.py -- read and write which library sections a Plex friend can see.

Pure stdlib (urllib + ElementTree). No plexapi import, deliberately: the parsing
half must be unit-testable on a workstation that has no venv and no token, and
plexapi's own share code reaches the network inside the function that computes
section ids. The wire format below was read off the live API on 2026-08-06 and
matches what plexapi 4.18.1 sends.

THE WIRE FORMAT
---------------
Three endpoints, all on plex.tv, all authenticated with the server owner's token:

  GET  /api/servers/<machineId>                    -> the section catalogue
  GET  /api/servers/<machineId>/shared_servers     -> every friend + their sections
  PUT  /api/servers/<machineId>/shared_servers/<shared_server_id>
       {"server_id": <machineId>,
        "shared_server": {"library_section_ids": [<int>, ...]}}

`library_section_ids` takes the Section **id** attribute, NOT the **key**. They
are different numbers for the same library (`id=132920523, key=4` for
QFlix - Movies) and the ids are the ones plex.tv registered globally, stable
across every share. Passing keys silently shares nothing, because plex.tv looks
the value up in a table where it does not appear.

`allLibraries="1"`
------------------
Every pre-existing share on this server carries `allLibraries="1"`, meaning "the
whole server, including anything added later". That is why creating the Welcome
library will hand it to all fourteen existing members for free, and it is also
why this module never tries to preserve the flag: writing an explicit
`library_section_ids` list turns it off, and there is no documented way to turn
it back on through this endpoint.

Losing the flag is fine and is in fact the safer state, but only because this
system re-computes the full section set from the live catalogue on EVERY run.
An entitled member is granted "every section that exists right now", so a
library added at 3pm reaches them by 3:15 without the flag. If that
recomputation is ever replaced by a hardcoded list, this paragraph becomes the
bug report.

THE EMPTY-LIST RULE
-------------------
An empty `library_section_ids` does not mean "share nothing". plex.tv reads it
as "unshare this server", which deletes the share object and evicts the person
-- they vanish from the friends list and cannot return without a fresh invite
they must accept out of their email.

That is the exact outcome the whole design forbids, and it is one empty list
away at all times: if the Welcome section (the REQUIRED floor library; optional
extras such as `QFlix - Test` only shrink the floor when missing) is missing or
renamed, the computed
"minimum access" set is `[]`, and the natural code path posts it. So
`set_sections()` REFUSES an empty list, loudly, always. It is not a parameter,
not a flag, and not overridable -- there is no legitimate caller in this system,
and the one illegitimate caller is a typo in a section title.

DOWNLOADS (allowSync) -- QFLX-49, 2026-10-10
--------------------------------------------
Every SharedServer also carries `allowSync`: "1" lets that friend download
(Plex calls it sync) from this server, "0" hides the Download button. It is a
per-share flag, not a server setting, and it is NOT carried by the
library_section_ids PUT above -- that write leaves it untouched either way.

Its default is OFF at every way a share is born: the Plex web invite dialog
leaves "Allow Downloads" unticked, and plexapi's `inviteFriend()` defaults to
`allowSync=False`. Operator policy is that every member may download, so a
share born with the default is drift, and set_allow_sync() is the corrector.

The write is a different endpoint keyed by the friend's plex.tv USER id, not
the shared_server id (plexapi 4.18.1 `updateFriend`, FRIENDUPDATE):

  PUT  /api/v2/sharings/<userID>?allowSync=1

Only the query args named are changed; the library set, camera upload and
channel flags are left as they are.

ACCESS TOKENS
-------------
Each SharedServer carries an `accessToken` that grants access to this server as
that user. It is never parsed, never stored, and never logged. Do not add it to
the dataclass "for completeness".
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import json
from typing import Dict, List, Optional, Sequence, Set
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PLEX_TV = "https://plex.tv"
TIMEOUT = 30
UA = "qflix-entitlement-gate/1.0"
# The /api/v2 endpoints want a client identity on every call (plexapi always
# sends one). Harmless on the v1 endpoints, so it is sent everywhere.
CLIENT_ID = "qflix-entitlement-gate"


class PlexShareError(Exception):
    """Any failure to read or write shares. Always fatal to the run.

    Unlike the entitlement client -- which degrades to UNKNOWN so one
    unreachable service cannot stall every household -- a Plex failure raises.
    The difference is that an entitlement UNKNOWN has a safe interpretation
    ("do nothing to this person") whereas a partial read of the share list has
    none: a share that failed to parse is indistinguishable from a share that
    does not exist, and "does not exist" is what this system would act on.
    """


@dataclass(frozen=True)
class Section:
    id: int          # what library_section_ids wants
    key: int         # what the local PMS calls it
    title: str
    type: str


@dataclass
class Share:
    """One friend's access to this server."""

    shared_server_id: int
    user_id: int
    email: str
    username: str
    section_ids: Set[int] = field(default_factory=set)
    all_libraries: bool = False
    accepted_at: Optional[dt.datetime] = None
    invited_at: Optional[dt.datetime] = None
    # None = plex.tv did not report the attribute at all. Never treat that as
    # False: "unknown" must not trigger a write, and must not page.
    allow_sync: Optional[bool] = None

    @property
    def accepted(self) -> bool:
        """Has the person actually taken the invite?

        A pending invite has `invitedAt` but no `acceptedAt`. Provisioning a
        Seerr account for somebody who has not accepted would create a row for
        a person who may never appear, and expanding their libraries would be
        writing to a share they do not hold.
        """
        return self.accepted_at is not None


def _ts(value: Optional[str]) -> Optional[dt.datetime]:
    """Unix seconds -> aware UTC datetime. 0, empty, and junk all become None."""
    if not value:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(n, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _flag(value: Optional[str]) -> Optional[bool]:
    """plex.tv "1"/"0" -> True/False; absent or anything else -> None."""
    if value == "1":
        return True
    if value == "0":
        return False
    return None


# ---------------------------------------------------------------------------
# Pure parsers -- no network, fully unit-testable from fixture XML
# ---------------------------------------------------------------------------

def parse_sections(xml_text: str) -> List[Section]:
    """Parse GET /api/servers/<machineId> into the section catalogue."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise PlexShareError("section catalogue is not valid XML: %s" % e)

    out: List[Section] = []
    for elem in root.iter("Section"):
        sid, key = elem.get("id"), elem.get("key")
        if sid is None or key is None:
            continue
        try:
            out.append(Section(id=int(sid), key=int(key),
                               title=elem.get("title") or "",
                               type=elem.get("type") or ""))
        except ValueError:
            continue
    if not out:
        raise PlexShareError(
            "section catalogue parsed to zero sections. Treating that as an "
            "empty server would compute an empty share set for everybody, "
            "which unshares the server. Refusing.")
    return out


def parse_shares(xml_text: str) -> List[Share]:
    """Parse GET /api/servers/<machineId>/shared_servers.

    A zero-share result is legitimate (a server with no friends) and is NOT an
    error, unlike a zero-section catalogue. Callers that would act on emptiness
    must make that judgement themselves.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise PlexShareError("shared_servers is not valid XML: %s" % e)

    shares: List[Share] = []
    for elem in root.iter("SharedServer"):
        try:
            ssid = int(elem.get("id"))
            uid = int(elem.get("userID"))
        except (TypeError, ValueError):
            # A share we cannot identify is one we must never write to. Skipping
            # is safe; guessing an id would write somebody else's access.
            continue
        ids: Set[int] = set()
        for sec in elem.findall("Section"):
            if sec.get("shared") != "1":
                continue
            try:
                ids.add(int(sec.get("id")))
            except (TypeError, ValueError):
                continue
        shares.append(Share(
            shared_server_id=ssid,
            user_id=uid,
            email=(elem.get("email") or "").strip(),
            username=(elem.get("username") or "").strip(),
            section_ids=ids,
            all_libraries=elem.get("allLibraries") == "1",
            accepted_at=_ts(elem.get("acceptedAt")),
            invited_at=_ts(elem.get("invitedAt")),
            allow_sync=_flag(elem.get("allowSync")),
        ))
    return shares


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PlexShareClient:
    def __init__(self, token: str, machine_id: str, base_url: str = PLEX_TV,
                 timeout: int = TIMEOUT, opener=None):
        if not token or not token.strip():
            raise ValueError("plex token is empty")
        if not machine_id or not machine_id.strip():
            raise ValueError("plex machineIdentifier is empty")
        self.token = token.strip()
        self.machine_id = machine_id.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    # -- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 query: Optional[Dict[str, str]] = None) -> str:
        q = dict(query or {})
        q["X-Plex-Token"] = self.token
        url = "%s%s?%s" % (self.base_url, path, urllib.parse.urlencode(q))
        data = None
        headers = {"Accept": "application/xml", "User-Agent": UA,
                   "X-Plex-Client-Identifier": CLIENT_ID,
                   "X-Plex-Product": CLIENT_ID}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # The URL carries the owner token. Never let it into an exception
            # string that ends up in a log, a Discord message, or journald.
            raise PlexShareError("%s %s -> HTTP %s" % (method, path, e.code))
        except Exception as e:
            raise PlexShareError("%s %s -> %s: %s" % (method, path, type(e).__name__, e))

    # -- reads --------------------------------------------------------------

    def sections(self) -> List[Section]:
        return parse_sections(self._request("GET", "/api/servers/" + self.machine_id))

    def shares(self) -> List[Share]:
        return parse_shares(self._request(
            "GET", "/api/servers/%s/shared_servers" % self.machine_id))

    # -- writes -------------------------------------------------------------

    def set_allow_sync(self, share: Share, allow: bool = True) -> None:
        """Set the friend's Download permission (allowSync) on this server.

        Keyed by plex.tv user id, not shared_server id -- see the module
        docstring's DOWNLOADS section. Idempotent: writing the value a share
        already holds is a no-op on plex.tv.
        """
        if share.user_id <= 0:
            raise PlexShareError(
                "refusing to set allowSync on shared_server %s: no plex.tv user "
                "id (a pending invite by email has none to address)"
                % share.shared_server_id)
        self._request("PUT", "/api/v2/sharings/%d" % share.user_id,
                      query={"allowSync": "1" if allow else "0"})


    def set_sections(self, share: Share, section_ids: Sequence[int]) -> None:
        """Replace the set of sections a friend can see.

        Refuses an empty list. See the module docstring: plex.tv reads `[]` as
        "unshare the server", which evicts the person rather than restricting
        them, and this system's entire revocation design rests on never doing
        that.
        """
        ids = sorted({int(i) for i in section_ids})
        if not ids:
            raise PlexShareError(
                "refusing to write an empty section list for shared_server %s. "
                "plex.tv reads an empty list as 'unshare this server', which "
                "DELETES the share and evicts the person -- they would need a "
                "fresh invite accepted from email to come back. If the intent "
                "was minimum access, the Welcome section is missing from the "
                "catalogue; fix that instead." % share.shared_server_id)

        self._request(
            "PUT",
            "/api/servers/%s/shared_servers/%s" % (self.machine_id, share.shared_server_id),
            body={"server_id": self.machine_id,
                  "shared_server": {"library_section_ids": ids}},
        )


# ---------------------------------------------------------------------------
# Section-set helpers (pure)
# ---------------------------------------------------------------------------

def find_section(sections: Sequence[Section], title: str) -> Optional[Section]:
    """Case-insensitive, whitespace-tolerant title lookup."""
    want = title.strip().lower()
    for s in sections:
        if s.title.strip().lower() == want:
            return s
    return None


def full_access_ids(sections: Sequence[Section], welcome_title: str,
                    extra_floor_titles: Sequence[str] = ()) -> List[int]:
    """Every section that exists RIGHT NOW, EXCEPT the floor (Welcome + extras).

    Recomputed each run on purpose -- see the allLibraries note in the module
    docstring. This is what replaces the flag that writing an explicit list
    destroys, and it is why a library created at 3pm reaches entitled members
    by 3:15 rather than never.

    WHY WELCOME IS SUBTRACTED HERE
    ------------------------------
    Welcome holds one video whose entire content is "go to Patreon and activate
    your subscription". It is the floor a NON-entitled account is reduced to,
    and it is addressed to exactly that audience. An entitled member who can see
    it is being asked to buy something they already pay for -- and because the
    two sets were previously "everything" and "Welcome", every entitled member
    would have been shown it the moment the gate was armed.

    So the two access levels are now disjoint by construction rather than
    nested: Welcome appears in `minimum_access_ids` and NOWHERE else. Membership
    of Welcome is therefore a faithful signal of "this account is not currently
    entitled", which is what makes the video's instruction true for everyone who
    can read it.

    Subtracting at the point the FULL set is computed -- rather than adding an
    exclusion at each call site -- means a future caller cannot reintroduce the
    overlap by forgetting to filter.

    RAISES rather than returning `[]`, for the same reason minimum_access_ids
    does. If Welcome is somehow the only section on the server, the subtraction
    would produce an empty list, and an empty list does not restrict a share --
    it DELETES it. Evicting every entitled member because a library was renamed
    is the single worst thing this module can do, so the degenerate case is
    named and refused instead of written.

    EXTRA FLOOR TITLES (QFLX-4, 2026-10-03)
    ---------------------------------------
    `QFlix - Test` holds playback test clips for prospects. It belongs to the
    not-entitled surface exactly like Welcome, so it is subtracted here for the
    same reason: subtracting at the point FULL is computed means no caller can
    re-add it by forgetting a filter. Matching is per-section through
    find_section(), so it is exactly as case/whitespace lenient as Welcome.
    """
    floor_titles = [welcome_title] + list(extra_floor_titles)
    ids = sorted(s.id for s in sections
                 if not any(find_section([s], t) is not None for t in floor_titles))
    if not ids:
        raise PlexShareError(
            "full access computes to an empty section list (sections=%d, "
            "welcome=%r, extra_floor=%r). An empty list unshares the server "
            "instead of granting it, so this is refused. Either the Plex server "
            "has no libraries besides the floor, or the section list failed to "
            "load." % (len(sections), welcome_title, list(extra_floor_titles)))
    return ids


def minimum_access_ids(sections: Sequence[Section], welcome_title: str,
                       extra_floor_titles: Sequence[str] = ()) -> List[int]:
    """The floor: Welcome (REQUIRED) plus any extra floor library that exists.

    Raises rather than returning `[]` when Welcome is absent. The empty list is
    the eviction bug; catching it here names the actual cause (a missing or
    renamed section) instead of letting set_sections() report the symptom.

    Extras are OPTIONAL by design: a missing `QFlix - Test` shrinks the floor to
    Welcome, it does not stop the gate. The anti-eviction guarantee rests on
    Welcome alone, and halting every grant and revoke because a convenience
    library was renamed would trade a cosmetic fault for a real one. The
    absence is surfaced by missing_floor_titles() in the run summary instead.
    """
    sec = find_section(sections, welcome_title)
    if sec is None:
        raise PlexShareError(
            "no Plex section titled %r, so 'minimum access' has no meaning and "
            "would compute to an empty share list -- which unshares the server "
            "instead of restricting it. Create the section (see "
            "scripts/configure/59b-plex-welcome-library.py) or correct the "
            "--welcome-section argument." % welcome_title)
    ids = {sec.id}
    for t in extra_floor_titles:
        extra = find_section(sections, t)
        if extra is not None:
            ids.add(extra.id)
    return sorted(ids)


def shares_without_downloads(shares: Sequence[Share]) -> List[Share]:
    """Accepted shares whose allowSync is explicitly OFF.

    Policy (QFLX-49): every member may download, floor and tagalong shares
    included -- downloads are a playback permission, not a library grant, so
    this never widens what anybody can SEE. Pending invites are skipped: they
    have no accepted user to address yet and are picked up on the first run
    after acceptance. An unreported flag (None) is not drift.
    """
    return [s for s in shares if s.accepted and s.allow_sync is False]


def missing_floor_titles(sections: Sequence[Section],
                         extra_floor_titles: Sequence[str]) -> List[str]:
    """Extra floor titles with no matching section -- for the run summary."""
    return [t for t in extra_floor_titles if find_section(sections, t) is None]
