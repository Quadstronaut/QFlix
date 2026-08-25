#!/usr/bin/env python3
"""63-plex-collection-display.py -- stop Plex advertising content it does not have.

Idempotent. Re-run freely: it reports NO-OP per already-correct section and
exits 0. Read-only unless a section actually differs.

THE DEFECT
----------
On 2026-08-24 a member browsing QFlix on someone else's TV saw a shelf of
franchise names -- Deadpool Collection, Dune Collection, Gladiator Collection,
Godzilla Collection, Guardians of the Galaxy, Mad Max, Moana, Sonic, Venom and
more -- and found NOTHING behind any of them. 14 of the 17 collections in
QFlix - Movies held **zero items**; both TV collections held zero. That is not
cosmetic: a collection tile is a promise about the library, and these were
promising films that had been deleted months earlier.

WHY THEY WERE EMPTY
-------------------
Plex auto-creates a franchise collection once a section holds `autoCollectionThreshold`
members of it. qflix-reaper.py then deletes the members under the retention
policy, and Plex leaves the collection object behind. Plex's own summary for
autoCollectionThreshold says the quiet part out loud: "Changing this value will
have no effect on existing collections." The threshold governs CREATION and
never CLEANUP, so husks accumulate for as long as the library churns.

Four of the husks were older debris still: `Expiring Movies`, `Expiring
Episodes`, `QFlix Movies-60d` and `QFlix TV-60d` are Maintainerr's naming, and
Maintainerr was purged 2026-06-26. They outlived the app that made them by two
months because a decommission checklist covers apps, units and nginx fragments
-- and had nothing to say about objects the app had created inside Plex.

WHAT THIS FILE ENFORCES, AND WHAT IT DELIBERATELY DOES NOT
----------------------------------------------------------
`collectionMode = 0` ("Hide collections but show their items") on every
member-visible section. Operator decision 2026-08-25, chosen over mode 1 and 2:
members get every film, and no collection tiles at all. It is the display half.

It does NOT touch `autoCollectionThreshold`. That is deliberate and worth
stating, because leaving a knob alone always looks like an oversight later: a
genuine 3-film franchise collection is not a false positive, and with display
hidden it costs a member nothing either way. Silencing creation as well would
also silently disarm the reaper's prune, which needs husks to exist in order to
be observed removing them.

The DATA half lives elsewhere, one concern per module (operator design law):
  * qflix-reaper.py::prune_empty_collections()  -- husks the reaper itself
    creates, pruned per library it actually deleted from, in the same run.
  * qflix-poster-janitor.py                     -- husks from any OTHER cause
    (decommissioned apps, hand-made collections emptied by hand), swept on its
    own daily cadence, because the reaper cannot see what it did not cause.

WHY A CONFIGURE SCRIPT AND NOT A ONE-OFF CURL
---------------------------------------------
The live setting was flipped by hand on 2026-08-25 and would have drifted back
the first time anyone restored a section, re-added a library, or reinstalled.
A live change nothing re-asserts is exactly the shape that let
fix-release-posters.py run zero times in fifteen days. Same lesson, one layer
over: if it matters, something must state it declaratively and re-state it.

Run on the seedbox (or over SSH from the repo):
    python3 scripts/configure/63-plex-collection-display.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# Plex on the Ultra.cc slot binds loopback on the panel port, NOT 32400 -- see
# 59-plex-anime-libraries.py for why the docker IP is wrong here.
PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:17025")

# Every member-visible section, INCLUDING Welcome. Welcome is out of scope for
# the poster janitor (its art is a deliberate custom frame grab) but there is no
# such argument here: a collection tile in the entitlement floor would advertise
# content to exactly the people who cannot play it.
SECTION_NAMES = ("QFlix - Movies", "QFlix - Anime Movies", "QFlix - TV",
                 "QFlix - Anime", "QFlix - Welcome")

# 0 = "Hide collections but show their items". Operator decision 2026-08-25.
#   1 = hide the ITEMS that are in collections -- would hide real films.
#   2 = show both -- the state that produced the defect.
COLLECTION_MODE = "0"

TIMEOUT = 15


def secret(name: str) -> str:
    with open(os.path.expanduser("~/secrets/" + name)) as f:
        return f.read().strip()


def _req(path: str, token: str, method: str = "GET"):
    """(status, body). Never raises -- mirrors qflix-reaper.py's Plex helpers."""
    sep = "&" if "?" in path else "?"
    url = PLEX_URL + path + sep + "X-Plex-Token=" + urllib.parse.quote(token)
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="ignore")[:400]
    except (urllib.error.URLError, socket.timeout) as exc:
        return 0, str(exc)
    except Exception as exc:                      # noqa: BLE001
        return 0, str(exc)


def sections(token: str) -> dict:
    """{title: key} for every section on the server."""
    status, body = _req("/library/sections", token)
    if status != 200:
        raise RuntimeError("GET /library/sections -> " + str(status) + " " + body[:120])
    return {el.get("title") or "": el.get("key") or "" for el in ET.fromstring(body)}


def current_mode(token: str, key: str):
    """The live collectionMode for a section, or None if the pref is absent.

    None is NOT treated as 0. An absent pref means this Plex version does not
    expose the setting, and writing blind would be a guess -- report and skip.
    """
    status, body = _req("/library/sections/" + key + "/prefs", token)
    if status != 200:
        return None
    for s in ET.fromstring(body).findall("Setting"):
        if s.get("id") == "collectionMode":
            return s.get("value")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change and write nothing")
    args = ap.parse_args()

    token = secret("plex.token")
    live = sections(token)

    changed = noop = skipped = failed = 0
    for name in SECTION_NAMES:
        key = live.get(name)
        if not key:
            print("  SKIP    %-22s not on this server" % name)
            skipped += 1
            continue
        mode = current_mode(token, key)
        if mode is None:
            print("  SKIP    %-22s no collectionMode pref exposed" % name)
            skipped += 1
            continue
        if mode == COLLECTION_MODE:
            print("  NO-OP   %-22s collectionMode=%s" % (name, mode))
            noop += 1
            continue
        if args.dry_run:
            print("  WOULD   %-22s collectionMode %s -> %s"
                  % (name, mode, COLLECTION_MODE))
            changed += 1
            continue
        status, body = _req(
            "/library/sections/" + key + "/prefs?collectionMode=" + COLLECTION_MODE,
            token, method="PUT")
        if status not in (200, 201, 204):
            print("  FAIL    %-22s PUT -> HTTP %s %s" % (name, status, body[:80]))
            failed += 1
            continue
        # Re-read rather than trust the 200. Plex answers 200 to a prefs PUT it
        # silently ignored; the only proof a setting took is reading it back.
        after = current_mode(token, key)
        if after != COLLECTION_MODE:
            print("  FAIL    %-22s PUT returned %s but value is still %s"
                  % (name, status, after))
            failed += 1
            continue
        print("  SET     %-22s collectionMode %s -> %s" % (name, mode, after))
        changed += 1

    print()
    print("Policy: collectionMode=%s (hide collections, show their items) on %d "
          "member-visible section(s)" % (COLLECTION_MODE, len(SECTION_NAMES)))
    print("changed=%d no-op=%d skipped=%d failed=%d%s"
          % (changed, noop, skipped, failed, "  (dry-run)" if args.dry_run else ""))
    # Every section skipped means nothing was asserted -- never let that read as
    # success (the reaper/torrent-janitor "skipped everything" rule).
    if skipped == len(SECTION_NAMES):
        print("ERROR: every section was skipped - nothing was enforced",
              file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
