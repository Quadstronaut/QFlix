#!/usr/bin/env bash
# tdarr-transcode-error canary: is anything parked in Tdarr's TERMINAL error
# state, older than the janitor's chance to fix it?
#
# WHY THIS EXISTS
# ---------------
# `TranscodeDecisionMaker: Transcode error` is terminal: Tdarr NEVER retries
# it. A file that lands there is parked forever, and nothing reported the
# population -- 13 episodes of one series sat parked from 2026-08-06 until a
# human went looking on 2026-08-08. The root cause that time (an unmappable
# codec_name=unknown placeholder stream aborting the matroska muxer:
# "Subtitle codec 0 is not supported") now has a dedicated janitor
# (unknown-codec-stream-janitor.py, daily 04:00 UTC), but the janitor fixes
# only the cause it knows. This canary watches the STATE, so the next cause
# nobody has thought of -- a new release group's container quirk, a Tdarr
# upgrade regression, a broken flow edit -- surfaces as a red instead of as a
# member asking why an episode never got its direct-play audio track.
#
# Same reasoning as unstick-rate: a guard on the RULE catches only the failure
# it was written for; watching the terminal OUTCOME catches every cause.
#
# WHY A GRACE WINDOW, AND WHY 48h. A fresh Transcode error is EXPECTED
# transient: the janitor sweeps daily, strips what it can, and live-requeues.
# Alerting on arrival would red every night the janitor is about to handle
# and train the operator to ignore it. 48h = two janitor passes; anything
# still parked after two passes is by definition a cause the janitor does not
# handle, which is exactly the population this canary exists to name.
#
# THE CLOCK IS THE RECORD FILE's MTIME, acknowledged as a C-04 tradeoff.
# Tdarr rewrites a FileJSONDB record when the state changes, so mtime ==
# "when this file entered (or last re-confirmed) its current state" -- a
# writer-driven clock, not a content-stamped one. The records carry no
# reliable state-transition timestamp to prefer. The failure direction is
# safe: a spurious rewrite RESETS the age and delays the alert by one grace
# window; it can never fabricate one.
#
# EXIT CODES
#   0 - no Transcode-error record older than the grace window (fresh ones are
#       counted and named in the PASS message, never hidden)
#   1 - parked population: N record(s) older than grace. Message carries the
#       count and the first offender's basename (content presence is fine to
#       surface; consumption never appears here).
#   2 - could not assert: FileJSONDB missing/unreadable, or ZERO records
#       total. Zero matters: this Tdarr has ~1,500 file records, so an empty
#       scan means the DB moved or the glob broke -- empty-because-broken
#       must never read as empty-because-clean.
#
# Overrides: QFLIX_CANARY_TDARR_TERR_GRACE_H (default 48).
#
# Lives on the seedbox at ~/scripts/canaries/tdarr-transcode-error.sh
# (deployed by 240-maintenance-install.sh). Invoked by
# manitoba-maint-canary-tdarr-transcode-error, which pushes status=up/down to
# Kuma monitor "Canary Tdarr Transcode Error".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

GRACE_H="${QFLIX_CANARY_TDARR_TERR_GRACE_H:-48}"

RES=$(sshm "GRACE_H=$GRACE_H"' python3 - <<PY
import glob, json, os, sys, time

db = os.path.expanduser("~/.apps/tdarr/server/Tdarr/DB2/FileJSONDB")
grace_s = int(os.environ.get("GRACE_H", "48")) * 3600
now = time.time()

paths = glob.glob(db + "/*.json")
if not paths:
    print("STAGE=tdarr-filedb-empty msg=zero-FileJSONDB-records-at-%s-db-moved-or-glob-broke" % db,
          file=sys.stderr)
    sys.exit(2)

total = 0
parked = []      # (age_h, basename) older than grace
fresh = 0        # in Transcode error but within grace (janitor gets a shot)
unreadable = 0
for p in paths:
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        unreadable += 1
        continue
    total += 1
    if d.get("TranscodeDecisionMaker") != "Transcode error":
        continue
    age_s = now - os.path.getmtime(p)
    src = (d.get("_id") or d.get("file") or "?").rsplit("/", 1)[-1][:70]
    if age_s > grace_s:
        parked.append((age_s / 3600.0, src))
    else:
        fresh += 1

if unreadable and total == 0:
    print("STAGE=tdarr-filedb-unreadable msg=%d-records-all-unparseable" % unreadable,
          file=sys.stderr)
    sys.exit(2)

if parked:
    parked.sort(reverse=True)
    age_h, first = parked[0]
    print("STAGE=tdarr-transcode-error-parked msg=%d-file(s)-terminal->%dh-oldest=%.0fh-first=%s-janitor-does-not-handle-this-cause"
          % (len(parked), int(os.environ.get("GRACE_H", "48")), age_h, first),
          file=sys.stderr)
    sys.exit(1)

print("PASS: tdarr-transcode-error - 0 parked beyond %sh grace (%d fresh error(s) awaiting the janitor, %d records scanned, %d unreadable)"
      % (os.environ.get("GRACE_H", "48"), fresh, total, unreadable))
sys.exit(0)
PY')
RC=$?
echo "$RES"
exit $RC
