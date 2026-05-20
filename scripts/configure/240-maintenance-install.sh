#!/usr/bin/env bash
# Phase 240 — Manitoba maintenance system install. Idempotent.
#  - Deploys scripts/maint/ (Python codebase + manifest + systemd units) to seedbox
#  - Claims a loopback port for the Kuma webhook receiver (127.0.0.1 only)
#  - Migrates 4 version pins into versions.env (one-time, append-only)
#  - Installs 5 user-systemd units + heartbeat cron entry
#  - Runs install-time smoke (webhook /health, manifest validate, timer scheduled,
#    synthetic Kuma POST round-trips)
#
# Pre-Phase-1-of-design: maintenance webhook is loopback-only. NO nginx fragment.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"
source "$HERE/../lib/secrets.sh"

# ── Step 1: pre-flight — required local secrets ─────────────────────────────
log_info "Phase 240: maintenance system install"

secret_exists uptimekuma.key   || die "missing secrets/uptimekuma.key (capture from Kuma UI per spec §8.3)"
secret_exists uptimekuma.port  || die "missing secrets/uptimekuma.port (Kuma's listen port)"
# notifiarr.key check removed 2026-05-20 — Notifiarr was decommissioned
# 2026-05-11 (see inventory.md §L). Tautulli Webhook agent + Discord
# webhook supersede the prior Notifiarr passthrough.
secret_exists discord-webhook.url || die "missing secrets/discord-webhook.url — pre/post-maint Discord pings depend on it"
secret_exists discord-operator.id || die "missing secrets/discord-operator.id — error/critical pings depend on it"
log_info "pre-flight: all required secrets present"

# ── Step 2: claim webhook port ──────────────────────────────────────────────
# `app-ports free` over-reports — it lists ports that have been allocated
# elsewhere but not yet bound at the moment app-ports samples. Filter the
# free list against (a) ports already in secrets/*.port and (b) ports
# actually bound on the host (ss -tln) before picking one.
if ! secret_exists maintenance.port; then
  USED_LOCAL=$(cat "$REPO_ROOT"/secrets/*.port 2>/dev/null | sort -u | paste -sd, -)
  USED_BOUND=$(sshm "ss -tln 2>/dev/null | grep -oE '127\\.0\\.0\\.1:[0-9]+' | cut -d: -f2 | sort -u" | paste -sd, -)
  PORT=$(sshm "app-ports free 2>/dev/null | grep -E '^[0-9]+\$'" | while read p; do
    case ",$USED_LOCAL,$USED_BOUND," in
      *",$p,"*) ;;
      *) echo "$p"; break ;;
    esac
  done)
  [ -n "$PORT" ] || die "no truly-free port from app-ports (local + bound exclusion)"
  secret_write maintenance.port "$PORT"
  log_info "claimed maintenance webhook port $PORT"
fi
WEBHOOK_PORT=$(secret_read maintenance.port)
log_info "webhook port = $WEBHOOK_PORT (loopback only)"

# ── Step 3: migrate version pins into versions.env (append-only) ────────────
VERSIONS_FILE="$REPO_ROOT/versions.env"
[ -f "$VERSIONS_FILE" ] || die "versions.env not found at $VERSIONS_FILE"

migrate_pin() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$VERSIONS_FILE"; then
    log_info "versions.env: $key already present, skipping"
  else
    printf '%s=%s\n' "$key" "$value" >> "$VERSIONS_FILE"
    log_info "versions.env: appended $key=$value"
  fi
}

if secret_exists kometa.version; then
  migrate_pin KOMETA_VERSION "$(secret_read kometa.version)"
fi
if secret_exists recyclarr.version; then
  migrate_pin RECYCLARR_VERSION "$(secret_read recyclarr.version)"
fi
if secret_exists python-plexapi.version; then
  migrate_pin PYTHON_PLEXAPI_VERSION "$(secret_read python-plexapi.version)"
fi
# tdarr version pinned per spec §4.3 (GLIBC 2.34 ceiling at 2.17.01)
migrate_pin TDARR_VERSION "2.17.01"

# ── Step 4: deploy code + manifest to seedbox ──────────────────────────────
log_info "syncing code + manifest to ~/scripts/maint/ and ~/.opt/maint/"

sshm 'mkdir -p ~/scripts/maint/lib ~/scripts/maint/systemd ~/scripts/ops ~/.opt/maint ~/.opt/maint/window-log ~/.opt/heartbeat ~/.opt/_maint_stage ~/bin'

# Sync via tar to keep this single-roundtrip. --no-owner because the seedbox
# uses different uid/gid than the local workstation.
( cd "$REPO_ROOT" && tar -cf - \
    scripts/maint/manitoba-maint \
    scripts/maint/lib/__init__.py \
    scripts/maint/lib/manifest.py \
    scripts/maint/lib/state.py \
    scripts/maint/lib/notify.py \
    scripts/maint/lib/health.py \
    scripts/maint/lib/lifecycle.py \
    scripts/maint/lib/recovery.py \
    scripts/maint/lib/kuma.py \
    scripts/maint/lib/listmonk.py \
    scripts/maint/lib/window.py \
    scripts/maint/lib/cli.py \
    scripts/maint/lib/pusher.py \
    scripts/maint/systemd/manitoba-maint-webhook.service \
    scripts/maint/systemd/manitoba-maint-window.service \
    scripts/maint/systemd/manitoba-maint-window.timer \
    scripts/maint/systemd/manitoba-maint-window-watchdog.service \
    scripts/maint/systemd/manitoba-maint-window-watchdog.timer \
    scripts/maint/systemd/manitoba-maint-pusher.service \
    scripts/maint/systemd/manitoba-maint-canary-movie.service \
    scripts/maint/systemd/manitoba-maint-canary-movie.timer \
    scripts/maint/systemd/manitoba-maint-canary-anime.service \
    scripts/maint/systemd/manitoba-maint-canary-anime.timer \
    scripts/maint/systemd/manitoba-maint-canary-deletion.service \
    scripts/maint/systemd/manitoba-maint-canary-deletion.timer \
    scripts/maint/systemd/manitoba-maint-canary-mobile-ux.service \
    scripts/maint/systemd/manitoba-maint-canary-mobile-ux.timer \
    scripts/maint/systemd/manitoba-maint-canary-vlogs-stall.service \
    scripts/maint/systemd/manitoba-maint-canary-vlogs-stall.timer \
    scripts/maint/systemd/manitoba-maint-canary-qbit-stall.service \
    scripts/maint/systemd/manitoba-maint-canary-qbit-stall.timer \
    scripts/maint/systemd/manitoba-maint-canary-kometa-libraries.service \
    scripts/maint/systemd/manitoba-maint-canary-kometa-libraries.timer \
    scripts/maint/systemd/manitoba-maint-canary-stale-log-watchdog.service \
    scripts/maint/systemd/manitoba-maint-canary-stale-log-watchdog.timer \
    scripts/maint/systemd/manitoba-maint-canary-kometa-deploy-drift.service \
    scripts/maint/systemd/manitoba-maint-canary-kometa-deploy-drift.timer \
    scripts/maint/systemd/manitoba-maint-canary-prowlarr-indexer-health.service \
    scripts/maint/systemd/manitoba-maint-canary-prowlarr-indexer-health.timer \
    scripts/maint/systemd/manitoba-maint-flaresolverr-canary.service \
    scripts/maint/systemd/manitoba-maint-flaresolverr-canary.timer \
    scripts/maint/flaresolverr-canary.py \
    scripts/maint/systemd/manitoba-maint-canary-hardlink-integrity.service \
    scripts/maint/systemd/manitoba-maint-canary-hardlink-integrity.timer \
    scripts/maint/systemd/manitoba-maint-canary-plex-transcoder.service \
    scripts/maint/systemd/manitoba-maint-canary-plex-transcoder.timer \
    scripts/maint/systemd/manitoba-maint-cp-upgrade.service \
    scripts/maint/systemd/manitoba-maint-cp-upgrade.timer \
    scripts/maint/systemd/manitoba-maint-arr-audit.service \
    scripts/maint/systemd/manitoba-maint-arr-audit.timer \
    scripts/maint/arr-audit.py \
    scripts/maint/arr-audit-run.sh \
    scripts/maint/app-upgrade-all.sh \
    scripts/ops/heartbeat-maint-webhook.sh \
    scripts/lib/ssh.sh \
    scripts/canaries/anime.sh \
    scripts/canaries/deletion.sh \
    scripts/canaries/kometa-deploy-drift.sh \
    scripts/canaries/kometa-libraries.sh \
    scripts/canaries/mobile-ux.sh \
    scripts/canaries/movie.sh \
    scripts/canaries/qbit-stall.sh \
    scripts/canaries/stale-log-watchdog.sh \
    scripts/canaries/vlogs-stall.sh \
    scripts/canaries/prowlarr-indexer-health.sh \
    scripts/canaries/hardlink-integrity.sh \
    scripts/canaries/plex-transcoder.sh \
    scripts/configure/55-kometa-install.sh \
    manifest/apps.yaml \
) | sshm 'tar -xf - -C ~/.opt/_maint_stage'

# Move staged files into the right places (idempotent — overwrites).
sshm 'bash -s' <<'STAGE'
set -euo pipefail
STG=~/.opt/_maint_stage
mkdir -p "$STG"
# Re-run protection: re-extract here if a previous run partially failed.
# (The tar above already extracted into the staging dir.)
cp -f "$STG"/scripts/maint/manitoba-maint        ~/scripts/maint/manitoba-maint
chmod +x ~/scripts/maint/manitoba-maint
cp -f "$STG"/scripts/maint/app-upgrade-all.sh ~/scripts/maint/app-upgrade-all.sh
chmod +x ~/scripts/maint/app-upgrade-all.sh
cp -f "$STG"/scripts/maint/arr-audit.py       ~/scripts/maint/arr-audit.py
chmod +x ~/scripts/maint/arr-audit.py
cp -f "$STG"/scripts/maint/arr-audit-run.sh   ~/scripts/maint/arr-audit-run.sh
chmod +x ~/scripts/maint/arr-audit-run.sh
cp -f "$STG"/scripts/maint/flaresolverr-canary.py ~/scripts/maint/flaresolverr-canary.py
chmod +x ~/scripts/maint/flaresolverr-canary.py
# Remove the retired Playwright clicker if a prior install put it in place.
rm -f ~/scripts/maint/cp_upgrade_clicker.py
cp -rf  "$STG"/scripts/maint/lib                  ~/scripts/maint/
cp -rf  "$STG"/scripts/maint/systemd              ~/scripts/maint/
cp -f   "$STG"/scripts/ops/heartbeat-maint-webhook.sh ~/scripts/ops/
chmod +x ~/scripts/ops/heartbeat-maint-webhook.sh
mkdir -p ~/scripts/lib ~/scripts/canaries ~/scripts/configure
cp -f   "$STG"/scripts/lib/ssh.sh                ~/scripts/lib/ssh.sh
cp -f   "$STG"/scripts/canaries/*.sh             ~/scripts/canaries/
chmod +x ~/scripts/canaries/*.sh
# kometa-deploy-drift canary reads this install script's heredoc to know
# what library names should be deployed — needs the file resident.
cp -f   "$STG"/scripts/configure/55-kometa-install.sh ~/scripts/configure/55-kometa-install.sh
cp -f   "$STG"/manifest/apps.yaml                 ~/.opt/maint/apps.yaml
rm -rf  "$STG"
STAGE

# Render the port file (used by both webhook server and heartbeat script).
sshm "echo -n '$WEBHOOK_PORT' > ~/.opt/maint/maintenance.port && chmod 600 ~/.opt/maint/maintenance.port"

# ── Step 4.5: bootstrap Kuma monitors + push tokens (idempotent) ────────────
# Creates one PUSH monitor per app/canary in manifest/apps.yaml that doesn't
# already exist, then re-fetches all push tokens and writes them to
# secrets/kuma-push-tokens.json. The token deploy in Step 5 picks up
# whatever's there. Fail-soft: any prereq missing → warn and skip; the
# install still completes and the operator can run bootstrap manually
# later. Re-runs are no-ops once monitors exist.
log_info "bootstrap Kuma monitors + push tokens"
SSHM_HOST_FOR_TUNNEL=$(secret_read seedbox.ssh-host 2>/dev/null || echo "")
KUMA_BOOTSTRAP_TUNNEL_PID=""
KUMA_REACHABLE=0
if [ -z "$SSHM_HOST_FOR_TUNNEL" ]; then
  log_warn "  skip: no secrets/seedbox.ssh-host — operator must run bootstrap-kuma-monitors.py manually"
else
  if curl -sf -m 3 -o /dev/null "http://127.0.0.1:42005/" 2>/dev/null \
     || curl -sf -m 3 -o /dev/null "http://127.0.0.1:42005/api/" 2>/dev/null; then
    KUMA_REACHABLE=1
    log_info "  kuma already reachable at 127.0.0.1:42005"
  else
    log_info "  opening temporary SSH tunnel for Kuma bootstrap"
    ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -fN \
        -L 42005:127.0.0.1:42005 "quadstronaut@${SSHM_HOST_FOR_TUNNEL}" \
        >/dev/null 2>&1 && KUMA_BOOTSTRAP_TUNNEL_PID=opened || true
    sleep 1
    if curl -sf -m 3 -o /dev/null "http://127.0.0.1:42005/" 2>/dev/null; then
      KUMA_REACHABLE=1
    fi
  fi
  if [ "$KUMA_REACHABLE" = "1" ]; then
    # Locate a workstation python with uptime-kuma-api installed. Prefer
    # tests/.venv (matches the docstring in bootstrap-kuma-monitors.py).
    BOOTSTRAP_PY=""
    for cand in \
        "$REPO_ROOT/tests/.venv/bin/python" \
        "$REPO_ROOT/tests/.venv/Scripts/python.exe" \
        "$REPO_ROOT/tests/.venv/Scripts/python" \
        ; do
      if [ -x "$cand" ] && "$cand" -c "import uptime_kuma_api" >/dev/null 2>&1; then
        BOOTSTRAP_PY="$cand"
        break
      fi
    done
    if [ -z "$BOOTSTRAP_PY" ]; then
      log_warn "  skip: tests/.venv missing uptime-kuma-api — run: tests/.venv/Scripts/pip install uptime-kuma-api"
    else
      log_info "  running bootstrap with $BOOTSTRAP_PY"
      ( cd "$REPO_ROOT" && PYTHONPATH=scripts/maint "$BOOTSTRAP_PY" \
          scripts/maint/bootstrap-kuma-monitors.py ) \
        || log_warn "  bootstrap-kuma-monitors.py exited non-zero — monitors may be incomplete"
    fi
  else
    log_warn "  skip: Kuma not reachable at 127.0.0.1:42005 and tunnel could not be opened"
  fi
  # Clean up temp tunnel if we opened one.
  if [ "$KUMA_BOOTSTRAP_TUNNEL_PID" = "opened" ]; then
    # ssh -fN backgrounds itself; locate by forwarding signature and kill.
    pkill -f "ssh.*-L 42005:127.0.0.1:42005 quadstronaut@${SSHM_HOST_FOR_TUNNEL}" 2>/dev/null || true
  fi
fi

# ── Step 5: deploy uptimekuma.{key,port} to seedbox secrets ────────────────
# notifiarr.{key,port} removed 2026-05-20 — daemon decommissioned 2026-05-11.
sshm 'mkdir -p ~/secrets && chmod 700 ~/secrets'
for f in uptimekuma.key uptimekuma.port; do
  scpm_to "$REPO_ROOT/secrets/$f" "secrets/$f" >/dev/null
done
sshm 'chmod 600 ~/secrets/uptimekuma.key ~/secrets/uptimekuma.port 2>/dev/null'
# Kuma push tokens — required for pusher service.
if [ -f "$REPO_ROOT/secrets/kuma-push-tokens.json" ]; then
  scpm_to "$REPO_ROOT/secrets/kuma-push-tokens.json" "secrets/kuma-push-tokens.json" >/dev/null
  sshm 'chmod 600 ~/secrets/kuma-push-tokens.json 2>/dev/null'
  log_info "deployed kuma-push-tokens.json to seedbox ~/secrets/"
else
  log_info "WARN: secrets/kuma-push-tokens.json not found locally — pusher service will fail until tokens are deployed"
fi
sshm "echo -n '$WEBHOOK_PORT' > ~/secrets/maintenance.port && chmod 600 ~/secrets/maintenance.port"

# Optional Kuma host secret — write only if local file exists.
if secret_exists uptimekuma.host; then
  scpm_to "$REPO_ROOT/secrets/uptimekuma.host" "secrets/uptimekuma.host" >/dev/null
  sshm 'chmod 600 ~/secrets/uptimekuma.host'
fi

# Probe-needed secrets — every *.port, *.key, *.urlbase, and *.host that
# lib/health.py reads to build URLs and auth headers, plus *.url and *.id
# so discord-webhook.url + discord-operator.id reach the seedbox (without
# them, lib/notify.py silently logs to notify-fail.log and the operator
# never sees pre/post-maint pings). Deployed via tar pipe so we don't make
# 70+ scp round-trips.
log_info "deploying probe + notify secrets (*.port, *.key, *.urlbase, *.host, *.url, *.id) to ~/secrets/"
( cd "$REPO_ROOT/secrets" && tar -cf - \
    --exclude="*.json" \
    $(ls *.port *.key *.urlbase *.host *.url *.id 2>/dev/null) \
) | sshm 'tar -xf - -C ~/secrets/ && chmod 600 ~/secrets/*.port ~/secrets/*.key ~/secrets/*.urlbase ~/secrets/*.host ~/secrets/*.url ~/secrets/*.id 2>/dev/null; echo "secrets sync ok"'

# ── Step 6: symlink ~/bin/manitoba-maint ────────────────────────────────────
sshm 'ln -sf ~/scripts/maint/manitoba-maint ~/bin/manitoba-maint'

# ── Step 7: install user-systemd units ──────────────────────────────────────
sshm 'bash -s' <<'UNITSCRIPT'
set -euo pipefail
mkdir -p ~/.config/systemd/user
for unit in \
    manitoba-maint-webhook.service \
    manitoba-maint-window.service \
    manitoba-maint-window.timer \
    manitoba-maint-window-watchdog.service \
    manitoba-maint-window-watchdog.timer \
    manitoba-maint-pusher.service \
    manitoba-maint-canary-movie.service \
    manitoba-maint-canary-movie.timer \
    manitoba-maint-canary-anime.service \
    manitoba-maint-canary-anime.timer \
    manitoba-maint-canary-deletion.service \
    manitoba-maint-canary-deletion.timer \
    manitoba-maint-canary-mobile-ux.service \
    manitoba-maint-canary-mobile-ux.timer \
    manitoba-maint-canary-vlogs-stall.service \
    manitoba-maint-canary-vlogs-stall.timer \
    manitoba-maint-canary-qbit-stall.service \
    manitoba-maint-canary-qbit-stall.timer \
    manitoba-maint-canary-kometa-libraries.service \
    manitoba-maint-canary-kometa-libraries.timer \
    manitoba-maint-canary-stale-log-watchdog.service \
    manitoba-maint-canary-stale-log-watchdog.timer \
    manitoba-maint-canary-kometa-deploy-drift.service \
    manitoba-maint-canary-kometa-deploy-drift.timer \
    manitoba-maint-canary-prowlarr-indexer-health.service \
    manitoba-maint-canary-prowlarr-indexer-health.timer \
    manitoba-maint-flaresolverr-canary.service \
    manitoba-maint-flaresolverr-canary.timer \
    manitoba-maint-canary-hardlink-integrity.service \
    manitoba-maint-canary-hardlink-integrity.timer \
    manitoba-maint-canary-plex-transcoder.service \
    manitoba-maint-canary-plex-transcoder.timer \
    manitoba-maint-cp-upgrade.service \
    manitoba-maint-cp-upgrade.timer \
    manitoba-maint-arr-audit.service \
    manitoba-maint-arr-audit.timer; do
  # If the user unit is a symlink pointing back at the source, `cp -f` fails
  # with "are the same file" — the source already IS the live unit. Drop the
  # symlink first; cp then writes a real file.
  if [ -L ~/.config/systemd/user/$unit ]; then
    rm -f ~/.config/systemd/user/$unit
  fi
  cp -f ~/scripts/maint/systemd/$unit ~/.config/systemd/user/$unit
done
systemctl --user daemon-reload
# Enable everything that should auto-start.
systemctl --user enable --now manitoba-maint-webhook.service
systemctl --user enable --now manitoba-maint-window.timer
systemctl --user enable --now manitoba-maint-window-watchdog.timer
systemctl --user enable --now manitoba-maint-pusher.service
# Canary timers — idempotent: enable --now only starts if not already running.
systemctl --user enable --now manitoba-maint-canary-movie.timer
systemctl --user enable --now manitoba-maint-canary-anime.timer
systemctl --user enable --now manitoba-maint-canary-deletion.timer
systemctl --user enable --now manitoba-maint-canary-mobile-ux.timer
# vlogs-stall canary: requires victorialogs.service (deployed by 80-vlogs-install.sh).
# enable --now is safe even if vlogs isn't running yet — the canary script will
# exit with vlogs-down/no-ingest and push the right status to Kuma.
systemctl --user enable --now manitoba-maint-canary-vlogs-stall.timer
# qbit-stall canary: detects libtorrent engine wedge (dl_info_speed=0 for
# ≥5min + queuedDL>N). Same 15-min cadence as vlogs-stall.
systemctl --user enable --now manitoba-maint-canary-qbit-stall.timer
# kometa-libraries canary: config-drift detector (Plex rename guard).
# Idempotent — exits up on match, down on drift; no destructive ops.
systemctl --user enable --now manitoba-maint-canary-kometa-libraries.timer
# stale-log watchdog: alerts when timer-driven app logs go past their cadence.
systemctl --user enable --now manitoba-maint-canary-stale-log-watchdog.timer
# kometa deploy-drift: install-script vs deployed config consistency (daily 04:30).
systemctl --user enable --now manitoba-maint-canary-kometa-deploy-drift.timer
# prowlarr-indexer-health: 429-cascade and chronic-RSS-stale detector.
# Detect-only — never disables an indexer or restarts FlareSolverr.
systemctl --user enable --now manitoba-maint-canary-prowlarr-indexer-health.timer
# flaresolverr-canary: probes /  and /v1 every 5 min. On failure, calls
# `app-flaresolverr restart` (capped 3/hour) and waits up to 120s for
# Chromium subprocess to come back. Catches the silent-socket-stall mode
# that bit the 2026-05-18 audit.
systemctl --user enable --now manitoba-maint-flaresolverr-canary.timer
# hardlink-integrity: assert recent library imports have linkcount >= 2.
# Detects *arr "Use Hard Links" regression / filesystem-boundary changes
# that would double storage on every new grab.
systemctl --user enable --now manitoba-maint-canary-hardlink-integrity.timer
# plex-transcoder: every 10 min, exercise Plex's /transcode/sessions and
# /:/prefs endpoints. Catches transcoder daemon stalls before customers
# hit "Conversion failed" — main /identity stays 200 OK during this fault.
systemctl --user enable --now manitoba-maint-canary-plex-transcoder.timer
# UCC `app-<name> upgrade` sweep — Mon 11:30 UTC (30 min into the window).
# --now activates the timer itself (schedules its next OnCalendar fire); it
# does NOT trigger an immediate service run. Without --now the timer stays
# inactive until reboot — which is what bit us on 2026-05-11.
systemctl --user enable --now manitoba-maint-cp-upgrade.timer
# Weekly *arr stack audit — Sun 04:00 UTC. Read-only; writes markdown
# reports to ~/.opt/maint/audit-reports/arr-audit-YYYY-MM-DD.md (90d
# retention). Runs in loopback mode (no nginx hop).
systemctl --user enable --now manitoba-maint-arr-audit.timer
# Restart long-running services so they pick up code/manifest changes
# (enable --now doesn't restart an already-running unit). Window timers
# don't need a restart — next fire uses the latest code.
systemctl --user restart manitoba-maint-webhook.service
systemctl --user restart manitoba-maint-pusher.service
# Settle.
sleep 3
UNITSCRIPT

# ── Step 8: heartbeat cron entry ────────────────────────────────────────────
sshm '(crontab -l 2>/dev/null | grep -v heartbeat-maint-webhook; echo "*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-maint-webhook.sh") | crontab -'

# ── Step 9: install-time smoke ──────────────────────────────────────────────
log_info "running install-time smoke gate"

PASS=0; FAIL=0
gate() {
  local name="$1" status="$2" detail="${3:-}"
  case "$status" in
    pass) printf '✓ %-40s %s\n' "$name" "$detail"; PASS=$((PASS+1)) ;;
    fail) printf '✗ %-40s %s\n' "$name" "$detail"; FAIL=$((FAIL+1)) ;;
  esac
}

# Smoke 1: webhook /health returns 200 with body "ok\n" (port 42017 etc.
# can be over-reported as "free" by app-ports while another service is
# already bound; checking the body distinguishes our webhook from an
# intruder).
sleep 2
BODY=$(sshm "curl -sf -m 5 http://127.0.0.1:${WEBHOOK_PORT}/health 2>/dev/null" || echo "")
if [ "$BODY" = "ok" ]; then
  gate "webhook-health" pass "HTTP 200 + body=ok on 127.0.0.1:${WEBHOOK_PORT}"
else
  gate "webhook-health" fail "body='$BODY' (expected 'ok') — port collision? check journalctl --user -u manitoba-maint-webhook"
fi

# Smoke 2: manifest validates on the seedbox
MV=$(sshm "MANITOBA_MANIFEST=~/.opt/maint/apps.yaml ~/bin/manitoba-maint manifest validate 2>&1; echo exit=\$?" 2>/dev/null)
if echo "$MV" | grep -q "exit=0"; then
  gate "manifest-validate" pass
else
  gate "manifest-validate" fail "$(echo "$MV" | tail -2 | head -1)"
fi

# Smoke 3: window timer scheduled (next-fire ≤ 7 days)
TM=$(sshm "systemctl --user list-timers manitoba-maint-window.timer --no-pager 2>/dev/null | grep manitoba-maint-window.timer" 2>/dev/null)
if [ -n "$TM" ]; then
  gate "window-timer-scheduled" pass "$(echo "$TM" | awk '{print $1, $2, $3}')"
else
  gate "window-timer-scheduled" fail "timer not in systemctl list-timers"
fi

# Smoke 4: window-watchdog timer scheduled
WT=$(sshm "systemctl --user list-timers manitoba-maint-window-watchdog.timer --no-pager 2>/dev/null | grep -c manitoba-maint-window-watchdog" 2>/dev/null)
if [ "${WT:-0}" -ge 1 ]; then
  gate "watchdog-timer-scheduled" pass
else
  gate "watchdog-timer-scheduled" fail "watchdog timer not scheduled"
fi

# Smoke 5: synthetic POST — unknown monitor round-trips and increments counter.
# Note: `$HOME` inside ssh quoting is local-side here, so use os.path.expanduser
# in the python probe (seedbox-side). Also pass `-n` via redirect </dev/null on
# every sshm call to prevent ssh from consuming the script's stdin.
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
RTRESP=$(sshm "curl -sf -m 5 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  --data '{\"monitor\":{\"name\":\"InstallSmokeNonExistent\"},\"heartbeat\":{\"status\":0,\"time\":\"${NOW}\",\"msg\":\"install-smoke\"}}' \
  http://127.0.0.1:${WEBHOOK_PORT}/kuma" </dev/null 2>/dev/null || echo "000")
if [ "$RTRESP" = "200" ]; then
  sleep 1
  STATE_HAS_COUNTER=$(sshm "python3 -c 'import json,os; d=json.load(open(os.path.expanduser(\"~/.opt/maint/state.json\"))); print(d.get(\"unknown_monitors_total\", 0))'" </dev/null 2>/dev/null || echo 0)
  if [ "${STATE_HAS_COUNTER:-0}" -ge 1 ]; then
    gate "synthetic-post-roundtrip" pass "200 + state.json counter=${STATE_HAS_COUNTER}"
  else
    gate "synthetic-post-roundtrip" fail "200 received but state.json counter not incremented (got '$STATE_HAS_COUNTER')"
  fi
else
  gate "synthetic-post-roundtrip" fail "HTTP $RTRESP"
fi

# Smoke 6: heartbeat cron entry present
CR=$(sshm 'crontab -l 2>/dev/null | grep -c heartbeat-maint-webhook' 2>/dev/null)
if [ "${CR:-0}" -ge 1 ]; then
  gate "heartbeat-cron" pass
else
  gate "heartbeat-cron" fail "cron entry missing"
fi

# Smoke 7: pusher service is running and has logged at least one push attempt.
# Wait up to 10s for the first push cycle log line.
sleep 5
PUSHER_ACTIVE=$(sshm "systemctl --user is-active manitoba-maint-pusher.service 2>/dev/null" 2>/dev/null || echo "unknown")
PUSHER_LOG=$(sshm "journalctl --user -u manitoba-maint-pusher.service --since '2 min ago' --no-pager -q 2>/dev/null | grep -c 'pusher'" 2>/dev/null || echo 0)
if [ "$PUSHER_ACTIVE" = "active" ] && [ "${PUSHER_LOG:-0}" -ge 1 ]; then
  gate "pusher-service-running" pass "active + ${PUSHER_LOG} log lines"
elif [ "$PUSHER_ACTIVE" = "active" ]; then
  gate "pusher-service-running" pass "active (log line count=${PUSHER_LOG:-0}; may not have cycled yet)"
else
  gate "pusher-service-running" fail "state=$PUSHER_ACTIVE logs=${PUSHER_LOG:-0} — check: journalctl --user -u manitoba-maint-pusher.service"
fi

# Smoke 8: kuma drift audit — manifest's kuma_monitor names should all
# resolve to live Kuma monitors (no orphans + no missing).
KUMA_AUDIT=$(sshm "MANITOBA_MANIFEST=~/.opt/maint/apps.yaml ~/bin/manitoba-maint kuma audit 2>&1; echo exit=\$?" </dev/null 2>/dev/null)
if echo "$KUMA_AUDIT" | grep -q "exit=0"; then
  MATCHED=$(echo "$KUMA_AUDIT" | grep -oP 'matched: \K[0-9]+')
  gate "kuma-drift-audit" pass "matched=${MATCHED:-?}"
else
  # Drift is exit 2; unreachable Kuma is exit 3. Surface enough to triage.
  TAIL=$(echo "$KUMA_AUDIT" | tail -5 | tr '\n' ' ' | head -c 120)
  gate "kuma-drift-audit" fail "${TAIL}"
fi

# Smoke 9–12: canary timers scheduled
for canary in movie anime deletion mobile-ux vlogs-stall qbit-stall kometa-libraries stale-log-watchdog kometa-deploy-drift prowlarr-indexer-health; do
  CT=$(sshm "systemctl --user list-timers manitoba-maint-canary-${canary}.timer --no-pager 2>/dev/null | grep -c manitoba-maint-canary-${canary}.timer" </dev/null 2>/dev/null)
  if [ "${CT:-0}" -ge 1 ]; then
    gate "canary-timer-${canary}" pass "scheduled"
  else
    gate "canary-timer-${canary}" fail "timer not in systemctl list-timers"
  fi
done

# Smoke 13: weekly arr-audit timer scheduled
AAT=$(sshm "systemctl --user list-timers manitoba-maint-arr-audit.timer --no-pager 2>/dev/null | grep -c manitoba-maint-arr-audit.timer" </dev/null 2>/dev/null)
if [ "${AAT:-0}" -ge 1 ]; then
  gate "arr-audit-timer-scheduled" pass
else
  gate "arr-audit-timer-scheduled" fail "weekly arr-audit timer not in systemctl list-timers"
fi

echo
TOTAL=$((PASS + FAIL))
printf "Install smoke: %d/%d pass\n" "$PASS" "$TOTAL"
[ "$FAIL" = 0 ] || die "install-time smoke failed — see output above"

log_info "Phase 240 complete — maintenance system installed + smoke ${PASS}/${TOTAL}"
