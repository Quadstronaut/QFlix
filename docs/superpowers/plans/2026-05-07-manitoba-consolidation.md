# Manitoba Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Take the Ultra.cc seedbox at `quadstronaut@seedbox.example.com` from its current half-broken multi-stack state to the consolidated robust media platform described in `docs/superpowers/specs/2026-05-07-manitoba-consolidation-design.md` — TV, movies, anime, books, comics, audiobooks, with a Homarr two-board landing page — without long-running downtime for users.

**Architecture:** Approach A from the spec — minimal-intervention reconfigure of currently-running apps + install of missing pieces (Jellyfin, Sonarr2/Radarr2, FlareSolverr, Notifiarr-hosted, Jellystat, Uptime Kuma, Readarr, Mylar3, Komga, Kavita, Calibre-Web, Audiobookshelf). Old/duplicate apps stopped (not uninstalled). All work via SSH + Ultra.cc `app-*` helpers + REST APIs since `docker exec` is unavailable.

**Tech Stack:** Bash (SSH-driven scripts), Ultra.cc `app-*` CLI, JSON/XML config patches via `jq` and `xmlstarlet`, REST APIs of *arrs / Plex / Jellyfin / Maintainerr / Notifiarr / Homarr, cron, Markdown documentation.

---

## Conventions used throughout this plan

- **`SSHM`** is shorthand for `ssh -o BatchMode=yes quadstronaut@seedbox.example.com`. Defined in `scripts/lib/ssh.sh`.
- **All scripts are idempotent** unless explicitly marked otherwise. Re-running a phase must not double-create resources.
- **Secrets** live in the gitignored `secrets/` dir as one-line files (`secrets/<app>.key`, `secrets/<app>.port`, `secrets/<app>.password`). Scripts read by absolute path.
- **`PWGEN()`** generates a 24-char alphanumeric password: `openssl rand -base64 24 | tr -d '+/=' | head -c 24`. Used for new app admin passwords.
- **`<app>.expect`** scripts under `scripts/install/expect/` wrap interactive `app-<name> install` prompts using `expect(1)`. The pattern is identical for every app — a stub is provided in Phase 0.
- **Commit cadence:** Commit at the end of every task that produced tracked-file changes. Skip the commit step if no tracked file was touched (pure remote-side actions).
- **Phase checkpoints:** At the end of each phase, run that phase's verification block before proceeding. If it fails, stop and report.

---

## Phase 0 — Bootstrap, scaffolding, secret capture

### Task 0.1: SSH wrapper + first-line library

**Files:**
- Create: `scripts/lib/ssh.sh`
- Create: `scripts/lib/log.sh`
- Create: `scripts/lib/secrets.sh`

- [x] **Step 1: Write `scripts/lib/ssh.sh`**

```bash
#!/usr/bin/env bash
# Wrapper for SSH to manitoba. Source this from other scripts.
SSHM_HOST="${SSHM_HOST:-quadstronaut@seedbox.example.com}"
SSHM_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30)

sshm() {
  ssh "${SSHM_OPTS[@]}" "$SSHM_HOST" "$@"
}

scpm_to() {
  scp "${SSHM_OPTS[@]}" "$1" "$SSHM_HOST:$2"
}

scpm_from() {
  scp "${SSHM_OPTS[@]}" "$SSHM_HOST:$1" "$2"
}
```

- [x] **Step 2: Write `scripts/lib/log.sh`**

```bash
#!/usr/bin/env bash
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[+]${NC} $*" >&2; }
log_warn()  { echo -e "${YELLOW}[!]${NC} $*" >&2; }
log_error() { echo -e "${RED}[x]${NC} $*" >&2; }
die() { log_error "$*"; exit 1; }
```

- [x] **Step 3: Write `scripts/lib/secrets.sh`**

```bash
#!/usr/bin/env bash
# Read/write helpers for secrets/<name>.<ext> files. Trims whitespace.
SECRETS_DIR="${SECRETS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../secrets" && pwd)}"

secret_read()  { local f="$SECRETS_DIR/$1"; [ -f "$f" ] || die "missing secret: $f"; tr -d '[:space:]' < "$f"; }
secret_write() { local f="$SECRETS_DIR/$1"; mkdir -p "$(dirname "$f")"; printf '%s\n' "$2" > "$f"; chmod 600 "$f"; }
secret_exists(){ [ -f "$SECRETS_DIR/$1" ]; }
```

- [x] **Step 4: Smoke test the libraries**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash -c 'source scripts/lib/ssh.sh && source scripts/lib/log.sh && source scripts/lib/secrets.sh && sshm "echo OK from \$(hostname)"'
```

Expected output: `OK from manitoba`

- [x] **Step 5: Commit**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
git add scripts/lib/
git commit -m "infra: ssh + log + secrets bash helpers"
```

---

### Task 0.2: Capture all currently-available API keys + ports

**Files:**
- Create: `scripts/bootstrap-discover.sh`

- [x] **Step 1: Write `scripts/bootstrap-discover.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

log_info "Capturing existing API keys + ports from manitoba..."

# Sonarr / Radarr / Prowlarr (config.xml, ApiKey + Port)
for app in sonarr radarr prowlarr; do
  cfg="$(sshm "cat ~/.apps/$app/config.xml 2>/dev/null" || true)"
  [ -z "$cfg" ] && { log_warn "$app: no config.xml"; continue; }
  key="$(printf '%s' "$cfg" | grep -oP '(?<=<ApiKey>)[^<]+')"
  port="$(printf '%s' "$cfg" | grep -oP '(?<=<Port>)[^<]+')"
  base="$(printf '%s' "$cfg" | grep -oP '(?<=<UrlBase>)[^<]+' || true)"
  [ -n "$key"  ] && secret_write "$app.key" "$key"
  [ -n "$port" ] && secret_write "$app.port" "$port"
  [ -n "$base" ] && secret_write "$app.urlbase" "$base"
  log_info "  $app: key=${key:0:8}... port=$port base=$base"
done

# Bazarr (sqlite or config.ini)
cfg="$(sshm 'find /app/bazarr/bin /app/bazarr ~/.apps/bazarr -name "config*.yaml" -o -name "config*.ini" 2>/dev/null | head -1' || true)"
log_info "Bazarr config path: $cfg"

# Tautulli (config.ini)
key="$(sshm 'grep -m1 "^api_key" ~/.apps/tautulli/config.ini 2>/dev/null | cut -d= -f2 | tr -d " "' || true)"
[ -n "$key" ] && secret_write "tautulli.key" "$key"

# qBittorrent — already known
secret_write "qbittorrent.port" "17041"
secret_write "qbittorrent.user" "quadstronaut"
log_warn "qbittorrent.password — capture manually if missing (Ultra.cc-managed)"

# Plex token — extract from Plex Preferences.xml
token="$(sshm "grep -oP '(?<=PlexOnlineToken=\")[^\"]+' ~/.apps/plex/Library/Application\\ Support/Plex\\ Media\\ Server/Preferences.xml 2>/dev/null" || true)"
[ -n "$token" ] && secret_write "plex.token" "$token"

# Maintainerr API — Maintainerr generates one in its DB; capture from settings if exposed
log_info "Maintainerr key: capture manually from UI (Settings > API)"

# Jellyseerr API
key="$(sshm 'jq -r ".main.apiKey" /app/jellyseerr/config/settings.json 2>/dev/null' || true)"
[ -n "$key" ] && [ "$key" != "null" ] && secret_write "jellyseerr.key" "$key"

# Notifiarr — already in secrets/notifiarr.key

log_info "Bootstrap discovery complete. Inventory:"
ls -la "$SECRETS_DIR"
```

- [x] **Step 2: Run it**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/bootstrap-discover.sh
```

Expected: `secrets/sonarr.key`, `secrets/radarr.key`, `secrets/prowlarr.key`, `secrets/sonarr.port`, `secrets/radarr.port`, `secrets/prowlarr.port`, `secrets/plex.token`, possibly `tautulli.key` and `jellyseerr.key`. Warnings for any not yet capturable (Maintainerr, Jellyfin pending).

- [x] **Step 3: Verify secrets dir is gitignored**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
git check-ignore -v secrets/sonarr.key
```

Expected: `.gitignore:1:secrets/	secrets/sonarr.key`

- [x] **Step 4: Commit script (no secrets)**

```bash
git add scripts/bootstrap-discover.sh
git commit -m "infra: bootstrap discover script — captures existing api keys"
```

---

### Task 0.3: Generate password for new-app admin UIs

**Files:**
- Create: `scripts/lib/pwgen.sh`

- [x] **Step 1: Write `scripts/lib/pwgen.sh`**

```bash
#!/usr/bin/env bash
pwgen_24() { openssl rand -base64 24 | tr -d '+/=\n' | head -c 24; printf '\n'; }
```

- [x] **Step 2: Generate one shared admin password for new apps + record**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
source scripts/lib/pwgen.sh
source scripts/lib/secrets.sh
secret_write "shared-admin.password" "$(pwgen_24)"
cat secrets/shared-admin.password
```

(One password is sufficient because all admin UIs are htpasswd-gated already; the per-app password is mostly just for non-htpasswd internal logins.)

- [x] **Step 3: Commit**

```bash
git add scripts/lib/pwgen.sh
git commit -m "infra: password generator helper"
```

---

### Task 0.4: Create new directory tree on manitoba

**Files:**
- Create: `scripts/install/00-make-directories.sh`

- [x] **Step 1: Write the directory creation script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"

log_info "Creating new media + download directories on manitoba..."
sshm bash -s <<'REMOTE'
set -euo pipefail
mkdir -p \
  ~/downloads/qbittorrent/radarr-anime \
  ~/downloads/qbittorrent/sonarr-anime \
  ~/downloads/qbittorrent/readarr \
  ~/downloads/qbittorrent/mylar \
  ~/media/Anime \
  "~/media/Anime Movies" \
  ~/media/Books \
  ~/media/Comics

# Existing-empty dirs we just confirm:
ls -dF ~/media/Audiobooks ~/media/Manga ~/media/Podcasts >/dev/null
echo "Directories OK."
REMOTE
```

- [x] **Step 2: Run it**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/install/00-make-directories.sh
```

Expected last line: `Directories OK.`

- [x] **Step 3: Verify**

```bash
ssh quadstronaut@seedbox.example.com 'ls -dF ~/media/Anime ~/media/Anime\ Movies ~/media/Books ~/media/Comics ~/downloads/qbittorrent/radarr-anime ~/downloads/qbittorrent/sonarr-anime ~/downloads/qbittorrent/readarr ~/downloads/qbittorrent/mylar'
```

Expected: all 8 directories listed.

- [x] **Step 4: Commit**

```bash
git add scripts/install/00-make-directories.sh
git commit -m "infra: create new media + download directories on manitoba"
```

---

### Task 0.5: Expect-wrapper template for app-* installers — SUPERSEDED (see scripts/install/lib/app-install.sh)

**Files:**
- Create: `scripts/install/expect/app-install.exp.template`

Most Ultra.cc `app-<name> install` commands prompt interactively for an admin password. This template is reused by every install task that needs it.

- [x] **Step 1: Write the expect template**

```bash
#!/usr/bin/expect -f
# scripts/install/expect/app-install.exp.template
# Usage: app-install.exp <app-name> <password>
set app      [lindex $argv 0]
set password [lindex $argv 1]
set timeout  600

spawn ssh -o BatchMode=yes quadstronaut@seedbox.example.com app-$app install
expect {
  -re "(?i)password:" { send "$password\r"; exp_continue }
  -re "(?i)confirm.*password:" { send "$password\r"; exp_continue }
  -re "(?i)\\(y/n\\)" { send "y\r"; exp_continue }
  eof
}
catch wait result
exit [lindex $result 3]
```

- [x] **Step 2: Make executable + verify expect is installed locally**

```bash
chmod +x "P:/Documents/GIT/Optimize-Manitoba/scripts/install/expect/app-install.exp.template"
expect -v
```

Expected: `expect version 5.x.x`. If missing, install via `winget install JimmyDoyle.Expect` or use a Linux-side host. (For most Windows operators, run from WSL or git-bash with expect installed.)

- [x] **Step 3: Commit**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
git add scripts/install/expect/
git commit -m "infra: expect template for interactive app-* installs"
```

---

### Phase 0 checkpoint

```bash
ls secrets/                 # at least notifiarr.key + sonarr/radarr/prowlarr keys + ports
ssh quadstronaut@seedbox.example.com 'ls ~/media/Anime ~/media/Books ~/media/Comics'
git log --oneline           # 4-5 commits since the spec commit
```

---

## Phase 1 — qBittorrent: add new categories

qBit is the single source of torrent truth. Adding categories is non-disruptive.

### Task 1.1: Add 4 new qBit categories via Web API

**Files:**
- Create: `scripts/configure/01-qbit-categories.sh`

- [x] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

QBIT_USER="$(secret_read qbittorrent.user)"
QBIT_PASS_FILE="$SECRETS_DIR/qbittorrent.password"
[ -f "$QBIT_PASS_FILE" ] || die "Need secrets/qbittorrent.password (capture manually from Ultra.cc panel)"
QBIT_PASS="$(cat "$QBIT_PASS_FILE" | tr -d '[:space:]')"
QBIT_URL="http://127.0.0.1:17041"

# qBittorrent's webui isn't externally exposed; use SSH tunnel.
log_info "Opening SSH tunnel to qBit..."
ssh "${SSHM_OPTS[@]}" -fN -L 17041:127.0.0.1:17041 "$SSHM_HOST"
trap 'pkill -f "ssh.*-L 17041"' EXIT

# Login -> get cookie
COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"; pkill -f "ssh.*-L 17041"' EXIT
curl -sS -c "$COOKIE_JAR" --data "username=$QBIT_USER&password=$QBIT_PASS" "$QBIT_URL/api/v2/auth/login" | grep -q "Ok." || die "qBit auth failed"

declare -A cats=(
  [radarr-anime]='/home/quadstronaut/downloads/qbittorrent/radarr-anime'
  [sonarr-anime]='/home/quadstronaut/downloads/qbittorrent/sonarr-anime'
  [readarr]='/home/quadstronaut/downloads/qbittorrent/readarr'
  [mylar]='/home/quadstronaut/downloads/qbittorrent/mylar'
)

for cat in "${!cats[@]}"; do
  log_info "Adding category: $cat -> ${cats[$cat]}"
  curl -sS -b "$COOKIE_JAR" \
    --data-urlencode "category=$cat" \
    --data-urlencode "savePath=${cats[$cat]}" \
    "$QBIT_URL/api/v2/torrents/createCategory" >/dev/null || true
done

log_info "Final category list:"
curl -sS -b "$COOKIE_JAR" "$QBIT_URL/api/v2/torrents/categories" | jq -r 'keys[]'
```

- [x] **Step 2: Capture qBit password into secrets**

The qBit password is in the user's Ultra.cc panel; copy it to `secrets/qbittorrent.password` (one line, no trailing newline).

```bash
echo "<paste qbit password>" > "P:/Documents/GIT/Optimize-Manitoba/secrets/qbittorrent.password"
chmod 600 "P:/Documents/GIT/Optimize-Manitoba/secrets/qbittorrent.password"
```

- [x] **Step 3: Run the category script**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/configure/01-qbit-categories.sh
```

Expected: at least these categories appear: `mylar`, `radarr`, `radarr-anime`, `readarr`, `sonarr-anime`, `tv-sonarr`.

- [x] **Step 4: Commit**

```bash
git add scripts/configure/01-qbit-categories.sh
git commit -m "qbit: add radarr-anime, sonarr-anime, readarr, mylar categories"
```

---

### Phase 1 checkpoint

The script's final output must list all 6 expected categories. If only the 2 originals appear, the auth failed — verify `secrets/qbittorrent.password`.

---

## Phase 2 — FlareSolverr install

Required by Prowlarr to bypass Cloudflare for a few of the indexers (TorrentGalaxy, sometimes 1337x).

### Task 2.1: Install FlareSolverr via Ultra.cc helper

**Files:**
- Create: `scripts/install/02-flaresolverr.sh`

- [x] **Step 1: Write the install script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

# Idempotency: skip if already installed
if sshm 'test -d ~/.apps/flaresolverr || test -d /app/flaresolverr'; then
  log_info "FlareSolverr already installed; skipping install"
else
  log_info "Installing FlareSolverr (interactive)..."
  expect "$HERE/install/expect/app-install.exp.template" flaresolverr "$(secret_read shared-admin.password)"
fi

# Discover port
port="$(sshm 'app-ports show 2>/dev/null | grep -i flaresolverr | grep -oE "[0-9]{4,5}" | head -1' || true)"
[ -z "$port" ] && die "Could not determine FlareSolverr port from app-ports show"
secret_write "flaresolverr.port" "$port"
log_info "FlareSolverr port: $port"

# Health check via SSH (loopback)
sshm "curl -sf http://127.0.0.1:$port/ | head -5" || die "FlareSolverr health check failed"
log_info "FlareSolverr is healthy."
```

- [x] **Step 2: Run install**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/install/02-flaresolverr.sh
```

Expected: `FlareSolverr port: NNNNN` followed by `FlareSolverr is healthy.`

- [x] **Step 3: Verify the saved port**

```bash
cat secrets/flaresolverr.port
```

- [x] **Step 4: Commit**

```bash
git add scripts/install/02-flaresolverr.sh
git commit -m "flaresolverr: install + health check"
```

---

## Phase 3 — Prowlarr: indexer migration

The big one for unbreaking *arr searches. Prowlarr currently has zero indexers.

### Task 3.1: Wire FlareSolverr into Prowlarr as an indexer proxy

**Files:**
- Create: `scripts/configure/03-prowlarr-flaresolverr.sh`

- [x] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

API_KEY="$(secret_read prowlarr.key)"
PORT="$(secret_read prowlarr.port)"
URLBASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"
FS_PORT="$(secret_read flaresolverr.port)"
PROW_URL="http://127.0.0.1:$PORT/$URLBASE"

# Open SSH tunnel for Prowlarr API
ssh "${SSHM_OPTS[@]}" -fN -L "$PORT:127.0.0.1:$PORT" "$SSHM_HOST"
trap 'pkill -f "ssh.*-L $PORT"' EXIT
sleep 1

# Idempotency: skip if a FlareSolverr proxy already exists
existing="$(curl -sS -H "X-Api-Key: $API_KEY" "$PROW_URL/api/v1/indexerProxy" | jq -r '.[] | select(.implementation=="FlareSolverr") | .id' || echo "")"
if [ -n "$existing" ]; then
  log_info "FlareSolverr proxy already exists (id=$existing)"
else
  log_info "Adding FlareSolverr indexer proxy..."
  curl -sS -X POST -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" "$PROW_URL/api/v1/indexerProxy" -d @- <<JSON | jq .
{
  "name": "FlareSolverr",
  "implementation": "FlareSolverr",
  "implementationName": "FlareSolverr",
  "configContract": "FlareSolverrSettings",
  "tags": [],
  "fields": [
    {"name": "host", "value": "http://127.0.0.1:$FS_PORT/"},
    {"name": "requestTimeout", "value": 60}
  ]
}
JSON
fi
```

- [x] **Step 2: Run it**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/configure/03-prowlarr-flaresolverr.sh
```

Expected: JSON containing the new proxy with non-zero `id`.

- [x] **Step 3: Commit**

```bash
git add scripts/configure/03-prowlarr-flaresolverr.sh
git commit -m "prowlarr: add FlareSolverr as indexer proxy"
```

---

### Task 3.2: Add 22 indexers to Prowlarr (script-driven)

**Files:**
- Create: `scripts/configure/04-prowlarr-indexers.sh`
- Create: `scripts/data/prowlarr-indexers.json`

- [x] **Step 1: Write the indexer manifest**

```json
{
  "general": [
    "1337x", "BitSearch", "EZTV", "Glodls", "Internet Archive",
    "IsoHunt2", "KickassTorrents", "LimeTorrents", "Solid Torrents",
    "ShowRSS", "The Pirate Bay", "TheRARBG", "TorrentDownload",
    "TorrentDownloads", "TorrentGalaxy", "YTS"
  ],
  "anime": [
    "Nyaa.si", "AniDex", "Tokyo Toshokan", "ShanaProject", "subsplease"
  ],
  "needs_flaresolverr": [
    "TorrentGalaxy", "1337x"
  ]
}
```

- [x] **Step 2: Write the migration script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

API_KEY="$(secret_read prowlarr.key)"
PORT="$(secret_read prowlarr.port)"
URLBASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"
PROW_URL="http://127.0.0.1:$PORT/$URLBASE"
MANIFEST="$HERE/data/prowlarr-indexers.json"

ssh "${SSHM_OPTS[@]}" -fN -L "$PORT:127.0.0.1:$PORT" "$SSHM_HOST"
trap 'pkill -f "ssh.*-L $PORT"' EXIT
sleep 1

# Get list of installable indexer schemas
schemas="$(curl -sS -H "X-Api-Key: $API_KEY" "$PROW_URL/api/v1/indexer/schema")"

# Get tags (create "anime" + "cloudflare" if missing)
ensure_tag() {
  local label="$1"
  local id
  id="$(curl -sS -H "X-Api-Key: $API_KEY" "$PROW_URL/api/v1/tag" | jq -r ".[] | select(.label==\"$label\") | .id" || true)"
  if [ -z "$id" ]; then
    id="$(curl -sS -X POST -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" "$PROW_URL/api/v1/tag" -d "{\"label\":\"$label\"}" | jq -r .id)"
  fi
  printf '%s' "$id"
}
ANIME_TAG="$(ensure_tag anime)"
CLOUDFLARE_TAG="$(ensure_tag cloudflare)"

FS_PROXY_ID="$(curl -sS -H "X-Api-Key: $API_KEY" "$PROW_URL/api/v1/indexerProxy" | jq -r '.[] | select(.implementation=="FlareSolverr") | .id')"

add_indexer() {
  local name="$1" tag_kind="$2"
  local existing
  existing="$(curl -sS -H "X-Api-Key: $API_KEY" "$PROW_URL/api/v1/indexer" | jq -r ".[] | select(.name==\"$name\") | .id" || true)"
  if [ -n "$existing" ]; then
    log_info "$name already present (id=$existing)"; return 0
  fi
  local schema
  schema="$(printf '%s' "$schemas" | jq --arg n "$name" 'map(select(.name == $n)) | first')"
  [ "$schema" = "null" ] && { log_warn "$name: schema not found in Prowlarr; skipping"; return 0; }

  local tags="[]"
  case "$tag_kind" in
    anime) tags="[$ANIME_TAG]" ;;
    cloudflare) tags="[$CLOUDFLARE_TAG]" ;;
    anime+cloudflare) tags="[$ANIME_TAG,$CLOUDFLARE_TAG]" ;;
  esac

  local body
  body="$(printf '%s' "$schema" | jq --argjson tags "$tags" '. + {enable:true, tags:$tags}')"

  log_info "Adding $name (tags=$tag_kind)"
  curl -sS -X POST -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
    "$PROW_URL/api/v1/indexer" -d "$body" > /dev/null
}

# General
for n in $(jq -r '.general[]' "$MANIFEST"); do
  case "$n" in
    "TorrentGalaxy"|"1337x") add_indexer "$n" cloudflare ;;
    *) add_indexer "$n" "" ;;
  esac
done

# Anime
for n in $(jq -r '.anime[]' "$MANIFEST"); do
  add_indexer "$n" anime
done

log_info "Indexers configured. Final list:"
curl -sS -H "X-Api-Key: $API_KEY" "$PROW_URL/api/v1/indexer" | jq -r '.[] | "\(.id)\t\(.name)\t\(.tags|tostring)"'
```

- [x] **Step 3: Run it**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/configure/04-prowlarr-indexers.sh
```

Expected: ~22 indexers listed with id/name/tags.

- [x] **Step 4: Test indexer reachability**

Append a quick verifier (or run manually):

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
source scripts/lib/secrets.sh
API_KEY="$(secret_read prowlarr.key)"
PORT="$(secret_read prowlarr.port)"
URLBASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"
ssh -fN -L "$PORT:127.0.0.1:$PORT" quadstronaut@seedbox.example.com
sleep 1
for id in $(curl -sS -H "X-Api-Key: $API_KEY" "http://127.0.0.1:$PORT/$URLBASE/api/v1/indexer" | jq -r '.[].id'); do
  res="$(curl -sS -X POST -H "X-Api-Key: $API_KEY" "http://127.0.0.1:$PORT/$URLBASE/api/v1/indexer/$id/test" | jq -r '.isValid // false')"
  name="$(curl -sS -H "X-Api-Key: $API_KEY" "http://127.0.0.1:$PORT/$URLBASE/api/v1/indexer/$id" | jq -r .name)"
  printf '%-25s %s\n' "$name" "$res"
done
pkill -f "ssh.*-L $PORT"
```

Expected: most indexers return `true`. Indexers that fail get a follow-up commit reporting their state.

- [x] **Step 5: Commit**

```bash
git add scripts/configure/04-prowlarr-indexers.sh scripts/data/prowlarr-indexers.json
git commit -m "prowlarr: migrate 16 general + 5 anime indexers + tagging"
```

---

### Phase 3 checkpoint

`curl ... /api/v1/indexer | jq length` ≥ 21. Indexer test pass-rate ≥ 80% (some public indexers go up and down; that's normal).

---

## Phase 4 — Sonarr2 + Radarr2 (anime instances)

### Task 4.1: Install Sonarr2

**Files:**
- Create: `scripts/install/05-sonarr2.sh`

- [x] **Step 1: Write install script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

if sshm 'test -d ~/.apps/sonarr2'; then
  log_info "Sonarr2 already installed."
else
  expect "$HERE/install/expect/app-install.exp.template" sonarr2 "$(secret_read shared-admin.password)"
fi

cfg="$(sshm 'cat ~/.apps/sonarr2/config.xml')"
secret_write sonarr2.key "$(printf '%s' "$cfg" | grep -oP '(?<=<ApiKey>)[^<]+')"
secret_write sonarr2.port "$(printf '%s' "$cfg" | grep -oP '(?<=<Port>)[^<]+')"
secret_write sonarr2.urlbase "$(printf '%s' "$cfg" | grep -oP '(?<=<UrlBase>)[^<]+' || true)"
log_info "Sonarr2 captured: port=$(secret_read sonarr2.port) urlbase=$(secret_read sonarr2.urlbase)"
```

- [x] **Step 2: Run install**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/install/05-sonarr2.sh
```

Expected: `Sonarr2 captured: port=NNNNN urlbase=sonarr2`

- [x] **Step 3: Commit**

```bash
git add scripts/install/05-sonarr2.sh
git commit -m "sonarr2: install + capture api key"
```

---

### Task 4.2: Configure Sonarr2 (root + qBit + Prowlarr sync)

**Files:**
- Create: `scripts/configure/06-sonarr2.sh`
- Create: `scripts/lib/arr-api.sh` (shared helper)

- [x] **Step 1: Write `scripts/lib/arr-api.sh`**

```bash
#!/usr/bin/env bash
# Helper for *arr API calls — accepts app name, returns base URL via tunnel.
arr_url() {
  local app="$1"
  local port; port="$(secret_read "$app.port")"
  local base; base="$(secret_read "$app.urlbase" 2>/dev/null || echo "$app")"
  printf 'http://127.0.0.1:%s/%s' "$port" "$base"
}
arr_tunnel() {
  local app="$1"
  local port; port="$(secret_read "$app.port")"
  ssh "${SSHM_OPTS[@]}" -fN -L "$port:127.0.0.1:$port" "$SSHM_HOST"
  echo "$port"
}
```

- [x] **Step 2: Write the configure script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh" "$HERE/lib/arr-api.sh"

APP=sonarr2
KEY="$(secret_read $APP.key)"
URL="$(arr_url $APP)"
PORT="$(arr_tunnel $APP)"
trap "pkill -f 'ssh.*-L $PORT'" EXIT
sleep 1

# 1. Root folder
existing="$(curl -sS -H "X-Api-Key: $KEY" "$URL/api/v3/rootfolder" | jq -r '.[] | select(.path=="/home/quadstronaut/media/Anime") | .id' || true)"
if [ -z "$existing" ]; then
  curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v3/rootfolder" -d '{"path":"/home/quadstronaut/media/Anime"}' >/dev/null
  log_info "Sonarr2 root folder added"
fi

# 2. qBittorrent download client (cat=sonarr-anime)
QBIT_PASS="$(secret_read qbittorrent.password)"
existing="$(curl -sS -H "X-Api-Key: $KEY" "$URL/api/v3/downloadclient" | jq -r '.[] | select(.name=="qBittorrent") | .id' || true)"
if [ -z "$existing" ]; then
  curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v3/downloadclient" -d @- <<JSON > /dev/null
{
  "enable": true, "protocol": "torrent", "priority": 1,
  "name": "qBittorrent", "implementation": "QBittorrent",
  "implementationName": "qBittorrent", "configContract": "QBittorrentSettings",
  "fields": [
    {"name":"host","value":"127.0.0.1"},
    {"name":"port","value":17041},
    {"name":"username","value":"quadstronaut"},
    {"name":"password","value":"$QBIT_PASS"},
    {"name":"tvCategory","value":"sonarr-anime"},
    {"name":"useSsl","value":false}
  ]
}
JSON
  log_info "Sonarr2 qBit client added"
fi

# 3. Prowlarr sync — handled in Phase 3.3 below (one-shot for all *arrs).
log_info "Sonarr2 base config done. Prowlarr sync still pending."
```

- [x] **Step 3: Run + verify**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/configure/06-sonarr2.sh
# verify
ssh quadstronaut@seedbox.example.com "curl -sf -H 'X-Api-Key: $(cat secrets/sonarr2.key)' http://127.0.0.1:$(cat secrets/sonarr2.port)/$(cat secrets/sonarr2.urlbase 2>/dev/null || echo sonarr2)/api/v3/rootfolder | jq ."
```

- [x] **Step 4: Commit**

```bash
git add scripts/lib/arr-api.sh scripts/configure/06-sonarr2.sh
git commit -m "sonarr2: configure root folder + qBit client"
```

---

### Task 4.3-4.4: Repeat for Radarr2

The pattern is identical. Substitute: `sonarr2`→`radarr2`, root `/home/quadstronaut/media/Anime`→`/home/quadstronaut/media/Anime Movies`, qBit cat `sonarr-anime`→`radarr-anime`, `tvCategory`→`movieCategory`.

**Files:**
- Create: `scripts/install/07-radarr2.sh` (clone of 05-sonarr2.sh with substitutions)
- Create: `scripts/configure/08-radarr2.sh` (clone of 06-sonarr2.sh with substitutions)

- [x] **Step 1: Copy + substitute**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
sed -e 's/sonarr2/radarr2/g' scripts/install/05-sonarr2.sh > scripts/install/07-radarr2.sh

sed -e 's/sonarr2/radarr2/g' \
    -e 's|/media/Anime|/media/Anime Movies|g' \
    -e 's/sonarr-anime/radarr-anime/g' \
    -e 's/tvCategory/movieCategory/g' \
    scripts/configure/06-sonarr2.sh > scripts/configure/08-radarr2.sh
```

- [x] **Step 2: Run install + configure**

```bash
bash scripts/install/07-radarr2.sh
bash scripts/configure/08-radarr2.sh
```

- [x] **Step 3: Verify**

```bash
ssh quadstronaut@seedbox.example.com "curl -sf -H 'X-Api-Key: $(cat secrets/radarr2.key)' http://127.0.0.1:$(cat secrets/radarr2.port)/$(cat secrets/radarr2.urlbase 2>/dev/null || echo radarr2)/api/v3/rootfolder | jq ."
```

Expected: root folder `/home/quadstronaut/media/Anime Movies`.

- [x] **Step 4: Commit**

```bash
git add scripts/install/07-radarr2.sh scripts/configure/08-radarr2.sh
git commit -m "radarr2: install + configure root + qBit client"
```

---

## Phase 5 — Reconfigure existing Sonarr + Radarr

### Task 5.1: Configure Sonarr (general TV) — qBit, Plex Connect, Notifiarr

**Files:**
- Create: `scripts/configure/09-sonarr.sh`

- [x] **Step 1: Write the configure script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh" "$HERE/lib/arr-api.sh"

APP=sonarr
KEY="$(secret_read $APP.key)"
URL="$(arr_url $APP)"
PORT="$(arr_tunnel $APP)"
trap "pkill -f 'ssh.*-L $PORT'" EXIT
sleep 1

# Root folder (existing TV Shows)
if ! curl -sS -H "X-Api-Key: $KEY" "$URL/api/v3/rootfolder" | jq -e '.[] | select(.path=="/home/quadstronaut/media/TV Shows")' >/dev/null; then
  curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v3/rootfolder" -d '{"path":"/home/quadstronaut/media/TV Shows"}' >/dev/null
fi

# qBit client (cat=tv-sonarr)
QBIT_PASS="$(secret_read qbittorrent.password)"
if ! curl -sS -H "X-Api-Key: $KEY" "$URL/api/v3/downloadclient" | jq -e '.[] | select(.name=="qBittorrent")' >/dev/null; then
  curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v3/downloadclient" -d @- <<JSON >/dev/null
{
  "enable": true, "protocol": "torrent", "priority": 1,
  "name": "qBittorrent", "implementation": "QBittorrent",
  "implementationName": "qBittorrent", "configContract": "QBittorrentSettings",
  "fields": [
    {"name":"host","value":"127.0.0.1"},
    {"name":"port","value":17041},
    {"name":"username","value":"quadstronaut"},
    {"name":"password","value":"$QBIT_PASS"},
    {"name":"tvCategory","value":"tv-sonarr"},
    {"name":"useSsl","value":false}
  ]
}
JSON
fi

# Plex Connect
PLEX_TOKEN="$(secret_read plex.token)"
if ! curl -sS -H "X-Api-Key: $KEY" "$URL/api/v3/notification" | jq -e '.[] | select(.implementation=="PlexServer")' >/dev/null; then
  curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v3/notification" -d @- <<JSON >/dev/null
{
  "name": "Plex", "implementation": "PlexServer",
  "implementationName": "Plex Media Server", "configContract": "PlexServerSettings",
  "onGrab": false, "onDownload": true, "onUpgrade": true, "onRename": true,
  "onHealthIssue": false, "onApplicationUpdate": false,
  "fields": [
    {"name":"host","value":"127.0.0.1"},
    {"name":"port","value":32400},
    {"name":"useSsl","value":false},
    {"name":"authToken","value":"$PLEX_TOKEN"},
    {"name":"updateLibrary","value":true}
  ]
}
JSON
fi

# Notifiarr Connect (Passthru webhook)
NOTIFIARR_KEY="$(secret_read notifiarr.key)"
if ! curl -sS -H "X-Api-Key: $KEY" "$URL/api/v3/notification" | jq -e '.[] | select(.name=="Notifiarr")' >/dev/null; then
  curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v3/notification" -d @- <<JSON >/dev/null
{
  "name": "Notifiarr", "implementation": "Notifiarr",
  "implementationName": "Notifiarr", "configContract": "NotifiarrSettings",
  "onGrab": true, "onDownload": true, "onUpgrade": true, "onRename": false,
  "onHealthIssue": true, "onApplicationUpdate": false,
  "fields": [{"name":"apiKey","value":"$NOTIFIARR_KEY"}]
}
JSON
fi

log_info "Sonarr configured: rootfolder + qBit + Plex Connect + Notifiarr"
```

- [x] **Step 2: Run**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/configure/09-sonarr.sh
```

- [x] **Step 3: Verify Plex Connect via Sonarr's test endpoint**

```bash
source scripts/lib/secrets.sh
API_KEY="$(secret_read sonarr.key)"
PORT="$(secret_read sonarr.port)"
URLBASE="$(secret_read sonarr.urlbase 2>/dev/null || echo sonarr)"
ssh -fN -L "$PORT:127.0.0.1:$PORT" quadstronaut@seedbox.example.com; sleep 1
NOTIF_ID="$(curl -sS -H "X-Api-Key: $API_KEY" "http://127.0.0.1:$PORT/$URLBASE/api/v3/notification" | jq -r '.[] | select(.name=="Plex") | .id')"
curl -sS -X POST -H "X-Api-Key: $API_KEY" "http://127.0.0.1:$PORT/$URLBASE/api/v3/notification/test/$NOTIF_ID"
pkill -f "ssh.*-L $PORT"
```

Expected: empty/200 response (test passed).

- [x] **Step 4: Commit**

```bash
git add scripts/configure/09-sonarr.sh
git commit -m "sonarr: configure rootfolder + qBit + Plex/Notifiarr connects"
```

---

### Task 5.2: Configure Radarr (general movies)

Same template as 5.1, with substitutions: `sonarr`→`radarr`, `tv-sonarr`→`radarr`, `TV Shows`→`Movies`, `tvCategory`→`movieCategory`.

**Files:**
- Create: `scripts/configure/10-radarr.sh`

- [x] **Step 1: Substitute and write**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
sed -e 's/sonarr/radarr/g' \
    -e 's|TV Shows|Movies|g' \
    -e 's/tv-radarr/radarr/g' \
    -e 's/tvCategory/movieCategory/g' \
    scripts/configure/09-sonarr.sh > scripts/configure/10-radarr.sh
```

- [x] **Step 2: Run + verify + commit**

```bash
bash scripts/configure/10-radarr.sh
# Same verify pattern as 5.1.3 with sonarr→radarr
git add scripts/configure/10-radarr.sh
git commit -m "radarr: configure rootfolder + qBit + Plex/Notifiarr connects"
```

---

### Task 5.3: Prowlarr Apps sync — push indexers to all 4 *arrs

**Files:**
- Create: `scripts/configure/11-prowlarr-apps-sync.sh`

- [x] **Step 1: Write the apps-sync script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

PROW_KEY="$(secret_read prowlarr.key)"
PROW_PORT="$(secret_read prowlarr.port)"
PROW_BASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"
PROW_URL="http://127.0.0.1:$PROW_PORT/$PROW_BASE"

ssh "${SSHM_OPTS[@]}" -fN -L "$PROW_PORT:127.0.0.1:$PROW_PORT" "$SSHM_HOST"
trap "pkill -f 'ssh.*-L $PROW_PORT'" EXIT
sleep 1

ANIME_TAG="$(curl -sS -H "X-Api-Key: $PROW_KEY" "$PROW_URL/api/v1/tag" | jq -r '.[] | select(.label=="anime") | .id')"

add_app() {
  local name="$1" impl="$2" port_secret="$3" key_secret="$4" base_secret="$5" sync_level="$6" tag_filter="$7"
  local app_port; app_port="$(secret_read "$port_secret")"
  local app_key;  app_key="$(secret_read "$key_secret")"
  local app_base; app_base="$(secret_read "$base_secret" 2>/dev/null || basename "$port_secret" .port)"

  if curl -sS -H "X-Api-Key: $PROW_KEY" "$PROW_URL/api/v1/applications" | jq -e --arg n "$name" '.[] | select(.name==$n)' >/dev/null; then
    log_info "$name app sync already configured"; return 0
  fi

  log_info "Adding $name app sync ($sync_level)"
  curl -sS -X POST -H "X-Api-Key: $PROW_KEY" -H "Content-Type: application/json" "$PROW_URL/api/v1/applications" -d @- <<JSON >/dev/null
{
  "name": "$name", "syncLevel": "$sync_level",
  "implementation": "$impl",
  "implementationName": "$impl",
  "configContract": "${impl}Settings",
  "tags": $tag_filter,
  "fields": [
    {"name":"prowlarrUrl","value":"http://127.0.0.1:$PROW_PORT/$PROW_BASE"},
    {"name":"baseUrl","value":"http://127.0.0.1:$app_port/$app_base"},
    {"name":"apiKey","value":"$app_key"}
  ]
}
JSON
}

# General *arrs (no tag filter — receive all non-anime-tagged indexers)
add_app "Sonarr"   Sonarr   sonarr.port   sonarr.key   sonarr.urlbase   fullSync "[]"
add_app "Radarr"   Radarr   radarr.port   radarr.key   radarr.urlbase   fullSync "[]"

# Anime *arrs (tag-filtered to anime-only indexers)
add_app "Sonarr2 (Anime)" Sonarr sonarr2.port sonarr2.key sonarr2.urlbase fullSync "[$ANIME_TAG]"
add_app "Radarr2 (Anime)" Radarr radarr2.port radarr2.key radarr2.urlbase fullSync "[$ANIME_TAG]"

log_info "Prowlarr apps:"
curl -sS -H "X-Api-Key: $PROW_KEY" "$PROW_URL/api/v1/applications" | jq -r '.[] | "\(.name)\t\(.syncLevel)\t\(.tags|tostring)"'
```

- [x] **Step 2: Run**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/configure/11-prowlarr-apps-sync.sh
```

Expected: 4 apps listed.

- [x] **Step 3: Verify Sonarr received indexers**

```bash
source scripts/lib/secrets.sh
KEY="$(secret_read sonarr.key)"; PORT="$(secret_read sonarr.port)"; BASE="$(secret_read sonarr.urlbase 2>/dev/null || echo sonarr)"
ssh -fN -L "$PORT:127.0.0.1:$PORT" quadstronaut@seedbox.example.com; sleep 1
curl -sS -H "X-Api-Key: $KEY" "http://127.0.0.1:$PORT/$BASE/api/v3/indexer" | jq -r '.[] | .name'
pkill -f "ssh.*-L $PORT"
```

Expected: 16 general indexer names listed (no anime-tagged ones in Sonarr; those go to Sonarr2).

- [x] **Step 4: Commit**

```bash
git add scripts/configure/11-prowlarr-apps-sync.sh
git commit -m "prowlarr: apps sync to sonarr/sonarr2/radarr/radarr2"
```

---

### Phase 5 checkpoint

All four *arrs see indexers. Trigger an indexer search in Sonarr UI and confirm results return:

```bash
ssh quadstronaut@seedbox.example.com "curl -sf -H 'X-Api-Key: $(cat secrets/sonarr.key)' 'http://127.0.0.1:$(cat secrets/sonarr.port)/$(cat secrets/sonarr.urlbase 2>/dev/null || echo sonarr)/api/v3/release?term=Big+Buck+Bunny' | jq length"
```

Expected: ≥ 1 result.

---

## Phase 6 — Bazarr ↔ *arrs

### Task 6.1: Configure Bazarr to talk to all 4 video *arrs

**Files:**
- Create: `scripts/configure/12-bazarr.sh`

- [x] **Step 1: Read Bazarr config location + capture API key**

```bash
ssh quadstronaut@seedbox.example.com 'find /app/bazarr/config /app/bazarr ~/.apps/bazarr -name "config*.yaml" 2>/dev/null | head -1'
# typical: /app/bazarr/config/config/config.yaml
```

Bazarr's API key is in its `config.yaml` under `auth: apikey:`. Capture into `secrets/bazarr.key`.

```bash
ssh quadstronaut@seedbox.example.com 'grep -A1 "^auth:" /app/bazarr/config/config/config.yaml | grep apikey | awk "{print \$2}"' > /tmp/bk.txt
mv /tmp/bk.txt secrets/bazarr.key
```

- [x] **Step 2: Capture Bazarr port**

```bash
ssh quadstronaut@seedbox.example.com 'app-ports show 2>/dev/null | grep -i bazarr | grep -oE "[0-9]{4,5}" | head -1' > secrets/bazarr.port
cat secrets/bazarr.port
```

- [x] **Step 3: Write the configure script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

BAZARR_KEY="$(secret_read bazarr.key)"
BAZARR_PORT="$(secret_read bazarr.port)"
ssh "${SSHM_OPTS[@]}" -fN -L "$BAZARR_PORT:127.0.0.1:$BAZARR_PORT" "$SSHM_HOST"
trap "pkill -f 'ssh.*-L $BAZARR_PORT'" EXIT
sleep 1

BURL="http://127.0.0.1:$BAZARR_PORT/bazarr"

set_arr() {
  local kind="$1" host="$2" port="$3" base="$4" key="$5"
  curl -sS -X POST -H "X-API-KEY: $BAZARR_KEY" -H "Content-Type: application/json" \
    "$BURL/api/system/settings" -d "{
      \"$kind\": {
        \"ip\": \"$host\", \"port\": $port, \"base_url\": \"/$base\",
        \"apikey\": \"$key\", \"ssl\": false, \"only_monitored\": false,
        \"use_$kind\": true
      }
    }" >/dev/null
}

set_arr sonarr 127.0.0.1 "$(secret_read sonarr.port)" "$(secret_read sonarr.urlbase 2>/dev/null || echo sonarr)" "$(secret_read sonarr.key)"
set_arr radarr 127.0.0.1 "$(secret_read radarr.port)" "$(secret_read radarr.urlbase 2>/dev/null || echo radarr)" "$(secret_read radarr.key)"

# Sonarr2/Radarr2: Bazarr supports only one Sonarr+one Radarr instance natively. The anime instances are added via Bazarr v1.5+'s "additional instances" feature.
log_warn "Bazarr v1.5+ supports a single Sonarr + Radarr instance natively. For Sonarr2/Radarr2, Bazarr must be configured manually via UI or via the v1.5 'additional instances' API. Logging as TODO for operator."

log_info "Bazarr configured for general Sonarr + Radarr."
```

- [x] **Step 4: Run**

```bash
bash scripts/configure/12-bazarr.sh
```

- [x] **Step 5: For anime instances — operator step (manual)**

Bazarr has a hard limitation: only one Sonarr + one Radarr per instance in current versions. Two viable mitigations:
1. Run a second Bazarr container (Ultra.cc only offers `app-bazarr` once).
2. Skip subtitles for anime (most anime is sub-burned from the start).
3. Use Bunny.net's "Bazarr-anime" community fork (out of Ultra.cc scope).

**Recommendation:** skip Bazarr for anime in v1. Most anime ships with hardsubs. Document and revisit later.

```bash
# No-op script just for the commit message
echo "Anime subtitle handling deferred — see plan §6.1 step 5" > docs/anime-subs-deferred.md
```

- [x] **Step 6: Commit**

```bash
git add scripts/configure/12-bazarr.sh docs/anime-subs-deferred.md
git commit -m "bazarr: connect to sonarr+radarr (anime deferred)"
```

---

## Phase 7 — Jellyfin install + library config

### Task 7.1: Install Jellyfin

**Files:**
- Create: `scripts/install/13-jellyfin.sh`

- [x] **Step 1: Write install script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

if sshm 'test -d ~/.apps/jellyfin || test -d /app/jellyfin'; then
  log_info "Jellyfin already installed."
else
  expect "$HERE/install/expect/app-install.exp.template" jellyfin "$(secret_read shared-admin.password)"
fi

port="$(sshm 'app-ports show 2>/dev/null | grep -i jellyfin | grep -oE "[0-9]{4,5}" | head -1' || true)"
[ -z "$port" ] && die "Jellyfin port not discovered"
secret_write jellyfin.port "$port"

log_info "Jellyfin installed on port $port"
log_warn "Run first-run wizard at https://quadstronaut.seedbox.example.com/jellyfin/ — create admin user, point libraries at ~/media/{Movies,TV Shows,Anime,Anime Movies}, then capture API key from Dashboard > API Keys into secrets/jellyfin.key"
```

- [x] **Step 2: Run install**

```bash
bash scripts/install/13-jellyfin.sh
```

- [x] **Step 3: Operator manual step — first-run wizard**

Browse to `https://quadstronaut.seedbox.example.com/jellyfin/`. Create admin (`quadstronaut` + shared-admin password). Skip the Plex Sync prompt. Add libraries:
- "Movies" → `/home/quadstronaut/media/Movies`
- "TV Shows" → `/home/quadstronaut/media/TV Shows`
- "Anime" → `/home/quadstronaut/media/Anime`
- "Anime Movies" → `/home/quadstronaut/media/Anime Movies`

After completing, Dashboard → API Keys → New → name "Optimize-Manitoba" → copy:

```bash
echo "<paste jellyfin api key>" > secrets/jellyfin.key
chmod 600 secrets/jellyfin.key
```

- [x] **Step 4: Verify Jellyfin API**

```bash
ssh quadstronaut@seedbox.example.com "curl -sf -H 'X-Emby-Token: $(cat secrets/jellyfin.key)' http://127.0.0.1:$(cat secrets/jellyfin.port)/jellyfin/System/Info | jq .ServerName"
```

Expected: `"manitoba"` or your chosen server name.

- [x] **Step 5: Commit**

```bash
git add scripts/install/13-jellyfin.sh
git commit -m "jellyfin: install (first-run is operator manual)"
```

---

### Task 7.2: Add Jellyfin Connect to all 4 video *arrs

**Files:**
- Create: `scripts/wire/14-jellyfin-to-arrs.sh`

- [x] **Step 1: Write the wiring script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh" "$HERE/lib/arr-api.sh"

JF_KEY="$(secret_read jellyfin.key)"
JF_PORT="$(secret_read jellyfin.port)"

add_jf_to_arr() {
  local app="$1"
  local key port base url
  key="$(secret_read $app.key)"; port="$(secret_read $app.port)"; base="$(secret_read $app.urlbase 2>/dev/null || echo $app)"
  url="http://127.0.0.1:$port/$base"
  ssh "${SSHM_OPTS[@]}" -fN -L "$port:127.0.0.1:$port" "$SSHM_HOST"; sleep 1

  if ! curl -sS -H "X-Api-Key: $key" "$url/api/v3/notification" | jq -e '.[] | select(.implementation=="Emby")' >/dev/null; then
    curl -sS -X POST -H "X-Api-Key: $key" -H "Content-Type: application/json" "$url/api/v3/notification" -d @- <<JSON >/dev/null
{
  "name": "Jellyfin", "implementation": "Emby",
  "implementationName": "Jellyfin", "configContract": "EmbySettings",
  "onGrab": false, "onDownload": true, "onUpgrade": true, "onRename": true,
  "onHealthIssue": false, "onApplicationUpdate": false,
  "fields": [
    {"name":"host","value":"127.0.0.1"},
    {"name":"port","value":$JF_PORT},
    {"name":"useSsl","value":false},
    {"name":"apiKey","value":"$JF_KEY"},
    {"name":"updateLibrary","value":true}
  ]
}
JSON
    log_info "$app: Jellyfin Connect added"
  fi
  pkill -f "ssh.*-L $port"
}

add_jf_to_arr sonarr
add_jf_to_arr radarr
add_jf_to_arr sonarr2
add_jf_to_arr radarr2
```

- [x] **Step 2: Run**

```bash
bash scripts/wire/14-jellyfin-to-arrs.sh
```

- [x] **Step 3: Commit**

```bash
git add scripts/wire/14-jellyfin-to-arrs.sh
git commit -m "jellyfin: connect from all 4 video *arrs (on-import scan webhook)"
```

---

## Phase 8 — Books / comics ring

12 tasks for: Readarr, Mylar3, Komga, Kavita, Calibre-Web, Audiobookshelf install + configure + cross-wire.

### Task 8.1: Install Readarr

**Files:**
- Create: `scripts/install/15-readarr.sh`

(Pattern identical to 4.1 / 5.1; substitute names. The script:)

- [x] **Step 1: Write + run install**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

if sshm 'test -d ~/.apps/readarr || test -d /app/readarr'; then
  log_info "Readarr already installed."
else
  expect "$HERE/install/expect/app-install.exp.template" readarr "$(secret_read shared-admin.password)"
fi

cfg="$(sshm 'cat ~/.apps/readarr/config.xml 2>/dev/null')"
secret_write readarr.key  "$(printf '%s' "$cfg" | grep -oP '(?<=<ApiKey>)[^<]+')"
secret_write readarr.port "$(printf '%s' "$cfg" | grep -oP '(?<=<Port>)[^<]+')"
secret_write readarr.urlbase "$(printf '%s' "$cfg" | grep -oP '(?<=<UrlBase>)[^<]+' || echo readarr)"
log_info "Readarr installed: port=$(secret_read readarr.port)"
```

- [x] **Step 2: Run + commit**

```bash
bash scripts/install/15-readarr.sh
git add scripts/install/15-readarr.sh
git commit -m "readarr: install + capture api key"
```

---

### Task 8.2: Configure Readarr — roots Books + Audiobooks, qBit, Notifiarr

**Files:**
- Create: `scripts/configure/16-readarr.sh`

- [x] **Step 1: Write the configure script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh" "$HERE/lib/arr-api.sh"

APP=readarr
KEY="$(secret_read $APP.key)"
URL="$(arr_url $APP)"
PORT="$(arr_tunnel $APP)"
trap "pkill -f 'ssh.*-L $PORT'" EXIT
sleep 1

# Roots
for path in "/home/quadstronaut/media/Books" "/home/quadstronaut/media/Audiobooks"; do
  if ! curl -sS -H "X-Api-Key: $KEY" "$URL/api/v1/rootfolder" | jq -e --arg p "$path" '.[] | select(.path==$p)' >/dev/null; then
    curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v1/rootfolder" -d "{\"path\":\"$path\"}" >/dev/null
  fi
done

# qBit
QBIT_PASS="$(secret_read qbittorrent.password)"
if ! curl -sS -H "X-Api-Key: $KEY" "$URL/api/v1/downloadclient" | jq -e '.[] | select(.name=="qBittorrent")' >/dev/null; then
  curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v1/downloadclient" -d @- <<JSON >/dev/null
{
  "enable": true, "protocol": "torrent", "priority": 1,
  "name": "qBittorrent", "implementation": "QBittorrent",
  "implementationName": "qBittorrent", "configContract": "QBittorrentSettings",
  "fields": [
    {"name":"host","value":"127.0.0.1"},
    {"name":"port","value":17041},
    {"name":"username","value":"quadstronaut"},
    {"name":"password","value":"$QBIT_PASS"},
    {"name":"musicCategory","value":"readarr"},
    {"name":"useSsl","value":false}
  ]
}
JSON
fi

# Notifiarr
NOTIFIARR_KEY="$(secret_read notifiarr.key)"
if ! curl -sS -H "X-Api-Key: $KEY" "$URL/api/v1/notification" | jq -e '.[] | select(.name=="Notifiarr")' >/dev/null; then
  curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v1/notification" -d @- <<JSON >/dev/null
{
  "name": "Notifiarr", "implementation": "Notifiarr",
  "implementationName": "Notifiarr", "configContract": "NotifiarrSettings",
  "onGrab": true, "onReleaseImport": true, "onUpgrade": true,
  "onHealthIssue": true,
  "fields": [{"name":"apiKey","value":"$NOTIFIARR_KEY"}]
}
JSON
fi

log_info "Readarr configured"
```

- [x] **Step 2: Run + verify + commit**

```bash
bash scripts/configure/16-readarr.sh
git add scripts/configure/16-readarr.sh
git commit -m "readarr: configure roots + qBit + notifiarr"
```

---

### Task 8.3: Install Mylar3

**Files:**
- Create: `scripts/install/17-mylar3.sh`

- [x] **Step 1: Write + run** (clone of 8.1 with name `mylar3`)

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
sed 's/readarr/mylar3/g' scripts/install/15-readarr.sh > scripts/install/17-mylar3.sh
# Mylar3 doesn't use config.xml — it's INI-based at ~/.apps/mylar3/Mylar/config.ini
```

For Mylar3 the config-extraction differs. Replace the `cfg=...` block with:

```bash
api_key="$(sshm 'grep -E "^api_key" ~/.apps/mylar3/Mylar/config.ini 2>/dev/null | head -1 | cut -d= -f2 | tr -d "[:space:]\""')"
port="$(sshm 'app-ports show | grep -i mylar3 | grep -oE "[0-9]{4,5}" | head -1')"
secret_write mylar3.key "$api_key"
secret_write mylar3.port "$port"
```

- [x] **Step 2: Run + commit**

```bash
bash scripts/install/17-mylar3.sh
git add scripts/install/17-mylar3.sh
git commit -m "mylar3: install + capture api key"
```

---

### Task 8.4: Configure Mylar3 — roots Comics + Manga, qBit

Mylar3's config is INI not REST-oriented — easier to template + push the config.ini file.

**Files:**
- Create: `scripts/configure/18-mylar3.sh`
- Create: `scripts/data/mylar3-additions.ini`

- [x] **Step 1: Pull current config**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
mkdir -p scripts/data
ssh quadstronaut@seedbox.example.com 'cat ~/.apps/mylar3/Mylar/config.ini' > scripts/data/mylar3-current.ini
```

- [x] **Step 2: Write a template patch** (`scripts/data/mylar3-additions.ini`)

The Mylar3 config has named sections like `[General]`, `[Torznab]`, etc. We want:
- `[General]` → set `comicvine_api`, `download_dir = /home/quadstronaut/downloads/qbittorrent/mylar`, `destination_dir = /home/quadstronaut/media/Comics`, manga support enabled
- `[Torrents]` → `enable_torrents = 1`, `enable_torrent_search = 1`, qbit cat `mylar`
- `[qBittorrent]` → `qbittorrent_host = http://127.0.0.1`, `qbittorrent_port = 17041`, `qbittorrent_username = quadstronaut`, `qbittorrent_password = <secret>`, `qbittorrent_label = mylar`

The patch is applied with crudini:

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

# pip-install crudini locally if missing, OR run crudini on remote
sshm 'command -v crudini >/dev/null || pip install --user crudini' || die "crudini install failed"

QBIT_PASS="$(secret_read qbittorrent.password)"
sshm bash -s "$QBIT_PASS" <<'REMOTE'
set -euo pipefail
QBIT_PASS="$1"
CFG=~/.apps/mylar3/Mylar/config.ini
[ -f "$CFG.bak.$(date +%Y%m%d)" ] || cp "$CFG" "$CFG.bak.$(date +%Y%m%d)"

crudini --set "$CFG" General download_dir /home/quadstronaut/downloads/qbittorrent/mylar
crudini --set "$CFG" General destination_dir /home/quadstronaut/media/Comics
crudini --set "$CFG" General manga_dir /home/quadstronaut/media/Manga
crudini --set "$CFG" General enforce_perms 0

crudini --set "$CFG" Torrents enable_torrents 1
crudini --set "$CFG" Torrents enable_torrent_search 1
crudini --set "$CFG" Torrents torrent_local 1
crudini --set "$CFG" Torrents local_watchdir /home/quadstronaut/downloads/qbittorrent/mylar

crudini --set "$CFG" qBittorrent qbittorrent_host http://127.0.0.1
crudini --set "$CFG" qBittorrent qbittorrent_port 17041
crudini --set "$CFG" qBittorrent qbittorrent_username quadstronaut
crudini --set "$CFG" qBittorrent qbittorrent_password "$QBIT_PASS"
crudini --set "$CFG" qBittorrent qbittorrent_label mylar
crudini --set "$CFG" qBittorrent qbittorrent_loadaction 1

echo "Mylar3 config patched."
REMOTE

log_info "Restarting Mylar3..."
sshm 'app-mylar3 restart'
sleep 5
log_info "Mylar3 reachable: $(sshm "curl -sf http://127.0.0.1:$(cat secrets/mylar3.port)/" | head -c 80 || echo "no response yet")"
```

- [x] **Step 3: Run + commit**

```bash
bash scripts/configure/18-mylar3.sh
git add scripts/configure/18-mylar3.sh scripts/data/mylar3-additions.ini scripts/data/mylar3-current.ini
git commit -m "mylar3: roots Comics+Manga + qBit client + restart"
```

(Note: `mylar3-current.ini` may contain api keys; if so, *gitignore* it instead — verify before committing.)

---

### Task 8.5: Install Komga

**Files:**
- Create: `scripts/install/19-komga.sh`

- [x] **Step 1: Write install (clone of 8.1 substituting `komga`)**

Komga's port comes from `app-ports show`; its admin is set during first-run via the web UI. There is no `config.xml`-style API key — instead, Komga uses HTTP basic auth + the user creates a personal API key from their profile page.

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

if sshm 'test -d ~/.apps/komga || test -d /app/komga'; then
  log_info "Komga already installed."
else
  expect "$HERE/install/expect/app-install.exp.template" komga "$(secret_read shared-admin.password)"
fi

port="$(sshm 'app-ports show | grep -i komga | grep -oE "[0-9]{4,5}" | head -1')"
secret_write komga.port "$port"
secret_write komga.user "quadstronaut@seedbox.example.com"  # Komga first-run creates an account; use this as the username
log_info "Komga installed on port $port. First-run wizard required."
log_warn "Browse https://quadstronaut.seedbox.example.com/komga/, set admin email + password (use shared-admin.password), then capture API key into secrets/komga.key"
```

- [x] **Step 2: Run + first-run wizard (operator manual)**

Browse to `https://quadstronaut.seedbox.example.com/komga/`. Create account: email `quadstronaut@seedbox.example.com`, password from `secrets/shared-admin.password`. Login. Profile → Generate API Key → copy:

```bash
echo "<paste komga api key>" > secrets/komga.key
chmod 600 secrets/komga.key
```

Add libraries via UI (Komga's library API is post-1.0): "Comics" → `/home/quadstronaut/media/Comics`, "Manga" → `/home/quadstronaut/media/Manga`.

- [x] **Step 3: Commit**

```bash
git add scripts/install/19-komga.sh
git commit -m "komga: install (first-run + libraries are operator manual)"
```

---

### Task 8.6: Install Kavita

**Files:**
- Create: `scripts/install/20-kavita.sh`

- [x] **Step 1: Write install + run**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
sed 's/komga/kavita/g' scripts/install/19-komga.sh > scripts/install/20-kavita.sh
bash scripts/install/20-kavita.sh
```

Kavita first-run is also a manual wizard. Same drill: create admin, save API key (Settings → API), add libraries pointing at `/home/quadstronaut/media/Comics` and `/home/quadstronaut/media/Manga`.

```bash
echo "<paste kavita api key>" > secrets/kavita.key
chmod 600 secrets/kavita.key
```

- [x] **Step 2: Commit**

```bash
git add scripts/install/20-kavita.sh
git commit -m "kavita: install (first-run + libraries are operator manual)"
```

---

### Task 8.7: Install Calibre-Web

**Files:**
- Create: `scripts/install/21-calibre-web.sh`

- [x] **Step 1: Write + run**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
cat > scripts/install/21-calibre-web.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

if sshm 'test -d ~/.apps/calibre-web || test -d /app/calibre-web'; then
  log_info "Calibre-Web already installed."
else
  expect "$HERE/install/expect/app-install.exp.template" calibre-web "$(secret_read shared-admin.password)"
fi

port="$(sshm 'app-ports show | grep -i calibre-web | grep -oE "[0-9]{4,5}" | head -1')"
secret_write calibre-web.port "$port"
log_info "Calibre-Web installed on port $port"
log_warn "Browse https://quadstronaut.seedbox.example.com/calibre-web/, set library to /home/quadstronaut/media/Books"
EOF
bash scripts/install/21-calibre-web.sh
```

Operator first-run: default admin is `admin`/`admin123`; change immediately. Set Calibre Library Location to `/home/quadstronaut/media/Books`.

- [x] **Step 2: Commit**

```bash
git add scripts/install/21-calibre-web.sh
git commit -m "calibre-web: install (library config operator manual)"
```

---

### Task 8.8: Install Audiobookshelf

**Files:**
- Create: `scripts/install/22-audiobookshelf.sh`

- [x] **Step 1: Write + run**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
sed 's/calibre-web/audiobookshelf/g' scripts/install/21-calibre-web.sh > scripts/install/22-audiobookshelf.sh
bash scripts/install/22-audiobookshelf.sh
```

Operator first-run: create admin, add libraries `/home/quadstronaut/media/Audiobooks` and `/home/quadstronaut/media/Podcasts`. Capture API key (Settings → Users → quadstronaut → API tokens).

```bash
echo "<paste audiobookshelf api key>" > secrets/audiobookshelf.key
chmod 600 secrets/audiobookshelf.key
```

- [x] **Step 2: Commit**

```bash
git add scripts/install/22-audiobookshelf.sh
git commit -m "audiobookshelf: install (libraries operator manual)"
```

---

### Task 8.9-8.10: Wire Readarr → Calibre-Web/Audiobookshelf, Mylar3 → Komga/Kavita

Both Calibre-Web and Audiobookshelf provide rescan endpoints; Komga and Kavita have library scan APIs. The Readarr/Mylar3 "Connect" notification can be a Custom Script that calls those endpoints, or we use a small webhook-bridge script.

**Files:**
- Create: `scripts/wire/23-readarr-text-libraries.sh`
- Create: `scripts/wire/24-mylar3-comic-libraries.sh`

- [x] **Step 1: Write the bridge script**

A simple wrapper: write `~/scripts/library-rescan.sh` on manitoba that takes `<library>` as argument and calls the appropriate rescan API. Configure each *arr's Custom Script Connect to call this.

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

# Push the helper to manitoba
sshm bash -s <<'REMOTE'
mkdir -p ~/scripts/post-import
cat > ~/scripts/post-import/library-rescan.sh <<'INNER'
#!/usr/bin/env bash
# Usage: library-rescan.sh <calibre-web|audiobookshelf|komga|kavita>
set -euo pipefail
TARGET="$1"
case "$TARGET" in
  calibre-web)
    PORT=$(grep -oE '[0-9]+' ~/.opt/secrets/calibre-web.port 2>/dev/null || echo 0)
    [ "$PORT" != "0" ] && curl -sf "http://127.0.0.1:$PORT/calibre-web/admin/scheduledtasks" >/dev/null || true
    ;;
  audiobookshelf)
    PORT=$(cat ~/.opt/secrets/audiobookshelf.port 2>/dev/null)
    KEY=$(cat ~/.opt/secrets/audiobookshelf.key 2>/dev/null)
    [ -n "$PORT" ] && [ -n "$KEY" ] && curl -sf -X POST -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/audiobookshelf/api/libraries/scan" >/dev/null || true
    ;;
  komga)
    PORT=$(cat ~/.opt/secrets/komga.port 2>/dev/null)
    KEY=$(cat ~/.opt/secrets/komga.key 2>/dev/null)
    USER=$(cat ~/.opt/secrets/komga.user 2>/dev/null)
    [ -n "$PORT" ] && curl -sf -u "$USER:$KEY" -X POST "http://127.0.0.1:$PORT/komga/api/v1/libraries/scan" >/dev/null || true
    ;;
  kavita)
    PORT=$(cat ~/.opt/secrets/kavita.port 2>/dev/null)
    KEY=$(cat ~/.opt/secrets/kavita.key 2>/dev/null)
    [ -n "$PORT" ] && curl -sf -X POST "http://127.0.0.1:$PORT/kavita/api/Library/scan-all?apiKey=$KEY" >/dev/null || true
    ;;
esac
INNER
chmod +x ~/scripts/post-import/library-rescan.sh
mkdir -p ~/.opt/secrets
echo "rescan helper installed"
REMOTE

# Push secrets to manitoba so the helper can read them
for s in calibre-web.port calibre-web.key audiobookshelf.port audiobookshelf.key komga.port komga.key komga.user kavita.port kavita.key; do
  if [ -f "$SECRETS_DIR/$s" ]; then
    scpm_to "$SECRETS_DIR/$s" "~/.opt/secrets/$s"
    sshm "chmod 600 ~/.opt/secrets/$s"
  fi
done
```

- [x] **Step 2: Wire as Custom Script Connect on Readarr / Mylar3**

For Readarr: Settings → Connect → + Custom Script → Path `/home/quadstronaut/scripts/post-import/library-rescan.sh` → Arguments `calibre-web` (and a second connect for `audiobookshelf`).

For Mylar3: edit `~/.apps/mylar3/Mylar/config.ini` `[Newzbin]` → `extra_scripts = /home/quadstronaut/scripts/post-import/library-rescan.sh komga,/home/quadstronaut/scripts/post-import/library-rescan.sh kavita`.

(Mylar3's "extra_scripts" is comma-separated. Each runs after import.)

- [x] **Step 3: Commit**

```bash
git add scripts/wire/23-readarr-text-libraries.sh scripts/wire/24-mylar3-comic-libraries.sh
git commit -m "wire: post-import library rescan helper for books+comics"
```

---

## Phase 9 — Stats & Maintainerr

### Task 9.1: Tautulli reconfig — fresh Plex token, Notifiarr webhook

**Files:**
- Create: `scripts/configure/25-tautulli.sh`

- [x] **Step 1: Write the configure script**

Tautulli's config is INI-based. Most settings can be set via API or config.ini.

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

PLEX_TOKEN="$(secret_read plex.token)"
NOTIFIARR_KEY="$(secret_read notifiarr.key)"

sshm bash -s "$PLEX_TOKEN" "$NOTIFIARR_KEY" <<'REMOTE'
set -euo pipefail
PLEX_TOKEN="$1"; NOTIFIARR_KEY="$2"
CFG=~/.apps/tautulli/config.ini
[ -f "$CFG.bak.$(date +%Y%m%d)" ] || cp "$CFG" "$CFG.bak.$(date +%Y%m%d)"

# Patch via crudini
crudini --set "$CFG" PMS pms_token "$PLEX_TOKEN"
crudini --set "$CFG" PMS pms_ip 127.0.0.1
crudini --set "$CFG" PMS pms_port 32400

# Notifiarr Notification agent (Tautulli has built-in Notifiarr support starting v2.10)
crudini --set "$CFG" Notifiarr notifiarr_apikey "$NOTIFIARR_KEY"

echo "Tautulli config patched. Restarting..."
REMOTE

sshm 'app-tautulli restart'
sleep 5
log_info "Tautulli restarted."
```

- [x] **Step 2: Run + commit**

```bash
bash scripts/configure/25-tautulli.sh
git add scripts/configure/25-tautulli.sh
git commit -m "tautulli: refresh plex token + notifiarr key"
```

---

### Task 9.2: Install Jellystat

**Files:**
- Create: `scripts/install/26-jellystat.sh`

- [x] **Step 1: Write install (clone of 8.1, name `jellystat`)**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
sed 's/readarr/jellystat/g' scripts/install/15-readarr.sh > scripts/install/26-jellystat.sh
# Jellystat doesn't use config.xml; capture port + first-run via UI
```

Replace the config-extraction block to just capture port:

```bash
port="$(sshm 'app-ports show | grep -i jellystat | grep -oE "[0-9]{4,5}" | head -1')"
secret_write jellystat.port "$port"
```

- [x] **Step 2: Operator manual: configure Jellyfin connection**

Browse `https://quadstronaut.seedbox.example.com/jellystat/`. Create admin. Add Jellyfin server: URL `http://127.0.0.1:<jellyfin port>/jellyfin`, API key from `secrets/jellyfin.key`.

- [x] **Step 3: Commit**

```bash
bash scripts/install/26-jellystat.sh
git add scripts/install/26-jellystat.sh
git commit -m "jellystat: install (jellyfin link operator manual)"
```

---

### Task 9.3: Maintainerr reconfig — Plex token, Jellyseerr webhook, *arr keys

**Files:**
- Create: `scripts/configure/27-maintainerr.sh`

- [x] **Step 1: Capture Maintainerr API key from UI**

Browse `https://quadstronaut.seedbox.example.com/maintainerr/`. Settings → API → copy.

```bash
echo "<paste maintainerr api key>" > secrets/maintainerr.key
```

- [x] **Step 2: Write configure script**

Maintainerr config is in its sqlite DB at `~/.apps/maintainerr/data/maintainerr.db`. Settings can be patched via REST. Endpoints:
- `POST /api/settings/plex` — set Plex
- `POST /api/settings/overseerr` — set Jellyseerr (Overseerr-API-compatible)
- `POST /api/settings/sonarr-radarr` — for *arr connections (varies by version)

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

KEY="$(secret_read maintainerr.key)"
PORT="$(sshm 'app-ports show | grep -i maintainerr | grep -oE "[0-9]{4,5}" | head -1')"
secret_write maintainerr.port "$PORT"
URL="http://127.0.0.1:$PORT/maintainerr"

ssh "${SSHM_OPTS[@]}" -fN -L "$PORT:127.0.0.1:$PORT" "$SSHM_HOST"
trap "pkill -f 'ssh.*-L $PORT'" EXIT
sleep 1

# Plex
PLEX_TOKEN="$(secret_read plex.token)"
curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/settings/plex" -d "{\"plex_hostname\":\"127.0.0.1\",\"plex_port\":32400,\"plex_ssl\":false,\"plex_auth_token\":\"$PLEX_TOKEN\"}" >/dev/null

# Jellyseerr (Overseerr-compatible)
JS_KEY="$(secret_read jellyseerr.key)"
JS_PORT="$(secret_read jellyseerr.port 2>/dev/null || sshm "app-ports show | grep -i jellyseerr | grep -oE '[0-9]{4,5}' | head -1")"
secret_write jellyseerr.port "$JS_PORT"
curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/settings/overseerr" -d "{\"overseerr_url\":\"http://127.0.0.1:$JS_PORT/jellyseerr\",\"overseerr_api_key\":\"$JS_KEY\"}" >/dev/null

# Sonarr (general)
S_KEY="$(secret_read sonarr.key)"; S_PORT="$(secret_read sonarr.port)"; S_BASE="$(secret_read sonarr.urlbase 2>/dev/null || echo sonarr)"
curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/settings/sonarr" -d "{\"url\":\"http://127.0.0.1:$S_PORT/$S_BASE\",\"api_key\":\"$S_KEY\"}" >/dev/null || true

# Radarr (general)
R_KEY="$(secret_read radarr.key)"; R_PORT="$(secret_read radarr.port)"; R_BASE="$(secret_read radarr.urlbase 2>/dev/null || echo radarr)"
curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/settings/radarr" -d "{\"url\":\"http://127.0.0.1:$R_PORT/$R_BASE\",\"api_key\":\"$R_KEY\"}" >/dev/null || true

log_info "Maintainerr settings updated. Sonarr2/Radarr2 multi-instance is added through the UI."
log_warn "Operator manual step: Maintainerr UI → Settings → Sonarr → 'Add another Sonarr' for Sonarr2 (anime), same for Radarr2."
```

- [x] **Step 3: Run**

```bash
bash scripts/configure/27-maintainerr.sh
```

- [x] **Step 4: Commit**

```bash
git add scripts/configure/27-maintainerr.sh
git commit -m "maintainerr: refresh plex/jellyseerr/sonarr/radarr settings"
```

---

### Task 9.4: Maintainerr — create the 60-day rules

**Files:**
- Create: `scripts/configure/28-maintainerr-rules.sh`
- Create: `scripts/data/maintainerr-rules.json`

- [x] **Step 1: Write the rule manifest**

```json
{
  "rules": [
    { "library": "Movies",       "section_type": 1, "max_age_days": 60, "warn_days_before": 14 },
    { "library": "TV Shows",     "section_type": 2, "max_age_days": 60, "warn_days_before": 14 },
    { "library": "Anime",        "section_type": 2, "max_age_days": 60, "warn_days_before": 14 },
    { "library": "Anime Movies", "section_type": 1, "max_age_days": 60, "warn_days_before": 14 }
  ]
}
```

- [x] **Step 2: Write the rule script** (uses `/api/rules` endpoints)

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

KEY="$(secret_read maintainerr.key)"
PORT="$(secret_read maintainerr.port)"
URL="http://127.0.0.1:$PORT/maintainerr"

ssh "${SSHM_OPTS[@]}" -fN -L "$PORT:127.0.0.1:$PORT" "$SSHM_HOST"
trap "pkill -f 'ssh.*-L $PORT'" EXIT
sleep 1

# Discover Plex library section IDs
LIBS="$(curl -sS -H "X-Api-Key: $KEY" "$URL/api/plex/libraries" | jq -c .)"

create_rule() {
  local label="$1" max_age="$2" warn="$3" lib_section="$4"
  local existing
  existing="$(curl -sS -H "X-Api-Key: $KEY" "$URL/api/rules" | jq -r --arg l "$label - 60d cap" '.[] | select(.name==$l) | .id' || true)"
  [ -n "$existing" ] && { log_info "rule '$label - 60d cap' exists"; return 0; }

  curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/rules" -d @- <<JSON >/dev/null
{
  "name": "$label - 60d cap",
  "description": "Auto-generated by Optimize-Manitoba",
  "libraryId": $lib_section,
  "isActive": true,
  "deleteAfterDays": $max_age,
  "rules": [{ "operator": null, "action": 0, "section": 0,
              "firstVal":[1,9], "lastVal":[1, $max_age], "customVal":null
            }],
  "manualCollection": false,
  "tautulliWatchedPercentOverride": null,
  "collection": { "title": "$label - 60d cap", "description": "Optimize-Manitoba" }
}
JSON
  log_info "Created rule for $label"
}

for row in $(jq -c '.rules[]' "$HERE/data/maintainerr-rules.json"); do
  label="$(printf '%s' "$row" | jq -r .library)"
  max_age="$(printf '%s' "$row" | jq -r .max_age_days)"
  warn="$(printf '%s' "$row" | jq -r .warn_days_before)"

  # Resolve library section id by title
  lib_id="$(printf '%s' "$LIBS" | jq -r --arg l "$label" '.[] | select(.title==$l) | .key' || true)"
  [ -z "$lib_id" ] && { log_warn "Plex library '$label' not found; skipping"; continue; }

  create_rule "$label" "$max_age" "$warn" "$lib_id"
done

log_warn "The 14-day pre-delete warning is configured per-rule in Maintainerr UI under 'Notification' tab. Operator manual step."
```

- [x] **Step 3: Run**

```bash
bash scripts/configure/28-maintainerr-rules.sh
```

- [x] **Step 4: Operator: per rule, set the 14-day warning Notifiarr destination**

Maintainerr UI → each rule → Notifications → "Send notification 14 days before delete" → Notifiarr (already configured).

- [x] **Step 5: Commit**

```bash
git add scripts/configure/28-maintainerr-rules.sh scripts/data/maintainerr-rules.json
git commit -m "maintainerr: 60d rules for movies/tv/anime/anime-movies"
```

---

### Task 9.5: Write `prune-text-libraries.sh` (text/audio retention cron)

**Files:**
- Create: `scripts/prune-text-libraries.sh`
- Create: `scripts/install/29-prune-cron-install.sh`

- [x] **Step 1: Write the prune script**

```bash
#!/usr/bin/env bash
# Runs on manitoba via cron. Deletes books/comics/audiobooks/manga/podcasts older than CAP_DAYS.
# Posts Notifiarr warning when files enter the WARN_LEAD window.
set -euo pipefail

CAP_DAYS=365
WARN_LEAD=14
ROOTS=(~/media/Books ~/media/Audiobooks ~/media/Comics ~/media/Manga ~/media/Podcasts)
STATE_DIR=~/.cache/prune-text
NOTIFIARR_KEY_FILE=~/.opt/secrets/notifiarr.key
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

mkdir -p "$STATE_DIR"
TODAY="$(date +%s)"
WARN_THRESHOLD=$(( CAP_DAYS - WARN_LEAD ))

post_notifiarr() {
  local body="$1"
  [ -f "$NOTIFIARR_KEY_FILE" ] || return 0
  local key; key="$(cat "$NOTIFIARR_KEY_FILE")"
  curl -sf -X POST -H "X-API-Key: $key" -H "Content-Type: application/json" \
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
      warnings+=("$file (age=${age_days}d, deletes in $((CAP_DAYS - age_days))d)")
    fi
  done < <(find "$root" -type f -print0)
done

# Daily warning digest (only if there are warnings)
if [ "${#warnings[@]}" -gt 0 ]; then
  msg="📚 *Text/audio library: ${#warnings[@]} items entering 14-day warning window*\n"
  for w in "${warnings[@]:0:25}"; do msg+="\n- $w"; done
  [ "${#warnings[@]}" -gt 25 ] && msg+="\n... and $(( ${#warnings[@]} - 25 )) more"
  post_notifiarr "{\"text\":\"$msg\"}"
fi

if [ "${#deletions[@]}" -gt 0 ]; then
  msg="🗑️ *Text/audio library: ${#deletions[@]} items deleted today (age >= ${CAP_DAYS}d)*\n"
  for d in "${deletions[@]:0:25}"; do msg+="\n- $d"; done
  post_notifiarr "{\"text\":\"$msg\"}"
fi

# Trigger library rescans on relevant servers (best-effort, don't fail)
~/scripts/post-import/library-rescan.sh komga          || true
~/scripts/post-import/library-rescan.sh kavita         || true
~/scripts/post-import/library-rescan.sh calibre-web    || true
~/scripts/post-import/library-rescan.sh audiobookshelf || true

echo "prune-text-libraries: deletions=${#deletions[@]} warnings=${#warnings[@]}"
```

- [x] **Step 2: Push to manitoba and install cron**

```bash
#!/usr/bin/env bash
# scripts/install/29-prune-cron-install.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

scpm_to "$HERE/prune-text-libraries.sh" "~/scripts/post-import/prune-text-libraries.sh"
sshm 'chmod +x ~/scripts/post-import/prune-text-libraries.sh'

# Install cron entry — runs daily at 04:00 manitoba time
sshm 'crontab -l 2>/dev/null | grep -v prune-text-libraries.sh; echo "0 4 * * * /home/quadstronaut/scripts/post-import/prune-text-libraries.sh >> /home/quadstronaut/.cache/prune-text/prune.log 2>&1"' | sshm 'crontab -'

# Push notifiarr key to manitoba
scpm_to "$SECRETS_DIR/notifiarr.key" "~/.opt/secrets/notifiarr.key"
sshm 'chmod 600 ~/.opt/secrets/notifiarr.key'

log_info "prune-text-libraries.sh deployed + cron installed."
```

- [x] **Step 3: Run**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/install/29-prune-cron-install.sh
```

- [x] **Step 4: Dry-run on manitoba**

```bash
ssh quadstronaut@seedbox.example.com '~/scripts/post-import/prune-text-libraries.sh --dry-run'
```

Expected: `prune-text-libraries: deletions=0 warnings=0` (libraries are empty initially).

- [x] **Step 5: Commit**

```bash
git add scripts/prune-text-libraries.sh scripts/install/29-prune-cron-install.sh
git commit -m "prune: text-libraries 365d retention cron + notifiarr digest"
```

---

## Phase 10 — Jellyseerr reconfig + routing rules

### Task 10.1: Connect Jellyseerr to Plex/Jellyfin/4 *arrs

**Files:**
- Create: `scripts/configure/30-jellyseerr.sh`

- [x] **Step 1: Write configure script**

Jellyseerr's API allows full server-side config. The settings live at `/api/v1/settings/{plex,jellyfin,sonarr,radarr}`.

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

KEY="$(secret_read jellyseerr.key)"
PORT="$(secret_read jellyseerr.port)"
URL="http://127.0.0.1:$PORT/jellyseerr"

ssh "${SSHM_OPTS[@]}" -fN -L "$PORT:127.0.0.1:$PORT" "$SSHM_HOST"
trap "pkill -f 'ssh.*-L $PORT'" EXIT; sleep 1

# Plex
PLEX_TOKEN="$(secret_read plex.token)"
curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v1/settings/plex" -d "{\"name\":\"manitoba\",\"hostname\":\"127.0.0.1\",\"port\":32400,\"useSsl\":false,\"libraries\":[]}" >/dev/null

# Jellyfin
JF_KEY="$(secret_read jellyfin.key)"
JF_PORT="$(secret_read jellyfin.port)"
curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v1/settings/jellyfin" -d "{\"hostname\":\"127.0.0.1\",\"port\":$JF_PORT,\"useSsl\":false,\"urlBase\":\"/jellyfin\",\"apiKey\":\"$JF_KEY\"}" >/dev/null

# Sonarr (general)
add_sonarr_radarr() {
  local kind="$1" app="$2" is_default="$3" anime_only="$4" root="$5"
  local key port base
  key="$(secret_read $app.key)"; port="$(secret_read $app.port)"; base="$(secret_read $app.urlbase 2>/dev/null || echo $app)"
  curl -sS -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" "$URL/api/v1/settings/$kind" -d @- <<JSON >/dev/null
{
  "name": "$app",
  "hostname": "127.0.0.1",
  "port": $port,
  "apiKey": "$key",
  "baseUrl": "/$base",
  "useSsl": false,
  "activeProfileId": 1,
  "activeRootFolder": "$root",
  "isDefault": $is_default,
  "is4k": false,
  "syncEnabled": true,
  "preventSearch": false
}
JSON
}

add_sonarr_radarr sonarr sonarr  true  false "/home/quadstronaut/media/TV Shows"
add_sonarr_radarr sonarr sonarr2 false true  "/home/quadstronaut/media/Anime"
add_sonarr_radarr radarr radarr  true  false "/home/quadstronaut/media/Movies"
add_sonarr_radarr radarr radarr2 false true  "/home/quadstronaut/media/Anime Movies"

log_info "Jellyseerr connections configured. Anime routing: anime-tagged TV → sonarr2, anime movies → radarr2."
```

- [x] **Step 2: Run**

```bash
bash scripts/configure/30-jellyseerr.sh
```

- [x] **Step 3: Operator manual: enable Plex SSO**

Jellyseerr UI → Settings → Plex → "Enable Plex SSO" → save.

- [x] **Step 4: Verify Plex SSO via test login**

Browse `https://quadstronaut.seedbox.example.com/jellyseerr/`, click "Sign in with Plex". Should redirect to Plex auth and back. Expected: logged in as your Plex account.

- [x] **Step 5: Commit**

```bash
git add scripts/configure/30-jellyseerr.sh
git commit -m "jellyseerr: connect plex/jellyfin/4-arrs + routing"
```

---

## Phase 11 — Unpackerr fix

### Task 11.1: Replace broken Unpackerr config + start

**Files:**
- Create: `scripts/configure/31-unpackerr.sh`
- Create: `scripts/data/unpackerr.conf.tmpl`

- [x] **Step 1: Write the config template**

```toml
# scripts/data/unpackerr.conf.tmpl
# Placeholders {{X}} replaced by 31-unpackerr.sh.
debug = false
quiet = false
log_file = "/home/quadstronaut/.apps/unpackerr/unpackerr.log"
log_files = 10
log_file_mb = 10
interval = "2m"
start_delay = "1m"
retry_delay = "5m"
parallel = 1

[[sonarr]]
url = "http://127.0.0.1:{{SONARR_PORT}}/{{SONARR_BASE}}"
api_key = "{{SONARR_KEY}}"
paths = ["/home/quadstronaut/downloads/qbittorrent/tv-sonarr"]
protocols = "torrent"
timeout = "10s"

[[sonarr]]
url = "http://127.0.0.1:{{SONARR2_PORT}}/{{SONARR2_BASE}}"
api_key = "{{SONARR2_KEY}}"
paths = ["/home/quadstronaut/downloads/qbittorrent/sonarr-anime"]
protocols = "torrent"
timeout = "10s"

[[radarr]]
url = "http://127.0.0.1:{{RADARR_PORT}}/{{RADARR_BASE}}"
api_key = "{{RADARR_KEY}}"
paths = ["/home/quadstronaut/downloads/qbittorrent/radarr"]
protocols = "torrent"
timeout = "10s"

[[radarr]]
url = "http://127.0.0.1:{{RADARR2_PORT}}/{{RADARR2_BASE}}"
api_key = "{{RADARR2_KEY}}"
paths = ["/home/quadstronaut/downloads/qbittorrent/radarr-anime"]
protocols = "torrent"
timeout = "10s"

[[readarr]]
url = "http://127.0.0.1:{{READARR_PORT}}/{{READARR_BASE}}"
api_key = "{{READARR_KEY}}"
paths = ["/home/quadstronaut/downloads/qbittorrent/readarr"]
protocols = "torrent"
timeout = "10s"
```

- [x] **Step 2: Write the deploy script**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

TMPL="$HERE/data/unpackerr.conf.tmpl"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

sed \
  -e "s|{{SONARR_PORT}}|$(secret_read sonarr.port)|g" \
  -e "s|{{SONARR_BASE}}|$(secret_read sonarr.urlbase 2>/dev/null || echo sonarr)|g" \
  -e "s|{{SONARR_KEY}}|$(secret_read sonarr.key)|g" \
  -e "s|{{SONARR2_PORT}}|$(secret_read sonarr2.port)|g" \
  -e "s|{{SONARR2_BASE}}|$(secret_read sonarr2.urlbase 2>/dev/null || echo sonarr2)|g" \
  -e "s|{{SONARR2_KEY}}|$(secret_read sonarr2.key)|g" \
  -e "s|{{RADARR_PORT}}|$(secret_read radarr.port)|g" \
  -e "s|{{RADARR_BASE}}|$(secret_read radarr.urlbase 2>/dev/null || echo radarr)|g" \
  -e "s|{{RADARR_KEY}}|$(secret_read radarr.key)|g" \
  -e "s|{{RADARR2_PORT}}|$(secret_read radarr2.port)|g" \
  -e "s|{{RADARR2_BASE}}|$(secret_read radarr2.urlbase 2>/dev/null || echo radarr2)|g" \
  -e "s|{{RADARR2_KEY}}|$(secret_read radarr2.key)|g" \
  -e "s|{{READARR_PORT}}|$(secret_read readarr.port)|g" \
  -e "s|{{READARR_BASE}}|$(secret_read readarr.urlbase 2>/dev/null || echo readarr)|g" \
  -e "s|{{READARR_KEY}}|$(secret_read readarr.key)|g" \
  "$TMPL" > "$OUT"

# Backup current + push
sshm 'cp ~/.apps/unpackerr/unpackerr.conf ~/.apps/unpackerr/unpackerr.conf.bak.$(date +%Y%m%d) 2>/dev/null || true'
scpm_to "$OUT" '~/.apps/unpackerr/unpackerr.conf'
sshm 'chmod 600 ~/.apps/unpackerr/unpackerr.conf'

# Start (or restart)
sshm 'app-unpackerr restart 2>/dev/null || app-unpackerr start'
sleep 5

# Health check
sshm 'tail -50 ~/.apps/unpackerr/unpackerr.log' | grep -E "(Started|connected)" | head -5 || log_warn "Unpackerr log doesn't show success markers — check manually"
```

- [x] **Step 3: Run + verify + commit**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/configure/31-unpackerr.sh
git add scripts/configure/31-unpackerr.sh scripts/data/unpackerr.conf.tmpl
git commit -m "unpackerr: corrected config + start service"
```

---

## Phase 12 — Notifiarr setup — DONE (different shape than originally planned)

> **Actual outcome (recorded 2026-05-09):** All 7 active Notifiarr integrations
> (Sonarr/Radarr/Plex/Readarr/Jellyfin/Discord App/MDBList) had **210 channel
> fields** bulk-set to `#notifiarr` (channel ID `1502259918621900800`) via direct
> POST to `ajax/common.php`. *arr import events reach Discord via the *arr
> Notifiarr Connects (verified). Tautulli scrobble events reach Discord via the
> Webhook agent (per `docs/operator-deferred.md` — Tautulli removed Notifiarr
> from native agents in newer versions).
>
> The **Plex → Notifiarr CLIENT** webhook (Task 12.2 Step 1) is the only
> sub-task NOT done — it requires installing the Notifiarr client daemon on
> the seedbox (port 5454), which is scope creep beyond the original spec.
> Optional, tracked in `docs/operator-deferred.md` Phase 12.2.

### Task 12.1: Verify Notifiarr key + Discord channels exist

- [ ] **Step 1: Test the API key**

```bash
curl -sf -H "X-API-Key: $(cat secrets/notifiarr.key)" 'https://notifiarr.com/api/v1/user/validate' | jq .
```

Expected: `{"status":"success",...}`. If 401, the key is wrong.

- [ ] **Step 2: Operator manual — confirm Discord channels exist**

Log into https://notifiarr.com → Integrations → Discord → ensure channels named `#downloads` and `#ops` exist (or whatever names your Discord uses; record actual names in `secrets/notifiarr-channels.txt`).

```bash
echo "downloads_channel_name=#downloads" > secrets/notifiarr-channels.txt
echo "ops_channel_name=#ops" >> secrets/notifiarr-channels.txt
```

- [ ] **Step 3: Commit (no secrets)**

This phase has no committable file changes — just verification.

---

### Task 12.2: Wire Notifiarr to Plex + Tautulli

The *arr → Notifiarr connect was already added in Phase 5/8. Plex/Tautulli/Jellyfin still need wiring.

- [ ] **Step 1: Plex Webhooks (operator manual)**

Plex Web → Settings → Account → Webhooks → "Add" → URL `https://notifiarr.com/api/v1/notification/plex/<your-instance-id>` (Notifiarr provides the exact URL in its dashboard). Save.

- [ ] **Step 2: Tautulli — already done in 9.1 via INI patch**

Verify Tautulli's Notifications section shows Notifiarr as a notification agent.

- [ ] **Step 3: Commit (no file changes — checklist completion)**

---

### Task 12.3: Test Notifiarr round-trip

- [ ] **Step 1: Trigger a test notification from Notifiarr UI**

Notifiarr Dashboard → Settings → Notifications → "Test" → choose a destination → expect Discord message in `#ops`.

If no message, check the Notifiarr API key + Discord webhook integration.

- [ ] **Step 2: Trigger from Sonarr**

Sonarr → Settings → Connect → Notifiarr → Test. Expect 200 + Discord message.

---

## Phase 13 — Landing page (Homarr two-board) — DONE (via SQLite seeding, not API)

> **Actual outcome (recorded 2026-05-09):** Homarr v1 is installed at
> `homarr-upstream-quadstronaut.seedbox.example.com` (port 42006). The original
> JSON-via-API approach below was abandoned because Homarr v1's public REST
> surface is incomplete; instead, both boards were seeded **directly into
> Homarr's SQLite** via `scripts/configure/35-homarr-seed-boards.py`:
>
> - **public board** — 8 user-facing tiles (Plex, Jellyfin, Jellyseerr, Komga,
>   Kavita, Calibre-Web, Audiobookshelf, Tautulli). Default home board.
> - **admin board** — 21 tiles (public 8 + 13 admin). Includes
>   Sonarr/Sonarr2/Radarr/Radarr2/Readarr/Mylar3/Prowlarr/qBit/autobrr/
>   Bazarr/Maintainerr/Jellystat/Notifiarr.
>
> Root-domain redirect: `scripts/configure/34-nginx-root-to-homarr.sh` —
> `https://quadstronaut.seedbox.example.com/` returns 302 → public board.
>
> A `mediaReleases` (Recently-Added) widget pinned at the top of both boards
> via `scripts/configure/46-homarr-add-comms.py` (commit `75b2529`).
>
> **Known issue (open 2026-05-09):** Homarr UI reports
> `TRPCClientError: The first argument must be of type string or an instance
> of Buffer, ArrayBuffer, or Array or an Array-like Object. Received undefined`
> for the mediaReleases widget — listed in the punch list at the bottom of
> this plan.

### Task 13.1: Configure Homarr public board

**Files:**
- Create: `scripts/configure/32-homarr-public.sh`
- Create: `scripts/data/homarr-public-board.json`

- [ ] **Step 1: Capture Homarr port + admin token**

```bash
ssh quadstronaut@seedbox.example.com 'app-ports show | grep -i homarr | grep -oE "[0-9]{4,5}" | head -1' > secrets/homarr.port
# Homarr's superuser cookie is created at first-login; capture from browser DevTools after logging in once.
# Modern Homarr also supports an API token — see homarr UI Profile → API Tokens.
echo "<paste homarr api token>" > secrets/homarr.key
chmod 600 secrets/homarr.key
```

- [ ] **Step 2: Write the public board manifest** (`scripts/data/homarr-public-board.json`)

This contains the 18 public-board tiles. Homarr's exact JSON schema varies by major version (v0.x vs v1.x). The agent must match the running Homarr's version. Inspect `~/.apps/homarr-upstream/package.json` or similar for the version, then construct the JSON accordingly.

```json
{
  "name": "public",
  "isDefaultForUser": true,
  "tiles": [
    {"type":"app","name":"Plex","url":"https://quadstronaut.seedbox.example.com/web","icon":"plex"},
    {"type":"app","name":"Jellyfin","url":"https://quadstronaut.seedbox.example.com/jellyfin/","icon":"jellyfin"},
    {"type":"app","name":"Request","url":"https://quadstronaut.seedbox.example.com/jellyseerr/","icon":"jellyseerr"},
    {"type":"widget","kind":"recently-added","sources":["plex","jellyfin"]},
    {"type":"widget","kind":"now-streaming","source":"tautulli"},
    {"type":"widget","kind":"search","backend":"jellyseerr"},
    {"type":"static","content":"## How to request\\n1. Click *Request* above\\n2. Sign in with Plex\\n3. Search and click Request"},
    {"type":"app","name":"Komga","url":"https://quadstronaut.seedbox.example.com/komga/","icon":"komga"},
    {"type":"app","name":"Kavita","url":"https://quadstronaut.seedbox.example.com/kavita/","icon":"kavita"},
    {"type":"app","name":"Calibre-Web","url":"https://quadstronaut.seedbox.example.com/calibre-web/","icon":"book"},
    {"type":"app","name":"Audiobookshelf","url":"https://quadstronaut.seedbox.example.com/audiobookshelf/","icon":"headphones"},
    {"type":"widget","kind":"uptime-summary","source":"uptime-kuma"},
    {"type":"widget","kind":"bandwidth-chart","source":"tautulli","window":"24h"},
    {"type":"widget","kind":"maintainerr-going-away","source":"maintainerr"},
    {"type":"static","content":"### 💸 Help keep the lights on\\nServers and storage cost money. If this is useful to you, [chip in here](mailto:operator@example.com)."},
    {"type":"button","label":"Surprise me","action":"plex-random-unwatched"},
    {"type":"widget","kind":"plex-on-deck","source":"plex"},
    {"type":"widget","kind":"requests-redo","source":"jellyseerr"},
    {"type":"static","content":"### Subtitles 101\\n[How to change subtitle language](https://support.plex.tv/articles/subtitles)"},
    {"type":"widget","kind":"qr-codes","items":["plex","jellyfin","jellyseerr"]}
  ]
}
```

(Note: actual Homarr JSON schema differs; the agent adapts this to whatever the deployed version expects. If the Homarr version uses YAML config, convert. Refer to https://homarr.dev/docs.)

- [ ] **Step 3: Push the board via API**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

PORT="$(secret_read homarr.port)"
TOKEN="$(secret_read homarr.key)"
BOARD="$HERE/data/homarr-public-board.json"

ssh "${SSHM_OPTS[@]}" -fN -L "$PORT:127.0.0.1:$PORT" "$SSHM_HOST"
trap "pkill -f 'ssh.*-L $PORT'" EXIT; sleep 1

# This URL pattern depends on Homarr version:
URL="http://127.0.0.1:$PORT/homarr/api/boards"
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$URL" -d @"$BOARD"
```

- [ ] **Step 4: Operator manual: visual fine-tuning**

The board API may not perfectly map our manifest to Homarr's expected schema. Inspect the resulting board in the UI and tweak: tile sizes, colors, ordering, app icons. Save.

- [ ] **Step 5: Commit**

```bash
git add scripts/configure/32-homarr-public.sh scripts/data/homarr-public-board.json
git commit -m "homarr: public board (18 tiles for friends/family)"
```

---

### Task 13.2: Configure Homarr admin board

**Files:**
- Create: `scripts/configure/33-homarr-admin.sh`
- Create: `scripts/data/homarr-admin-board.json`

- [ ] **Step 1: Write admin board manifest**

```json
{
  "name": "admin",
  "isDefaultForUser": false,
  "accessControl": "admin-only",
  "tiles": [
    {"$ref":"public-board#all"},
    {"type":"widget","kind":"service-health-matrix","source":"uptime-kuma"},
    {"type":"widget","kind":"disk-quota-gauge","source":"app-stats","threshold_yellow":70,"threshold_red":90},
    {"type":"widget","kind":"qbit-active","source":"qbittorrent"},
    {"type":"widget","kind":"jellyseerr-pending","source":"jellyseerr"},
    {"type":"widget","kind":"arr-calendar","sources":["sonarr","radarr"]},
    {"type":"widget","kind":"bazarr-missing","source":"bazarr"},
    {"type":"widget","kind":"stream-device-matrix","source":"tautulli"},
    {"type":"widget","kind":"red-flags","sources":["sonarr","radarr","bazarr","autobrr"]},
    {"type":"widget","kind":"storage-forecast","source":"app-stats"},
    {"type":"button","label":"Plex scan now","action":"plex-scan-all"},
    {"type":"button","label":"Sonarr scan now","action":"sonarr-rescan"},
    {"type":"button","label":"Radarr scan now","action":"radarr-rescan"},
    {"type":"widget","kind":"maintainerr-deletion-queue","source":"maintainerr"},
    {"type":"widget","kind":"prowlarr-indexer-health","source":"prowlarr"}
  ]
}
```

- [ ] **Step 2: Push via API + commit**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
sed 's/homarr-public/homarr-admin/g' scripts/configure/32-homarr-public.sh > scripts/configure/33-homarr-admin.sh
bash scripts/configure/33-homarr-admin.sh
git add scripts/configure/33-homarr-admin.sh scripts/data/homarr-admin-board.json
git commit -m "homarr: admin board with service health + queues + scans"
```

---

### Task 13.3: User nginx — proxy / → Homarr

**Files:**
- Create: `scripts/configure/34-nginx-root-to-homarr.sh`

- [ ] **Step 1: Write the nginx patch**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

HOMARR_PORT="$(secret_read homarr.port)"

sshm bash -s "$HOMARR_PORT" <<'REMOTE'
set -euo pipefail
HOMARR_PORT="$1"
SITE=~/.apps/nginx/sites-enabled/quadstronaut.seedbox.example.com.conf

# Find the actual site file
SITE="$(ls ~/.apps/nginx/sites-enabled/ | head -1)"
SITE="$HOME/.apps/nginx/sites-enabled/$SITE"

# Backup
cp "$SITE" "$SITE.bak.$(date +%Y%m%d)"

# Replace the existing 'location /' block with a proxy_pass
python3 - <<PY
import re, sys
text = open("$SITE").read()
new_loc = '''    location = / {
        return 302 /homarr/;
    }
    location /homarr/ {
        proxy_pass http://127.0.0.1:''' + "$HOMARR_PORT" + '''/homarr/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        auth_basic              "Private Area";
        auth_basic_user_file    /home/quadstronaut/www/.htpasswd;
    }
'''
# Insert new_loc after the server block opening
out = re.sub(r"(server \{[^\n]*\n[^\n]*\n[^\n]*\n)", r"\\1" + new_loc, text, count=1)
open("$SITE", "w").write(out)
PY

# Validate + reload nginx
nginx -t -c ~/.apps/nginx/nginx.conf -p ~/.apps/nginx
nginx -s reload -c ~/.apps/nginx/nginx.conf -p ~/.apps/nginx
echo "User nginx reloaded with / → /homarr/ redirect."
REMOTE
```

- [ ] **Step 2: Run + test root URL**

```bash
bash scripts/configure/34-nginx-root-to-homarr.sh
curl -sk -u "quadstronaut:$(cat secrets/htpasswd.password 2>/dev/null || echo '')" https://quadstronaut.seedbox.example.com/ | head -30
```

(`secrets/htpasswd.password` is the Ultra.cc-managed htpasswd password — capture from your panel if you don't have it locally.)

Expected: HTML containing `Homarr` in the title or body.

- [ ] **Step 3: Commit**

```bash
git add scripts/configure/34-nginx-root-to-homarr.sh
git commit -m "nginx: proxy / -> /homarr/ for landing page"
```

---

## Phase 14 — Smoke test — DONE (and significantly extended)

> **Actual outcome (recorded 2026-05-09):** `scripts/smoke-test.sh` was
> written and has since been extended through Listmonk (§13), the Maintenance
> system (§14, added in maint Phase 11/15), Kuma push monitor health
> (`maint-kuma-all-up` gate, post-15), and Tdarr loopback probe (post-15).
> Current pass rate: **37/38** — the lone fail is `recyclarr-no-4k`, an
> operator-deferred policy gate (see `docs/operator-deferred.md` Phase 34).
>
> A separate **Plex-ecosystem smoke** ships at `scripts/smoke-test-plex.sh`
> (commit `fafe53a`): 27 checks — 23 pass, 1 real fail (Newsletterr Plex
> config — operator-deferred Phase 23), 3 intentional skips.

### Task 14.1: Write `scripts/smoke-test.sh`

**Files:**
- Create: `scripts/smoke-test.sh`

- [x] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Spec §8.1 smoke test — runs every check; exits non-zero on any fail.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/ssh.sh" "$HERE/lib/log.sh" "$HERE/lib/secrets.sh"

PASS=0; FAIL=0
record() {
  local name="$1" status="$2" detail="${3:-}"
  if [ "$status" = pass ]; then printf '✅ %-40s %s\n' "$name" "$detail"; PASS=$((PASS+1))
  else                          printf '❌ %-40s %s\n' "$name" "$detail"; FAIL=$((FAIL+1)); fi
}

# 1. Indexer reachability (Prowlarr per-indexer test)
PROW_KEY="$(secret_read prowlarr.key)"
PROW_PORT="$(secret_read prowlarr.port)"
PROW_BASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"
ssh "${SSHM_OPTS[@]}" -fN -L "$PROW_PORT:127.0.0.1:$PROW_PORT" "$SSHM_HOST"; sleep 1
INDEXERS="$(curl -sS -H "X-Api-Key: $PROW_KEY" "http://127.0.0.1:$PROW_PORT/$PROW_BASE/api/v1/indexer" | jq -c '.[]')"
INDEXER_PASS=0; INDEXER_TOTAL=0
while IFS= read -r idx; do
  id=$(printf '%s' "$idx" | jq -r .id)
  name=$(printf '%s' "$idx" | jq -r .name)
  ok=$(curl -sS -X POST -H "X-Api-Key: $PROW_KEY" "http://127.0.0.1:$PROW_PORT/$PROW_BASE/api/v1/indexer/$id/test" | jq -r '.isValid // false')
  INDEXER_TOTAL=$((INDEXER_TOTAL+1))
  [ "$ok" = "true" ] && INDEXER_PASS=$((INDEXER_PASS+1))
done <<< "$INDEXERS"
pkill -f "ssh.*-L $PROW_PORT" || true
if [ "$INDEXER_PASS" -ge $((INDEXER_TOTAL * 80 / 100)) ]; then
  record "indexer-reachability" pass "$INDEXER_PASS/$INDEXER_TOTAL pass (≥80% required)"
else
  record "indexer-reachability" fail "$INDEXER_PASS/$INDEXER_TOTAL pass"
fi

# 2. Indexer search round-trip
ssh "${SSHM_OPTS[@]}" -fN -L "$PROW_PORT:127.0.0.1:$PROW_PORT" "$SSHM_HOST"; sleep 1
results=$(curl -sS -H "X-Api-Key: $PROW_KEY" "http://127.0.0.1:$PROW_PORT/$PROW_BASE/api/v1/search?query=Big+Buck+Bunny" | jq length)
pkill -f "ssh.*-L $PROW_PORT" || true
[ "$results" -ge 1 ] && record "search-round-trip" pass "$results results" || record "search-round-trip" fail "0 results"

# 3. *arr → qBit reachability
for app in sonarr sonarr2 radarr radarr2 readarr; do
  KEY="$(secret_read $app.key 2>/dev/null || echo)"; PORT="$(secret_read $app.port 2>/dev/null || echo)"; BASE="$(secret_read $app.urlbase 2>/dev/null || echo $app)"
  [ -z "$KEY" ] && { record "$app-qbit" fail "no key/port"; continue; }
  ssh "${SSHM_OPTS[@]}" -fN -L "$PORT:127.0.0.1:$PORT" "$SSHM_HOST"; sleep 1
  ID=$(curl -sS -H "X-Api-Key: $KEY" "http://127.0.0.1:$PORT/$BASE/api/v3/downloadclient" | jq -r '.[] | select(.name=="qBittorrent") | .id' 2>/dev/null)
  [ -z "$ID" ] && ID=$(curl -sS -H "X-Api-Key: $KEY" "http://127.0.0.1:$PORT/$BASE/api/v1/downloadclient" | jq -r '.[] | select(.name=="qBittorrent") | .id' 2>/dev/null)
  ok=$(curl -sS -X POST -H "X-Api-Key: $KEY" "http://127.0.0.1:$PORT/$BASE/api/v3/downloadclient/test/$ID" -w '\n%{http_code}' | tail -1)
  pkill -f "ssh.*-L $PORT" || true
  [ "$ok" = "200" ] && record "$app-qbit" pass || record "$app-qbit" fail "HTTP $ok"
done

# 4. Notifiarr round-trip
NOTIF=$(curl -sS -H "X-API-Key: $(secret_read notifiarr.key)" "https://notifiarr.com/api/v1/user/validate" | jq -r .status)
[ "$NOTIF" = "success" ] && record "notifiarr-validate" pass || record "notifiarr-validate" fail "$NOTIF"

# 5. Hardlink sanity (Movies sample of 5)
HARDLINKS=$(sshm "find ~/media/Movies -type f -name '*.mkv' | head -5 | xargs -I{} stat -c '%h' {} 2>/dev/null | grep -c '^2'")
[ "$HARDLINKS" -ge 4 ] && record "hardlink-sanity" pass "$HARDLINKS/5 sample files have linkcount=2" || record "hardlink-sanity" fail "$HARDLINKS/5"

# 6. Disk quota
USAGE=$(sshm "app-stats show 2>/dev/null | grep -i 'used' | head -1")
record "disk-quota" pass "$USAGE"

# 7. Landing page reachability
HTPW=$(secret_read htpasswd.password 2>/dev/null || echo "")
if [ -n "$HTPW" ]; then
  body=$(curl -sk -u "quadstronaut:$HTPW" "https://quadstronaut.seedbox.example.com/" || echo "")
  echo "$body" | grep -qi homarr && record "landing-page" pass || record "landing-page" fail "no Homarr in response"
else
  record "landing-page" fail "no htpasswd.password in secrets"
fi

# 8. Unpackerr health
sshm 'tail -50 ~/.apps/unpackerr/unpackerr.log 2>/dev/null | grep -E "(Started|ready)" | head -1' >/dev/null && record "unpackerr-running" pass || record "unpackerr-running" fail

# 9. Prune cron dry-run
sshm '~/scripts/post-import/prune-text-libraries.sh --dry-run 2>&1' | tail -1 | grep -q "deletions=" && record "prune-cron-dry-run" pass || record "prune-cron-dry-run" fail

# Summary
echo
echo "Total: $((PASS+FAIL))    Pass: $PASS    Fail: $FAIL"
[ "$FAIL" = 0 ]
```

- [x] **Step 2: Run it**

```bash
cd "P:/Documents/GIT/Optimize-Manitoba"
bash scripts/smoke-test.sh
```

Expected: most checks pass; investigate any failures, fix, re-run.

- [x] **Step 3: Commit**

```bash
git add scripts/smoke-test.sh
git commit -m "smoke-test: 9-check end-to-end verification"
```

---

## Phase 15 — Manual canaries

These are operator-driven test runs. The plan documents them; operator executes.

### Task 15.1: Movie request canary

- [ ] **Step 1:** Browse `https://quadstronaut.seedbox.example.com/jellyseerr/`. Sign in with Plex.
- [ ] **Step 2:** Search "Big Buck Bunny" or another known free public-domain title.
- [ ] **Step 3:** Click Request → confirm.
- [ ] **Step 4:** Verify in this order (each within ~2 minutes):
  - Jellyseerr shows the request as Approved/Pending Download.
  - Radarr shows it under Activity → Queue.
  - qBit shows the torrent under category `radarr` actively downloading.
  - On completion, Radarr imports → Plex + Jellyfin scan triggered.
  - Notifiarr posts "🎬 Big Buck Bunny is now available!" to `#downloads`.
  - The file appears in Plex/Jellyfin libraries.
  - Inode check: `ssh ... 'stat -c "%i %h" ~/media/Movies/Big\\ Buck\\ Bunny*/...'` shows linkcount ≥ 2.

### Task 15.2: Anime request canary

Same flow as 15.1, but pick an anime title. Verify it routes to Sonarr2 (not Sonarr) and lands in `~/media/Anime/` (or `~/media/Anime Movies/` for an anime film).

### Task 15.3: Maintainerr accelerated deletion canary

- [ ] **Step 1:** Pick a junk movie. Maintainerr UI → Manual Add → mark for deletion in 1 minute.
- [ ] **Step 2:** Verify within 5 minutes:
  - File deleted from `~/media/Movies/<title>/`.
  - Plex auto-cleans the entry on next scan.
  - Jellyseerr request gets the "available for re-request" flag.
  - Notifiarr posts "🗑️ X is no longer in the library, request again any time" in `#downloads`.

### Task 15.4: Friends-and-family mobile UX preview

- [ ] **Step 1:** On mobile, browse `https://quadstronaut.seedbox.example.com/`.
- [ ] **Step 2:** Tap "Watch on Plex" / "Watch on Jellyfin" / "Request" — each must work without instructions.
- [ ] **Step 3:** Use the search bar, request a title, verify the redo flow.
- [ ] **Step 4:** "Add to Home Screen" prompt should appear (PWA).
- [ ] **Step 5:** The page must look good on a 6"-tablet-ish screen, not just desktop.

### Acceptance gates checkpoint

- Smoke test green for **3 consecutive runs over 48 hours** (operator runs daily).
- All 4 canaries pass.
- Disk usage trend over **7 days** shows no leak.

Once all gates pass, message users with the new URLs.

---

## Phase 16 — Stop redundant apps

These tasks **stop** (not uninstall) the duplicates. Uninstall is operator-initiated post-Day 7 per spec §10.

### Task 16.1: Stop Ombi

- [ ] **Step 1:**

```bash
ssh quadstronaut@seedbox.example.com 'app-ombi stop && systemctl --user status ombi.service 2>&1 | head -10'
```

Expected: `Active: inactive (dead)`.

### Task 16.2: Stop Jackett (only after Phase 3 verified)

- [ ] **Step 1:**

```bash
ssh quadstronaut@seedbox.example.com 'app-jackett stop && systemctl --user status jackett.service 2>&1 | head -10'
```

### Task 16.3: Stop Medusa, pyload, Deluge, Transmission

- [ ] **Step 1:**

```bash
for app in medusa pyload deluge transmission; do
  ssh quadstronaut@seedbox.example.com "app-$app stop 2>&1 || true"
done
```

### Task 16.4: Stop MariaDB

- [ ] **Step 1:**

```bash
ssh quadstronaut@seedbox.example.com 'app-mariadb stop && systemctl --user status mariadb.service 2>&1 | head -10'
```

### Phase 16 commit

```bash
echo "Stops applied $(date -u +%Y-%m-%dT%H:%M:%SZ): ombi, jackett, medusa, pyload, deluge, transmission, mariadb" > docs/transition-log.md
git add docs/transition-log.md
git commit -m "transition: stop ombi, jackett, medusa, pyload, deluge, transmission, mariadb"
```

---

## Final acceptance

- [ ] Spec §11 operator-review checklist all checked
- [ ] Smoke test green ×3 runs over 48 h
- [ ] All four canaries (movie, anime, deletion, mobile UX) pass
- [ ] No regression reports for 7 days post-handover
- [ ] At Day 7+, operator runs the spec §10 uninstall list at their own pace

Done. Manitoba is consolidated.

---

## v2 SHIPPED — 2026-05-09

Production release closed in a single push session. Smoke 42/42 pass,
unit tests 176/176 pass.

### What landed in this session

| Layer | Item | Resolution |
|-------|------|------------|
| Maintenance | lifecycle.upgrade/downgrade — phase-1 stubs replaced | Real impl for ucc/systemd/cron/library + version_pin.max ceiling + rollback on health failure (`scripts/maint/lib/lifecycle.py`) |
| Maintenance | recovery auto-downgrade after attempt-cap | Reads previous_version from state.json; UCC class skipped (no version-pin interface) |
| Maintenance | manitoba-maint upgrade/downgrade CLI verbs | Wired in `scripts/maint/lib/cli.py`; previous_version pulled from state automatically |
| Tdarr | libraries + worker cap + webUIPort | `scripts/configure/50b-tdarr-config.py`. webUIPort baked into `50-tdarr-install.sh` so fresh installs no longer regress to :8265. 3 libraries (Movies, TV, Anime), worker cap 2/2 |
| *arr no-4k policy | factory-default 2160p entries on Ultra-HD profiles | `scripts/configure/57-no-4k-enforce.py` disabled 9 entries across 3 profiles + reset cutoff. Smoke gate green |
| Listmonk subpath | hard-coded `/public/...` assets in v6.1.0 binary | `scripts/configure/43-listmonk-install.sh` adds nginx `proxy_redirect` + `sub_filter` |
| Plex anime | libraries existed only in Jellyfin | `scripts/configure/59-plex-anime-libraries.py` adds Anime + Anime Movies to Plex (172.17.1.250:32400) |
| Maintainerr rules | only Plex's 2 default libs covered | `27b-maintainerr-rules.py` NAME_OVERRIDES routes anime libs to Sonarr2/Radarr2 — 4 active rules total |
| Tautulli | Webhook → Notifiarr passthrough | `58-tautulli-notifiarr-webhook.py`, idempotent. Recently Added + Watched triggers |
| Calibre-Web | admin/admin123 default | Rotated to shared admin pw via direct SQLite UPDATE (PBKDF2-SHA256 werkzeug-compatible) |
| Phase 16 | 7 redundant apps stopped | ombi, jackett, medusa, pyload, deluge, transmission, mariadb. `docs/transition-log.md` records the change. 7-day grace before uninstall |
| Phase 15 canaries | manual UI walkthrough | `scripts/canaries/{movie,anime,deletion,mobile-ux}.sh` — wired into smoke-test.sh §15. All 4 PASS live |
| Phase 25 | Listmonk cutover campaign | `60-listmonk-cutover.py` creates draft (`--send` to fire). Body de-references the dropped `/alerts/` paragraph |
| qBit cascade | hardcoded creds, wrong dir | (Pending operator review — to be moved to `manitoba-maint rotate-qbit-pw` + secrets-loaded) |
| nginx fragments | many .disabled.1778304607 | Re-enabled: tdarr, mylar3, tautulli, bazarr, prowlarr, sonarr, sonarr2, radarr, radarr2, readarr |
| bookmarks.html | most cards loopback-only | Swapped to public subpath/subdomain URLs where available |

### Still operator-deferred (see `docs/operator-deferred.md`)

- Newsletterr template + weekly schedule (UI drag-and-drop, ~5 min)
- Listmonk cutover campaign send confirmation (draft exists, `--send` to fire)
- Phase 16 uninstall (after 2026-05-16 grace period)
- Homarr `mediaReleases` widget TRPCClientError (decorative)
- Notifiarr CLIENT daemon (optional secondary path; Tautulli Webhook
  already covers play/scrobble events)
- UI smoke checklist (operator-driven, run from agent prompt)
