"""lib/seerrusers.py -- read, provision, enable and disable Seerr accounts.

Pure stdlib. THE APP IS SEERR, on secrets/seerr.port + seerr.key. Jellyseerr is
deprecated and uninstalled; secrets/jellyseerr.* are stale leftovers that
connection-refuse, and reading them produces a silent no-op that looks like
"no users found". Never read them. (This trap has already cost one wrong
outage call in this repo.)

WHAT "DISABLED" MEANS HERE
-------------------------
Seerr has no `isDisabled` column -- the live user schema was read on 2026-08-06
and there is no such field. The only lever is the `permissions` bitfield, and
`0` is the disabled state: the person can still sign in and browse, but every
permission check fails, so they cannot request anything.

That is exactly the intended stage-1/stage-3 posture. The real access control
is which Plex libraries they can see; Seerr permissions control whether they can
ask for more. A revoked member should be able to log in and find the pitch,
not hit a wall that looks like a bug.

THE SELF-PROVISIONING RACE, and why defaultPermissions must be 0
----------------------------------------------------------------
This server runs `mediaServerLogin: true`, `localLogin: false`, and crucially
`newPlexLogin: true`. That last flag means Seerr will CREATE an account, on the
spot, for any Plex friend who signs in -- and it grants them
`settings.main.defaultPermissions`, which was 1153433760 (a full member) when
this was built.

So the sequence "operator invites -> person accepts -> person opens Seerr
before the 15-minute cron fires" hands them a fully-enabled account with no
entitlement, and this system's careful provisioning is a race it can lose. The
window is up to fifteen minutes wide and the person is, by construction,
someone who just got an invite and is curious.

The fix is structural rather than faster polling: set Seerr's
`defaultPermissions` to **0**. Then every auto-created account is born disabled,
which is precisely stage 1, and this system's job narrows to granting
permissions when entitlement appears rather than racing to remove permissions it
never wanted granted. `ensure_default_permissions_are_zero()` below asserts it,
and the gate reports loudly when it drifts.

Because `defaultPermissions` can then no longer be the source of "what does a
member get", the grant value is configuration on this side --
MEMBER_PERMISSIONS, the value Seerr was configured with on 2026-08-06. A
returning member is restored to their OWN saved prior value instead, so a
hand-tuned account is never flattened to the default by a lapse.

PROVISIONING IS import-from-plex, NOT create-local-user
-------------------------------------------------------
`localLogin` is false: a locally-created user could not sign in at all. Accounts
must be `userType: 1` (Plex-linked), which is what
`POST /api/v1/user/import-from-plex` produces. Creating a local user would make
a row that looks right in the admin list and can never be used.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30
UA = "qflix-entitlement-gate/1.0"

# Disabled. Not a sentinel -- Seerr's actual "no permissions" bitfield value.
PERMISSIONS_DISABLED = 0

# What an entitled member gets when we have no saved prior value for them.
#
# This is what 12 of the 14 live accounts actually hold, NOT what
# settings.main.defaultPermissions said. Those were different numbers on
# 2026-08-06 -- the setting read 1153433760 while the membership read
# 1155539104 -- and the setting is the wrong one to copy: it is a
# newer, NARROWER default that only the most recently added account carries.
# 1155539104 adds two bits (0x2000 and 0x200000) that the real membership has
# and the setting does not.
#
# Using the setting's value would mean a member whose saved prior permissions
# were lost -- state file gone, or provisioned before this system existed --
# is silently DEMOTED on restore, losing rights every one of their peers keeps.
# A silent demotion is worse than a loud failure: nobody reports it, because
# the feature they lost is one they rarely use, and the log says "restored".
#
# Captured as a constant rather than read live because live is now 0: the
# defaultPermissions setting was deliberately zeroed to close the
# self-provisioning race described in the module docstring, so reading it at
# runtime would grant nothing at all. Override per-run with
# --member-permissions if the membership baseline ever moves.
MEMBER_PERMISSIONS = 1155539104

USER_TYPE_PLEX = 1


class SeerrError(Exception):
    """Any Seerr failure. Fatal to the run, for the same reason PlexShareError is:
    a partial user list is indistinguishable from a short one, and this system
    acts on absence."""


@dataclass
class SeerrUser:
    id: int
    email: str
    username: str
    permissions: int
    user_type: int
    plex_id: Optional[int] = None

    @property
    def disabled(self) -> bool:
        return self.permissions == PERMISSIONS_DISABLED

    @property
    def plex_linked(self) -> bool:
        return self.user_type == USER_TYPE_PLEX


def _as_user(d: dict) -> Optional[SeerrUser]:
    try:
        uid = int(d["id"])
    except (KeyError, TypeError, ValueError):
        return None
    perms = d.get("permissions")
    plex_id = d.get("plexId")
    return SeerrUser(
        id=uid,
        email=(d.get("email") or "").strip(),
        username=(d.get("plexUsername") or d.get("username")
                  or d.get("displayName") or "").strip(),
        permissions=perms if isinstance(perms, int) else 0,
        user_type=d.get("userType") if isinstance(d.get("userType"), int) else 0,
        plex_id=plex_id if isinstance(plex_id, int) else None,
    )


class SeerrClient:
    def __init__(self, port: str, api_key: str, host: str = "127.0.0.1",
                 timeout: int = TIMEOUT, opener=None):
        if not api_key or not api_key.strip():
            raise ValueError("seerr api key is empty")
        if not str(port).strip():
            raise ValueError("seerr port is empty")
        self.base = "http://%s:%s/api/v1" % (host, str(port).strip())
        self.api_key = api_key.strip()
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def _request(self, method: str, path: str, body: Optional[dict] = None):
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json",
                   "User-Agent": UA}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise SeerrError("%s %s -> HTTP %s %s" % (method, path, e.code, detail))
        except Exception as e:
            raise SeerrError("%s %s -> %s: %s" % (method, path, type(e).__name__, e))
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError as e:
            raise SeerrError("%s %s -> unparseable JSON: %s" % (method, path, e))

    # -- reads --------------------------------------------------------------

    def users(self) -> List[SeerrUser]:
        """Every Seerr account, paged to exhaustion.

        Paging is followed rather than assumed-away: `take=200` happens to cover
        14 users today, but silently truncating at a page boundary would make
        members past the cut look like they have no Seerr account, and this
        system provisions on absence. It would create duplicates, forever.
        """
        out: List[SeerrUser] = []
        skip, take = 0, 100
        while True:
            page = self._request("GET", "/user?" + urllib.parse.urlencode(
                {"take": take, "skip": skip}))
            if not isinstance(page, dict):
                raise SeerrError("/user returned %s, expected object" % type(page).__name__)
            results = page.get("results")
            if not isinstance(results, list):
                raise SeerrError("/user response has no results list")
            for row in results:
                u = _as_user(row) if isinstance(row, dict) else None
                if u is not None:
                    out.append(u)
            if len(results) < take:
                break
            skip += take
            if skip > 10000:                      # runaway guard
                raise SeerrError("/user paging exceeded 10000 rows; refusing to loop")
        return out

    def by_email(self) -> Dict[str, SeerrUser]:
        """email (lowercased) -> user. Case folded because Plex and Seerr
        disagree about case more often than anyone expects, and a case-sensitive
        miss reads as 'no account' and provisions a duplicate."""
        return {u.email.lower(): u for u in self.users() if u.email}

    def default_permissions(self) -> int:
        s = self._request("GET", "/settings/main")
        if not isinstance(s, dict):
            raise SeerrError("/settings/main returned %s" % type(s).__name__)
        val = s.get("defaultPermissions")
        if not isinstance(val, int):
            raise SeerrError("settings.main.defaultPermissions is %r" % (val,))
        return val

    def unimported_plex_users(self) -> List[dict]:
        """Plex friends who have no Seerr account yet. Each has a Plex `id`."""
        rows = self._request("GET", "/settings/plex/users")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    # -- writes -------------------------------------------------------------

    def import_from_plex(self, plex_ids: Sequence[int]) -> List[SeerrUser]:
        """Create Plex-linked accounts for the given Plex user ids.

        Seerr grants them settings.main.defaultPermissions, which is why that
        setting must be 0 -- see the module docstring. The caller must NOT
        assume the result is disabled; it verifies by reading back.
        """
        ids = [str(int(i)) for i in plex_ids]
        if not ids:
            return []
        created = self._request("POST", "/user/import-from-plex",
                                body={"plexIds": ids})
        if not isinstance(created, list):
            return []
        return [u for u in (_as_user(r) for r in created if isinstance(r, dict))
                if u is not None]

    def set_permissions(self, user: SeerrUser, permissions: int) -> SeerrUser:
        """Write a permission bitfield and VERIFY it by reading the response.

        Seerr answers 200 for a write it did not apply (a rejected field is
        dropped, not errored). An unverified disable would leave a revoked
        member fully able to request while this system logs success -- so the
        returned object is checked and a mismatch raises.
        """
        if not isinstance(permissions, int) or permissions < 0:
            raise ValueError("permissions must be a non-negative int, got %r" % (permissions,))
        got = self._request("PUT", "/user/%d" % user.id, body={"permissions": permissions})
        after = _as_user(got) if isinstance(got, dict) else None
        if after is None:
            after = self.get(user.id)
        if after is None or after.permissions != permissions:
            raise SeerrError(
                "permission write to user %d did not stick: asked for %d, "
                "server reports %s. Treating an unverified write as success "
                "would leave a revoked member able to request while the log "
                "says otherwise."
                % (user.id, permissions, after.permissions if after else "unknown"))
        return after

    def get(self, user_id: int) -> Optional[SeerrUser]:
        try:
            d = self._request("GET", "/user/%d" % int(user_id))
        except SeerrError:
            return None
        return _as_user(d) if isinstance(d, dict) else None

    def disable(self, user: SeerrUser) -> SeerrUser:
        return self.set_permissions(user, PERMISSIONS_DISABLED)

    def enable(self, user: SeerrUser, permissions: Optional[int] = None) -> SeerrUser:
        """Restore access. Prefers the member's OWN saved prior permissions.

        Falling back to MEMBER_PERMISSIONS only when nothing was saved means a
        hand-tuned account (extra quota, 4K rights, issue management) is not
        silently flattened to the default by the act of lapsing and returning.
        """
        return self.set_permissions(user, permissions if permissions else MEMBER_PERMISSIONS)


# ---------------------------------------------------------------------------
# Config drift check
# ---------------------------------------------------------------------------

def check_default_permissions(client: SeerrClient) -> Optional[str]:
    """Return a human warning if Seerr would auto-enable new members, else None.

    Not a raise: this is a config drift the operator should hear about, not a
    reason to stop provisioning and revoking everybody else. The gate reports it
    every run until it is fixed.
    """
    try:
        val = client.default_permissions()
    except SeerrError as e:
        return "could not read Seerr defaultPermissions (%s)" % e
    if val != PERMISSIONS_DISABLED:
        return (
            "Seerr settings.main.defaultPermissions is %d, not 0. newPlexLogin "
            "is on, so any Plex friend who signs in before this gate next runs "
            "is auto-created with those permissions -- a fully enabled account "
            "with no entitlement. Set it to 0 in Seerr > Settings > Users; this "
            "gate grants %d on entitlement instead."
            % (val, MEMBER_PERMISSIONS))
    return None


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _secrets_dir() -> Path:
    env = os.environ.get("MANITOBA_SECRETS")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[3] / "secrets"
    if repo.exists():
        return repo
    return Path.home() / "secrets"


def client_from_secrets(opener=None) -> SeerrClient:
    d = _secrets_dir()
    try:
        port = (d / "seerr.port").read_text(encoding="utf-8").strip()
        key = (d / "seerr.key").read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SeerrError(
            "cannot read Seerr credentials from %s (%s). NOTE: the app is "
            "SEERR -- do not fall back to jellyseerr.port/jellyseerr.key, "
            "which are stale leftovers that connection-refuse and would make "
            "every member look like they have no account." % (d, e))
    return SeerrClient(port=port, api_key=key, opener=opener)
