#!/usr/bin/env bash
# Phase 34 — Recyclarr install. Idempotent.
#  - Self-contained .NET binary (recyclarr-linux-x64.tar.xz), no runtime needed
#  - Pinned via secrets/recyclarr.version (default: latest stable at first run)
#  - Generates ~/.apps/recyclarr/config/recyclarr.yml — 1080p ceiling enforced
#  - sonarr (TV English) + sonarr2 (Anime) + radarr (Movies) + radarr2 (Anime/Foreign)
#  - systemd --user TIMER at Sun 04:30 weekly + RandomizedDelaySec=1800
#  - First-run is a `--preview` (operator inspects before live sync)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"
source "$HERE/../lib/secrets.sh"
# Authenticated GitHub API: 60 req/h per SHARED IP -> 5000 per token. Fails
# open if secrets/github.pat is absent, so a missing optional credential can
# never break an install.
source "$HERE/../lib/github.sh"

# ── Step 1: pin version ─────────────────────────────────────────────────────
if ! secret_exists recyclarr.version; then
  TAG=$(gh_latest_tag recyclarr/recyclarr)
  [ -n "$TAG" ] || die "could not resolve Recyclarr latest tag"
  secret_write recyclarr.version "$TAG"
  log_info "pinned recyclarr.version = $TAG"
fi
RVER=$(secret_read recyclarr.version)
RVER_NUM="${RVER#v}"
log_info "recyclarr version = $RVER"

# ── Step 2: download + install binary ───────────────────────────────────────
sshm "RVER='${RVER}' RVER_NUM='${RVER_NUM}' bash -s" <<'INSTSCRIPT'
set -euo pipefail
mkdir -p ~/.apps/recyclarr/{bin,config,logs,cache}
cd ~/.apps/recyclarr
NEED_DL=1
if [ -x bin/recyclarr ]; then
  V=$(./bin/recyclarr --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  [ "$V" = "${RVER_NUM}" ] && NEED_DL=0
fi
if [ "$NEED_DL" = "1" ]; then
  URL="https://github.com/recyclarr/recyclarr/releases/download/${RVER}/recyclarr-linux-x64.tar.xz"
  curl -fsSL "$URL" -o /tmp/recyclarr.tar.xz
  tar -xf /tmp/recyclarr.tar.xz -C bin
  chmod +x bin/recyclarr
  rm -f /tmp/recyclarr.tar.xz
fi
./bin/recyclarr --version | head -1
INSTSCRIPT

# ── Step 3: render recyclarr.yml ────────────────────────────────────────────
SONARR_KEY=$(secret_read sonarr.key)
SONARR_PORT=$(secret_read sonarr.port)
SONARR_BASE=$(secret_read sonarr.urlbase)
SONARR2_KEY=$(secret_read sonarr2.key)
SONARR2_PORT=$(secret_read sonarr2.port)
SONARR2_BASE=$(secret_read sonarr2.urlbase)
RADARR_KEY=$(secret_read radarr.key)
RADARR_PORT=$(secret_read radarr.port)
RADARR_BASE=$(secret_read radarr.urlbase)
RADARR2_KEY=$(secret_read radarr2.key)
RADARR2_PORT=$(secret_read radarr2.port)
RADARR2_BASE=$(secret_read radarr2.urlbase)

# Render YAML by local-side substitution. Vars are expanded by local bash
# before SSH ships the body to the remote.
#
# Recyclarr v8 dropped the `include: - template:` syntax we'd inherited from
# the plan (which was written for v7). v8 replaces it with direct `trash_id`
# references in the quality_profiles block. Per upstream upgrade guide:
#   v7:  include: [ - template: radarr-quality-profile-hd-bluray-web ]
#   v8:  quality_profiles: [ - trash_id: d1d67249... ]
#
# trash_ids extracted from the official v8 config-templates repo:
#   Sonarr WEB-1080p             72dae194fc92bf828f32cde7744e51a1
#   Sonarr Anime Remux-1080p     20e0fc959f1f1704bed501f23bdae76f
#   Radarr HD Bluray + WEB       d1d67249d3890e49bc12e275d989a7e9
#
# radarr2 is the operator's anime/foreign-language Radarr (per 2026-05-08
# interview); anime-specific CFs target episodic content (Sonarr), so for
# movies the standard HD-Bluray-WEB profile covers foreign + anime films too.
sshm "cat > ~/.apps/recyclarr/config/recyclarr.yml" <<EOF
# Recyclarr config — Manitoba
# 1080p cap enforced everywhere per feedback_no-4k-profiles.md
# DO NOT add a UHD/2160p quality_profile entry without operator approval

sonarr:
  tv:
    base_url: http://127.0.0.1:${SONARR_PORT}/${SONARR_BASE}
    api_key: ${SONARR_KEY}
    quality_definition:
      type: series
    quality_profiles:
      - trash_id: 72dae194fc92bf828f32cde7744e51a1  # WEB-1080p
        reset_unmatched_scores:
          enabled: true

  anime:
    base_url: http://127.0.0.1:${SONARR2_PORT}/${SONARR2_BASE}
    api_key: ${SONARR2_KEY}
    quality_definition:
      type: anime
    quality_profiles:
      - trash_id: 20e0fc959f1f1704bed501f23bdae76f  # Anime Remux-1080p
        reset_unmatched_scores:
          enabled: true

radarr:
  movies:
    base_url: http://127.0.0.1:${RADARR_PORT}/${RADARR_BASE}
    api_key: ${RADARR_KEY}
    quality_definition:
      type: movie
    quality_profiles:
      - trash_id: d1d67249d3890e49bc12e275d989a7e9  # HD Bluray + WEB
        reset_unmatched_scores:
          enabled: true

  anime_foreign:
    base_url: http://127.0.0.1:${RADARR2_PORT}/${RADARR2_BASE}
    api_key: ${RADARR2_KEY}
    quality_definition:
      type: movie
    quality_profiles:
      - trash_id: d1d67249d3890e49bc12e275d989a7e9  # HD Bluray + WEB
        reset_unmatched_scores:
          enabled: true
EOF
sshm 'chmod 600 ~/.apps/recyclarr/config/recyclarr.yml'
log_info "wrote ~/.apps/recyclarr/config/recyclarr.yml"

# ── Step 4: dry-run preview to logs (operator-reviewable) ───────────────────
sshm 'cd ~/.apps/recyclarr && ./bin/recyclarr sync --preview --config config/recyclarr.yml 2>&1 | tee logs/preview.log | tail -40'

# ── Step 5: live sync (idempotent — Recyclarr only writes deltas) ───────────
sshm 'cd ~/.apps/recyclarr && ./bin/recyclarr sync --config config/recyclarr.yml 2>&1 | tee -a logs/recyclarr.log | tail -10'

# ── Step 6: systemd timer (weekly Sun 04:30) ────────────────────────────────
sshm "bash -s" <<'UNITSCRIPT'
set -euo pipefail
cat > ~/.config/systemd/user/recyclarr.service <<'UNIT'
[Unit]
Description=Recyclarr TRaSH-guide sync
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/.apps/recyclarr
ExecStart=%h/.apps/recyclarr/bin/recyclarr sync --config %h/.apps/recyclarr/config/recyclarr.yml
Nice=15
StandardOutput=append:%h/.apps/recyclarr/logs/recyclarr.log
StandardError=append:%h/.apps/recyclarr/logs/recyclarr.err
UNIT

cat > ~/.config/systemd/user/recyclarr.timer <<'UNIT'
[Unit]
Description=Recyclarr weekly sync (Sun 04:30 + 30min jitter)

[Timer]
OnCalendar=Sun *-*-* 04:30:00
RandomizedDelaySec=1800
Persistent=true
Unit=recyclarr.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now recyclarr.timer
systemctl --user list-timers recyclarr.timer --no-pager | head -3
UNITSCRIPT

sshm 'systemctl --user list-timers recyclarr.timer --no-pager 2>/dev/null | grep -q recyclarr.timer' || die "recyclarr.timer not scheduled"

log_info "Phase 34 complete — recyclarr installed + timer scheduled"
log_warn "smoke gate recyclarr-no-4k will fail until pre-existing default profiles"
log_warn "(sonarr2 Ultra-HD, radarr2 Any/Ultra-HD) are reviewed — see docs/operator-deferred.md"
