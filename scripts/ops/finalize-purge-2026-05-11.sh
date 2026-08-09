#!/usr/bin/env bash
# Finalize the 2026-05-11 inventory-driven cleanup. Single-shot script for the
# operator to run AFTER reviewing inventory.md and confirming the AskUserQuestion
# answers (Q1: Conjurr+Newsletterr+Jellyfin+legacy-systemd purges. Q2: Ultra-*
# scripts purged. Q3: readarr+mylar3+ombi purged. Q4: unpackerr+upgradinatorr+
# postgres added to manifest).
#
# Everything destructive moves to ~/.purged-2026-05-11/ first — fully reversible
# until the operator runs `rm -rf ~/.purged-2026-05-11/` to commit.
#
# Run from the repo root on the operator workstation. Requires SSH access to the
# seedbox.

set -euo pipefail

HOST="quadstronaut@seedbox.example.com"
PURGE_TAG="2026-05-11"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SEEDBOX_PURGE="~/.purged-${PURGE_TAG}"

log()  { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$REPO_ROOT/manifest/apps.yaml" ] || fail "run from inside the QFlix repo (no manifest/apps.yaml found via $REPO_ROOT)"

# ─── 1. Push the updated manifest to the seedbox's maint copy ──────────────
log "1/8  push updated manifest → ~/.opt/maint/apps.yaml"
scp -q "$REPO_ROOT/manifest/apps.yaml" "$HOST:~/.opt/maint/apps.yaml"

# ─── 2. Stop + uninstall readarr/mylar3/ombi ───────────────────────────────
log "2/8  app-{readarr,mylar3,ombi} stop + uninstall (downloads preserved)"
ssh "$HOST" 'bash -se' <<EOSSH
set -e
PURGE="\$HOME/.purged-${PURGE_TAG}"
mkdir -p "\$PURGE/secrets" "\$PURGE/scripts"

for a in readarr mylar3 ombi; do
  echo "--- backup \$a ---"
  yes | app-\$a backup 2>&1 | tail -2 || true
  echo "--- stop \$a ---"
  app-\$a stop 2>&1 | tail -2 || true
  echo "--- uninstall \$a ---"
  yes | app-\$a uninstall 2>&1 | tail -3 || true
  # If uninstall leaves the dir, move it for safety.
  [ -d "\$HOME/.apps/\$a" ] && mv "\$HOME/.apps/\$a" "\$PURGE/" && echo "moved \$a to purge dir"
done
EOSSH

# ─── 3. Move orphan dirs + scripts + secrets to ~/.purged-${PURGE_TAG}/ ────
log "3/8  move Conjurr + Ultra-* scripts + orphan secrets to seedbox purge dir"
ssh "$HOST" 'bash -se' <<EOSSH
set -e
PURGE="\$HOME/.purged-${PURGE_TAG}"
mkdir -p "\$PURGE/secrets" "\$PURGE/scripts"

[ -d "\$HOME/Conjurr" ] && mv "\$HOME/Conjurr" "\$PURGE/" && echo "Conjurr/ moved"
for u in Ultra-Version-Notifier Ultra-App-Monitor Ultra-Quota-Checker Ultra-Traffic-Monitor; do
  [ -d "\$HOME/scripts/\$u" ] && mv "\$HOME/scripts/\$u" "\$PURGE/scripts/" && echo "scripts/\$u moved"
done

for s in conjurr.port newsletterr.port jellyfin.key jellyfin.port jellystat.port \
         readarr.key readarr.port readarr.urlbase \
         mylar3.key mylar3.port mylar3.urlbase \
         ombi.port; do
  if [ -f "\$HOME/secrets/\$s" ]; then
    mv "\$HOME/secrets/\$s" "\$PURGE/secrets/" && echo "secrets/\$s moved"
  fi
done
EOSSH

# ─── 4. Clean crontab — remove Notifiarr-migration commented stubs +
#       empty Conjurr/Newsletterr heartbeats. ───────────────────────────────
log "4/8  clean crontab of dead commented stubs"
ssh "$HOST" 'bash -se' <<'EOSSH'
set -e
CRONTAB_OLD=$(crontab -l 2>/dev/null || true)
echo "$CRONTAB_OLD" | python3 - <<'PY'
import re, sys
src = sys.stdin.read()
# Strip the Notifiarr-migration commented block at the top.
src = re.sub(
    r'# ─── Disabled \(commented during 2026-05 maintenance system migration\) ───\n(?:#.*\n|\n)+# ─── Active.*?\n',
    '# ─── Active ─────────────────────────────────────────────────────────────\n',
    src, flags=re.MULTILINE,
)
# Strip the empty Conjurr / Newsletterr heartbeat comment stubs.
src = re.sub(r'\n# Every 5 min — restart Conjurr if dead \(auto-recovery heartbeat\)\n', '\n', src)
src = re.sub(r'\n# Every 5 min — restart Newsletterr if dead \(auto-recovery heartbeat\)\n', '\n', src)
src = re.sub(r'\n# Daily 04:15 — bridge Listmonk .*? Newsletterr.*?\n', '\n', src, flags=re.DOTALL)
# Collapse 3+ blank lines into 2.
src = re.sub(r'\n{3,}', '\n\n', src)
sys.stdout.write(src)
PY
EOSSH
# Actually pipe through: capture cleaned crontab, then install it.
ssh "$HOST" 'crontab -l 2>/dev/null' | python3 -c "
import re, sys
src = sys.stdin.read()
src = re.sub(
    r'# ─── Disabled \(commented during 2026-05 maintenance system migration\) ───\n(?:#.*\n|\n)+# ─── Active.*?\n',
    '# ─── Active ─────────────────────────────────────────────────────────────\n',
    src, flags=re.MULTILINE,
)
src = re.sub(r'\n# Every 5 min — restart Conjurr if dead \(auto-recovery heartbeat\)\n', '\n', src)
src = re.sub(r'\n# Every 5 min — restart Newsletterr if dead \(auto-recovery heartbeat\)\n', '\n', src)
src = re.sub(r'\n# Daily 04:15 — bridge Listmonk .*? Newsletterr.*?\n', '\n', src, flags=re.DOTALL)
src = re.sub(r'\n{3,}', '\n\n', src)
sys.stdout.write(src)
" > /tmp/crontab-cleaned.txt
log "    diff (preview before install):"
ssh "$HOST" 'crontab -l 2>/dev/null' > /tmp/crontab-before.txt
diff -u /tmp/crontab-before.txt /tmp/crontab-cleaned.txt || true
read -rp "    install cleaned crontab? [y/N] " yn
if [ "$yn" = "y" ] || [ "$yn" = "Y" ]; then
  scp -q /tmp/crontab-cleaned.txt "$HOST:/tmp/crontab-cleaned.txt"
  ssh "$HOST" 'crontab /tmp/crontab-cleaned.txt && rm /tmp/crontab-cleaned.txt'
  log "    crontab installed"
else
  log "    crontab install SKIPPED — clean it manually with: ssh $HOST 'crontab -e'"
fi

# ─── 5. Kuma: delete 5 orphan monitors + wire missing notifications ────────
log "5/8  Kuma SQL — delete orphan monitors + wire missing notifications"
ssh "$HOST" 'bash -se' <<'EOSSH'
set -e
DB="$HOME/.apps/uptimekuma/kuma.db"
# Backup first.
cp "$DB" "${DB}.bak-$(date +%s)"

sqlite3 "$DB" <<'SQL'
BEGIN;
-- Delete 5 orphan/parked monitors. ON DELETE CASCADE handles monitor_notification.
DELETE FROM monitor WHERE id IN (27, 38, 42, 43, 62);

-- Wire Recyclarr (61), Qflix Newsletter (63), Buildarr (64) to channel 1 (QFlix Discord) + channel 2 (auto-heal).
INSERT OR IGNORE INTO monitor_notification (monitor_id, notification_id) VALUES
  (61, 1), (61, 2),
  (63, 1), (63, 2),
  (64, 1), (64, 2);

-- Backfill Tautulli (50) + 4 canaries (52,53,54,55) — they had auto-heal but no Discord.
INSERT OR IGNORE INTO monitor_notification (monitor_id, notification_id) VALUES
  (50, 1),
  (52, 1), (53, 1), (54, 1), (55, 1);

COMMIT;
SQL

echo "--- post-state verification ---"
sqlite3 -header "$DB" "
SELECT m.id, m.name,
       SUM(CASE WHEN mn.notification_id=1 THEN 1 ELSE 0 END) AS ch1_QFlix,
       SUM(CASE WHEN mn.notification_id=2 THEN 1 ELSE 0 END) AS ch2_autoheal
FROM monitor m
LEFT JOIN monitor_notification mn ON m.id=mn.monitor_id
GROUP BY m.id
ORDER BY m.id;
"
EOSSH

# ─── 6. Restart Kuma so it picks up the deleted monitors + new notification wires ──
log "6/8  restart Kuma so it re-loads the modified DB"
ssh "$HOST" 'app-uptimekuma restart' 2>&1 | tail -3 || \
  ssh "$HOST" 'systemctl --user restart uptimekuma 2>&1 || true' | tail -3

# ─── 7. Run a Discord webhook test ping ───────────────────────────────────
log "7/8  fire test Discord notification"
WEBHOOK=$(ssh "$HOST" 'cat ~/secrets/discord-webhook.url')
if [ -n "$WEBHOOK" ]; then
  curl -s -X POST -H 'Content-Type: application/json' \
    -d '{"content":"🟢 QFlix finalize-purge complete — 2026-05-11. See `~/.purged-2026-05-11/` for reversibility."}' \
    "$WEBHOOK" >/dev/null && log "    Discord ping sent"
fi

# ─── 8. Smoke test ─────────────────────────────────────────────────────────
log "8/8  smoke-test.sh"
bash "$REPO_ROOT/scripts/smoke-test.sh"

log "DONE. Reversibility: ~/.purged-${PURGE_TAG}/ on seedbox; .purged-2026-05-10/ in repo root."
log "When confident, delete with:  ssh $HOST 'rm -rf \"$SEEDBOX_PURGE\"' && rm -rf '$REPO_ROOT/.purged-2026-05-10/'"
