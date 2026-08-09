#!/usr/bin/env bash
# SAB-stall canary: detect a silently-dead Usenet pipeline.
#
# Failure mode this guards (gap found 2026-07-19): SABnzbd's process + web UI
# stay up (app monitor green) while every queued job sits at 0 KB/s — expired
# Frugal block account, dead server credentials, provider outage, or an
# article-fetch wedge. Torrent stalls surface through qBit states + the
# collector's stale-state loop; a Usenet stall had NO detector at all.
#
# Mirror of qbit-stall: fires when queue speed is ~0 for >= threshold while
# non-paused slots are waiting. Wallclock via state file
# ~/.opt/maint/sab-stall-since.epoch across runs.
#
# Stage labels (stderr on failure -> Kuma `msg=`):
#   sab-up-fail        — SAB API unreachable / bad JSON
#   sab-stalled        — kbpersec ~0 for >= threshold with active slots waiting
#   sab-paused-pinned  — slot-level Paused job(s) sitting >= 24h while the
#                        queue itself is unpaused. Nothing on this stack
#                        legitimately slot-pauses; the 2026-07-19 incident
#                        (2 jobs pinned Paused since 07-16 by a wedged SAB
#                        queue object, resume API a silent no-op, restart
#                        no help) sat invisible for 3 days in exactly this
#                        state. Remediation that worked: *arr-side unstick
#                        (delete + blocklist + auto re-search).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
PORT=$(cat ~/secrets/sabnzbd.port)
KEY=$(cat ~/secrets/sabnzbd.key)
STATE_DIR=~/.opt/maint
STATE_FILE="$STATE_DIR/sab-stall-since.epoch"
PAUSED_STATE_FILE="$STATE_DIR/sab-paused-since.epoch"
# 600s: Usenet queues legitimately idle at 0 KB/s briefly between jobs and
# during par2 verification handoff; 10 min of zero with work waiting is real.
STALL_THRESHOLD_SEC=${QFLIX_CANARY_SAB_STALL_THRESHOLD_SEC:-600}
# 24h: no human/automation slot-pauses on this stack — a day-old paused slot
# is a wedged queue object, not intent.
PAUSED_THRESHOLD_SEC=${QFLIX_CANARY_SAB_PAUSED_THRESHOLD_SEC:-86400}
mkdir -p "$STATE_DIR"

Q=$(curl -sf -m 8 "http://127.0.0.1:${PORT}/api?mode=queue&output=json&apikey=${KEY}")
[ -n "$Q" ] || { printf "STAGE=sab-up-fail msg=queue-api-empty\n" >&2; exit 1; }

# Payload via temp file, NOT a pipe — `python3 -` reads its SCRIPT from
# stdin, so piping the JSON in as well hands python the heredoc as data
# (the qbit-stall canary hit the same trap; same fix).
printf "%s" "$Q" > /tmp/sab-canary-queue.json
# SLOTS counts only ACTIVE work (slot status != Paused). Slot-level-paused
# jobs are administrative (duplicate hold, operator pause) — they can sit
# for days legitimately and must not read as a pipeline stall (the 2
# paused slots present at rollout would have false-fired this canary).
read KBPS SLOTS PAUSED PAUSED_SLOTS < <(python3 - <<"PY"
import json
q = json.load(open("/tmp/sab-canary-queue.json")).get("queue") or {}
kbps = float(q.get("kbpersec") or 0)
slots = q.get("slots") or []
paused_slots = sum(1 for s in slots
                   if (s.get("status") or "").lower() == "paused")
print(int(kbps), len(slots) - paused_slots, 1 if q.get("paused") else 0,
      paused_slots)
PY
) || { printf "STAGE=sab-up-fail msg=queue-json-parse\n" >&2; exit 1; }
rm -f /tmp/sab-canary-queue.json

NOW=$(date +%s)

# Pinned-paused detector (independent of the speed stall): slot-level Paused
# job(s) while the queue is unpaused, persisting across a full day.
if [ "$PAUSED" = "0" ] && [ "$PAUSED_SLOTS" -gt 0 ]; then
  if [ ! -f "$PAUSED_STATE_FILE" ]; then
    echo "$NOW" > "$PAUSED_STATE_FILE"
  else
    P_ELAPSED=$((NOW - $(cat "$PAUSED_STATE_FILE")))
    if [ "$P_ELAPSED" -ge "$PAUSED_THRESHOLD_SEC" ]; then
      printf "STAGE=sab-paused-pinned msg=%s-slot(s)-paused-for-%ss\n" \
        "$PAUSED_SLOTS" "$P_ELAPSED" >&2
      exit 1
    fi
  fi
else
  [ -f "$PAUSED_STATE_FILE" ] && rm -f "$PAUSED_STATE_FILE"
fi
STALLED=0
# Paused is NOT a stall — the operator/app paused deliberately, and the
# status doc already warns on paused. Stall = work waiting + zero movement.
if [ "$PAUSED" = "0" ] && [ "$SLOTS" -gt 0 ] && [ "$KBPS" -lt 10 ]; then
  STALLED=1
fi

if [ "$STALLED" = "1" ]; then
  if [ ! -f "$STATE_FILE" ]; then
    echo "$NOW" > "$STATE_FILE"
    printf "sab-stall first-seen=%s slots=%s\n" "$NOW" "$SLOTS" >&2
    exit 0
  fi
  SINCE=$(cat "$STATE_FILE")
  ELAPSED=$((NOW - SINCE))
  if [ "$ELAPSED" -ge "$STALL_THRESHOLD_SEC" ]; then
    printf "STAGE=sab-stalled msg=kbps-%s-for-%ss-slots-%s\n" "$KBPS" "$ELAPSED" "$SLOTS" >&2
    exit 1
  fi
  printf "sab-stall pending elapsed=%ss threshold=%ss slots=%s\n" "$ELAPSED" "$STALL_THRESHOLD_SEC" "$SLOTS" >&2
  exit 0
fi

# Healthy (flowing, idle, or deliberately paused) — clear pending state.
[ -f "$STATE_FILE" ] && rm -f "$STATE_FILE"
printf "sab-flowing kbps=%s slots=%s paused=%s\n" "$KBPS" "$SLOTS" "$PAUSED"
exit 0
')
RC=$?
echo "$RES"
exit $RC
