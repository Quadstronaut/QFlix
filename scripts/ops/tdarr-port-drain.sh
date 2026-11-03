#!/usr/bin/env bash
# tdarr-port-drain — wait for the previous Tdarr Server to actually let go of
# its listener, and clear our own leftovers, BEFORE the next one starts.
#
# THE BUG THIS FIXED (2026-08-18/20)
# ----------------------------------
# `systemctl --user restart tdarr-server` returns as soon as systemd has
# reaped the main PID, but Tdarr_Server's listener does not always go with it:
# the process sometimes lingers past SIGTERM while it flushes its DB. When
# Tdarr listened on TCP the replacement died on EADDRINUSE:
#
#   [ERROR] Tdarr_Server - Error: listen EADDRINUSE: address already in use :::42018
#
# 6 occurrences across every restart path (config deploy, the 5-minute
# heartbeat, on-failure). That is why this lives in ExecStartPre: it is the one
# place all of them pass through.
#
# WHAT IT WATCHES NOW (QFLX-97)
# -----------------------------
# Tdarr no longer owns a TCP port. It listens on a unix socket in
# /run/user/<uid>/qflix-tdarr/ and tdarr-uid-relay holds 127.0.0.1:<serverPort>
# permanently, so draining the TCP port would block every start forever. The
# patched Tdarr unlinks a stale socket path before it binds, so EADDRINUSE can
# no longer happen either. What still matters is the reason the drain existed:
# never start a second Tdarr_Server while the old one is still alive with the
# database open. "Still listening on the socket path" is that signal.
#
# OWNERSHIP. Nobody else can listen on that path (the directory is 0700), but
# the kill is still restricted to our own Tdarr/node processes by comm, and the
# give-up path refuses the start instead of crash-looping.
#
# Exit 0 — no listener on the socket (or it was cleared by us). Safe to start.
# Exit 1 — still held after DRAIN_TIMEOUT by something we will not kill.
set -uo pipefail

# Override FIRST, then the default. The TCP-era version read the config before
# its override (`:=` only fires on an empty value), so the first attempt to point
# it at a scratch port drained the REAL one and SIGKILLed the live Tdarr_Server
# (2026-08-20). A destructive script must be pointable somewhere safe.
SOCK="${TDARR_SOCK:-}"
if [ -z "$SOCK" ]; then
  SOCK="/run/user/$(id -u)/qflix-tdarr/tdarr.sock"
fi
TIMEOUT=${TDARR_DRAIN_TIMEOUT:-30}

log() { logger -t tdarr-port-drain "$*" 2>/dev/null || true; echo "tdarr-port-drain: $*"; }

# Own PIDs listening on the socket path.
holders() {
  ss -lxpH src "$SOCK" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u
}

sock_busy() {
  [ -n "$(ss -lxH src "$SOCK" 2>/dev/null)" ]
}

sock_busy || exit 0

log "$SOCK still has a listener at start; draining (timeout ${TIMEOUT}s)"

# Phase 1 — politely wait: Tdarr_Server ignores SIGTERM for a beat while it
# flushes its DB.
WAITED=0
while [ "$WAITED" -lt "$TIMEOUT" ]; do
  sock_busy || { log "$SOCK free after ${WAITED}s"; exit 0; }
  # Phase 2 — if the holder is OURS and is a Tdarr process, help it along.
  # Only escalate past the halfway mark, so the polite wait gets a real chance.
  if [ "$WAITED" -ge $((TIMEOUT / 2)) ]; then
    for pid in $(holders); do
      [ -n "$pid" ] || continue
      COMM=$(cat "/proc/$pid/comm" 2>/dev/null || echo "")
      case "$COMM" in
        Tdarr*|node)
          if kill -0 "$pid" 2>/dev/null; then
            log "SIGKILL leftover $COMM pid=$pid holding $SOCK"
            kill -9 "$pid" 2>/dev/null || true
          fi
          ;;
        *)
          [ -n "$COMM" ] && log "$SOCK held by pid=$pid comm=$COMM — not ours to kill"
          ;;
      esac
    done
  fi
  sleep 1
  WAITED=$((WAITED + 1))
done

sock_busy || { log "$SOCK free after ${WAITED}s"; exit 0; }

HOLDER=$(ss -lxpH src "$SOCK" 2>/dev/null | head -1)
log "REFUSING START: $SOCK still held after ${TIMEOUT}s by a process we will not kill — $HOLDER"
exit 1
