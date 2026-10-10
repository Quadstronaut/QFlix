#!/usr/bin/env bash
# claim_port <secret-file-name>  e.g. claim_port vlogs.port
# Idempotent: an existing secret is left untouched (no ssh at all). Otherwise
# fetches `app-ports free` and `ss -tln` from the box, then lets lib/ports.py
# pick the first free candidate under flock and write the secret atomically.
# Needs: sshm, die, log_info, SECRETS_DIR (source ssh.sh, log.sh, secrets.sh first).
_PORTS_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")/../maint/lib" && pwd)/ports.py"

claim_port() {
  local name="$1" ap ss port
  if secret_exists "$name"; then return 0; fi
  ap=$(sshm "app-ports free 2>/dev/null") || ap=""
  ss=$(sshm "ss -tln 2>/dev/null") || ss=""
  port=$(python3 "$_PORTS_PY" claim "$name" --secrets-dir "$SECRETS_DIR" \
           --app-ports "$ap" --ss "$ss") || die "no truly-free port for $name (app-ports minus claimed minus bound)"
  log_info "claimed $name = $port"
}
