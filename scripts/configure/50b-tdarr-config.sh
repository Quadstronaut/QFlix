#!/usr/bin/env bash
# Phase 29 — Tdarr config orchestrator.
#
# Wraps 50b-tdarr-config.py so the Python script can read the Flow JSON
# sidecar even when invoked via SSH stdin (which strips $0's directory).
#
# Steps:
#   1. scp scripts/configure/tdarr-flows/qflix-direct-play-fix.json -> seedbox
#   2. pipe the Python script over SSH; it reads the deployed JSON from
#      ~/.apps/tdarr/configs/ (see FLOW_FILE_CANDIDATES in 50b-tdarr-config.py)
#
# Idempotent: every step skips on a no-op. Safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

FLOW_JSON_LOCAL="$HERE/tdarr-flows/qflix-direct-play-fix.json"
FLOW_JSON_REMOTE='~/.apps/tdarr/configs/qflix-direct-play-fix.json'

if [ ! -f "$FLOW_JSON_LOCAL" ]; then
  echo "FATAL: missing $FLOW_JSON_LOCAL" >&2
  exit 1
fi

log_info "Uploading flow sidecar -> $FLOW_JSON_REMOTE"
sshm 'mkdir -p ~/.apps/tdarr/configs'
scpm_to "$FLOW_JSON_LOCAL" "$FLOW_JSON_REMOTE"

log_info "Running 50b-tdarr-config.py on seedbox"
sshm 'python3 -' < "$HERE/50b-tdarr-config.py"

log_info "50b complete"
