#!/usr/bin/env bash
# scripts/migrate/_common.sh -- shared plumbing for the box-2 migration scripts.
#
# Sourced, never run. Gives every NN-*.sh the same:
#   * argument contract:  NN-x.sh NEW_HOST [--old-host HOST] [--execute] [--yes] ...
#     NEW_HOST / OLD_HOST are ALWAYS arguments (or, for OLD_HOST only, the
#     gitignored secrets/seedbox.ssh-host via lib/ssh.sh). No host is ever
#     written in this repo: it is public.
#   * two explicit ssh helpers: sshb (blue = OLD_HOST) and sshg (green =
#     NEW_HOST). Deliberately NOT lib/ssh.sh's sshm(): sshm short-circuits to a
#     local `bash -c` when it thinks it is on the box, and a migration must
#     always know exactly which box a command lands on.
#   * house error style: STAGE=<token> msg=<detail> on stderr; exit 0 ok,
#     1 finding/failure, 2 could-not-assert / refused (usage, unreachable,
#     unresolvable host profile, inside a maintenance window).
#   * I-3: EXECUTE=0 unless --execute. Dry runs make NO ssh connection at all.
#   * the window guard (spec 5.6): before any live run, ask OLD_HOST for its
#     host profile (fail closed, I-12) and refuse inside its maintenance window.
#
# Test seams (env): QFLIX_MIGRATE_PYTHON, QFLIX_MIGRATE_STATE_DIR,
# QFLIX_GREEN_SECRETS_DIR, QFLIX_MIGRATE_MANIFEST, QFLIX_NOW, SECRETS_DIR.

MIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$MIG_DIR/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/log.sh"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/secrets.sh"
# shellcheck source=/dev/null
source "$MIG_DIR/migrate.conf"

PY="${QFLIX_MIGRATE_PYTHON:-python3}"
MSTATE="${QFLIX_MIGRATE_STATE_DIR:-$ROOT/$MIGRATE_STATE_REL}"
GREEN_SECRETS="${QFLIX_GREEN_SECRETS_DIR:-$ROOT/secrets/green}"
MIG_SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30)

NEW_HOST=""; OLD_HOST=""; EXECUTE=0; YES=0; DELTA=0
declare -A FLAG=()

# stage EXIT TOKEN DETAIL -- the one way a migrate script dies.
stage() {
  local code="$1" token="$2"; shift 2
  local msg="$*"
  printf 'STAGE=%s msg=%s\n' "$token" "${msg// /-}" >&2
  exit "$code"
}
plan() { printf '  [PLAN] %s\n' "$*"; }

# mig_args ALLOWED_FLAGS "$@"
#   ALLOWED_FLAGS: space-separated extra --flags this script accepts (stored in
#   FLAG[name]=1). --execute, --yes, --old-host are always accepted.
mig_args() {
  local allowed=" $1 "; shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --execute) EXECUTE=1 ;;
      --yes) YES=1 ;;
      --delta) DELTA=1; FLAG[delta]=1 ;;
      --old-host=*) OLD_HOST="${1#--old-host=}" ;;
      --old-host) shift; OLD_HOST="${1:-}" ;;
      -h|--help) usage; exit 0 ;;
      --*)
        local f="${1#--}"
        case "$allowed" in
          *" --$f "*) FLAG[$f]=1 ;;
          *) usage; stage 2 usage "unknown-flag:$1" ;;
        esac ;;
      *) if [ -z "$NEW_HOST" ]; then NEW_HOST="$1"; else usage; stage 2 usage "unexpected-argument:$1"; fi ;;
    esac
    shift
  done
}

need_new_host() {
  [ -n "$NEW_HOST" ] || { usage; stage 2 usage "missing-NEW_HOST"; }
}

# OLD_HOST: the argument, else what lib/ssh.sh resolves from the gitignored
# secrets/seedbox.ssh-host (exactly the host every other repo script targets).
resolve_old_host() {
  if [ -z "$OLD_HOST" ]; then
    # shellcheck source=/dev/null
    source "$ROOT/scripts/lib/ssh.sh"
    OLD_HOST="$SSHM_HOST"
  fi
  [ -n "$OLD_HOST" ] || stage 2 usage "missing-OLD_HOST"
  [ "$OLD_HOST" != "$NEW_HOST" ] || stage 2 usage "OLD_HOST-equals-NEW_HOST"
}

sshb() { ssh "${MIG_SSH_OPTS[@]}" "$OLD_HOST" "$@"; }
sshg() { ssh "${MIG_SSH_OPTS[@]}" "$NEW_HOST" "$@"; }
# (CR stripped: a Windows python writes CRLF, which would make "MISSING\r"
# compare unequal to MISSING on the workstation. Exit status preserved.)
mm()   { "$PY" "$MIG_DIR/migrate_manifest.py" "$@" | tr -d '\r'; return "${PIPESTATUS[0]}"; }
# Fail closed BEFORE using any table: a manifest app with no data strategy
# would otherwise vanish from a `mapfile < <(mm ...)` (process substitution
# drops the exit code).
need_tables() {
  mm apps >/dev/null || stage 2 manifest-table "migrate_manifest.py-could-not-place-every-manifest-app"
}

# Run a repo python helper ON a box, fed over stdin (nothing to deploy first).
on_box() {  # on_box b|g SCRIPT ARGS...
  local side="$1" script="$2"; shift 2
  local q=""; local a
  for a in "$@"; do q="$q $(printf '%q' "$a")"; done
  if [ "$side" = b ]; then sshb "python3 -$q" < "$MIG_DIR/$script"
  else sshg "python3 -$q" < "$MIG_DIR/$script"; fi
}

# window_guard -- refuse a live run inside OLD_HOST's maintenance window.
# The profile is the box's own fail-closed answer (hostpolicy.py preflight);
# an unreachable box or an unresolvable profile is a refusal (exit 2), never a
# default. The Ultra window is evaluated here from hostpolicy_ultra (code, not
# config); a generic host is asked about its own operator-configured window.
window_guard() {
  local prof rc
  prof="$(sshb 'python3 ~/scripts/maint/lib/hostpolicy.py preflight' 2>/dev/null | tr -d '\r' | tail -n 1)"
  rc=$?
  case "$prof" in
    ultra|generic) ;;
    *) stage 2 old-host-profile-unresolved "OLD_HOST-host.profile-unreadable-(I-12-fail-closed)-got:${prof:-nothing}" ;;
  esac
  OLD_PROFILE="$prof"
  if [ "$prof" = ultra ]; then
    mm in-window --profile ultra; rc=$?
  else
    sshb 'python3 ~/scripts/maint/lib/hostpolicy.py in-window' >/dev/null 2>&1; rc=$?
  fi
  case "$rc" in
    0) stage 2 maintenance-window "OLD_HOST-profile-$prof-is-inside-its-maintenance-window;-no-box-operations-now" ;;
    1) log_info "window guard: OLD_HOST profile=$prof, outside its maintenance window" ;;
    *) stage 2 window-unknown "could-not-evaluate-the-$prof-window-(rc=$rc)" ;;
  esac
}

# confirm "TEXT" -- per-step operator gate on top of --execute (--yes skips).
confirm() {
  [ "$YES" -eq 1 ] && return 0
  printf '\n>>> %s\nProceed with this step? [y/N] ' "$1" >&2
  local ans; read -r ans
  case "$ans" in y|Y|yes|YES) return 0 ;; esac
  step_fail 1 operator-abort "declined:$1"
}

# --- ordered-step bookkeeping (50 / 55) -------------------------------------
COMPLETED=()
step_done() { COMPLETED+=("$1"); log_info "[x] step $1 complete"; }
step_fail() {  # step_fail EXIT TOKEN DETAIL -- stop on first failure, list progress
  local code="$1" token="$2" msg="$3"
  printf 'STAGE=%s msg=%s\n' "$token" "${msg// /-}" >&2
  if [ "${#COMPLETED[@]}" -gt 0 ]; then
    printf 'COMPLETED: %s\n' "${COMPLETED[*]}" >&2
  else
    printf 'COMPLETED: (none -- nothing on either box was changed)\n' >&2
  fi
  exit "$code"
}

# --- I-1 remote command builders (idempotent; run via sshb or sshg) ---------
# Comms = the member-facing jobs (newsletter timer, listmonk-sync cron).
comms_cmd() {  # comms_cmd hold|release
  local verb="$1" kind target key cmd="set -u; rc=0;"
  while IFS=$'\t' read -r key kind target; do
    [ -n "$key" ] || continue
    if [ "$kind" = timer ]; then
      if [ "$verb" = hold ]; then cmd="$cmd systemctl --user disable --now $target || rc=1;"
      else cmd="$cmd systemctl --user enable --now $target || rc=1;"; fi
    else
      local pat; pat="$(basename "$target")"
      if [ "$verb" = hold ]; then
        cmd="$cmd (crontab -l 2>/dev/null | sed '/^#/!{/$pat/s|^|$CRON_HOLD_TAG|}') | crontab - || rc=1;"
      else
        cmd="$cmd (crontab -l 2>/dev/null | sed 's|^$CRON_HOLD_TAG||') | crontab - || rc=1;"
      fi
    fi
  done < <(mm comms "${COMMS_JOBS[@]}")
  printf '%s exit $rc' "$cmd"
}

# Pager = the maint daemon's direct Discord webhook (Kuma is kuma_channels.py).
pager_cmd() {  # pager_cmd mute|loud
  local s="~/secrets/$DISCORD_WEBHOOK_SECRET"
  if [ "$1" = mute ]; then
    printf 'if [ -f %s ]; then mv -f %s %s.held; fi; test ! -f %s' "$s" "$s" "$s" "$s"
  else
    printf 'if [ -f %s.held ]; then mv -f %s.held %s; fi; test -f %s' "$s" "$s" "$s" "$s"
  fi
}

# Gate (I-5): the --execute drop-in is the arming switch on a box.
gate_disarm_cmd() {
  printf 'rm -f ~/%s && systemctl --user daemon-reload; test ! -e ~/%s' "$GATE_DROPIN_REL" "$GATE_DROPIN_REL"
}
gate_state_cmd() {  # prints "armed" or "disarmed"
  printf 'if [ -e ~/%s ]; then echo armed; else echo disarmed; fi' "$GATE_DROPIN_REL"
}

mkdir_state() { mkdir -p "$MSTATE" && chmod 700 "$MSTATE" 2>/dev/null; return 0; }
