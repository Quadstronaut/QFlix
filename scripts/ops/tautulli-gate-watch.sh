#!/usr/bin/env bash
# =============================================================================
# TEMPORARY WATCHER — DELETE WHEN UCC MAINTENANCE RESOLVES.
# Ticket: Ultra.cc post-maintenance lifecycle gate + Plex re-IP (manitoba),
# filed 2026-05-24. This is a one-shot throwaway, NOT part of the maint stack.
# Remove with:  rm ~/tautulli-gate-watch.sh ~/.opt/maint/tautulli-watch.log
# =============================================================================
#
# WHAT IT WATCHES
#   Tautulli is down because Ultra.cc's maintenance gates `app-* start`
#   ({"result": false, ... "no longer available due to maintenance"}). The
#   config is already re-pinned to Plex at the docker bridge gateway and Plex
#   itself is confirmed reachable there — so the ONLY remaining blocker is the
#   gate. This script polls until the gate lifts, then (if AUTO_START=1) issues
#   a single `app-tautulli start`, verifies Tautulli serves HTTP and can reach
#   Plex, and pings the operator's Discord. It exits 0 once recovered.
#
# WHY START-AS-PROBE IS SAFE HERE
#   The only non-read op that reveals gate state is a lifecycle call. Tautulli
#   is DOWN and we WANT it up with the already-fixed config, so issuing `start`
#   is both the probe and the cure. It never STOPS or RESTARTS anything — the
#   2026-05 lesson was "don't stop a UCC app mid-maintenance", not "don't start
#   a down one". While gated, start is a confirmed no-op.
#
# RUN IT (on the seedbox, detached so it survives the SSH session):
#   setsid nohup bash ~/tautulli-gate-watch.sh >> ~/.opt/maint/tautulli-watch.log 2>&1 &
# Watch it:   tail -f ~/.opt/maint/tautulli-watch.log
# Stop it:    pkill -f tautulli-gate-watch.sh
# -----------------------------------------------------------------------------
set -uo pipefail

# --- knobs -------------------------------------------------------------------
AUTO_START=1            # 1 = start Tautulli the instant the gate lifts.
                        # 0 = pure watch (only notices if Tautulli comes up on
                        #     its own; cannot detect the gate without probing).
POLL_SECONDS=300        # 5 min between checks (gate state changes slowly).
HEARTBEAT_SECONDS=21600 # 6 h: a "still gated" reassurance ping so silence on
                        # Discord can't be mistaken for a dead watcher.
START_GRACE=600         # after a successful `start`, allow Tautulli this long
                        # to begin serving HTTP before flagging it as stuck.

KUMA_SLUG="public"      # status page whose incident we clear on recovery

SECRETS="$HOME/secrets"
WEBHOOK="$(tr -d '[:space:]' < "$SECRETS/discord-webhook.url" 2>/dev/null || true)"
OPERATOR_ID="$(tr -d '[:space:]' < "$SECRETS/discord-operator.id" 2>/dev/null || true)"
TAUT_PORT="$(tr -d '[:space:]' < "$SECRETS/tautulli.port" 2>/dev/null || true)"
PLEX_PORT="$(tr -d '[:space:]' < "$SECRETS/plex.port" 2>/dev/null || echo 17025)"
PLEX_HOSTPORT="172.17.0.1:${PLEX_PORT}"

log() { printf '%s  %s\n' "$(date -u +%FT%TZ)" "$*"; }

# --- Discord -----------------------------------------------------------------
# mention=1 adds <@operator> in `content` so it pushes to your phone; embeds
# alone do not. Uses python3 for safe JSON escaping (curl + bash quoting is a
# footgun). Mirrors scripts/maint/lib/notify.py colors (decimal ints).
ping_discord() {  # $1=title  $2=message  $3=color  $4=mention(0/1)
  local title="$1" msg="$2" color="${3:-3447003}" mention="${4:-0}"
  [ -n "$WEBHOOK" ] || { log "NO WEBHOOK — would have pinged: $title"; return 1; }
  WH="$WEBHOOK" OP="$OPERATOR_ID" T="$title" M="$msg" C="$color" MEN="$mention" \
  python3 - <<'PY'
import json, os, sys, urllib.request, urllib.error
url = os.environ["WH"]
payload = {"username": "tautulli-watch",
           "embeds": [{"title": os.environ["T"],
                       "description": os.environ["M"][:4000],
                       "color": int(os.environ["C"])}]}
if os.environ.get("MEN") == "1" and os.environ.get("OP"):
    op = os.environ["OP"]
    payload["content"] = f"<@{op}>"
    payload["allowed_mentions"] = {"users": [op]}
req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json",
                                      # Discord (Cloudflare) 403s the default
                                      # "Python-urllib/x" UA. Any real-looking
                                      # UA is accepted.
                                      "User-Agent": "tautulli-watch/1.0"})
try:
    urllib.request.urlopen(req, timeout=15)
    print("discord ok")
except urllib.error.URLError as e:
    print(f"discord FAIL: {e}", file=sys.stderr); sys.exit(1)
PY
}

# --- Kuma status-page incident ----------------------------------------------
# On full recovery, unpin the public status-page incident so users stop seeing
# the "stats unavailable" banner. Uses the raw socket emit to dodge
# uptime-kuma-api's buggy save_status_page() reconciliation on this Kuma
# version (the emit persists server-side on its own). Best-effort: logs and
# returns nonzero on failure, never aborts the watcher.
clear_kuma_incident() {
  local kport kpw
  kport="$(tr -d '[:space:]' < "$SECRETS/uptimekuma.port" 2>/dev/null || true)"
  kpw="$(cat "$SECRETS/htpasswd.password" 2>/dev/null || true)"
  [ -n "$kport" ] && [ -n "$kpw" ] || { log "kuma: missing port/password — cannot clear incident"; return 1; }
  KPORT="$kport" KPW="$kpw" KSLUG="$KUMA_SLUG" python3 - <<'PY'
import os, sys
try:
    from uptime_kuma_api import UptimeKumaApi
except Exception as e:
    print(f"kuma: import failed: {e}", file=sys.stderr); sys.exit(2)
api = UptimeKumaApi("http://127.0.0.1:" + os.environ["KPORT"])
try:
    api.login("quadstronaut", os.environ["KPW"])
    # raw emit; persists without the buggy whole-page re-save
    api._call("unpinIncident", os.environ["KSLUG"])
    print("kuma: incident unpinned")
except Exception as e:
    print(f"kuma: unpin failed: {e}", file=sys.stderr); sys.exit(1)
finally:
    try:
        api.disconnect()
    except Exception:
        pass
PY
}

# --- probes ------------------------------------------------------------------
tautulli_up() {   # web serving? accept any 2xx/3xx (login may 30x)
  [ -n "$TAUT_PORT" ] || return 1
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 8 "http://127.0.0.1:${TAUT_PORT}/" 2>/dev/null)
  [[ "$code" =~ ^(200|301|302|303)$ ]]
}

plex_up() {       # Plex /identity returns 200 + MediaContainer, no token needed
  curl -s -m 8 "http://${PLEX_HOSTPORT}/identity" 2>/dev/null | grep -q 'MediaContainer'
}

# Returns one of: started | gated | error:<text>
attempt_start() {
  local out
  out=$(app-tautulli start 2>&1)
  if printf '%s' "$out" | grep -qi 'maintenance'; then echo "gated"; return; fi
  if printf '%s' "$out" | grep -qE '"result":\s*true'; then echo "started"; return; fi
  echo "error:$out"
}

# --- main --------------------------------------------------------------------
log "watcher armed: AUTO_START=$AUTO_START poll=${POLL_SECONDS}s tautulli_port=$TAUT_PORT plex=$PLEX_HOSTPORT"
ping_discord "👁️ Tautulli watcher armed (manitoba)" \
  "Watching for the Ultra.cc lifecycle gate to lift. Tautulli is down; config already re-pinned to Plex at \`${PLEX_HOSTPORT}\` (Plex confirmed reachable). AUTO_START=${AUTO_START}, polling every $((POLL_SECONDS/60))m. I'll ping when it recovers." \
  3447003 0

last_heartbeat=$(date +%s)
start_issued_at=0          # 0 = no start issued yet this run

while true; do
  now=$(date +%s)

  # 1) Already serving? Then we're done — verify the Plex link and report.
  if tautulli_up; then
    if plex_up; then
      log "RECOVERED: Tautulli serving + Plex reachable"
      if clear_kuma_incident; then
        kuma_note="Status-page incident cleared (users no longer see the banner)."
      else
        kuma_note="⚠️ Couldn't auto-clear the Kuma incident — unpin it manually on the status page."
      fi
      ping_discord "✅ Tautulli recovered (manitoba)" \
        "Tautulli web is serving (\`127.0.0.1:${TAUT_PORT}\`) and Plex is reachable at \`${PLEX_HOSTPORT}\`. ${kuma_note} The storm should be gone — spot-check the dashboard. Watcher exiting; safe to delete the script." \
        3066993 1
    else
      log "PARTIAL: Tautulli up but Plex unreachable at $PLEX_HOSTPORT"
      ping_discord "⚠️ Tautulli up — but can't reach Plex (manitoba)" \
        "Tautulli is serving but \`${PLEX_HOSTPORT}\` is not answering /identity. Plex may have moved again, or its container is down. Needs a look. Watcher exiting." \
        15976736 1
    fi
    exit 0
  fi

  # 2) Down. Try to lift+start if armed (this is also the only gate probe).
  if [ "$AUTO_START" = "1" ] && [ "$start_issued_at" = "0" ]; then
    res=$(attempt_start)
    case "$res" in
      gated)
        if (( now - last_heartbeat >= HEARTBEAT_SECONDS )); then
          log "still gated (heartbeat)"
          ping_discord "⏳ Still gated (manitoba)" \
            "Ultra.cc lifecycle gate still up; \`app-tautulli start\` returns the maintenance message. No change. Still watching." \
            10197915 0
          last_heartbeat=$now
        else
          log "still gated"
        fi
        ;;
      started)
        start_issued_at=$now
        log "GATE LIFTED — issued app-tautulli start; awaiting web (grace ${START_GRACE}s)"
        ping_discord "🔓 Gate lifted — starting Tautulli (manitoba)" \
          "\`app-tautulli start\` was accepted (gate is down). Waiting for Tautulli to serve, then I'll confirm the Plex link." \
          3447003 1
        ;;
      error:*)
        log "unexpected start response: ${res#error:}"
        ping_discord "❓ Unexpected start response (manitoba)" \
          "\`app-tautulli start\` returned something other than gated/success:\n\`\`\`${res#error:}\`\`\`\nStill watching." \
          15976736 1
        # back off so we don't spam on a persistent oddity
        start_issued_at=$now
        ;;
    esac
  fi

  # 3) If we issued a start but Tautulli still isn't serving past the grace
  #    window, flag it once and keep watching (top-of-loop will catch a late boot).
  if [ "$start_issued_at" != "0" ] && (( now - start_issued_at > START_GRACE )); then
    log "start issued ${START_GRACE}s ago but web still down — flagging, continuing to watch"
    ping_discord "⚠️ Start accepted but Tautulli not serving (manitoba)" \
      "Issued \`app-tautulli start\` over $((START_GRACE/60))m ago; web still \`000\`. Container may be crash-looping. Needs eyes. Still watching." \
      15976736 1
    start_issued_at=$now  # re-arm the grace so it re-flags every START_GRACE if needed
  fi

  sleep "$POLL_SECONDS"
done
