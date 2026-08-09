#!/usr/bin/env bash
# qBit-stall canary: detect a wedged libtorrent engine.
#
# Failure mode observed 2026-05-12: qBittorrent's libtorrent engine
# deadlocked during heavy queue churn (Sonarr blocklist+search storm +
# manual mass-recheck triggered simultaneously). Symptoms:
#   - dl_info_speed = 0 and up_info_speed = 0 across the whole client
#   - queuedDL count > 5 (active queue, not just an idle catalog)
#   - all qBit threads sleeping (no recheck worker activity)
#   - tracker status=1 (never contacted) on torrents that *should* be announcing
#
# Process appears alive (WebAPI responsive, threads exist, no segfault). Only
# fix discovered: `systemctl --user restart qbittorrent.service`.
#
# This canary fires when dl_info_speed has been zero for >5 min while queuedDL
# > QFLIX_CANARY_QBIT_STALL_MIN_QUEUE (default 5). The wallclock check uses a
# state file ~/.opt/maint/qbit-stall-since.epoch to track first-detected-stall
# timestamp across runs.
#
# Stage labels (printed to stderr on failure → Kuma `msg=`):
#   qbit-up-fail         — qBit WebAPI unreachable
#   qbit-auth-fail       — qBit auth failed (htpasswd password drift?)
#   qbit-engine-wedged   — dl_info_speed=0 for ≥5min + queuedDL > N
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
PWFILE=~/secrets/htpasswd.password
QB=http://127.0.0.1:17041
STATE_DIR=~/.opt/maint
STATE_FILE="$STATE_DIR/qbit-stall-since.epoch"
MIN_QUEUE=${QFLIX_CANARY_QBIT_STALL_MIN_QUEUE:-5}
STALL_THRESHOLD_SEC=${QFLIX_CANARY_QBIT_STALL_THRESHOLD_SEC:-300}
mkdir -p "$STATE_DIR"

# Auth
RC=$(curl -s -o /tmp/qbit-canary-login.txt -w "%{http_code}" -m 5 \
  -c /tmp/qbit-canary.cookie \
  -X POST "$QB/api/v2/auth/login" \
  -H "Referer: $QB" \
  --data-urlencode "username=quadstronaut" \
  --data-urlencode "password=$(cat "$PWFILE")")
[ "$RC" = "200" ] || { printf "STAGE=qbit-up-fail msg=login-http-%s\n" "$RC" >&2; exit 1; }
grep -q "^Ok\.$" /tmp/qbit-canary-login.txt || { printf "STAGE=qbit-auth-fail msg=login-body-%s\n" "$(head -c 40 /tmp/qbit-canary-login.txt)" >&2; exit 1; }

# Transfer info
TINFO=$(curl -sf -m 5 -b /tmp/qbit-canary.cookie "$QB/api/v2/transfer/info")
[ -n "$TINFO" ] || { printf "STAGE=qbit-up-fail msg=transfer-info-empty\n" >&2; exit 1; }

# State counts
STATES_JSON=$(curl -sf -m 8 -b /tmp/qbit-canary.cookie "$QB/api/v2/torrents/info")
[ -n "$STATES_JSON" ] || { printf "STAGE=qbit-up-fail msg=torrents-info-empty\n" >&2; exit 1; }

# Initialize before the python heredoc so a parse failure leaves them set
# (set -u would otherwise abort the script with an unbound-variable error
# that bypasses the STAGE= label and shows up in Kuma as a bare exit-1).
DL_SPEED=0; UP_SPEED=0; QUEUED_DL=0; DOWNLOADING=0; METADL=0
printf "%s\n---\n%s" "$TINFO" "$STATES_JSON" > /tmp/qbit-canary-payload.json
read DL_SPEED UP_SPEED QUEUED_DL DOWNLOADING METADL < <(python3 - <<"PY"
import json
data = open("/tmp/qbit-canary-payload.json").read()
ti_str, tors_str = data.split("\n---\n", 1)
t = json.loads(ti_str)
tors = json.loads(tors_str)
qd = sum(1 for x in tors if x.get("state") == "queuedDL")
dl = sum(1 for x in tors if x.get("state") == "downloading")
md = sum(1 for x in tors if x.get("state") == "metaDL")
print(t["dl_info_speed"], t["up_info_speed"], qd, dl, md)
PY
)
rm -f /tmp/qbit-canary-payload.json

NOW=$(date +%s)
STALLED=0
# Wedged libtorrent: downloads stuck while seeding may continue independently
# (different worker pools). The original UP_SPEED=0 guard masked the wedge
# any time even one seed completed in the cycle; the real symptom that
# motivated this canary is DL_SPEED=0 while a download queue exists.
if [ "$DL_SPEED" = "0" ] && [ "$QUEUED_DL" -gt "$MIN_QUEUE" ]; then
  STALLED=1
fi

if [ "$STALLED" = "1" ]; then
  if [ ! -f "$STATE_FILE" ]; then
    echo "$NOW" > "$STATE_FILE"
    printf "qbit-stall first-seen=%s queuedDL=%s\n" "$NOW" "$QUEUED_DL" >&2
    exit 0
  fi
  SINCE=$(cat "$STATE_FILE")
  ELAPSED=$((NOW - SINCE))
  if [ "$ELAPSED" -ge "$STALL_THRESHOLD_SEC" ]; then
    printf "STAGE=qbit-engine-wedged msg=dl-speed-0-for-%ss-queuedDL-%s-downloading-%s-metaDL-%s\n" \
      "$ELAPSED" "$QUEUED_DL" "$DOWNLOADING" "$METADL" >&2
    exit 1
  fi
  printf "qbit-stall pending elapsed=%ss threshold=%ss queuedDL=%s\n" "$ELAPSED" "$STALL_THRESHOLD_SEC" "$QUEUED_DL" >&2
  exit 0
fi

# Healthy — clear the state file if it exists
[ -f "$STATE_FILE" ] && rm -f "$STATE_FILE"
printf "qbit-flowing dl=%s up=%s queuedDL=%s downloading=%s\n" "$DL_SPEED" "$UP_SPEED" "$QUEUED_DL" "$DOWNLOADING"
exit 0
')
RC=$?
echo "$RES"
exit $RC
