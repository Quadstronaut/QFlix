#!/usr/bin/env bash
# unstick-rate canary: destructive automation must never be silent.
#
# WHY THIS EXISTS
# ---------------
# qflix-collect.py's `act_on_candidates()` calls unstick.py, which performs
#
#     DELETE /queue/{id}?removeFromClient=true&blocklist=true
#
# on anything the stale loop nominates. That deletes the download, removes it
# from the client, AND blocklists the release so the *arr will not grab it
# again. It is the single most destructive autonomous action on this stack, it
# is member-facing (a blocklisted release is content that does not arrive), and
# on 2026-08-07 it destroyed TEN legitimate Vanderpump releases in one run --
# with NO alert, NO Kuma red, and nothing anywhere for the operator to see. The
# only reason it stopped at ten is MAX_ACTIONS_PER_DAY, and that cap resets at
# 00:00 UTC.
#
# The rule that nominated them has been fixed (SAB reports every queued slot as
# "Downloading" while transferring one at a time, so zero byte-movement was the
# normal state of everything behind the head of the queue). This canary is the
# SECOND leg: the fix stops that particular false positive, this notices if
# ANY future rule change, *arr behaviour change or download-client quirk starts
# feeding the destructive path again.
#
# It deliberately watches the ACTION, not the rule. A guard that watches the
# rule can only catch the failure it was written for; watching the outcome
# catches every cause, including ones nobody has thought of.
#
# PREDICATES, read from the durable audit trail the actor already writes
# (~/.opt/qflix-collect/events/YYYY-MM-DD.jsonl, one JSON line per action):
#
#   1. WARN  at >= QFLIX_CANARY_UNSTICK_WARN (default 3) actions in the UTC day.
#      A healthy stack needs the occasional unstick. It does not need three.
#   2. FAIL  at >= QFLIX_CANARY_UNSTICK_FAIL (default 5), and ALWAYS if the
#      daily cap was reached -- hitting the cap means the system wanted to do
#      MORE than it was allowed, which is the signature of a runaway rule
#      rather than of a few genuine stalls.
#
# The cap-reached test is separate from the numeric threshold on purpose: the
# cap is operator-tunable, so a raised cap must not silently raise the alarm
# threshold with it. Reaching whatever cap is in force is itself the finding.
#
# WHY NOT "ALERT ON ANY ACTION": unstick exists to act unattended, and a canary
# that reds on every legitimate use gets muted, which would leave this exactly
# as blind as it was before. Thresholds keep the signal meaningful.
#
# EXIT CODES
#   0 - under the warn threshold (or a clean day with zero actions)
#   1 - unstick-rate-high / unstick-cap-reached
#   2 - could not assert: events dir missing, or a line that will not parse.
#       A malformed audit trail is NOT a quiet day -- that is the
#       empty-because-broken trap, and this canary exists precisely because
#       silence was mistaken for health once already.
#
# Lives on the seedbox at ~/scripts/canaries/unstick-rate.sh (deployed by
# 240-maintenance-install.sh). Invoked by manitoba-maint-canary-unstick-rate,
# which pushes status=up/down to Kuma monitor "Canary Unstick Rate".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail

WARN_AT=${QFLIX_CANARY_UNSTICK_WARN:-3}
FAIL_AT=${QFLIX_CANARY_UNSTICK_FAIL:-5}
EVENTS=${QFLIX_CANARY_UNSTICK_EVENTS:-$HOME/.opt/qflix-collect/events}
CAP=${QFLIX_COLLECT_MAX_ACTIONS:-10}

if [ ! -d "$EVENTS" ]; then
  # The actor creates this directory before its first write. Absent means the
  # collector has never acted OR the path moved; either way we cannot assert a
  # rate, and reporting "0 actions, all clear" would be a lie of exactly the
  # shape this canary exists to prevent.
  printf "STAGE=unstick-events-missing msg=no-events-dir-at-%s-cannot-assert-action-rate\n" "$EVENTS" >&2
  exit 2
fi

TODAY=$(date -u +%Y-%m-%d)
F="$EVENTS/$TODAY.jsonl"

if [ ! -f "$F" ]; then
  printf "PASS: unstick-rate - 0 destructive action(s) today (warn>=%s fail>=%s cap=%s)\n" \
    "$WARN_AT" "$FAIL_AT" "$CAP"
  exit 0
fi

# Count and summarise. A line that will not parse is a BROKEN audit trail, not
# a quiet one: exit 2 rather than undercount.
SUMMARY=$(python3 - "$F" <<PY 2>/dev/null
import json, sys, collections
n = 0
bad = 0
res = collections.Counter()
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        bad += 1
        continue
    if d.get("action") == "unstick":
        n += 1
        res[str(d.get("result"))] += 1
print(n)
print(bad)
print(",".join(f"{k}={v}" for k, v in sorted(res.items())) or "none")
PY
)
RC=$?
if [ "$RC" -ne 0 ] || [ -z "$SUMMARY" ]; then
  printf "STAGE=unstick-events-unreadable msg=could-not-parse-%s-rc-%s\n" "$F" "$RC" >&2
  exit 2
fi
N=$(printf "%s" "$SUMMARY" | sed -n 1p)
BAD=$(printf "%s" "$SUMMARY" | sed -n 2p)
BY=$(printf "%s" "$SUMMARY" | sed -n 3p)

if [ "${BAD:-0}" -gt 0 ]; then
  printf "STAGE=unstick-events-unreadable msg=%s-unparseable-line(s)-in-%s-refusing-to-undercount\n" \
    "$BAD" "$TODAY" >&2
  exit 2
fi

# Cap reached is its own finding, independent of the numeric thresholds: the cap
# is operator-tunable, and raising it must not silently raise the alarm too.
if [ "${N:-0}" -ge "${CAP:-10}" ]; then
  printf "STAGE=unstick-cap-reached msg=%s-destructive-action(s)-today-HIT-THE-DAILY-CAP-of-%s-results:%s-a-runaway-rule-looks-exactly-like-this\n" \
    "$N" "$CAP" "$BY" >&2
  exit 1
fi
if [ "${N:-0}" -ge "${FAIL_AT:-5}" ]; then
  printf "STAGE=unstick-rate-high msg=%s-destructive-action(s)-today-fail-threshold-%s-results:%s\n" \
    "$N" "$FAIL_AT" "$BY" >&2
  exit 1
fi
if [ "${N:-0}" -ge "${WARN_AT:-3}" ]; then
  printf "PASS-WARN: unstick-rate - %s destructive action(s) today (warn>=%s fail>=%s cap=%s) results:%s\n" \
    "$N" "$WARN_AT" "$FAIL_AT" "$CAP" "$BY"
  exit 0
fi
printf "PASS: unstick-rate - %s destructive action(s) today (warn>=%s fail>=%s cap=%s) results:%s\n" \
  "$N" "$WARN_AT" "$FAIL_AT" "$CAP" "$BY"
exit 0
')
RC=$?
echo "$RES"
exit $RC
