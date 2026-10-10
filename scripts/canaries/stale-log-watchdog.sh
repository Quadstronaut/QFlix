#!/usr/bin/env bash
# Stale-log watchdog canary.
#
# Each managed timer-driven app produces a log file with a known cadence.
# If the log mtime falls outside the expected window, the timer or the
# app itself has silently broken — exactly the failure mode that hid the
# recyclarr v8 migration bug behind 6 days of stale "Unable to find
# include template" errors before audit picked it up.
#
# This complements scripts/maint/qflix-vlogs-ingest.py's dormant-skip
# filter: the ingester quietly drops dormant logs from the index; this
# canary loudly fails when a log is dormant LONGER than its schedule
# allows.
#
# Watched apps + their expected freshness windows:
#   kometa     ~/.apps/kometa/config/logs/meta.log   daily      → ≤ 36h
#   recyclarr  ~/.apps/recyclarr/logs/recyclarr.log  Sun weekly → ≤ 252h (10.5d)
#   buildarr   ~/.apps/buildarr/logs/buildarr.log    daily      → ≤ 36h
#
# Stage labels (printed to stderr on failure → Kuma `msg=`):
#   log-stale-<app>   — log mtime exceeds the cadence's stale threshold
#   log-missing-<app> — expected log file does not exist on the seedbox
#
# unpackerr leg (QFLX-46): unpackerr.log has no cadence of its own, so instead of
# a fixed cap it is compared with the newest journald line of the native unit.
# Reds when the file is > 2h behind the journal (the [[general]] TOML trap voids
# log_file while the process keeps running and logging to stdout), or when the
# conf carries a [[general]] header. No journal line (UCC era) = nothing to
# compare against = pass.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
# (app|path|max_age_seconds) — bash assoc arrays choke on pipes in keys,
# so use a flat pipe-delimited list.
WATCHED=(
  "kometa|$HOME/.apps/kometa/config/logs/meta.log|129600"
  "recyclarr|$HOME/.apps/recyclarr/logs/recyclarr.log|907200"
  "buildarr|$HOME/.apps/buildarr/logs/buildarr.log|129600"
  # Weekly (Sun 06:00 UTC), same 1.5x-cadence threshold as recyclarr. Added
  # 2026-08-06: upgradinatorr was the only cron-class app in manifest/apps.yaml
  # absent from this table despite writing four dated logs, and its own monitor
  # cannot cover the gap - _probe_systemd_oneshot treats Result=success from a
  # run three months ago identically to one five minutes ago, so a timer that
  # stopped scheduling reads green forever. All four logs are written in the
  # same run, so watching one is sufficient.
  "upgradinatorr|$HOME/.apps/upgradinatorr/logs/sonarr.log|907200"
  # Nightly (04:00 UTC) Plex->Listmonk subscriber reconcile, appending to
  # sync.log on every run. Added 2026-08-08 to CLOSE the cron-listmonk-sync
  # open_gap in manifest/jobs.yaml: its own adjudication named this canary as
  # "the natural home for a freshness check", and the gap stayed open only
  # because nobody had added the line. 36h = the same 1.5x-cadence threshold
  # the other daily jobs use. A silent stop means new members quietly stop
  # being onboarded to the newsletter -- member-visible, slow, exactly the
  # shape a freshness check catches and an app monitor cannot.
  "listmonk-sync|$HOME/.apps/listmonk/logs/sync.log|129600"
)
NOW=$(date -u +%s)
FAILED=()
PASSED=()

# BEGIN unpackerr-leg (QFLX-46; tests extract this block, keep it quote-free)
unpackerr_leg() {
  local conf="$HOME/.apps/unpackerr/unpackerr.conf"
  local log="$HOME/.apps/unpackerr/unpackerr.log"
  local line jt lm gap
  if [ -f "$conf" ] && grep -qiE "^[[:space:]]*\[\[?[[:space:]]*general[[:space:]]*\]\]?" "$conf"; then
    FAILED+=("unpackerr:conf-general-header")
  fi
  line=$(journalctl --user -u qflix-unpackerr.service _COMM=unpackerr -n 1 -o short-unix --no-pager 2>/dev/null | grep -E "^[0-9]" | tail -1)
  jt=${line%% *}
  jt=${jt%%.*}
  case "$jt" in ""|*[!0-9]*) PASSED+=("unpackerr:no-journal"); return 0 ;; esac
  if [ ! -f "$log" ]; then
    FAILED+=("log-missing-unpackerr")
    return 0
  fi
  lm=$(stat -c %Y "$log" 2>/dev/null || echo 0)
  gap=$((jt - lm))
  if [ "$gap" -gt 7200 ]; then
    FAILED+=("unpackerr:log-behind-journal=$((gap / 3600))h>cap=2h")
  else
    PASSED+=("unpackerr:log-in-step")
  fi
}
# END unpackerr-leg
for entry in "${WATCHED[@]}"; do
  app=$(printf %s "$entry" | cut -d "|" -f1)
  path=$(printf %s "$entry" | cut -d "|" -f2)
  max_age=$(printf %s "$entry" | cut -d "|" -f3)
  if [ ! -f "$path" ]; then
    FAILED+=("log-missing-$app")
    continue
  fi
  mtime=$(stat -c %Y "$path" 2>/dev/null || echo 0)
  age=$((NOW - mtime))
  if [ "$age" -gt "$max_age" ]; then
    age_h=$((age / 3600))
    cap_h=$((max_age / 3600))
    FAILED+=("$app:age=${age_h}h>cap=${cap_h}h")
  else
    age_h=$((age / 3600))
    PASSED+=("$app:${age_h}h")
  fi
done

unpackerr_leg

if [ ${#FAILED[@]} -gt 0 ]; then
  joined_fail=$(IFS=,; echo "${FAILED[*]}")
  joined_pass=$(IFS=,; echo "${PASSED[*]}")
  printf "STAGE=log-stale msg=stale-%s-fresh-%s\n" "$joined_fail" "$joined_pass" >&2
  exit 1
fi
joined_pass=$(IFS=,; echo "${PASSED[*]}")
printf "PASS: stale-log-watchdog — %d apps fresh (%s)\n" "${#PASSED[@]}" "$joined_pass"
exit 0
')
RC=$?
echo "$RES"
exit $RC
