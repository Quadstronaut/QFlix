#!/usr/bin/env bash
# tdarr-port-drain — wait for Tdarr Server's port to actually be free, and
# clear our own leftovers, BEFORE the next Tdarr_Server process tries to bind.
#
# THE BUG THIS FIXES
# ------------------
# `systemctl --user restart tdarr-server` returns as soon as systemd has
# reaped the main PID, but Tdarr_Server's listener does not always go with it:
# the socket sits in the kernel a moment longer, and the process sometimes
# lingers past SIGTERM. The replacement then binds instantly and dies:
#
#   [ERROR] Tdarr_Server - Error: listen EADDRINUSE: address already in use :::42018
#
# `Restart=on-failure` retries 10s later, which usually wins, so the symptom is
# a burst of restarts that "self-heals" — 4 in 24 minutes on 2026-08-20
# (21:12, 21:26, 21:35, 21:36), 2 more on 2026-08-18. Every restart path hits
# it: a config deploy, the 5-minute heartbeat, or on-failure itself. That is
# why the fix lives in ExecStartPre rather than in any one caller — it is the
# only place all three paths pass through.
#
# WHY NOT JUST WIDEN RestartSec
#   That trades a fast recovery for a slower one and still races. Waiting for
#   the actual condition is both faster in the common case (the port is usually
#   free within a second) and correct in the uncommon one.
#
# SHARED BOX CAVEAT. `ss -ltnp` only shows the PID for OUR OWN sockets. If the
# port is held by another tenant we can see the socket but not the owner, and
# we must not kill blindly. That case exits 1 so the unit goes visibly FAILED
# with a legible journald line, instead of crash-looping on an EADDRINUSE
# buried in a logfile nobody tails. This is the same squatter shape that made
# qBittorrent's WebUI bind race so hard to diagnose.
#
# Exit 0 — port is free (or was freed by us). Safe to start.
# Exit 1 — port still held after DRAIN_TIMEOUT by a process we do not own.
set -uo pipefail

# PORT resolution order is deliberate and the override MUST come first.
#
# This was `PORT=$(grep ... "$CONF"); : "${PORT:=${TDARR_PORT:-42018}}"`, where
# `:=` only fires on an unset-or-empty PORT — so a successful config read made
# TDARR_PORT dead, silently. The first attempt to exercise this script against a
# scratch port therefore drained the REAL one and SIGKILLed the live
# Tdarr_Server (2026-08-20; it came back 10s later on Restart=on-failure, which
# at least proved that path). A script that cannot be pointed somewhere safe is
# a script that only ever gets tested in production — the same reasoning behind
# the canary's QFLIX_CANARY_TDARR_HOUR injection.
CONF="$HOME/.apps/tdarr/configs/Tdarr_Server_Config.json"
PORT="${TDARR_PORT:-}"
if [ -z "$PORT" ]; then
  PORT=$(grep -oP '"serverPort":\s*"?\K[0-9]+' "$CONF" 2>/dev/null | head -1)
fi
: "${PORT:=42018}"
TIMEOUT=${TDARR_DRAIN_TIMEOUT:-30}

log() { logger -t tdarr-port-drain "$*" 2>/dev/null || true; echo "tdarr-port-drain: $*"; }

# Own-PIDs currently listening on $PORT, excluding this script's own tree.
holders() {
  ss -ltnpH "sport = :$PORT" 2>/dev/null \
    | grep -oP 'pid=\K[0-9]+' | sort -u
}

port_busy() {
  # -H so an empty result really is empty; the socket may be visible with no
  # readable pid, which still counts as busy.
  [ -n "$(ss -ltnH "sport = :$PORT" 2>/dev/null)" ]
}

port_busy || exit 0

log "port $PORT busy at start; draining (timeout ${TIMEOUT}s)"

# Phase 1 — politely wait. A socket in TIME_WAIT/lingering close clears on its
# own, and Tdarr_Server ignores SIGTERM for a beat while it flushes its DB.
WAITED=0
while [ "$WAITED" -lt "$TIMEOUT" ]; do
  port_busy || { log "port $PORT free after ${WAITED}s"; exit 0; }
  # Phase 2 — if the holder is OURS and is a Tdarr process, help it along.
  # Only escalate past the halfway mark, so the polite wait gets a real chance.
  if [ "$WAITED" -ge $((TIMEOUT / 2)) ]; then
    for pid in $(holders); do
      [ -n "$pid" ] || continue
      COMM=$(cat "/proc/$pid/comm" 2>/dev/null || echo "")
      case "$COMM" in
        Tdarr*|node)
          if kill -0 "$pid" 2>/dev/null; then
            log "SIGKILL leftover $COMM pid=$pid holding port $PORT"
            kill -9 "$pid" 2>/dev/null || true
          fi
          ;;
        *)
          [ -n "$COMM" ] && log "port $PORT held by pid=$pid comm=$COMM — not ours to kill"
          ;;
      esac
    done
  fi
  sleep 1
  WAITED=$((WAITED + 1))
done

port_busy || { log "port $PORT free after ${WAITED}s"; exit 0; }

FOREIGN=$(ss -ltnH "sport = :$PORT" 2>/dev/null | head -1)
log "REFUSING START: port $PORT still held after ${TIMEOUT}s by a process we do not own — $FOREIGN"
exit 1
