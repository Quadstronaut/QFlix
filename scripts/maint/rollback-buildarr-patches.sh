#!/usr/bin/env bash
# Roll the buildarr venv + config + manifest back to the 2026-05-11 06:51 CEST
# pre-autonomous-fix snapshot. Use when an autonomous-patching session goes
# sideways. Idempotent — safe to re-run.
#
# Snapshot was taken before launching the autonomous "fix buildarr against
# Sonarr v4 / Radarr v5" session and captures:
#   - ~/.apps/buildarr/buildarr.yml
#   - venv-installed buildarr_sonarr/config/profiles/release.py (+ its .bak)
#   - venv-installed radarr/models/colon_replacement_format.py (+ its .bak)
#   - deployed ~/.opt/maint/apps.yaml
# Plus a pip freeze at ~/.purged-2026-05-11/buildarr-pip-freeze-pre-autonomous.txt
# for re-creating the exact dependency set if a plugin got upgraded mid-session.
#
# What this DOES NOT touch (deliberately, since the autonomous session is
# only allowed to mutate buildarr-internal state):
#   - The 4 heartbeat scripts (XDG_RUNTIME_DIR fix is independent of buildarr)
#   - nginx proxy.d (the 2026-05-11 sweep is independent)
#   - The systemd_oneshot probe code in lib/health.py + lib/lifecycle.py +
#     lib/recovery.py (still useful for the other 4 timer apps)
#   - The Kuma DB
#   - Any non-buildarr secrets
#
# If those got modified, the autonomous session broke the rules and you need
# manual investigation — not just this rollback.

set -euo pipefail

SNAPSHOT="$HOME/.purged-2026-05-11/buildarr-pre-autonomous-fix.tar.gz"
FREEZE="$HOME/.purged-2026-05-11/buildarr-pip-freeze-pre-autonomous.txt"

if [ ! -f "$SNAPSHOT" ]; then
  echo "FATAL: snapshot not found at $SNAPSHOT" >&2
  echo "       Either the autonomous-fix session was never launched (no rollback needed)" >&2
  echo "       or the snapshot was deleted. Check ~/.purged-2026-05-11/ for alternates." >&2
  exit 2
fi

echo "[1/6] Stopping buildarr.service if running"
systemctl --user stop buildarr.service 2>/dev/null || true
systemctl --user reset-failed buildarr.service 2>/dev/null || true

echo "[2/6] Restoring files from snapshot"
# tar was created with --absolute-names so paths start with / once extracted
tar xzf "$SNAPSHOT" -C / --absolute-names

echo "[3/6] Verifying file ownership + modes"
chown quadstronaut:quadstronaut "$HOME/.apps/buildarr/buildarr.yml"
chmod 600 "$HOME/.apps/buildarr/buildarr.yml"

echo "[4/6] Clearing bytecode caches for the buildarr-sonarr + radarr packages"
find "$HOME/.apps/buildarr/.venv/lib/python3.11/site-packages/buildarr_sonarr" \
     "$HOME/.apps/buildarr/.venv/lib/python3.11/site-packages/buildarr_radarr" \
     "$HOME/.apps/buildarr/.venv/lib/python3.11/site-packages/radarr" \
     -name '*.pyc' -delete 2>/dev/null || true

echo "[5/6] Comparing current pip freeze against snapshot"
DIFF_LINES=$(diff <(~/.apps/buildarr/.venv/bin/pip freeze 2>/dev/null) "$FREEZE" | wc -l || echo "?")
if [ "$DIFF_LINES" != "0" ]; then
  echo "  WARNING: pip freeze drift detected (${DIFF_LINES} lines). To restore exact deps:"
  echo "    ~/.apps/buildarr/.venv/bin/pip install -r $FREEZE"
  echo "  (Not running automatically — review the diff first with:"
  echo "    diff <(~/.apps/buildarr/.venv/bin/pip freeze) $FREEZE )"
fi

echo "[6/6] Restarting pusher to pick up restored manifest"
systemctl --user restart manitoba-maint-pusher.service
sleep 12
PUSHER_STATE=$(systemctl --user is-active manitoba-maint-pusher.service)
echo "  pusher: $PUSHER_STATE"

echo
echo "--- VERIFICATION ---"
echo "buildarr.service state:"
systemctl --user show buildarr.service -p ActiveState -p Result | sed 's/^/  /'
echo "Kuma Buildarr monitor (should be status=1, msg=active):"
sqlite3 "$HOME/.apps/uptimekuma/kuma.db" \
  "SELECT name,status,datetime(time,'localtime'),substr(msg,1,40) FROM heartbeat h JOIN monitor m ON h.monitor_id=m.id WHERE h.id=(SELECT MAX(id) FROM heartbeat WHERE monitor_id=m.id) AND name='Buildarr';" \
  | sed 's/^/  /'

echo
echo "Rollback complete. Buildarr is back to the pre-autonomous-fix state:"
echo "  - buildarr.yml is the original commented-out template (no plugins configured)"
echo "  - 3 venv patches from 2026-05-11 are in place (preferred + IPWR optional, SMART enum)"
echo "  - manifest entry is on legacy systemd_only probe against buildarr.timer"
echo "  - Kuma monitor stays green via the legacy probe; Discord won't ping"
