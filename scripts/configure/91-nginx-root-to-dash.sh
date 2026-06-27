#!/usr/bin/env bash
# CUTOVER — point user-nginx root at the QFlix Dashboard, replacing the Homarr
# root 302 redirect. Codifies the validated 2026-06-27 cutover. Runs from the
# workstation; SSHes in. Backs up the default site, edits it, validates with
# `nginx -t -p <prefix>` (gating on "syntax is ok" + no emerg), reloads via
# `app-nginx restart`, verifies root 200 + marker, and AUTO-ROLLS-BACK on any
# failure (a bad reload would take down ALL public services).
#
# Idempotent: re-running when the dash-root block is already present is a no-op.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOST="$(tr -d '[:space:]' < "$HERE/secrets/seedbox.ssh-host" 2>/dev/null \
        || tr -d '[:space:]' < "$HERE/secrets/seedbox.host")"
PORT="$(tr -d '[:space:]' < "$HERE/secrets/qflix-dash.port" 2>/dev/null || echo 42020)"

ssh -o BatchMode=yes -o ConnectTimeout=20 "quadstronaut@$HOST" PORT="$PORT" 'bash -l -s' <<'EOS'
set -uo pipefail
DEF=$HOME/.apps/nginx/sites-available/default
if grep -q 'manitoba-qflix-dash-root' "$DEF"; then echo "[skip] dash root already installed"; exit 0; fi
BAK=$DEF.bak.qflix.$(date +%s); cp "$DEF" "$BAK"; echo "backup: $BAK"
NGINX=""; for b in $HOME/.apps/nginx/sbin/nginx $HOME/bin/nginx $(command -v nginx 2>/dev/null); do [ -x "$b" ] && NGINX="$b" && break; done

PORT="${PORT:-42020}" python3 - "$DEF" <<PY
import re, sys, os, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text(); port = os.environ.get("PORT","42020")
t, n1 = re.subn(r'[ \t]*# manitoba-homarr-root-redirect\n[ \t]*location = / \{[\s\S]*?\n[ \t]*\}\n', '', t)
new_root = f"""    # manitoba-qflix-dash-root
    location / {{
        auth_basic              off;
        proxy_pass              http://127.0.0.1:{port};
        proxy_http_version      1.1;
        proxy_set_header        Host                 \$host;
        proxy_set_header        X-Forwarded-Host     \$http_host;
        proxy_set_header        X-Forwarded-Proto    \$scheme;
        proxy_set_header        Upgrade              \$http_upgrade;
        proxy_set_header        Connection           "upgrade";
    }}"""
t, n2 = re.subn(r'[ \t]*location / \{\n[ \t]*autoindex on;[\s\S]*?\n[ \t]*\}', new_root, t)
assert n1 == 1 and n2 == 1, f"n1={n1} n2={n2}"
p.write_text(t); print("edit ok")
PY
[ $? -eq 0 ] || { echo "EDIT FAILED"; cp "$BAK" "$DEF"; exit 1; }

TOUT=$("$NGINX" -t -p "$HOME/.apps/nginx" -c "$HOME/.apps/nginx/nginx.conf" 2>&1)
if ! echo "$TOUT" | grep -q "syntax is ok" || echo "$TOUT" | grep -q emerg; then
  echo "CONFIG INVALID:"; echo "$TOUT" | sed 's/^/  /'; cp "$BAK" "$DEF"; exit 1; fi
echo "nginx -t: syntax ok"
app-nginx restart >/dev/null 2>&1 || { echo "RELOAD FAILED"; cp "$BAK" "$DEF"; app-nginx restart >/dev/null 2>&1; exit 1; }
sleep 2
H="$(cat ~/secrets/seedbox.host)"
CODE=$(curl -sk -o /dev/null -w "%{http_code}" -H "Host: $H" http://127.0.0.1:17040/)
MARK=$(curl -sk -H "Host: $H" http://127.0.0.1:17040/ | grep -o data-qflix-dash | head -1)
if [ "$CODE" != "200" ] || [ "$MARK" != "data-qflix-dash" ]; then
  echo "VERIFY FAILED ($CODE/$MARK) — rollback"; cp "$BAK" "$DEF"; app-nginx restart >/dev/null 2>&1; exit 1; fi
echo "CUTOVER OK: root 200 + dashboard marker (backup $BAK)"
EOS
