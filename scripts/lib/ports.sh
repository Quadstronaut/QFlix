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
  # A failed ss is a refusal, never "everything is free" (matches appctl ports-free).
  ss=$(sshm "ss -tln 2>/dev/null") || die "ss failed on the box; refusing to claim $name"
  # Via stdin, not argv: the shared slot's ss output overflowed ARG_MAX (2026-10-10).
  port=$(printf '%s\n' "$ss" | python3 "$_PORTS_PY" claim "$name" --secrets-dir "$SECRETS_DIR" \
           --app-ports "$ap" --ss-file -) || die "no truly-free port for $name (app-ports minus claimed minus bound)"
  log_info "claimed $name = $port"
}
