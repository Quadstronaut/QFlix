#!/usr/bin/env bash
# Wrapper for SSH to manitoba. Source this from other scripts.
SSHM_HOST="${SSHM_HOST:-quadstronaut@seedbox.example.com}"
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
