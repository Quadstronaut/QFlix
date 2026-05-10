#!/usr/bin/env bash
# Phase 13.4 — point user nginx root at the Homarr public board.
# Adds a `location = /` redirect to https://homarr-upstream-quadstronaut.../boards/public.
# Idempotent: if the marker comment is already present, does nothing.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"

CONF="\$HOME/.apps/nginx/sites-available/default"
MARKER="# manitoba-homarr-root-redirect"
TARGET_HOST="homarr-upstream-quadstronaut.seedbox.example.com"
TARGET_PATH="/boards/public"

read -r -d '' SCRIPT <<EOF || true
set -e
if grep -q '${MARKER}' ${CONF}; then
  echo "[skip] redirect already installed"
  exit 0
fi

cp ${CONF} ${CONF}.bak.\$(date +%s)

# Insert a location = / redirect block immediately before the existing 'location /' line.
# The existing block stays so that other paths (autoindex, css, etc.) still work.
python3 - <<'PY'
import re, sys
from pathlib import Path
p = Path("${CONF}".replace(r"\$HOME", str(Path.home())))
text = p.read_text()
inject = """
    ${MARKER}
    location = / {
        auth_basic              "Private Area";
        auth_basic_user_file    /home/quadstronaut/www/.htpasswd;
        return 302 https://${TARGET_HOST}${TARGET_PATH};
    }

"""
# anchor: the first 'location / {' (with autoindex)
m = re.search(r"^( {4}location / \{)", text, re.M)
if not m:
    print("could not find anchor 'location / {' — aborting", file=sys.stderr)
    sys.exit(2)
text = text[:m.start()] + inject + text[m.start():]
p.write_text(text)
print("[ok] injected redirect block")
PY

# Test config + reload
~/.apps/nginx/conf/../../../bin/nginx -t -c ~/.apps/nginx/nginx.conf 2>&1 | tail -5 || true
nginx -t -c ~/.apps/nginx/nginx.conf 2>&1 | tail -5 || true
~/.apps/nginx/conf/../../../bin/nginx -s reload -c ~/.apps/nginx/nginx.conf 2>/dev/null || \\
  systemctl --user reload nginx 2>/dev/null || \\
  pkill -HUP -f 'nginx.*master' || true
echo "[ok] nginx reloaded"
EOF

sshm "$SCRIPT"
log_info "verifying:"
sleep 2
HTPW="$(cat "$(dirname "$HERE")/secrets/htpasswd.password")"
RC=$(curl -sko /dev/null -m 10 -u "quadstronaut:$HTPW" -w "%{http_code}" "https://quadstronaut.seedbox.example.com/")
LOC=$(curl -sko /dev/null -m 10 -u "quadstronaut:$HTPW" -D - "https://quadstronaut.seedbox.example.com/" | grep -i '^location' | tr -d '\r')
log_info "/ -> HTTP $RC, $LOC"
if [ "$RC" = "302" ] && printf '%s' "$LOC" | grep -q homarr; then
  log_info "redirect confirmed"
else
  log_info "redirect NOT in place — manual investigation needed"
  exit 1
fi
