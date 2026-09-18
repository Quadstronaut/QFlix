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
# SECOND ACTOR, ADDED 2026-09-17: the arr-regrab loop guard
# ---------------------------------------------------------
# Everything above watches ONE actor -- qflix-collect.py -> unstick.py, via the
# events JSONL it writes. `scripts/maint/arr-housekeeping.py --unstick` runs
# HOURLY and issues the identical destructive call (DELETE /queue/{id}
# ?removeFromClient=true&blocklist=true), up to 10 per run, and it was
# completely invisible here: it writes no events JSONL, so the most-watched
# destructive action class on this stack had a second, busier actor nobody was
# counting. That blind spot is what this sub-check closes.
#
# It does NOT feed arr-housekeeping action counts into the WARN=3/FAIL=5 daily
# counters above, and must not. Those were calibrated for one actor bounded by
# MAX_ACTIONS_PER_DAY; arr-housekeeping does up to 10 per run, every hour.
# Blending them would red this monitor permanently, which gets it muted, which
# is strictly worse than the blind spot it was meant to close.
#
# What it watches instead is the arr-regrab loop guard PARKED POPULATION
# (~/.opt/maint/arr-regrab-ledger.json). A park means the guard unmonitored an
# episode or movie because the same title was blocklisted N times in M hours:
# member-facing, terminal until a human acts, and the count of them is the one
# number that says whether the guard is holding a handful of hopeless titles
# or quietly unmonitoring the library.
#
# MISSING FILE PASSES -- DELIBERATELY THE OPPOSITE OF THE EVENTS-DIR RULE
# above, and the asymmetry is the point. The events directory is created by the
# actor before its first write, so its absence means the actor moved or broke.
# The regrab ledger is written only when the guard has something to remember:
# a stack with no re-grab loops never creates it, and that cold start is the
# documented NORMAL state, possibly for months. Exiting 2 on it would page the
# operator daily for a healthy system and would be using missing data as an
# interlock -- an operator directive in its own right. A ledger that
# EXISTS but will not parse is the empty-because-broken case and still exits 2.
#
# EXIT CODES
#   0 - under the warn threshold (or a clean day with zero actions)
#   1 - unstick-rate-high / unstick-cap-reached / regrab-parked-population
#   2 - could not assert: events dir missing, or a line that will not parse,
#       or a regrab ledger that exists and will not parse.
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

# --- sub-check: arr-regrab loop guard parked population ------------------
# Independently tunable, independently sourced, and deliberately NOT mixed
# into the action counters below (see the header). Runs first and
# short-circuits: a parked population or a broken ledger is a finding in its
# own right, and a canary reports one finding at a time.
REGRAB_LEDGER=${QFLIX_CANARY_REGRAB_LEDGER:-$HOME/.opt/maint/arr-regrab-ledger.json}
REGRAB_PARK_FAIL=${QFLIX_CANARY_REGRAB_PARK_FAIL:-10}

# Absent ledger = cold start = PASS, silently. See the header for why this is
# the opposite of the events-dir rule directly below it.
if [ -f "$REGRAB_LEDGER" ]; then
python3 - "$REGRAB_LEDGER" "$REGRAB_PARK_FAIL" <<PY
import json, sys
path, fail_at = sys.argv[1], int(sys.argv[2])
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("payload is a " + type(data).__name__ + ", not an object")
except Exception as exc:
    sys.stderr.write(
        "STAGE=regrab-ledger-unreadable msg=ledger-%s-exists-but-will-not-parse-%s-%s\n"
        % (path, type(exc).__name__, str(exc)[:80].replace(" ", "-")))
    raise SystemExit(2)
parked = sorted(k for k, v in data.items()
                if isinstance(v, dict) and v.get("parked"))
n = len(parked)
if n >= fail_at:
    sys.stderr.write(
        "STAGE=regrab-parked-population msg=%s-item(s)-unmonitored-by-the-arr-regrab-loop-guard-threshold-%s-each-needs-a-human-sample:%s\n"
        % (n, fail_at, ",".join(parked[:5]) or "none"))
    raise SystemExit(1)
print("PASS: unstick-rate regrab sub-check - %s item(s) parked by the "
      "re-grab loop guard (fail>=%s, ledger=%s)" % (n, fail_at, path))
PY
REGRAB_RC=$?
if [ "$REGRAB_RC" -ne 0 ]; then
  exit "$REGRAB_RC"
fi
fi

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
