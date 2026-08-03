#!/usr/bin/env python3
"""patreon-report.py -- report Patreon pledge status against the roster.

WHAT THIS IS
------------
A REPORT. It reads Patreon's view of who is pledging, matches those patrons to
roster households on (rail='patreon', payer_ref), and prints the reconciliation.
That is the whole job.

WHAT THIS DELIBERATELY IS NOT
-----------------------------
It does not gate. There is no code path in this file that writes gate state,
calls Plex/Seerr/Listmonk, un-shares a library, or mutates the roster. It has no
--execute flag because it has nothing to execute. `patron_status` from this tool
is INFORMATION for a human, not an entitlement decision, and the separation is
the same one lib/members.py already draws in its docstring: "the roster is
operator intent, the rail is external fact, and conflating them is how a payment
outage turns into a mass revocation."

THE FAILURE MODE THIS FILE IS BUILT AROUND
------------------------------------------
Patreon creator access tokens EXPIRE (historically ~1 month) and they expire
SILENTLY -- the API answers 401, and a naive client that swallows that reports
zero patrons. Zero patrons is indistinguishable from "everyone lapsed at once",
which is the single most damaging thing a report like this could say. So:

  - an auth failure is a hard, loud, non-zero exit with its own status string
    (AUTH_FAILED), never an empty member list;
  - a genuinely empty campaign reports OK with count=0 and says so explicitly;
  - the two are different exit codes, so a cron wrapper cannot conflate them.

Same law REA's deadman path follows: empty-because-clean must never look like
empty-because-broken.

TOKEN ROTATION
--------------
On 401 the refresh token is spent for a new pair and secrets/patreon.json is
rewritten atomically (tmp file + os.replace). Non-atomic rewrites are how you
end up with a half-written credentials file and no way back in -- the refresh
token is single-use on rotation, so losing it mid-write costs a manual
re-registration.

PII
---
Patron names and emails are member data. This tool masks them by default and
REFUSES to write output anywhere inside a tracked directory (same guard as
lib/members.find_roster). Nothing it produces belongs in git.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

API = "https://www.patreon.com/api/oauth2/v2"
TOKEN_URL = "https://www.patreon.com/api/oauth2/token"
UA = "qflix-support-reporter/1.0"
TIMEOUT = 25

# Exit codes. Distinct on purpose -- see the module docstring.
EXIT_OK = 0
EXIT_AUTH_FAILED = 3
EXIT_API_FAILED = 4
EXIT_CONFIG = 5

# Member attributes we ask for. Keep this list explicit: Patreon v2 returns an
# EMPTY attributes object if you request none, which reads as "no data" rather
# than as the programming error it is.
MEMBER_FIELDS = ",".join([
    "patron_status",
    "currently_entitled_amount_cents",
    "last_charge_date",
    "last_charge_status",
    "full_name",
    "email",
    "pledge_relationship_start",
])

_TRACKED_DIRS = ("manifest", "docs", "scripts", "tests", "apps")


class PatreonError(Exception):
    """Any failure that must not be mistaken for 'no patrons'."""


class AuthError(PatreonError):
    """Credentials are dead. NEVER degrade this into an empty result."""


# ---------- credentials ----------

def creds_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("QFLIX_PATREON_CREDS")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "secrets" / "patreon.json"


def load_creds(path: Path) -> Dict[str, str]:
    if not path.exists():
        raise PatreonError(
            "no Patreon credentials at %s -- register a client at "
            "patreon.com/portal/registration/register-clients and write "
            "client_id/client_secret/access_token/refresh_token there" % path)
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in ("client_id", "client_secret", "access_token", "refresh_token")
               if not data.get(k)]
    if missing:
        raise PatreonError("%s is missing required keys: %s" % (path, ", ".join(missing)))
    return data


def save_creds(path: Path, creds: Dict[str, str]) -> None:
    """Atomic rewrite. A torn credentials file costs a manual re-registration."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(creds, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------- api ----------

def _get(url: str, token: str) -> Tuple[int, str]:
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def refresh(creds: Dict[str, str], path: Path) -> Dict[str, str]:
    """Spend the refresh token for a new pair and persist it."""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            new = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AuthError(
            "token refresh REJECTED (HTTP %s): %s -- the refresh token is spent "
            "or revoked. Re-register the client and rewrite %s."
            % (e.code, e.read().decode("utf-8", "replace")[:300], path))
    if not new.get("access_token"):
        raise AuthError("token refresh returned no access_token: %r" % new)
    creds = dict(creds)
    creds["access_token"] = new["access_token"]
    # Patreon rotates the refresh token too; keeping the old one locks you out.
    if new.get("refresh_token"):
        creds["refresh_token"] = new["refresh_token"]
    save_creds(path, creds)
    return creds


def api_get(url: str, creds: Dict[str, str], path: Path,
            _retried: bool = False) -> Tuple[Dict, Dict[str, str]]:
    status, body = _get(url, creds["access_token"])
    if status == 401 and not _retried:
        creds = refresh(creds, path)
        return api_get(url, creds, path, _retried=True)
    if status == 401:
        raise AuthError("still 401 after a successful token refresh: %s" % body[:300])
    if status != 200:
        raise PatreonError("Patreon API returned HTTP %s for %s: %s"
                           % (status, url, body[:300]))
    return json.loads(body), creds


def campaign_id(creds: Dict[str, str], path: Path) -> Tuple[str, Dict[str, str]]:
    data, creds = api_get(API + "/campaigns", creds, path)
    rows = data.get("data") or []
    if not rows:
        raise PatreonError("this token sees no campaigns -- wrong account, or the "
                           "client lacks the 'campaigns' scope")
    if len(rows) > 1:
        # Ambiguity resolves to stop-and-ask, per the roster module's law.
        raise PatreonError("token sees %d campaigns; pass --campaign-id to disambiguate"
                           % len(rows))
    return rows[0]["id"], creds


def fetch_members(cid: str, creds: Dict[str, str], path: Path) -> List[Dict]:
    """All members, following Patreon's cursor pagination."""
    out: List[Dict] = []
    url = "%s/campaigns/%s/members?%s" % (API, cid, urllib.parse.urlencode({
        "fields[member]": MEMBER_FIELDS, "page[count]": 200}))
    seen_cursors = set()
    while url:
        data, creds = api_get(url, creds, path)
        out.extend(data.get("data") or [])
        nxt = (data.get("meta", {}).get("pagination", {})
                   .get("cursors", {}).get("next"))
        if not nxt or nxt in seen_cursors:
            break
        seen_cursors.add(nxt)
        url = "%s/campaigns/%s/members?%s" % (API, cid, urllib.parse.urlencode({
            "fields[member]": MEMBER_FIELDS, "page[count]": 200, "page[cursor]": nxt}))
    return out


# ---------- reconciliation ----------

def mask(value: Optional[str]) -> str:
    if not value:
        return "(none)"
    if "@" in value:
        local, _, domain = value.partition("@")
        return (local[:2] if len(local) > 2 else local[:1]) + "***@" + domain
    return value[:2] + "***"


def reconcile(members: List[Dict], roster) -> Dict:
    """Match patrons to roster households. Pure data; decides nothing."""
    by_ref = roster.by_payer_ref() if roster is not None else {}
    matched, unmatched = [], []
    for m in members:
        a = m.get("attributes", {})
        # payer_ref is matched case-insensitively, same as members.by_payer_ref builds it.
        keys = [(("patreon", (a.get(f) or "").strip().lower()))
                for f in ("full_name", "email") if a.get(f)]
        hh = next((by_ref[k] for k in keys if k in by_ref), None)
        rec = {
            "patreon_member_id": m.get("id"),
            "full_name": a.get("full_name"),
            "email": a.get("email"),
            "patron_status": a.get("patron_status"),
            "cents": a.get("currently_entitled_amount_cents") or 0,
            "last_charge_status": a.get("last_charge_status"),
            "last_charge_date": a.get("last_charge_date"),
            "household_id": getattr(hh, "hid", None) if hh else None,
        }
        (matched if hh else unmatched).append(rec)

    # Roster households that say they pay on patreon but have no patron record.
    on_rail = []
    if roster is not None:
        seen = {r["household_id"] for r in matched if r["household_id"]}
        for h in roster:
            b = getattr(h, "billing", None)
            if b and b.rail == "patreon" and getattr(h, "hid", None) not in seen:
                on_rail.append({"household_id": h.hid,
                                "payer_ref": b.payer_ref,
                                "amount_usd": b.amount_usd})
    return {"matched": matched, "unmatched_patrons": unmatched,
            "roster_on_patreon_without_patron": on_rail}


def render(rep: Dict, members: List[Dict], full: bool) -> str:
    show = (lambda v: v or "(none)") if full else mask
    L = []
    n = len(members)
    counts: Dict[str, int] = {}
    cents = 0
    for m in members:
        a = m.get("attributes", {})
        counts[a.get("patron_status") or "unknown"] = counts.get(
            a.get("patron_status") or "unknown", 0) + 1
        cents += a.get("currently_entitled_amount_cents") or 0

    L.append("Patreon support report")
    L.append("=" * 60)
    if n == 0:
        L.append("STATUS: OK - the campaign has ZERO patrons.")
        L.append("        This is a real, successful read, not an auth failure.")
        L.append("        (An expired token exits %d with AUTH_FAILED instead.)"
                 % EXIT_AUTH_FAILED)
        return "\n".join(L)
    L.append("patrons: %d   entitled: $%.2f/mo" % (n, cents / 100.0))
    L.append("status : " + ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    L.append("")
    L.append("-- matched to a roster household --")
    for r in rep["matched"] or []:
        L.append("  %-22s %-16s $%6.2f  charge=%-9s  household=%s"
                 % (show(r["full_name"]), r["patron_status"], r["cents"] / 100.0,
                    r["last_charge_status"], r["household_id"]))
    if not rep["matched"]:
        L.append("  (none)")
    L.append("")
    L.append("-- patrons with NO roster household --")
    for r in rep["unmatched_patrons"] or []:
        L.append("  %-22s %-16s $%6.2f  email=%s"
                 % (show(r["full_name"]), r["patron_status"], r["cents"] / 100.0,
                    show(r["email"])))
    if not rep["unmatched_patrons"]:
        L.append("  (none)")
    L.append("")
    L.append("-- roster says rail=patreon but no patron record --")
    for r in rep["roster_on_patreon_without_patron"] or []:
        L.append("  household=%-14s payer_ref=%s amount=%s"
                 % (r["household_id"], show(r["payer_ref"]), r["amount_usd"]))
    if not rep["roster_on_patreon_without_patron"]:
        L.append("  (none)")
    L.append("")
    L.append("This report changes nothing. It grants and revokes no access.")
    return "\n".join(L)


def _reject_if_tracked(p: Path) -> Path:
    parts = {x.lower() for x in p.resolve().parts}
    if parts & {d.lower() for d in _TRACKED_DIRS}:
        raise PatreonError("refusing to write patron data to %s -- that is a tracked "
                           "directory and this output contains member PII" % p)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Report Patreon pledge status. Read-only.")
    ap.add_argument("--creds", help="path to patreon.json (default secrets/patreon.json)")
    ap.add_argument("--campaign-id", help="skip discovery / disambiguate multiple campaigns")
    ap.add_argument("--roster", help="path to members.yaml (default: members.find_roster())")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--full", action="store_true", help="do NOT mask names/emails")
    ap.add_argument("--out", help="write to a file (refused inside tracked dirs)")
    args = ap.parse_args(argv)

    cpath = creds_path(args.creds)
    try:
        creds = load_creds(cpath)
    except PatreonError as e:
        print("CONFIG: %s" % e, file=sys.stderr)
        return EXIT_CONFIG

    try:
        cid = args.campaign_id
        if not cid:
            cid, creds = campaign_id(creds, cpath)
        members = fetch_members(cid, creds, cpath)
    except AuthError as e:
        # The whole point of this branch: never let this become "0 patrons".
        print("AUTH_FAILED: %s" % e, file=sys.stderr)
        print("AUTH_FAILED: reporting NOTHING rather than a false mass-lapse.",
              file=sys.stderr)
        return EXIT_AUTH_FAILED
    except PatreonError as e:
        print("API_FAILED: %s" % e, file=sys.stderr)
        return EXIT_API_FAILED

    roster = None
    try:
        import members as members_mod  # noqa: E402  (path injected above)
        rpath = Path(args.roster) if args.roster else members_mod.find_roster()
        roster = members_mod.load(rpath)
    except Exception as e:
        # A missing/!invalid roster must not fail the report -- the Patreon side is
        # still true and useful on its own. Say so instead of silently degrading.
        print("note: roster unavailable (%s); reporting Patreon side only" % e,
              file=sys.stderr)

    rep = reconcile(members, roster)
    rep["campaign_id"] = cid
    rep["patron_count"] = len(members)
    text = json.dumps(rep, indent=2) if args.json else render(rep, members, args.full)

    if args.out:
        p = _reject_if_tracked(Path(args.out))
        p.write_text(text + "\n", encoding="utf-8")
        print("wrote %s" % p)
    else:
        print(text)
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PatreonError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(EXIT_API_FAILED)
