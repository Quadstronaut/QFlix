#!/usr/bin/env bash
# Phase 26 — sync this branch's Jinja templates into Listmonk as named
# templates. The branch determines the env:
#   master   → ENV=prod   → updates "Prod · Weekly Digest" etc.
#   staging  → ENV=stage  → updates "Stage · Weekly Digest" etc.
#
# Workflow:
#   1. Validate current branch is master or staging
#   2. Re-deploy the qflix_newsletter package (rsync via tar+ssh) so the
#      seedbox has the latest templates dir
#   3. SSH to seedbox and invoke ``python -m qflix_newsletter.sync``
#   4. Audit Listmonk and print the post-sync template list
#
# Idempotent — running this on the same branch twice just overwrites
# the existing 3 templates with the latest rendered HTML.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"
BRANCH=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)

case "$BRANCH" in
  master|main)
    ENV=prod
    ;;
  staging)
    ENV=stage
    ;;
  *)
    log_error "current branch '$BRANCH' isn't master or staging — refusing to sync"
    log_error "switch to master (prod) or staging (stage) before running this"
    exit 1
    ;;
esac

log_info "branch=$BRANCH → ENV=$ENV"

# ── Step 1: rsync the package code (templates + sync.py + supporting
# modules) to ~/.apps/qflix-newsletter/. Mirrors the pattern in
# 49-qflix-newsletter-install.sh — same exclusions, same untar target.
log_info "syncing package code to ~/.apps/qflix-newsletter/"
sshm 'mkdir -p ~/.apps/qflix-newsletter && rm -rf ~/.apps/qflix-newsletter/qflix_newsletter'
(cd "$HERE/.." && tar czf - \
  --exclude='.venv' --exclude='.venv-dev' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='logs' \
  qflix-newsletter/qflix_newsletter) | \
  ssh -o BatchMode=yes -o ConnectTimeout=10 "${SSHM_HOST}" \
  'cd ~/.apps && tar xzf -'

# ── Step 2: invoke sync.py on the seedbox using the existing venv
log_info "invoking sync.py on seedbox (env=$ENV)"
sshm "cd ~/.apps/qflix-newsletter && .venv/bin/python -m qflix_newsletter.sync --env $ENV --verbose"

# ── Step 3: audit
log_info "post-sync Listmonk state:"
sshm 'PORT=$(cat ~/secrets/listmonk.port); USER=$(cat ~/secrets/listmonk.api_user); TOKEN=$(cat ~/secrets/listmonk.api_token); curl -sf -u "$USER:$TOKEN" "http://127.0.0.1:$PORT/api/templates" | jq -r ".data[] | \"  id=\(.id)  type=\(.type)  default=\(.is_default)  name=\(.name)\""'

log_info "sync complete (env=$ENV)"
