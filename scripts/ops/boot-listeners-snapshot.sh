#!/usr/bin/env bash
# boot-listeners-snapshot.sh — capture TCP listeners + thread usage across the
# first minutes after a (re)boot, so a transient port-squatter is IDENTIFIED
# next time instead of vanishing unlogged.
#
# Motivation (2026-07-08 incident, see memory qbit-webui-boot-bind-race): a host
# maintenance reboot left qBittorrent unable to bind its WebUI port 17041
# ("address already in use") because something transiently held the port during
# qBit's ~T+100s WebUI init, then released it before anyone could look. journald
# records service starts, not *who holds a socket* — so the squatter stayed
# anonymous. This oneshot samples `ss -ltnp` on a short cadence over the boot
# window and writes an explicit "who holds :17041" line each pass.
#
# Tenant-owned, no root required. Runs as a systemd --user oneshot pulled in by
# default.target (see manitoba-maint-boot-listeners.service). Overridable via env
# so it can be exercised by hand in ~2s: BOOT_LISTENERS_SAMPLES=2
# BOOT_LISTENERS_INTERVAL=1 bash boot-listeners-snapshot.sh
set -uo pipefail

LOG="${BOOT_LISTENERS_LOG:-$HOME/.opt/maint/boot-listeners.log}"
WATCH_PORT="${BOOT_LISTENERS_WATCH_PORT:-17041}"   # qBit WebUI — the known offender
SAMPLES="${BOOT_LISTENERS_SAMPLES:-12}"            # 12 x 15s = ~3min, spans qBit's ~T+100s bind
INTERVAL="${BOOT_LISTENERS_INTERVAL:-15}"
MAX_LINES="${BOOT_LISTENERS_MAX_LINES:-1200}"     # keep the last few boots, then trim

mkdir -p "$(dirname "$LOG")"

_now() { date -u +%H:%M:%SZ 2>/dev/null || echo "??:??:??"; }

{
  echo "==== boot-listeners | run=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) boot=$(uptime -s 2>/dev/null || echo '?') host=$(hostname 2>/dev/null) ===="
  echo "[$(_now)] threads(user)=$(ps --no-headers -L -u "$USER" 2>/dev/null | wc -l) ulimit_u=$(ulimit -u 2>/dev/null)"
} >> "$LOG"

for _ in $(seq 1 "$SAMPLES"); do
  # `ss` shows the socket for any tenant; the users:(...) process detail resolves
  # only for our own processes. Either way the (address:port) occupancy is logged.
  holder="$(ss -ltnp 2>/dev/null | grep -E ":${WATCH_PORT}([^0-9]|$)" || true)"
  if [ -n "$holder" ]; then
    echo "[$(_now)] :${WATCH_PORT} HELD -> $(echo "$holder" | tr -s ' ' | head -1)" >> "$LOG"
  else
    echo "[$(_now)] :${WATCH_PORT} free" >> "$LOG"
  fi
  sleep "$INTERVAL"
done

# For the record: OUR stack's listeners only. On a shared box `ss` lists every
# tenant's sockets (hundreds) but resolves the users:(...) process detail only
# for our own — filtering to those keeps the log compact and relevant. A
# box-wide LISTEN count gives context without dumping foreign sockets.
{
  total="$(ss -ltn 2>/dev/null | grep -c LISTEN)"
  echo "---- our listeners @ $(_now) (box-wide LISTEN total=${total}) ----"
  ss -ltnp 2>/dev/null | grep 'users:(' || echo "(none resolved to our processes)"
  echo
} >> "$LOG"

if [ -f "$LOG" ]; then
  tail -n "$MAX_LINES" "$LOG" > "$LOG.tmp" 2>/dev/null && mv -f "$LOG.tmp" "$LOG"
fi
exit 0
