#!/usr/bin/env bash
# 40-validate-green.sh -- is green ready (pre) / healthy after the flip (post)?
#
# Spec section 8 row 40: "Add runtime-parity, every canary in muted mode, and an
# assertion of gate armed:false". Read-only; no --execute (nothing to gate),
# but the window guard still runs (a probe is a box operation).
#
# The verdict is computed LOCALLY by `migrate_manifest.py evaluate-green` from
# two JSON documents read off green, so the logic is unit-tested without SSH:
#   * green's own `manitoba-maint status --all --json` (every app's real health
#     probe + every canary's last result), checked against the app set of THIS
#     repo's manifest/apps.yaml -- an app missing from green fails;
#   * green_facts.py: gate drop-in, members.yaml `armed`, runtime-parity
#     violations in the pusher state, Discord webhook parked?, Tdarr threadcap
#     shim, media present, timer count;
#   * kuma_channels.py status: green's Kuma reachable and (pre) no monitor
#     paging a human / (post) every monitor paging one.
# Timer count is compared with 00-preflight's blue baseline, tolerating the
# comms timers I-1 holds.
#
# USAGE: 40-validate-green.sh NEW_HOST [--old-host HOST] [--post]
#        --post: the 50-cutover step-7 health gate (green loud, webhook live)
# EXIT:  0 every check passed | 1 >=1 check failed | 2 could-not-assert
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

usage() { echo "usage: $0 NEW_HOST [--old-host HOST] [--post]" >&2; }
mig_args "--post" "$@"
need_new_host
MODE=pre; [ -n "${FLAG[post]+x}" ] && MODE=post
resolve_old_host

sshg true >/dev/null 2>&1 || stage 2 green-unreachable "ssh-to-NEW_HOST-failed"
window_guard
echo "== 40-validate-green: $NEW_HOST (mode=$MODE) =="

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
sshg 'MANITOBA_MANIFEST=~/.opt/maint/apps.yaml ~/bin/manitoba-maint status --all --json' \
  > "$WORK/status.json" 2>/dev/null
"$PY" -c 'import json,sys; json.load(open(sys.argv[1]))' "$WORK/status.json" 2>/dev/null \
  || stage 2 status-unreadable "green-manitoba-maint-status--all--json-was-not-JSON"

LIBS_JSON="$(printf '"%s",' "${MEDIA_LIBRARIES[@]}")"
ARGS="{\"dropin\": \"$GATE_DROPIN_REL\", \"members\": \"$MEMBERS_REL\", \"webhook\": \"$DISCORD_WEBHOOK_SECRET\", \"media_root\": \"$MEDIA_ROOT_REL\", \"libraries\": [${LIBS_JSON%,}]}"
on_box g green_facts.py "$ARGS" > "$WORK/facts.raw" 2>/dev/null \
  || stage 2 facts-unreadable "green_facts.py-failed-on-green"
KUMA="$(on_box g kuma_channels.py status 2>/dev/null | tail -n 1)"
"$PY" - "$WORK/facts.raw" "$KUMA" > "$WORK/facts.json" <<'PY' || stage 2 facts-unreadable "facts-not-JSON"
import json, sys
f = json.loads(open(sys.argv[1]).read().strip().splitlines()[-1])
try:
    f["kuma"] = json.loads(sys.argv[2])
except ValueError:
    f["kuma"] = {"reachable": False, "error": "kuma_channels-no-json"}
print(json.dumps(f))
PY

# Tolerance, not a requirement: green may be short by the comms timers I-1
# holds (always before step 8 of the cutover, which runs after the post gate).
HELD="$(mm comms "${COMMS_JOBS[@]}" | awk -F'\t' '$2=="timer"' | grep -c .)"
"$PY" -c 'import json,sys
n = json.load(open(sys.argv[1])).get("notes") or {}
for k, v in sorted(n.items()): print("  [NOTE] %-34s %s" % (k, v))' "$WORK/facts.json"
mm evaluate-green --status "$WORK/status.json" --facts "$WORK/facts.json" --mode "$MODE" \
  --baseline "$MSTATE/migration-state.json" --held-timers "$HELD"
exit $?
