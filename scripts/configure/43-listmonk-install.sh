#!/usr/bin/env bash
# Phase 19 — Listmonk 6.1.0 install. Idempotent.
#  - DB create on existing Postgres 17.9 (127.0.0.1:42009)
#  - binary into ~/.apps/listmonk/bin/
#  - config.toml with admin user/pass (admin reuses secrets/htpasswd.password)
#  - --install --idempotent --yes (schema bootstrap)
#  - user-systemd service + heartbeat cron
#  - nginx /listmonk/ fragment
#  - Root URL set via API (operator UI step eliminated)
#  - SMTP wired via API (Gmail App Password from secrets/listmonk.smtp_password)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"
source "$HERE/../lib/secrets.sh"

LM_VER="6.1.0"
LM_URL="https://github.com/knadh/listmonk/releases/download/v${LM_VER}/listmonk_${LM_VER}_linux_amd64.tar.gz"
PUBLIC_HOST="quadstronaut.seedbox.example.com"
ROOT_URL="https://${PUBLIC_HOST}/listmonk"

# ── Step 1: claim port ──────────────────────────────────────────────────────
if ! secret_exists listmonk.port; then
  PORT=$(sshm 'app-ports free 2>/dev/null | grep -E "^[0-9]+$" | head -1')
  [ -n "$PORT" ] || die "app-ports free returned no port"
  secret_write listmonk.port "$PORT"
  log_info "claimed listmonk port $PORT"
fi
PORT=$(secret_read listmonk.port)
log_info "listmonk port = $PORT"

# ── Step 2: admin user/pw (reuse htpasswd.password) ─────────────────────────
secret_exists listmonk.admin_user || secret_write listmonk.admin_user "quadstronaut"
[ -f secrets/htpasswd.password ] || die "missing secrets/htpasswd.password"
[ -f secrets/listmonk.smtp_password ] || die "missing secrets/listmonk.smtp_password"
ADMIN_USER=$(secret_read listmonk.admin_user)
ADMIN_PASS=$(secret_read htpasswd.password)
SMTP_PASS=$(secret_read listmonk.smtp_password)

# ── Step 3: decode postgres password ────────────────────────────────────────
PG_PASS=$(sshm 'base64 -d ~/.apps/postgres/.encoded.dat | head -c 24')
[ -n "$PG_PASS" ] || die "could not decode postgres password"

# ── Step 4: create DB if absent ─────────────────────────────────────────────
sshm "PGPASSWORD='${PG_PASS}' psql -h 127.0.0.1 -p 42009 -U quadstronaut -d postgres -tc \
  \"SELECT 1 FROM pg_database WHERE datname = 'listmonk'\" | grep -q 1 \
  || PGPASSWORD='${PG_PASS}' psql -h 127.0.0.1 -p 42009 -U quadstronaut -d postgres \
       -c 'CREATE DATABASE listmonk OWNER quadstronaut'"
log_info "listmonk DB ready"

# ── Step 5: download binary (skip if already at correct version) ────────────
sshm "bash -s" <<EOF
set -euo pipefail
mkdir -p ~/.apps/listmonk/{bin,etc,logs,uploads}
NEED_DL=1
if [ -x ~/.apps/listmonk/bin/listmonk ]; then
  V=\$(~/.apps/listmonk/bin/listmonk --version 2>&1 | grep -oE 'v?[0-9]+\.[0-9]+\.[0-9]+' | head -1 | tr -d v)
  [ "\$V" = "${LM_VER}" ] && NEED_DL=0
fi
if [ "\$NEED_DL" = "1" ]; then
  cd /tmp
  curl -fsSL "${LM_URL}" -o listmonk.tgz
  tar -xzf listmonk.tgz
  mv listmonk ~/.apps/listmonk/bin/listmonk
  rm -f listmonk.tgz config.toml.sample 2>/dev/null || true
  chmod +x ~/.apps/listmonk/bin/listmonk
fi
~/.apps/listmonk/bin/listmonk --version 2>&1 | head -1
EOF

# ── Step 6: write config.toml ───────────────────────────────────────────────
sshm "PORT='${PORT}' ADMIN_USER='${ADMIN_USER}' ADMIN_PASS='${ADMIN_PASS}' PG_PASS='${PG_PASS}' bash -s" <<'CFGSCRIPT'
cat > ~/.apps/listmonk/etc/config.toml <<TOML
[app]
address = "127.0.0.1:${PORT}"
admin_username = "${ADMIN_USER}"
admin_password = "${ADMIN_PASS}"

[db]
host = "127.0.0.1"
port = 42009
user = "quadstronaut"
password = "${PG_PASS}"
database = "listmonk"
ssl_mode = "disable"
max_open = 25
max_idle = 25
max_lifetime = "300s"
TOML
chmod 600 ~/.apps/listmonk/etc/config.toml
CFGSCRIPT

# ── Step 7: --install schema (idempotent) ───────────────────────────────────
sshm "cd ~/.apps/listmonk && \
  LISTMONK_ADMIN_USER='${ADMIN_USER}' \
  LISTMONK_ADMIN_PASSWORD='${ADMIN_PASS}' \
  ./bin/listmonk --config etc/config.toml --install --idempotent --yes 2>&1 | tail -20"
log_info "listmonk schema bootstrapped"

# ── Step 8: user-systemd service ────────────────────────────────────────────
sshm "bash -s" <<'UNITSCRIPT'
set -euo pipefail
cat > ~/.config/systemd/user/listmonk.service <<'UNIT'
[Unit]
Description=Listmonk newsletter / mailing list manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.apps/listmonk
ExecStart=%h/.apps/listmonk/bin/listmonk --config %h/.apps/listmonk/etc/config.toml
Restart=on-failure
RestartSec=5s
TimeoutStopSec=20
StandardOutput=append:%h/.apps/listmonk/logs/listmonk.log
StandardError=append:%h/.apps/listmonk/logs/listmonk.err

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable listmonk.service
systemctl --user restart listmonk.service
UNITSCRIPT
sleep 4
sshm 'systemctl --user is-active listmonk.service' | grep -q active || die "listmonk not active"
log_info "listmonk.service active"

# ── Step 9: heartbeat cron ──────────────────────────────────────────────────
sshm 'mkdir -p ~/scripts/ops'
scpm_to "$HERE/../ops/heartbeat-listmonk.sh" '~/scripts/ops/heartbeat-listmonk.sh'
sshm 'chmod +x ~/scripts/ops/heartbeat-listmonk.sh && (crontab -l 2>/dev/null | grep -v heartbeat-listmonk; echo "*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-listmonk.sh") | crontab -'
log_info "heartbeat cron installed"

# ── Step 10: nginx /listmonk/ fragment ──────────────────────────────────────
sshm "PORT=${PORT} bash -s" <<'NGXSCRIPT'
cat > ~/.apps/nginx/proxy.d/listmonk.conf <<NGX
location /listmonk/ {
    # Listmonk handles its own auth on admin paths; public unsubscribe paths
    # (/subscription/*, /uc/*) MUST be reachable without htpasswd.
    auth_basic off;

    proxy_pass http://127.0.0.1:${PORT}/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-Prefix /listmonk;

    # Listmonk redirects (e.g. /admin/ -> /admin/login) emit a Location header
    # without the subpath prefix; rewrite so users stay inside /listmonk/.
    proxy_redirect / /listmonk/;

    # v6.1.0 public-site templates hard-code absolute "/public/..." asset paths
    # (baked into the Go binary, ignores app.root_url). Rewrite on the fly so
    # CSS/JS/images resolve under /listmonk/. Upstream: knadh/listmonk#824.
    proxy_set_header Accept-Encoding "";
    sub_filter_once off;
    sub_filter 'href="/public/' 'href="/listmonk/public/';
    sub_filter 'src="/public/'  'src="/listmonk/public/';
}
NGX
/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t
systemctl --user reload nginx
NGXSCRIPT
log_info "nginx /listmonk/ fragment live"

# ── Step 11: wait for HTTP, then set Root URL via API ───────────────────────
sleep 2
for i in 1 2 3 4 5 6 7 8; do
  if curl -sfk -m 5 -u "${ADMIN_USER}:${ADMIN_PASS}" \
       "https://${PUBLIC_HOST}/listmonk/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
HEALTH=$(curl -sfk -m 5 -u "${ADMIN_USER}:${ADMIN_PASS}" "https://${PUBLIC_HOST}/listmonk/api/health" || true)
echo "$HEALTH" | grep -q '"data":true' \
  && log_info "✓ listmonk /api/health = ok" \
  || die "listmonk health check failed: $HEALTH"

# ── Step 12: configure Root URL + SMTP via API ──────────────────────────────
# GET current settings, mutate root URL + SMTP block, POST back.
TMP_SETTINGS=$(mktemp)
trap 'rm -f "$TMP_SETTINGS"' EXIT
curl -sfk -m 10 -u "${ADMIN_USER}:${ADMIN_PASS}" \
     "https://${PUBLIC_HOST}/listmonk/api/settings" -o "$TMP_SETTINGS"
[ -s "$TMP_SETTINGS" ] || die "could not GET listmonk settings"

python3 - "$TMP_SETTINGS" "$ROOT_URL" "$SMTP_PASS" <<'PY' > "$TMP_SETTINGS.new"
import json, sys
path, root_url, smtp_pass = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(path))["data"]
data["app.root_url"] = root_url
data["app.from_email"] = "Manitoba Media <operator@example.com>"
data["smtp"] = [{
    "uuid": "00000000-0000-0000-0000-000000000001",
    "enabled": True,
    "host": "smtp.gmail.com",
    "port": 587,
    "auth_protocol": "login",
    "username": "operator@example.com",
    "password": smtp_pass,
    "email_headers": [],
    "hello_hostname": "seedbox.example.com",
    "max_conns": 10,
    "max_msg_retries": 2,
    "idle_timeout": "15s",
    "wait_timeout": "5s",
    "send_timeout": "10s",
    "tls_type": "STARTTLS",
    "tls_skip_verify": False,
}]
print(json.dumps(data))
PY

curl -sfk -m 15 -u "${ADMIN_USER}:${ADMIN_PASS}" -X PUT \
     -H "Content-Type: application/json" \
     --data-binary "@${TMP_SETTINGS}.new" \
     "https://${PUBLIC_HOST}/listmonk/api/settings" \
  | grep -q '"data":true' \
  && log_info "✓ Root URL + SMTP settings applied via API" \
  || log_warn "settings PUT did not return data:true — check admin UI"
rm -f "${TMP_SETTINGS}.new"

# Listmonk requires reload after settings change.
sshm 'systemctl --user restart listmonk.service'
sleep 3
sshm 'systemctl --user is-active listmonk.service' | grep -q active || die "listmonk did not restart cleanly"

# ── Step 13: provision API user + per-list role binding ────────────────────
# Listmonk v6+ requires:
#   - an api-type user (HTTP Basic with username:token works for /api/*)
#   - the user must be bound to a list-type role with explicit per-list grants
#     (lists:manage_all on the user role is NOT enough — the server enforces
#     per-list permissions at every subscriber/campaign operation)
# The legacy config.toml admin_user/admin_pass auth has no list_role_id and
# is rejected for list ops. After provisioning, we strip admin creds from
# config.toml so future restarts use the DB user model exclusively.

if ! secret_exists listmonk.api_token; then
  log_info "provisioning Listmonk API user..."

  # Step 13a: create the parent list-role + per-list children + bind to web user (id=1)
  PG_PASS_LM="$PG_PASS"
  sshm "PGPASSWORD='${PG_PASS_LM}' psql -h 127.0.0.1 -p 42009 -U quadstronaut -d listmonk" <<'PSQLSCRIPT'
DO $$
DECLARE
  list_role_id INT;
BEGIN
  -- Find or create the parent list role (idempotent on name).
  SELECT id INTO list_role_id FROM roles WHERE type='list' AND name='Manitoba List Access';
  IF list_role_id IS NULL THEN
    INSERT INTO roles (type, parent_id, list_id, permissions, name)
    VALUES ('list', NULL, NULL, '{}', 'Manitoba List Access')
    RETURNING id INTO list_role_id;
  END IF;

  -- Ensure each list has a child grant under that role.
  INSERT INTO roles (type, parent_id, list_id, permissions, name)
  SELECT 'list', list_role_id, l.id, '{list:get,list:manage}', NULL
  FROM lists l
  WHERE NOT EXISTS (
    SELECT 1 FROM roles r
    WHERE r.type='list' AND r.parent_id=list_role_id AND r.list_id=l.id
  );

  -- Bind the web user (the one created by --install) to this list role.
  UPDATE users SET list_role_id = list_role_id WHERE username = 'quadstronaut' AND list_role_id IS NULL;
END $$;
SELECT 'list_role_id=' || list_role_id FROM roles WHERE type='list' AND name='Manitoba List Access';
PSQLSCRIPT

  # Step 13b: temporarily restore config.toml admin creds so we can call /api/users
  sshm "bash -s" <<EOF
sed -i '/^address /a admin_username = "${ADMIN_USER}"\\nadmin_password = "${ADMIN_PASS}"' ~/.apps/listmonk/etc/config.toml
systemctl --user restart listmonk.service
EOF
  sleep 4

  # Step 13c: create the api-type user; Listmonk returns the token in the response
  LIST_ROLE_ID=$(sshm "PGPASSWORD='${PG_PASS_LM}' psql -h 127.0.0.1 -p 42009 -U quadstronaut -d listmonk -tA -c \"SELECT id FROM roles WHERE type='list' AND name='Manitoba List Access'\"")
  [ -n "$LIST_ROLE_ID" ] || die "could not find list role id"

  CREATE_OUT=$(curl -sfk -m 15 -u "${ADMIN_USER}:${ADMIN_PASS}" -X POST \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"api-bootstrap\",\"email\":\"api-bootstrap@manitoba.local\",\"name\":\"API bootstrap\",\"status\":\"enabled\",\"type\":\"api\",\"user_role_id\":1,\"list_role_id\":${LIST_ROLE_ID}}" \
    "https://${PUBLIC_HOST}/listmonk/api/users")
  TOKEN=$(echo "$CREATE_OUT" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['password'])")
  [ -n "$TOKEN" ] && [ "$TOKEN" != "null" ] || die "API user create failed: $CREATE_OUT"

  secret_write listmonk.api_user "api-bootstrap"
  secret_write listmonk.api_token "$TOKEN"
  log_info "API user provisioned, token saved to secrets/listmonk.api_token"
fi

# Step 13d: ensure config.toml has NO admin creds (every install ends here)
sshm "sed -i '/^admin_username/d; /^admin_password/d' ~/.apps/listmonk/etc/config.toml; systemctl --user restart listmonk.service"
sleep 3
sshm 'systemctl --user is-active listmonk.service' | grep -q active || die "listmonk did not restart cleanly after admin-strip"

log_info "Phase 19 complete — Listmonk admin: ${ROOT_URL}/admin (user: ${ADMIN_USER})"
