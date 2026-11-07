#!/usr/bin/env bash
# kuma-push-stall canary: how long does Kuma stop answering /api/push? (QFLX-104)
#
# Uptime Kuma's built-in clear-old-data job (cron 14 03 * * *, container-local)
# blocks /api/push for ~70s every night. The pusher trips its breaker on that
# timeout and appends one "open" and one "close" line per stall to
#   $MANITOBA_STATE_DIR/kuma-push-stalls.jsonl   (default ~/.opt/maint/)
#   {"event":"open","at":"<iso>"}
#   {"event":"close","at":"<iso>","opened_at":"<iso>","seconds":75.0,"deferred":31}
# This canary only READS that ledger and surfaces the stall length, so a stall
# that is growing is caught before it makes a push monitor go falsely DOWN.
#
# Budget: STALL_BUDGET_S=120. A 90s push monitor with maxretries=2 / retry 60s
# goes DOWN after ~210s of silence and the pusher cadence is ~65s, so a 120s
# stall still leaves headroom; the nightly ~70s one is green but visible in the
# message (stalls24h=1 max=75s budget=120s).
#
# RED when, inside the window (24h by "at"):
#   * any close has seconds >= budget, or
#   * the newest event is an open with no later close and it is older than the
#     budget (the stall is still going, or the pusher died mid-stall).
# Suppression is EARNED: "ledger absent" is green ONLY when the state dir
# exists and is readable+traversable; otherwise RED (kuma-stall-unreachable).
# Malformed lines are counted and reported (bad_lines=N), never skipped quietly.
#
# Exit 0 green / 1 red (STAGE=kuma-stall-over-budget | kuma-stall-ongoing |
# kuma-stall-unreachable) / 2 could-not-assert (kuma-stall-bad-config: a knob
# is not a positive number). Detect-only, read-only.
#
# Overrides (tests / tuning): QFLIX_CANARY_KUMA_STALL_DIR (state dir),
# QFLIX_CANARY_KUMA_STALL_BUDGET_S, QFLIX_CANARY_KUMA_STALL_WINDOW_H,
# QFLIX_CANARY_KUMA_STALL_NOW (epoch seconds). MANITOBA_STATE_DIR is honoured.
#
# Lives on the seedbox at ~/scripts/canaries/kuma-push-stall.sh (deployed by
# 240-maintenance-install.sh). Invoked by manitoba-maint-canary-kuma-push-stall,
# which pushes status=up/down to Kuma monitor "Canary Kuma Push Stall".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

# The body below sits inside sshm '...' and an UNQUOTED heredoc on the box:
# no apostrophes, no dollar signs, no backslashes in it.
RES=$(sshm '
python3 - <<PYEOF
import calendar, json, os, sys, time

E = os.environ.get
NL = chr(10)


def fail(stage, msg, rc):
    sys.stderr.write("STAGE=%s msg=%s%s" % (stage, msg, NL))
    sys.exit(rc)


def knob(name, default):
    raw = E(name)
    if raw is None or raw == "":
        return default
    try:
        val = float(raw)
    except ValueError:
        fail("kuma-stall-bad-config", "%s-is-not-a-number-cannot-assert" % name, 2)
    if not (val > 0):
        fail("kuma-stall-bad-config", "%s-must-be-positive-cannot-assert" % name, 2)
    return val


BUDGET = knob("QFLIX_CANARY_KUMA_STALL_BUDGET_S", 120.0)
WINDOW_H = knob("QFLIX_CANARY_KUMA_STALL_WINDOW_H", 24.0)
NOW = knob("QFLIX_CANARY_KUMA_STALL_NOW", time.time())
STATE_DIR = (E("QFLIX_CANARY_KUMA_STALL_DIR") or E("MANITOBA_STATE_DIR")
             or os.path.expanduser("~") + "/.opt/maint")
LEDGER = STATE_DIR + "/kuma-push-stalls.jsonl"
BUD = "%ds" % BUDGET


def parse_ts(text):
    """ISO-8601 UTC with optional fraction, e.g. 2026-11-07T02:14:30.1Z -> epoch."""
    if not isinstance(text, str) or not text.endswith("Z"):
        raise ValueError("timestamp")
    base, _, frac = text[:-1].partition(".")
    epoch = calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S"))
    return epoch + (float("0." + frac) if frac else 0.0)


# Earned suppression: absent is only benign if we could have SEEN it.
if not (os.path.isdir(STATE_DIR) and os.access(STATE_DIR, os.R_OK | os.X_OK)):
    fail("kuma-stall-unreachable",
         "state-dir-missing-or-unreadable-cannot-see-the-stall-ledger", 1)

events = []   # (at, kind, seconds, file_order)
bad = 0
absent = not os.path.exists(LEDGER)
if not absent:
    try:
        with open(LEDGER, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        fail("kuma-stall-unreachable", "ledger-exists-but-cannot-be-read", 1)
    for order, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict) or obj.get("event") not in ("open", "close"):
                raise ValueError("event")
            at = parse_ts(obj.get("at"))
            secs = None
            if obj["event"] == "close":
                secs = obj.get("seconds")
                if isinstance(secs, bool) or not isinstance(secs, (int, float)) or secs < 0:
                    raise ValueError("seconds")
                secs = float(secs)
            events.append((at, obj["event"], secs, order))
        except (ValueError, TypeError, OverflowError):
            bad += 1

cutoff = NOW - WINDOW_H * 3600
recent = [ev for ev in events if ev[0] >= cutoff]
closes = [ev for ev in recent if ev[1] == "close"]
count = len(closes)
worst = max([ev[2] for ev in closes] or [0.0])
trend = "stalls24h=%d max=%ds budget=%s bad_lines=%d" % (count, round(worst), BUD, bad)
if absent:
    trend += " ledger=absent"

if worst >= BUDGET:
    fail("kuma-stall-over-budget",
         "%s-a-stall-reached-the-budget-push-monitors-can-flap-DOWN" % trend.replace(" ", "-"), 1)

if recent:
    last = max(recent, key=lambda ev: (ev[0], ev[3]))
    if last[1] == "open" and NOW - last[0] > BUDGET:
        fail("kuma-stall-ongoing",
             "%s-open-for-%ds-with-no-close-kuma-still-not-answering-pushes"
             % (trend.replace(" ", "-"), round(NOW - last[0])), 1)

print("PASS: kuma-push-stall - %s" % trend)
PYEOF
')
RC=$?
echo "$RES"
exit $RC
