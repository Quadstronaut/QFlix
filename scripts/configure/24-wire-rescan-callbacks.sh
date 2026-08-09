#!/usr/bin/env bash
# Wire post-import rescan callbacks (Phase 8.9-8.10).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

# Step 1: Readarr CustomScript Connect (run python on remote)
scpm_to "$HERE/configure/24-wire-rescan-callbacks.py" /tmp/24.py
sshm "READARR_KEY='$(secret_read readarr.key)' \
      READARR_PORT='$(secret_read readarr.port)' \
      READARR_BASE='$(secret_read readarr.urlbase)' \
      python3 /tmp/24.py"

# Step 2: Mylar3 extra_scripts (single helper script that runs both komga + kavita)
log_info "Mylar3 extra_scripts (single combo script):"
sshm 'cat > ~/scripts/post-import/library-rescan-comics.sh << "EOF"
#!/usr/bin/env bash
/home/quadstronaut/scripts/post-import/library-rescan.sh komga
/home/quadstronaut/scripts/post-import/library-rescan.sh kavita
EOF
chmod +x ~/scripts/post-import/library-rescan-comics.sh

CRUDINI="$HOME/.local/bin/crudini"
[ -x "$CRUDINI" ] || CRUDINI=$(which crudini)
CFG="$HOME/.apps/mylar3/mylar/config.ini"
SECTION=$(awk "/^\[/{s=\$0} /^enable_extra_scripts/{print s; exit}" "$CFG" | tr -d "[]")
echo "  mylar3 section for extra_scripts: $SECTION"
"$CRUDINI" --set "$CFG" "$SECTION" enable_extra_scripts True
"$CRUDINI" --set "$CFG" "$SECTION" extra_scripts "/home/quadstronaut/scripts/post-import/library-rescan-comics.sh"
echo "  set extra_scripts -> library-rescan-comics.sh (komga + kavita)"
app-mylar3 restart 2>&1 | tail -2
echo "  mylar3 restarted"
'
