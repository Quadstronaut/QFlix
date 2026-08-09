#!/usr/bin/env bash
# Wrapper for SSH to manitoba. Source this from other scripts.
# Host is read from secrets/seedbox.host (gitignored — operator's real FQDN).
# Falls back to the sanitized public placeholder so the script is still
# readable in the public repo.
_SSHM_SECRETS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../secrets" 2>/dev/null && pwd)"
# On Ultra.cc shared seedboxes the SSH hostname is the SHARED box (e.g.
# seedbox.example.com) while the public HTTPS hostname is the operator slot
# (e.g. quadstronaut.seedbox.example.com). Prefer seedbox.ssh-host; fall back
# to seedbox.host for single-domain setups; finally fall back to the
# sanitized placeholder.
if [ -f "$_SSHM_SECRETS_DIR/seedbox.ssh-host" ]; then
  _SSHM_FQDN="$(tr -d '[:space:]' < "$_SSHM_SECRETS_DIR/seedbox.ssh-host")"
elif [ -f "$_SSHM_SECRETS_DIR/seedbox.host" ]; then
  _SSHM_FQDN="$(tr -d '[:space:]' < "$_SSHM_SECRETS_DIR/seedbox.host")"
else
  _SSHM_FQDN="seedbox.example.com"
fi
SSHM_HOST="${SSHM_HOST:-quadstronaut@$_SSHM_FQDN}"
SSHM_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30)

# If we're already running on the seedbox (e.g., from systemd canary timers),
# skip the SSH hop entirely — the seedbox doesn't have key-auth back to itself,
# and the network round-trip is wasted anyway.
_sshm_on_host() {
  [ "$(hostname 2>/dev/null)" = "manitoba" ]
}

sshm() {
  if _sshm_on_host; then
    bash -c "$*"
  else
    ssh "${SSHM_OPTS[@]}" "$SSHM_HOST" "$@"
  fi
}

scpm_to() {
  if _sshm_on_host; then
    cp "$1" "${2/#\~/$HOME}"
  else
    scp "${SSHM_OPTS[@]}" "$1" "$SSHM_HOST:$2"
  fi
}

scpm_from() {
  if _sshm_on_host; then
    cp "${1/#\~/$HOME}" "$2"
  else
    scp "${SSHM_OPTS[@]}" "$SSHM_HOST:$1" "$2"
  fi
}
