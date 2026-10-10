#!/usr/bin/env bash
# 15-bootstrap-new.sh -- give green (box 2) the repo, its host identity and the
# operator's identity secrets. Green only: blue is never written (I-2); blue is
# only ASKED for its host profile by the window guard.
#
# Spec section 8 row 15: "Rewrite: run 240 + the proven 3NN-native-* installers
# with host.profile=generic. No panel installs." This script is the half that
# makes green able to run them; 20-install-stack.sh runs them. The stale branch
# checked for `app-<slug>` panel wrappers and ran bootstrap-discover.sh to
# scrape panel-generated ports/keys. Both are gone: green is a generic Linux
# account, and the native installers claim ports (lib/ports.py) and mint keys
# themselves (spec 5.5, fresh install).
#
# STEPS (plan always printed; nothing but the plan without --execute, I-3):
#   1. green reachable + prerequisites (python3, PyYAML, git, rsync,
#      systemd --user, linger). A missing one is a finding (exit 1).
#   2. clone/update the public repo at ~/.opt/qflix-src (I-4: fetch+reset).
#   3. seed ~/scripts from the checkout (what deploy-drift asserts later).
#   4. write ~/secrets/host.profile=generic and host.id (I-12). An EXISTING
#      different profile is a refusal, never an overwrite.
#   5. copy the identity-secret ALLOWLIST (local secrets/ -> green ~/secrets and
#      the local green secrets dir secrets/green/ that 20 runs 240 with).
#      discord-webhook.url lands PARKED (.held) on green: I-1, green pages
#      nobody until 50-cutover step 5. members.yaml is NOT here: 35 copies it
#      with `armed: false` forced (I-5).
#
# USAGE: 15-bootstrap-new.sh NEW_HOST [--old-host HOST] [--host-id ID] [--execute]
#        (--host-id defaults to green's `hostname -s`)
# EXIT:  0 ok | 1 finding (missing prerequisite, step failed) | 2 refused /
#        could-not-assert (usage, unreachable, profile conflict, window)
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

usage() { echo "usage: $0 NEW_HOST [--old-host HOST] [--host-id ID] [--execute]" >&2; }
HOST_ID=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --host-id=*) HOST_ID="${1#--host-id=}" ;;
    --host-id) shift; HOST_ID="${1:-}" ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
mig_args "" "${ARGS[@]+"${ARGS[@]}"}"
need_new_host

# Identity follows the OPERATOR, not the box: an allowlist (a new secret must be
# deliberately added), and never a *.port / *.urlbase / *.key of an app (those
# are minted fresh on green or copied with the app's data by 35).
IDENTITY_SECRETS=(
  discord-webhook.url discord-operator.id
  plex.token
  nzbgeek.key nzbgeek.url
  github.pat
  entitlement.key entitlement.url
  tmdb.read_token tmdb.api_key
  uptimekuma.key htpasswd.password shared-admin.password
)
GH_REPO="Quadstronaut/QFlix"
secret_exists github.repo && GH_REPO="$(secret_read github.repo)"
REPO_URL="https://github.com/$GH_REPO.git"

echo "=== 15-bootstrap-new: green = $NEW_HOST ($([ "$EXECUTE" -eq 1 ] && echo EXECUTE || echo dry-run)) ==="
plan "1 check green: python3 + PyYAML, git, rsync, systemctl --user, loginctl Linger=yes"
plan "2 green: ~/.opt/qflix-src <- $REPO_URL (fetch + reset to origin/master if present)"
plan "3 green: rsync -a --delete ~/.opt/qflix-src/scripts/ ~/scripts/"
plan "4 green: ~/secrets/host.profile = generic ; host.id = ${HOST_ID:-<green hostname -s>} (refuse if a different profile exists)"
for s in "${IDENTITY_SECRETS[@]}"; do
  if [ "$s" = "$DISCORD_WEBHOOK_SECRET" ]; then plan "5 copy $s -> green ~/secrets/$s.held (PARKED, I-1) + secrets/green/$s"
  else plan "5 copy $s -> green ~/secrets/$s + secrets/green/$s"; fi
done
if [ "$EXECUTE" -ne 1 ]; then
  echo "[dry-run] no ssh made, nothing changed. Re-run with --execute."
  exit 0
fi

resolve_old_host
sshg true >/dev/null 2>&1 || stage 2 green-unreachable "ssh-to-NEW_HOST-failed"
window_guard

# 1 -- prerequisites (read-only)
PRE="$(sshg 'for c in python3 git rsync systemctl; do command -v $c >/dev/null 2>&1 || echo "missing:$c"; done
  python3 -c "import yaml" 2>/dev/null || echo "missing:python3-yaml"
  [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" = yes ] || echo "missing:linger"
  systemctl --user show-environment >/dev/null 2>&1 || echo "missing:systemd-user"' 2>/dev/null | tr -d '\r')"
if printf '%s' "$PRE" | grep -q '^missing:'; then
  stage 1 green-prereq "$(printf '%s' "$PRE" | grep '^missing:' | tr '\n' ',')"
fi
log_info "1 prerequisites present on green"

# 2 + 3 -- repo + ~/scripts (idempotent)
sshg "set -u
  if [ -d ~/.opt/qflix-src/.git ]; then
    git -C ~/.opt/qflix-src fetch -q origin && git -C ~/.opt/qflix-src reset -q --hard origin/master
  else
    mkdir -p ~/.opt && git clone -q '$REPO_URL' ~/.opt/qflix-src
  fi" || stage 1 clone-failed "git-clone-or-update-on-green"
sshg 'mkdir -p ~/scripts && rsync -a --delete ~/.opt/qflix-src/scripts/ ~/scripts/' \
  || stage 1 seed-failed "rsync-of-scripts-on-green"
log_info "2-3 repo + ~/scripts seeded"

# 4 -- host identity (I-12: explicit, never guessed, never silently replaced)
[ -n "$HOST_ID" ] || HOST_ID="$(sshg 'hostname -s' 2>/dev/null | tr -d '\r[:space:]')"
[ -n "$HOST_ID" ] || stage 2 host-id-unresolved "pass---host-id"
CUR="$(sshg 'cat ~/secrets/host.profile 2>/dev/null' | tr -d '\r[:space:]')"
if [ -n "$CUR" ] && [ "$CUR" != generic ]; then
  stage 2 profile-conflict "green-already-declares-host.profile=$CUR;-refusing-to-overwrite"
fi
sshg "mkdir -p ~/secrets ~/.config/qflix && chmod 700 ~/secrets &&
  printf 'generic\n' > ~/secrets/host.profile && printf '%s\n' '$HOST_ID' > ~/secrets/host.id &&
  chmod 600 ~/secrets/host.profile ~/secrets/host.id" || stage 1 host-identity "write-failed"
mkdir -p "$GREEN_SECRETS" && chmod 700 "$GREEN_SECRETS" 2>/dev/null
printf 'generic\n' > "$GREEN_SECRETS/host.profile"
printf '%s\n' "$HOST_ID" > "$GREEN_SECRETS/host.id"
log_info "4 host.profile=generic host.id=$HOST_ID"

# 5 -- identity secrets
skipped=()
for s in "${IDENTITY_SECRETS[@]}"; do
  if ! secret_exists "$s"; then skipped+=("$s"); continue; fi
  dest="$s"; [ "$s" = "$DISCORD_WEBHOOK_SECRET" ] && dest="$s.held"
  scp "${MIG_SSH_OPTS[@]}" "$SECRETS_DIR/$s" "$NEW_HOST:~/secrets/$dest" >/dev/null \
    && sshg "chmod 600 ~/secrets/$dest" \
    || stage 1 secret-copy "copy-failed:$s"
  cp -f "$SECRETS_DIR/$s" "$GREEN_SECRETS/$s" && chmod 600 "$GREEN_SECRETS/$s" 2>/dev/null
done
[ "${#skipped[@]}" -eq 0 ] || log_warn "not in local secrets/, skipped (capture + re-run): ${skipped[*]}"
log_info "5 identity secrets copied (webhook parked on green)"
log_info "Bootstrap complete. Next: scripts/migrate/20-install-stack.sh $NEW_HOST"
exit 0
