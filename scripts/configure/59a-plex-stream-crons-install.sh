#!/usr/bin/env bash
# 59a-plex-stream-crons-install.sh — deploy the Plex stream-management scripts
# and install their every-minute crons (idempotent).
#
# Provisions the two paired every-minute Plex crons that were previously only
# ever added to the crontab BY HAND — so a slot rebuild silently dropped them
# and the repo could not reproduce them:
#   - kill_stream.sh --max N : per-USER concurrent-stream cap (default 4). The
#     cap lives here (KS_MAX) — change it in one place, re-run, done.
#   - stream_stats.sh        : logs Plex stream stats -> JSON (dashboard /api
#     and smoke-test #13 read the freshness of that state file).
#
# Depends on 59-python-plexapi-venv.sh (the python-plexapi venv both wrappers
# invoke) and secrets/plex.{host,port,token} on the box.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "$HERE/lib/ssh.sh"
# shellcheck source=/dev/null
source "$HERE/lib/log.sh"

KS_MAX="${KS_MAX:-4}"   # per-user concurrent-stream cap enforced by kill_stream

# 1. Deploy the three Plex scripts to the box.
sshm 'mkdir -p ~/scripts/plex ~/.apps/stream-stats/logs'
for f in kill_stream.sh kill_stream.py stream_stats.sh; do
  scpm_to "$HERE/plex/$f" "/home/quadstronaut/scripts/plex/$f"
done
sshm 'chmod +x ~/scripts/plex/kill_stream.sh ~/scripts/plex/kill_stream.py ~/scripts/plex/stream_stats.sh'

# 2. Install both crons idempotently: strip any prior copy of either line, then
#    re-add. The cron lines are interpolated locally so the remote just writes
#    literals (the `*` fields never glob — they're inside single quotes there).
KILL_LINE="* * * * * /home/quadstronaut/scripts/plex/kill_stream.sh --max ${KS_MAX} >/dev/null 2>&1"
STATS_LINE="* * * * * /home/quadstronaut/scripts/plex/stream_stats.sh >/dev/null 2>&1"
sshm "crontab -l 2>/dev/null | grep -vE 'kill_stream[.]sh|stream_stats[.]sh' > /tmp/_ksc
printf '%s\n' '$KILL_LINE' '$STATS_LINE' >> /tmp/_ksc
crontab /tmp/_ksc && rm -f /tmp/_ksc"

log_info "plex-stream crons installed (kill_stream --max ${KS_MAX}, stream_stats):"
sshm "crontab -l 2>/dev/null | grep -E 'kill_stream|stream_stats'"

# 3. Verify kill_stream runs clean through the wrapper (dry-run, no kills).
log_info "kill_stream dry-run:"
sshm "~/scripts/plex/kill_stream.sh --dry-run --max ${KS_MAX} >/dev/null 2>&1 && echo '  OK' || echo '  FAILED'"
