#!/usr/bin/env bash
# plex-unmatched canary: detect Plex library items that never got matched to a
# metadata agent and are stuck on a `local://` guid — the member-visible defect
# where an episode shows up with NO synopsis, NO agent artwork (just a
# frame-grab thumbnail) and NO air date.
#
# WHY THIS EXISTS
# On 2026-08-03 a read-only audit found 30 episodes carrying `guid` values of
# the form `local://NNNN` — 29 in the TV section, 1 in Anime. Three complete
# first seasons (What We Do in the Shadows S1, Squid Game S1, Monster (2022)
# S1) plus one stray anime file. Nothing in this repo looks at Plex match
# state: `git grep -nE "local://|unmatched|fix.?match|guid" -- scripts/ tests/
# manifest/` returned zero probes. The nearest neighbour, qflix-reaper's
# "orphan" concept, means something else entirely (a Plex item with no backing
# *arr record, inside the DELETE path) and keys off external tmdb/tvdb ids —
# it would never see these, and all 30 DO have complete Sonarr records.
#
# TWO ROOT CAUSES produce the same signal, which is why this detects rather
# than remediates:
#   (A) Plex episode-match race — the scanner created episode rows ~3s BEFORE
#       the show/season got their remote match, and Plex never revisited the
#       stubs. Sibling seasons of the SAME series matched fine. NOT
#       self-healing: all 29 were refreshed on 2026-07-27, 15 days after add,
#       and are still local://.
#   (B) Sonarr misconfiguration — sonarr2 has renameEpisodes=False and one
#       series has seasonFolder=False, so `[MTBB] ... S2 - 13 ...mkv` files
#       land in the series root, Plex reads the trailing "- NN" as an episode
#       number and files them under Season 1.
#
# DETECTION ROUTE: HTTP, not sqlite. Every episode returned by
# `GET /library/sections/{id}/all?type=4` with `Accept: application/json`
# carries its `guid` verbatim, plus grandparentTitle / parentIndex / index /
# addedAt — enough to name AND age every finding in one round trip. Measured
# live: 963,946 bytes in 0.122 s for a 382-episode section. The rejected
# alternative was copying Plex's 19 MB library DB per run and juggling its
# -wal; strictly worse for identical signal.
#
# The `excludeElements=...&excludeFields=summary,tagline` variant was ALSO
# rejected. It works today (verified: still returns all 29 guids, saves
# ~340 KB) but field-stripping semantics are exactly the kind of thing a Plex
# update changes silently — and a silent change there would zero the signal
# while this canary kept exiting 0. Pay the 340 KB.
#
# SECTIONS ARE DISCOVERED, NOT HARDCODED. Only `type: show` libraries can
# produce this (movie sections measured 0/42 and 0/3), but a hardcoded id list
# means the next TV library anyone adds is silently unmonitored. Non-show
# sections are skipped, COUNTED and NAMED in the output — never dropped in
# silence.
#
# THRESHOLD, and why it is a count with a grace window rather than a ratio:
#   A freshly imported episode is legitimately local:// for the seconds-to-
#   minutes between the scan creating the row and the agent match landing. A
#   raw `count > 0` alert would therefore flap on every Sonarr import. Every
#   genuinely stuck item found in the audit was 76.4 h to 526.9 h old, so a 6 h
#   grace separates signal from scan-lag by two orders of magnitude while still
#   catching a stuck season the same day. Same shape as qflix-reaper's existing
#   orphan-grace (docs/superpowers/specs/2026-07-14-reaper-orphan-grace-design.md).
#   A PERCENTAGE floor was rejected outright: 30/445 is 6.7% overall, but the
#   Anime section is 1/63 = 1.6%. Any percentage low enough to catch that is
#   low enough to be noise, and any percentage that isn't noise hides a whole
#   stuck season in a big library.
#
# Tunables (systemd `Environment=` or env):
#   PLEX_UNMATCHED_GRACE_HOURS   default 6    below this an item is suppressed
#                                             (and COUNTED — see rule 4)
#   PLEX_UNMATCHED_MAX_AGED      default 0    fail when aged items EXCEED this
#   PLEX_UNMATCHED_PAGE_SIZE     default 500  X-Plex-Container-Size per page
#   PLEX_UNMATCHED_TIMEOUT       default 25   seconds per HTTP call
#   PLEX_UNMATCHED_NOW           test-only clock override (epoch seconds)
#
# Flags:
#   --json   emit the full machine-readable report on stdout (dashboard /
#            newsletter consumers) instead of the one-line summary. Exit codes
#            are identical either way.
#
# Stage labels (failure messages on stderr -> Kuma msg=):
#   plex-unmatched-config-missing  plex.host/port/token unreadable  (BROKEN)
#   plex-sections-unreachable      GET /library/sections failed     (BROKEN)
#   plex-no-show-sections          Plex returned zero show libraries(BROKEN)
#   plex-section-unreachable       a section listing failed         (BROKEN)
#   plex-section-truncated         section declared N items, fewer  (BROKEN)
#                                  arrived — an empty Metadata on a
#                                  totalSize>0 section is BROKEN, not clean
#   plex-no-episodes               every show section returned ZERO  (BROKEN)
#                                  episodes — see the denominator note
#   plex-unmatched-stuck           aged local:// items found        (RED)
#
# Exits — empty-because-clean is distinguishable from empty-because-broken:
#   0 — queried every show section successfully, zero AGED local:// items
#       (any under-grace items are reported by count, not hidden)
#   1 — aged local:// items exceed PLEX_UNMATCHED_MAX_AGED (real finding)
#   2 — could not query Plex / config missing / a section read was truncated
#       (the canary asserted NOTHING; do not read this as "library is fine")
#
# This canary does NOT remediate, by design. A metadata refresh demonstrably
# does not fix these (proved: refreshed 2026-07-27, still local://) and
# `GET /library/metadata/{rk}/matches` returns 0 candidates at episode level.
# The only remedy that works is an unmatch+rematch at SERIES level, which
# recreates every episode row and discards ratingKeys, watch state and
# Tautulli history. That is an operator judgement call, not a timer's.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

EMIT_JSON=0
for arg in "$@"; do
  case "$arg" in
    --json) EMIT_JSON=1 ;;
    *) printf "usage: %s [--json]\n" "$(basename "$0")" >&2; exit 2 ;;
  esac
done

# The remote body is built in a QUOTED heredoc so nothing in it is expanded
# locally (the embedded python is full of $ and backslashes), then handed to
# sshm prefixed with the one value the caller can vary. Env does not propagate
# over ssh, so --json has to travel as text, not as an exported variable.
REMOTE=$(cat <<'REMOTE_EOF'
set -uo pipefail
SECRETS=~/secrets
PLEX_HOST=$(cat "$SECRETS/plex.host" 2>/dev/null)
PLEX_PORT=$(cat "$SECRETS/plex.port" 2>/dev/null)
PLEX_TOKEN=$(cat "$SECRETS/plex.token" 2>/dev/null)

# A missing credential exits 2 (BROKEN), never 0. The C-09 silent-exit class:
# a canary that cannot find its secret and exits clean shows Kuma a green push
# it never earned, and the check is dead for weeks before anyone notices.
if [ -z "$PLEX_HOST" ] || [ -z "$PLEX_PORT" ] || [ -z "$PLEX_TOKEN" ]; then
  printf "STAGE=plex-unmatched-config-missing msg=host=%s-port=%s-token=%s\n" \
    "${PLEX_HOST:+SET}" "${PLEX_PORT:+SET}" "${PLEX_TOKEN:+SET}" >&2
  exit 2
fi

export PLEX_BASE="http://${PLEX_HOST}:${PLEX_PORT}"
export PLEX_TOKEN
python3 <<"PYEND"
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = (os.environ.get("PLEX_BASE") or "").rstrip("/")
TOKEN = os.environ.get("PLEX_TOKEN") or ""
GRACE_HOURS = float(os.environ.get("PLEX_UNMATCHED_GRACE_HOURS") or 6)
MAX_AGED = int(os.environ.get("PLEX_UNMATCHED_MAX_AGED") or 0)
PAGE_SIZE = int(os.environ.get("PLEX_UNMATCHED_PAGE_SIZE") or 500)
TIMEOUT = float(os.environ.get("PLEX_UNMATCHED_TIMEOUT") or 25)
EMIT_JSON = (os.environ.get("PLEX_UNMATCHED_JSON") or "0") == "1"
NOW = float(os.environ.get("PLEX_UNMATCHED_NOW") or time.time())
# A section needing more than this many pages is a runaway, not a library.
MAX_PAGES = int(os.environ.get("PLEX_UNMATCHED_MAX_PAGES") or 200)

LOCAL_PREFIX = "local://"


def _short(value, limit=60):
    """One-line, bounded. Everything here ends up in a Kuma msg= param."""
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    return text[:limit]


def _broken(stage, msg):
    """Exit 2 — the canary asserted NOTHING. Distinct from a clean exit
    (asserted, nothing stuck) and exit 1 (asserted, found something). Rule:
    empty-because-clean must never look like empty-because-broken.

    Deliberately worded WITHOUT the literal token audit detector C-09 scans
    for: this line is prose, not a clean-exit site, and spelling it out here
    inflated that detector's cross-check by one against its own boundary regex.
    """
    sys.stderr.write("STAGE=%s msg=%s\n" % (stage, _short(msg, 180)))
    sys.exit(2)


def _get(path, params=None, headers=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("X-Plex-Token", TOKEN)
    for key, value in (headers or {}).items():
        req.add_header(key, str(value))
    resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    try:
        body = resp.read()
        code = resp.getcode()
    finally:
        resp.close()
    if code != 200:
        raise RuntimeError("http-%s" % code)
    return json.loads(body.decode("utf-8", "replace"))


def _get_or_broken(path, stage, params=None, headers=None):
    """Never leaks the base URL into stderr — that string carries the host and
    port, and this text lands in Kuma and Discord."""
    try:
        return _get(path, params, headers)
    except urllib.error.HTTPError as exc:
        _broken(stage, "path=%s-http=%s" % (path, exc.code))
    except urllib.error.URLError as exc:
        _broken(stage, "path=%s-unreachable=%s" % (path, _short(exc.reason, 40)))
    except Exception as exc:                      # noqa: BLE001 - report, don't crash
        _broken(stage, "path=%s-error=%s" % (path, _short(exc, 40)))


if not BASE or not TOKEN:
    _broken("plex-unmatched-config-missing", "base=%s-token=%s"
            % ("SET" if BASE else "EMPTY", "SET" if TOKEN else "EMPTY"))

# --- 1. Discover sections -------------------------------------------------
# Only `show` libraries can carry an unmatched EPISODE. Movie sections were
# measured at 0/42 and 0/3 during the audit. Deriving the list is cheaper than
# justifying a hardcoded [2, 6], and it means the next TV library added is
# monitored on its first tick instead of never.
sections_doc = _get_or_broken("/library/sections", "plex-sections-unreachable")
directories = (sections_doc.get("MediaContainer") or {}).get("Directory") or []

show_sections = []
skipped_sections = []
for entry in directories:
    stype = (entry.get("type") or "").lower()
    key = str(entry.get("key") or "")
    title = entry.get("title") or ("section-" + key)
    if stype == "show" and key:
        show_sections.append((key, title))
    else:
        # Rule 4: a skip is counted and named, never silent. If someone later
        # wonders why the movie libraries produce no findings, the answer is
        # in the output rather than in this comment alone.
        skipped_sections.append("%s(%s)" % (title, stype or "?"))

if not show_sections:
    _broken("plex-no-show-sections",
            "directories=%d-skipped=%d" % (len(directories), len(skipped_sections)))


# --- 2. Walk every show section, paginated --------------------------------
def _scan_section(key, title):
    """Return (declared_total, items). Pagination is explicit rather than
    trusting Plex's default page size: a silent server-side cap would make this
    canary read the first N episodes and call the rest clean."""
    collected = []
    declared = None
    pages = 0
    while True:
        pages += 1
        if pages > MAX_PAGES:
            _broken("plex-section-truncated",
                    "section=%s-runaway-pages=%d" % (_short(title, 40), pages))
        doc = _get_or_broken(
            "/library/sections/%s/all" % urllib.parse.quote(str(key)),
            "plex-section-unreachable",
            params={"type": "4"},
            headers={"X-Plex-Container-Start": len(collected),
                     "X-Plex-Container-Size": PAGE_SIZE},
        )
        container = doc.get("MediaContainer") or {}
        items = container.get("Metadata") or []
        if declared is None and "totalSize" in container:
            declared = int(container.get("totalSize") or 0)
        collected.extend(items)
        if declared is not None and len(collected) >= declared:
            break
        # No totalSize (older Plex, or a proxy that strips it): stop on a short
        # page rather than looping forever.
        if len(items) < PAGE_SIZE:
            break
    if declared is None:
        declared = len(collected)
    if declared > 0 and len(collected) < declared:
        # Includes the specific case called out in the design: a section that
        # reports totalSize > 0 and hands back an EMPTY Metadata array. That is
        # broken, not clean, and must not exit 0.
        _broken("plex-section-truncated",
                "section=%s-declared=%d-received=%d"
                % (_short(title, 40), declared, len(collected)))
    return declared, collected


def _describe(item, section_title):
    added = item.get("addedAt")
    try:
        age_hours = None if added is None else round((NOW - float(added)) / 3600.0, 1)
    except (TypeError, ValueError):
        age_hours = None
    return {
        "section": section_title,
        "series": item.get("grandparentTitle") or item.get("parentTitle") or "?",
        "season": item.get("parentIndex"),
        "episode": item.get("index"),
        "title": item.get("title") or "?",
        "ratingKey": item.get("ratingKey"),
        "guid": item.get("guid"),
        "ageHours": age_hours,
        "hasSummary": bool((item.get("summary") or "").strip()),
    }


per_section = []
aged_items = []
under_grace_items = []
total_episodes = 0
total_local = 0

for key, title in show_sections:
    declared, items = _scan_section(key, title)
    section_local = 0
    section_aged = 0
    section_grace = 0
    for item in items:
        guid = item.get("guid") or ""
        if not guid.startswith(LOCAL_PREFIX):
            continue
        section_local += 1
        record = _describe(item, title)
        # An item with no usable addedAt counts as AGED. A grace window that
        # silently swallows items it cannot age is a grace window that hides
        # findings — fail loud, and say why in the record (ageHours=null).
        if record["ageHours"] is None or record["ageHours"] >= GRACE_HOURS:
            section_aged += 1
            aged_items.append(record)
        else:
            section_grace += 1
            under_grace_items.append(record)
    total_episodes += len(items)
    total_local += section_local
    per_section.append({
        "section": title,
        "key": key,
        "episodes": len(items),
        "declared": declared,
        "local": section_local,
        "aged": section_aged,
        "under_grace": section_grace,
    })

# --- 3. THE DENOMINATOR MUST BE REAL --------------------------------------
# The per-section truncation guard is conditioned on `declared > 0`, so a
# section reporting totalSize=0 walks straight past it. That is not a
# hypothetical shape: if the `type=4` episode filter ever stops matching (a
# Plex schema or version change), every show section returns
# {"totalSize":0,"size":0,"Metadata":[]} and this canary exits 0 with
# episodes=0 — asserting nothing while printing "clean". The original mutation
# test used declared=382 and so never exercised the declared=0 path.
#
# "Zero episodes" alone cannot decide it, because a genuinely empty new library
# looks identical. So ASK A SECOND QUESTION, and one that does not depend on
# the thing under suspicion: re-list the section with NO type filter at all. If
# the library holds items but our type=4 query found no episodes, the filter or
# the schema moved and this canary is blind. If the library is empty too, it is
# an empty library and that is a content state, not a fault.
#
# Costs one extra HTTP call per EMPTY section only — zero on a normal run.
for _s in per_section:
    if _s["episodes"] or _s["declared"]:
        continue
    _doc = _get_or_broken(
        "/library/sections/%s/all" % urllib.parse.quote(str(_s["key"])),
        "plex-section-unreachable",
        headers={"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 1},
    )
    _held = int((_doc.get("MediaContainer") or {}).get("totalSize") or 0)
    if _held > 0:
        _broken("plex-no-episodes",
                "section=%s-holds=%d-items-but-type4-returned-0-episodes"
                % (_short(_s["section"], 40), _held))

aged_items.sort(key=lambda r: (-(r["ageHours"] or 1e9), r["series"], r["season"] or 0,
                               r["episode"] or 0))
total_aged = len(aged_items)
total_grace = len(under_grace_items)
affected = [s for s in per_section if s["aged"] > 0]

# Group by series, because that is the unit of remediation. Both root causes
# are fixed at series level (unmatch+rematch the show; or fix the Sonarr
# instance's renameEpisodes/seasonFolder for that series) — never episode by
# episode. An alert listing "the 3 oldest episodes" would show three rows of
# the SAME series and hide the other two entirely.
by_series = {}
for record in aged_items:
    bucket = by_series.setdefault((record["section"], record["series"]),
                                  {"section": record["section"],
                                   "series": record["series"],
                                   "episodes": 0, "max_age_hours": None})
    bucket["episodes"] += 1
    age = record["ageHours"]
    if age is not None and (bucket["max_age_hours"] is None
                            or age > bucket["max_age_hours"]):
        bucket["max_age_hours"] = age
affected_series = sorted(by_series.values(),
                         key=lambda b: (-(b["max_age_hours"] or 1e9), b["series"]))


def _join_capped(parts, limit, sep=";"):
    """Join WHOLE elements up to `limit` chars, then say how many were dropped.

    A plain `[:limit]` slice cuts mid-token — the first draft of this canary
    emitted `... S1E2 5` where the age was `527.0h`, which reads as a different
    (and wrong) number rather than as an obvious truncation.

    The marker counts against the budget as well: a "+N more" that overflows is
    itself sliced by cli.py, which reintroduces exactly the mid-token cut."""
    parts = list(parts)
    out = []
    used = 0
    for part in parts:
        cost = len(part) + (len(sep) if out else 0)
        if used + cost > limit:
            break
        out.append(part)
        used += cost
    else:
        return sep.join(out)
    while True:
        dropped = len(parts) - len(out)
        marker = ("%s+%d more" % (sep, dropped)) if out else "+%d more" % dropped
        if not out or used + len(marker) <= limit:
            return sep.join(out) + marker
        last = out.pop()
        used -= len(last) + (len(sep) if out else 0)

report = {
    "canary": "plex-unmatched",
    "status": "stuck" if total_aged > MAX_AGED else "clean",
    "now": int(NOW),
    "grace_hours": GRACE_HOURS,
    "max_aged": MAX_AGED,
    "sections_scanned": [s["section"] for s in per_section],
    "sections_skipped": skipped_sections,
    "episodes": total_episodes,
    "local": total_local,
    "aged": total_aged,
    "under_grace": total_grace,
    "per_section": per_section,
    "affected_sections": [s["section"] for s in affected],
    "affected_series": affected_series,
    "items": aged_items,
    "under_grace_items": under_grace_items,
}

if EMIT_JSON:
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")

if total_aged > MAX_AGED:
    # cli.py truncates stderr to 200 chars for the Kuma msg=, so the budget is
    # spent deliberately: the counts first (never truncated), then the section
    # split, then as many affected SERIES as fit.
    sections_hint = _join_capped(
        ["%s:%d" % (_short(s["section"], 24), s["aged"]) for s in affected],
        44, sep=",")
    series_hint = _join_capped(
        ["%s x%d %gh" % (_short(b["series"], 26), b["episodes"],
                         b["max_age_hours"] if b["max_age_hours"] is not None else 0)
         for b in affected_series],
        62)
    sys.stderr.write(
        "STAGE=plex-unmatched-stuck msg=aged=%d/%d-suppressed=%d-grace=%gh-"
        "sections=[%s]-series=[%s]\n"
        % (total_aged, total_episodes, total_grace, GRACE_HOURS,
           sections_hint, series_hint))
    if not EMIT_JSON:
        # Full detail on stdout for journald / an operator running it by hand.
        # The Kuma msg is the one-liner above; this is the triage view.
        sys.stdout.write("plex-unmatched-stuck aged=%d under_grace=%d episodes=%d "
                         "sections=%d skipped=%d\n"
                         % (total_aged, total_grace, total_episodes,
                            len(per_section), len(skipped_sections)))
        for bucket in affected_series:
            sys.stdout.write("  %s | %s | %d episode(s) | oldest %sh\n"
                             % (bucket["section"], bucket["series"],
                                bucket["episodes"],
                                bucket["max_age_hours"]
                                if bucket["max_age_hours"] is not None else "?"))
    sys.exit(1)

if not EMIT_JSON:
    # The under_grace count is printed on the CLEAN line too. A suppression the
    # operator cannot see is a suppression that becomes permanent by accident.
    sys.stdout.write(
        "plex-unmatched-clean local=%d aged=%d under_grace=%d grace=%gh "
        "episodes=%d sections=%d skipped=%d\n"
        % (total_local, total_aged, total_grace, GRACE_HOURS,
           total_episodes, len(per_section), len(skipped_sections)))
sys.exit(0)
PYEND
REMOTE_EOF
)

RES=$(sshm "export PLEX_UNMATCHED_JSON=${EMIT_JSON}
$REMOTE")
RC=$?
echo "$RES"
exit $RC
