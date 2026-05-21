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
LIB_DEFAULTS_LOCAL="$HERE/tdarr-flows/library-defaults.json"
LIB_DEFAULTS_REMOTE='~/.apps/tdarr/configs/library-defaults.json'

for f in "$FLOW_JSON_LOCAL" "$LIB_DEFAULTS_LOCAL"; do
  if [ ! -f "$f" ]; then
    echo "FATAL: missing $f" >&2
    exit 1
  fi
done

log_info "Uploading flow + library defaults sidecars -> ~/.apps/tdarr/configs/"
sshm 'mkdir -p ~/.apps/tdarr/configs'
scpm_to "$FLOW_JSON_LOCAL" "$FLOW_JSON_REMOTE"
scpm_to "$LIB_DEFAULTS_LOCAL" "$LIB_DEFAULTS_REMOTE"

log_info "Running 50b-tdarr-config.py on seedbox"
sshm 'python3 -' < "$HERE/50b-tdarr-config.py"

log_info "50b complete"
