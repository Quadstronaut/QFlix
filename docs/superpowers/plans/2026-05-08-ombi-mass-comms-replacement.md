# Ombi Mass-Comms Replacement Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Replace Ombi's mass-email function — the only Ombi capability the rest of the consolidated Manitoba ecosystem doesn't already cover — before stopping `app-ombi`. Three layered channels covering a non-technical user base (boomers, country folk, occasional youngins): browser dashboard widget (zero effort), email digests (zero effort), browser/phone push (one-tap subscribe).

**Architecture:** All-binary user-systemd installs reusing existing patterns. ntfy v2.22.0 for push, Listmonk v6.1.0 for email, single shared Postgres 17.9 (already running) for Listmonk's DB, Homarr widget for the dashboard channel. Same nginx + per-app proxy.d fragment + heartbeat-cron pattern as the rest of the stack.

**Tech Stack:** Bash (SSH-driven), Python 3 + `pg8000` (already pip-installed) + `sqlite3` for sync, ntfy native web app for push subscription UX (no custom service worker), Listmonk REST API for subscriber upsert, cron for heartbeats and nightly reconcile.

---

## Probe findings (verified 2026-05-08)

These are the load-bearing facts the plan rests on. If any of these change, re-probe before executing.

| Fact | Value | Source of truth |
|---|---|---|
| Postgres version | 17.9 (Debian pkg) | `SELECT version()` on `127.0.0.1:42009` |
| Postgres user/pass | `quadstronaut` / decode `~/.apps/postgres/.encoded.dat` (base64) | live connect verified |
| Postgres role | superuser, can `CREATE DATABASE` | `\du` on connect |
| Existing DBs | `jfstat`, `postgres`, `quadstronaut`, `template{0,1}` | `\l+` |
| nginx | `/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf`, listens `:17040`, `server_name quadstronaut.seedbox.example.com`, `include proxy.d/*.conf` | `~/.apps/nginx/sites-available/default` |
| nginx reload | `systemctl --user reload nginx` (the documented `app-nginx restart` is not actually implemented — only backup/install/migrate/repair) | `~/.config/systemd/user/nginx.service` |
| Public exposure model | **path-based only for custom installs** — `https://quadstronaut.seedbox.example.com/<app>/`. Subdomains are reserved for official Ultra.cc `app-*` installers. | [Ultra.cc generic-software-installation docs](https://docs.ultra.cc/unofficial-application-installers/generic-software-installation) |
| Port allocation | Must use ports from user's assigned range — discover via `app-ports free`. Using out-of-range ports = Fair Usage violation. Claimed for this plan: **42014 (ntfy), 42015 (Listmonk), 42016 (Conjurr), 42017 (Newsletterr)** from the free 42xxx range. | `app-ports show` / `app-ports free` |
| Public host TLS | Let's Encrypt valid until 2026-06-22, auto-renewed by Ultra.cc | `openssl s_client` |
| Ombi cohort | 13 unique emails (12 friends + 1 admin) — see Phase 20 for the list | `~/.apps/ombi/Ombi.db` `AspNetUsers` |
| Plex.tv friends | 3 (2 with email, 1 home) | `plex.tv/api/v2/friends?X-Plex-Token=…` |
| Jellyseerr users | 1 (admin only — Quadstronaut) | `127.0.0.1:17013/api/v1/user` |
| Jellyfin users | 1 (admin only, no email) | `127.0.0.1:17002/jellyfin/Users` |
| Disk free | 11T / 20T (45% used) | `df -h ~` |
| Python pg client | `pg8000 1.31.5` installed via `pip3 --user` | `python3 -c "import pg8000"` |
| ntfy release | v2.22.0 (2026-04-21) — `ntfy_2.22.0_linux_amd64.tar.gz` | GitHub API |
| Listmonk release | v6.1.0 (2026-03-29) — `listmonk_6.1.0_linux_amd64.tar.gz` | GitHub API |
| Docker socket | denied for user — all installs MUST be binary | confirmed prior session |
| Existing user-systemd services | logrotate, nginx, qbittorrent, qbt_pub | `~/.config/systemd/user/` |

---

## Conventions

- `SSHM` shorthand: `ssh -o BatchMode=yes quadstronaut@seedbox.example.com` (defined in `scripts/lib/ssh.sh`).
- All install scripts idempotent. Re-running a phase must not double-install or break running services.
- Secrets in gitignored `secrets/` (one file per name). Scripts use `secret_read`/`secret_write` from `scripts/lib/secrets.sh`.
- Ports allocated via `app-ports free` (Ultra.cc-blessed discovery). Custom apps in this plan claim from the 42xxx range: ntfy=42014, Listmonk=42015, Conjurr=42016, Newsletterr=42017. Out-of-range ports trigger Fair Use enforcement.
- Domain pattern: **path-based** under the main host: `https://quadstronaut.seedbox.example.com/<app>/`. Per Ultra.cc docs, custom (unofficial) installs cannot register subdomains — only official `app-*` installers get them. Side benefit: same-origin makes Web Push trivial, no cross-host service-worker scope issues.
- `Co-Authored-By: Claude Opus 4.7` on each commit.

---

## Phase 17 — ntfy server install — **SKIPPED 2026-05-08**

**Reason:** Two-sided architectural blocker discovered during execution.
1. ntfy upstream refuses sub-path hosting at runtime — `if set, base-url must not have a path (/ntfy)`. Hosting on a sub-path is explicitly unsupported (issue tracker confirms long-standing).
2. Ultra.cc's edge proxy only routes subdomains it has pre-registered for official `app-*` installers — `ntfy-quadstronaut.seedbox.example.com` returns 403 from their edge nginx (the *.seedbox.example.com wildcard TLS cert covers it, but no upstream is wired). Custom subdomains require a support ticket which breaks continuous execution.

**Operator decision (2026-05-08):** drop the push channel entirely. The remaining two channels (Homarr Recently-Added widget + Newsletterr weekly email + existing Discord/Notifiarr) cover the user base. Phase 18 (`/alerts/`) is also skipped — it existed only as a launch-pad for ntfy subscribe.

The original Phase 17 spec is preserved below for archive only. Do not execute.

---



### Task 17.1: Download + place binary

**Files:**
- Create: `scripts/configure/40-ntfy-install.sh`

- [ ] **Step 1: Write installer**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"
source "$HERE/../lib/secrets.sh"

NTFY_VER="2.22.0"
NTFY_URL="https://github.com/binwiederhier/ntfy/releases/download/v${NTFY_VER}/ntfy_${NTFY_VER}_linux_amd64.tar.gz"

# Claim a port from the user's assigned range via Ultra.cc's app-ports tool.
# Default claim: 42014 (next free in 42xxx range). If taken, fall back to first free.
if ! secret_exists ntfy.port; then
  PORT=$(sshm 'app-ports free 2>/dev/null | grep -E "^[0-9]+$" | head -1')
  [ -n "$PORT" ] || die "app-ports free returned no port; check user allocation"
  secret_write ntfy.port "$PORT"
  log_info "allocated ntfy port $PORT (from app-ports free)"
fi
PORT=$(secret_read ntfy.port)

sshm bash <<EOF
set -euo pipefail
mkdir -p ~/.apps/ntfy/{bin,cache,lib,etc}
if [ ! -x ~/.apps/ntfy/bin/ntfy ] || ! ~/.apps/ntfy/bin/ntfy --version 2>&1 | grep -q "${NTFY_VER}"; then
  cd /tmp
  curl -fsSL "${NTFY_URL}" -o ntfy.tgz
  tar -xzf ntfy.tgz
  mv "ntfy_${NTFY_VER}_linux_amd64/ntfy" ~/.apps/ntfy/bin/ntfy
  rm -rf "ntfy_${NTFY_VER}_linux_amd64" ntfy.tgz
fi
~/.apps/ntfy/bin/ntfy --version
EOF
```

- [ ] **Step 2: Run + verify**

```
sshm '~/.apps/ntfy/bin/ntfy --version' should print "ntfy 2.22.0 ..."
```

### Task 17.2: Generate VAPID keys + write server.yml

**Files:**
- Modify: `scripts/configure/40-ntfy-install.sh`

- [ ] **Step 1: Generate VAPID once and capture**

```bash
if ! secret_exists ntfy.webpush_public; then
  KEYS=$(sshm '~/.apps/ntfy/bin/ntfy webpush keys' 2>&1)
  PUB=$(echo "$KEYS" | grep -E 'public-key' | awk '{print $NF}')
  PRIV=$(echo "$KEYS" | grep -E 'private-key' | awk '{print $NF}')
  [ -n "$PUB" ] && [ -n "$PRIV" ] || die "VAPID gen failed: $KEYS"
  secret_write ntfy.webpush_public  "$PUB"
  secret_write ntfy.webpush_private "$PRIV"
fi
PUB=$(secret_read ntfy.webpush_public)
PRIV=$(secret_read ntfy.webpush_private)
TOPIC="manitoba-new-media"
secret_exists ntfy.topic || secret_write ntfy.topic "$TOPIC"
```

- [ ] **Step 2: Write server.yml on the seedbox**

The file lives at `~/.apps/ntfy/etc/server.yml`. `behind-proxy: true` so client IPs are read from `X-Forwarded-For`. Web push enabled. Auth deny-all-by-default with the `manitoba-new-media` topic granted public read-write so anyone with the URL can subscribe (the topic name is unguessable to anyone without it).

```bash
sshm "cat > ~/.apps/ntfy/etc/server.yml" <<YML
base-url: "https://quadstronaut.seedbox.example.com/ntfy"
listen-http: "127.0.0.1:${PORT}"
behind-proxy: true

cache-file: "/home/quadstronaut/.apps/ntfy/cache/cache.db"
cache-duration: "24h"
cache-startup-queries: "pragma journal_mode = WAL; pragma synchronous = normal;"

attachment-cache-dir: "/home/quadstronaut/.apps/ntfy/cache/attachments"
attachment-total-size-limit: "1G"
attachment-file-size-limit: "10M"

auth-file: "/home/quadstronaut/.apps/ntfy/lib/auth.db"
auth-default-access: "deny-all"
enable-signup: false
enable-login: true
enable-reservations: false

web-push-public-key:  "${PUB}"
web-push-private-key: "${PRIV}"
web-push-file: "/home/quadstronaut/.apps/ntfy/lib/webpush.db"
web-push-email-address: "operator@example.com"

upstream-base-url: ""
visitor-request-limit-burst: 60
visitor-request-limit-replenish: "5s"
YML
```

- [ ] **Step 3: Grant the topic public READ access only**

```bash
sshm '~/.apps/ntfy/bin/ntfy access --config ~/.apps/ntfy/etc/server.yml "*" manitoba-new-media read-only'
```

Wildcard ACL gives everyone READ (subscribe) access. Write (publish) access is gated behind the `publisher` token created in 17.4 — strangers with the topic URL cannot spam the channel.

### Task 17.3: User-systemd service + heartbeat cron

**Files:**
- Create: `scripts/configure/40-ntfy-install.sh` (continued)
- Create: `scripts/ops/heartbeat-ntfy.sh`

- [ ] **Step 1: Write the service unit**

```bash
sshm "cat > ~/.config/systemd/user/ntfy.service" <<'UNIT'
[Unit]
Description=ntfy push notification server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.apps/ntfy/bin/ntfy serve --config %h/.apps/ntfy/etc/server.yml
Restart=on-failure
RestartSec=5s
TimeoutStopSec=20
StandardOutput=append:%h/.apps/ntfy/logs/ntfy.log
StandardError=append:%h/.apps/ntfy/logs/ntfy.err

[Install]
WantedBy=default.target
UNIT
sshm 'mkdir -p ~/.apps/ntfy/logs && systemctl --user daemon-reload && systemctl --user enable --now ntfy.service'
sleep 2
sshm 'systemctl --user is-active ntfy.service' | grep -q active || die "ntfy not active"
```

- [ ] **Step 2: Heartbeat script**

```bash
cat > scripts/ops/heartbeat-ntfy.sh <<'EOF'
#!/usr/bin/env bash
# Restart ntfy if dead. Quiet on success. Reads port from server.yml so it stays in sync.
PORT=$(grep -oP 'listen-http:.*:\K[0-9]+' ~/.apps/ntfy/etc/server.yml)
curl -sfm 5 "http://127.0.0.1:${PORT}/v1/health" >/dev/null && exit 0
systemctl --user is-active ntfy.service >/dev/null && exit 0
logger -t ntfy-heartbeat "ntfy unhealthy — restarting"
systemctl --user restart ntfy.service
EOF
chmod +x scripts/ops/heartbeat-ntfy.sh
scpm_to scripts/ops/heartbeat-ntfy.sh '~/scripts/ops/heartbeat-ntfy.sh'
sshm "(crontab -l 2>/dev/null | grep -v heartbeat-ntfy; echo '*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-ntfy.sh') | crontab -"
```

### Task 17.4: nginx path-based fragment

**Files:**
- Create: `scripts/configure/40-ntfy-install.sh` (continued)

ntfy supports `base-url` with a path prefix natively (set in 17.2). nginx forwards `/ntfy/...` → `127.0.0.1:42014/...`, ntfy emits links rooted at `/ntfy/...`, browser sees a single same-origin app under `quadstronaut.seedbox.example.com`.

- [ ] **Step 1: Drop the proxy.d fragment**

```bash
sshm "cat > ~/.apps/nginx/proxy.d/ntfy.conf" <<EOF
# Public — anyone with the URL can subscribe to manitoba-new-media (read-only).
# Publishing is gated behind the publisher token (17.4 Step 2).
location /ntfy/ {
    auth_basic off;

    proxy_pass http://127.0.0.1:${PORT}/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    # WebSockets for /ws and /sse subscriptions
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 1d;
    proxy_send_timeout 1d;
    proxy_buffering off;
}
EOF
sshm '/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t && systemctl --user reload nginx'
```

**Same-origin Web Push:** since `/ntfy/` and `/alerts/` are now on the SAME origin, ntfy's native web app handles the entire Push subscription flow — service worker scoped to `/ntfy/`, no cross-origin scope issues. The `/alerts/` button just navigates to `/ntfy/manitoba-new-media` and the boomer never leaves the host.

- [ ] **Step 2: Mint a publish token (operator)**

```bash
# Create a user 'publisher' with write-access to the topic, generate a token for *arr scripts.
sshm '~/.apps/ntfy/bin/ntfy user --config ~/.apps/ntfy/etc/server.yml add publisher'
# Prompted for password; pick a strong one and save in secrets/ntfy.publisher_password
sshm '~/.apps/ntfy/bin/ntfy access --config ~/.apps/ntfy/etc/server.yml publisher manitoba-new-media write-only'
sshm '~/.apps/ntfy/bin/ntfy token --config ~/.apps/ntfy/etc/server.yml add publisher' > /tmp/ntfy_token
TOKEN=$(grep -oE 'tk_[A-Za-z0-9]+' /tmp/ntfy_token)
secret_write ntfy.publisher_token "$TOKEN"
```

The token is what *arr / Tautulli / Notifiarr Custom Script connects use as `Authorization: Bearer tk_…`.

### Task 17.5: Verify subscribe flow end-to-end

- [ ] **Step 1: Public landing test**

```bash
curl -sf "https://quadstronaut.seedbox.example.com/ntfy/v1/health" | jq .
# Expect: {"healthy":true}
```

- [ ] **Step 2: Publish + receive smoke test**

```bash
TOKEN=$(secret_read ntfy.publisher_token)
TOPIC=$(secret_read ntfy.topic)
curl -sf -H "Authorization: Bearer $TOKEN" \
     -d "Production smoke test — please ignore" \
     "https://quadstronaut.seedbox.example.com/ntfy/${TOPIC}"
# Then in a second terminal, before the curl:
#   curl -sf "https://quadstronaut.seedbox.example.com/ntfy/${TOPIC}/sse"
# Should receive the message via SSE.
```

- [ ] **Step 3: Browser test**

Open `https://quadstronaut.seedbox.example.com/ntfy/manitoba-new-media` in Chrome/Firefox/Safari 16.4+. Click **Subscribe**. Browser prompts for notification permission. Approve. Then re-run Step 2. Notification should land on the desktop with browser closed.

---

## Phase 18 — `/alerts` onboarding page — **SKIPPED 2026-05-08**

**Reason:** Existed solely as the entry point to ntfy subscribe (Phase 17). With ntfy dropped, the page would just be a static FAQ duplicating what the Homarr widget + Newsletterr emails already convey. Not worth the maintenance.

The original Phase 18 spec is preserved below for archive only. Do not execute.

---



### Task 18.1: Static HTML page (small intro + big button)

**Files:**
- Create: `scripts/configure/41-alerts-page.sh`

Page lives at `https://quadstronaut.seedbox.example.com/alerts/` (the existing public-host nginx). Auth-basic OFF so a Listmonk welcome email's "click here" link works without prompting. Page has 4 visible elements: explanatory sentence, big button, QR code, FAQ.

- [ ] **Step 1: Write installer + page**

```bash
cat > scripts/configure/41-alerts-page.sh <<'INSTALLER'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"

# Page content — single self-contained HTML, no external deps except a CDN QR generator.
sshm 'mkdir -p ~/www/alerts'
sshm "cat > ~/www/alerts/index.html" <<'HTML'
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Manitoba Media — Get notified</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 560px; margin: 4rem auto; padding: 0 1.5rem; line-height: 1.6;
         color: #222; background: #fafafa; }
  h1 { font-size: 1.6rem; margin-bottom: 0.5rem; }
  p.lede { font-size: 1.15rem; color: #444; }
  a.bigbtn { display: block; text-align: center; background: #fa5252; color: #fff;
             padding: 1.4rem 1rem; border-radius: 12px; text-decoration: none;
             font-weight: 700; font-size: 1.25rem; margin: 2rem 0; box-shadow: 0 2px 6px rgba(0,0,0,0.12); }
  a.bigbtn:hover { background: #e63946; }
  .qr { display: block; margin: 1.5rem auto; max-width: 220px; }
  .qr-cap { text-align: center; color: #666; font-size: 0.95rem; }
  details { margin-top: 2rem; background: #fff; padding: 1rem 1.2rem; border-radius: 8px;
            border: 1px solid #eee; }
  details summary { cursor: pointer; font-weight: 600; }
  details p { margin-top: 0.6rem; }
  hr { margin: 2.5rem 0; border: none; border-top: 1px solid #ddd; }
  footer { color: #888; font-size: 0.9rem; text-align: center; }
</style>
</head>
<body>
  <h1>Get a ping when new movies and shows are added</h1>
  <p class="lede">Tap the red button. Your browser will ask if it's OK to send you alerts. Say yes. That's it.</p>

  <a class="bigbtn" href="https://quadstronaut.seedbox.example.com/ntfy/manitoba-new-media">
    Turn on alerts &nbsp;&rarr;
  </a>

  <p class="qr-cap">On your phone? Point your camera at this code:</p>
  <img class="qr"
       alt="QR code to subscribe on phone"
       src="https://api.qrserver.com/v1/create-qr-code/?size=220x220&amp;data=https%3A%2F%2Fquadstronaut.seedbox.example.com%2Fntfy%2Fmanitoba-new-media">

  <hr>

  <details>
    <summary>What happens after I tap the button?</summary>
    <p>You'll land on a page with one big <b>Subscribe</b> button. Tap it. Your browser will pop up a window asking if you want to allow notifications — tap <b>Allow</b>. From then on, you'll get a small popup whenever something new is added to the library, even if your browser is closed.</p>
    <p>If you don't want them anymore, come back to that same page and tap Unsubscribe. No spam, no email needed.</p>
  </details>

  <details>
    <summary>I'd rather just get an email.</summary>
    <p>That's already on. You'll see a weekly "Here's what's new this week" email from Manitoba Media. If you stop wanting them, every email has an Unsubscribe link at the bottom.</p>
  </details>

  <details>
    <summary>I'm on Discord — do I need this?</summary>
    <p>Nope. Discord users already get pings in <code>#notifiarr</code>. This is only if you'd like alerts on a phone or computer that doesn't run Discord.</p>
  </details>

  <hr>
  <footer>Manitoba Media — Powered by ntfy.sh</footer>
</body>
</html>
HTML

# Add nginx location stanza that disables auth-basic for /alerts/
sshm "cat > ~/.apps/nginx/proxy.d/alerts.conf" <<'NGX'
location /alerts/ {
    auth_basic off;
    alias /home/quadstronaut/www/alerts/;
    try_files $uri $uri/ /alerts/index.html;
}
location = /alerts {
    return 302 /alerts/;
}
NGX

sshm '/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t && systemctl --user reload nginx'
INSTALLER
chmod +x scripts/configure/41-alerts-page.sh
```

- [ ] **Step 2: Verify page loads without auth**

```bash
curl -sIk "https://quadstronaut.seedbox.example.com/alerts/" | head -3
# Expect HTTP/1.1 200 OK (NOT 401)
```

---

## Phase 19 — Listmonk install

### Task 19.1: Create database + role

**Files:**
- Create: `scripts/configure/43-listmonk-db.sh`

- [ ] **Step 1: Create dedicated DB**

```bash
cat > scripts/configure/43-listmonk-db.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"
source "$HERE/../lib/secrets.sh"

# Decode postgres pass once.
PG_PASS=$(sshm 'base64 -d ~/.apps/postgres/.encoded.dat | head -c 24')
[ -n "$PG_PASS" ] || die "couldn't decode postgres pass"

# Create the listmonk DB if it doesn't exist (idempotent).
sshm "PGPASSWORD='${PG_PASS}' psql -h 127.0.0.1 -p 42009 -U quadstronaut -d postgres -tc \"SELECT 1 FROM pg_database WHERE datname = 'listmonk'\" | grep -q 1 \
  || PGPASSWORD='${PG_PASS}' psql -h 127.0.0.1 -p 42009 -U quadstronaut -d postgres -c \"CREATE DATABASE listmonk OWNER quadstronaut\""

log_info "listmonk DB ready"
EOF
chmod +x scripts/configure/43-listmonk-db.sh
```

DB owner is `quadstronaut` (the same user Listmonk's process runs as). No new role needed; Postgres trust within own user is fine here.

### Task 19.2: Download binary + write config

**Files:**
- Create: `scripts/configure/44-listmonk-install.sh`

- [ ] **Step 1: Allocate port + secrets (reuse shared admin password)**

```bash
if ! secret_exists listmonk.port; then
  # Skip the port already taken by ntfy (next free might still be that one).
  PORT=$(sshm "app-ports free 2>/dev/null | grep -E '^[0-9]+$' | grep -v '^$(secret_read ntfy.port 2>/dev/null)$' | head -1")
  [ -n "$PORT" ] || die "no free port from app-ports"
  secret_write listmonk.port "$PORT"
fi

# Reuse the shared admin password for ALL admin-facing apps on this domain.
# Browser pw-managers dedupe poorly across multiple apps under quadstronaut.seedbox.example.com.
[ -f secrets/htpasswd.password ] || die "missing secrets/htpasswd.password"
secret_exists listmonk.admin_user || secret_write listmonk.admin_user "quadstronaut"
# listmonk.admin_password is intentionally NOT a separate secret — read htpasswd.password at runtime.
```

- [ ] **Step 2: Download + extract**

```bash
LISTMONK_VER="6.1.0"
LISTMONK_URL="https://github.com/knadh/listmonk/releases/download/v${LISTMONK_VER}/listmonk_${LISTMONK_VER}_linux_amd64.tar.gz"

sshm "bash -s" <<EOF
set -euo pipefail
mkdir -p ~/.apps/listmonk/{bin,etc,logs,uploads}
if [ ! -x ~/.apps/listmonk/bin/listmonk ] || ! ~/.apps/listmonk/bin/listmonk --version 2>&1 | grep -q "${LISTMONK_VER}"; then
  cd /tmp
  curl -fsSL "${LISTMONK_URL}" -o listmonk.tgz
  tar -xzf listmonk.tgz
  mv listmonk ~/.apps/listmonk/bin/listmonk
  rm listmonk.tgz config.toml.sample 2>/dev/null || true
fi
~/.apps/listmonk/bin/listmonk --version
EOF
```

- [ ] **Step 3: Generate config.toml**

```bash
PG_PASS=$(sshm 'base64 -d ~/.apps/postgres/.encoded.dat | head -c 24')
PORT=$(secret_read listmonk.port)

ADMIN_USER=$(secret_read listmonk.admin_user)
ADMIN_PASS=$(secret_read htpasswd.password)
sshm "cat > ~/.apps/listmonk/etc/config.toml" <<TOML
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
```

### Task 19.3: Run `--install` to create schema

- [ ] **Step 1: One-shot DB install**

```bash
sshm "cd ~/.apps/listmonk && \
  LISTMONK_ADMIN_USER='$(secret_read listmonk.admin_user)' \
  LISTMONK_ADMIN_PASSWORD='$(secret_read htpasswd.password)' \
  ./bin/listmonk --config etc/config.toml --install --idempotent --yes"
```

`--idempotent` skips already-installed schemas. `--yes` accepts data-loss warnings (none on first install).

### Task 19.4: User-systemd service + heartbeat

- [ ] **Step 1: Service unit**

```bash
sshm "cat > ~/.config/systemd/user/listmonk.service" <<'UNIT'
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
sshm 'systemctl --user daemon-reload && systemctl --user enable --now listmonk.service'
sleep 3
sshm 'systemctl --user is-active listmonk.service' | grep -q active || die "listmonk not active"
```

- [ ] **Step 2: Heartbeat cron** (mirrors `heartbeat-ntfy.sh`)

```bash
cat > scripts/ops/heartbeat-listmonk.sh <<'EOF'
#!/usr/bin/env bash
PORT=$(grep -oP 'address = "127.0.0.1:\K[0-9]+' ~/.apps/listmonk/etc/config.toml)
curl -sfm 5 "http://127.0.0.1:${PORT}/health" >/dev/null && exit 0
systemctl --user is-active listmonk.service >/dev/null && exit 0
logger -t listmonk-heartbeat "listmonk unhealthy — restarting"
systemctl --user restart listmonk.service
EOF
scpm_to scripts/ops/heartbeat-listmonk.sh '~/scripts/ops/heartbeat-listmonk.sh'
sshm 'chmod +x ~/scripts/ops/heartbeat-listmonk.sh && (crontab -l 2>/dev/null | grep -v heartbeat-listmonk; echo "*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-listmonk.sh") | crontab -'
```

### Task 19.5: nginx path-based fragment

- [ ] **Step 1: Drop the proxy.d fragment**

```bash
PORT=$(secret_read listmonk.port)
sshm "cat > ~/.apps/nginx/proxy.d/listmonk.conf" <<EOF
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
}
EOF
sshm '/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t && systemctl --user reload nginx'
```

- [ ] **Step 2: Set Listmonk's public root URL** — Admin UI → Settings → General → "Root URL" = `https://quadstronaut.seedbox.example.com/listmonk` (so generated unsubscribe links use the correct base). Save.

- [ ] **Step 3: Verify**

```bash
curl -sIk "https://quadstronaut.seedbox.example.com/listmonk/" | head -3
# Expect HTTP 200 or 302 (redirect to login)
curl -sk -u "$(secret_read listmonk.admin_user):$(secret_read htpasswd.password)" \
     "https://quadstronaut.seedbox.example.com/listmonk/api/health" | jq .
```

### Task 19.6: SMTP backend (operator)

**Files:**
- Modify: `docs/operator-deferred.md` — Phase 17 entry

Listmonk sends via SMTP — no built-in mail server. Options, in order of recommended:

1. **Gmail SMTP with App Password** — operator already has the Google account `operator@example.com`. Generate App Password at `myaccount.google.com/apppasswords` (requires 2FA). Limit ~500/day. Sender displays as "Manitoba Media \<operator@example.com>". 5-min setup. **Pick this for v1.**
2. **Mailgun** — free 100/day, then paid. Better deliverability for cold-list bulk sends.
3. **SendGrid** — similar pricing/reputation as Mailgun.

Configure via Listmonk admin UI → Settings → SMTP. Save the App Password to `secrets/listmonk.smtp_password` (gitignored).

---

## Phase 20 — Subscriber sync

### Task 20.1: Bootstrap from Ombi (one-shot)

**Files:**
- Create: `scripts/configure/45-listmonk-bootstrap.py`

The Ombi cohort is the canonical seed. 13 user-email pairs were verified
2026-05-08 — the actual list is captured locally in `secrets/ombi-cohort.tsv`
(gitignored, never committed) and seeded into Listmonk via the bootstrap
script. Format is `<ombi_username>\t<email>`, one per line.

- [ ] **Step 1: Bootstrap script — creates list "All Members" + tag-segments, seeds Ombi users**

```python
#!/usr/bin/env python3
"""One-shot Listmonk bootstrap. Idempotent: re-running won't dupe lists or subscribers."""
import base64, json, os, ssl, subprocess, sys, sqlite3
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_DIR = os.path.join(SCRIPT_DIR, "..", "..", "secrets")
def s(name): return open(os.path.join(SECRETS_DIR, name)).read().strip()

LM_HOST = "https://quadstronaut.seedbox.example.com/listmonk"
LM_AUTH = (s("listmonk.admin_user"), s("htpasswd.password"))  # shared admin pw across this domain

def lm_req(method, path, body=None):
    url = f"{LM_HOST}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, method=method, data=data, headers={"Content-Type":"application/json"})
    creds = base64.b64encode(f"{LM_AUTH[0]}:{LM_AUTH[1]}".encode()).decode()  # admin from htpasswd.password
    req.add_header("Authorization", f"Basic {creds}")
    with urllib.request.urlopen(req, context=ssl.create_default_context()) as r:
        return json.loads(r.read())

def get_or_create_list(name, tags):
    lists = lm_req("GET", "/api/lists?per_page=all")["data"]["results"]
    for L in lists:
        if L["name"] == name: return L["id"]
    return lm_req("POST", "/api/lists", {"name": name, "type": "private", "optin": "single",
                                          "tags": tags, "description": f"Auto-created: {name}"})["data"]["id"]

def upsert_subscriber(email, name, list_ids, attribs=None):
    # Upsert by email. If exists, update; if not, create with preconfirm so they stay subscribed.
    body = {"email": email, "name": name, "status": "enabled",
            "lists": list_ids, "preconfirm_subscriptions": True,
            "attribs": attribs or {}}
    try:
        return lm_req("POST", "/api/subscribers", body)
    except urllib.error.HTTPError as e:
        if e.code == 409:  # exists — update
            existing = lm_req("GET", f"/api/subscribers?query=subscribers.email='{email}'")["data"]["results"]
            if existing:
                sid = existing[0]["id"]
                return lm_req("PUT", f"/api/subscribers/{sid}", body)
        raise

def main():
    main_list = get_or_create_list("All Members", ["all"])
    src_ombi = get_or_create_list("Ombi imports (legacy)", ["ombi", "legacy"])
    src_plex = get_or_create_list("Plex friends", ["plex"])
    src_jsr  = get_or_create_list("Jellyseerr requesters", ["jellyseerr"])

    # Pull Ombi via SSH-side sqlite dump piped over stdout.
    out = subprocess.check_output([
        "ssh","-o","BatchMode=yes","quadstronaut@seedbox.example.com",
        "sqlite3 -separator '|' ~/.apps/ombi/Ombi.db "
        "\"SELECT UserName, Email FROM AspNetUsers "
        "WHERE Email IS NOT NULL AND Email != '' AND UserName != 'Api'\""
    ]).decode().strip()
    seeded = 0
    for line in out.splitlines():
        username, email = line.split("|", 1)
        upsert_subscriber(email, username, [main_list, src_ombi],
                          attribs={"source": "ombi", "ombi_username": username})
        seeded += 1
    print(f"Bootstrapped {seeded} Ombi subscribers into 'All Members' + 'Ombi imports'")

if __name__ == "__main__": main()
```

- [ ] **Step 2: Run bootstrap once**

```bash
python3 scripts/configure/45-listmonk-bootstrap.py
# Expect: "Bootstrapped 13 Ombi subscribers ..."
```

### Task 20.2: Nightly reconcile cron

**Files:**
- Create: `scripts/ops/listmonk-sync-subscribers.py` (lives on seedbox)

Reconciles Plex friends + Jellyseerr users + Jellyfin users into the appropriate source lists. Does NOT remove subscribers (subscribers self-unsubscribe via Listmonk). Idempotent.

- [ ] **Step 1: Write sync script (see Task 20.1's script structure; queries against Plex/Jellyseerr/Jellyfin instead of Ombi)**

```python
# Pseudocode shape:
# - Pull Plex.tv friends → upsert into "Plex friends" list (skip empty-email home accounts)
# - Pull Jellyseerr /api/v1/user → upsert into "Jellyseerr requesters" list (where email exists)
# - Pull Jellyfin /Users → upsert into "Jellyfin users" list (where email exists)
# - All subscribers also get added to "All Members" list
# - Log delta to ~/.apps/listmonk/logs/sync.log
```

- [ ] **Step 2: Cron**

```bash
sshm "(crontab -l | grep -v listmonk-sync; echo '0 4 * * * /usr/bin/python3 /home/quadstronaut/scripts/ops/listmonk-sync-subscribers.py >> /home/quadstronaut/.apps/listmonk/logs/sync.log 2>&1') | crontab -"
```

Runs nightly at 04:00 (after the existing prune cron). Daily delta is small (handful of users at most).

---

## Phase 21 — Homarr Recently Added widget + Get-notified tile

### Task 21.1: Extend the existing seed script

**Files:**
- Modify: `scripts/configure/35-homarr-seed-boards.py`
- Or create: `scripts/configure/46-homarr-add-comms.py` (preferred — keeps the original seed pristine and re-runnable)

- [ ] **Step 1: New "Get notified" tile**

Add to `PUBLIC_APPS` array:
```python
("Get notified", "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/ntfy.svg",
 "https://quadstronaut.seedbox.example.com/alerts/"),
```

Place it in the top row of the public board (adjust grid offsets so it sits next to Plex and Jellyfin).

- [ ] **Step 2: Recently Added widget**

Homarr has a built-in `mediaServer` integration widget. The pattern:

```python
# In the board's section, add a widget item (kind='widget') with:
options = {"json": {"kind": "mediaServer-recentlyAdded", "appId": <plex-app-id>, "limit": 12}}
```

Insert via SQL similar to the existing `add_app_tiles`. Span 6 columns × 4 rows in the top of the public board (push existing tiles down).

- [ ] **Step 3: Run + verify**

```bash
ssh quadstronaut@seedbox.example.com 'python3 ~/scripts/configure/46-homarr-add-comms.py'
curl -skL -u "quadstronaut:$(cat secrets/htpasswd.password)" "https://quadstronaut.seedbox.example.com/" | grep -ci 'recently'
```

---

## Phase 22 — Conjurr install (AI rec engine)

[Conjurr](https://github.com/yungsnuzzy/conjurr) v4.1.0 (Python 3.11+, Flask, port 2665, MIT-style, ~95 stars). Pulls Tautulli watch history, calls Google Gemini for personalized recommendations, checks Overseerr/Jellyseerr for availability. Newsletterr depends on this for the AI-recommendations section of the digest.

### Task 22.1: Operator prerequisite — Google Gemini API key

- [ ] **Step 1: Create key at https://aistudio.google.com/app/apikey**
  - Free tier: `gemini-1.5-flash` 1500 req/day, more than enough for 13 subscribers × weekly digest = ~13 req/week.
  - Save to `secrets/gemini.api_key` (gitignored).

### Task 22.2: Clone + venv + install

**Files:**
- Create: `scripts/configure/47-conjurr-install.sh`

- [ ] **Step 1: Allocate port (default 2665 if free)**

```bash
if ! secret_exists conjurr.port; then
  # Claim from app-ports free, skipping ports already taken by ntfy/listmonk.
  USED=$(printf '%s\n' "$(secret_read ntfy.port 2>/dev/null)" "$(secret_read listmonk.port 2>/dev/null)" | sort -u)
  PORT=$(sshm "app-ports free 2>/dev/null | grep -E '^[0-9]+$'" | grep -vxF "$USED" | head -1)
  [ -n "$PORT" ] || die "no free port from app-ports"
  secret_write conjurr.port "$PORT"
fi
```

- [ ] **Step 2: Clone + venv**

```bash
sshm 'bash -s' <<'EOF'
set -euo pipefail
mkdir -p ~/.apps/conjurr
cd ~/.apps/conjurr
[ -d repo ] || git clone --depth 1 https://github.com/yungsnuzzy/conjurr.git repo
cd repo
git pull --ff-only
python3.11 -m venv .venv 2>/dev/null || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
EOF
```

If `python3.11` isn't available on the seedbox, the install will fall back to `python3` (Conjurr lists 3.11+ as required — verify Python version during install and bail with a clear error if older).

- [ ] **Step 3: Write `.env`**

```bash
TAUTULLI_KEY=$(secret_read tautulli.key)
TAUTULLI_PORT=$(secret_read tautulli.port)
JSR_KEY=$(secret_read jellyseerr.key)
JSR_PORT=$(secret_read jellyseerr.port)
GEMINI_KEY=$(secret_read gemini.api_key)
PORT=$(secret_read conjurr.port)

sshm "cat > ~/.apps/conjurr/repo/.env" <<EOF
TAUTULLI_URL=http://127.0.0.1:${TAUTULLI_PORT}/tautulli
TAUTULLI_API_KEY=${TAUTULLI_KEY}
GOOGLE_API_KEY=${GEMINI_KEY}
USER_MODE=1
OVERSEERR_URL=http://127.0.0.1:${JSR_PORT}
OVERSEERR_API_KEY=${JSR_KEY}
EOF
chmod 600 ~/.apps/conjurr/repo/.env
```

(Conjurr treats Jellyseerr as an Overseerr-compatible backend — same API shape.)

### Task 22.3: User-systemd service + heartbeat

- [ ] **Step 1: Service unit**

```bash
sshm "cat > ~/.config/systemd/user/conjurr.service" <<'UNIT'
[Unit]
Description=Conjurr AI recommendation engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.apps/conjurr/repo
EnvironmentFile=%h/.apps/conjurr/repo/.env
ExecStart=%h/.apps/conjurr/repo/.venv/bin/python app.py
Restart=on-failure
RestartSec=10s
StandardOutput=append:%h/.apps/conjurr/conjurr.log
StandardError=append:%h/.apps/conjurr/conjurr.err

[Install]
WantedBy=default.target
UNIT
sshm 'mkdir -p ~/.apps/conjurr/logs && systemctl --user daemon-reload && systemctl --user enable --now conjurr.service'
sleep 5
sshm 'systemctl --user is-active conjurr.service' | grep -q active || die "conjurr not active"
```

- [ ] **Step 2: Heartbeat cron**

```bash
cat > scripts/ops/heartbeat-conjurr.sh <<'EOF'
#!/usr/bin/env bash
PORT=$(grep -oP 'PORT=\K[0-9]+' ~/.apps/conjurr/repo/.env 2>/dev/null || echo 2665)
curl -sfm 5 "http://127.0.0.1:${PORT}/" >/dev/null && exit 0
systemctl --user is-active conjurr.service >/dev/null && exit 0
logger -t conjurr-heartbeat "conjurr unhealthy — restarting"
systemctl --user restart conjurr.service
EOF
scpm_to scripts/ops/heartbeat-conjurr.sh '~/scripts/ops/heartbeat-conjurr.sh'
sshm 'chmod +x ~/scripts/ops/heartbeat-conjurr.sh && (crontab -l | grep -v heartbeat-conjurr; echo "*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-conjurr.sh") | crontab -'
```

### Task 22.4: nginx path-based fragment (htpasswd-protected — admin-only)

- [ ] **Step 1: Drop the proxy.d fragment**

Conjurr ships without built-in auth — the parent server block's `auth_basic` (htpasswd) protects it.

```bash
PORT=$(secret_read conjurr.port)
sshm "cat > ~/.apps/nginx/proxy.d/conjurr.conf" <<EOF
location /conjurr/ {
    # Inherit auth_basic from parent server block (htpasswd protects admin-only UI).
    proxy_pass http://127.0.0.1:${PORT}/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-Prefix /conjurr;
    proxy_set_header X-Script-Name /conjurr;
}
EOF
sshm '/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t && systemctl --user reload nginx'
```

- [ ] **Step 2: Add `SCRIPT_NAME` to Conjurr's environment**

Flask uses `SCRIPT_NAME` to know its base path for URL generation. Append to `~/.apps/conjurr/repo/.env`:

```
SCRIPT_NAME=/conjurr
```

Then `systemctl --user restart conjurr.service`.

### Task 22.5: Verify

- [ ] **Step 1: API health**

```bash
curl -sfk "https://quadstronaut.seedbox.example.com/conjurr/health" | head
```

- [ ] **Step 2: Sample recommendation**

```bash
# Conjurr exposes POST /recommendations — pull recs for one user.
curl -sfk -X POST "https://quadstronaut.seedbox.example.com/conjurr/recommendations" \
     -H "Content-Type: application/json" \
     -d '{"username":"quadstronaut","limit":5}'
```

Expect a JSON list of 5 movies/shows pulled from Gemini, with TMDb IDs and Overseerr availability flags. If empty → check Tautulli watch history exists for that user.

---

## Phase 23 — Newsletterr install (auto weekly digest with AI recs)

[Newsletterr](https://github.com/jma1ice/newsletterr) v2026.1 (Python 3.9+, Flask, port 6397, MIT, ~91 stars). Drag-and-drop newsletter builder pulling from Tautulli + Conjurr, sends via SMTP on a schedule. Replaces Ombi's auto-newsletter feature.

### Task 23.1: Allocate port + clone

**Files:**
- Create: `scripts/configure/48-newsletterr-install.sh`

- [ ] **Step 1: Port + clone**

```bash
if ! secret_exists newsletterr.port; then
  USED=$(printf '%s\n' "$(secret_read ntfy.port 2>/dev/null)" "$(secret_read listmonk.port 2>/dev/null)" "$(secret_read conjurr.port 2>/dev/null)" | sort -u)
  PORT=$(sshm "app-ports free 2>/dev/null | grep -E '^[0-9]+$'" | grep -vxF "$USED" | head -1)
  [ -n "$PORT" ] || die "no free port from app-ports"
  secret_write newsletterr.port "$PORT"
fi

sshm 'bash -s' <<'EOF'
set -euo pipefail
mkdir -p ~/.apps/newsletterr
cd ~/.apps/newsletterr
[ -d repo ] || git clone --depth 1 https://github.com/jma1ice/newsletterr.git repo
cd repo
git pull --ff-only
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
# Newsletterr renders charts via Playwright headless chromium.
.venv/bin/python -m playwright install chromium
EOF
```

The Playwright chromium download is ~150 MB. One-time. Verify with `du -sh ~/.cache/ms-playwright`.

### Task 23.2: User-systemd service + heartbeat

- [ ] **Step 1: Service unit**

```bash
PORT=$(secret_read newsletterr.port)
sshm "cat > ~/.config/systemd/user/newsletterr.service" <<UNIT
[Unit]
Description=Newsletterr — Plex auto-newsletter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.apps/newsletterr/repo
Environment=PORT=${PORT}
Environment=PUBLIC_BASE_URL=https://quadstronaut.seedbox.example.com/newsletterr
Environment=SCRIPT_NAME=/newsletterr
ExecStart=%h/.apps/newsletterr/repo/.venv/bin/python newsletterr.py
Restart=on-failure
RestartSec=10s
StandardOutput=append:%h/.apps/newsletterr/newsletterr.log
StandardError=append:%h/.apps/newsletterr/newsletterr.err

[Install]
WantedBy=default.target
UNIT
sshm 'systemctl --user daemon-reload && systemctl --user enable --now newsletterr.service'
sleep 5
sshm 'systemctl --user is-active newsletterr.service' | grep -q active || die "newsletterr not active"
```

- [ ] **Step 2: Heartbeat cron** (mirror conjurr pattern; check `http://127.0.0.1:$PORT/` returns 200)

### Task 23.3: nginx path-based fragment + initial config (operator)

- [ ] **Step 1: Drop the proxy.d fragment**

```bash
PORT=$(secret_read newsletterr.port)
sshm "cat > ~/.apps/nginx/proxy.d/newsletterr.conf" <<EOF
location /newsletterr/ {
    # Inherit htpasswd from parent server block (admin-only UI).
    proxy_pass http://127.0.0.1:${PORT}/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-Prefix /newsletterr;
    proxy_set_header X-Script-Name /newsletterr;
    # Newsletterr serves Highcharts via Playwright — chunked responses can be large
    proxy_buffering off;
    proxy_read_timeout 120s;
}
EOF
sshm '/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t && systemctl --user reload nginx'
```

- [ ] **Step 2: Operator: open the UI at `https://quadstronaut.seedbox.example.com/newsletterr/`. Settings → fill in:**
  - Tautulli URL: `http://127.0.0.1:<tautulli.port>/tautulli` + key from `secrets/tautulli.key`
  - Conjurr URL: `http://127.0.0.1:<conjurr.port>` (so it can pull AI recs)
  - SMTP: same Gmail App Password used for Listmonk (`secrets/listmonk.smtp_password`)
  - From: `Manitoba Media <operator@example.com>`
- [ ] Operator: Templates → New → drop in a "Recently Added" snap-in + a "Personalized recommendations" snap-in (Conjurr-powered). Save.
- [ ] Operator: Schedule → weekly Sunday 09:00.

(Newsletterr's recipient table location: `~/.apps/newsletterr/repo/database/data.db` — referenced by the Phase 24 bridge sync.)

### Task 23.4: Verify auto-send

- [ ] Click "Send Now" on the test template. Verify a real email lands in `operator@example.com`. Verify the AI-rec section is populated.

---

## Phase 24 — Listmonk → Newsletterr recipient bridge

Newsletterr keeps its own recipient list in `~/.apps/newsletterr/repo/database/data.db` (SQLite). To preserve the "subscriber-sync is fully automated" property of Ombi, we need a daily reconcile that copies Listmonk's "All Members" subscribers into Newsletterr.

### Task 24.1: Schema discovery (one-shot probe)

- [ ] **Step 1: Inspect Newsletterr's recipient table on first install**

```bash
sshm 'sqlite3 ~/.apps/newsletterr/repo/database/data.db ".tables"'
sshm 'sqlite3 ~/.apps/newsletterr/repo/database/data.db ".schema users"'  # or whatever the table is named
```

Capture the schema in `docs/external/newsletterr-schema.md` so the sync script breaks loudly if Newsletterr changes its schema in a future version (forking trigger).

### Task 24.2: Bridge script

**Files:**
- Create: `scripts/ops/listmonk-to-newsletterr-sync.py` (lives on seedbox)

```python
#!/usr/bin/env python3
"""Daily: pull Listmonk 'All Members' subscribers, upsert into Newsletterr's SQLite.

Idempotent. Logs deltas. Exits non-zero if Newsletterr's schema diverges from the captured one.
"""
import base64, json, os, sqlite3, sys, urllib.request, ssl
from pathlib import Path

LM_HOST = "https://quadstronaut.seedbox.example.com/listmonk"
NL_DB = Path.home() / ".apps/newsletterr/repo/database/data.db"
SECRETS = Path.home() / "scripts/Optimize-Manitoba/secrets"  # adjust to wherever the repo is checked out
def s(name): return (SECRETS / name).read_text().strip()

def lm_get_members():
    creds = base64.b64encode(f"{s('listmonk.admin_user')}:{s('htpasswd.password')}".encode()).decode()
    req = urllib.request.Request(f"{LM_HOST}/api/subscribers?per_page=all&list_id=1",
                                  headers={"Authorization": f"Basic {creds}"})
    with urllib.request.urlopen(req, context=ssl.create_default_context()) as r:
        d = json.load(r)
    return [(x["email"], x["name"]) for x in d["data"]["results"] if x["status"] == "enabled"]

def main():
    members = lm_get_members()
    con = sqlite3.connect(str(NL_DB))
    cur = con.cursor()
    # SCHEMA-CRITICAL — fail loud if Newsletterr renames the table or columns.
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    if not cur.fetchone():
        print("FATAL: Newsletterr schema changed — table 'users' not found. Re-run schema discovery.", file=sys.stderr)
        sys.exit(2)
    added = updated = 0
    for email, name in members:
        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        if cur.fetchone():
            cur.execute("UPDATE users SET name = ? WHERE email = ?", (name, email))
            updated += 1
        else:
            cur.execute("INSERT INTO users (email, name, created_at) VALUES (?, ?, datetime('now'))", (email, name))
            added += 1
    con.commit()
    con.close()
    print(f"sync: {added} added, {updated} updated, {len(members)} total in Listmonk")

if __name__ == "__main__": main()
```

### Task 24.3: Daily cron (after Listmonk sync)

```bash
sshm "(crontab -l | grep -v listmonk-to-newsletterr; echo '15 4 * * * /usr/bin/python3 /home/quadstronaut/scripts/ops/listmonk-to-newsletterr-sync.py >> /home/quadstronaut/.apps/newsletterr/sync.log 2>&1') | crontab -"
```

04:15 — runs after the 04:00 Listmonk subscriber sync (Phase 20.2), so any new Plex/Jellyseerr users that appeared overnight propagate all the way to Newsletterr by 04:16.

### Task 24.4: Unsubscribe consistency (operator)

When a user clicks Listmonk's unsubscribe link, they're removed from Listmonk's "All Members" list. The next 04:15 sync will see them gone — but the bridge script ADDS missing users; it doesn't REMOVE departed ones.

- [ ] **Add a removal pass**: after upserts, SELECT all newsletterr emails NOT in Listmonk's enabled list AND mark them disabled (don't delete — keep history). Pseudocode in the script comments.

This way, "unsubscribe via Listmonk's link in any email" cleanly stops both Listmonk broadcasts AND Newsletterr's weekly digest. Single unsubscribe path, consistent UX.

---

## Phase 25 — Cutover migration

### Task 25.1: Configure SMTP in Listmonk admin UI (operator)

- [ ] **Step 1: Settings → SMTP**
  - Host: `smtp.gmail.com`
  - Port: `587`
  - Auth: `LOGIN`
  - Username: `operator@example.com`
  - Password: Gmail App Password
  - Hello hostname: `seedbox.example.com`
  - Test send to self before saving.

### Task 25.2: Build cutover email template

- [ ] **Step 1: Admin UI → Templates → New (visual editor)**

Subject: **Manitoba Media — small update**

Body (plain, no HTML chrome):

```
Hi {{ .Subscriber.FirstName }},

We're tidying up the way you hear about new movies, shows, and books on Manitoba.
Three small things:

1. The dashboard you already use will keep showing what's new at the top:
   https://quadstronaut.seedbox.example.com/

2. If you want a quick ping on your phone or computer when something is added
   (no app, no signup), tap here once:
   https://quadstronaut.seedbox.example.com/alerts/

3. And you'll keep getting this kind of email about once a week. Nothing changes
   there. There's an Unsubscribe link at the bottom of every one if you'd rather
   not.

That's it. No action needed unless you want the phone alerts. Thanks for reading.

— Manitoba Media
```

Footer (Listmonk auto-adds): "Unsubscribe: {{ UnsubscribeURL }}"

### Task 25.3: Send cutover email to All Members

- [ ] **Step 1: Admin UI → Campaigns → New**
  - Name: "Cutover 2026-05"
  - Subject: as above
  - Lists: `All Members`
  - Template: the one created in 25.2
  - **Schedule**: send immediately (small list — 13 recipients).

- [ ] **Step 2: Verify deliverability**
  - Check `operator@example.com` inbox first (admin is in the list).
  - Listmonk Dashboard → Campaign → click-through stats. Wait 24 hours.
  - Look for any soft/hard bounces; remove bounced addresses from the list.

### Task 25.4: Observe one full Newsletterr digest cycle

The weekly auto-digest is owned by Newsletterr (Phase 23) — Listmonk is for ad-hoc only. Wait for the first Sunday-09:00 auto-send and verify:

- [ ] **Step 1: Confirm send happened**
  - `~/.apps/newsletterr/newsletterr.log` shows "campaign sent" entry.
  - Operator's own inbox received the digest.
- [ ] **Step 2: Spot-check the AI rec section**
  - Confirm Conjurr-driven recs appear (otherwise Conjurr→Newsletterr wiring needs a look).
- [ ] **Step 3: Observe**
  - Soft/hard bounces in Newsletterr's send log.
  - If a recipient asks to be removed, click their unsubscribe in Listmonk → next 04:15 sync removes from Newsletterr too (per Phase 24.4 removal pass).
  - If <2 opens out of 13 over 2-3 weeks, the channel isn't pulling its weight — re-evaluate.

---

## Phase 26 — Ombi decommission

**Only after Phase 25 has run for at least one full digest cycle (~7 days) and observation shows email is landing.**

### Task 26.1: Stop Ombi

- [ ] **Step 1: Stop the daemon**

```bash
ssh quadstronaut@seedbox.example.com 'app-ombi stop'
```

- [ ] **Step 2: Remove Ombi from Homarr admin board** (it's not on the public board)

```bash
# Edit scripts/configure/35-homarr-seed-boards.py — remove Ombi from ADMIN_EXTRA_APPS, re-run.
```

- [ ] **Step 3: Don't `app-ombi uninstall` for 7+ days** — keep the DB around as a fallback in case we missed a subscriber.

- [ ] **Step 4: After 7+ days of stable operation, optionally**: `app-ombi uninstall`. Update `docs/operator-deferred.md` to mark Phase 26 complete.

---

## Smoke test additions

Add four new tests to `scripts/smoke-test.sh` (was six — `ntfy-health` and `alerts-page` dropped per Phase 17/18 skip 2026-05-08):

```bash
# (13. ntfy-health — REMOVED 2026-05-08 — ntfy not deployed)

# 14. Listmonk health + subscriber count
echo "14. Listmonk"
LM_USER=$(secret_read listmonk.admin_user)
LM_PASS=$(secret_read htpasswd.password)
LM_SUB_COUNT=$(curl -sfk -m 10 -u "$LM_USER:$LM_PASS" "https://quadstronaut.seedbox.example.com/listmonk/api/dashboard/counts" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["subscribers"]["total"])' 2>/dev/null)
if [ "${LM_SUB_COUNT:-0}" -ge 13 ]; then
  record "listmonk-subscribers" pass "$LM_SUB_COUNT subscribers"
else
  record "listmonk-subscribers" fail "expected ≥13, got $LM_SUB_COUNT"
fi

# (15. alerts-page — REMOVED 2026-05-08 — /alerts/ not deployed)

# 16. Conjurr health (htpasswd-protected, expect 200 with creds)
echo "16. Conjurr"
HTPW=$(secret_read htpasswd.password)
CJ_HTTP=$(curl -sIk -u "quadstronaut:$HTPW" -o /dev/null -w '%{http_code}' "https://quadstronaut.seedbox.example.com/conjurr/")
case "$CJ_HTTP" in 200|302) record "conjurr-up" pass "HTTP $CJ_HTTP" ;; *) record "conjurr-up" fail "HTTP $CJ_HTTP" ;; esac

# 17. Newsletterr health (htpasswd-protected) + recipient count >= Listmonk subscribers
echo "17. Newsletterr"
NL_HTTP=$(curl -sIk -u "quadstronaut:$HTPW" -o /dev/null -w '%{http_code}' "https://quadstronaut.seedbox.example.com/newsletterr/")
case "$NL_HTTP" in 200|302) record "newsletterr-up" pass "HTTP $NL_HTTP" ;; *) record "newsletterr-up" fail "HTTP $NL_HTTP" ;; esac

NL_COUNT=$(sshm "sqlite3 ~/.apps/newsletterr/repo/database/data.db 'SELECT COUNT(*) FROM users'" 2>/dev/null)
if [ "${NL_COUNT:-0}" -ge "${LM_SUB_COUNT:-1}" ]; then
  record "newsletterr-recipients" pass "$NL_COUNT recipients (Listmonk: $LM_SUB_COUNT)"
else
  record "newsletterr-recipients" fail "Newsletterr=$NL_COUNT < Listmonk=$LM_SUB_COUNT — bridge sync may be stale"
fi
```

After Phase 17-24 complete, smoke is 23/23 (existing 18 + 5 new).

---

## Rollback per phase

| Phase | If broken, rollback |
|---|---|
| 17 (ntfy) | `systemctl --user disable --now ntfy.service && rm -rf ~/.apps/ntfy ~/.config/systemd/user/ntfy.service ~/.apps/nginx/proxy.d/ntfy.conf && systemctl --user reload nginx`. Crontab strip the heartbeat. |
| 18 (alerts page) | `rm -rf ~/www/alerts ~/.apps/nginx/proxy.d/alerts.conf && systemctl --user reload nginx`. |
| 19 (Listmonk) | `systemctl --user disable --now listmonk.service && rm -rf ~/.apps/listmonk ~/.apps/nginx/proxy.d/listmonk.conf && systemctl --user reload nginx` + drop DB: `psql -c 'DROP DATABASE listmonk'`. Strip heartbeat from crontab. |
| 20 (Sync) | Subscribers in Listmonk are isolated — drop DB to fully reset. Or via UI: select all + delete. |
| 21 (Homarr) | Re-run `35-homarr-seed-boards.py` after editing back. SQLite has `db.sqlite.bak` if you want to atomically restore. |
| 22 (Conjurr) | `systemctl --user disable --now conjurr.service && rm -rf ~/.apps/conjurr ~/.config/systemd/user/conjurr.service ~/.apps/nginx/proxy.d/conjurr.conf && systemctl --user reload nginx`. Strip heartbeat. Gemini key remains in `secrets/`. |
| 23 (Newsletterr) | `systemctl --user disable --now newsletterr.service && rm -rf ~/.apps/newsletterr ~/.config/systemd/user/newsletterr.service ~/.apps/nginx/proxy.d/newsletterr.conf && systemctl --user reload nginx`. Playwright chromium cache: `rm -rf ~/.cache/ms-playwright`. Strip heartbeat. |
| 24 (Bridge sync) | `crontab -l \| grep -v listmonk-to-newsletterr \| crontab -` and `rm scripts/ops/listmonk-to-newsletterr-sync.py`. No data loss — Newsletterr's DB persists. |
| 25 (Cutover) | Cannot un-send an email. If the email is broken/wrong, send a follow-up correction. Don't decom Ombi until cutover delivered + first auto-digest cycle observed. |
| 26 (Ombi stop) | `app-ombi start` — Ombi is fine to come back online; nothing else depends on it being down. |

---

## Open decisions (operator)

These are NOT blockers for the install but require human input before Phase 25:

1. **SMTP backend** — recommend Gmail App Password for v1. Same App Password used by both Listmonk (cutover/ad-hoc) and Newsletterr (weekly digest). Confirm or pick Mailgun/SendGrid.
2. **Cutover email copy** — the body in Task 25.2 is a draft. Operator should review/edit voice.
3. **Newsletterr template aesthetics** — Phase 23.3 has operator drag-and-drop the snap-ins. Operator picks layout, color, frequency (default Sunday 09:00 weekly). Confirm or adjust.
4. **Google Gemini API key** — Phase 22.1 prerequisite. Free tier covers our load (1500 req/day vs ~13/wk needed). Operator creates the key.
5. **No SMS confirmed** — email-to-SMS gateways shut down 2024-2025; paid alternatives don't fit "free" constraint. Confirmed previously.
6. **Discord remains primary for power users** — already wired this session, no change.

---

## Backups + disaster recovery

- **Listmonk DB**: covered by existing `app-postgres dump` cron (if not already scheduled, add it). Output goes to `~/.apps/backup/postgres-<date>.sql` and includes the new `listmonk` DB automatically.
- **Listmonk attachments / uploads** (`~/.apps/listmonk/uploads/`): low-priority for v1 (we send digest emails, not heavy media attachments). Add to backup script when uploads start being used.
- **ntfy state** (`~/.apps/ntfy/cache/cache.db` + `~/.apps/ntfy/lib/{auth,webpush}.db`): subscriber Web Push subscriptions live in `webpush.db`. If lost, every subscriber must re-subscribe via the alerts page. Worth backing up nightly: `tar -czf ~/.apps/backup/ntfy-$(date +%F).tgz ~/.apps/ntfy/{cache,lib,etc}`.
- **VAPID keys**: stored in `secrets/ntfy.webpush_{public,private}` (gitignored). If these are lost, every Web Push subscription becomes invalid. Operator should keep a copy in a password manager.
- **Newsletterr SQLite** (`~/.apps/newsletterr/repo/database/data.db`): contains subscriber list + template definitions. Worth backing up nightly: `tar -czf ~/.apps/backup/newsletterr-$(date +%F).tgz ~/.apps/newsletterr/repo/database/`.
- **Conjurr SQLite** (lives inside `~/.apps/conjurr/repo/`): stores user-rec history. Lower priority — cache that can rebuild from Tautulli.
- **Gemini API key**: in `secrets/gemini.api_key`. Replaceable any time at aistudio.google.com.
- **Shared admin password**: stored in `secrets/htpasswd.password`, used by Listmonk + Conjurr + Newsletterr admin UIs + nginx htpasswd. Keep an offline copy — losing it means re-bootstrapping every admin login.

## Cost summary

- ntfy: $0 — fully self-hosted on existing seedbox.
- Listmonk: $0 — fully self-hosted on existing Postgres.
- Conjurr: $0 — Gemini free tier covers ~13 reqs/week (free quota is 1500/day for `gemini-1.5-flash`).
- Newsletterr: $0 — fully self-hosted, MIT license.
- SMTP: $0 if Gmail App Password (free tier ~500/day, well above 13 recipients × weekly).
- Operator effort to maintain: ~5 min/week (Newsletterr auto-sends; operator just spot-checks the inbox).
- One-time disk: ~330 MB (ntfy ~30 + Listmonk ~50 + Conjurr ~50 + Newsletterr ~50 + Playwright chromium ~150).

## What this plan does NOT do

- **No SMS.** Email-to-SMS carrier gateways shut down 2024-2025 (AT&T June 2025, T-Mobile December 2024, Verizon phasing out by March 2027). Free SMS APIs cap at ~1/day on free tier. Paid 10DLC alternatives violate the "$0" constraint.
- **No Web Push from a custom origin.** ntfy's native subscribe page is the entry point — same-origin Web Push, zero custom service worker code.
- **No replacement for Ombi's request UI.** Jellyseerr already covers that; that's a Phase-13 deliverable.
- **No Plex friend email scrape via local /accounts.** Plex's `/accounts` endpoint doesn't expose emails. Plex.tv `/api/v2/friends` is the only source — limited to currently-shared 3 friends, of which 2 have emails. Ombi.db remains the canonical seed.
- **No Listmonk-as-newsletter-content-renderer.** Listmonk handles subscriber DB + ad-hoc broadcasts only. Newsletterr+Conjurr own auto-content. Backup plan if Newsletterr falls through: a Python "Tautulli → Listmonk transactional" relay (~150 lines) that creates and sends weekly campaigns from Listmonk directly. Implement only if Newsletterr proves unreliable.

---

## Total scope

- **13 new tracked files** (`scripts/configure/40..48-*` + `scripts/ops/heartbeat-{ntfy,listmonk,conjurr,newsletterr}.sh` + `scripts/ops/listmonk-sync-subscribers.py` + `scripts/ops/listmonk-to-newsletterr-sync.py` + `docs/external/newsletterr-schema.md`)
- **2 binary downloads** (ntfy 2.22.0, Listmonk 6.1.0)
- **2 git clones** (Conjurr v4.1.0, Newsletterr v2026.1)
- **1 new Postgres DB** (`listmonk` on existing 17.9 instance)
- **4 new user-systemd services** (`ntfy.service`, `listmonk.service`, `conjurr.service`, `newsletterr.service`)
- **4 new heartbeat crons** (every 5 min, one per service)
- **2 new sync crons** (Listmonk subscriber sync 04:00; Listmonk→Newsletterr bridge 04:15)
- **4 new nginx path fragments** (`/ntfy/`, `/listmonk/`, `/conjurr/`, `/newsletterr/`) under existing `quadstronaut.seedbox.example.com` — no subdomain registration, no Ultra.cc support ticket required
- **4 ports claimed from `app-ports free`** (42014-42017 by default — within user's assigned range)
- **1 new public path** (`/alerts/` on the existing public host, auth-basic off)
- **1 Homarr widget + 1 Homarr tile** added to the public board
- **5 new smoke tests** (`ntfy-health`, `listmonk-subscribers`, `alerts-page`, `conjurr-up`, `newsletterr-up`, `newsletterr-recipients`)
- **0 secrets committed.** All new credentials live in gitignored `secrets/`.

Estimated install time end-to-end: ~5-6 hours of careful execution (incl. Playwright chromium download + Gemini key + operator's SMTP-config + Newsletterr template build + cutover-email steps). Zero waiting on Ultra.cc support — entirely self-serve via SSH + nginx fragments.

Estimated install time end-to-end: ~3-4 hours of careful execution + operator's SMTP-config + cutover-email steps.

---

## Execution protocol

### Step 0 — Pre-execution check

- [ ] Operator pre-reqs from "Open decisions" / probe-findings answered.
- [ ] **`secrets/listmonk.smtp_password` exists** (Gmail App Password). Hard blocker for Phase 19.
- [ ] **`secrets/gemini.api_key` exists**. Hard blocker for Phase 22 (Conjurr).
- [ ] **Wizarr install (separate plan) is at least at Phase 36** — Ombi decom in Phase 26 is gated on Wizarr being live.
- [ ] Postgres native instance reachable + decoded password verified.
- [ ] Working tree clean.

### Step 1 — Credential pre-flight (consolidated, one-time)

| Cred | Used by (this plan) | Likely already exists? |
|---|---|---|
| `listmonk.smtp_password` | Phase 19 SMTP | **Yes** (operator captured) |
| `gemini.api_key` | Phase 22 Conjurr | **Yes** (operator captured) |
| `htpasswd.password` | Phases 19, 22, 23 (admin paths) | **Yes** — shared admin password |
| `plex.token` | Phase 20 subscriber-sync, Phase 22-23 watch-history | **Yes** |
| `jellyseerr.key`, `jellyseerr.port` | Phase 20 subscriber-sync | **Yes** |
| `jellyfin.key`, `jellyfin.port` | Phase 20 subscriber-sync | **Yes** |
| `ntfy.webpush_public`, `ntfy.webpush_private` | Phase 17 VAPID | No — generated at install |
| `listmonk.api_user`, `listmonk.api_token` | Phases 19, 20, 24 | No — Listmonk generates at first run |
| `conjurr.app_secret` | Phase 22 Flask | No — generated at install |
| `newsletterr.flask_secret` | Phase 23 Flask | No — generated at install |
| Postgres password | Phase 19 DB create | **Yes** — already decoded from `~/.apps/postgres/.encoded.dat` |
| `ntfy.port`, `listmonk.port`, `conjurr.port`, `newsletterr.port` | service binding | No — claimed via `app-ports free` |

No hard blockers remaining (operator provided both Gmail App Password + Gemini key).

**Browser policy:**
- Phase 17 ntfy admin: CLI via `ntfy access`.
- Phase 19 Listmonk first-run wizard: Web UI mostly; admin password reuse + import-list both have CLI/API equivalents — prefer those.
- Phase 22 Conjurr admin Web UI: minimal config; reasonable to do via Web UI.
- Phase 23 Newsletterr template builder: **drag-and-drop in browser** — unavoidable, document in `docs/operator-deferred.md`.
- All other phases: CLI/API.

### Step 2 — Implementation phases

Continuous execution per `feedback_continuous-execution-preferred.md`: don't ask for per-phase approval. Pause only on genuine blockers.

Phases in this plan:
- **Phase 17** — ntfy install + service + nginx + heartbeat
- **Phase 18** — `/alerts` onboarding page (boomer-proof)
- **Phase 19** — Listmonk install + DB + service + bootstrap
- **Phase 20** — Subscriber-sync cron (Plex/Jellyseerr/Jellyfin → Listmonk)
- **Phase 21** — Homarr Recently Added widget + Get-notified tile
- **Phase 22** — Conjurr (AI rec engine) install + service
- **Phase 23** — Newsletterr install + service + initial config
- **Phase 24** — Listmonk → Newsletterr recipient bridge
- **Phase 25** — Migration cutover email + Newsletterr digest cycle
- **Phase 26** — Ombi decom (gated on Wizarr live + ≥1 Wizarr invite issued)

Each phase = one commit.

### Step 3 — Self-check (after Phase 26)

1. Run `scripts/smoke-test.sh` — all 6 new tests must pass: `ntfy-health`, `listmonk-subscribers`, `alerts-page`, `conjurr-up`, `newsletterr-up`, `newsletterr-recipients`.
2. `git status` — clean.
3. Re-run smoke twice — `listmonk-subscribers` and `newsletterr-recipients` are sync-cron-dependent and can flake on race conditions.
4. Send a real cutover email via Listmonk to one test recipient + verify delivery + verify unsubscribe link works.

### Step 4 — Log audit

1. `journalctl --user -u {ntfy,listmonk,conjurr,newsletterr}.service --since "today" -p err`
2. App logs at `~/.apps/{ntfy,listmonk,conjurr,newsletterr}/logs/`
3. nginx error log — 4 new path fragments are added; misconfig surfaces here
4. Postgres log — `~/.apps/postgres/log/postgresql-*.log` for `listmonk` DB-related errors
5. Cron sync scripts: `~/.apps/listmonk/logs/sync.log` + `~/.apps/newsletterr/logs/bridge.log`

Classify each error:
- **Cosmetic** (Conjurr "Gemini quota near limit" warnings) — monitor.
- **Actionable** (listmonk SMTP auth fail, ntfy webpush registration fail) — fix, re-test, re-audit.
- **Blocking** (Postgres connection refused, Listmonk admin login fails) — stop, surface immediately.

### Step 5 — Final summary template

```
# Mass-comms (Ombi replacement) implementation
- Phases completed: 17, 18, 19, 20, 21, 22, 23, 24, 25, 26
- Scripts added: 13 (configure + ops/heartbeat + sync)
- Services: ntfy, listmonk, conjurr, newsletterr (4 user-systemd)
- Configs: 4 nginx fragments, 1 Postgres DB, VAPID keys
- Smoke: N/N pass (was M/M before)

# Cutover verification
- Test cutover email delivered: yes/no
- Unsubscribe link works: yes/no
- ntfy push received on operator's phone: yes/no
- Newsletterr first auto-digest sent: yes/no
- Ombi decom (Phase 26): completed / blocked on [reason]

# Log audit
- Cosmetic: [list]
- Actionable (fixed): [list]
- Blockers requiring operator: [list, or "none"]

# Follow-up parking lot
- Conjurr free-tier rate-limit alarm threshold tuning
- Newsletterr template iteration based on user feedback
- Python relay fallback (if Newsletterr stability issues emerge)
```

### Hard rules (non-negotiable)

- **No 4K** anywhere — Recyclarr profiles, Kometa overlays, transcoder hints, request quotas. 1080p ceiling. (per `feedback_no-4k-profiles.md`)
- **Pin every version** — never `latest`, never `main`. Surface pinned versions in `versions.env` at repo root for the future updater. (per `feedback_pin-app-versions.md`)
- **Plex-primary** in all media-server config; Jellyfin gets parity only where the feature explicitly serves trial users. (per `project_plex-primary-jellyfin-trial.md`)
- **Reuse `secrets/htpasswd.password`** for any new admin-facing self-hosted app. (per `feedback_shared-admin-password.md`)
- **Read ports from `~/.apps/nginx/proxy.d/<app>.conf`** at runtime, not config.xml. (per `project_manitoba-network-model.md`)
- **Continuous execution** — no per-phase approval. Pause only on missing creds, smoke failure, or blocking log errors. (per `feedback_continuous-execution-preferred.md`)
- **Browser is last resort** — CLI/API everywhere; defer manual browser steps to `docs/operator-deferred.md`; Playwright authorized only when no alternative exists.
- **Modern AI-augmented preferred** when there's a choice; willing to fork if upstream stalls. (per `feedback_modern-ai-augmented-apps.md`)

### Failure modes to avoid (this plan)

- **Never decom Ombi (Phase 26) without Wizarr live + ≥1 successful Wizarr invite issued** — Ombi was the invite path too. Decom without Wizarr cutover = friends/family lose onboarding capability silently.
- **Never let Listmonk admin path lose htpasswd** — admin UI must always be auth-gated. Public unsubscribe path stays open by design.
- **Never put 4K/UHD profile selectors in newsletter template** — even if a user requests "send me only 4K releases", policy says no.
- **Never run Conjurr without Gemini quota guard** — free tier is 1500 req/day; runaway prompts can exhaust it. The plan caps at ~13 reqs/week (well under) but sanity-check after first week.
- **Never ship VAPID keys** without offline backup — losing them invalidates every subscriber's Web Push.
- **Never trust Plex's `/accounts` for emails** — it doesn't expose them. Subscriber sync must use Plex.tv `/api/v2/friends` + Ombi.db seed.
- **Never let Newsletterr's Playwright Chromium auto-update** without operator approval — pinned chromium version is the contract; auto-update breaks reproducibility.
- Don't commit secrets — gitignored.
