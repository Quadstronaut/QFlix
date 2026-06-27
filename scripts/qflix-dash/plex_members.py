#!/usr/bin/env python3
"""Print QFlix Plex authorized accounts (owner + shared/Home users) as JSON.

Reuses the existing python-plexapi venv. Token comes from $PLEX_TOKEN (a plex.tv
*account* token). Output: [{"id": int, "email": str, "username": str}, ...] to
stdout; non-zero exit + stderr message on failure. Consumed by the dashboard's
membership check (src/lib/server/membership.ts), cached 10 min.
"""
import json
import os
import sys

from plexapi.myplex import MyPlexAccount


def main() -> None:
    token = os.environ.get("PLEX_TOKEN")
    if not token:
        sys.exit("PLEX_TOKEN unset")
    acct = MyPlexAccount(token=token)
    out = [{"id": acct.id, "email": (acct.email or "").lower(), "username": acct.username}]
    for u in acct.users():
        out.append({"id": u.id, "email": (u.email or "").lower(), "username": u.title})
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
