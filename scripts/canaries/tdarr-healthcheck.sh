#!/usr/bin/env bash
# Tdarr health-check canary: assert the health-check pipeline actually produces
# SUCCESSES, and that each library points at an engine whose binary exists.
#
# WHY: from 2026-05-21 to 2026-07-28 every single Tdarr health check failed —
# 2,866 failures over 54 days, 242 files parked at HealthCheck=Error, a literal
# 0% success rate (healthCheckScore 0.000) — and nothing alerted for 68 days.
# Root cause: the libraries carried Tdarr's stock `handbrakescan=true`, so the
# worker spawned `HandBrakeCLI -i <file> --scan`, and HandBrakeCLI does not
# exist on this rootless Ultra.cc slot:
#     Subworker:a.Error executing binary: HandBrakeCLI Error: spawn HandBrakeCLI ENOENT
# It stayed invisible because the failure is ORTHOGONAL to transcoding —
# transcodes use the bundled ffmpeg-static and were succeeding the whole time
# (1,168 successes over the same window), so the dashboard, the Kuma monitors,
# the scanner canary and the unit states all looked perfectly healthy. Nothing
# in the fleet was watching the one number that was wrong.
#
# Fixed 2026-07-28 by switching all libraries to `ffmpegscan` (the bundled
# ffmpeg-static that already carries every transcode; full-decodes at ~20x
# realtime here). This canary is the durable half: it makes a silent
# 0%-success health-check pipeline impossible to miss again.
#
# Three independent predicates, because a ratio alone is a lagging indicator
# AND a ratio can only ever describe checks that actually RAN:
#   1. ENGINE SANITY (leading, exact) — for every library with health checks on,
#      resolve the configured engine's binary. handbrakescan -> HandBrakeCLI,
#      ffmpegscan -> the node's ffmpeg. Missing binary, or neither flag set, is
#      an instant FAIL. This is the precise 2026-05-21 bug and it reds on the
#      first tick, before a single file has been mis-verdicted.
#   2. ERROR RATIO (lagging, statistical) — of the health checks that have
#      COMPLETED (not Queued), what fraction errored. A healthy library sits
#      near 0%; a genuinely corrupt file or two is normal, an engine that cannot
#      spawn is 100%. Needs MIN_SAMPLE completed checks first so a fresh library
#      with 3 checks can't trip it on noise.
#   3. PROGRESS / STALL (catches the wedge neither of the above can see) — a
#      ratio describes checks that RAN; nothing in it notices checks that
#      STOPPED running. Without this, a pipeline that dies while under
#      MIN_SAMPLE completed parks on the "not enough data to judge" branch and
#      reports PASS-WARN forever — i.e. this canary would reproduce the exact
#      green-while-dead failure it was built to prevent. Progress is persisted
#      to a state file; any increase in completed refreshes the clock. FAILs
#      after STALL_HOURS with a non-empty queue and a RUNNING node (the node is
#      intentionally stopped 18:00-23:00 UTC, and no progress then is correct,
#      so the threshold also sits above that 5h pause).
#
# Thresholds (override via env QFLIX_CANARY_TDARR_HC_*):
#   20% WARN → annotate Kuma msg, stay UP. Worth a look; could be real corruption.
#   50% FAIL → DOWN. At half the completed checks failing this is systemic,
#              not a bad file. No autonomous repair: the fix is a config change
#              (engine flag) or a missing binary, neither safe to guess at.
#   MIN_SAMPLE=20 completed checks before the ratio is judged at all.
#
# Stage labels (printed to stderr on failure -> Kuma `msg=`):
#   STAGE=tdarr-hc-engine-missing  — library points at an engine with no binary
#   STAGE=tdarr-hc-engine-unset    — neither scan flag set: health checks no-op
#   STAGE=tdarr-hc-error-ratio     — completed checks are >=FAIL% errors
#   STAGE=tdarr-hc-stalled         — queue non-empty, node up, no new completed
#                                    check in STALL_HOURS: pipeline wedged
#
# Deliberately NOT a failure (stays UP, says so) — these are indeterminate or
# operator choices, and a red here would be exactly the noise the 2026-07-28
# work removed:
#   - tdarr-server inactive: the tdarr-scanner canary owns that red. Two reds
#     for one cause is correlated noise.
#   - health checks disabled on every library: a legitimate operator decision.
#   - no completed checks yet (fresh install / everything still Queued).
#
# Exit:
#   0 — healthy, or WARN, or indeterminate
#   1 — engine broken or error ratio past FAIL
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

WARN_PCT=${QFLIX_CANARY_TDARR_HC_WARN_PCT:-20}
FAIL_PCT=${QFLIX_CANARY_TDARR_HC_FAIL_PCT:-50}
MIN_SAMPLE=${QFLIX_CANARY_TDARR_HC_MIN_SAMPLE:-20}
# Hours with a non-empty queue, a running node, and ZERO new completed checks
# before we call the pipeline wedged. Must exceed the 5h quiet-hours pause so a
# normal pause can never look like a stall; 8h leaves margin either side.
STALL_HOURS=${QFLIX_CANARY_TDARR_HC_STALL_HOURS:-8}
# Overridable so the fail/warn/indeterminate branches can be exercised against
# fixtures instead of only ever the one state the live box happens to be in.
DB_ROOT=${QFLIX_CANARY_TDARR_HC_DB:-}
STATE_PATH=${QFLIX_CANARY_TDARR_HC_STATE:-}

# Config is interpolated in the double-quoted half; the analysis body is single
# quoted so the embedded Python needs no backslash gymnastics.
RES=$(sshm "
set -uo pipefail
export HC_WARN='${WARN_PCT}' HC_FAIL='${FAIL_PCT}' HC_MIN='${MIN_SAMPLE}' HC_DB='${DB_ROOT}'
export HC_STALL_HOURS='${STALL_HOURS}' HC_STATE='${STATE_PATH}'
STATE=\$(systemctl --user is-active tdarr-server.service 2>/dev/null)
export HC_SERVER=\"\${STATE:-unknown}\"
NSTATE=\$(systemctl --user is-active tdarr-node.service 2>/dev/null)
export HC_NODE=\"\${NSTATE:-unknown}\"
export HC_NOW=\$(date -u +%s)
"'
python3 - <<PYEOF
import json, glob, os, shutil, sys

warn_pct = float(os.environ.get("HC_WARN", "20"))
fail_pct = float(os.environ.get("HC_FAIL", "50"))
min_sample = int(os.environ.get("HC_MIN", "20"))
server = os.environ.get("HC_SERVER", "unknown")
db = os.environ.get("HC_DB") or os.path.expanduser("~/.apps/tdarr/server/Tdarr/DB2")

def out(msg):
    print(msg)
    sys.exit(0)

def fail(stage, msg):
    sys.stderr.write("STAGE=%s msg=%s\n" % (stage, msg))
    sys.exit(1)

# The tdarr-scanner canary owns the server-down red; do not double-report it.
if server != "active":
    out("PASS-WARN: tdarr-server-%s-healthcheck-state-indeterminate" % server)

libs = sorted(glob.glob(os.path.join(db, "LibrarySettingsJSONDB", "*.json")))
if not libs:
    out("PASS-WARN: tdarr-no-library-records-state-indeterminate")

# ---- predicate 1: engine sanity (leading, exact) ----------------------------
# ffmpeg ships inside the node; a bare name is resolved against PATH the same
# way Node spawn() would, which is exactly how HandBrakeCLI failed ENOENT.
node_ffmpeg = os.path.expanduser(
    "~/.apps/tdarr/Tdarr_Node/node_modules/ffmpeg-static/ffmpeg")

def resolvable(binary):
    if os.path.sep in binary:
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return shutil.which(binary) is not None

active, broken, unset = [], [], []
for path in libs:
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (ValueError, OSError):
        continue
    name = doc.get("name") or "?"
    if not doc.get("processHealthChecks"):
        continue
    active.append(name)
    if doc.get("handbrakescan"):
        engine, binary = "handbrake", "HandBrakeCLI"
    elif doc.get("ffmpegscan"):
        engine, binary = "ffmpeg", node_ffmpeg
    else:
        unset.append(name)
        continue
    if not resolvable(binary):
        broken.append("%s:%s(%s)" % (name, engine, os.path.basename(binary)))

if broken:
    fail("tdarr-hc-engine-missing",
         "engine-binary-absent-" + "+".join(broken)
         + "-EVERY-HEALTHCHECK-WILL-SPAWN-FAIL-ENOENT")
if unset:
    fail("tdarr-hc-engine-unset",
         "no-scan-engine-selected-" + "+".join(unset)
         + "-healthchecks-are-a-silent-noop")
if not active:
    out("PASS-WARN: tdarr-healthchecks-disabled-on-all-libraries-operator-choice")

# ---- predicate 2: error ratio (lagging, statistical) ------------------------
counts = {}
for path in glob.glob(os.path.join(db, "FileJSONDB", "*.json")):
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (ValueError, OSError):
        continue
    state = doc.get("HealthCheck")
    if state:
        counts[state] = counts.get(state, 0) + 1

queued = counts.get("Queued", 0)
errored = counts.get("Error", 0)
completed = sum(v for k, v in counts.items() if k != "Queued")
libstr = "libs=" + ",".join(sorted(active))

# ---- predicate 3: progress (catches a WEDGE the ratio can never see) --------
# Without this, a pipeline that stops dead sits on the "not enough completed to
# judge" branch below and reports PASS-WARN forever — the exact
# green-while-dead shape that cost 68 days. A ratio only speaks about checks
# that RAN; nothing else here notices checks that stopped running. Progress is
# tracked in a tiny state file: any increase in the completed count refreshes
# the timestamp, so a stall is measured from the last real work, not from boot.
# NOTE: this heredoc is unquoted (it must be, to sit inside the single-quoted
# outer string), so the remote shell still expands $ and backticks in here.
# Keep both out of this Python body or the shell executes them.
now = int(os.environ.get("HC_NOW") or 0)
node = os.environ.get("HC_NODE", "unknown")
stall_s = float(os.environ.get("HC_STALL_HOURS", "8")) * 3600
state_path = os.environ.get("HC_STATE") or os.path.expanduser(
    "~/.opt/maint/tdarr-healthcheck/state.json")

prev = {}
try:
    with open(state_path) as fh:
        prev = json.load(fh)
except (ValueError, OSError):
    prev = {}

last_completed = prev.get("completed")
last_progress = prev.get("last_progress_ts") or now
if last_completed is None or completed > last_completed:
    last_progress = now
try:
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"completed": completed, "last_progress_ts": last_progress,
                   "queued": queued, "updated_ts": now}, fh)
    os.replace(tmp, state_path)
except OSError:
    pass  # never let bookkeeping break the probe

stalled_s = now - last_progress
# Only judge while the node is actually up: it is intentionally stopped
# 18:00-23:00 UTC (fair-use quiet hours) and no progress then is correct.
if queued > 0 and node == "active" and stalled_s > stall_s:
    fail("tdarr-hc-stalled",
         "no-new-completed-checks-in-%.1fh-queued=%d-completed=%d-node=active-"
         "PIPELINE-WEDGED-not-merely-slow-%s"
         % (stalled_s / 3600.0, queued, completed, libstr))

progress = "completed=%d-queued=%d-idle=%.1fh-node=%s" % (
    completed, queued, stalled_s / 3600.0, node)

if completed < min_sample:
    # Still UP — but the stall predicate above now owns the wedge case, so this
    # branch can no longer stay quiet forever on a dead pipeline.
    out("PASS-WARN: tdarr-hc-only-%d-completed-checks-need-%d-to-judge-%s-%s"
        % (completed, min_sample, progress, libstr))

ratio = (errored * 100.0) / completed
tail = ("errors=%d/%d-%.1f%%-queued=%d-idle=%.1fh-warn=%.0f%%-fail=%.0f%%-%s"
        % (errored, completed, ratio, queued, stalled_s / 3600.0,
           warn_pct, fail_pct, libstr))

if ratio >= fail_pct:
    fail("tdarr-hc-error-ratio", tail + "-SYSTEMIC-not-bad-files")
if ratio >= warn_pct:
    out("PASS-WARN: tdarr-hc-" + tail)
out("PASS: tdarr-hc-" + tail)
PYEOF
') || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC
