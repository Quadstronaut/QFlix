#!/usr/bin/env bash
# 301-native-flaresolverr-install.sh -- QFLX-26 (UCC divorce A2, convert flaresolverr).
#
# Moves flaresolverr off the Ultra.cc container manager (UCC) onto a user unit
# the repo owns: the upstream PyInstaller linux-x64 release (bundled Chromium) at
# EXACTLY the container's version, sha256-pinned, run as qflix-flaresolverr.service.
# Spec: docs/superpowers/specs/2026-10-09-ucc-divorce-design.md 5.1-5.9.
# Same shape as the pilot, scripts/configure/300-native-unpackerr-install.sh.
#
# RUNS ON THE BOX. 240-maintenance-install.sh deploys it to ~/scripts/configure/
# with ~/scripts/lib/native.sh beside it. Started from the workstation it
# re-executes the deployed copy over ssh (scripts/lib/ssh.sh).
#
# INERT BY DEFAULT (I-3). Every mode prints its plan (DRY-RUN) and touches
# nothing unless --execute is also given. Modes, in swap order:
#
#   --precheck  Dependency probe, the D-7 gate. Fetch + sha256-verify the
#               release into a scratch dir, `ldd` the bundled chrome and the
#               bootloader, refuse (exit 3, BLOCKED) when any shared library is
#               "not found". Installs nothing. Xvfb is only warned about here:
#               --prove decides it empirically (see below).
#   --install   5.9 step 1. The same probe, then parity with `appctl version
#               flaresolverr` (I-10), bin/<ver> + `current`, the env file and
#               the STAGED unit in ~/.apps/flaresolverr/native/. NOT copied into
#               ~/.config/systemd/user and NOT enabled (WantedBy=default.target
#               would start it beside the live container, I-6).
#   --prove     5.9 step 2. Inert proof: the native build on a FRESH 127.0.0.1
#               port, HEADLESS=true, the unit's PATH; a local fixture page
#               served from 127.0.0.1; POST /v1 request.get must return a
#               solution that carries the fixture token. The process-tree task
#               count is sampled while the request runs (delta). Refused when
#               current + delta reaches 70% of the task ceiling (G-2); BLOCKED
#               (exit 3, D-7) when the request fails (missing Xvfb/libs) or the
#               delta exceeds QFLIX_FS_DELTA_MAX (80). Never touches the live
#               port. Writes swap/flaresolverr/proof.json.
#   --swap      5.9 steps 3-6: needs the proof and the DEPLOYED pending-swap
#               manifest flip. Refuses while the self-destructing unsuppress
#               watcher units exist (O-8: it would lift suppression mid-swap as
#               soon as the port answers). Unmask (rollback step 4), parity,
#               capture the listen set (must be EXACTLY net.app_host:<port>),
#               suppress the app + prowlarr-proxy-link-fatal +
#               prowlarr-indexer-health + thread-ceiling, stop the container
#               through appctl, POLL until no container process is left and the
#               port is free (abort and restore the container otherwise),
#               enable --now, verify parity (unit active, listen set identical,
#               GET / answers "FlareSolverr is ready"), record swap state
#               (14-day soak). Suppression stays ON.
#   --finish    5.9 step 9, after the follow-up PR dropped `swap_state` and 240
#               deployed it: verify, then lift every suppression together.
#   --rollback  Rollback 0-5. 0: re-suppress, park the unit and MASK it. 1: stop
#               it and wait for exit AND the port to free. 2: the DEPLOYED
#               manifest must dispatch flaresolverr as UCC again (otherwise exit
#               10; re-run after the revert PR + 240). 3: start the container
#               through appctl. 5: nothing to restore (stateless).
#
# The bind is NEVER widened: HOST=<net.app_host, the Docker bridge gateway> only (an auth-less Chromium proxy;
# never 0.0.0.0, never loopback-plus-public).
#
# Exit: 0 ok | 1 refused/failed | 3 BLOCKED by the dependency/thread gate (stay
# UCC, convert on box 2) | 10 rollback paused for the manifest revert | 64 usage.
#
# Overrides (tests; resolved at call time): QFLIX_APPS_DIR QFLIX_UNIT_DIR
# QFLIX_ENV_DIR QFLIX_SWAP_DIR MANITOBA_STATE_DIR QFLIX_MANIFEST QFLIX_PROC
# QFLIX_PYTHON QFLIX_APPCTL QFLIX_SYSTEMCTL QFLIX_SS QFLIX_PS QFLIX_LDD
# QFLIX_XVFB QFLIX_CURL (download) QFLIX_HTTP_CURL (prove) QFLIX_PROBE_CURL
# (live probe) QFLIX_HOSTPOLICY QFLIX_HOST_ID_FILE QFLIX_FLARESOLVERR_SHA256
# QFLIX_SECRETS_DIR QFLIX_POLL_S QFLIX_SETTLE_S QFLIX_STOP_TIMEOUT_S
# QFLIX_PROOF_TIMEOUT_S QFLIX_FS_DELTA_MAX QFLIX_KEEP_PROOF.
set -uo pipefail

SLUG=flaresolverr
VERSION="3.5.2"              # == versions.env FLARESOLVERR_VERSION (test-pinned)
SHA256="84f6df48849b2e1742692841805c4f284118fe4e2798b49d3a78159e89a46a91"
URL="https://github.com/FlareSolverr/FlareSolverr/releases/download/v${VERSION}/flaresolverr_linux_x64.tar.gz"
UNIT="qflix-${SLUG}.service"
FAMILY=python
EXE=flaresolverr
EXEC_ARGS=""
# The one and only bind address: the net.app_host secret (the Docker bridge
# gateway; manifest comment + inventory.md row). Resolved and validated by
# check_bind_host; the live listen set must then equal exactly <it>:<port>.
BIND_HOST=""
# Muted with the app (plan row A2). Keys follow cli.py: canary-<name>.
SUPPRESS=("$SLUG" "canary-prowlarr-proxy-link-fatal" "canary-prowlarr-indexer-health" "canary-thread-ceiling")
# The self-destructing watcher (flaresolverr-unsuppress-watch.sh) lifts the
# suppression above the moment the port answers: units must be gone (O-8).
WATCHER_UNITS=("manitoba-maint-${SLUG}-unsuppress.timer" "manitoba-maint-${SLUG}-unsuppress.service")
PATTERN="flaresolverr"       # matches /app/flaresolverr.py (container) and bin/current/flaresolverr (native)
# Container env worth carrying over (names only; PROXY_* may hold credentials
# and is never copied). Value must be a boring token.
ENV_KEYS=(LOG_LEVEL LOG_HTML CAPTCHA_SOLVER BROWSER_TIMEOUT TEST_URL TZ LANG)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # .../scripts
ARGS=("$@")

info() { echo "[301-flaresolverr] $*"; }
die()  { echo "[301-flaresolverr] ERROR: $*" >&2; exit 1; }
blocked() {
  echo "[301-flaresolverr] BLOCKED (D-7): $*" >&2
  echo "[301-flaresolverr] flaresolverr stays a UCC app on this slot; it converts on box 2." >&2
  exit 3
}
usage() {
  echo "usage: $0 [--precheck|--install|--prove|--swap|--finish|--rollback] [--execute]" >&2
  exit 64
}

MODE=install
EXECUTE=0
for a in "$@"; do
  case "$a" in
    --precheck|--install|--prove|--swap|--finish|--rollback) MODE="${a#--}" ;;
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
SECRETS="${QFLIX_SECRETS_DIR:-$HOME/secrets}"
MAINT_LIB="$HERE/maint/lib"
PY="${QFLIX_PYTHON:-python3}"
APPCTL="${QFLIX_APPCTL:-$HOME/bin/appctl}"
SYSTEMCTL="${QFLIX_SYSTEMCTL:-systemctl}"
SS="${QFLIX_SS:-ss}"
PS="${QFLIX_PS:-ps}"
LDD="${QFLIX_LDD:-ldd}"
XVFB="${QFLIX_XVFB:-Xvfb}"
HTTP_CURL="${QFLIX_HTTP_CURL:-curl}"
PROBE_CURL="${QFLIX_PROBE_CURL:-curl}"
PROC="${QFLIX_PROC:-/proc}"
POLL="${QFLIX_POLL_S:-2}"
SETTLE="${QFLIX_SETTLE_S:-10}"
STOP_TIMEOUT="${QFLIX_STOP_TIMEOUT_S:-120}"
PROOF_TIMEOUT="${QFLIX_PROOF_TIMEOUT_S:-180}"
DELTA_MAX="${QFLIX_FS_DELTA_MAX:-80}"
WANT_SHA="${QFLIX_FLARESOLVERR_SHA256:-$SHA256}"
# Test-only knob: where the staged-unit PATH looks for Xvfb. Left empty on the box.
UNIT_PATH_EXTRA="${QFLIX_PROVE_PATH:-}"

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
    precheck) info "would fetch $URL (sha256 $WANT_SHA) into a scratch dir, ldd the bundled chrome, refuse (BLOCKED, exit 3) if any library is missing, then delete the scratch dir" ;;
    install)  info "would run the precheck, check version parity, lay out $APPDIR/bin/$VERSION, write $ENV_DIR/$SLUG.env (HOST=$BIND_HOST), stage $APPDIR/native/$UNIT (not enabled)" ;;
    prove)    info "would boot the native build on a fresh 127.0.0.1 port, POST /v1 request.get against a local fixture, gate the task delta at 70% of the ceiling and ${DELTA_MAX} tasks, then delete the copy" ;;
    swap)     info "would require the unsuppress watcher units gone, capture the listen set (exactly $BIND_HOST:<port>), suppress ${SUPPRESS[*]}, stop the container, wait for exit, enable --now $UNIT, verify, record swap state" ;;
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
  sshm "~/scripts/configure/301-native-flaresolverr-install.sh $(printf '%q ' "${ARGS[@]}")"
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

CLEANUP_PATHS=()
PROOF_PID=""
FIX_PID=""
cleanup() {
  [ -n "$PROOF_PID" ] && { kill "$PROOF_PID" 2>/dev/null; wait "$PROOF_PID" 2>/dev/null; }
  [ -n "$FIX_PID" ] && { kill "$FIX_PID" 2>/dev/null; wait "$FIX_PID" 2>/dev/null; }
  sleep 0.3
  local p
  for p in "${CLEANUP_PATHS[@]}"; do rm -rf "$p"; done
}
trap cleanup EXIT

TAR=(tar --force-local)

# --- helpers -------------------------------------------------------------------------
# The listen port is the recorded one (secret flaresolverr.port, == the Prowlarr
# proxy URL). Unknown fails closed.
fs_port() {
  local p
  p="$(tr -d '[:space:]' < "$SECRETS/flaresolverr.port" 2>/dev/null)"
  [[ "$p" =~ ^[0-9]{2,5}$ ]] || return 1
  printf '%s' "$p"
}
# net.app_host is THE bind. Refuse anything that is not a plain IPv4 bridge
# address: empty, the wildcard, loopback or a name would each widen or move the
# auth-less Chromium proxy.
check_bind_host() {
  local h
  h="$(tr -d '[:space:]' < "$SECRETS/net.app_host" 2>/dev/null)"
  [[ "$h" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] && [ "$h" != "0.0.0.0" ] && [[ "$h" != 127.* ]]     || die "secret net.app_host='$h' is not a bridge address (empty, wildcard, loopback or non-IPv4); refusing (never widen the bind)"
  BIND_HOST="$h"
}

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

# Is anything still LISTENing on the live port?
port_free() {
  local port; port="$(fs_port)" || return 1
  ! "$SS" -tlnH "sport = :$port" 2>/dev/null | grep -q ":$port\b"
}

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
# A live container legitimately holds the port (nothing was swapped); otherwise the
# native unit's listener must be gone before the container may start on it.
native_gone()    { [ -z "$(scan_pids native)" ] && { container_up || port_free; }; }

user_tasks() { "$PS" -u "$(id -u)" -L --no-headers 2>/dev/null | wc -l | tr -d ' '; }

# Total tasks (threads) of a process and all its descendants, from $PROC.
tree_tasks() {
  "$PY" - "$PROC" "$1" <<'PY' | tr -d '\r'
import os, sys
proc, root = sys.argv[1], sys.argv[2]
kids, tasks = {}, {}
for pid in os.listdir(proc):
    if not pid.isdigit():
        continue
    try:
        with open(os.path.join(proc, pid, "status"), encoding="utf-8", errors="replace") as fh:
            ppid = next((l.split()[1] for l in fh if l.startswith("PPid:")), "0")
        tasks[pid] = len(os.listdir(os.path.join(proc, pid, "task")))
    except OSError:
        continue
    kids.setdefault(ppid, []).append(pid)
seen, todo, total = set(), [root], 0
while todo:
    p = todo.pop()
    if p in seen:
        continue
    seen.add(p)
    total += tasks.get(p, 0)
    todo.extend(kids.get(p, []))
print(total)
PY
}

is_masked() {
  local f="$UNIT_DIR/$UNIT"
  [ -L "$f" ] || { [ -f "$f" ] && [ ! -s "$f" ]; }
}

mask_unit() {
  if is_masked; then return 0; fi
  if [ -f "$UNIT_DIR/$UNIT" ]; then
    sysd disable "$UNIT" >/dev/null 2>&1 || true
    mkdir -p "$ENV_DIR/parked-units"
    mv -f "$UNIT_DIR/$UNIT" "$ENV_DIR/parked-units/$UNIT" || die "cannot park $UNIT"
  fi
  sysd mask "$UNIT" || die "systemctl --user mask $UNIT failed"
  is_masked || die "$UNIT not masked after mask"
  sysd daemon-reload || true
}

# Ready probe against the LIVE bind: HTTP 200 and the "FlareSolverr is ready" body.
probe_ready() {
  local port body
  port="$(fs_port)" || return 1
  body="$("$PROBE_CURL" -s -m 8 "http://$BIND_HOST:$port/" 2>/dev/null)" || return 1
  case "$body" in *"FlareSolverr is ready"*) return 0 ;; *) return 1 ;; esac
}

# The unit PATH is part of the golden unit; the proof runs under the same one.
unit_path() { printf '%s' "$APPDIR/bin/current:$HOME/bin:/usr/local/bin:/usr/bin:/bin"; }

# Whitelisted, boring-valued env of the running container -> KEY=VALUE lines.
container_env() {
  local pid k v line
  pid="$(scan_pids container | head -1)"
  [ -n "$pid" ] && [ -r "$PROC/$pid/environ" ] || return 0
  for k in "${ENV_KEYS[@]}"; do
    line="$(tr '\0' '\n' < "$PROC/$pid/environ" 2>/dev/null | grep -m1 "^${k}=")" || continue
    v="${line#*=}"
    [[ "$v" =~ ^[A-Za-z0-9._:/,+-]{1,64}$ ]] && printf '%s=%s\n' "$k" "$v"
  done
}

# --- dependency probe (D-7) ---------------------------------------------------------
# Args: directory holding the extracted `flaresolverr/` tree.
dep_probe() {
  local root="$1" missing="" f out
  [ -f "$root/flaresolverr/$EXE" ] || die "tarball has no flaresolverr/$EXE"
  for f in "$root/flaresolverr/$EXE" "$root/flaresolverr/_internal/chrome/chrome"; do
    [ -f "$f" ] || { missing="$missing $f(absent)"; continue; }
    out="$("$LDD" "$f" 2>&1)" || die "ldd failed on $f: $(echo "$out" | head -2 | tr '\n' ' ') (fail closed)"
    if echo "$out" | grep -q "not found"; then
      missing="$missing $(echo "$out" | grep 'not found' | awk '{print $1}' | tr '\n' ',')"
    fi
  done
  [ -z "$missing" ] || blocked "missing libraries for the bundled chrome/bootloader:$missing"
  if ! command -v "$XVFB" >/dev/null 2>&1; then
    info "WARN: $XVFB not found on PATH; --prove decides empirically whether the HEADLESS build runs without it"
  fi
  info "dependency probe: every shared library resolves"
}

fetch_extract() {
  local stage="$1"
  native_fetch_verify "$URL" "$WANT_SHA" "$stage/f.tgz" || die "fetch/sha256 verify failed"
  mkdir -p "$stage/x"
  "${TAR[@]}" -xzf "$stage/f.tgz" -C "$stage/x" || die "cannot extract the release"
  rm -f "$stage/f.tgz"
}

# --- modes ----------------------------------------------------------------------------
do_precheck() {
  local stage
  mkdir -p "$APPS" || die "cannot create $APPS"
  stage="$(mktemp -d "$APPS/.stage-$SLUG.XXXXXX")" || die "mktemp failed"
  CLEANUP_PATHS+=("$stage")
  fetch_extract "$stage"
  dep_probe "$stage/x"
  info "PRECHECK OK (nothing installed)"
}

do_install() {
  local stage port
  port="$(fs_port)" || die "secret flaresolverr.port missing/invalid; refusing"
  check_bind_host
  mkdir -p "$APPS" || die "cannot create $APPS"
  stage="$(mktemp -d "$APPS/.stage-$SLUG.XXXXXX")" || die "mktemp failed"
  CLEANUP_PATHS+=("$stage")
  fetch_extract "$stage"
  dep_probe "$stage/x"
  chmod 0755 "$stage/x/flaresolverr/$EXE" "$stage/x/flaresolverr/_internal/chrome/chrome" 2>/dev/null
  native_install_versioned "$SLUG" "$VERSION" "$stage/x/flaresolverr" || die "install refused (see above)"
  mkdir -p "$APPDIR/state/config" "$APPDIR/state/cache"
  local -a extra=("HOST=$BIND_HOST" "PORT=$port" "HEADLESS=true"
                  "XDG_CONFIG_HOME=$APPDIR/state/config" "XDG_CACHE_HOME=$APPDIR/state/cache")
  local kv k
  # Container config wins over defaults for the whitelisted keys.
  local -A seen=()
  while IFS= read -r kv; do
    [ -n "$kv" ] || continue
    extra+=("$kv"); seen["${kv%%=*}"]=1
  done < <(container_env)
  [ -n "${seen[LOG_LEVEL]:-}" ] || extra+=("LOG_LEVEL=info")
  native_render_env "$SLUG" "$FAMILY" "$VERSION" "${extra[@]}" \
    | native_write_secure "$ENV_DIR/$SLUG.env" 0600 || die "env file write failed"
  native_render_unit "$SLUG" "$FAMILY" "$EXE" "$EXEC_ARGS" | sed 's/[ \t]*$//' \
    | native_write_secure "$APPDIR/native/$UNIT" 0644 || die "unit staging failed"
  info "installed $VERSION; unit staged at $APPDIR/native/$UNIT (not enabled). Next: --prove --execute"
}

do_prove() {
  local before ceiling delta peak sample fixport proofport token body rc start
  [ -x "$APPDIR/bin/current/$EXE" ] || die "not installed; run --install --execute first"
  ceiling="$(hostpolicy task-ceiling)" || die "task ceiling unknown; refusing (G-2)"
  ceiling="${ceiling%$'\r'}"
  rm -rf "$PROVE"
  mkdir -p "$PROVE/site" "$PROVE/config" "$PROVE/cache" || die "cannot create $PROVE"
  [ "${QFLIX_KEEP_PROOF:-0}" = 1 ] || CLEANUP_PATHS+=("$PROVE")
  token="qflix-proof-$$-$RANDOM"
  printf '<html><body><p id="t">%s</p></body></html>\n' "$token" > "$PROVE/site/index.html"
  read -r fixport proofport < <("$PY" -c 'import socket
a,b=socket.socket(),socket.socket()
a.bind(("127.0.0.1",0)); b.bind(("127.0.0.1",0))
print(a.getsockname()[1], b.getsockname()[1])' | tr -d '\r')
  [ -n "$fixport" ] && [ -n "$proofport" ] || die "cannot pick free loopback ports"
  # Fixture page on loopback only: no outbound dependency, nothing to challenge.
  ( cd "$PROVE/site" && exec "$PY" -m http.server "$fixport" --bind 127.0.0.1 >/dev/null 2>&1 ) &
  FIX_PID=$!
  before="$(user_tasks)"
  [[ "$before" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  ( cd "$PROVE" && exec env -u DISPLAY -u XAUTHORITY HOME="$HOME" PATH="$(unit_path)${UNIT_PATH_EXTRA:+:$UNIT_PATH_EXTRA}" \
      HOST=127.0.0.1 PORT="$proofport" HEADLESS=true LOG_LEVEL=info MALLOC_ARENA_MAX=2 \
      XDG_CONFIG_HOME="$PROVE/config" XDG_CACHE_HOME="$PROVE/cache" QFLIX_PROC="$PROC" QFLIX_PYTHON="$PY" \
      "$APPDIR/bin/current/$EXE" >"$PROVE/stdout.log" 2>&1 ) &
  PROOF_PID=$!
  start=$SECONDS
  ready() { "$HTTP_CURL" -s -m 5 "http://127.0.0.1:$proofport/" 2>/dev/null | grep -q "FlareSolverr is ready"; }
  if ! wait_until "$PROOF_TIMEOUT" ready; then
    tail -5 "$PROVE/stdout.log" >&2
    blocked "the native build never answered on 127.0.0.1:$proofport within ${PROOF_TIMEOUT}s (missing Xvfb or libraries?)"
  fi
  peak="$(tree_tasks "$PROOF_PID")"
  # The request runs in the background so the tree can be sampled while Chromium is up.
  "$HTTP_CURL" -s -m "$PROOF_TIMEOUT" -H 'Content-Type: application/json' \
    -d "{\"cmd\":\"request.get\",\"url\":\"http://127.0.0.1:$fixport/\",\"maxTimeout\":60000}" \
    "http://127.0.0.1:$proofport/v1" >"$PROVE/response.json" 2>/dev/null &
  local req=$!
  while kill -0 "$req" 2>/dev/null; do
    sample="$(tree_tasks "$PROOF_PID")"
    [ "${sample:-0}" -gt "${peak:-0}" ] && peak="$sample"
    sleep "$POLL"
  done
  wait "$req"; rc=$?
  sample="$(tree_tasks "$PROOF_PID")"; [ "${sample:-0}" -gt "${peak:-0}" ] && peak="$sample"
  body="$(cat "$PROVE/response.json" 2>/dev/null)"
  if [ "$rc" != 0 ] || ! echo "$body" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
sys.exit(0 if d.get("status") == "ok" and sys.argv[1] in str(d.get("solution", {}).get("response", "")) else 1)' "$token"; then
    tail -5 "$PROVE/stdout.log" >&2
    blocked "POST /v1 request.get returned no solution carrying the fixture (rc=$rc)"
  fi
  delta="$peak"
  kill "$PROOF_PID" 2>/dev/null; wait "$PROOF_PID" 2>/dev/null; PROOF_PID=""
  kill "$FIX_PID" 2>/dev/null; FIX_PID=""
  [[ "$delta" =~ ^[0-9]+$ ]] && [ "$delta" -gt 0 ] || die "proof: cannot read the task count; refusing"
  if [ "$delta" -gt "$DELTA_MAX" ]; then
    blocked "task delta $delta exceeds $DELTA_MAX (before=$before ceiling=$ceiling)"
  fi
  if [ $(( (before + delta) * 100 )) -ge $(( 70 * ceiling )) ]; then
    die "thread gate: $before + $delta tasks reaches 70% of the ceiling $ceiling; refusing the swap"
  fi
  mkdir -p "$SWAPDIR"
  printf '{"ok": true, "version": "%s", "before": %s, "delta": %s, "ceiling": %s, "at": "%s"}\n' \
    "$VERSION" "$before" "$delta" "$ceiling" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    | native_write_secure "$SWAPDIR/proof.json" 0644 || die "cannot record the proof"
  info "PROOF OK: request.get returned the fixture; before=$before delta=$delta ceiling=$ceiling elapsed=$((SECONDS - start))s. Next: --swap --execute"
}

verify_native() {
  local n
  sleep "$SETTLE"
  sysd is-active "$UNIT" >/dev/null 2>&1 || { echo "unit not active" >&2; return 1; }
  [ -z "$(scan_pids container)" ] || { echo "a container process is running" >&2; return 1; }
  n="$(scan_pids native | wc -l | tr -d ' ')"
  [ "$n" -ge 1 ] || { echo "no native process" >&2; return 1; }
  native_listen_compare "$SLUG" >/dev/null || { echo "listen set differs from listen-set.before" >&2; return 1; }
  wait_until 60 probe_ready || { echo "GET / on $BIND_HOST did not answer 'FlareSolverr is ready'" >&2; return 1; }
}

do_swap() {
  local st cls ss_state ver port f
  [ -f "$SWAPDIR/proof.json" ] || die "no proof recorded; run --prove --execute first"
  [ -x "$APPDIR/bin/current/$EXE" ] && [ -f "$APPDIR/native/$UNIT" ] && [ -f "$ENV_DIR/$SLUG.env" ] \
    || die "not installed; run --install --execute first"
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state _ <<<"$st"
  [ "$cls" = systemd ] && [ "$ss_state" = pending-swap ] \
    || die "the deployed manifest is not the pending-swap flip (class=$cls swap_state=$ss_state); merge + deploy it via 240 first"
  check_bind_host
  port="$(fs_port)" || die "secret flaresolverr.port missing/invalid; refusing"
  grep -qx "HOST=$BIND_HOST" "$ENV_DIR/$SLUG.env" && grep -qx "PORT=$port" "$ENV_DIR/$SLUG.env" \
    || die "$ENV_DIR/$SLUG.env does not bind exactly $BIND_HOST:$port; re-run --install --execute"

  if sysd is-active "$UNIT" >/dev/null 2>&1 && [ -z "$(scan_pids container)" ]; then
    info "already swapped; verifying only"
    verify_native || die "native flaresolverr fails parity; run --rollback --execute"
    info "verified"
    return 0
  fi

  # O-8: the watcher lifts suppression the moment the port answers.
  for f in "${WATCHER_UNITS[@]}"; do
    if [ -e "$UNIT_DIR/$f" ] || [ -e "$UNIT_DIR/timers.target.wants/$f" ]; then
      die "$f still exists: the unsuppress watcher would lift suppression mid-swap (O-8); remove it first"
    fi
  done

  if is_masked; then
    sysd unmask "$UNIT" || die "unmask $UNIT failed"
    rm -f "$ENV_DIR/parked-units/$UNIT"
  fi

  ver="$(native_ucc_version "$SLUG")"
  [ "${ver#v}" = "$VERSION" ] || die "version parity: container=$ver native=$VERSION"
  native_listen_capture "$SLUG" "$port" >/dev/null || die "listen-set capture failed"
  [ "$(cat "$SWAPDIR/listen-set.before" 2>/dev/null)" = "$BIND_HOST:$port" ] \
    || die "listen set is not exactly $BIND_HOST:$port ($(tr '\n' ' ' < "$SWAPDIR/listen-set.before" 2>/dev/null)); refusing (auth-less Chromium proxy)"
  swapstate set "$SLUG" "ucc_version=$VERSION" >/dev/null || die "cannot record ucc_version"
  container_env > "$SWAPDIR/container-env.before" 2>/dev/null || true

  suppression add "${SUPPRESS[@]}" --reason "QFLX-26 swap to native" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to swap unsuppressed"

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

  if ! verify_native; then
    die "native flaresolverr fails parity after the swap; suppression kept ON; run --rollback --execute"
  fi
  local now soak
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  soak="$(date -u -d '+14 days' +%Y-%m-%dT%H:%M:%SZ)" || die "cannot compute soak_until"
  swapstate set "$SLUG" "swap_date=$now" "soak_until=$soak" "rollback_window=open" >/dev/null \
    || die "cannot record swap state"
  info "SWAPPED to native $VERSION on $BIND_HOST:$port; soak until $soak. elapsed=$((SECONDS - T0))s"
  info "Next: prowlarr indexer-proxy test, PR dropping swap_state: pending-swap, deploy via 240, then --finish --execute"
}

do_finish() {
  local st cls ss_state dormant
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state dormant <<<"$st"
  [ "$cls" = systemd ] && [ -z "$ss_state" ] && [ "$dormant" = 1 ] \
    || die "deployed manifest still says class=$cls swap_state=${ss_state:-none} dormant=$dormant; deploy the follow-up (no pending-swap) first"
  verify_native || die "native flaresolverr fails parity; run --rollback --execute"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "FINISHED: ${SUPPRESS[*]} unsuppressed; 14-day soak running"
}

do_rollback() {
  local isn
  suppression add "${SUPPRESS[@]}" --reason "QFLX-26 rollback to UCC" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to roll back unsuppressed"
  mask_unit
  sysd stop "$UNIT" >/dev/null 2>&1 || true
  # Same address as the container: the port must be free before it can start.
  wait_until "$STOP_TIMEOUT" native_gone || die "native flaresolverr did not stop (or the port stayed bound) within ${STOP_TIMEOUT}s"
  isn="$("$APPCTL" is-native "$SLUG" 2>/dev/null)"
  if [ "${isn%$'\r'}" != ucc ]; then
    echo "[301-flaresolverr] PAUSED: native stopped + masked; revert the deployed manifest (PR + 240) so appctl dispatches $SLUG as UCC, then re-run --rollback --execute" >&2
    exit 10
  fi
  "$APPCTL" start "$SLUG" >/dev/null 2>&1 || info "appctl start returned non-zero; polling decides"
  wait_until "$STOP_TIMEOUT" container_up || die "the container did not come back within ${STOP_TIMEOUT}s; still suppressed"
  wait_until 60 probe_ready || die "container is up but GET / on $BIND_HOST is not ready; still suppressed"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "ROLLED BACK to UCC; $UNIT masked (unmasked by the next --swap). elapsed=$((SECONDS - T0))s"
}

# Every mode that talks to the live bind resolves + validates it first.
case "$MODE" in finish|rollback) check_bind_host ;; esac

case "$MODE" in
  precheck) do_precheck ;;
  install)  do_install ;;
  prove)    do_prove ;;
  swap)     do_swap ;;
  finish)   do_finish ;;
  rollback) do_rollback ;;
esac
