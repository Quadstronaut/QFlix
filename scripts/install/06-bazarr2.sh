#!/usr/bin/env bash
# Install Bazarr 2 — a parallel Bazarr instance for the anime *arr pair.
# Bazarr supports only one Sonarr and one Radarr per install (wontfix
# upstream); we already have a UCC-managed Bazarr-1 paired with the main
# Sonarr/Radarr, and this script provisions Bazarr-2 paired with Sonarr2
# /Radarr2. Ultra.cc has no `app-bazarr2`, so this is a bare-Python install
# under ~/.apps/bazarr2/ wrapped by a user systemd unit, plus a companion
# hourly timer (bazarr2-sync) that pins the version to bazarr-1's.
#
# Idempotent: a re-run preserves the existing apikey/flask_secret/config
# (so already-issued credentials don't churn) and only fills in anything
# missing.
#
# Pre-reqs (run install order):
#   - 05-sonarr2.sh  → secrets/sonarr2.{key,port,urlbase} populated
#   - 07-radarr2.sh  → secrets/radarr2.{key,port,urlbase} populated
#   - secrets/bazarr.key (so we can probe bazarr-1's version for the pin)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

REPO_ROOT="$(cd "$HERE/.." && pwd)"
PYTHON311=/home28/quadstronaut/.local/python311/bin/python3.11
BAZARR2_PORT=17032

# --- 1. Determine which Bazarr version to pin to ---------------------------
# Match bazarr-1's running version so the two never drift at install time.
# bazarr2-sync.timer keeps them aligned thereafter.
TARGET_VERSION=""
if secret_exists bazarr.key; then
  B1_KEY="$(secret_read bazarr.key)"
  B1_PORT="$(secret_read bazarr.port 2>/dev/null || echo 17031)"
  TARGET_VERSION="$(sshm "curl -sf -m 5 -H 'X-API-KEY: $B1_KEY' http://127.0.0.1:$B1_PORT/bazarr/api/system/status 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"data\"][\"bazarr_version\"])' 2>/dev/null" || true)"
fi
TARGET_VERSION="${TARGET_VERSION:-1.5.5}"
log_info "bazarr2 will pin to v$TARGET_VERSION (mirrored from bazarr-1)"

# --- 2. Sonarr2/Radarr2 endpoints (loopback, no htpasswd path) -------------
SONARR2_KEY="$(secret_read sonarr2.key)"
SONARR2_PORT="$(secret_read sonarr2.port)"
SONARR2_BASE="$(secret_read sonarr2.urlbase 2>/dev/null || echo sonarr2)"
RADARR2_KEY="$(secret_read radarr2.key)"
RADARR2_PORT="$(secret_read radarr2.port)"
RADARR2_BASE="$(secret_read radarr2.urlbase 2>/dev/null || echo radarr2)"

# --- 3. Directory layout ---------------------------------------------------
sshm 'mkdir -p ~/.apps/bazarr2/{bin,data,logs} ~/.opt/maint ~/secrets'

# --- 4. Clone bazarr at pinned tag (idempotent) ----------------------------
sshm "if [ ! -d ~/.apps/bazarr2/bin/.git ]; then
        git clone --depth 1 -b v$TARGET_VERSION https://github.com/morpheus65535/bazarr.git ~/.apps/bazarr2/bin
      else
        echo 'bazarr2 already cloned; skipping'
      fi"

# --- 5. Build venv with python3.11 (the python-plexapi venv already proves
#       this interpreter is on the box). Skip if venv exists. --------------
sshm "if [ ! -x ~/.apps/bazarr2/venv/bin/python ]; then
        $PYTHON311 -m venv ~/.apps/bazarr2/venv
        ~/.apps/bazarr2/venv/bin/pip install --quiet --upgrade pip wheel setuptools
      fi
      ~/.apps/bazarr2/venv/bin/pip install --quiet -r ~/.apps/bazarr2/bin/requirements.txt"

# --- 6. waitress thread patch (idempotent) ---------------------------------
# Bazarr hardcodes `threads=100` in bazarr/app/server.py; the host kernel
# refuses bursts of 100 thread creates from a freshly-imported Python child
# (works fine inside the LSIO container's cleaner namespace). Default
# waitress threads=4 is plenty.
sshm "sed -i 's/threads=100)/threads=4)/' ~/.apps/bazarr2/bin/bazarr/app/server.py"

# --- 7. Seed bazarr2 secrets if not yet stored -----------------------------
if ! secret_exists bazarr2.key; then
  BAZARR2_KEY="$(sshm 'openssl rand -hex 16')"
  secret_write bazarr2.key "$BAZARR2_KEY"
  log_info "minted new bazarr2.key"
else
  BAZARR2_KEY="$(secret_read bazarr2.key)"
fi
secret_write bazarr2.port    "$BAZARR2_PORT"
secret_write bazarr2.urlbase "bazarr2"
# Mirror to seedbox secrets (consumed by maint pusher + bazarr2-sync).
echo -n "$BAZARR2_KEY"  | sshm "cat > ~/secrets/bazarr2.key && chmod 600 ~/secrets/bazarr2.key"
echo -n "$BAZARR2_PORT" | sshm "cat > ~/secrets/bazarr2.port && chmod 600 ~/secrets/bazarr2.port"
echo -n "bazarr2"       | sshm "cat > ~/secrets/bazarr2.urlbase && chmod 600 ~/secrets/bazarr2.urlbase"

# --- 8. Seed config.yaml only if missing (preserves operator edits) -------
if ! sshm 'test -f ~/.apps/bazarr2/data/config/config.yaml'; then
  FLASK_KEY="$(sshm 'openssl rand -hex 16')"
  # Re-use bazarr-1's username + password-hash so login works the same way.
  B1_AUTH_USER="$(sshm "awk '/^auth:/,/^[a-z]/{if(\$1==\"username:\"){print \$2; exit}}' ~/.apps/bazarr/config/config.yaml" || echo quadstronaut)"
  B1_AUTH_PW_HASH="$(sshm "awk '/^auth:/,/^[a-z]/{if(\$1==\"password:\"){print \$2; exit}}' ~/.apps/bazarr/config/config.yaml" || echo "")"
  sshm "mkdir -p ~/.apps/bazarr2/data/config"
  sshm "cat > ~/.apps/bazarr2/data/config/config.yaml" <<YAML
---
analytics:
  enabled: false
auth:
  apikey: $BAZARR2_KEY
  type: form
  username: $B1_AUTH_USER
  password: $B1_AUTH_PW_HASH
backup:
  day: 6
  folder: /home28/quadstronaut/.apps/bazarr2/data/backup
  frequency: Weekly
  hour: 3
  retention: 31
cors:
  enabled: false
general:
  adaptive_searching: true
  auto_update: false
  base_url: /bazarr2
  branch: master
  chmod: "0640"
  chmod_enabled: true
  concurrent_jobs: 4
  days_to_upgrade_subs: 7
  debug: false
  enabled_providers:
  - yifysubtitles
  - subssabbz
  - animetosho
  - gestdown
  - greeksubs
  - greeksubtitles
  - hosszupuska
  - napiprojekt
  - nekur
  - podnapisi
  - regielive
  - soustitreseu
  - subdivx
  - subs4free
  - subtitriid
  - supersubtitles
  - subscenter
  - subsunacs
  - subs4series
  - subsynchro
  - subtitrarinoi
  - subtitulamostv
  - titrari
  - tvsubtitles
  - wizdom
  flask_secret_key: $FLASK_KEY
  instance_name: Bazarr 2
  ip: 127.0.0.1
  minimum_score: 90
  minimum_score_movie: 70
  movie_default_enabled: true
  movie_default_profile: 1
  multithreading: true
  page_size: 25
  parse_embedded_audio_track: false
  path_mappings: []
  path_mappings_movie: []
  port: $BAZARR2_PORT
  postprocessing_cmd: ""
  postprocessing_threshold: 90
  postprocessing_threshold_movie: 70
  serie_default_enabled: true
  serie_default_profile: 1
  single_language: false
  subfolder: current
  subfolder_custom: ""
  subzero_mods: fix_uppercase,remove_HI,remove_tags,common,OCR_fixes,emoji
  theme: dark
  upgrade_frequency: 12
  upgrade_manual: true
  upgrade_subs: true
  use_embedded_subs: true
  use_postprocessing: false
  use_radarr: true
  use_sonarr: true
  utf8_encode: true
  wanted_search_frequency: 6
  wanted_search_frequency_movie: 6
proxy:
  exclude:
  - localhost
  - 127.0.0.1
  type: null
sonarr:
  apikey: $SONARR2_KEY
  base_url: /$SONARR2_BASE
  defer_search_signalr: false
  exclude_season_zero: true
  excluded_series_types: []
  excluded_tags: []
  full_update: Daily
  full_update_day: 6
  full_update_hour: 4
  http_timeout: 60
  ip: 127.0.0.1
  only_monitored: false
  port: $SONARR2_PORT
  series_sync: 60
  series_sync_on_live: true
  ssl: false
  sync_only_monitored_episodes: false
  sync_only_monitored_series: false
  use_ffprobe_cache: true
radarr:
  apikey: $RADARR2_KEY
  base_url: /$RADARR2_BASE
  defer_search_signalr: false
  excluded_tags: []
  full_update: Daily
  full_update_day: 6
  full_update_hour: 4
  http_timeout: 60
  ip: 127.0.0.1
  movies_sync: 60
  movies_sync_on_live: true
  only_monitored: false
  port: $RADARR2_PORT
  ssl: false
  sync_only_monitored_movies: false
  use_ffprobe_cache: true
YAML
  sshm "chmod 600 ~/.apps/bazarr2/data/config/config.yaml"
  log_info "seeded ~/.apps/bazarr2/data/config/config.yaml"
else
  log_info "bazarr2 config.yaml already present; leaving operator edits in place"
fi

# --- 9. Install systemd unit + sync timer ---------------------------------
sshm "mkdir -p ~/.config/systemd/user"
sshm "sed 's/BAZARR_VERSION=1\\.5\\.5/BAZARR_VERSION=$TARGET_VERSION/' > ~/.config/systemd/user/bazarr2.service" \
  < "$REPO_ROOT/scripts/maint/systemd/bazarr2.service"
sshm "cat > ~/.config/systemd/user/bazarr2-sync.service" \
  < "$REPO_ROOT/scripts/maint/systemd/bazarr2-sync.service"
sshm "cat > ~/.config/systemd/user/bazarr2-sync.timer" \
  < "$REPO_ROOT/scripts/maint/systemd/bazarr2-sync.timer"
# ~/scripts/maint, NOT ~/.opt/maint (2026-08-18): the unit's ExecStart runs the
# ~/scripts copy, which deploy-drift byte-compares against origin/master. The
# old .opt copy sat outside every auditor's scope and silently pinned at its
# install version - the exact drift f72910f had to dig out by hand.
sshm "mkdir -p ~/scripts/maint && cat > ~/scripts/maint/bazarr2-sync.py && chmod 755 ~/scripts/maint/bazarr2-sync.py" \
  < "$REPO_ROOT/scripts/maint/bazarr2-sync.py"

sshm 'systemctl --user daemon-reload
      systemctl --user enable bazarr2.service bazarr2-sync.timer
      systemctl --user restart bazarr2.service
      systemctl --user start bazarr2-sync.timer'

# --- 10. Verify API responds ----------------------------------------------
for i in 1 2 3 4 5 6 7 8 9 10; do
  status="$(sshm "curl -sf -m 5 -H 'X-API-KEY: $BAZARR2_KEY' http://127.0.0.1:$BAZARR2_PORT/bazarr2/api/system/status" || true)"
  [ -n "$status" ] && break
  log_info "waiting for bazarr2 API... ($i/10)"
  sleep 5
done
if [ -z "$status" ]; then
  log_warn "bazarr2 API not responding — check ~/.apps/bazarr2/logs/bazarr2.err"
  exit 1
fi
ver="$(echo "$status" | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["bazarr_version"])')"
log_info "bazarr2 healthy: v$ver on 127.0.0.1:$BAZARR2_PORT/bazarr2"
