#!/usr/bin/env bash
# tdarr-transcode-stall canary: is the TRANSCODE half of the pipeline actually
# doing work, or is it idle with a backlog in front of it?
#
# WHY THIS EXISTS
# ---------------
# On 2026-08-23, after a routine `systemctl --user restart tdarr-server
# tdarr-node`, every transcode worker died the instant it was handed a job:
#
#   [FATAL] Tdarr_Node - Error: EACCES: permission denied, mkdir
#   '/tdarr-workDir-node-YjouEnw6d-worker-lame-loris-ts-1787514583085'
#   Worker lame-loris exited with code 1 and signal null
#   Worker lame-loris disconnected. Pruning.
#
# Three of the five libraries carried `cache: ""`, and Tdarr concatenates that
# with "/tdarr-workDir-..." — producing an absolute path at the FILESYSTEM
# ROOT, which a rootless Ultra.cc slot can never create. The node then pruned
# the worker and never retried. Transcoding was 100% dead.
#
# EVERY EXISTING SURFACE STAYED GREEN THROUGH IT:
#   * "Tdarr" and "Tdarr Node" — the server and the node are both up and the
#     node is registered. It is the WORKER child that dies.
#   * tdarr-healthcheck — health checks run on a different code path and kept
#     completing normally, so its stall predicate never armed.
#   * tdarr-transcode-error — a file whose worker died never reaches
#     TranscodeDecisionMaker=Error. It stays Queued. The parked population was
#     genuinely 0, and the canary correctly said so.
#   * tdarr-throttle-integrity — the CAP was intact. Two worker slots that are
#     allowed and never used look exactly like two worker slots at rest.
#   * timer-liveness, deploy-drift, thread-ceiling — all orthogonal.
#
# So the fleet could sit at 76/76 green with the transcode pipeline stone dead
# and a backlog in front of it. That is the same green-while-dead shape that
# cost 68 days on the HandBrake health-check bug, and it is why this is a
# separate canary rather than another predicate bolted onto an existing one:
# the operator design law is one concern per module, independently tunable.
#
# THE PREDICATE
# -------------
# FAIL when ALL of these hold:
#   1. the transcode BACKLOG is non-empty (>=1 record at
#      TranscodeDecisionMaker=Queued), so there is work to do;
#   2. a node is registered, not paused, and its transcodecpu limit is >0, so
#      the node is supposed to be doing it;
#   3. ZERO transcode workers are currently busy, so nothing is in flight;
#   4. no transcode has REACHED A TERMINAL VERDICT ("Transcode success" or
#      "Transcode error") in STALL_HOURS.
#
# Condition 3 is what makes this safe to run against long jobs. A 5 GB HEVC
# feature can hold a worker for hours while `completed` does not move, and that
# is healthy — but a busy worker means condition 3 is false and the canary
# stays green. Idle workers plus a queue plus no completions is not slow, it is
# stopped.
#
# Condition 4 is measured from the last real completion, persisted to a state
# file, exactly like the tdarr-healthcheck stall predicate. Boot does not reset
# it, so a restart loop cannot hide a stall.
#
# GHOST RECORDS ARE EXCLUDED, AND THE EXCLUSION IS EARNED. A Queued record for
# a file that no longer exists can never be worked and would hold the backlog
# above zero forever (see the 2026-08-23 ghost incident and the same guard in
# tdarr-healthcheck.sh). A record only counts as a ghost when its DIRECTORY was
# readable and the file was not in it — "absent" and "I could not look" are the
# same os.path.exists() answer and want opposite verdicts, and an unreadable
# media tree must never be able to empty the backlog into a false green.
#
# THRESHOLD. 3h default. Not tuned to how long a transcode takes — condition 3
# already handles that — but to how long an idle-with-backlog state may
# plausibly be a scheduling artifact (the server stages on its own scan cycle)
# rather than a fault. The real incident was dead within 90 seconds of the
# restart and would have stayed dead indefinitely.
#
# EXIT CODES
#   0 - working, or idle with nothing to do, or node paused (operator choice),
#       or not enough history to judge
#   1 - tdarr-transcode-stalled: backlog + idle workers + no completions
#   2 - could not assert: server unreachable, no registered node, FileJSONDB
#       missing or empty. Empty-because-broken must never read as
#       empty-because-clean.
#
# Overrides: QFLIX_CANARY_TDARR_TS_STALL_HOURS (default 3),
#            QFLIX_CANARY_TDARR_TS_DB, QFLIX_CANARY_TDARR_TS_STATE.
#
# Lives on the seedbox at ~/scripts/canaries/tdarr-transcode-stall.sh (deployed
# by 240-maintenance-install.sh). Invoked by
# manitoba-maint-canary-tdarr-transcode-stall, which pushes status=up/down to
# Kuma monitor "Canary Tdarr Transcode Stall".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

STALL_HOURS=${QFLIX_CANARY_TDARR_TS_STALL_HOURS:-3}
DB_ROOT=${QFLIX_CANARY_TDARR_TS_DB:-}
STATE_PATH=${QFLIX_CANARY_TDARR_TS_STATE:-}

RES=$(sshm "
set -uo pipefail
export TS_STALL_HOURS='${STALL_HOURS}' TS_DB='${DB_ROOT}' TS_STATE='${STATE_PATH}'
export TS_NOW=\$(date -u +%s)
CONF=\$HOME/.apps/tdarr/configs/Tdarr_Server_Config.json
P=\$(grep -oP '\"serverPort\":\s*\"?\K[0-9]+' \"\$CONF\" 2>/dev/null | head -1)
export TS_PORT=\${P:-42018}
export TS_NODES=\$(curl -sfm 10 \"http://127.0.0.1:\${TS_PORT}/api/v2/get-nodes\" 2>/dev/null)
"'
python3 - <<PYEOF
import json, glob, os, sys

now = int(os.environ.get("TS_NOW") or 0)
stall_s = float(os.environ.get("TS_STALL_HOURS", "3")) * 3600
db = os.environ.get("TS_DB") or os.path.expanduser(
    "~/.apps/tdarr/server/Tdarr/DB2")
state_path = os.environ.get("TS_STATE") or os.path.expanduser(
    "~/.opt/maint/tdarr-transcode-stall/state.json")
port = os.environ.get("TS_PORT", "42018")


def out(msg):
    print(msg)
    sys.exit(0)


def fail(stage, msg):
    sys.stderr.write("STAGE=%s msg=%s\n" % (stage, msg))
    sys.exit(1)


def cannot(stage, msg):
    sys.stderr.write("STAGE=%s msg=%s\n" % (stage, msg))
    sys.exit(2)


# ---- the node: is anything even supposed to be transcoding? ----------------
raw = os.environ.get("TS_NODES") or ""
if not raw.strip():
    cannot("tdarr-ts-server-unreachable",
           "get-nodes-empty-on-port-%s-cannot-assert-transcode-progress" % port)
try:
    nodes = json.loads(raw)
except ValueError:
    cannot("tdarr-ts-nodes-unparseable", "get-nodes-returned-non-json")
if not isinstance(nodes, dict) or not nodes:
    cannot("tdarr-ts-no-nodes",
           "server-up-but-zero-registered-nodes-cannot-assert-transcode-progress")

capacity = 0
busy = 0
paused_all = True
names = []
for key, node in nodes.items():
    node = node or {}
    names.append(str(node.get("nodeName") or key))
    if not node.get("nodePaused"):
        paused_all = False
    limits = node.get("workerLimits") or {}
    for k in ("transcodecpu", "transcodegpu"):
        try:
            capacity += int(limits.get(k) or 0)
        except (TypeError, ValueError):
            pass
    for worker in (node.get("workers") or {}).values():
        if str((worker or {}).get("workerType") or "").startswith("transcode"):
            busy += 1

nodestr = "nodes=" + ",".join(sorted(names))

# A paused node is an operator decision, not a fault. Say so and stay up.
if paused_all:
    out("PASS-WARN: tdarr-transcode-stall-all-nodes-paused-operator-choice-%s"
        % nodestr)
if capacity <= 0:
    out("PASS-WARN: tdarr-transcode-stall-zero-transcode-worker-capacity-%s"
        % nodestr)

# ---- the backlog and the progress clock ------------------------------------
files = glob.glob(os.path.join(db, "FileJSONDB", "*.json"))
if not files:
    cannot("tdarr-ts-filedb-empty",
           "zero-FileJSONDB-records-at-%s-db-moved-or-glob-broke" % db)

queued = 0
completed = 0
ghosts = []
unreachable = 0


def is_ghost(src):
    """True only when the DIRECTORY was readable and the file was not in it.

    Absence and unreadability are the same os.path.exists() answer and want
    opposite verdicts: an unmounted or permission-lost media tree would
    otherwise reclassify the whole backlog as ghosts, empty it, and hold this
    canary green on a stopped pipeline. Suppression is earned by evidence.
    """
    parent = os.path.dirname(src) or "/"
    if not os.path.isdir(parent) or not os.access(parent, os.R_OK | os.X_OK):
        return False
    return not os.path.exists(src)


for path in files:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (ValueError, OSError):
        continue
    state = doc.get("TranscodeDecisionMaker")
    if state in ("Transcode success", "Transcode error"):
        completed += 1
        continue
    if state != "Queued":
        continue
    src = doc.get("_id") or doc.get("file") or ""
    if src and not os.path.exists(src):
        if is_ghost(src):
            ghosts.append(src.rsplit("/", 1)[-1][:70])
            continue
        unreachable += 1
    queued += 1

tail = "queued=%d-busy=%d/%d-completed=%d-%s" % (
    queued, busy, capacity, completed, nodestr)
if ghosts:
    tail += "-ghosts=%d-first=%s" % (len(ghosts), ghosts[0])
if unreachable:
    tail += "-unreachable=%d-MEDIA-TREE-NOT-READABLE" % unreachable

prev = {}
try:
    with open(state_path, encoding="utf-8") as fh:
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
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"completed": completed, "last_progress_ts": last_progress,
                   "queued": queued, "busy": busy, "updated_ts": now}, fh)
    os.replace(tmp, state_path)
except OSError:
    pass  # bookkeeping must never break the probe

idle_h = (now - last_progress) / 3600.0

# Work in flight: the pipeline is alive by definition, however long the job.
if busy > 0:
    out("PASS: tdarr-transcode-stall-working-%s-idle=%.1fh" % (tail, idle_h))
# Nothing to do: idle is the correct state.
if queued == 0:
    out("PASS: tdarr-transcode-stall-backlog-empty-%s-idle=%.1fh"
        % (tail, idle_h))
# Backlog, capacity, nothing running, nothing finishing.
if (now - last_progress) > stall_s:
    fail("tdarr-transcode-stalled",
         "backlog=%d-with-%d-free-worker-slot(s)-and-no-transcode-completed-in-"
         "%.1fh-WORKERS-ARE-NOT-PICKING-UP-WORK-check-node.log-for-worker-exit-"
         "%s" % (queued, capacity - busy, idle_h, tail))

out("PASS: tdarr-transcode-stall-backlog=%d-awaiting-pickup-idle=%.1fh<%.0fh-%s"
    % (queued, idle_h, stall_s / 3600.0, tail))
PYEOF
') || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC
