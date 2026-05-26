#!/usr/bin/env bash
# prune-app-backups.sh — retain the N most recent app-manager backups per app.
#
# app-manager writes ~/.apps/backup/<app>-<YYYY-MM-DD>_<HH-MM>_<hash>.zip on
# every install / migrate / backup. These accumulate unbounded (12G of zips
# back to 2025-04 by the 2026-05 server migration). We keep the KEEP most
# recent per app so a config rollback stays possible while reclaiming the
# long tail.
#
# Why "keep N per app" and not "-mtime +90 -delete" like the poster cache:
# most apps are only backed up during installs/migrations, so an app stable
# for >90 days would have ALL its backups purged by an age rule — leaving zero
# rollback. A per-app count guarantees rollback regardless of age.
#
# Grouping strips the "-<date>_<time>_<hash>.zip" suffix, so multi-word app
# names (e.g. homarr-upstream) group as one key, not split on the hyphen.
#
# Env:
#   BACKUP_DIR   backup directory (default ~/.apps/backup)
#   KEEP         backups to retain per app (default 3)
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/.apps/backup}"
KEEP="${KEEP:-3}"

if [ ! -d "$BACKUP_DIR" ]; then
  echo "prune-app-backups: no backup dir ($BACKUP_DIR) — nothing to do"
  exit 0
fi

# Emit "<mtime>\t<basename>\t<path>" for every backup zip, sort newest-first,
# then awk keeps a per-app counter and prints only the paths past KEEP.
# sort -rn on the float mtime = strict newest-first ordering.
mapfile -t to_delete < <(
  find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.zip' -printf '%T@\t%f\t%p\n' \
    | sort -rn \
    | awk -F'\t' -v keep="$KEEP" '
        {
          key = $2
          sub(/-[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}_[0-9]+\.zip$/, "", key)
          if (++count[key] > keep) print $3
        }'
)

if [ "${#to_delete[@]}" -eq 0 ]; then
  echo "prune-app-backups: nothing to prune (≤${KEEP} per app in $BACKUP_DIR)"
  exit 0
fi

freed=0
for f in "${to_delete[@]}"; do
  sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
  if rm -f -- "$f"; then
    freed=$((freed + sz))
    echo "pruned $(basename "$f")"
  fi
done
printf 'prune-app-backups: removed %d file(s), freed %d MiB (KEEP=%d)\n' \
  "${#to_delete[@]}" "$((freed / 1048576))" "$KEEP"
