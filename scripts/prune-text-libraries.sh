#!/usr/bin/env bash
# Daily prune cron for text/audio libraries (operator: spec §7.3).
# Files older than CAP_DAYS are deleted; files within WARN_LEAD of cap fire a
# Notifiarr digest. Run as quadstronaut user via crontab.
#
# Idempotent / stateless. Best-effort library rescans after deletes (will fail
# silently if Komga/Kavita/Calibre-Web/Audiobookshelf API keys aren't set yet).
set -euo pipefail

CAP_DAYS=365
WARN_LEAD=14
ROOTS=(
  "/home/quadstronaut/media/Books"
  "/home/quadstronaut/media/Audiobooks"
  "/home/quadstronaut/media/Comics"
  "/home/quadstronaut/media/Manga"
  "/home/quadstronaut/media/Podcasts"
)
NOTIFIARR_KEY_FILE=/home/quadstronaut/.opt/secrets/notifiarr.key
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

TODAY=$(date +%s)
WARN_THRESHOLD=$(( CAP_DAYS - WARN_LEAD ))

post_notifiarr() {
  local body="$1"
  [ -f "$NOTIFIARR_KEY_FILE" ] || return 0
  local key
  key=$(tr -d '[:space:]' < "$NOTIFIARR_KEY_FILE")
  curl -sf --max-time 10 -X POST -H "X-API-Key: $key" -H "Content-Type: application/json" \
    "https://notifiarr.com/api/v1/notification/passthrough/$key" \
    -d "$body" >/dev/null || true
}

deletions=()
warnings=()

for root in "${ROOTS[@]}"; do
  [ -d "$root" ] || continue
  while IFS= read -r -d '' file; do
    age_days=$(( (TODAY - $(stat -c %Y "$file")) / 86400 ))
    if [ "$age_days" -ge "$CAP_DAYS" ]; then
      deletions+=("$file (age=${age_days}d)")
      [ "$DRY_RUN" = 0 ] && rm -f "$file"
    elif [ "$age_days" -ge "$WARN_THRESHOLD" ]; then
      remaining=$(( CAP_DAYS - age_days ))
      warnings+=("$file (age=${age_days}d, deletes in ${remaining}d)")
    fi
  done < <(find "$root" -type f -print0)
done

if [ "${#warnings[@]}" -gt 0 ]; then
  msg="Text/audio library: ${#warnings[@]} items entering 14-day warning window"
  for w in "${warnings[@]:0:25}"; do msg+=$'\n- '$w; done
  [ "${#warnings[@]}" -gt 25 ] && msg+=$'\n... and '$(( ${#warnings[@]} - 25 ))" more"
  post_notifiarr "{\"text\":\"$msg\"}"
fi

if [ "${#deletions[@]}" -gt 0 ]; then
  msg="Text/audio library: ${#deletions[@]} items deleted today (age >= ${CAP_DAYS}d)"
  for d in "${deletions[@]:0:25}"; do msg+=$'\n- '$d; done
  post_notifiarr "{\"text\":\"$msg\"}"
fi

# Best-effort library rescans (silently skip if helper or keys absent)
RESCAN=/home/quadstronaut/scripts/post-import/library-rescan.sh
if [ -x "$RESCAN" ]; then
  for target in komga kavita calibre-web audiobookshelf; do
    "$RESCAN" "$target" >/dev/null 2>&1 || true
  done
fi

echo "prune-text-libraries: deletions=${#deletions[@]} warnings=${#warnings[@]} dry_run=$DRY_RUN"
