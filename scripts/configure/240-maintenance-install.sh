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

# ~/.opt/maint/dash-asset-integrity/ is the dash-asset-integrity canary's own
# state dir: heal-latch.epoch (the 1-per-24h self-heal breaker), the durable
# per-day heal log, and events/YYYY-MM-DD.jsonl (one JSON object per heal
# attempt). Pre-created here for the same reason the reaper's dir is: the journal
# on this shared slot is permission-restricted and rotation-prone, so a
# self-owned logfile is the only reliable audit trail of an autonomous restart.
sshm 'mkdir -p ~/scripts/maint/lib ~/scripts/maint/systemd ~/scripts/ops ~/.opt/maint ~/.opt/maint/window-log ~/.opt/maint/dash-asset-integrity/events ~/.opt/heartbeat ~/.opt/_maint_stage ~/bin'

# Sync via tar to keep this single-roundtrip. --no-owner because the seedbox
# uses different uid/gid than the local workstation.
( cd "$REPO_ROOT" && tar -cf - \
    scripts/maint/manitoba-maint \
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
    scripts/maint/lib/secrets.py \
    scripts/maint/lib/fleet.py \
    scripts/maint/lib/suppression.py \
    scripts/maint/lib/qbit.py \
    scripts/maint/lib/deep_check.py \
    scripts/maint/lib/ucc.py \
    scripts/maint/lib/ucc_incident.py \
    scripts/maint/lib/ucc_response.py \
    scripts/maint/prune-app-backups.sh \
    scripts/maint/qflix-collect.py \
    scripts/maint/systemd/qflix-collect.service \
    scripts/maint/systemd/qflix-collect.timer \
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
    scripts/maint/systemd/manitoba-maint-canary-prowlarr-app-sync.service \
    scripts/maint/systemd/manitoba-maint-canary-prowlarr-app-sync.timer \
    scripts/maint/systemd/manitoba-maint-canary-plex-unmatched.service \
    scripts/maint/systemd/manitoba-maint-canary-plex-unmatched.timer \
    scripts/maint/systemd/manitoba-maint-canary-rea-liveness.service \
    scripts/maint/systemd/manitoba-maint-canary-rea-liveness.timer \
    scripts/maint/systemd/manitoba-maint-flaresolverr-canary.service \
    scripts/maint/systemd/manitoba-maint-flaresolverr-canary.timer \
    scripts/maint/flaresolverr-canary.py \
    scripts/maint/systemd/manitoba-maint-canary-hardlink-integrity.service \
    scripts/maint/systemd/manitoba-maint-canary-hardlink-integrity.timer \
    scripts/maint/systemd/manitoba-maint-canary-plex-transcoder.service \
    scripts/maint/systemd/manitoba-maint-canary-plex-transcoder.timer \
    scripts/maint/systemd/manitoba-maint-canary-quota.service \
    scripts/maint/systemd/manitoba-maint-canary-quota.timer \
    scripts/maint/systemd/manitoba-maint-canary-tautulli-plex-link.service \
    scripts/maint/systemd/manitoba-maint-canary-tautulli-plex-link.timer \
    scripts/maint/systemd/manitoba-maint-canary-newsletter-digest.service \
    scripts/maint/systemd/manitoba-maint-canary-newsletter-digest.timer \
    scripts/maint/systemd/manitoba-maint-cp-upgrade.service \
    scripts/maint/systemd/manitoba-maint-arr-audit.service \
    scripts/maint/systemd/manitoba-maint-arr-audit.timer \
    scripts/maint/systemd/manitoba-maint-ucc-detect.service \
    scripts/maint/systemd/manitoba-maint-ucc-detect.timer \
    scripts/maint/systemd/manitoba-maint-backup-prune.service \
    scripts/maint/systemd/manitoba-maint-backup-prune.timer \
    scripts/maint/systemd/manitoba-maint-anime-janitor.service \
    scripts/maint/systemd/manitoba-maint-anime-janitor.timer \
    scripts/maint/qflix-anime-janitor.py \
    scripts/maint/qflix-anime-janitor.exclude \
    scripts/maint/qflix-reaper.py \
    scripts/maint/qflix-reaper.exclude \
    scripts/maint/audio-disposition-janitor.py \
    scripts/maint/functional-audit.py \
    scripts/maint/bootstrap-kuma-monitors.py \
    scripts/maint/qflix-torrent-janitor.py \
    scripts/maint/qflix-torrent-janitor.exclude \
    scripts/maint/systemd/manitoba-maint-audit.service \
    scripts/maint/systemd/manitoba-maint-audit.timer \
    scripts/maint/qflix-audit-live.py \
    scripts/maint/systemd/manitoba-maint-audit-live.service \
    scripts/maint/systemd/manitoba-maint-audit-live.timer \
    scripts/maint/systemd/manitoba-maint-torrent-janitor.service \
    scripts/maint/systemd/manitoba-maint-torrent-janitor.timer \
    scripts/maint/systemd/manitoba-maint-canary-thread-ceiling.service \
    scripts/maint/systemd/manitoba-maint-canary-thread-ceiling.timer \
    scripts/maint/systemd/manitoba-maint-canary-sab-stall.service \
    scripts/maint/systemd/manitoba-maint-canary-sab-stall.timer \
    scripts/maint/systemd/manitoba-maint-canary-tdarr-scanner.service \
    scripts/maint/systemd/manitoba-maint-canary-tdarr-scanner.timer \
    scripts/maint/systemd/manitoba-maint-canary-tdarr-healthcheck.service \
    scripts/maint/systemd/manitoba-maint-canary-tdarr-healthcheck.timer \
    scripts/maint/systemd/manitoba-maint-canary-ucc-gate-stuck.service \
    scripts/maint/systemd/manitoba-maint-canary-ucc-gate-stuck.timer \
    scripts/maint/systemd/manitoba-maint-canary-dash-asset-integrity.service \
    scripts/maint/systemd/manitoba-maint-canary-dash-asset-integrity.timer \
    scripts/canaries/timer-liveness.sh \
    scripts/canaries/deploy-drift.sh \
    scripts/maint/systemd/manitoba-maint-canary-timer-liveness.service \
    scripts/maint/systemd/manitoba-maint-canary-timer-liveness.timer \
    scripts/maint/systemd/manitoba-maint-canary-deploy-drift.service \
    scripts/maint/systemd/manitoba-maint-canary-deploy-drift.timer \
    scripts/maint/systemd/manitoba-maint-boot-listeners.service \
    scripts/maint/arr-audit.py \
    scripts/maint/arr-audit-run.sh \
    scripts/maint/app-upgrade-all.sh \
    scripts/ops/heartbeat-maint-webhook.sh \
    scripts/ops/boot-listeners-snapshot.sh \
    scripts/lib/ssh.sh \
    scripts/canaries/anime.sh \
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
    scripts/canaries/quota.sh \
    scripts/canaries/tautulli-plex-link.sh \
    scripts/canaries/newsletter-digest-stale.sh \
    scripts/canaries/thread-ceiling.sh \
    scripts/canaries/sab-stall.sh \
    scripts/canaries/tdarr-scanner.sh \
    scripts/canaries/tdarr-healthcheck.sh \
    scripts/canaries/ucc-gate-stuck.sh \
    scripts/canaries/dash-asset-integrity.sh \
    scripts/canaries/prowlarr-app-sync.sh \
    scripts/canaries/plex-unmatched.sh \
    scripts/canaries/rea-liveness.sh \
    scripts/configure/55-kometa-install.sh \
    manifest/apps.yaml \
    manifest/jobs.yaml \
    manifest/rea-noise-classes.yaml \
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
cp -f "$STG"/scripts/maint/prune-app-backups.sh ~/scripts/maint/prune-app-backups.sh
chmod +x ~/scripts/maint/prune-app-backups.sh
# QFlix hourly collector — migrated off the workstation 2026-07-09 so the
# "QFlix Collect" Kuma dead-man + autonomous unstick loop no longer depend on
# the operator PC being on (was a customer-visible false red on the status page).
cp -f "$STG"/scripts/maint/qflix-collect.py ~/scripts/maint/qflix-collect.py
chmod +x ~/scripts/maint/qflix-collect.py
# QFlix torrent janitor — purge completed *arr-untracked seeding leftovers
# (added 2026-07-27). Ships DRY-RUN; armed via an on-box drop-in like the reaper.
cp -f "$STG"/scripts/maint/qflix-torrent-janitor.py ~/scripts/maint/qflix-torrent-janitor.py
chmod +x ~/scripts/maint/qflix-torrent-janitor.py

# COUNCIL FINDING: the box was running DIFFERENT code from the repo for
# these. qflix-anime-janitor.py was staged into $STG and never copied out;
# qflix-reaper.py (which DELETES media), audio-disposition-janitor.py and
# functional-audit.py were not staged at all. "What is running" silently
# diverged from "what is in git" -- the exact gap the live auditor exists to
# catch, and it caught it. The installer is now the single deploy path.
cp -f "$STG"/scripts/maint/qflix-anime-janitor.py ~/scripts/maint/qflix-anime-janitor.py
chmod +x ~/scripts/maint/qflix-anime-janitor.py
cp -f "$STG"/scripts/maint/qflix-anime-janitor.exclude ~/scripts/maint/qflix-anime-janitor.exclude 2>/dev/null || true
cp -f "$STG"/scripts/maint/qflix-reaper.py ~/scripts/maint/qflix-reaper.py
chmod +x ~/scripts/maint/qflix-reaper.py
cp -f "$STG"/scripts/maint/qflix-reaper.exclude ~/scripts/maint/qflix-reaper.exclude 2>/dev/null || true
cp -f "$STG"/scripts/maint/audio-disposition-janitor.py ~/scripts/maint/audio-disposition-janitor.py
chmod +x ~/scripts/maint/audio-disposition-janitor.py
cp -f "$STG"/scripts/maint/functional-audit.py ~/scripts/maint/functional-audit.py
chmod +x ~/scripts/maint/functional-audit.py
# Kept in sync rather than left stale: it CREATES Kuma monitors, and a
# day-old copy on the box is more dangerous than a current one. No timer
# runs it; it is operator-invoked.
cp -f "$STG"/scripts/maint/bootstrap-kuma-monitors.py ~/scripts/maint/bootstrap-kuma-monitors.py
chmod +x ~/scripts/maint/bootstrap-kuma-monitors.py

# The LIVE half of the audit regime runs ON the box against the deployed tree
# (it reads staged units, kuma.db, secrets and quota), so unlike qflix-audit.py
# -- which needs a git checkout and runs from ~/.opt/qflix-src -- it IS copied.
cp -f "$STG"/scripts/maint/qflix-audit-live.py ~/scripts/maint/qflix-audit-live.py
chmod +x ~/scripts/maint/qflix-audit-live.py

# Convergent Audit Regime (landed on master 2026-07-30). Its units sat in the
# repo with ZERO installer wiring, so the audit would have shipped as a guard
# that is committed but never scheduled -- the exact C-01/C-10 class it exists
# to enumerate.
#
# It is NOT copied into ~/scripts/maint: the audit needs a real git checkout
# (its boundary is `git ls-files`) and a manifest/ dir above the script, and
# the deployed layout has neither. It runs out of ~/.opt/qflix-src instead --
# the same shallow checkout qflix-stats.py maintains. Ensure it exists here so
# the timer never fires against a missing directory.
if [ ! -d "$HOME/.opt/qflix-src/.git" ]; then
  git clone --depth 1 https://github.com/Quadstronaut/QFlix.git "$HOME/.opt/qflix-src" >/dev/null 2>&1 \
    && echo "[+] cloned ~/.opt/qflix-src for the audit regime" \
    || echo "[!] could not clone ~/.opt/qflix-src - audit timer will fail until it exists"
else
  echo "[+] ~/.opt/qflix-src present for the audit regime"
fi
cp -f "$STG"/scripts/maint/qflix-torrent-janitor.exclude ~/scripts/maint/qflix-torrent-janitor.exclude
# Remove the retired Playwright clicker if a prior install put it in place.
rm -f ~/scripts/maint/cp_upgrade_clicker.py
# Remove maint/lib/__init__.py if a prior install put it in place. The 2026-
# 05-12 namespace-package fix (commit 1242706) requires it absent so the
# `lib.X` import root can simultaneously resolve scripts/maint/lib AND
# scripts/mcp/lib. A re-introduction on 2026-05-20 (A6 install repair) broke
# `from lib.qbit_client import` in collect.py — undo on every install so
# stale boxes self-heal.
rm -f ~/scripts/maint/lib/__init__.py
cp -rf  "$STG"/scripts/maint/lib                  ~/scripts/maint/
cp -rf  "$STG"/scripts/maint/systemd              ~/scripts/maint/
cp -f   "$STG"/scripts/ops/heartbeat-maint-webhook.sh ~/scripts/ops/
chmod +x ~/scripts/ops/heartbeat-maint-webhook.sh
cp -f   "$STG"/scripts/ops/boot-listeners-snapshot.sh ~/scripts/ops/
chmod +x ~/scripts/ops/boot-listeners-snapshot.sh
mkdir -p ~/scripts/lib ~/scripts/canaries ~/scripts/configure
cp -f   "$STG"/scripts/lib/ssh.sh                ~/scripts/lib/ssh.sh
cp -f   "$STG"/scripts/canaries/*.sh             ~/scripts/canaries/
chmod +x ~/scripts/canaries/*.sh
# kometa-deploy-drift canary reads this install script's heredoc to know
# what library names should be deployed — needs the file resident.
cp -f   "$STG"/scripts/configure/55-kometa-install.sh ~/scripts/configure/55-kometa-install.sh
cp -f   "$STG"/manifest/apps.yaml                 ~/.opt/maint/apps.yaml
# jobs.yaml is the timer<->dead-man ledger the timer-liveness canary reads. The
# box has no repo checkout, so it must be staged flat like apps.yaml.
cp -f   "$STG"/manifest/jobs.yaml                 ~/.opt/maint/jobs.yaml
# rea-noise-classes.yaml carries `deadman_reasons` — the vocabulary of REA
# failure reasons, mirrored from qflix-rea.ps1's $Script:DeadmanReasons for
# audit detector C-09. The rea-liveness canary reads it to LABEL a failure
# reason known-vs-drift. It degrades gracefully when absent (still reds on a
# fail, and counts the degradation as `reason-table-unavailable`), but staging
# it costs one file and restores the enrichment — and keeps the box's copy from
# drifting away from the PowerShell constant it mirrors.
cp -f   "$STG"/manifest/rea-noise-classes.yaml    ~/.opt/maint/rea-noise-classes.yaml
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
    manitoba-maint-canary-prowlarr-app-sync.service \
    manitoba-maint-canary-prowlarr-app-sync.timer \
    manitoba-maint-canary-plex-unmatched.service \
    manitoba-maint-canary-plex-unmatched.timer \
    manitoba-maint-canary-rea-liveness.service \
    manitoba-maint-canary-rea-liveness.timer \
    manitoba-maint-flaresolverr-canary.service \
    manitoba-maint-flaresolverr-canary.timer \
    manitoba-maint-canary-hardlink-integrity.service \
    manitoba-maint-canary-hardlink-integrity.timer \
    manitoba-maint-canary-plex-transcoder.service \
    manitoba-maint-canary-plex-transcoder.timer \
    manitoba-maint-canary-quota.service \
    manitoba-maint-canary-quota.timer \
    manitoba-maint-canary-tautulli-plex-link.service \
    manitoba-maint-canary-tautulli-plex-link.timer \
    manitoba-maint-canary-newsletter-digest.service \
    manitoba-maint-canary-newsletter-digest.timer \
    manitoba-maint-cp-upgrade.service \
    manitoba-maint-arr-audit.service \
    manitoba-maint-arr-audit.timer \
    manitoba-maint-ucc-detect.service \
    manitoba-maint-ucc-detect.timer \
    manitoba-maint-backup-prune.service \
    manitoba-maint-backup-prune.timer \
    manitoba-maint-anime-janitor.service \
    manitoba-maint-anime-janitor.timer \
    manitoba-maint-torrent-janitor.service \
    manitoba-maint-torrent-janitor.timer \
    manitoba-maint-audit.service \
    manitoba-maint-audit.timer \
    manitoba-maint-audit-live.service \
    manitoba-maint-audit-live.timer \
    manitoba-maint-canary-thread-ceiling.service \
    manitoba-maint-canary-thread-ceiling.timer \
    manitoba-maint-canary-sab-stall.service \
    manitoba-maint-canary-sab-stall.timer \
    manitoba-maint-canary-tdarr-scanner.service \
    manitoba-maint-canary-tdarr-scanner.timer \
    manitoba-maint-canary-tdarr-healthcheck.service \
    manitoba-maint-canary-tdarr-healthcheck.timer \
    manitoba-maint-canary-ucc-gate-stuck.service \
    manitoba-maint-canary-ucc-gate-stuck.timer \
    manitoba-maint-canary-dash-asset-integrity.service \
    manitoba-maint-canary-dash-asset-integrity.timer \
    manitoba-maint-canary-timer-liveness.service \
    manitoba-maint-canary-timer-liveness.timer \
    manitoba-maint-canary-deploy-drift.service \
    manitoba-maint-canary-deploy-drift.timer \
    qflix-collect.service \
    qflix-collect.timer \
    manitoba-maint-boot-listeners.service; do
  # If the user unit is a symlink pointing back at the source, `cp -f` fails
  # with "are the same file" — the source already IS the live unit. Drop the
  # symlink first; cp then writes a real file.
  if [ -L ~/.config/systemd/user/$unit ]; then
    rm -f ~/.config/systemd/user/$unit
  fi
  cp -f ~/scripts/maint/systemd/$unit ~/.config/systemd/user/$unit
done
systemctl --user daemon-reload
# Retire the cp-upgrade timer (folded into the window orchestrator 2026-06-28).
# On boxes provisioned before the fold-in, disable + remove it so it can't fire a
# second, UNLOCKED sweep at 11:30. The .service unit stays (manual sweeps only).
systemctl --user disable --now manitoba-maint-cp-upgrade.timer 2>/dev/null || true
rm -f ~/.config/systemd/user/manitoba-maint-cp-upgrade.timer
systemctl --user daemon-reload
# Enable everything that should auto-start.
systemctl --user enable --now manitoba-maint-webhook.service
systemctl --user enable --now manitoba-maint-window.timer
systemctl --user enable --now manitoba-maint-window-watchdog.timer
systemctl --user enable --now manitoba-maint-pusher.service
# Canary timers — idempotent: enable --now only starts if not already running.
systemctl --user enable --now manitoba-maint-canary-movie.timer
systemctl --user enable --now manitoba-maint-canary-anime.timer
systemctl --user enable --now manitoba-maint-canary-mobile-ux.timer
# vlogs-stall canary: requires victorialogs.service (deployed by 80-vlogs-install.sh).
# enable --now is safe even if vlogs isn't running yet — the canary script will
# exit with vlogs-down/no-ingest and push the right status to Kuma.
systemctl --user enable --now manitoba-maint-canary-vlogs-stall.timer
# qbit-stall canary: detects libtorrent engine wedge (dl_info_speed=0 for
# ≥5min + queuedDL>N). Same 15-min cadence as vlogs-stall.
systemctl --user enable --now manitoba-maint-canary-qbit-stall.timer
# sab-stall canary: usenet twin of qbit-stall (queue speed ~0 with active slots
# waiting, or a slot-level Paused job pinned >=24h). Shipped 2026-07-19 with a
# manifest entry, a script and both units but ZERO installer wiring, so it was
# never staged, never installed and never enabled - a guard the repo read as
# covered while nothing ran it. Wired 2026-07-29 alongside dash-asset-integrity;
# tests/unit/test_canary_wiring.py now makes that class of omission impossible.
systemctl --user enable --now manitoba-maint-canary-sab-stall.timer
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
# prowlarr-app-sync: the CONFIG half of the same surface — every indexer
# Prowlarr intends to sync is actually present in that *arr, and the two
# indexers with a diagnosed 403'ing proxy download path still prefer magnets.
# Deliberately a separate timer from the canary above: that one reads logs and
# Prowlarr /api/v1/health, which is `[]` for both faults here.
# EXPECT THIS RED on the first tick until step 1 of
# docs/prowlarr-indexer-remediation-2026-08-03.md is applied — the red IS the
# acceptance test for that runbook.
systemctl --user enable --now manitoba-maint-canary-prowlarr-app-sync.timer
# plex-unmatched: episodes stuck on a `local://` guid (scanner beat the agent
# match), so the member gets no synopsis, no artwork and no air date. Detect
# only — the remedy destroys ratingKeys and watch state, so it stays an operator
# decision. Expect ~30 aged findings on the first live run; that is the audit's
# measured backlog, not a canary fault.
systemctl --user enable --now manitoba-maint-canary-plex-unmatched.timer
# rea-liveness: the dead-man for the operator workstation's Random Error Audit,
# the one component that does not run on this box and has never been watched.
# The judgement runs HERE so the alarm never depends on REA being healthy enough
# to diagnose itself. UNTIL the writer half lands in qflix-rea.ps1 this exits 2
# with STAGE=rea-heartbeat-absent — accurate, actionable, and pushed every hour,
# which is the point. Land the writer first if you want to deploy into green.
systemctl --user enable --now manitoba-maint-canary-rea-liveness.timer
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
# quota: track per-user Ultra.cc quota at 80%/90%/98% (warn/crit/fail).
# At 90% fires Maintainerr execute + collections/handle autonomously to
# reclaim space before the 100% wall causes SQLite I/O errors stack-wide.
systemctl --user enable --now manitoba-maint-canary-quota.timer
# tautulli-plex-link: every 15 min, assert Tautulli's CONFIGURED pms target is
# a live Plex /identity. Catches "Tautulli web up but pinned to a dead/old Plex
# IP" — the 2026-05-20 re-IP class the app monitor stayed green through.
# Read-only probe; never restarts anything.
systemctl --user enable --now manitoba-maint-canary-tautulli-plex-link.timer
# newsletter-digest: fires 3x Monday 14:20/14:50/15:20 UTC bracketing the
# 15:00 UTC send. Freshness is enforced only inside the script's own
# Mon-14:15-24:00 UTC window (Persistent=true is safe: a catch-up firing
# outside that window is a silent not-in-eval-window no-op). Detects a
# stale/absent digest.json — the silent-fallback condition changelog.py
# degrades through without ever raising.
systemctl --user enable --now manitoba-maint-canary-newsletter-digest.timer
# (UCC app-upgrade sweep timer retired 2026-06-28 — now a step inside the window
# orchestrator; see the disable/remove right after daemon-reload above.)
# Weekly *arr stack audit — Sun 04:00 UTC. Read-only; writes markdown
# reports to ~/.opt/maint/audit-reports/arr-audit-YYYY-MM-DD.md (90d
# retention). Runs in loopback mode (no nginx hop).
systemctl --user enable --now manitoba-maint-arr-audit.timer
# UCC upstream-maintenance gate probe — every 5 min (OnActiveSec gives the
# first fire ~1min after enable). On a clear→active edge it pins a Kuma
# status-page incident, fires the "Upstream Maintenance Start" subscriber
# email, and Discord-notifies; on active→clear it unpins + fires "Complete"
# + triggers the post-window deep-check. Edge-triggered via
# ~/.opt/maint/ucc-response-state.json — NOT per-cycle. NOTE: the first run
# while UCC maintenance is active WILL fire one customer "Start" email; the
# email is idempotent at the campaign level but the operator should be aware.
# Incident pin requires secrets/uptimekuma.password (skips with a warning if
# absent). Disable with: systemctl --user disable --now manitoba-maint-ucc-detect.timer
systemctl --user enable --now manitoba-maint-ucc-detect.timer
# App-backup retention prune — Sun 03:30 UTC. Keeps the 3 most recent
# app-manager backups per app in ~/.apps/backup; deletes the rest. --now
# activates the timer's schedule (does not run an immediate prune).
systemctl --user enable --now manitoba-maint-backup-prune.timer
# Anime-library janitor — daily 03:00 UTC. Ships DRY-RUN (ExecStart has no
# --execute): classifies non-anime in the anime libs + flags the reverse,
# mutates nothing. --now activates the schedule (no immediate run). Arming
# --execute is an operator step gated on Phase-0 live validation + re-council.
systemctl --user enable --now manitoba-maint-anime-janitor.timer
# Torrent janitor — daily 05:30 UTC. Purges completed *arr-untracked qBit
# seeding leftovers ("nothing forever"). Ships DRY-RUN (ExecStart has no
# --execute); arm via an on-box drop-in like the reaper. --now activates the
# schedule (no immediate run).
systemctl --user enable --now manitoba-maint-torrent-janitor.timer
# Convergent audit: daily enumeration of the declared defect classes, self-
# pushing "QFlix Audit Regime". Exits 1 on any ENFORCED finding.
systemctl --user enable --now manitoba-maint-audit.timer
# LIVE audit: what is actually running vs what the repo says. Every 6h.
systemctl --user enable --now manitoba-maint-audit-live.timer
# Thread-ceiling canary — every 15 min. Tracks user task count vs ulimit -u
# (RLIMIT_NPROC), 70% WARN / 85% FAIL. Guards the GOMAXPROCS thread-exhaustion
# class (memory seedbox-thread-cap-gomaxprocs).
systemctl --user enable --now manitoba-maint-canary-thread-ceiling.timer
# UCC gate-stuck canary — every 15 min. Watches the maintenance-gate DETECTOR
# itself: a probe erroring past ~1h means detection is dark, and a gate held
# active 6h+ means something froze it on. Before the 2026-07-29 cap, a broken
# probe silently suppressed fleet-wide auto-recovery (found at 128 errors,
# ~10.6h) with nothing watching. Reads the state file, not lib.ucc, so it
# survives a bug in that module.
systemctl --user enable --now manitoba-maint-canary-ucc-gate-stuck.timer
# Dash asset-integrity canary - every 15 min. Asserts the SERVED shell only
# references /_app/immutable assets the RUNNING server can actually deliver.
# On 2026-07-29 the dashboard served a dead shell for ~22h with every monitor
# green: build/ was rewritten at 01:33 and 03:31, while node PID 15178 had been
# up since 2026-07-08 - and adapter-node's sirv snapshots its file manifest ONCE
# at boot, so 6 of 10 referenced modules 404'd though the files sat on disk at
# mode 644. mobile-ux and the smoke only grepped the server-rendered
# data-qflix-dash marker, which survives a total hydration failure, and
# qflix-dash's own /healthz probe was answered by the stale process. Self-heals
# a stale sirv manifest (restart, breaker 1/24h, Monday-window suppressed,
# re-verified) but NEVER when the files are genuinely absent - that is a partial
# deploy, a different fault, and a restart would not fix it.
# NOTE: `enable --now` on a TIMER is correct and idempotent (a timer has no
# long-running in-memory state to go stale). It is `enable --now` on a SERVICE
# that caused the incident this canary guards - see the restart in
# scripts/configure/90-qflix-dash-install.sh.
systemctl --user enable --now manitoba-maint-canary-dash-asset-integrity.timer
# timer-liveness: the generic dead-man for every scheduled job. Closes the four
# open_gap entries in manifest/jobs.yaml with one check instead of four monitors.
systemctl --user enable --now manitoba-maint-canary-timer-liveness.timer

# deploy-drift: asserts the box RUNS what git says. The source audit reads a
# checkout; the box runs ~/scripts, and until this existed nothing compared
# them -- 8 files were found stale by up to 3 months, one of them a daily cron
# job whose deployed copy still swallowed Notifiarr failures.
systemctl --user enable --now manitoba-maint-canary-deploy-drift.timer
# Tdarr scanner canary — hourly. Guards FFprobe/Exiftool (pipeline-blocking if
# they break) and tracks the known-dead Mediainfo probe without parking a
# permanent red on it. Mediainfo is unfixable here: the slot's ulimit -v 10GB
# can't host Node's ~8GB wasm trap guard, and NODE_OPTIONS rejects the cure.
systemctl --user enable --now manitoba-maint-canary-tdarr-scanner.timer
# Tdarr health-check canary — hourly. Guards the health-check pipeline, which ran
# 100% dead and silent for 68 days (2026-05-21..07-28): libraries defaulted to
# handbrakescan and HandBrakeCLI does not exist on this rootless slot, so every
# check spawn-failed ENOENT while transcodes (bundled ffmpeg-static) kept
# succeeding and masked it. Reds on a missing engine binary immediately, and on a
# pathological completed-check error ratio.
systemctl --user enable --now manitoba-maint-canary-tdarr-healthcheck.timer
# QFlix hourly collector — snapshot + stale-detect + autonomous unstick + Kuma
# heartbeat. Migrated off the workstation 2026-07-09 (was Windows Task
# \QFlix\Hourly Collect, now disabled). Feeds the "QFlix Collect (workstation)"
# push monitor from the always-on box so a PC-off no longer false-reds the
# public status page. --now activates the hourly schedule.
systemctl --user enable --now qflix-collect.timer
# Boot-time listener snapshot — pulled in by default.target, runs once per boot
# to log TCP-listener occupancy (identifies a port squatter after a reboot; see
# memory qbit-webui-boot-bind-race). Plain `enable` (NOT --now): it's a ~3min
# sampling oneshot, so --now would block this installer; it fires on next boot.
systemctl --user enable manitoba-maint-boot-listeners.service
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

# `VAR=$(sshm "... | grep -c ...")` is a `set -e` landmine, and it silently
# disabled every counting gate below. A bare assignment inherits the command
# substitution's exit status, and `grep -c` exits 1 when it counts ZERO. So the
# exact condition each gate exists to catch - a timer that is NOT scheduled -
# terminated the installer AT THE ASSIGNMENT (line 11 sets -euo pipefail) instead
# of reaching the `gate ... fail` branch. Those fail branches were unreachable
# dead code; with `2>/dev/null` also swallowing ssh's stderr the operator got a
# bare rc=1 and no indication which check failed. `remote_count` neutralises the
# status and always echoes an integer, so a missing timer produces a FAIL record.
remote_count() {
  local out
  out=$(sshm "$1" </dev/null 2>/dev/null || true)
  out=$(printf '%s\n' "$out" | tail -1 | tr -d '[:space:]')
  case "$out" in
    '' | *[!0-9]*) printf '0' ;;
    *) printf '%s' "$out" ;;
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
TM=$(sshm "systemctl --user list-timers manitoba-maint-window.timer --no-pager 2>/dev/null | grep manitoba-maint-window.timer" </dev/null 2>/dev/null || true)
if [ -n "$TM" ]; then
  gate "window-timer-scheduled" pass "$(echo "$TM" | awk '{print $1, $2, $3}')"
else
  gate "window-timer-scheduled" fail "timer not in systemctl list-timers"
fi

# Smoke 4: window-watchdog timer scheduled
WT=$(remote_count "systemctl --user list-timers manitoba-maint-window-watchdog.timer --no-pager 2>/dev/null | grep -c manitoba-maint-window-watchdog")
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
CR=$(remote_count 'crontab -l 2>/dev/null | grep -c heartbeat-maint-webhook')
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
# Every canary in manifest/apps.yaml must appear here - tests/unit/test_canary_wiring.py
# asserts that, so a new canary cannot ship with a timer nobody checks.
for canary in movie anime mobile-ux vlogs-stall qbit-stall sab-stall kometa-libraries stale-log-watchdog kometa-deploy-drift prowlarr-indexer-health prowlarr-app-sync tautulli-plex-link quota hardlink-integrity plex-transcoder plex-unmatched newsletter-digest thread-ceiling tdarr-scanner tdarr-healthcheck ucc-gate-stuck dash-asset-integrity timer-liveness deploy-drift rea-liveness; do
  CT=$(remote_count "systemctl --user list-timers manitoba-maint-canary-${canary}.timer --no-pager 2>/dev/null | grep -c manitoba-maint-canary-${canary}.timer")
  if [ "${CT:-0}" -ge 1 ]; then
    gate "canary-timer-${canary}" pass "scheduled"
  else
    gate "canary-timer-${canary}" fail "timer not in systemctl list-timers"
  fi

  # A scheduled timer is NOT evidence the canary reports anything. `manitoba-maint
  # canary push <name>` SILENTLY EXITS 0 when its token is absent from
  # ~/secrets/kuma-push-tokens.json, so the unit runs, systemd records success,
  # and Kuma never hears from it -- the monitor then sits DOWN on "No heartbeat
  # in the time window" with zero real coverage.
  #
  # dash-asset-integrity shipped exactly that way on 2026-07-30: the bootstrap
  # hit a create-then-read race, left the key out of the token file, warned, and
  # exited 0. Timer-scheduled and token-present are independent facts and both
  # have to be asserted ON THE BOX, against the file the consumer actually reads.
  TOKPRESENT=$(remote_count "python3 -c \"import json;d=json.load(open('\$HOME/secrets/kuma-push-tokens.json'));print(1 if d.get('canary-${canary}') else 0)\" 2>/dev/null")
  if [ "${TOKPRESENT:-0}" -ge 1 ]; then
    gate "canary-token-${canary}" pass "push token present on box"
  else
    gate "canary-token-${canary}" fail "NO push token — canary would exit 0 and push nothing"
  fi
done

# Smoke 13: weekly arr-audit timer scheduled
AAT=$(remote_count "systemctl --user list-timers manitoba-maint-arr-audit.timer --no-pager 2>/dev/null | grep -c manitoba-maint-arr-audit.timer")
if [ "${AAT:-0}" -ge 1 ]; then
  gate "arr-audit-timer-scheduled" pass
else
  gate "arr-audit-timer-scheduled" fail "weekly arr-audit timer not in systemctl list-timers"
fi

# Smoke 14: UCC upstream-maintenance detect timer scheduled
UDT=$(remote_count "systemctl --user list-timers manitoba-maint-ucc-detect.timer --no-pager 2>/dev/null | grep -c manitoba-maint-ucc-detect.timer")
if [ "${UDT:-0}" -ge 1 ]; then
  gate "ucc-detect-timer-scheduled" pass
else
  gate "ucc-detect-timer-scheduled" fail "ucc-detect timer not in systemctl list-timers"
fi

# Smoke 15: app-backup retention prune timer scheduled
BPT=$(remote_count "systemctl --user list-timers manitoba-maint-backup-prune.timer --no-pager 2>/dev/null | grep -c manitoba-maint-backup-prune.timer")
if [ "${BPT:-0}" -ge 1 ]; then
  gate "backup-prune-timer-scheduled" pass
else
  gate "backup-prune-timer-scheduled" fail "backup-prune timer not in systemctl list-timers"
fi

echo
TOTAL=$((PASS + FAIL))
printf "Install smoke: %d/%d pass\n" "$PASS" "$TOTAL"
[ "$FAIL" = 0 ] || die "install-time smoke failed — see output above"

log_info "Phase 240 complete — maintenance system installed + smoke ${PASS}/${TOTAL}"
