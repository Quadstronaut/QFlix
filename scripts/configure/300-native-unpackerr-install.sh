#!/usr/bin/env bash
# 300-native-unpackerr-install.sh -- QFLX-25 (UCC divorce A1, the pilot).
#
# Moves unpackerr off the Ultra.cc container manager (UCC) onto a user unit the
# repo owns: the upstream static Go binary at EXACTLY the container's version,
# sha256-pinned, run as qflix-unpackerr.service. Spec:
# docs/superpowers/specs/2026-10-09-ucc-divorce-design.md 5.1-5.9, row 1 of 6.
#
# RUNS ON THE BOX. 240-maintenance-install.sh deploys it to ~/scripts/configure/
# with ~/scripts/lib/native.sh beside it. Started from the workstation it
# re-executes the deployed copy over ssh (scripts/lib/ssh.sh).
#
# INERT BY DEFAULT (I-3). Every mode prints its plan (DRY-RUN) and touches
# nothing unless --execute is also given. Modes, in swap order:
#
#   --install   5.9 step 1. Fetch + sha256-verify the release, refuse unless
#               the version equals `appctl version unpackerr` (I-10), lay out
#               ~/.apps/unpackerr/bin/<ver> + `current`, write the env file and
#               STAGE the unit in ~/.apps/unpackerr/native/. Deliberately NOT
#               copied into ~/.config/systemd/user and NOT enabled: the unit is
#               WantedBy=default.target, so an enabled unit would start beside
#               the live container on the next user-manager start (I-6).
#   --prove     5.9 step 2. Inert proof: a config with ONLY a [[folder]]
#               fixture (no arr sections, no webserver; I-11) under
#               ~/.apps/.prove/unpackerr, a fixture rar extracted end to end,
#               the proof's task count measured and refused when current +
#               delta reaches 70% of the host task ceiling (G-2). Never side by
#               side against live queues. Writes swap/<slug>/proof.json.
#   --swap      5.9 steps 3-6: needs the proof and the DEPLOYED pending-swap
#               manifest flip. Unmask (rollback step 4), capture the listen set
#               (unpackerr has no listener: the set must be EMPTY), path-audit
#               the config, suppress the app + its canaries, snapshot the
#               config, stop the container through appctl, POLL until no
#               container process is left and the probe port is free (abort
#               and restore the container otherwise), install + enable --now
#               the unit, verify parity, record swap state (14-day soak).
#               Suppression stays ON: the manifest still says pending-swap.
#   --finish    5.9 step 9, after the follow-up PR dropped `swap_state` and 240
#               deployed it: verify, then lift the app + canaries together.
#   --rollback  Rollback 0-5. 0: re-suppress, park the unit file and MASK it
#               (without the mask, pusher recovery restarts even a disabled
#               unit: two runtimes, I-6). 1: stop it and wait for exit. 2: the
#               DEPLOYED manifest must dispatch unpackerr as UCC again (revert
#               PR + 240, or the pending-swap state); otherwise exit 10 and
#               re-run after the revert. 3: start the container through
#               appctl. 5: no snapshot restore (stateless, versions equal).
#               4 (unmask) happens at the next --swap.
#
# The swap and rollback print elapsed=<s>: the pilot drills the rollback once
# for real and records the timing in the ticket.
#
# Exit: 0 ok | 1 refused/failed | 10 rollback paused for the manifest revert |
# 64 usage.
#
# Overrides (tests; resolved at call time): QFLIX_APPS_DIR QFLIX_UNIT_DIR
# QFLIX_ENV_DIR QFLIX_SWAP_DIR MANITOBA_STATE_DIR QFLIX_MANIFEST QFLIX_PROC
# QFLIX_PYTHON QFLIX_APPCTL QFLIX_SYSTEMCTL QFLIX_SS QFLIX_PS QFLIX_RAR
# QFLIX_CURL QFLIX_HOSTPOLICY QFLIX_HOST_ID_FILE QFLIX_UNPACKERR_SHA256
# QFLIX_POLL_S QFLIX_SETTLE_S QFLIX_STOP_TIMEOUT_S QFLIX_PROOF_TIMEOUT_S
# QFLIX_KEEP_PROOF.
set -uo pipefail

SLUG=unpackerr
VERSION="0.16.1"             # == versions.env UNPACKERR_VERSION (test-pinned)
SHA256="821b84f96f99213e30a675e1fdcd4266d7b60c926bb05e795feffdfd928d6eb5"
URL="https://github.com/Unpackerr/unpackerr/releases/download/v${VERSION}/unpackerr_${VERSION}_linux_amd64.tar.gz"
UNIT="qflix-${SLUG}.service"
FAMILY=go
EXE=unpackerr
# %h stays literal: systemd expands it. The config is used IN PLACE: the
# container mounted ~/.apps/unpackerr at /config and $HOME at the same path,
# so every path in it is already a host path (box, 2026-10-10).
EXEC_ARGS="-c %h/.apps/${SLUG}/unpackerr.conf"
# The container ran with TZ=Europe/Amsterdam (its /proc/<pid>/environ, box
# 2026-10-10). Keeping it keeps the log timestamps vlogs ingests unshifted
# (QFLX-44 class).
TZ_ENV="TZ=Europe/Amsterdam"
# Behavioural canaries muted with the app (plan A-table row A1). Keys follow
# cli.py: canary-<name>.
SUPPRESS=("$SLUG" "canary-thread-ceiling")
# unpackerr's only possible listener is its metrics webserver (default :5656,
# off unless [webserver] metrics = true). The captured set must be empty.
PROBE_PORT=5656
PATTERN="/unpackerr"         # manifest health.process_pattern

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # .../scripts
ARGS=("$@")

info() { echo "[300-unpackerr] $*"; }
die()  { echo "[300-unpackerr] ERROR: $*" >&2; exit 1; }
usage() {
  echo "usage: $0 [--install|--prove|--swap|--finish|--rollback] [--execute]" >&2
  exit 64
}

MODE=install
EXECUTE=0
for a in "$@"; do
  case "$a" in
    --install|--prove|--swap|--finish|--rollback) MODE="${a#--}" ;;
    --execute) EXECUTE=1 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

APPS="${QFLIX_APPS_DIR:-$HOME/.apps}"
APPDIR="$APPS/$SLUG"
UNIT_DIR="${QFLIX_UNIT_DIR:-$HOME/.config/systemd/user}"
ENV_DIR="${QFLIX_ENV_DIR:-$HOME/.config/qflix}"
SWAPDIR="${QFLIX_SWAP_DIR:-$HOME/.opt/maint/swap}/$SLUG"
PROVE="$APPS/.prove/$SLUG"
MANIFEST="${QFLIX_MANIFEST:-$HOME/.opt/maint/apps.yaml}"
MAINT_LIB="$HERE/maint/lib"
PY="${QFLIX_PYTHON:-python3}"
APPCTL="${QFLIX_APPCTL:-$HOME/bin/appctl}"
SYSTEMCTL="${QFLIX_SYSTEMCTL:-systemctl}"
SS="${QFLIX_SS:-ss}"
PS="${QFLIX_PS:-ps}"
RAR="${QFLIX_RAR:-rar}"
PROC="${QFLIX_PROC:-/proc}"
POLL="${QFLIX_POLL_S:-2}"
SETTLE="${QFLIX_SETTLE_S:-10}"
STOP_TIMEOUT="${QFLIX_STOP_TIMEOUT_S:-120}"
PROOF_TIMEOUT="${QFLIX_PROOF_TIMEOUT_S:-300}"
WANT_SHA="${QFLIX_UNPACKERR_SHA256:-$SHA256}"

hostpolicy() {
  if [ -n "${QFLIX_HOSTPOLICY:-}" ]; then "$QFLIX_HOSTPOLICY" "$@"
  else "$PY" "$MAINT_LIB/hostpolicy.py" "$@"; fi
}
swapstate()   { "$PY" "$MAINT_LIB/swapstate.py" "$@"; }
suppression() { "$PY" "$MAINT_LIB/suppression.py" "$@"; }
sysd()        { "$SYSTEMCTL" --user "$@"; }

# --- plan (DRY-RUN) ------------------------------------------------------------
plan() {
  info "DRY-RUN mode=$MODE (nothing touched; add --execute to run)"
  case "$MODE" in
    install)  info "would fetch $URL (sha256 $WANT_SHA), check version parity, lay out $APPDIR/bin/$VERSION, write $ENV_DIR/$SLUG.env, stage $APPDIR/native/$UNIT (not enabled)" ;;
    prove)    info "would boot an inert [[folder]]-only copy in $PROVE, extract a fixture rar, gate the task delta at 70% of the ceiling, then delete the copy" ;;
    swap)     info "would capture the listen set (port $PROBE_PORT, must be empty), path-audit, suppress ${SUPPRESS[*]}, snapshot, stop the container, wait for exit, enable --now $UNIT, verify, record swap state" ;;
    finish)   info "would verify the native unit and lift suppression for ${SUPPRESS[*]}" ;;
    rollback) info "would suppress ${SUPPRESS[*]}, park + mask $UNIT, stop it, wait for the manifest revert, start the container via appctl, unsuppress" ;;
  esac
  exit 0
}
[ "$EXECUTE" = 1 ] || plan

# --- run on the box --------------------------------------------------------------
_on_host() {
  local m="${QFLIX_HOST_ID_FILE:-$HOME/.config/qflix/host.id}"
  [ -s "$m" ] && return 0
  [ "$(hostname 2>/dev/null)" = "manitoba" ]
}
if ! _on_host; then
  # shellcheck source=/dev/null
  source "$HERE/lib/ssh.sh"
  info "not on the box: running the deployed copy there"
  sshm "~/scripts/configure/300-native-unpackerr-install.sh $(printf '%q ' "${ARGS[@]}")"
  exit $?
fi

# shellcheck source=/dev/null
source "$HERE/lib/native.sh" || die "cannot source $HERE/lib/native.sh (run 240 first)"

PROFILE="$(hostpolicy preflight)" || die "host profile unresolved; refusing (I-12)"
PROFILE="${PROFILE%$'\r'}"
if hostpolicy in-window; then
  die "inside the Monday maintenance window; no box operations"
fi
case "$MODE" in
  swap|finish|rollback)
    [ "$PROFILE" = ultra ] || die "mode $MODE swaps against a UCC container; host profile is '$PROFILE'" ;;
esac

T0=$SECONDS

# One EXIT trap for every temp path / proof process (a RETURN trap set inside a
# function stays armed globally and would fire on every later return).
CLEANUP_PATHS=()
PROOF_PID=""
cleanup() {
  [ -n "$PROOF_PID" ] && kill "$PROOF_PID" 2>/dev/null
  local p
  for p in "${CLEANUP_PATHS[@]}"; do rm -rf "$p"; done
}
trap cleanup EXIT

# GNU tar reads "C:/x" as host:path; --force-local keeps archive names local.
TAR=(tar --force-local)

# --- helpers -------------------------------------------------------------------------
# Deployed-manifest view of the app: "<class>|<swap_state>|<dormant 0/1>".
manifest_state() {
  "$PY" - "$MANIFEST" "$SLUG" <<'PY'
import sys
try:
    import yaml
    with open(sys.argv[1], encoding="utf-8") as fh:
        a = ((yaml.safe_load(fh) or {}).get("apps") or {}).get(sys.argv[2]) or {}
except Exception as exc:
    sys.stderr.write("manifest unreadable: %s\n" % exc)
    sys.exit(2)
print("|".join([str(a.get("class") or ""), str(a.get("swap_state") or ""),
                "0" if a.get("ucc_dormant") in (None, False) else "1"]))
PY
}

# PIDs under OUR uid whose cmdline carries the app pattern. kind=container: in
# a container cgroup (docker/libpod/...), not the unit's. kind=native: in the
# unit's cgroup. A `tail` of the log or this script is in neither.
scan_pids() {
  local kind="$1" uid d pid cg cmd
  uid="$(id -u)"
  for d in "$PROC"/[0-9]*; do
    [ -d "$d" ] || continue
    pid="${d##*/}"
    [ "$(awk '/^Uid:/ {print $2; exit}' "$d/status" 2>/dev/null)" = "$uid" ] || continue
    cg="$(cat "$d/cgroup" 2>/dev/null)" || continue
    cmd="$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)" || continue
    case "$cmd" in *"$PATTERN"*) ;; *) continue ;; esac
    if [ "$kind" = native ]; then
      case "$cg" in *"$UNIT"*) echo "$pid" ;; esac
    else
      case "$cg" in *"$UNIT"*) continue ;; esac
      if [[ "$cg" =~ docker|libpod|podman|containerd|crio ]]; then echo "$pid"; fi
    fi
  done
}

port_free() { ! "$SS" -tlnH "sport = :$PROBE_PORT" 2>/dev/null | grep -q ":$PROBE_PORT\b"; }

# wait_until <timeout_s> <predicate...>
wait_until() {
  local limit="$1"; shift
  local start=$SECONDS
  while :; do
    "$@" && return 0
    [ $((SECONDS - start)) -ge "$limit" ] && return 1
    sleep "$POLL"
  done
}
container_gone() { [ -z "$(scan_pids container)" ] && port_free; }
container_up()   { [ -n "$(scan_pids container)" ]; }
native_gone()    { [ -z "$(scan_pids native)" ]; }

user_tasks() { "$PS" -u "$(id -u)" -L --no-headers 2>/dev/null | wc -l | tr -d ' '; }

# Container paths (/config, /data, /downloads) mean nothing on the host (5.9 step 3).
path_audit() {
  local hits
  hits="$(grep -nE "[\"'](/config|/data|/downloads)([/\"'])" "$APPDIR/unpackerr.conf" || true)"
  [ -z "$hits" ] || die "container path(s) in $APPDIR/unpackerr.conf: $(echo "$hits" | head -3 | tr '\n' ' ')"
}

# systemd treats a unit linked to /dev/null OR an empty unit file as masked.
is_masked() {
  local f="$UNIT_DIR/$UNIT"
  [ -L "$f" ] || { [ -f "$f" ] && [ ! -s "$f" ]; }
}

mask_unit() {
  if is_masked; then return 0; fi                            # already masked
  if [ -f "$UNIT_DIR/$UNIT" ]; then
    sysd disable "$UNIT" >/dev/null 2>&1 || true
    mkdir -p "$ENV_DIR/parked-units"
    mv -f "$UNIT_DIR/$UNIT" "$ENV_DIR/parked-units/$UNIT" || die "cannot park $UNIT"
  fi
  # `systemctl mask` refuses while a real unit file sits in the same dir, hence
  # the park above.
  sysd mask "$UNIT" || die "systemctl --user mask $UNIT failed"
  is_masked || die "$UNIT not masked after mask"
  sysd daemon-reload || true
}

# --- modes ----------------------------------------------------------------------------
do_install() {
  local stage
  mkdir -p "$APPS" || die "cannot create $APPS"
  stage="$(mktemp -d "$APPS/.stage-$SLUG.XXXXXX")" || die "mktemp failed"
  CLEANUP_PATHS+=("$stage")
  native_fetch_verify "$URL" "$WANT_SHA" "$stage/u.tgz" || die "fetch/sha256 verify failed"
  mkdir -p "$stage/x"
  "${TAR[@]}" -xzf "$stage/u.tgz" -C "$stage/x" "$EXE" || die "tarball has no $EXE"
  [ -f "$stage/x/$EXE" ] || die "tarball has no $EXE"
  chmod 0755 "$stage/x/$EXE"
  native_install_versioned "$SLUG" "$VERSION" "$stage/x/$EXE" || die "install refused (see above)"
  native_render_env "$SLUG" "$FAMILY" "$VERSION" "$TZ_ENV" \
    | native_write_secure "$ENV_DIR/$SLUG.env" 0600 || die "env file write failed"
  native_render_unit "$SLUG" "$FAMILY" "$EXE" "$EXEC_ARGS" \
    | native_write_secure "$APPDIR/native/$UNIT" 0644 || die "unit staging failed"
  [ -f "$APPDIR/unpackerr.conf" ] || die "$APPDIR/unpackerr.conf missing (the config is used in place)"
  info "installed $VERSION; unit staged at $APPDIR/native/$UNIT (not enabled). Next: --prove --execute"
}

do_prove() {
  local before ceiling delta token
  [ -x "$APPDIR/bin/current/$EXE" ] || die "not installed; run --install --execute first"
  ceiling="$(hostpolicy task-ceiling)" || die "task ceiling unknown; refusing (G-2)"
  ceiling="${ceiling%$'\r'}"
  rm -rf "$PROVE"
  mkdir -p "$PROVE/watch" "$PROVE/extract" "$PROVE/stage/fixture-dir" || die "cannot create $PROVE"
  [ "${QFLIX_KEEP_PROOF:-0}" = 1 ] || CLEANUP_PATHS+=("$PROVE")
  # INERT (I-11): one [[folder]], nothing else. No arr section can grab or
  # import, no [webserver] can listen, no webhook can notify.
  cat >"$PROVE/unpackerr.conf" <<EOF
log_file = "$PROVE/unpackerr.log"
start_delay = "1s"

[folders]
interval = "5s"

[[folder]]
path = "$PROVE/watch"
extract_path = "$PROVE/extract"
delete_after = "0s"
EOF
  if grep -qE '^\[\[(sonarr|radarr|lidarr|readarr|whisparr|webhook|cmdhook)\]\]|^\[webserver\]' "$PROVE/unpackerr.conf"; then
    die "proof config is not inert"
  fi
  token="qflix-proof-$$-$RANDOM"
  printf '%s\n' "$token" > "$PROVE/stage/qflix-fixture.txt"
  "$RAR" a -ep -idq "$PROVE/stage/fixture-dir/fixture.rar" "$PROVE/stage/qflix-fixture.txt" \
    || die "cannot build the fixture rar"
  before="$(user_tasks)"
  [[ "$before" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  GOMAXPROCS=4 MALLOC_ARENA_MAX=2 "$APPDIR/bin/current/$EXE" -c "$PROVE/unpackerr.conf" \
    >"$PROVE/stdout.log" 2>&1 &
  PROOF_PID=$!
  sleep "$SETTLE"
  mv "$PROVE/stage/fixture-dir" "$PROVE/watch/" || die "cannot drop the fixture"
  found_ok() {
    local f
    f="$(find "$PROVE/extract" -type f -name qflix-fixture.txt 2>/dev/null | head -1)"
    [ -n "$f" ] && [ "$(cat "$f")" = "$token" ]
  }
  if ! wait_until "$PROOF_TIMEOUT" found_ok; then
    tail -5 "$PROVE/stdout.log" "$PROVE/unpackerr.log" 2>/dev/null >&2
    die "proof: fixture not extracted within ${PROOF_TIMEOUT}s"
  fi
  delta="$(ls "$PROC/$PROOF_PID/task" 2>/dev/null | wc -l | tr -d ' ')"
  [ "${delta:-0}" -gt 0 ] || die "proof: cannot read the task count of pid $PROOF_PID; refusing"
  kill "$PROOF_PID" 2>/dev/null; wait "$PROOF_PID" 2>/dev/null; PROOF_PID=""
  if [ $(( (before + delta) * 100 )) -ge $(( 70 * ceiling )) ]; then
    die "thread gate: $before + $delta tasks reaches 70% of the ceiling $ceiling; refusing the swap"
  fi
  mkdir -p "$SWAPDIR"
  printf '{"ok": true, "version": "%s", "before": %s, "delta": %s, "ceiling": %s, "at": "%s"}\n' \
    "$VERSION" "$before" "$delta" "$ceiling" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    | native_write_secure "$SWAPDIR/proof.json" 0644 || die "cannot record the proof"
  info "PROOF OK: fixture extracted; before=$before delta=$delta ceiling=$ceiling. Next: --swap --execute"
}

verify_native() {
  local n
  sleep "$SETTLE"
  sysd is-active "$UNIT" >/dev/null 2>&1 || { echo "unit not active" >&2; return 1; }
  [ -z "$(scan_pids container)" ] || { echo "a container process is running" >&2; return 1; }
  n="$(scan_pids native | wc -l | tr -d ' ')"
  [ "$n" -ge 1 ] || { echo "no native process" >&2; return 1; }
  native_listen_compare "$SLUG" >/dev/null || { echo "listen set differs from listen-set.before" >&2; return 1; }
}

do_swap() {
  local st cls ss_state ver snap now soak
  [ -f "$SWAPDIR/proof.json" ] || die "no proof recorded; run --prove --execute first"
  [ -x "$APPDIR/bin/current/$EXE" ] && [ -f "$APPDIR/native/$UNIT" ] && [ -f "$ENV_DIR/$SLUG.env" ] \
    || die "not installed; run --install --execute first"
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state _ <<<"$st"
  [ "$cls" = systemd ] && [ "$ss_state" = pending-swap ] \
    || die "the deployed manifest is not the pending-swap flip (class=$cls swap_state=$ss_state); merge + deploy it via 240 first"

  if sysd is-active "$UNIT" >/dev/null 2>&1 && [ -z "$(scan_pids container)" ]; then
    info "already swapped; verifying only"
    verify_native || die "native unpackerr fails parity; run --rollback --execute"
    info "verified"
    return 0
  fi

  # Rollback step 4: unmask only at the next forward swap.
  if is_masked; then
    sysd unmask "$UNIT" || die "unmask $UNIT failed"
    rm -f "$ENV_DIR/parked-units/$UNIT"
  fi

  # Step 3: capture + audits (no listener -> no auth-bypass setting to assert).
  ver="$(native_ucc_version "$SLUG")"
  [ "${ver#v}" = "$VERSION" ] || die "version parity: container=$ver native=$VERSION"
  native_listen_capture "$SLUG" "$PROBE_PORT" >/dev/null || die "listen-set capture failed"
  [ ! -s "$SWAPDIR/listen-set.before" ] \
    || die "listen set on :$PROBE_PORT is not empty ($(tr '\n' ' ' < "$SWAPDIR/listen-set.before")); unpackerr must have no listener"
  swapstate set "$SLUG" "ucc_version=$VERSION" >/dev/null || die "cannot record ucc_version"
  path_audit

  # Step 4: suppress the app and its canaries together.
  suppression add "${SUPPRESS[@]}" --reason "QFLX-25 swap to native" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to swap unsuppressed"

  # Step 5: snapshot (the config is the only state).
  snap="$SWAPDIR/snapshot-$(date -u +%Y%m%dT%H%M%SZ).tgz"
  "${TAR[@]}" -czf "$snap" -C "$APPS" --exclude="$SLUG/bin" --exclude="$SLUG/native" \
      --exclude="$SLUG/*.log*" "$SLUG" || die "snapshot failed"

  # Step 6: stop the container; its exit is asynchronous Docker behaviour, so
  # the STATE decides, not the exit code.
  "$APPCTL" stop "$SLUG" >/dev/null 2>&1 || info "appctl stop returned non-zero; polling decides"
  if ! wait_until "$STOP_TIMEOUT" container_gone; then
    "$APPCTL" start "$SLUG" >/dev/null 2>&1 || true
    suppression remove "${SUPPRESS[@]}" >/dev/null || true
    die "the container did not exit within ${STOP_TIMEOUT}s (pids: $(scan_pids container | tr '\n' ' ')); swap aborted, container start requested, suppression lifted"
  fi
  mkdir -p "$UNIT_DIR"
  native_write_secure "$UNIT_DIR/$UNIT" 0644 < "$APPDIR/native/$UNIT" || die "unit install failed"
  sysd daemon-reload || die "daemon-reload failed"
  sysd enable --now "$UNIT" || die "enable --now $UNIT failed; run --rollback --execute"

  # Step 8 (in-place part): parity.
  if ! verify_native; then
    die "native unpackerr fails parity after the swap; suppression kept ON; run --rollback --execute"
  fi
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  soak="$(date -u -d '+14 days' +%Y-%m-%dT%H:%M:%SZ)" || die "cannot compute soak_until"
  swapstate set "$SLUG" "swap_date=$now" "soak_until=$soak" "rollback_window=open" >/dev/null \
    || die "cannot record swap state"
  info "SWAPPED to native $VERSION; soak until $soak. elapsed=$((SECONDS - T0))s"
  info "Next: PR dropping swap_state: pending-swap, deploy via 240, then --finish --execute"
}

do_finish() {
  local st cls ss_state dormant
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state dormant <<<"$st"
  [ "$cls" = systemd ] && [ -z "$ss_state" ] && [ "$dormant" = 1 ] \
    || die "deployed manifest still says class=$cls swap_state=${ss_state:-none} dormant=$dormant; deploy the follow-up (no pending-swap) first"
  verify_native || die "native unpackerr fails parity; run --rollback --execute"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "FINISHED: ${SUPPRESS[*]} unsuppressed; 14-day soak running"
}

do_rollback() {
  local isn
  # Step 0: re-suppress + mask BEFORE anything stops.
  suppression add "${SUPPRESS[@]}" --reason "QFLX-25 rollback to UCC" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to roll back unsuppressed"
  mask_unit
  # Step 1: disable --now (disabled while parking; stop now).
  sysd stop "$UNIT" >/dev/null 2>&1 || true
  wait_until "$STOP_TIMEOUT" native_gone || die "native unpackerr did not stop within ${STOP_TIMEOUT}s"
  # Step 2: the DEPLOYED manifest must dispatch unpackerr as UCC again.
  isn="$("$APPCTL" is-native "$SLUG" 2>/dev/null)"
  if [ "${isn%$'\r'}" != ucc ]; then
    echo "[300-unpackerr] PAUSED: native stopped + masked; revert the deployed manifest (PR + 240) so appctl dispatches $SLUG as UCC, then re-run --rollback --execute" >&2
    exit 10
  fi
  # Step 3.
  "$APPCTL" start "$SLUG" >/dev/null 2>&1 || info "appctl start returned non-zero; polling decides"
  wait_until "$STOP_TIMEOUT" container_up || die "the container did not come back within ${STOP_TIMEOUT}s; still suppressed"
  # Step 5: nothing to restore (stateless; the versions match, no migration ran).
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "ROLLED BACK to UCC; $UNIT masked (unmasked by the next --swap). elapsed=$((SECONDS - T0))s"
}

case "$MODE" in
  install)  do_install ;;
  prove)    do_prove ;;
  swap)     do_swap ;;
  finish)   do_finish ;;
  rollback) do_rollback ;;
esac
