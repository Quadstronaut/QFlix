#!/usr/bin/env bash
# ucc-gate-stuck canary: catch the UCC maintenance-gate DETECTOR itself
# wedging, independent of whatever recovery-suppression consumes its output.
#
# WHY this exists (audit finding, 2026-07-29): lib/ucc.py's `detect()` state
# machine has a "probe-error" branch that holds the prior `active` flag and
# only increments `consecutive_error` -- before the same-day fix, that
# counter had NO upper bound anywhere in the file, and a production instance
# was found stuck at consecutive_error == 128 (~10.6h at the 5-min
# manitoba-maint-ucc-detect.timer cadence). lib/suppression.py's
# ucc_active()/recovery_suppressed() read that frozen `active` flag on
# EVERY webhook down-event, so a probe that merely couldn't reach the host
# (not necessarily a real UCC gate) silently disabled auto-recovery for
# every ucc-class app, fleet-wide, for as long as the probe stayed broken --
# with nothing watching for it.
#
# The same-day fix caps the hold (UCC_PROBE_ERROR_CAP = 3 consecutive
# errors, 15 min) and fails OPEN (forces active -> False) once past it. This
# canary is the second, independent leg of that fix (per the standing design
# law: every maintenance concern gets its own module/timer/Kuma check) --
# it does NOT re-check suppression behavior, it watches the ucc-window.json
# STATE FILE directly for the two ways the detector itself can still be
# unhealthy even after the fail-open cap exists:
#
#   1. consecutive_error kept climbing well past the fail-open cap -- the
#      cap stops the SUPPRESSION damage, but a probe that stays broken for
#      an hour+ means the detection pipeline itself is dark (SSH broken,
#      app-manager renamed/removed, secrets/ucc.probe_app misconfigured) and
#      nobody would otherwise notice: the one Discord notify at fail-open is
#      a single edge event, not a repeating page.
#   2. `active` has been continuously true far longer than any real UCC
#      provider maintenance window plausibly runs -- defense in depth against
#      a DIFFERENT bug class (e.g. a future regression in the gated/clear
#      branches, not the probe-error path the same-day fix covers) still
#      being able to freeze the gate on indefinitely.
#
# Stage labels (stderr on failure -> Kuma `msg=`):
#   ucc-probe-error-stuck  — consecutive_error exceeds the threshold: the
#                            probe (`app-<name> start`) has been erroring for
#                            far longer than the fail-open cap allows for.
#   ucc-active-stuck       — active has been continuously true longer than
#                            any real UCC maintenance window would last.
#   ucc-state-malformed    — ucc-window.json exists but isn't the shape
#                            lib/ucc.py writes (parse failure, non-object
#                            root, or active=true with no first_detected_at)
#                            -- something OTHER than ucc.py touched/corrupted
#                            it, or ucc.py itself regressed.
#   ucc-state-unreadable   — state file exists but couldn't be read (permissions)
#
# A MISSING state file is NOT a failure here: it means ucc-detect has never
# recorded a window (fresh install, or the gate has literally never fired) --
# a distinct concern (timer liveness) that belongs to its own canary per the
# design law, not this one.
#
# Deliberately reads the state file directly rather than importing lib.ucc --
# this canary must keep working (and catching a wedged detector) even if a
# future bug in lib/ucc.py itself makes the module unimportable or its own
# `status()` call misbehave. It only assumes the on-disk JSON shape
# lib/ucc.py's write_state() has always produced.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
STATE_FILE="$HOME/.opt/maint/ucc-window.json"

# 12 cycles (~1h at the 5-min probe cadence) -- well past the
# UCC_PROBE_ERROR_CAP lib/ucc.py itself enforces (3 cycles / 15min, which
# already fails OPEN by then).
# This threshold only fires when the probe has STILL not produced a single
# gated/clear result a full hour after fail-open should have kicked in --
# i.e. the detection pipeline itself, not just the suppression it feeds, is
# dark.
ERROR_THRESHOLD=${QFLIX_CANARY_UCC_GATE_ERROR_THRESHOLD:-12}

# 6h ceiling: real UCC (Ultra.cc host) maintenance has been observed on the
# order of minutes to a few hours (the 2026-05-24 incident that prompted
# sub-project A); the separate QFlix-owned weekly window (lib/window.py) is
# a bounded 4h Monday slot. Nothing legitimate should hold the upstream gate
# continuously active for 6h+ -- if it ever genuinely does, that is itself
# worth an operator page, not silence.
MAX_ACTIVE_HOURS=${QFLIX_CANARY_UCC_GATE_MAX_ACTIVE_HOURS:-6}

python3 - "$STATE_FILE" "$ERROR_THRESHOLD" "$MAX_ACTIVE_HOURS" <<"PYEOF"
import sys
import json
import datetime as dt


def main():
    path, err_threshold_s, max_hours_s = sys.argv[1], sys.argv[2], sys.argv[3]
    err_threshold = int(err_threshold_s)
    max_hours = float(max_hours_s)

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        # No window ever recorded -- benign (see header: a distinct concern
        # belonging to a different canary, not this one).
        print("PASS: ucc-gate-stuck no-state-file (no window recorded yet)")
        return 0
    except Exception as exc:
        print(f"STAGE=ucc-state-unreadable msg={type(exc).__name__}", file=sys.stderr)
        return 1

    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        print(f"STAGE=ucc-state-malformed msg=json-parse-error-{type(exc).__name__}",
              file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print(f"STAGE=ucc-state-malformed msg=not-an-object-type={type(data).__name__}",
              file=sys.stderr)
        return 1

    active = bool(data.get("active", False))
    consecutive_error = int(data.get("consecutive_error", 0) or 0)
    first_detected_at = data.get("first_detected_at")

    # Condition 1: probe has been broken far longer than the fail-open cap
    # allows for -- the cap already stopped the suppression damage, this
    # catches the detector itself staying dark.
    if consecutive_error > err_threshold:
        print(
            f"STAGE=ucc-probe-error-stuck "
            f"msg=consecutive_error={consecutive_error}-over-threshold={err_threshold}",
            file=sys.stderr,
        )
        return 1

    if not active:
        print(f"PASS: ucc-gate-stuck active=False consecutive_error={consecutive_error}/{err_threshold}")
        return 0

    # active=True: lib/ucc.py always sets first_detected_at on the clear->active
    # flip (see detect()). Its absence means something wrote/corrupted the
    # file outside that path.
    if not isinstance(first_detected_at, str) or not first_detected_at:
        print("STAGE=ucc-state-malformed msg=active-true-without-first_detected_at",
              file=sys.stderr)
        return 1

    try:
        started = dt.datetime.fromisoformat(first_detected_at.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=dt.timezone.utc)
    except Exception as exc:
        print(f"STAGE=ucc-state-malformed msg=bad-first_detected_at-{type(exc).__name__}",
              file=sys.stderr)
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    elapsed_h = (now - started).total_seconds() / 3600.0

    # Condition 2: defense-in-depth ceiling on how long the gate can
    # plausibly stay active for real.
    if elapsed_h >= max_hours:
        print(
            f"STAGE=ucc-active-stuck "
            f"msg=active-for-{elapsed_h:.1f}h-over-ceiling-{max_hours:.0f}h",
            file=sys.stderr,
        )
        return 1

    print(
        f"PASS: ucc-gate-stuck active=True elapsed={elapsed_h:.1f}h/{max_hours:.0f}h "
        f"consecutive_error={consecutive_error}/{err_threshold}"
    )
    return 0


try:
    sys.exit(main())
except Exception as exc:  # boundary of last resort -- never an uncaught traceback
    print(f"STAGE=ucc-state-malformed msg=unhandled-{type(exc).__name__}", file=sys.stderr)
    sys.exit(1)
PYEOF
')
RC=$?
echo "$RES"
exit $RC
