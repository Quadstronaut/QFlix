#!/usr/bin/env bash
# 15-bootstrap-new.sh -- stand up green's baseline: verify it's reachable and
# panel-provisioned, clone the public ops repo, seed the runtime scripts dir,
# let green discover ITS OWN freshly-generated secrets, then hand it the
# identity secrets that follow the operator (not the box) from local secrets/.
#
# Runs against: green only. Blue is never touched (I-2) -- this script never
# sources scripts/lib/ssh.sh's sshm(), which is hard-wired to blue via
# secrets/seedbox.*-host. Green is always the explicit NEW_HOST argument
# (user@host or an ssh-config alias), per house rule: never a hardcoded FQDN.
#
# Mutating steps (clone, seed, discover, secret copy) are INERT by default
# (I-3): every one prints its exact plan; none of them touch green's disk
# without --execute. The two verification steps (SSH reachable, panel apps
# present) are read-only against green and always run for real -- there is
# nothing in them to gate.
#
# Usage: 15-bootstrap-new.sh NEW_HOST [--execute]
#
# STAGE=<token> msg=<detail> on stderr for failures (house style).
# Exit codes: 0 ok, 1 finding/failure, 2 could-not-assert.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/log.sh"

EXECUTE=0
NEW_HOST=""
for arg in "$@"; do
  case "$arg" in
    --execute) EXECUTE=1 ;;
    --*) printf "STAGE=usage msg=unknown-flag(%s)\n" "$arg" >&2; exit 2 ;;
    *) [ -z "$NEW_HOST" ] && NEW_HOST="$arg" ;;
  esac
done
if [ -z "$NEW_HOST" ]; then
  printf "STAGE=usage msg=missing-NEW_HOST\n" >&2
  echo "usage: $0 NEW_HOST [--execute]" >&2
  exit 2
fi

# --- green SSH: plain ssh/scp against the explicit argument. Deliberately NOT
# sshm() from lib/ssh.sh -- that helper resolves its target from
# secrets/seedbox.*-host, which names BLUE. Reusing it here would silently
# retarget every "green" action at the live box.
SSHG_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30)
sshg()    { ssh "${SSHG_OPTS[@]}" "$NEW_HOST" "$@"; }
scpg_to() { scp "${SSHG_OPTS[@]}" "$1" "$NEW_HOST:$2"; }
plan()    { echo "  [PLAN] $*"; }   # always printed, --execute or not

# ---------------------------------------------------------------------------
# 1/5 -- SSH reachable. Read-only; if this fails nothing downstream can be
# asserted, so it's a could-not-assert (2), not a finding (1).
# ---------------------------------------------------------------------------
log_info "1/5 verifying SSH reachability: $NEW_HOST"
if ! sshg true >/dev/null 2>&1; then
  printf "STAGE=ssh-unreachable msg=cannot-reach-%s(check-key-auth-and-panel-ssh-toggle)\n" "$NEW_HOST" >&2
  exit 2
fi
log_info "  reachable."

# ---------------------------------------------------------------------------
# 2/5 -- panel apps present. The 18 UCC-class apps are read straight out of
# manifest/apps.yaml (same source 10-provision-checklist.md's list comes
# from) so this can never drift from the checklist it's grading against.
# A missing app-<slug> wrapper means the operator skipped a checklist row --
# a real finding, not an uncertainty, so this is exit 1, not 2.
# ---------------------------------------------------------------------------
log_info "2/5 verifying panel apps (app-<slug> wrappers on PATH)..."
UCC_SLUGS="$(grep -oP '(?<=ucc_slug: )\S+' "$ROOT/manifest/apps.yaml" | sort -u | tr '\n' ' ')"
CHECK_CMD="for s in $UCC_SLUGS; do command -v app-\$s >/dev/null 2>&1 && echo OK \$s || echo MISSING \$s; done"
CHECK_OUT="$(sshg "$CHECK_CMD")"
MISSING="$(echo "$CHECK_OUT" | awk '/^MISSING/{print $2}')"
TOTAL="$(wc -w <<<"$UCC_SLUGS")"
PRESENT="$(echo "$CHECK_OUT" | grep -c '^OK' || true)"
if [ -n "$MISSING" ]; then
  printf "STAGE=panel-apps-missing msg=%d-of-%d-ucc-apps-not-installed apps=%s\n" \
    "$(wc -w <<<"$MISSING")" "$TOTAL" "$(echo $MISSING | tr '\n' ' ')" >&2
  echo "  Install the missing app(s) from the Ultra.cc panel per" \
       "scripts/migrate/10-provision-checklist.md, then re-run." >&2
  exit 1
fi
log_info "  $PRESENT/$TOTAL panel apps present."

# ---------------------------------------------------------------------------
# 3/5 -- clone the public ops repo to ~/.opt/qflix-src. Idempotent: a rerun
# fetches+resets rather than failing on a non-empty directory (I-4).
# Repo is read from secrets/github.repo (owner/name) if the operator has set
# one, else the documented default -- never hardcoded past this one fallback.
# ---------------------------------------------------------------------------
GH_REPO_FILE="$ROOT/secrets/github.repo"
GH_REPO="$( [ -f "$GH_REPO_FILE" ] && tr -d '[:space:]' < "$GH_REPO_FILE" || echo "Quadstronaut/QFlix" )"
REPO_URL="https://github.com/$GH_REPO.git"
log_info "3/5 clone public repo ($GH_REPO) -> green:~/.opt/qflix-src"
plan "ssh $NEW_HOST: if ~/.opt/qflix-src/.git exists: fetch+reset --hard origin/master"
plan "              else: git clone $REPO_URL ~/.opt/qflix-src"
if [ "$EXECUTE" -eq 1 ]; then
  sshg "
    set -uo pipefail
    if [ -d ~/.opt/qflix-src/.git ]; then
      git -C ~/.opt/qflix-src fetch -q origin &&
      git -C ~/.opt/qflix-src reset -q --hard origin/master
    else
      mkdir -p ~/.opt && git clone -q '$REPO_URL' ~/.opt/qflix-src
    fi
  " || { printf "STAGE=clone-failed msg=git-clone-or-reset-failed-on-%s\n" "$NEW_HOST" >&2; exit 1; }
fi

# ---------------------------------------------------------------------------
# 4/5 -- seed ~/scripts from the checkout's scripts/. This is the SAME
# SRC/DEPLOYED relationship deploy-drift.sh asserts stays true forever after:
# ~/scripts must equal origin/master's scripts/ byte-for-byte. --delete keeps
# it that way on a first bootstrap (nothing pre-existing to protect here).
# ---------------------------------------------------------------------------
log_info "4/5 seed ~/scripts from ~/.opt/qflix-src/scripts/"
plan "ssh $NEW_HOST: mkdir -p ~/scripts && rsync -a --delete ~/.opt/qflix-src/scripts/ ~/scripts/"
if [ "$EXECUTE" -eq 1 ]; then
  sshg "mkdir -p ~/scripts && rsync -a --delete ~/.opt/qflix-src/scripts/ ~/scripts/" \
    || { printf "STAGE=seed-failed msg=rsync-of-scripts-failed-on-%s\n" "$NEW_HOST" >&2; exit 1; }
fi

# ---------------------------------------------------------------------------
# 5a/5 -- run bootstrap-discover.sh ON green to build GREEN's own secrets
# (freshly panel-installed apps each generated their own API keys/ports --
# this scrapes those into green's ~/secrets/, exactly like it does for blue).
#
# The hostname() shadow below is required, not decorative: lib/ssh.sh's
# _sshm_on_host() hardcodes hostname == "manitoba" (blue's box name) as its
# "we're already sitting on the seedbox, skip the ssh hop" check. On green
# that check is false, so bootstrap-discover.sh's own sshm() calls would try
# to hop back OUT over ssh to read files that are already sitting right here
# on green's local disk -- and hop to nowhere, since green has no
# secrets/seedbox.ssh-host of its own yet (and per ssh.sh's own comment, a
# seedbox has no key-auth back to itself anyway). Shadowing hostname() for
# this one call makes _sshm_on_host see "manitoba" and read everything
# locally via `bash -c`, which is the correct behavior when the script is
# genuinely running ON the box it's inspecting.
# ---------------------------------------------------------------------------
log_info "5/5 discover green's secrets (bootstrap-discover.sh, run ON green)"
plan "ssh $NEW_HOST: cd ~/scripts && bash bootstrap-discover.sh"
plan "              (hostname() shadowed to 'manitoba' for this call -- see"
plan "               script comment; makes bootstrap-discover.sh read green's"
plan "               own ~/.apps/*/config.xml locally instead of hopping off-box)"
if [ "$EXECUTE" -eq 1 ]; then
  sshg '
    set -uo pipefail
    hostname() { echo manitoba; }
    export -f hostname
    cd ~/scripts && bash bootstrap-discover.sh
  ' || { printf "STAGE=discover-failed msg=bootstrap-discover.sh-failed-on-%s\n" "$NEW_HOST" >&2; exit 1; }
fi

# ---------------------------------------------------------------------------
# 5b/5 -- copy IDENTITY secrets from the local checkout's secrets/ to green's
# ~/secrets/. Allowlist, not a denylist, on purpose: a denylist of "slot-
# specific" names silently starts copying anything NEW added later; an
# allowlist of "identity" names has to be deliberately extended instead.
# Per spec section 2 decision 3 ("Secrets" bullet): identity data follows the
# operator across boxes; slot-specific ports/urlbases are regenerated fresh
# by bootstrap-discover.sh above and must NEVER be copied -- hence no *.port
# or *.urlbase file appears in this list, ever.
# ---------------------------------------------------------------------------
IDENTITY_SECRETS=(
  discord-webhook.url    # single notification channel, operator's own
  discord-operator.id    # operator's Discord user-id (for @-pings)
  plex.token              # SAME Plex account reused on green (spec decision 3)
  nzbgeek.key             # Usenet indexer credential
  nzbgeek.url
  github.pat              # rate-limit-only PAT, zero permissions
  entitlement.key         # entitlement-gate service credential
  entitlement.url
  tmdb.read_token         # spec sec.2 names TMDB alongside these explicitly
  tmdb.api_key
  members.yaml            # roster; ships with armed:true but green's gate
                          # stays disarmed until 50-cutover.sh flips it (spec sec.5)
)

log_info "5/5 copy identity secrets: local secrets/ -> green:~/secrets/"
plan "ssh $NEW_HOST: mkdir -p ~/secrets && chmod 700 ~/secrets"
[ "$EXECUTE" -eq 1 ] && sshg "mkdir -p ~/secrets && chmod 700 ~/secrets"

skipped=()
for name in "${IDENTITY_SECRETS[@]}"; do
  src="$ROOT/secrets/$name"
  if [ ! -f "$src" ]; then
    skipped+=("$name")
    continue
  fi
  plan "scp $name -> $NEW_HOST:~/secrets/$name"
  if [ "$EXECUTE" -eq 1 ]; then
    scpg_to "$src" "~/secrets/$name" \
      && sshg "chmod 600 ~/secrets/$name" \
      || { printf "STAGE=scp-failed msg=copy-failed name=%s\n" "$name" >&2; exit 1; }
  fi
done
if [ "${#skipped[@]}" -gt 0 ]; then
  log_warn "not captured locally yet, skipped (idempotent -- capture + re-run): ${skipped[*]}"
fi

echo
if [ "$EXECUTE" -eq 1 ]; then
  log_info "Bootstrap complete on $NEW_HOST. Next: scripts/migrate/20-install-stack.sh $NEW_HOST"
else
  log_info "Dry run complete -- no changes made. Re-run with --execute to apply the plan above."
fi
exit 0
