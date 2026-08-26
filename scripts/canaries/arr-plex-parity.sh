#!/usr/bin/env bash
# arr-plex-parity canary: does Plex actually SHOW everything the *arrs
# believe they imported?
#
# WHY THIS EXISTS
# ---------------
# Mob Psycho 100 (2026-08-16): sonarr2 had renameEpisodes=false, Plex could
# not match the un-renamed files, and TWO WHOLE SEASONS were invisible to
# members while every surface stayed green — the files existed, sonarr said
# imported, Plex said healthy, and nothing anywhere compared the two sides.
# The rename fix shipped the same day; the DETECTION gap (recorded in memory
# as "no canary watches *arr<->Plex season/media-count parity") is this file.
#
# THE PREDICATE — file-path parity, not guid matching
# ---------------------------------------------------
# For every file the four *arrs believe is imported (sonarr + sonarr2
# episodefiles, radarr + radarr2 moviefiles), the same path must appear in
# Plex media_parts. Path is the honest join key on this box: this PMS returns
# NO Guid children on section listings (includeGuids=1 verified empty
# 2026-08-26) and the DB has no external-guids table, while both sides speak
# the identical /home/quadstronaut/media/... namespace (verified live). A
# file Plex never indexed — the Mob Psycho class — is exactly a path in the
# arr set and absent from media_parts.
#
# NOISE CONTROL (operator directive 2026-08-26: false positives render the
# whole alert channel unusable). A path only counts when ALL of these hold:
#   1. its arr dateAdded is older than GRACE_H (26h) — imports need a scan
#      cycle, and Monday-window churn needs a full day to settle;
#   2. the file still EXISTS on disk — a stale arr record for a file Tdarr
#      replaced or a janitor removed is the arr lagging its own rescan, not a
#      Plex gap, and pages nothing;
#   3. it was ALSO missing on the PREVIOUS run (state file) — one sighting
#      arms, two consecutive sightings page. A transient scan gap self-heals
#      inside a day and is never seen twice.
# Detection latency is therefore ~2 days. Mob Psycho sat invisible for WEEKS;
# two quiet days is the right trade against a channel the operator mutes.
#
# EXIT CODES
#   0 - parity holds, or first-sighting arm (msg says watching=N)
#   1 - >=1 path missing from Plex on two consecutive runs
#   2 - could not assert: an arr API unreachable, the Plex DB unreadable, or
#       the Plex path set empty. Empty-because-broken must never read as
#       empty-because-clean.
#
# Overrides: QFLIX_CANARY_PARITY_GRACE_H (default 26),
#            QFLIX_CANARY_PARITY_STATE, QFLIX_CANARY_PARITY_PLEX_DB.
#
# Lives on the seedbox at ~/scripts/canaries/arr-plex-parity.sh (deployed by
# 240-maintenance-install.sh). Invoked by manitoba-maint-canary-arr-plex-parity
# (daily), which pushes status=up/down to Kuma monitor "Canary Arr Plex Parity".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

GRACE_H=${QFLIX_CANARY_PARITY_GRACE_H:-26}
STATE_PATH=${QFLIX_CANARY_PARITY_STATE:-}
PLEX_DB=${QFLIX_CANARY_PARITY_PLEX_DB:-}

RES=$(sshm "
set -uo pipefail
export PAR_GRACE_H='${GRACE_H}' PAR_STATE='${STATE_PATH}' PAR_PLEX_DB='${PLEX_DB}'
export PAR_NOW=\$(date -u +%s)
"'
python3 - <<PYEOF
import calendar, json, os, sqlite3, sys, time, urllib.request

now = int(os.environ.get("PAR_NOW") or 0) or int(time.time())
grace_s = float(os.environ.get("PAR_GRACE_H", "26")) * 3600
state_path = os.environ.get("PAR_STATE") or os.path.expanduser(
    "~/.opt/maint/arr-plex-parity/state.json")
plex_db = os.environ.get("PAR_PLEX_DB") or os.path.expanduser(
    "~/.config/plex/Library/Application Support/Plex Media Server"
    "/Plug-in Support/Databases/com.plexapp.plugins.library.db")


def out(msg):
    print(msg)
    sys.exit(0)


def fail(stage, msg):
    sys.stderr.write("STAGE=%s msg=%s\n" % (stage, msg))
    sys.exit(1)


def cannot(stage, msg):
    sys.stderr.write("STAGE=%s msg=%s\n" % (stage, msg))
    sys.exit(2)


def secret(name):
    p = os.path.expanduser("~/secrets/" + name)
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def arr_get(port, base, key, path):
    # urlbase files carry NO leading slash and the prefix is MANDATORY on
    # this box (bare /api/v3 answers 307) — same rule 62-arr-extra-files
    # records. 30s timeout: a slow arr is not a missing arr.
    url = "http://127.0.0.1:%s/%s/api/v3/%s" % (port, base, path) if base \
        else "http://127.0.0.1:%s/api/v3/%s" % (port, path)
    req = urllib.request.Request(url, headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def parse_iso_epoch(stamp):
    # arr dateAdded is "2026-08-20T05:18:19Z" (sometimes with fraction).
    # Fail OPEN by treating an unparseable stamp as age 0 — a file whose age
    # we cannot read must never be old enough to page on its own.
    if not stamp:
        return now
    s = stamp.rstrip("Z").split(".")[0]
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return now


# ---- the arr side: every file the four instances believe is imported ------
arr_files = {}   # path -> (arr name, added epoch)
for arr, kind in (("sonarr", "tv"), ("sonarr2", "tv"),
                  ("radarr", "movie"), ("radarr2", "movie")):
    key, port = secret(arr + ".key"), secret(arr + ".port")
    base = secret(arr + ".urlbase") or arr
    if not key or not port:
        cannot("parity-secrets-missing",
               "no-key-or-port-for-%s-cannot-assert-parity" % arr)
    try:
        if kind == "movie":
            for m in arr_get(port, base, key, "movie"):
                mf = m.get("movieFile") or {}
                p = mf.get("path")
                if m.get("hasFile") and p:
                    arr_files[p] = (arr, parse_iso_epoch(mf.get("dateAdded")))
        else:
            for s in arr_get(port, base, key, "series"):
                stats = s.get("statistics") or {}
                if not stats.get("episodeFileCount"):
                    continue
                for ef in arr_get(port, base, key,
                                  "episodefile?seriesId=%d" % s["id"]):
                    p = ef.get("path")
                    if p:
                        arr_files[p] = (arr,
                                        parse_iso_epoch(ef.get("dateAdded")))
    except Exception as exc:
        cannot("parity-arr-unreachable",
               "%s-api-failed-%s" % (arr, str(exc).replace(" ", "_")[:80]))

if not arr_files:
    cannot("parity-arr-empty",
           "zero-imported-files-across-all-four-arrs-cannot-be-real")

# ---- the Plex side: every path media_parts knows ---------------------------
try:
    con = sqlite3.connect("file:%s?mode=ro" % plex_db.replace(" ", "%20"),
                          uri=True)
    rows = con.execute(
        "SELECT mp.file FROM media_parts mp"
        " JOIN media_items mi ON mi.id = mp.media_item_id"
        " JOIN metadata_items md ON md.id = mi.metadata_item_id"
        " WHERE md.metadata_type IN (1, 4) AND mp.file != \"\"").fetchall()
    con.close()
except sqlite3.Error as exc:
    cannot("parity-plex-db", "plex-db-unreadable-%s"
           % str(exc).replace(" ", "_")[:80])
plex_paths = set(r[0] for r in rows)
if not plex_paths:
    cannot("parity-plex-empty",
           "media_parts-returned-zero-video-paths-db-moved-or-schema-changed")

# ---- the deficit, filtered by every noise gate -----------------------------
missing = []
for p, (arr, added) in sorted(arr_files.items()):
    if p in plex_paths:
        continue
    if now - added < grace_s:
        continue                     # gate 1: younger than a scan cycle
    if not os.path.exists(p):
        continue                     # gate 2: stale arr record, not a Plex gap
    missing.append((p, arr))

# ---- persistence: two consecutive sightings page ---------------------------
prev = set()
try:
    with open(state_path, encoding="utf-8") as fh:
        prev = set(json.load(fh).get("missing", []))
except (OSError, ValueError):
    prev = set()
os.makedirs(os.path.dirname(state_path), exist_ok=True)
tmp = state_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump({"missing": [p for p, _ in missing], "checked": now,
               "arr_files": len(arr_files), "plex_paths": len(plex_paths)}, fh)
os.replace(tmp, state_path)

confirmed = [(p, arr) for p, arr in missing if p in prev]
if confirmed:
    per_arr = {}
    for _, arr in confirmed:
        per_arr[arr] = per_arr.get(arr, 0) + 1
    detail = ",".join("%s:%d" % (a, n) for a, n in sorted(per_arr.items()))
    first = os.path.basename(confirmed[0][0])
    fail("arr-plex-parity",
         "%d-file(s)-imported-but-absent-from-Plex-2-runs-running-%s-first=%s"
         % (len(confirmed), detail, first))

note = "watching=%d" % len(missing) if missing else "clean"
out("parity holds: %d arr files, %d plex paths, %s"
    % (len(arr_files), len(plex_paths), note))
PYEOF
')
rc=$?
if [ $rc -eq 0 ]; then
    echo "OK: arr-plex-parity - $RES"
fi
exit $rc
