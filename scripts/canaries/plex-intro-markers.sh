#!/usr/bin/env bash
# plex-intro-markers canary: Skip Intro coverage on the TV libraries, and the
# settings that produce it.
#
# WHY THIS EXISTS
# 2026-09-20, from a member report of "missing episodes" that turned out to be
# a different complaint entirely: Skip Intro was absent on roughly a third of
# the TV library (299 of 478 episodes had an intro marker, 63%). Nothing in
# this repo looked at marker coverage, so the gap was invisible until someone
# watching an episode noticed the button was not there.
#
# Two facts make this worth a detector rather than a one-off fix:
#
#   1. INTRO MARKERS ARE BUTLER-ONLY. Measured on Squid Game S1: a forced
#      PUT /library/metadata/{season}/analyze produced 9 credits markers and
#      ZERO intro markers. Plex generates intro markers by fingerprint-
#      comparing a whole season, and only inside the nightly butler window.
#      There is no per-item "make an intro marker" call to remediate with.
#   2. THE WINDOW IS A FIXED BUDGET AND EVERY MARKER TASK SHARES IT. Marker
#      creation timestamps cluster entirely inside ButlerStartHour..EndHour.
#      When credits, ad markers and voice activity all run too, intro markers
#      trickle out at 1-9 a night and a backlog never clears. On 2026-09-20
#      credits generation was turned OFF on the two show libraries and ad
#      markers set to never, precisely to hand that budget to intro.
#
# So the coverage number is downstream of four settings that are edited in the
# Plex UI, by hand, with no git history and no other guard. A silent flip of
# any of them stops Skip Intro for every future episode, and nothing else in
# the stack would say a word. THAT is what this canary is really watching;
# the coverage percentage is the outcome, the settings are the cause.
#
# WHAT IT CHECKS
#   A. Every `show` section has enableIntroMarkerGeneration = true.
#   B. Server GenerateIntroMarkerBehavior is not `never`.
#   C. Intro-marker coverage across all show sections is at or above
#      PLEX_INTRO_MIN_PCT, counting only episodes old enough to have had a
#      butler pass at them.
#
# COVERAGE IS MEASURED OVER AGED EPISODES ONLY, and that is the whole reason
# this canary is not permanently red. An episode imported an hour ago has had
# no butler window yet; counting it would make every evening's imports look
# like a regression. PLEX_INTRO_GRACE_HOURS (default 48 = two butler windows,
# since a single window can be missed entirely if the server is busy) is the
# line. Under-grace episodes are COUNTED AND REPORTED on both the clean and
# the failing line, never silently dropped.
#
# THE FLOOR IS A PERCENTAGE HERE, unlike plex-unmatched's count-with-grace,
# and the difference is deliberate. An unmatched item is always a defect, so
# any count above zero is real. An episode WITHOUT an intro marker frequently
# is not a defect at all: Plex cannot mark an intro that does not exist, and
# plenty of shows have no recurring title sequence (talk shows, reality,
# documentary, many Netflix dramas). 100% is unreachable and chasing it would
# page forever. The floor answers a different question — "is the pipeline
# still producing markers at the rate it used to" — which is the question that
# actually has an actionable answer.
#
# DEFAULT FLOOR IS DELIBERATELY BELOW TODAY'S NUMBER. Coverage at the time of
# writing is 63%; the default floor is 55%. A floor set at today's measurement
# reds on the first legitimately marker-less series someone adds. The point is
# to catch a COLLAPSE (a settings flip takes it toward 0 as the library turns
# over) not to ratchet.
#
# NOT REMEDIATED, and unusually this one CANNOT be: there is no API that makes
# an intro marker on demand (fact 1 above). The remedy for a settings drift is
# to put the setting back — named in the failure message — and then wait for
# butler. The remedy for genuine low coverage is to widen the butler window,
# which is an operator judgement about CPU on a shared box, not something a
# timer should do unattended.
#
# Tunables (systemd `Environment=` or env):
#   PLEX_INTRO_MIN_PCT       default 55   fail below this % aged coverage
#   PLEX_INTRO_GRACE_HOURS   default 48   younger episodes are not counted
#   PLEX_INTRO_MIN_SAMPLE    default 25   below this many aged episodes the
#                                         percentage is not meaningful; report
#                                         and pass rather than page on noise
#   PLEX_INTRO_PAGE_SIZE     default 500  X-Plex-Container-Size per page
#   PLEX_INTRO_TIMEOUT       default 25   seconds per HTTP call
#   PLEX_INTRO_NOW           test-only clock override (epoch seconds)
#
# Flags:
#   --json   machine-readable report on stdout (dashboard consumers) instead
#            of the one-line summary. Exit codes are identical either way.
#
# Stage labels (failure messages on stderr -> Kuma msg=):
#   plex-intro-config-missing      plex.host/port/token unreadable   (BROKEN)
#   plex-intro-sections-unreachable GET /library/sections failed     (BROKEN)
#   plex-intro-no-show-sections    Plex returned zero show libraries (BROKEN)
#   plex-intro-prefs-unreachable   server or section prefs failed    (BROKEN)
#   plex-intro-section-unreachable a section listing failed          (BROKEN)
#   plex-intro-section-truncated   declared N items, fewer arrived   (BROKEN)
#   plex-intro-no-episodes         every show section returned ZERO  (BROKEN)
#   plex-intro-generation-disabled a library toggle or the server    (RED)
#                                  behavior is off
#   plex-intro-coverage-low        aged coverage under the floor     (RED)
#
# Exits — empty-because-clean is distinguishable from empty-because-broken:
#   0 — queried every show section, settings correct, coverage at or above the
#       floor (or sample too small to judge, which is reported)
#   1 — a generation setting is off, or coverage is below the floor
#   2 — could not query Plex / config missing / a section read was truncated
#       (the canary asserted NOTHING)
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

REMOTE=$(cat <<'REMOTE_EOF'
set -uo pipefail
SECRETS=~/secrets
PLEX_HOST=$(cat "$SECRETS/plex.host" 2>/dev/null)
PLEX_PORT=$(cat "$SECRETS/plex.port" 2>/dev/null)
PLEX_TOKEN=$(cat "$SECRETS/plex.token" 2>/dev/null)

# A missing credential exits 2 (BROKEN), never 0 — the C-09 silent-exit class.
if [ -z "$PLEX_HOST" ] || [ -z "$PLEX_PORT" ] || [ -z "$PLEX_TOKEN" ]; then
  printf "STAGE=plex-intro-config-missing msg=host=%s-port=%s-token=%s\n" \
    "${PLEX_HOST:+SET}" "${PLEX_PORT:+SET}" "${PLEX_TOKEN:+SET}" >&2
  exit 2
fi

export PLEX_BASE="http://${PLEX_HOST}:${PLEX_PORT}"
export PLEX_TOKEN
python3 <<"PYEND"
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = (os.environ.get("PLEX_BASE") or "").rstrip("/")
TOKEN = os.environ.get("PLEX_TOKEN") or ""
MIN_PCT = float(os.environ.get("PLEX_INTRO_MIN_PCT") or 55)
GRACE_HOURS = float(os.environ.get("PLEX_INTRO_GRACE_HOURS") or 48)
MIN_SAMPLE = int(os.environ.get("PLEX_INTRO_MIN_SAMPLE") or 25)
PAGE_SIZE = int(os.environ.get("PLEX_INTRO_PAGE_SIZE") or 500)
TIMEOUT = float(os.environ.get("PLEX_INTRO_TIMEOUT") or 25)
EMIT_JSON = (os.environ.get("PLEX_INTRO_JSON") or "0") == "1"
NOW = float(os.environ.get("PLEX_INTRO_NOW") or time.time())
MAX_PAGES = int(os.environ.get("PLEX_INTRO_MAX_PAGES") or 200)


def _short(value, limit=60):
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    return text[:limit]


def _broken(stage, msg):
    """Exit 2 — the canary asserted NOTHING. Never looks like a clean run."""
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
    """Never leaks the base URL into stderr — it carries host and port, and
    this text lands in Kuma and Discord."""
    try:
        return _get(path, params, headers)
    except urllib.error.HTTPError as exc:
        _broken(stage, "path=%s-http=%s" % (path, exc.code))
    except urllib.error.URLError as exc:
        _broken(stage, "path=%s-unreachable=%s" % (path, _short(exc.reason, 40)))
    except Exception as exc:                      # noqa: BLE001 - report, don't crash
        _broken(stage, "path=%s-error=%s" % (path, _short(exc, 40)))


def _truthy(value):
    """Plex answers these toggles as the STRINGS "true"/"false" in JSON, not
    as booleans. `bool("false")` is True, so a naive read would call a
    disabled library enabled and this canary would be green on the exact
    failure it exists to catch."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


if not BASE or not TOKEN:
    _broken("plex-intro-config-missing", "base=%s-token=%s"
            % ("SET" if BASE else "EMPTY", "SET" if TOKEN else "EMPTY"))

# --- 1. Discover show sections -------------------------------------------
# Derived, never hardcoded: a hardcoded id list means the next TV library
# anyone adds is silently unmonitored. Movie sections cannot carry intro
# markers at all (Plex does not generate them for movies), so they are skipped,
# COUNTED and NAMED rather than dropped in silence.
sections_doc = _get_or_broken("/library/sections", "plex-intro-sections-unreachable")
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
        skipped_sections.append("%s(%s)" % (title, stype or "?"))

if not show_sections:
    _broken("plex-intro-no-show-sections",
            "directories=%d-skipped=%d" % (len(directories), len(skipped_sections)))

# --- 2. The settings that PRODUCE the coverage ----------------------------
prefs_doc = _get_or_broken("/:/prefs", "plex-intro-prefs-unreachable")
server_behavior = None
for setting in (prefs_doc.get("MediaContainer") or {}).get("Setting") or []:
    if setting.get("id") == "GenerateIntroMarkerBehavior":
        server_behavior = str(setting.get("value") or "")
        break

disabled = []
if server_behavior is not None and server_behavior.lower() == "never":
    disabled.append("server:GenerateIntroMarkerBehavior=never")

section_settings = {}
for key, title in show_sections:
    doc = _get_or_broken("/library/sections/%s/prefs" % urllib.parse.quote(key),
                         "plex-intro-prefs-unreachable")
    enabled = None
    for setting in (doc.get("MediaContainer") or {}).get("Setting") or []:
        if setting.get("id") == "enableIntroMarkerGeneration":
            enabled = _truthy(setting.get("value"))
            break
    section_settings[key] = enabled
    # `None` means the pref is absent on a show library, which should not
    # happen — treat it as a finding rather than as consent.
    if enabled is not True:
        disabled.append("%s:enableIntroMarkerGeneration=%s"
                        % (_short(title, 30),
                           "absent" if enabled is None else "false"))

# --- 3. Coverage over aged episodes ---------------------------------------
def _paged(path, stage, title, params=None):
    """Explicit pagination. Trusting Plex's default page size would read the
    first N rows and call the rest covered."""
    collected = []
    declared = None
    pages = 0
    while True:
        pages += 1
        if pages > MAX_PAGES:
            _broken("plex-intro-section-truncated",
                    "section=%s-runaway-pages=%d" % (_short(title, 40), pages))
        doc = _get_or_broken(path, stage, params=params,
                             headers={"X-Plex-Container-Start": len(collected),
                                      "X-Plex-Container-Size": PAGE_SIZE})
        container = doc.get("MediaContainer") or {}
        items = container.get("Metadata") or []
        if declared is None and "totalSize" in container:
            declared = int(container.get("totalSize") or 0)
        collected.extend(items)
        if declared is not None and len(collected) >= declared:
            break
        if len(items) < PAGE_SIZE:
            break
    if declared is not None and declared > 0 and len(collected) < declared:
        _broken("plex-intro-section-truncated",
                "section=%s-declared=%d-received=%d"
                % (_short(title, 40), declared, len(collected)))
    return collected


# COVERAGE COMES FROM THE DATABASE, READ-ONLY, AND THE SETTINGS COME FROM HTTP.
# That split is forced by Plex, not chosen for convenience. Measured
# 2026-09-20 against the deployed 1.43.3:
#   /library/sections/{id}/all?type=4&includeMarkers=1   -> no Marker element
#   /library/metadata/{show}/allLeaves?includeMarkers=1  -> no Marker element
#   /library/metadata/{season}/children?includeMarkers=1 -> no Marker element
#   /library/metadata/{episode}?includeMarkers=1         -> markers, correctly
# Only the single-item route carries them, so an HTTP census of a 478-episode
# library is 478 round trips. A first version of this canary trusted the bulk
# parameter, got zero markers back, and reported 0% on a library the database
# showed at 63% -- it would have paged every single run while the thing it
# watches was healthy. The DB answers the same question in one query.
#
# THE RISK OF READING PLEX'S SCHEMA IS A SILENT ZERO, so it is guarded: if the
# whole database contains no markers of EITHER type, that is a schema or path
# change (BROKEN, exit 2), never "coverage collapsed to zero". A real collapse
# still leaves the credits markers already on disk, and a real empty library is
# caught separately by the no-episodes gate.
def _db_coverage(section_keys):
    """{section_key: (aged, aged_with_intro, under_grace, episodes)} plus the
    all-type marker total used as the schema sanity gate."""
    db = os.path.expanduser(
        "~/.config/plex/Library/Application Support/Plex Media Server/"
        "Plug-in Support/Databases/com.plexapp.plugins.library.db")
    if not os.path.exists(db):
        _broken("plex-intro-db-missing", "path-absent")
    cutoff = NOW - GRACE_HOURS * 3600.0
    query = (
        "SELECT mi.library_section_id,"
        " sum(CASE WHEN mi.added_at IS NOT NULL AND mi.added_at > %d"
        "          THEN 1 ELSE 0 END),"
        " sum(CASE WHEN mi.added_at IS NULL OR mi.added_at <= %d"
        "          THEN 1 ELSE 0 END),"
        " sum(CASE WHEN (mi.added_at IS NULL OR mi.added_at <= %d)"
        "          AND EXISTS(SELECT 1 FROM taggings t"
        "                     WHERE t.metadata_item_id = mi.id"
        "                       AND t.text = 'intro') THEN 1 ELSE 0 END),"
        " count(*)"
        " FROM metadata_items mi WHERE mi.metadata_type = 4"
        " GROUP BY mi.library_section_id;"
        % (cutoff, cutoff, cutoff))
    gate = ("SELECT count(*) FROM taggings"
            " WHERE text IN ('intro', 'credits');")
    try:
        proc = subprocess.run(
            ["sqlite3", "-readonly", "-separator", "|", db, query + gate],
            capture_output=True, text=True, timeout=TIMEOUT)
    except Exception as exc:                      # noqa: BLE001
        _broken("plex-intro-db-unreadable", _short(exc, 60))
    if proc.returncode != 0:
        _broken("plex-intro-db-unreadable",
                "sqlite-rc=%d-%s" % (proc.returncode, _short(proc.stderr, 60)))
    rows = [r for r in proc.stdout.strip().splitlines() if r.strip()]
    if not rows:
        _broken("plex-intro-db-unreadable", "query-returned-nothing")
    total_markers = int(rows[-1].split("|")[0] or 0)
    out = {}
    for row in rows[:-1]:
        parts = row.split("|")
        if len(parts) != 5:
            continue
        out[parts[0]] = (int(parts[2] or 0), int(parts[3] or 0),
                         int(parts[1] or 0), int(parts[4] or 0))
    return out, total_markers


coverage, total_markers = _db_coverage([k for k, _ in show_sections])
if total_markers == 0:
    _broken("plex-intro-db-schema",
            "zero-markers-of-either-type-in-db-schema-or-path-changed")

per_section = []
total_episodes = 0
total_aged = 0
total_aged_with_intro = 0
total_grace = 0

for key, title in show_sections:
    aged, aged_with_intro, under_grace, episodes = coverage.get(key, (0, 0, 0, 0))
    total_episodes += episodes
    total_aged += aged
    total_aged_with_intro += aged_with_intro
    total_grace += under_grace
    per_section.append({
        "section": title,
        "episodes": episodes,
        "aged": aged,
        "aged_with_intro": aged_with_intro,
        "under_grace": under_grace,
        "pct": round(100.0 * aged_with_intro / aged, 1) if aged else None,
        "intro_generation": section_settings.get(key),
    })

if total_episodes == 0:
    # Every show section empty is BROKEN, not 100% coverage of nothing.
    _broken("plex-intro-no-episodes",
            "sections=%d-skipped=%d" % (len(per_section), len(skipped_sections)))

pct = round(100.0 * total_aged_with_intro / total_aged, 1) if total_aged else None
sample_too_small = total_aged < MIN_SAMPLE

report = {
    "status": "ok",
    "pct": pct,
    "aged": total_aged,
    "aged_with_intro": total_aged_with_intro,
    "under_grace": total_grace,
    "episodes": total_episodes,
    "floor_pct": MIN_PCT,
    "grace_hours": GRACE_HOURS,
    "min_sample": MIN_SAMPLE,
    "sample_too_small": sample_too_small,
    "server_behavior": server_behavior,
    "disabled": disabled,
    "sections": per_section,
    "skipped_sections": skipped_sections,
}

# --- 4. Verdict -----------------------------------------------------------
# Settings first: a disabled toggle is the CAUSE and coverage is the symptom,
# so reporting the cause is strictly more actionable. Coverage can still be
# high for weeks after generation is switched off — every already-marked
# episode keeps its marker — which is exactly why the settings check cannot be
# left to the percentage to discover.
if disabled:
    report["status"] = "generation-disabled"
    sys.stderr.write("STAGE=plex-intro-generation-disabled msg=%s\n"
                     % _short("-".join(disabled), 180))
    if EMIT_JSON:
        sys.stdout.write(json.dumps(report) + "\n")
    else:
        sys.stdout.write("plex-intro-generation-disabled %s\n" % "; ".join(disabled))
    sys.exit(1)

if pct is not None and not sample_too_small and pct < MIN_PCT:
    report["status"] = "coverage-low"
    worst = sorted((s for s in per_section if s["pct"] is not None),
                   key=lambda s: s["pct"])[:2]
    hint = ",".join("%s=%s%%" % (_short(s["section"], 18), s["pct"]) for s in worst)
    sys.stderr.write("STAGE=plex-intro-coverage-low "
                     "msg=%s%%-of-%d-aged-episodes-floor=%s%%-worst=%s\n"
                     % (pct, total_aged, MIN_PCT, _short(hint, 70)))
    if EMIT_JSON:
        sys.stdout.write(json.dumps(report) + "\n")
    else:
        sys.stdout.write("plex-intro-coverage-low pct=%s aged=%d with_intro=%d "
                         "under_grace=%d floor=%s\n"
                         % (pct, total_aged, total_aged_with_intro,
                            total_grace, MIN_PCT))
        for section in per_section:
            sys.stdout.write("  %s | %s%% | %d/%d aged | %d under grace\n"
                             % (section["section"], section["pct"],
                                section["aged_with_intro"], section["aged"],
                                section["under_grace"]))
    sys.exit(1)

if EMIT_JSON:
    sys.stdout.write(json.dumps(report) + "\n")
else:
    # under_grace and the sample note are on the CLEAN line too: a suppression
    # the operator cannot see is one that becomes permanent by accident.
    sys.stdout.write("plex-intro-clean pct=%s aged=%d with_intro=%d under_grace=%d "
                     "floor=%s%s episodes=%d sections=%d skipped=%d\n"
                     % (pct, total_aged, total_aged_with_intro, total_grace,
                        MIN_PCT, " SAMPLE-TOO-SMALL" if sample_too_small else "",
                        total_episodes, len(per_section), len(skipped_sections)))
sys.exit(0)
PYEND
REMOTE_EOF
)

# Every tunable is forwarded EXPLICITLY. `ssh` does not carry the caller's
# environment, so a documented knob set as `PLEX_INTRO_MIN_PCT=90 ./canary`
# from the workstation would otherwise be silently ignored and the run would
# report the default while claiming to honour the override. Caught doing
# exactly that during bring-up. Unset variables stay unset on the far side
# (${VAR+...}) so the remote defaults still apply.
FORWARD="export PLEX_INTRO_JSON=${EMIT_JSON}"
for var in PLEX_INTRO_MIN_PCT PLEX_INTRO_GRACE_HOURS PLEX_INTRO_MIN_SAMPLE            PLEX_INTRO_PAGE_SIZE PLEX_INTRO_TIMEOUT PLEX_INTRO_MAX_PAGES            PLEX_INTRO_NOW; do
  eval "value=\${$var+set}"
  if [ "${value:-}" = "set" ]; then
    eval "value=\$$var"
    FORWARD="$FORWARD
export $var=$(printf %q "$value")"
  fi
done

RES=$(sshm "$FORWARD
$REMOTE")
RC=$?
echo "$RES"
exit $RC
