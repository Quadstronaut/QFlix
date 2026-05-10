# Wizarr Install Plan (Manitoba) — **PARKED 2026-05-08**

> **PARKED.** Wizarr's Flask middleware hardcodes paths like `request.path.startswith("/setup")` and provides no `APPLICATION_ROOT` / `url_prefix` knob, so sub-path hosting at `quadstronaut.seedbox.example.com/wizarr/` doesn't work. Unlike Conjurr/Newsletterr (which we made internal-only), Wizarr's invite links MUST be PUBLIC for invitees to click — internal-only is not a viable pivot.
>
> Operator decision (2026-05-08): keep Ombi running for the invite use-case only (its mass-comms function is now Listmonk's per Phase 19-24). Phase 26 (Ombi decom) stays parked indefinitely. Revisit Wizarr if/when an Ultra.cc subdomain becomes available, or if upstream Wizarr adds `APPLICATION_ROOT` support.

> **For agentic workers:** Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax.

**Goal:** Install Wizarr to handle new-user invitations + automatic library access for Plex (and Jellyfin, since it's running in trial). Replaces the invite functionality bundled into Ombi when Ombi is decommissioned.

**Architecture:** Python Flask + Gunicorn web app, SQLite-backed, self-hosted. Path-based nginx exposure under `quadstronaut.seedbox.example.com/wizarr/`. Public invite paths (`/wizarr/j/<token>`) bypass htpasswd so invitees can complete onboarding without a shared password; admin paths (`/wizarr/admin`, `/wizarr/settings`) are htpasswd-protected.

**Why Wizarr (not Ombi v2 or Jellyseerr):** Ombi is being decom'd. Jellyseerr handles request management beautifully but has no invite flow — you still have to manually share a Plex library and walk the user through app install. Wizarr is the missing onboarding piece: generate a link → user clicks → account is provisioned on Plex (and optionally Jellyfin) → libraries are auto-shared → user is walked through app install on their device. Active project (3.2k stars, monthly releases, MIT).

**Non-goals:**
- Replacing Jellyseerr / Plex's request management.
- Replacing Plex's own user management UI (Wizarr writes via API, doesn't replace the dashboard).
- Audiobookshelf / Komga / Kavita invite handling (Wizarr supports them; defer until operator wants it — Plex + Jellyfin first).

---

## Probe findings (verified 2026-05-08, applicable to this plan)

| Fact | Value | Source |
|---|---|---|
| Public exposure model | **path-based** — `https://quadstronaut.seedbox.example.com/wizarr/`. Public invite path (`/wizarr/j/...` and assets) NOT htpasswd-protected; everything else IS. | [Ultra.cc generic-software-installation docs](https://docs.ultra.cc/unofficial-application-installers/generic-software-installation) |
| Port allocation | Use `app-ports free`. Pre-claimed: **42020** (next free after Tdarr 42018+42019). | live probe |
| nginx reload | `systemctl --user reload nginx`. | live probe |
| Plex token + URL | `secrets/plex.token` + `secrets/plex.host` + `secrets/plex.port` (already captured). | existing |
| Jellyfin URL + key | `secrets/jellyfin.port` + `secrets/jellyfin.key` (already captured). | existing |
| Jellyseerr URL + key | `secrets/jellyseerr.port` + `secrets/jellyseerr.key` (already captured). For optional Wizarr → Jellyseerr handoff. | existing |
| Wizarr version | v2026.4.0 latest stable (released 2026-04-01); pin via `secrets/wizarr.version`. | [Wizarr GitHub releases](https://github.com/wizarrrr/wizarr/releases) |
| Stack | Python 3.10+, Flask + Gunicorn, SQLite (default) or Postgres (we'll keep SQLite — DB is small, ~MB scale, no Postgres benefit). | Wizarr docs |
| Auth model | Wizarr's *admin UI* has its own login (username + password). Per `feedback_shared-admin-password.md`, reuse `secrets/htpasswd.password`. The htpasswd at nginx is defense-in-depth. | feedback_shared-admin-password.md |

## Conventions

- `SSHM` = `ssh -o BatchMode=yes quadstronaut@seedbox.example.com`. Defined in `scripts/lib/ssh.sh`.
- All installs idempotent.
- **Pin versions.** Wizarr pinned in `secrets/wizarr.version` (default: v2026.4.0).
- **Shared admin password.** Wizarr admin login reuses `secrets/htpasswd.password`. Username: `quadstronaut`.
- All commits include `Co-Authored-By: Claude Opus 4.7`.

---

## Phase 35 — Wizarr install

### Task 35.1: Allocate port + capture pin

**Files:**
- Create: `scripts/configure/57-wizarr-install.sh`

- [ ] **Step 1: Pin version + claim port**

```bash
if ! secret_exists wizarr.version; then
  secret_write wizarr.version "v2026.4.0"  # operator may bump; check GitHub releases
fi

if ! secret_exists wizarr.port; then
  USED=""
  for s in tdarr.webui_port tdarr.server_port ntfy.port listmonk.port conjurr.port newsletterr.port; do
    secret_exists "$s" && USED="${USED}|$(secret_read $s)"
  done
  PORT=$(sshm "app-ports free 2>/dev/null | grep -E '^[0-9]+$'" | grep -vE "^(${USED#|})$" | head -1)
  [ -n "$PORT" ] || die "no free port from app-ports"
  secret_write wizarr.port "$PORT"
fi
```

### Task 35.2: Clone + venv + dependencies

- [ ] **Step 1: Install**

```bash
WVER=$(secret_read wizarr.version)

sshm 'bash -s' <<EOF
set -euo pipefail
mkdir -p ~/.apps/wizarr
cd ~/.apps/wizarr
if [ ! -d wizarr ]; then
  git clone --branch "$WVER" --depth 1 https://github.com/wizarrrr/wizarr.git wizarr
else
  cd wizarr
  git fetch --tags --depth 1 origin "$WVER:refs/tags/$WVER" || true
  git checkout "$WVER"
  cd ..
fi

if [ ! -d venv ]; then
  python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip wheel >/dev/null
./venv/bin/pip install --no-cache-dir -r wizarr/requirements.txt >/dev/null
./venv/bin/pip install --no-cache-dir gunicorn >/dev/null

mkdir -p ~/.apps/wizarr/database ~/.apps/wizarr/logs
EOF
```

### Task 35.3: Bootstrap config + initial admin

**Files:**
- Env file: `~/.apps/wizarr/wizarr.env`

- [ ] **Step 1: Render env file**

```bash
PORT=$(secret_read wizarr.port)
HTPW=$(secret_read htpasswd.password)
SECRET_KEY=$(openssl rand -hex 32)

# Capture Wizarr's Flask SECRET_KEY for session signing — must persist across restarts.
if ! secret_exists wizarr.flask_secret_key; then
  secret_write wizarr.flask_secret_key "$SECRET_KEY"
fi
SECRET_KEY=$(secret_read wizarr.flask_secret_key)

sshm "cat > ~/.apps/wizarr/wizarr.env" <<EOF
# Wizarr environment — generated by scripts/configure/57-wizarr-install.sh.
APP_URL=https://quadstronaut.seedbox.example.com/wizarr
DATABASE_URL=sqlite:////home/quadstronaut/.apps/wizarr/database/database.db
SECRET_KEY=${SECRET_KEY}
SCRIPT_NAME=/wizarr
DISABLE_BUILTIN_AUTH=false
LOG_LEVEL=INFO
GUNICORN_WORKERS=2
GUNICORN_THREADS=4
EOF
chmod 600 ~/.apps/wizarr/wizarr.env
```

`SCRIPT_NAME=/wizarr` is the standard Flask path-prefix env var. Wizarr's docs confirm support; the prefix is also reflected to clients via the `X-Forwarded-Prefix` header set in nginx (Phase 35.5).

- [ ] **Step 2: First-run admin bootstrap**

Wizarr creates the admin account on first launch via env vars (`ADMIN_USERNAME` + `ADMIN_PASSWORD`) — but only if no admin exists yet. Add to env:

```bash
sshm "cat >> ~/.apps/wizarr/wizarr.env" <<EOF
ADMIN_USERNAME=quadstronaut
ADMIN_PASSWORD=${HTPW}
EOF
```

After first successful start, the admin account exists in SQLite. We leave the env vars in place — they're idempotent (Wizarr won't recreate).

### Task 35.4: User-systemd service

**Files:**
- Service: `~/.config/systemd/user/wizarr.service`

- [ ] **Step 1: Service unit**

```bash
PORT=$(secret_read wizarr.port)

sshm "cat > ~/.config/systemd/user/wizarr.service" <<UNIT
[Unit]
Description=Wizarr (Plex/Jellyfin onboarding & invites)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.apps/wizarr/wizarr
EnvironmentFile=%h/.apps/wizarr/wizarr.env
ExecStart=%h/.apps/wizarr/venv/bin/gunicorn \\
  --bind 127.0.0.1:${PORT} \\
  --workers 2 \\
  --threads 4 \\
  --timeout 120 \\
  --access-logfile %h/.apps/wizarr/logs/access.log \\
  --error-logfile %h/.apps/wizarr/logs/error.log \\
  --forwarded-allow-ips '127.0.0.1' \\
  app:app
Restart=on-failure
RestartSec=10s
TimeoutStopSec=30

[Install]
WantedBy=default.target
UNIT
sshm 'systemctl --user daemon-reload && systemctl --user enable --now wizarr.service'
sleep 5
sshm 'systemctl --user is-active wizarr.service' | grep -q active || die "wizarr not active"
```

**Note on `app:app`:** Wizarr's WSGI entrypoint may be at `app.py:app` or `wizarr/app.py:app`. Verify with `grep -RE '^(app|application)\s*=' wizarr/*.py wizarr/wizarr/*.py 2>/dev/null` after clone — adjust the gunicorn arg if needed.

### Task 35.5: nginx path fragment

- [ ] **Step 1: Drop fragment**

The fragment must split into TWO `location` blocks:
- `/wizarr/j/` (and other public invite paths) → `auth_basic off` so invitees can complete onboarding.
- `/wizarr/` (admin, settings) → htpasswd inherited from server block.

```bash
PORT=$(secret_read wizarr.port)
sshm "cat > ~/.apps/nginx/proxy.d/wizarr.conf" <<EOF
# Public invite-acceptance paths — no htpasswd (invitees won't have it).
# Wizarr's own auth model: invite tokens are unguessable, server validates server-side.
location ~ ^/wizarr/(j/|setup/|join/|static/|assets/|public/|api/public/) {
    auth_basic off;
    proxy_pass http://127.0.0.1:${PORT};
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-Prefix /wizarr;
    proxy_buffering off;
}

# Admin / settings / everything else — htpasswd-protected (inherited from server block).
location /wizarr/ {
    proxy_pass http://127.0.0.1:${PORT};
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-Prefix /wizarr;
    proxy_buffering off;
}
EOF
sshm '/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t && systemctl --user reload nginx'
```

**Security note:** the `~ ^/wizarr/(...)` regex is the *more specific* match in nginx, so it wins over `/wizarr/`. Verify with: `curl -sI https://quadstronaut.seedbox.example.com/wizarr/j/test` returns 200/302/404 (no 401), and `curl -sI https://quadstronaut.seedbox.example.com/wizarr/admin` returns 401.

### Task 35.6: Heartbeat

**Files:**
- Heartbeat: `scripts/ops/heartbeat-wizarr.sh`

```bash
cat > scripts/ops/heartbeat-wizarr.sh <<'EOF'
#!/usr/bin/env bash
PORT=$(grep -oE '127\.0\.0\.1:[0-9]+' ~/.config/systemd/user/wizarr.service | head -1 | cut -d: -f2)
[ -n "$PORT" ] || exit 1
curl -sfm 5 "http://127.0.0.1:${PORT}/wizarr/health" >/dev/null && exit 0
systemctl --user is-active wizarr.service >/dev/null && exit 0
logger -t wizarr-heartbeat "wizarr unhealthy — restarting"
systemctl --user restart wizarr.service
EOF
scpm_to scripts/ops/heartbeat-wizarr.sh '~/scripts/ops/heartbeat-wizarr.sh'
sshm 'chmod +x ~/scripts/ops/heartbeat-wizarr.sh && (crontab -l | grep -v heartbeat-wizarr; echo "*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-wizarr.sh") | crontab -'
```

If `/wizarr/health` doesn't exist on this Wizarr version, fall back to `/wizarr/` and accept any 200/302.

### Task 35.7: Verify

- [ ] **Step 1: Admin login reachable**

```bash
HTPW=$(secret_read htpasswd.password)
curl -sIk -u "quadstronaut:$HTPW" "https://quadstronaut.seedbox.example.com/wizarr/admin"
# Expect 200 or 302 (redirect to /wizarr/login)
```

- [ ] **Step 2: Public invite URL reachable WITHOUT htpasswd**

```bash
curl -sIk "https://quadstronaut.seedbox.example.com/wizarr/j/no-such-token-yet"
# Expect 200/302/404 — but NOT 401 (which would mean htpasswd leaked into public path)
```

- [ ] **Step 3: Login as admin, complete the setup wizard in browser** — see Phase 36.

---

## Phase 36 — Operator: connect Plex + Jellyfin + Jellyseerr

This phase is operator-driven Web UI work. The Wizarr UI walks you through; this checklist ensures nothing is missed.

### Task 36.1: Initial setup wizard

- [ ] **Step 1:** Open `https://quadstronaut.seedbox.example.com/wizarr/` in a browser (with htpasswd on the admin path).
- [ ] **Step 2:** Login as `quadstronaut` / shared admin password.
- [ ] **Step 3:** Setup wizard prompts for:
  - **Server type**: Plex (primary).
  - **Plex URL**: `http://127.0.0.1:<plex.port>` from secrets.
  - **Plex Token**: paste `secret_read plex.token`.
  - Wizarr fetches the library list — confirm and pick which libraries new invitees will be auto-shared into.
  - **Default access policy**: pick "Pending Plex Pass features off / On" matching your environment.

### Task 36.2: Add Jellyfin as secondary server (optional)

Per memory: Plex is primary, Jellyfin is in trial. If Jellyfin trial is still ongoing, add it to the invite flow so trial users get accounts on both:

- [ ] Settings → Servers → Add Server → Jellyfin.
- [ ] **Jellyfin URL**: `http://127.0.0.1:<jellyfin.port>`.
- [ ] **API key**: paste `secret_read jellyfin.key`.
- [ ] Pick which libraries are auto-shared.
- [ ] Wizarr creates Jellyfin user accounts in addition to Plex on invite acceptance — invitees get one link, two accounts.

### Task 36.3: Connect Jellyseerr (request handoff)

- [ ] Settings → Integrations → Jellyseerr.
- [ ] **Jellyseerr URL**: `http://127.0.0.1:<jellyseerr.port>`.
- [ ] **API key**: paste `secret_read jellyseerr.key`.
- [ ] Toggle "Auto-create Jellyseerr account on invite acceptance" → ON.

After this, the invite flow becomes: link → Plex account + Jellyfin account + Jellyseerr account, library access pre-shared, Jellyseerr request UI ready to use, all from one click.

### Task 36.4: Configure invitation defaults

- [ ] **Default expiration**: 7 days for invite links (operator can override per-invite).
- [ ] **Default tier / access level**: define one (e.g. "Standard User" — full library access, no admin) and make it the default. Tiers are operator-configurable per-Wizarr — set what makes sense for your audience.
- [ ] **SSO / Discord** integrations: skip unless operator wants them — Discord webhook is nice for "user joined" notifications, ties into ntfy ecosystem already planned.
- [ ] **Custom welcome page**: edit Settings → Customization → Welcome message. Match the Plex-primary, country-folk-friendly tone established in `/alerts` (mass-comms plan Phase 18). Keep it under 100 words.

### Task 36.5: Generate test invite + walk through

- [ ] **Step 1:** Admin → Invitations → New Invite. Copy the invite URL (looks like `https://quadstronaut.seedbox.example.com/wizarr/j/<32-char-token>`).
- [ ] **Step 2:** Open in incognito browser (no htpasswd cookie). Confirm: full onboarding flow runs end-to-end without ever hitting htpasswd. New Plex user appears in Plex's Users tab.
- [ ] **Step 3:** Delete the test user from Plex (and Jellyfin, and Jellyseerr) before going live.

---

## Phase 37 — Cutover: Ombi → Wizarr (replaces mass-comms Phase 26 step 4)

This phase folds into the existing mass-comms plan's Ombi decom. Adds the invite-flow handoff that the mass-comms plan didn't account for.

### Task 37.1: Migrate existing users (one-time bulk import)

Ombi knows about the existing Plex users (it auto-syncs them). Wizarr can ingest them so they show up in the dashboard:

- [ ] **Step 1: Export from Ombi** — Ombi → Settings → Users → "Export to CSV" (or query the Ombi sqlite directly):

```bash
sshm "sqlite3 ~/.apps/ombi/database/Ombi.db 'SELECT UserName, Email FROM AspNetUsers WHERE EmailConfirmed = 1' -csv > /tmp/ombi-users.csv"
scpm_from /tmp/ombi-users.csv ./tmp/ombi-users.csv
```

- [ ] **Step 2: Import into Wizarr** — Wizarr does NOT have a CSV bulk-import (verified by checking the v2026.4.0 release notes). Instead: the operator runs a one-time Python script that hits Wizarr's REST API per user.

This is small enough (typical user list ~10-30) that the operator can also just use the Wizarr UI to "Add user manually" for each — preserve audience names that match Plex usernames so cross-references work.

### Task 37.2: Update mass-comms `/alerts` page

Replace any "Click here to invite a friend" link in `/alerts` (mass-comms plan Phase 18) that pointed at Ombi → point at the Wizarr admin invite-generation flow OR keep it admin-only and remove the link entirely. Operator decides — most installs make invite generation admin-only.

### Task 37.3: Decommission Ombi (only after Wizarr cutover lands)

This step lives in the mass-comms plan as Phase 26. Wizarr install must be done + at least one new invite must have been issued via Wizarr before Ombi is shut down. Update the mass-comms plan's Phase 26 prerequisites to include "Wizarr install complete + 1 successful invite issued".

---

## Smoke test additions

Add three new tests to `scripts/smoke-test.sh`:

```bash
# 25. Wizarr admin reachable + auth-gated
echo "25. Wizarr admin"
HTPW=$(secret_read htpasswd.password)
WA_HTTP=$(curl -sIk -u "quadstronaut:$HTPW" -o /dev/null -w '%{http_code}' "https://quadstronaut.seedbox.example.com/wizarr/admin")
case "$WA_HTTP" in 200|302) record "wizarr-admin-up" pass "HTTP $WA_HTTP" ;; *) record "wizarr-admin-up" fail "HTTP $WA_HTTP" ;; esac

# 26. Wizarr public invite path NOT auth-gated (critical — invitees don't have htpasswd)
echo "26. Wizarr public invite path"
WP_HTTP=$(curl -sIk -o /dev/null -w '%{http_code}' "https://quadstronaut.seedbox.example.com/wizarr/j/smoke-token")
if [ "$WP_HTTP" = "401" ]; then
  record "wizarr-public-no-htpasswd" fail "public path is htpasswd-gated — invitees will be blocked"
else
  record "wizarr-public-no-htpasswd" pass "HTTP $WP_HTTP (no htpasswd on public path)"
fi

# 27. Wizarr service is up
echo "27. Wizarr service"
WS=$(sshm "systemctl --user is-active wizarr.service 2>/dev/null")
if [ "$WS" = "active" ]; then
  record "wizarr-service" pass
else
  record "wizarr-service" fail "$WS"
fi
```

Test 26 is the **critical one** — if htpasswd accidentally leaks onto `/wizarr/j/`, every invite is broken silently for the recipient. Smoke test catches the regression on every run.

---

## Rollback per phase

| Phase | If broken |
|---|---|
| 35 (Install) | `systemctl --user disable --now wizarr.service && rm -rf ~/.apps/wizarr ~/.config/systemd/user/wizarr.service ~/.apps/nginx/proxy.d/wizarr.conf && systemctl --user reload nginx`. Crontab strip the heartbeat. **Plex / Jellyfin / Jellyseerr are untouched** (Wizarr never deletes accounts on its end-of-life). |
| 36 (Connect) | Wrong Plex URL or token → fix in Wizarr UI → Settings → Servers. No rollback needed. |
| 37 (Cutover) | If users start complaining post-Ombi-decom, the safest rollback is to **temporarily restore Ombi from backup** (mass-comms plan Phase 26 retains an Ombi DB snapshot at `~/.apps/backup/ombi-pre-decom.tar.gz`). Wizarr is the steady-state plan; only revert in genuine emergency. |

---

## Cost summary

- **Wizarr**: $0 (MIT, open source).
- **Disk**: ~50 MB code + venv + ~MB-scale SQLite database growth.
- **CPU**: minimal — Flask/Gunicorn idle is single-digit MB RAM, near-0 CPU.
- **Operator effort**: ~30 min for Phase 35 automation, ~15 min for Phase 36 setup wizard, ~ad-hoc per new invite.

---

## What this plan does NOT do

- **No automatic CSV import from Ombi** — Wizarr v2026.4.0 lacks bulk-import; operator handles ad-hoc.
- **No SSO / Discord integration by default** — opt-in via Phase 36.4 if desired.
- **No Wizarr-as-Plex-replacement.** Wizarr is invite-only; Plex's own UI still handles user management for power users.
- **No Audiobookshelf / Komga / Kavita integration** by default — defer until operator wants it (Wizarr supports these but they're outside the Plex-primary trial scope).
- **No multi-tenant** — single Wizarr instance for the single Plex server.

---

## Total scope

- **2 install scripts** (`scripts/configure/57-wizarr-install.sh` + `scripts/ops/heartbeat-wizarr.sh`)
- **1 git clone** (wizarrrr/wizarr, pinned tag)
- **1 Python venv** at `~/.apps/wizarr/venv/`
- **1 user-systemd service** (`wizarr.service`)
- **1 nginx path fragment** (`/wizarr/`) split into public (no auth) + admin (htpasswd) location blocks
- **1 port claimed** from `app-ports free` (42020 default)
- **1 heartbeat cron** (5-min interval)
- **3 new smoke tests** (`wizarr-admin-up`, `wizarr-public-no-htpasswd`, `wizarr-service`)
- **0 secrets committed.** Plex/Jellyfin/Jellyseerr keys reused, Wizarr admin password = shared admin password — all in gitignored `secrets/`.

Estimated install time: ~45 min (30 min Phase 35 install + 15 min Phase 36 connect-and-test).

---

## Open decisions (operator)

1. **Jellyfin in invite flow** — auto-create Jellyfin accounts alongside Plex? (Recommended *yes* during Jellyfin trial; can be toggled off later.)
2. **Default invite expiration** — 7 days suggested. Operator may want 24h for stricter security or 30 days for sloppy onboarding.
3. **Tiers** — define one default tier or multiple (e.g. "Family", "Friends", "Trial")? Multi-tier is operator effort vs. flexibility.
4. **Phase out Ombi invites first** — recommend running Wizarr in parallel with Ombi for ~1 week before flipping the decom switch in mass-comms plan Phase 26.
5. **CSV bulk-import scripting** — operator can request a small Python helper if user list is too long for ad-hoc. Default plan: skip; ad-hoc add per-user.

---

## Execution protocol

### Step 0 — Pre-execution check

- [ ] Operator pre-reqs from "Open decisions" answered (or defaults accepted).
- [ ] Required credentials in `secrets/` (see Step 1 below).
- [ ] Mass-comms Phase 26 (Ombi decom) is **NOT YET** flipped — Wizarr must be running + ≥1 successful invite before decom.
- [ ] Working tree clean.

### Step 1 — Credential pre-flight (consolidated, one-time)

| Cred | Used by (this plan) | Likely already exists? |
|---|---|---|
| `plex.token`, `plex.host`, `plex.port` | Phase 36 Plex connect | **Yes** |
| `jellyfin.port`, `jellyfin.key` | Phase 36 Jellyfin connect (optional) | **Yes** |
| `jellyseerr.port`, `jellyseerr.key` | Phase 36 Jellyseerr handoff (optional) | **Yes** |
| `htpasswd.password` | Phase 35 admin bootstrap (shared admin password) | **Yes** |
| `wizarr.flask_secret_key` | Phase 35 session signing | No — generated at install (`openssl rand -hex 32`) |
| `wizarr.port` | Phase 35 service binding | No — claimed via `app-ports free` |
| `wizarr.version` | Phase 35 pin | No — defaults to `v2026.4.0` |

No hard blockers — all required creds already exist or auto-generate.

**Browser policy:** Phase 36 setup wizard is browser-only (no API for Wizarr's first-run config flow). Document the click sequence in `docs/operator-deferred.md` so the operator owns the work. Phase 35 is fully CLI/API.

### Step 2 — Implementation phases

Continuous execution per `feedback_continuous-execution-preferred.md`: don't ask for per-phase approval. Pause only on genuine blockers.

Phases in this plan:
- **Phase 35** — Wizarr install + venv + service + nginx (Tasks 35.1-35.7)
- **Phase 36** — Operator: Plex/Jellyfin/Jellyseerr connect via Web UI (Tasks 36.1-36.5)
- **Phase 37** — Cutover bridge to mass-comms Phase 26 (Tasks 37.1-37.3)

Each phase = one commit.

### Step 3 — Self-check (after Phase 37)

1. Run `scripts/smoke-test.sh` — `wizarr-admin-up`, `wizarr-public-no-htpasswd`, `wizarr-service` must all pass.
2. `git status` — clean (3 new commits ahead of `origin/main`).
3. Re-run smoke twice. **`wizarr-public-no-htpasswd` is the critical gate** — if it ever flakes, every invite is broken silently for the recipient.
4. Generate one real test invite + complete acceptance in incognito browser before declaring "done".

### Step 4 — Log audit

1. `journalctl --user -u wizarr.service --since "today" -p err`
2. `~/.apps/wizarr/logs/{access.log,error.log}` — `grep -E 'ERROR|500|Traceback'`
3. nginx error log — `~/.apps/nginx/logs/error.log` for Wizarr-related entries (path-fragment misconfigs surface here)

Classify each error:
- **Cosmetic** (e.g. SQLAlchemy deprecation warnings) — note, don't act.
- **Actionable** (e.g. SCRIPT_NAME causing redirect loops) — fix, restart, re-audit.
- **Blocking** (e.g. Plex 401 in invite-acceptance flow) — stop, surface in summary.

### Step 5 — Final summary template

```
# Wizarr implementation
- Phases completed: 35, 36, 37
- Scripts added: 1 install + 1 heartbeat
- Configs: wizarr.env, nginx fragment (split public/admin)
- Smoke: N/N pass (was M/M before)

# Self-check results
- Public invite path verified WITHOUT htpasswd: yes/no
- Test invite acceptance end-to-end: yes/no

# Log audit
- Cosmetic: [list]
- Actionable (fixed): [list]
- Blockers requiring operator: [list, or "none"]

# Follow-up parking lot
- Audiobookshelf/Komga/Kavita invite integration
- Bulk Ombi → Wizarr CSV import script (if operator requests)
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

- **Never let htpasswd leak onto `/wizarr/j/`** — invitees don't have the shared password; if leaked, every invite returns 401 to the recipient. The smoke test `wizarr-public-no-htpasswd` is the regression gate; do NOT remove it.
- **Never decom Ombi until Wizarr cutover proven** — mass-comms Phase 26 must check Wizarr healthy + ≥1 invite issued before flipping the kill switch.
- **Don't restart Wizarr without graceful gunicorn shutdown** during an in-flight invite acceptance — invitees mid-onboarding will see a 502. `Restart=on-failure` + `TimeoutStopSec=30` cover this; don't shorten.
- **Don't expose Jellyfin user-create or Plex user-share via Wizarr's API** without auth — both endpoints sit behind admin htpasswd path; verify before publishing the API URL anywhere.
- Don't commit `wizarr.flask_secret_key` — gitignored.
- Don't run as the wrong WSGI entry — verify `app:app` matches the cloned tag's actual entrypoint (Phase 35.4 note).
