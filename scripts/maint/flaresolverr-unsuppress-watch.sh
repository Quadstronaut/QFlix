#!/usr/bin/env bash
# flaresolverr-unsuppress-watch.sh — self-destructing watcher.
#
# While flaresolverr is push-suppressed (muted in Kuma via the pusher's
# push-suppress registry, pending the Ultra.cc cap_setuid ticket), this runs
# on a timer and polls flaresolverr's health. Once it's been live for
# UP_DEBOUNCE consecutive checks, it removes the suppression entries (restoring
# real alerting) and DELETES ITSELF — units, state, and script.
#
# It lifts BOTH "flaresolverr" and "canary-prowlarr-indexer-health": the
# prowlarr canary is a downstream symptom (all indexer proxies unavailable =
# flaresolverr down), so it auto-recovers once flaresolverr is live.
#
# It only ever touches local files (Kuma's admin pause API is operator-only,
# and a seedbox-resident watcher can't reach it anyway). Restoring the alert =
# removing the registry entry; the pusher then resumes pushing real status.
set -euo pipefail

APP="flaresolverr"
PORT_SECRET="${HOME}/secrets/flaresolverr.port"
HOSTNAME_OVERRIDE="172.17.0.1"
UP_DEBOUNCE=2

STATE_DIR="${MANITOBA_STATE_DIR:-${HOME}/.opt/maint}"
SUPPRESS_FILE="${STATE_DIR}/push-suppress.json"
COUNTER_FILE="${STATE_DIR}/${APP}-unsuppress.state"

UNIT_DIR="${HOME}/.config/systemd/user"
TIMER="manitoba-maint-${APP}-unsuppress.timer"
SERVICE="manitoba-maint-${APP}-unsuppress.service"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ${APP}-unsuppress: $*"; }

# If the app is no longer suppressed (someone removed it manually), there's
# nothing to watch — self-destruct.
if [ ! -f "$SUPPRESS_FILE" ] || ! grep -q "\"${APP}\"" "$SUPPRESS_FILE" 2>/dev/null; then
  log "no suppression entry for ${APP} — nothing to watch, self-destructing"
  SELF_DESTRUCT=1
else
  SELF_DESTRUCT=0
fi

if [ "$SELF_DESTRUCT" = "0" ]; then
  PORT="$(cat "$PORT_SECRET" 2>/dev/null || echo 17011)"
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
            "http://${HOSTNAME_OVERRIDE}:${PORT}/" 2>/dev/null || echo 000)"
  COUNT="$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)"

  if [ "$CODE" = "200" ]; then
    COUNT=$((COUNT + 1))
    echo "$COUNT" > "$COUNTER_FILE"
    log "flaresolverr live (HTTP 200) — consecutive=${COUNT}/${UP_DEBOUNCE}"
    if [ "$COUNT" -ge "$UP_DEBOUNCE" ]; then
      SELF_DESTRUCT=1
    fi
  else
    echo 0 > "$COUNTER_FILE"
    log "flaresolverr still down (HTTP ${CODE}) — staying suppressed"
    exit 0
  fi
fi

[ "$SELF_DESTRUCT" = "1" ] || exit 0

# --- restore alerting: remove the suppression entries atomically ---
# Lifts flaresolverr AND its downstream prowlarr-indexer-health canary.
if [ -f "$SUPPRESS_FILE" ]; then
  python3 - "$SUPPRESS_FILE" "$APP" "canary-prowlarr-indexer-health" <<'PY'
import json, os, sys, tempfile
path, keys = sys.argv[1], sys.argv[2:]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {}
for k in keys:
    data.pop(k, None)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
os.replace(tmp, path)
PY
  log "removed ${APP} + canary-prowlarr-indexer-health from push-suppress — alerts restored"
fi

# --- self-destruct: tear down own units + state + script ---
log "self-destructing"
systemctl --user disable --now "$TIMER" 2>/dev/null || true
rm -f "${UNIT_DIR}/${TIMER}" "${UNIT_DIR}/${SERVICE}" \
      "${UNIT_DIR}/timers.target.wants/${TIMER}" \
      "$COUNTER_FILE"
systemctl --user daemon-reload 2>/dev/null || true
# Delete the script itself last (inode persists until this process exits).
rm -f -- "$0"
exit 0
