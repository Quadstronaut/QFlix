#!/usr/bin/env bash
# cron-liveness canary: does the live crontab match the ledger, both ways?
#
# WHY THIS EXISTS
# ---------------
# QFlix schedules work on THREE planes, and until 2026-08-07 the dead-man ledger
# could only see one and a half of them:
#
#   1. repo-tracked systemd timers   (jobs.yaml `timer:`)  - always covered
#   2. installer-generated timers    (jobs.yaml `unit:`)   - covered 2026-08-06
#   3. the user CRONTAB                                    - covered by this
#
# Plane 3 was not merely unmonitored, it was inexpressible: a crontab line has
# no unit name AND no file in git, so neither `timer:` nor `unit:` could name
# one. All ten lines were unadjudicated and unadjudicatABLE - including
# `kill_stream.sh --max 4`, which is member-facing enforcement of the per-member
# concurrent-stream cap.
#
# manifest/jobs.yaml `cron:` entries now declare them. This canary is the LIVE
# half of that: the offline C-01 detector reads the declaration and demands a
# written dead-man answer for each, but it runs against git and cannot see the
# box, so it can never notice a crontab line that was DROPPED or one that was
# ADDED without being declared. Same offline-audits-SOURCE / live-audits-RUNNING
# split the rest of the regime uses.
#
# TWO DIRECTIONS, the prowlarr-app-sync shape:
#
#   cron-declared-missing  - the ledger declares it, `crontab -l` does not have
#                            it. The job is gone. 59a-plex-stream-crons-install
#                            .sh records that a slot rebuild has already wiped
#                            these once, silently.
#   cron-unclaimed         - a crontab line no `cron:` entry claims. A job is
#                            running that nothing has adjudicated a dead-man
#                            for. This is the direction that catches the NEXT
#                            hand-added cron before it becomes another
#                            invisible member-facing dependency.
#
# WHY A SEPARATE CANARY AND NOT A LEG OF timer-liveness.sh. That canary owns
# "declared systemd timers are loaded, active and scheduled" - a different
# mechanism, a different failure vocabulary (loaded/active/next-fire has no
# crontab analogue), and a different blast radius. The house precedent is
# explicit: prowlarr-app-sync was deliberately a SECOND Prowlarr canary rather
# than a third probe bolted onto the first, "different signal, different
# cadence, independently swappable". Same reasoning here.
#
# MATCHING IS ON `cron:` ONLY, NEVER ON `schedule:`. The schedule field is
# documentation for the reader; if it drifted from the real crontab expression
# this check would start failing for a cosmetic reason. Matching on the command
# substring means the check tracks what actually runs.
#
# COMMENTS AND BLANKS ARE NOT JOBS. `crontab -l` includes comment lines and any
# MAILTO=/PATH= environment assignments; those are configuration, not scheduled
# work, and are skipped, counted and named rather than reported as unclaimed.
#
# EXIT CODES
#   0 - every declared cron present, every crontab job claimed
#   1 - cron-declared-missing / cron-unclaimed
#   2 - could not assert: ledger unreadable, `crontab -l` failed, or the ledger
#       declares ZERO cron jobs. That last one matters: an empty declaration set
#       would make "everything declared is present" trivially true, which is the
#       empty-because-broken-reads-as-empty-because-clean trap.
#
# Lives on the seedbox at ~/scripts/canaries/cron-liveness.sh (deployed by
# 240-maintenance-install.sh). Invoked by manitoba-maint-canary-cron-liveness,
# which pushes status=up/down to Kuma monitor "Canary Cron Liveness".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail

# Same candidate list timer-liveness.sh uses, for the same reason: the repo
# checkout is not guaranteed to exist on the box.
LEDGER=""
for _cand in "$HOME/.opt/maint/jobs.yaml" "$HOME/manifest/jobs.yaml" "$HOME/scripts/maint/jobs.yaml"; do
  [ -f "$_cand" ] && { LEDGER="$_cand"; break; }
done
if [ -z "$LEDGER" ]; then
  printf "STAGE=cron-ledger-missing msg=no-jobs.yaml-on-box\n" >&2
  exit 2
fi

DECLARED=$(python3 - "$LEDGER" <<PY 2>/dev/null
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
for name, v in (d.get("jobs") or {}).items():
    if isinstance(v, dict) and v.get("cron"):
        print(str(v["cron"]))
PY
)
RC=$?
if [ "$RC" -ne 0 ]; then
  printf "STAGE=cron-ledger-unreadable msg=could-not-parse-jobs.yaml-rc-%s\n" "$RC" >&2
  exit 2
fi
DECLARED=$(printf "%s" "$DECLARED" | tr -d "\r")
NDECL=$(printf "%s\n" "$DECLARED" | grep -c . || true)
if [ "${NDECL:-0}" -lt 1 ]; then
  # An empty declaration set makes direction 1 vacuously true. Refuse to pass.
  printf "STAGE=cron-none-declared msg=ledger-declares-zero-cron-jobs-nothing-to-assert\n" >&2
  exit 2
fi

CRONTAB=$(crontab -l 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$CRONTAB" ]; then
  printf "STAGE=cron-crontab-unreadable msg=crontab-l-returned-nothing-declared=%s\n" "$NDECL" >&2
  exit 2
fi

# Real scheduled lines only: drop comments, blanks and VAR= assignments.
JOBLINES=$(printf "%s\n" "$CRONTAB" \
  | grep -vE "^[[:space:]]*#" \
  | grep -vE "^[[:space:]]*$" \
  | grep -vE "^[[:space:]]*[A-Z_]+=")
NJOBS=$(printf "%s\n" "$JOBLINES" | grep -c . || true)
NSKIP=$(( $(printf "%s\n" "$CRONTAB" | grep -c . || true) - NJOBS ))

MISSING=""
while IFS= read -r want; do
  [ -z "$want" ] && continue
  if ! printf "%s\n" "$JOBLINES" | grep -qF -- "$want"; then
    MISSING="$MISSING $want"
  fi
done <<< "$DECLARED"

UNCLAIMED=0
UNCLAIMED_EG=""
while IFS= read -r line; do
  [ -z "$line" ] && continue
  hit=0
  while IFS= read -r want; do
    [ -z "$want" ] && continue
    case "$line" in *"$want"*) hit=1; break ;; esac
  done <<< "$DECLARED"
  if [ "$hit" -eq 0 ]; then
    UNCLAIMED=$((UNCLAIMED + 1))
    # First offender only, and only its COMMAND, to keep the Kuma msg bounded.
    [ -z "$UNCLAIMED_EG" ] && UNCLAIMED_EG=$(printf "%s" "$line" | awk "{for(i=6;i<=NF;i++) printf \"%s \", \$i}" | cut -c1-60)
  fi
done <<< "$JOBLINES"

FAILED=0
if [ -n "$MISSING" ]; then
  printf "STAGE=cron-declared-missing msg=declared-in-jobs.yaml-but-absent-from-crontab:%s\n" "$MISSING" >&2
  FAILED=1
fi
if [ "$UNCLAIMED" -gt 0 ]; then
  printf "STAGE=cron-unclaimed msg=%s-crontab-job(s)-no-cron:-entry-claims-first=%s\n" \
    "$UNCLAIMED" "$UNCLAIMED_EG" >&2
  FAILED=1
fi
[ "$FAILED" -eq 1 ] && exit 1

printf "PASS: cron-liveness - %s/%s declared cron jobs present, 0 unclaimed (%s non-job line(s) skipped)\n" \
  "$NDECL" "$NJOBS" "$NSKIP"
exit 0
')
RC=$?
echo "$RES"
exit $RC
