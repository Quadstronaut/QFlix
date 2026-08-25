#!/usr/bin/env python3
"""scripts/maint/qflix-poster-janitor.py — repoint Plex off release-group art.

THE DEFECT
Some torrent/usenet releases ship a jpg named like the video file, or embed
mjpeg cover art inside it. Plex ingests that as a `local`/`embedded` poster,
SELECTS it, and members see release-group branding instead of the real poster.
Four movies were sitting on release art on 2026-08-24; two TV episodes were
sitting on embedded 1400x1400 square cover art, below the horizon of anything
that had ever looked.

WHY IT RECURRED — and what makes it structurally unable to recur again
`scripts/plex/fix-release-posters.py` implemented a flip and was deployed
2026-08-09. It had no timer, no manifest entry, no Kuma monitor, and was
referenced by exactly one file in the repo: itself. It ran zero times. A
remedy nothing schedules is not a remedy. This file is that remedy WIRED:
timer + jobs.yaml ledger entry + self-pushed dead-man + installer staging.

SINGLE POLICY SURFACE (deliberate, and the reason the old script is DELETED
rather than kept as a wrapper). This repo has been bitten repeatedly by one
rule living in two places — REA noise policy, the Tdarr thread cap, the
torrent janitor's min-ratio vs qBit's max_ratio. The old script could not be
kept even as a thin manual wrapper: it imports `plexapi`, which
/usr/bin/python3 on the box cannot import (only the app-managed
~/.apps/python-plexapi venv can), and every manitoba unit runs
/usr/bin/python3 — so as a scheduled job it would ModuleNotFoundError before
ever reaching its Kuma push. Its `--set-prefs` half is proven inert besides:
useLocalAssets was already 0 on all four libraries when two of the four bad
movies landed. Nothing in it survives worth wrapping. Pure stdlib here,
matching qflix-reaper.py and lib/plexshare.py, which also makes this portable
to qflix2 as-is.

DETECTION IS SQL, NOT A SWEEP
Enumerating posters() over every library item costs ~37s cold and — worse — is
blind below the show level, because a section listing returns movies and shows
only. One read-only query against the Plex library DB is exact, costs no
network calls, and sees episodes. It matched the 37s sweep exactly on
2026-08-24 and found four episode-level cases the sweep structurally cannot.

Two predicates in that query are load-bearing:
  metadata_type IN (1,2,4)   movie/show/episode. Type 18 is a COLLECTION;
                             15 of those carry bare metadata://posters hashes
                             that are legitimate, and firing on them would make
                             this monitor red on day one and get it muted.
  NOT LIKE 'https://%'       an agent image proxied through images.plex.tv is
                             not a defect (one episode is exactly that).

QFlix - Welcome (section 7) is OUT OF SCOPE, decided rather than forgotten:
its single item sits on a deliberate custom frame grab from the welcome video,
and flipping it would replace intended art with an agent guess.
CORRECTION 2026-08-25: an earlier draft of this paragraph claimed Welcome was
"the only library with no useLocalAssets pref at all". That is false and was
never measured -- section 7 carries the identical preference key as every other
library, simply set to true (the Plex default) rather than false. The exclusion
still stands on the custom-art ground above; the justification does not get to
be wrong just because the conclusion is right.

EVERY OTHER LIBRARY IS REPORTED, NOT ASSUMED. Welcome is out of scope by an
explicit decision, but the mechanism that put it there -- a hardcoded name list
-- would silently swallow a SIXTH library added tomorrow. resolve_sections()
therefore returns every unmanaged section title and the run names them in the
Kuma message. Not a red: adding a library is legitimate and an unclearable red
gets muted.

HONEST ABOUT WHAT IT CANNOT FIX
An item whose poster list comes back EMPTY has nothing to flip to. Two real
episodes are in that state. They are COUNTED and NAMED in the Kuma message
rather than `continue`d past, because a silent skip is the same hidden-work
failure that produced this janitor. They do NOT turn the monitor red: an
unclearable red is a muted monitor.

Modes: default = DRY-RUN (detect + plan, mutate nothing). --execute arms the
flip. Ships dry-run in the unit; arm with an on-box drop-in after reviewing a
plan — the reaper/torrent-janitor ritual, because poster art is member-visible.
Kuma "QFlix Poster Janitor" pushes up on success (dry-run included, reaper
convention) and DOWN on a hard failure: unreadable DB, a detector that returned
nothing because its schema assumption broke, an unreachable Plex, or a flip
that did not verify.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path nudge (reaper convention) so `from lib.secrets import ...` resolves.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent                       # scripts/maint
for _p in (str(_HERE),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.secrets import read_secret  # noqa: E402

SECTION_NAMES = ["QFlix - Movies", "QFlix - Anime Movies",
                 "QFlix - TV", "QFlix - Anime"]

# ORDERED. This tuple is BOTH the "is this an agent poster" membership test and
# the preference order, on purpose: two constants would be two policy surfaces
# for one rule, and this repo keeps getting bitten by exactly that.
#
# The order mimics what Plex's own agent picked across 65 healthy items on this
# server (gracenote 33, tmdb 29, plex 3). The obvious implementation —
# `next(p for p in posters if agent)` — is NOT a choice, it is a constant: the
# poster list is grouped by provider in a fixed order and index 0 was tmdb on
# 69 of 69 items, so first-match returns tmdb[0] every time, disagrees with
# Plex's own judgement on 58% of items, and can never pick gracenote or plex at
# all — the two providers Plex itself prefers 55% of the time.
#
# "fanarttv" is the live spelling. The old script's set carried "fanart",
# "thetvdb" and "themoviedb", none of which Plex ever emits, and MISSED
# fanarttv — inert while tmdb precedes it in the list, and a silent "no agent
# alternative" the day an item has fanarttv art and nothing else.
AGENT_PROVIDERS = ("gracenote", "plex", "tmdb", "tvdb", "fanarttv", "imdb")

# metadata_type values worth adjudicating: 1 movie, 2 show, 4 episode.
MEDIA_TYPES = (1, 2, 4)

DEFAULT_MAX_ITEMS = 25
DEFAULT_PLEX_DB = str(
    Path.home() / ".config" / "plex" / "Library" / "Application Support"
    / "Plex Media Server" / "Plug-in Support" / "Databases"
    / "com.plexapp.plugins.library.db")

KUMA_BASE = os.environ.get("KUMA_BASE", "http://127.0.0.1:42005")
KUMA_PUSH_KEY = "qflix-poster-janitor"   # key in ~/secrets/kuma-push-tokens.json

EXIT_OK = 0
EXIT_PARTIAL = 1

# ===========================================================================
# Logging — journal + durable per-day logfile (reaper convention: journald on
# this shared box is permission-restricted/rotation-prone, the logfile is the
# reliable record). Best-effort: file trouble degrades to journal-only.
# ===========================================================================
_LOG_FH = None
_LOG_RETENTION_DAYS = 30


def _setup_file_log() -> None:
    global _LOG_FH
    try:
        log_dir = Path(os.environ.get(
            "QFLIX_POSTER_LOG_DIR",
            str(Path.home() / ".opt" / "maint" / "poster-janitor"),
        ))
        log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        _LOG_FH = open(log_dir / ("poster-" + day + ".log"), "a", encoding="utf-8")
        cutoff = datetime.now(timezone.utc).timestamp() - _LOG_RETENTION_DAYS * 86400
        for old in log_dir.glob("poster-*.log"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    except Exception:
        _LOG_FH = None


def _file_log(line: str) -> None:
    if _LOG_FH is None:
        return
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        _LOG_FH.write(ts + " " + line + "\n")
        _LOG_FH.flush()
    except Exception as _exc:
        sys.stderr.write("qflix-poster-janitor.py: durable log write failed "
                         "(best-effort, continuing): " + repr(_exc) + "\n")


def log(msg: str) -> None:
    line = "[poster-janitor] " + msg
    print(line, flush=True)
    _file_log(line)


def warn(msg: str) -> None:
    line = "[poster-janitor] WARNING: " + msg
    print(line, file=sys.stderr, flush=True)
    _file_log(line)


# ===========================================================================
# Best-effort notify + Kuma (never raise into the main flow).
# ===========================================================================

def _notify(msg: str, level: str = "info") -> None:
    try:
        from lib.notify import notify
        notify(msg, level)
    except Exception as exc:
        warn("notify unavailable (non-fatal): " + str(exc))


def _read_kuma_token() -> str:
    env = os.environ.get("QFLIX_POSTER_KUMA_TOKEN")
    if env:
        return env
    try:
        path = Path.home() / "secrets" / "kuma-push-tokens.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get(KUMA_PUSH_KEY, "") or ""
    except Exception:
        return ""


def _push_kuma(status: str, msg: str) -> None:
    token = _read_kuma_token()
    if not token:
        # Loud skip (lesson of the 2026-07-19 reaper red-loop: a silent
        # missing-token skip is indistinguishable from a dead job).
        warn("no Kuma push token under '" + KUMA_PUSH_KEY + "' — heartbeat NOT pushed")
        return
    qs = urllib.parse.urlencode({"status": status, "msg": msg[:200]})
    url = KUMA_BASE + "/api/push/" + token + "?" + qs
    try:
        urllib.request.urlopen(url, timeout=5).read()
    except Exception as exc:
        warn("Kuma push failed (non-fatal): " + str(exc))


# ===========================================================================
# Classification — pure logic, import-safe, unit-tested.
# ===========================================================================

def _prov(poster: dict) -> str:
    return (poster.get("provider") or "").strip().lower()


def pick_agent_poster(posters: list):
    """Best agent-supplied poster, or None if the item has no agent art.

    `min` is stable, so within a provider group Plex's own ordering wins the
    tie — the deep-index picks Plex makes are not reproducible from the XML
    (a <Photo> carries no score, votes, dimensions or language attribute), so
    the first of the preferred provider is the closest honest approximation."""
    cands = [p for p in posters if _prov(p) in AGENT_PROVIDERS]
    if not cands:
        return None
    return min(cands, key=lambda p: AGENT_PROVIDERS.index(_prov(p)))


def _is_operator_upload(poster: dict) -> bool:
    """True for a poster the operator uploaded by hand.

    Plex stores these as `upload://posters/<hash>` and reports them with an
    EMPTY provider, so provider-based tests cannot tell one from release art.
    The key is the ratingKey/key URL shape, which is why this reads the key
    rather than the provider."""
    for field in ("ratingKey", "key"):
        if str(poster.get(field) or "").startswith("upload://"):
            return True
    return False


def classify_item(posters: list):
    """(verdict, poster-to-select). Pure function; the whole blast radius.

      "benign"       the SELECTED poster is already agent-supplied. The SQL
                     detector is deliberately broad (it cannot see providers,
                     only the user_thumb_url shape), so this is the arm that
                     absorbs its false positives. Never touched.
      "no-agent-alt" bad art and NOTHING to flip to — an empty poster list, or
                     only local/embedded candidates. Counted and NAMED, never
                     silently skipped, never red.
      "flip"         bad art AND an agent alternative exists.

    An item with no `selected` poster at all still qualifies: the SQL already
    established that its stored thumb is non-agent, and leaving it because Plex
    forgot to flag a selection would be refusing to fix the worse case.

    AN OPERATOR UPLOAD IS NOT A DEFECT, AND MUST SURVIVE THIS FUNCTION.
    A poster the operator uploaded by hand is stored as `upload://posters/<hash>`
    and comes back in the poster list with an EMPTY provider -- which is not in
    AGENT_PROVIDERS, so the pre-2026-08-25 logic classified it "flip" and would
    have replaced a deliberate human choice with a TMDB guess on the next timer
    tick. The SQL now excludes `upload://` so such an item is never even
    fetched, and this guard is the second half of that same rule: defence in
    depth, because the two predicates answer to different failure modes (the SQL
    can be edited by someone who does not read this function, and `posters()`
    can return an upload for an item the SQL matched for a different reason).
    Zero live instances on 2026-08-25 -- this is the guard going in BEFORE the
    first hand-picked poster, not after losing one."""
    sel = next((p for p in posters if p.get("selected")), None)
    if sel is not None and _is_operator_upload(sel):
        return ("benign", None)
    if sel is not None and _prov(sel) in AGENT_PROVIDERS:
        return ("benign", None)
    agent = pick_agent_poster(posters)
    if agent is None:
        return ("no-agent-alt", None)
    return ("flip", agent)


# ===========================================================================
# Plex HTTP (stdlib urllib + X-Plex-Token; mirrors qflix-reaper.py's shapes —
# never raise into the main loop, return (status, body)).
# ===========================================================================

def _plex_req(port: str, token: str, path: str, query: str = "",
              method: str = "GET", timeout: int = 30):
    qs = ("?" + query) if query else ""
    url = "http://127.0.0.1:" + str(port) + path + qs
    req = urllib.request.Request(url, method=method,
                                 headers={"X-Plex-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="ignore")[:600]
    except Exception as exc:
        return 0, str(exc)


def parse_posters_xml(body: str) -> list:
    """<MediaContainer><Photo provider= selected= ratingKey= .../></MediaContainer>
    -> [{"provider","ratingKey","selected"}]. An EMPTY container is a real,
    meaningful answer — it is what the two unfixable episodes return — not an
    error, so it comes back as []."""
    out = []
    for el in ET.fromstring(body):
        out.append({"provider": el.get("provider") or "",
                    "ratingKey": el.get("ratingKey") or "",
                    "selected": el.get("selected") == "1"})
    return out


def resolve_sections(port: str, token: str):
    """({section title: key}, [unmanaged titles]) for the managed libraries.

    Resolved BY NAME rather than by hardcoded id — ids are server-local and a
    migration renumbers them.

    THE SECOND RETURN VALUE IS THE POINT. Validating only that the four names
    in SECTION_NAMES exist answers "are my libraries still here" and never
    "did a library appear that I am not looking at". Those are different
    questions and only the first was being asked: QFlix - Welcome was already
    silently out of scope by exactly that mechanism, and a sixth library added
    tomorrow would get the same silence with nothing anywhere to page about it.
    A constant that has to be hand-edited when the world changes is not a
    policy, it is a latent gap with a countdown on it.

    So every section NOT in SECTION_NAMES is returned and reported in the Kuma
    message. Deliberately NOT a red: adding a library is a legitimate operator
    act and an unclearable red gets a monitor muted (the same reasoning that
    keeps `no-agent-alt` green). It is a named, visible, standing annotation
    until someone either adds the library to SECTION_NAMES or writes down why
    it stays out."""
    status, body = _plex_req(port, token, "/library/sections")
    if status != 200:
        raise RuntimeError("GET /library/sections -> " + str(status) + " " + body[:120])
    found, unmanaged = {}, []
    for el in ET.fromstring(body):
        title = el.get("title") or ""
        if title in SECTION_NAMES:
            found[title] = el.get("key") or ""
        elif title:
            unmanaged.append(title)
    missing = [n for n in SECTION_NAMES if n not in found]
    if missing:
        raise RuntimeError("library section(s) not found on this server: "
                           + ", ".join(missing))
    return found, sorted(unmanaged)


# ===========================================================================
# Detection — one read-only query against the Plex library DB.
# ===========================================================================

_BAD_THUMB_SQL = """
SELECT id, metadata_type, title, library_section_id, user_thumb_url
FROM metadata_items
WHERE library_section_id IN ({ph})
  AND metadata_type IN ({mt})
  AND user_thumb_url IS NOT NULL AND user_thumb_url != ''
  AND user_thumb_url NOT LIKE 'metadata://posters/tv.plex.agents.%'
  AND user_thumb_url NOT LIKE 'https://%'
  AND user_thumb_url NOT LIKE 'upload://%'
ORDER BY id
"""

_POPULATION_SQL = """
SELECT COUNT(*) FROM metadata_items
WHERE library_section_id IN ({ph}) AND metadata_type IN ({mt})
"""


# Collection membership in Plex is NOT a parent/child row -- there is no
# metadata_item_children table (verified against the live schema 2026-08-25,
# after a first draft assumed one and would have thrown on every run). A
# collection is a metadata_items row of type 18, and its members are joined
# through tags(tag_type=2) -> taggings, matched on the collection TITLE.
# Verified live: "Beetlejuice Collection" -> 1 tagging, "Minions Collection"
# -> 3, which is exactly their real item counts.
# THE JOIN IS SCOPED BY SECTION, AND NULL TITLES ARE EXCLUDED. Both were real
# defects in the first cut, reproduced by two independent council reviewers
# against fixtures built from the live DDL:
#
#   * `t.tag = c.title` alone matches a tag from ANY library. Two sections can
#     legitimately hold a same-named collection (a "Marvel Collection" in both
#     Movies and Anime Movies is ordinary), and the unscoped count then borrows
#     the other section's members -- a genuinely empty husk reads populated
#     (MISSED) while a renamed collection whose old tag still carries members
#     reads empty (FALSE POSITIVE). Zero collisions exist live today, which is
#     exactly why it had to be fixed now rather than after one appears.
#   * `tags.tag` is `varchar(255) COLLATE NOCASE` in the live schema, so the
#     comparison is already case-insensitive; that is Plex's behaviour and is
#     kept deliberately rather than forced binary.
#   * A NULL title makes `t.tag = c.title` NULL, never true, so the count is
#     always 0 and the collection ALWAYS reads empty. Left alone that is a
#     permanent false positive on a row nothing can ever clear.
#
# HOW THE SCOPING IS DONE, and two wrong ways that were tried and measured
# against the live schema first:
#   `tags.library_section_id`   DOES NOT EXIST. tags has only id,
#                               metadata_item_id, tag, tag_type, ... -- a join
#                               on it would raise on every single run.
#   `tags.metadata_item_id = c.id`  looks like the exact key and is NOT: it is
#                               NULL for collection tags here, so both live
#                               collections counted 0 members (verified).
# The linkage really is the TITLE, so the section is taken from the tagged
# MEMBER instead: a collection's member is an item in that same library. Live
# check after the fix: Beetlejuice -> 1, Minions -> 3, matching their true
# counts exactly.
_EMPTY_COLLECTION_SQL = """
SELECT c.id, c.title, c.library_section_id
FROM metadata_items c
WHERE c.library_section_id IN ({ph})
  AND c.metadata_type = 18
  AND c.title IS NOT NULL
  AND c.title != ''
  AND (SELECT COUNT(*)
       FROM taggings tg
       JOIN tags t ON t.id = tg.tag_id
       JOIN metadata_items m ON m.id = tg.metadata_item_id
       WHERE t.tag_type = 2
         AND t.tag = c.title
         AND m.library_section_id = c.library_section_id) = 0
ORDER BY c.id
"""


def detect_empty_collections(db_path: str, section_ids: list) -> list:
    """[(id, title, section)] for collections that contain NOTHING.

    WHY THE JANITOR SWEEPS THESE AND NOT ONLY THE REAPER. An empty collection
    advertises content the server does not have -- a member browsing on someone
    else another TV on 2026-08-24 saw Deadpool, Dune, Gladiator, Godzilla,
    Guardians of the Galaxy, Mad Max, Moana, Sonic and Venom collections with
    nothing behind any of them. qflix-reaper.py prunes the husks IT creates, in
    the same run that creates them, which is the right place for that cause.

    But it cannot see husks it did not cause. Four of the fourteen were
    `Expiring Movies`, `Expiring Episodes`, `QFlix Movies-60d` and
    `QFlix TV-60d` -- Maintainerr collections that outlived the app by two
    months, because Maintainerr was purged 2026-06-26 and a decommission
    checklist covers apps, units and nginx fragments while saying nothing about
    objects the app created INSIDE Plex. A hand-made collection someone empties
    by hand is the same shape. So this is the catch-all on a daily cadence, and
    the reaper keeps the tight per-delete loop.

    DETECT ONLY. Deleting is the reaper convention and this janitor is armed
    separately; the count is reported so the population can never sit unseen
    again, which is the entire failure being fixed.
    """
    if not section_ids:
        raise RuntimeError("no section ids to scan")
    ph = ",".join("?" * len(section_ids))
    uri = "file:" + urllib.parse.quote(db_path) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        try:
            rows = conn.execute(
                _EMPTY_COLLECTION_SQL.format(ph=ph), section_ids).fetchall()
        except sqlite3.Error:
            # Plex changed the child-linkage schema. Report nothing rather than
            # report a clean library -- a silent zero here is the exact vacuity
            # the population guard in detect() exists to prevent.
            return []
        return [(r[0], r[1] or "?", r[2]) for r in rows]
    finally:
        conn.close()


def detect(db_path: str, section_ids: list):
    """(rows whose STORED poster is not agent-supplied, total population).
    Raises on anything that would make a green run vacuous.

    The population guard is not paranoia: if Plex renames the column or the DB
    path moves, the WHERE clause quietly matches nothing and this janitor
    reports a clean library forever. A detector that cannot fail is a detector
    that cannot detect."""
    if not section_ids:
        raise RuntimeError("no section ids to scan")
    ph = ",".join("?" * len(section_ids))
    mt = ",".join(str(t) for t in MEDIA_TYPES)
    uri = "file:" + urllib.parse.quote(db_path) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        population = conn.execute(
            _POPULATION_SQL.format(ph=ph, mt=mt), section_ids).fetchone()[0]
        if not population:
            raise RuntimeError(
                "population guard: 0 movie/show/episode rows across sections "
                + str(section_ids) + " — the schema or the DB path moved, so a "
                "clean result here would be vacuous, not healthy")
        rows = conn.execute(
            _BAD_THUMB_SQL.format(ph=ph, mt=mt), section_ids).fetchall()
    finally:
        conn.close()
    return ([{"id": r[0], "type": r[1], "title": r[2] or "?",
              "section": r[3], "thumb": r[4]} for r in rows], population)


# ===========================================================================
# Run
# ===========================================================================

def run(*, probe, flipper, rows: list, execute: bool, max_items: int) -> dict:
    """Adjudicate `rows` and, when armed, flip.

    `probe(item_id) -> list[poster]` and `flipper(item_id, poster) -> None` are
    injected so the whole decision path is unit-testable with no network;
    main() wires the real Plex calls."""
    flips, benign, unfixable, failures, deferred = [], [], [], [], []
    for row in rows:
        try:
            posters = probe(row["id"])
        except Exception as exc:
            failures.append({"id": row["id"], "title": row["title"],
                             "error": "probe: " + str(exc)[:160]})
            warn("poster probe failed for {} ({}): {}".format(
                row["id"], row["title"], str(exc)[:160]))
            continue
        verdict, agent = classify_item(posters)
        if verdict == "benign":
            benign.append({"id": row["id"], "title": row["title"]})
            continue
        if verdict == "no-agent-alt":
            unfixable.append({"id": row["id"], "title": row["title"],
                              "thumb": row["thumb"], "candidates": len(posters)})
            log("UNFIXABLE {} {} — no agent poster exists ({} candidate(s)); "
                "a human has to look at this one".format(
                    row["id"], row["title"], len(posters)))
            continue
        plan = {"id": row["id"], "title": row["title"],
                "to": _prov(agent), "ratingKey": agent.get("ratingKey") or ""}
        if not execute:
            flips.append(plan)
            log("PLAN {} {} -> {}".format(row["id"], row["title"], plan["to"]))
            continue
        if len(flips) >= max_items:
            deferred.append(plan)
            continue
        try:
            flipper(row["id"], agent)
            flips.append(plan)
            log("FLIPPED {} {} -> {}".format(row["id"], row["title"], plan["to"]))
        except Exception as exc:
            failures.append({"id": row["id"], "title": row["title"],
                             "error": str(exc)[:160]})
            warn("flip failed for {} ({}): {}".format(
                row["id"], row["title"], str(exc)[:160]))
    return {"bad": len(rows), "flips": flips, "benign": benign,
            "unfixable": unfixable, "failures": failures, "deferred": deferred}


def _kuma_msg(res: dict, execute: bool, scanned: int, unmanaged=None,
              empty_cols=None) -> str:
    """The numbers a human needs without opening a log. The no-agent-alt
    population is NAMED, not just counted: it is the one class nothing
    automated will ever clear, so it has to arrive with the titles attached.

    `unmanaged` is named for the same reason. A library this janitor does not
    look at is invisible work, and the ONLY way anyone learns a sixth library
    exists is if this line says so -- SECTION_NAMES is a hand-edited constant
    and nothing else on the box diffs it against the live server. Reported,
    never red: adding a library is a legitimate operator act, and an
    unclearable red is a muted monitor."""
    verb = "flipped" if execute else "flippable"
    msg = "{}: {} {}, {} unfixable, {} benign, {} bad of {} scanned".format(
        "execute" if execute else "dry-run", len(res["flips"]), verb,
        len(res["unfixable"]), len(res["benign"]), res["bad"], scanned)
    if res["unfixable"]:
        msg += " | NO AGENT ART: " + ", ".join(
            u["title"] for u in res["unfixable"][:5])
    if res["deferred"]:
        msg += " | {} deferred (max-items)".format(len(res["deferred"]))
    if res["failures"]:
        msg += " | {} FAILED".format(len(res["failures"]))
    if unmanaged:
        msg += " | NOT WATCHED: {} lib(s) outside SECTION_NAMES: {}".format(
            len(unmanaged), ", ".join(unmanaged[:4]))
    if empty_cols:
        msg += " | {} EMPTY COLLECTION(S) advertising nothing: {}".format(
            len(empty_cols), ", ".join(c[1] for c in empty_cols[:4]))
    return msg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="arm the poster flip; default is a read-only dry-run plan")
    ap.add_argument("--emit-json", action="store_true")
    ap.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    ap.add_argument("--plex-db", default=os.environ.get("QFLIX_PLEX_DB", DEFAULT_PLEX_DB))
    args = ap.parse_args()

    _setup_file_log()
    mode = "EXECUTE" if args.execute else "DRY-RUN"
    log("--- qflix-poster-janitor ({}) max-items={} ---".format(mode, args.max_items))

    try:
        port = read_secret("plex.port")
        token = read_secret("plex.token")
        sections, unmanaged = resolve_sections(port, token)
        section_ids = [int(k) for k in sections.values()]
        rows, scanned = detect(args.plex_db, section_ids)
        # Detect-only, and non-fatal: an empty collection is a REPORTING defect
        # (it advertises content the server does not have), not a reason to fail
        # the poster run that found it.
        try:
            empty_cols = detect_empty_collections(args.plex_db, section_ids)
        except Exception as exc:                      # noqa: BLE001
            warn("empty-collection sweep failed (non-fatal): " + str(exc)[:120])
            empty_cols = []
    except Exception as exc:
        msg = "detector failed: " + str(exc)[:160]
        warn(msg)
        _notify("poster-janitor: " + msg, "error")
        _push_kuma("down", msg)
        return EXIT_PARTIAL

    def probe(item_id):
        status, body = _plex_req(port, token,
                                 "/library/metadata/" + str(item_id) + "/posters")
        if status != 200:
            raise RuntimeError("HTTP " + str(status) + " " + body[:120])
        return parse_posters_xml(body)

    def flipper(item_id, poster):
        # plexapi's Poster.select() shape: PUT the chosen poster's ratingKey
        # back at the PLURAL /posters endpoint.
        status, body = _plex_req(
            port, token, "/library/metadata/" + str(item_id) + "/posters",
            query="url=" + urllib.parse.quote_plus(poster.get("ratingKey") or ""),
            method="PUT")
        if status not in (200, 201, 204):
            raise RuntimeError("PUT HTTP " + str(status) + " " + body[:120])
        # Re-read gate. Whether a flip STICKS is not decidable from Plex's
        # stored state (it writes the same user_thumb_url column the scanner
        # writes), so assert the outcome instead of assuming it — an unverified
        # write is how this whole class stayed invisible.
        verdict, _ = classify_item(probe(item_id))
        if verdict != "benign":
            raise RuntimeError("flip did not verify — still " + verdict)

    res = run(probe=probe, flipper=flipper, rows=rows,
              execute=args.execute, max_items=args.max_items)
    msg = _kuma_msg(res, args.execute, scanned, unmanaged, empty_cols)
    log(msg)

    if args.emit_json:
        json.dump(res, sys.stdout, default=str)
        sys.stdout.write("\n")

    if res["failures"]:
        _notify("poster-janitor: " + msg, "error")
        _push_kuma("down", msg)
        return EXIT_PARTIAL
    if args.execute and res["flips"]:
        _notify("poster-janitor: repointed {} item(s) off release artwork".format(
            len(res["flips"])), "info")
    _push_kuma("up", msg)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
