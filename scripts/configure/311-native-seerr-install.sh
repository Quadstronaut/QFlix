#!/usr/bin/env bash
# 311-native-seerr-install.sh -- QFLX-36 (UCC divorce A12, convert seerr).
#
# Moves Seerr off the Ultra.cc container manager (UCC) onto a user unit the repo
# owns: OUR build of the exact commit the container runs (upstream publishes no
# linux-x64 build; .github/workflows/seerr-artifact.yml builds it inside
# debian:bullseye and publishes release seerr-v<ver> of this repo), sha256-pinned,
# run by a pinned portable Node 22 as qflix-seerr.service. Data stays in place:
# CONFIG_DIRECTORY=~/.apps/seerr (settings.json + db/db.sqlite3 + logs), the
# directory the container mounted at /app/config.
# Spec: docs/superpowers/specs/2026-10-09-ucc-divorce-design.md 5.1-5.9, row 12
# of 6, I-13. Shape: 302-native-bazarr-install.sh (pilot 300-...-unpackerr).
#
# THE UCC SEERR APP IS NEVER UNINSTALLED (I-8): it owns the seerr-<slot> vhost.
# It stays installed and stopped forever; the manifest keeps it in the
# generated app-upgrade-all skip list (ucc_dormant: true).
#
# RUNS ON THE BOX. 240-maintenance-install.sh deploys it to ~/scripts/configure/
# with ~/scripts/lib/native.sh, ~/scripts/maint/native_sanitize.py and
# ~/scripts/data/seerr-listen.cjs. Started from the workstation it re-executes
# the deployed copy over ssh.
#
# INERT BY DEFAULT (I-3). Every mode prints its plan (DRY-RUN) and touches
# nothing unless --execute is also given. Modes, in swap order:
#
#   --install   5.9 step 1. Fetch + sha256-verify the Node runtime into
#               ~/.apps/seerr/bin/node-v<node> (+ `bin/node` link) and the Seerr
#               artifact; refuse unless package.json + committag.json match the
#               pin and the version equals `appctl version seerr` (I-10); lay out
#               bin/<ver> + `current`; copy the listen preload to native/; write
#               the env file (the container's NODE_ENV/TZ/COMMIT_TAG + PORT +
#               CONFIG_DIRECTORY + thread caps) and STAGE the unit in native/.
#               NOT copied into the unit dir, NOT enabled (I-6).
#   --prove     5.9 step 2. VACUUM INTO copy of db.sqlite3 + settings.json under
#               ~/.apps/.prove/seerr, native_sanitize (arr servers removed,
#               notification agents off, Plex library sync off, per-user
#               watchlist auto-requests off) with ZERO counts asserted, booted on
#               a free 127.0.0.1 port: /api/v1/status must report the pin and
#               /api/v1/auth/me must authenticate (login). NEVER a request from
#               the copy. Task delta gated at 70% of the ceiling (G-2). The copy
#               is destroyed. Writes swap/seerr/proof.json.
#   --swap      5.9 steps 3-6 + 8 + I-13: needs the proof and the DEPLOYED
#               pending-swap manifest flip. Unmask, capture the listen set (THREE
#               addresses, F-17, reproduced through SEERR_LISTEN by the preload;
#               a wildcard is refused), path-audit settings.json, then PAUSE THE
#               ENTITLEMENT GATE: verify + record its armed state, park the
#               `--execute` execute.conf drop-in in swap state, daemon-reload,
#               run the gate once and REQUIRE `execute=False` in its log (else
#               reinstall + abort). Suppress the app + its canaries, wait for a
#               gap of QFLIX_GATE_GAP_S before the next gate timer run, stop the
#               container through appctl, POLL until no container process is
#               left, the port is free and nothing holds db.sqlite3 / -wal,
#               snapshot, enable --now the unit, verify parity (unit active,
#               listen set equal, status == pin, login, the panel vhost answers
#               from native), request smoke (canary movie + anime scripts),
#               record swap state. Gate stays PAUSED, suppression stays ON.
#   --finish    5.9 step 9 after the follow-up PR dropped `swap_state` and 240
#               deployed it: verify, REINSTALL the gate drop-in and verify the
#               armed state equals the recorded one, then lift the app +
#               canaries together.
#   --rollback  Rollback 0-5. 0: re-suppress, park + MASK the unit. 1: stop it,
#               wait for exit. 2: the DEPLOYED manifest must dispatch seerr as
#               UCC again (revert PR + 240), else exit 10 (re-run after the
#               revert). 3: start the container through appctl. Gate drop-in
#               reinstalled, then unsuppress. 5: no snapshot restore (versions
#               equal, no migration). 4 (unmask) happens at the next --swap.
#   --post-upgrade VER   the tarball_swap post step (manifest upgrade.post_steps).
#               Runs INSIDE the Monday upgrade sweep (skips the window gate).
#               Checks bin/VER is a Seerr build of VER for this Node major, flips
#               `current`, rewrites COMMIT_TAG. lifecycle restarts the unit.
#
# Exit: 0 ok | 1 refused/failed | 10 rollback paused for the manifest revert |
# 64 usage.
#
# Overrides (tests; resolved at call time): QFLIX_APPS_DIR QFLIX_UNIT_DIR
# QFLIX_ENV_DIR QFLIX_SWAP_DIR QFLIX_SECRETS_DIR MANITOBA_STATE_DIR QFLIX_MANIFEST
# QFLIX_PROC QFLIX_PYTHON QFLIX_APPCTL QFLIX_SYSTEMCTL QFLIX_SS QFLIX_PS
# QFLIX_FUSER QFLIX_CURL QFLIX_HOSTPOLICY QFLIX_HOST_ID_FILE QFLIX_CANARIES_DIR
# QFLIX_GATE_LOG_DIR QFLIX_SEERR_SHA256 QFLIX_NODE_SHA256 QFLIX_SEERR_PORT
# QFLIX_SEERR_VHOST_URL QFLIX_POLL_S QFLIX_SETTLE_S QFLIX_STOP_TIMEOUT_S
# QFLIX_PROOF_TIMEOUT_S QFLIX_STATUS_TIMEOUT_S QFLIX_GATE_GAP_S
# QFLIX_GATE_RUN_TIMEOUT_S QFLIX_KEEP_PROOF.
set -uo pipefail

SLUG=seerr
VERSION="3.5.0"              # == versions.env SEERR_VERSION == `appctl version seerr` (test-pinned)
COMMIT="e2f24cb46079746936516c723b09820360f95113"   # == versions.env SEERR_COMMIT == container COMMIT_TAG
ARTIFACT_URL="https://github.com/Quadstronaut/QFlix/releases/download/seerr-v${VERSION}/seerr-${VERSION}-linux-x64.tar.gz"
# sha256 of the asset published by seerr-artifact run 38044628810 (release
# seerr-v3.5.0, never replaced; later runs verify the asset against this pin).
ARTIFACT_SHA256="44eb1e87990a55abe7b4a055fd6079bb468dc5a5fa445afbfb15fbdd1e1129b0"
NODE_VERSION="22.23.2"       # == versions.env SEERR_NODE_VERSION == the container's NODE_VERSION
NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz"
NODE_SHA256="b294a556e639d64338823920e5866c21c02741742d2e1529ee1a225c1ec9252a"
UNIT="qflix-${SLUG}.service"
FAMILY=node
# Node lives OUTSIDE bin/current (native_render_unit takes %h/... verbatim) and
# render_unit puts --disable-wasm-trap-handler right after it (CLI, never
# NODE_OPTIONS). The preload reproduces the recorded listen set.
EXE="%h/.apps/${SLUG}/bin/node/bin/node"
EXEC_ARGS="--require %h/.apps/${SLUG}/native/seerr-listen.cjs %h/.apps/${SLUG}/bin/current/dist/index.js"
# Next.js resolves its .next build from the CWD (upstream `next({ dev })`).
WORKDIR="%h/.apps/${SLUG}/bin/current"
# The container's own env (/proc/<pid>/environ, box 2026-10-10): NODE_ENV,
# TZ, COMMIT_TAG. NODE_VERSION/YARN_VERSION/HOME=/ are image plumbing.
TZ_ENV="TZ=Europe/Amsterdam"
# Muted with the app (plan A-table row A12 + the thread ceiling every A-ticket
# names). Keys follow cli.py: canary-<name>.
SUPPRESS=("$SLUG" "canary-movie" "canary-anime" "canary-seerr-arr-parity"
          "canary-entitlement-service" "canary-thread-ceiling")
PATTERN="dist/index.js"
GATE_UNIT="manitoba-maint-entitlement.service"
GATE_TIMER="manitoba-maint-entitlement.timer"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # .../scripts
ARGS=("$@")

info() { echo "[311-seerr] $*"; }
die()  { echo "[311-seerr] ERROR: $*" >&2; exit 1; }
usage() {
  echo "usage: $0 [--install|--prove|--swap|--finish|--rollback|--post-upgrade VER] [--execute]" >&2
  exit 64
}

MODE=install
EXECUTE=0
UPGRADE_VER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --install|--prove|--swap|--finish|--rollback) MODE="${1#--}" ;;
    --post-upgrade) MODE=post-upgrade; shift; UPGRADE_VER="${1:-}"; [ -n "$UPGRADE_VER" ] || usage ;;
    --execute) EXECUTE=1 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
  shift
done

APPS="${QFLIX_APPS_DIR:-$HOME/.apps}"
APPDIR="$APPS/$SLUG"
UNIT_DIR="${QFLIX_UNIT_DIR:-$HOME/.config/systemd/user}"
ENV_DIR="${QFLIX_ENV_DIR:-$HOME/.config/qflix}"
ENV_FILE="$ENV_DIR/$SLUG.env"
SWAPDIR="${QFLIX_SWAP_DIR:-$HOME/.opt/maint/swap}/$SLUG"
SECRETS="${QFLIX_SECRETS_DIR:-$HOME/secrets}"
STATE_DIR="${MANITOBA_STATE_DIR:-$HOME/.opt/maint}"
PROVE="$APPS/.prove/$SLUG"
MANIFEST="${QFLIX_MANIFEST:-$HOME/.opt/maint/apps.yaml}"
MAINT_LIB="$HERE/maint/lib"
SANITIZE="$HERE/maint/native_sanitize.py"
PRELOAD_SRC="$HERE/data/seerr-listen.cjs"
CANARIES="${QFLIX_CANARIES_DIR:-$HERE/canaries}"
PY="${QFLIX_PYTHON:-python3}"
APPCTL="${QFLIX_APPCTL:-$HOME/bin/appctl}"
SYSTEMCTL="${QFLIX_SYSTEMCTL:-systemctl}"
SS="${QFLIX_SS:-ss}"
PS="${QFLIX_PS:-ps}"
FUSER="${QFLIX_FUSER:-fuser}"
CURL="${QFLIX_CURL:-curl}"
PROC="${QFLIX_PROC:-/proc}"
POLL="${QFLIX_POLL_S:-2}"
SETTLE="${QFLIX_SETTLE_S:-10}"
STOP_TIMEOUT="${QFLIX_STOP_TIMEOUT_S:-120}"
PROOF_TIMEOUT="${QFLIX_PROOF_TIMEOUT_S:-300}"
STATUS_TIMEOUT="${QFLIX_STATUS_TIMEOUT_S:-180}"
GATE_GAP="${QFLIX_GATE_GAP_S:-600}"
GATE_RUN_TIMEOUT="${QFLIX_GATE_RUN_TIMEOUT_S:-600}"
WANT_SHA="${QFLIX_SEERR_SHA256:-$ARTIFACT_SHA256}"
WANT_NODE_SHA="${QFLIX_NODE_SHA256:-$NODE_SHA256}"
# The container's published port (manifest health.port_secret seerr.port).
PROBE_PORT="${QFLIX_SEERR_PORT:-42011}"
SETTINGS="$APPDIR/settings.json"
DB="$APPDIR/db/db.sqlite3"
NODE_BIN="$APPDIR/bin/node/bin/node"
PRELOAD="$APPDIR/native/seerr-listen.cjs"
# Entitlement gate (I-13): pause = park THIS file, never touch members.yaml.
GATE_DROPIN="$UNIT_DIR/$GATE_UNIT.d/execute.conf"
GATE_PARKED="$SWAPDIR/gate/execute.conf"
GATE_JSON="$SWAPDIR/gate.json"
GATE_LOG_DIR="${QFLIX_GATE_LOG_DIR:-$STATE_DIR/entitlement}"
MEMBERS="$SECRETS/members.yaml"
SCOPE=""                     # the container's cgroup, captured at --swap

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
    install)  info "would fetch Node $NODE_VERSION ($NODE_URL, sha256 $WANT_NODE_SHA) into $APPDIR/bin/node-v$NODE_VERSION and the Seerr $VERSION artifact ($ARTIFACT_URL, sha256 ${WANT_SHA:-UNPINNED}), check version parity, lay out $APPDIR/bin/$VERSION, write $ENV_FILE, stage $APPDIR/native/$UNIT (not enabled)" ;;
    prove)    info "would VACUUM INTO a copy of $DB + settings.json under $PROVE, sanitize it (zero counts), boot it on a free 127.0.0.1 port, check status == $VERSION + login, gate the task delta at 70% of the ceiling, then delete the copy" ;;
    swap)     info "would capture the listen set (port $PROBE_PORT), path-audit settings.json, PAUSE the entitlement gate (park $GATE_DROPIN, confirm execute=False), suppress ${SUPPRESS[*]}, stop the container, wait for exit + free port + idle db, snapshot, enable --now $UNIT with SEERR_LISTEN, verify (status, login, vhost), request smoke, record swap state" ;;
    finish)   info "would verify the native unit, reinstall $GATE_DROPIN and verify the gate's armed state, then lift suppression for ${SUPPRESS[*]}" ;;
    rollback) info "would suppress ${SUPPRESS[*]}, park + mask $UNIT, stop it, wait for the manifest revert, start the container via appctl, reinstall the gate drop-in, unsuppress" ;;
    post-upgrade) info "would check $APPDIR/bin/$UPGRADE_VER is a Seerr $UPGRADE_VER build, flip bin/current, rewrite COMMIT_TAG in $ENV_FILE" ;;
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
  sshm "~/scripts/configure/311-native-seerr-install.sh $(printf '%q ' "${ARGS[@]}")"
  exit $?
fi

# shellcheck source=/dev/null
source "$HERE/lib/native.sh" || die "cannot source $HERE/lib/native.sh (run 240 first)"

if [ "$MODE" != post-upgrade ]; then
  PROFILE="$(hostpolicy preflight)" || die "host profile unresolved; refusing (I-12)"
  PROFILE="${PROFILE%$'\r'}"
  if hostpolicy in-window; then
    die "inside the Monday maintenance window; no box operations"
  fi
  case "$MODE" in
    swap|finish|rollback)
      [ "$PROFILE" = ultra ] || die "mode $MODE swaps against a UCC container; host profile is '$PROFILE'" ;;
  esac
fi

T0=$SECONDS

CLEANUP_PATHS=()
PROOF_PID=""
cleanup() {
  [ -n "$PROOF_PID" ] && kill "$PROOF_PID" 2>/dev/null
  local p
  for p in "${CLEANUP_PATHS[@]}"; do rm -rf "$p"; done
}
trap cleanup EXIT

# --- python helpers ----------------------------------------------------------------
# http_json URL [SETTINGS_JSON]: GET, print the body, exit 0 only on HTTP 200.
# With SETTINGS_JSON the X-Api-Key header is that file's main.apiKey.
http_json() {
  "$PY" - "$@" <<'PY'
import json, sys, urllib.request
url = sys.argv[1]
hdr = {}
if len(sys.argv) > 2 and sys.argv[2]:
    try:
        key = json.load(open(sys.argv[2], encoding="utf-8"))["main"]["apiKey"]
    except Exception:
        sys.exit(1)
    if not key:
        sys.exit(1)
    hdr["X-Api-Key"] = key
try:
    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=10) as r:
        body = r.read().decode("utf-8", "replace")
        if r.status != 200:
            sys.exit(1)
except Exception:
    sys.exit(1)
print(body)
PY
}

# jget FILE KEY.PATH: one value out of a JSON file (empty if absent).
# jstr TEXT KEY.PATH: the same out of a JSON string (argv: stdin carries the
# heredoc script, so it cannot carry the data too).
jget() { _jpath file "$@"; }
jstr() { _jpath text "$@"; }
_jpath() {
  "$PY" - "$@" <<'PY'
import json, sys
kind, src, path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.loads(src) if kind == "text" else json.load(open(src, encoding="utf-8"))
except Exception:
    sys.exit(1)
for k in path.split("."):
    data = data.get(k) if isinstance(data, dict) else None
print("" if data is None else data)
PY
}

# status_probe PORT: /api/v1/status on loopback; prints the version, 0 only on 200.
status_probe() {
  local body
  body="$(http_json "http://127.0.0.1:$1/api/v1/status")" || return 1
  jstr "$body" version
}

# auth_probe SETTINGS PORT: "login" = /api/v1/auth/me authenticated with the
# instance's own API key, read from settings.json INSIDE python (never on a
# command line, never a password).
auth_probe() {
  local body id
  body="$(http_json "http://127.0.0.1:$2/api/v1/auth/me" "$1")" || return 1
  id="$(jstr "$body" id)" && [ -n "$id" ]
}

# vhost_probe: the panel-owned seerr-<slot> vhost (I-8) must answer from native.
vhost_url() {
  local u="${QFLIX_SEERR_VHOST_URL:-}"
  [ -n "$u" ] || u="$(jget "$SETTINGS" main.applicationUrl)" || return 1
  u="${u%/}"
  case "$u" in https://*|http://*) printf '%s' "$u" ;; *) return 1 ;; esac
}
vhost_probe() {
  local u body
  u="$(vhost_url)" || { echo "no applicationUrl in settings.json; cannot test the vhost" >&2; return 1; }
  body="$("$CURL" -fsS -m 15 "$u/api/v1/status" 2>/dev/null)" || return 1
  jstr "$body" version
}

# settings.json path audit: a container path (/app, /config) anywhere except the
# arr server blocks (their activeDirectory is the ARR's path, passed through).
settings_audit() {
  "$PY" - "$1" <<'PY'
import json, re, sys
s = json.load(open(sys.argv[1], encoding="utf-8"))
pat = re.compile(r"^/(app|config)(/|$)")
bad = []
def walk(o, path):
    if isinstance(o, dict):
        for k, v in o.items():
            walk(v, path + [str(k)])
    elif isinstance(o, list):
        for i, v in enumerate(o):
            walk(v, path + [str(i)])
    elif isinstance(o, str) and pat.match(o):
        bad.append(".".join(path))
for k, v in s.items():
    if k not in ("radarr", "sonarr"):
        walk(v, [k])
for b in bad:
    print(b)
sys.exit(1 if bad else 0)
PY
}

# Safe extraction of a sha-pinned tarball (no absolute / .. members, no links
# out of the tree).
untar() {
  "$PY" - "$1" "$2" <<'PY'
import os, sys, tarfile
src, dst = sys.argv[1], sys.argv[2]
def escapes(p):
    p = os.path.normpath(p).replace("\\", "/")
    return p == ".." or p.startswith("../") or p.startswith("/")
with tarfile.open(src) as t:
    for m in t.getmembers():
        n = m.name
        if n.startswith("/") or ".." in n.split("/"):
            sys.exit("unsafe member " + n)
        if m.issym():      # relative to the link's own directory
            if m.linkname.startswith("/") or escapes(os.path.join(os.path.dirname(n), m.linkname)):
                sys.exit("unsafe symlink " + n + " -> " + m.linkname)
        elif m.islnk():    # relative to the archive root
            if m.linkname.startswith("/") or escapes(m.linkname):
                sys.exit("unsafe hardlink " + n + " -> " + m.linkname)
    try:
        t.extractall(dst, filter="fully_trusted")
    except TypeError:
        t.extractall(dst)
PY
}

free_port() {
  "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

roster_armed() {
  "$PY" - "$MEMBERS" <<'PY'
import sys
try:
    import yaml
    v = (yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}).get("armed")
except Exception:
    v = None
print({True: "true", False: "false"}.get(v, "unknown"))
PY
}

# --- helpers -------------------------------------------------------------------------
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

# PIDs under OUR uid. kind=container: in a container cgroup and either running
# Seerr's entrypoint or inside the scope captured at --swap (npm start, its
# children). kind=native: in the unit's cgroup.
scan_pids() {
  local kind="$1" uid d pid cg cmd
  uid="$(id -u)"
  for d in "$PROC"/[0-9]*; do
    [ -d "$d" ] || continue
    pid="${d##*/}"
    [ "$(awk '/^Uid:/ {print $2; exit}' "$d/status" 2>/dev/null)" = "$uid" ] || continue
    cg="$(cat "$d/cgroup" 2>/dev/null)" || continue
    cmd="$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)" || continue
    if [ "$kind" = native ]; then
      case "$cg" in *"$UNIT"*) case "$cmd" in *"$PATTERN"*) echo "$pid" ;; esac ;; esac
      continue
    fi
    case "$cg" in *"$UNIT"*) continue ;; esac
    [[ "$cg" =~ docker|libpod|podman|containerd|crio ]] || continue
    if [ -n "$SCOPE" ] && [ "${cg%%$'\n'*}" = "$SCOPE" ]; then echo "$pid"; continue; fi
    case "$cmd" in *"$PATTERN"*) echo "$pid" ;; esac
  done
}

container_scope() {
  local pid
  pid="$(scan_pids container | head -n 1)"
  [ -n "$pid" ] || return 1
  head -n 1 "$PROC/$pid/cgroup"
}

port_free() { ! "$SS" -tlnH "sport = :$PROBE_PORT" 2>/dev/null | grep -q ":$PROBE_PORT\b"; }

db_idle() {
  command -v "$FUSER" >/dev/null 2>&1 || { echo "fuser not found; cannot prove the db idle" >&2; return 1; }
  ! "$FUSER" "$DB" "$DB-wal" >/dev/null 2>&1
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
container_gone() { [ -z "$(scan_pids container)" ] && port_free && db_idle; }
container_up()   { [ -n "$(scan_pids container)" ]; }
native_gone()    { [ -z "$(scan_pids native)" ]; }

user_tasks() { "$PS" -u "$(id -u)" -L --no-headers 2>/dev/null | wc -l | tr -d ' '; }

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

# env body: thread caps + the container's env + PORT + CONFIG_DIRECTORY
# [+ SEERR_LISTEN]. write_env LISTEN COMMIT_TAG
write_env() {
  local listen="${1:-}" tag="$2" extra=()
  extra=("NODE_ENV=production" "$TZ_ENV" "COMMIT_TAG=$tag" "CONFIG_DIRECTORY=$APPDIR" "PORT=$PROBE_PORT")
  [ -n "$listen" ] && extra+=("SEERR_LISTEN=$listen")
  native_render_env "$SLUG" "$FAMILY" "$VERSION" "${extra[@]}" | native_write_secure "$ENV_FILE" 0600
}

# "a:p b:p c:p" from listen-set.before; refuses empty / wildcard sets.
listen_env() {
  local f="$SWAPDIR/listen-set.before" out="" l
  [ -s "$f" ] || { echo "listen set is empty" >&2; return 1; }
  while IFS= read -r l; do
    l="${l%$'\r'}"; [ -n "$l" ] || continue
    case "$l" in
      0.0.0.0:*|\*:*|\[::\]:*|::*) echo "wildcard listener '$l' in the recorded set; refusing (0.0.0.0 is never an option)" >&2; return 1 ;;
    esac
    out="$out${out:+ }$l"
  done < "$f"
  [ -n "$out" ] || return 1
  printf '%s' "$out"
}

# --- entitlement gate (I-13) ----------------------------------------------------------
gate_idle() { ! sysd is-active "$GATE_UNIT" >/dev/null 2>&1; }

# Seconds until the gate timer fires next (exit 1 = unknown).
gate_next_in() {
  local n t
  # Human form on the box ("Sat 2026-10-10 12:23:20 CEST", date -d parses it);
  # an "@epoch" form is accepted too.
  n="$(sysd show "$GATE_TIMER" -p NextElapseUSecRealtime --value 2>/dev/null)" || return 1
  n="${n%$'\r'}"; n="${n#@}"
  if [[ "$n" =~ ^[0-9]+$ ]]; then t="$n"
  else t="$(date -u -d "$n" +%s 2>/dev/null)" || return 1
  fi
  echo $((t - $(date -u +%s)))
}

# The last execute=<bool> the gate logged in a run that started after MARKER.
gate_last_execute() {
  local marker="$1" f
  f="$(find "$GATE_LOG_DIR" -maxdepth 1 -name '*.log' -newer "$marker" 2>/dev/null | sort | tail -n 1)"
  [ -n "$f" ] || return 1
  grep -o 'execute=\(True\|False\)' "$f" | tail -n 1
}

gate_exec_line_has_execute() {
  sysd show "$GATE_UNIT" -p ExecStart --value 2>/dev/null | grep -q -- '--execute'
}

gate_state_line() {
  local d=absent
  [ -f "$GATE_DROPIN" ] && d=present
  echo "drop-in=$d roster_armed=$(roster_armed) exec_has_execute=$(gate_exec_line_has_execute && echo yes || echo no)"
}

# One report-only run of the gate; REQUIRE execute=False in its log line.
gate_confirm_report_only() {
  local marker="$SWAPDIR/gate/.run-marker" last
  mkdir -p "$SWAPDIR/gate"
  wait_until "$GATE_RUN_TIMEOUT" gate_idle || { echo "a gate run is still active" >&2; return 1; }
  : > "$marker"
  sleep 1
  sysd start "$GATE_UNIT" >/dev/null 2>&1 || info "gate run exited non-zero; its log line decides"
  last="$(gate_last_execute "$marker")" || { echo "no gate log line after the run" >&2; return 1; }
  [ "$last" = "execute=False" ] || { echo "gate logged $last" >&2; return 1; }
  info "gate confirmed report-only: next run logged $last"
}

gate_restore() {
  [ -f "$GATE_PARKED" ] || return 0
  mkdir -p "$(dirname "$GATE_DROPIN")" || return 1
  if [ -f "$GATE_DROPIN" ]; then
    cmp -s "$GATE_DROPIN" "$GATE_PARKED" || { echo "a DIFFERENT $GATE_DROPIN exists; not overwriting it" >&2; return 1; }
  else
    native_write_secure "$GATE_DROPIN" 0644 < "$GATE_PARKED" || return 1
  fi
  cmp -s "$GATE_DROPIN" "$GATE_PARKED" || return 1
  sysd daemon-reload || return 1
  gate_exec_line_has_execute || { echo "gate ExecStart lacks --execute after the reinstall" >&2; return 1; }
  rm -f "$GATE_PARKED"
  info "gate drop-in reinstalled ($(gate_state_line))"
}

# Verify + record the armed state BEFORE, park the drop-in, confirm report-only.
gate_pause() {
  local before roster
  mkdir -p "$SWAPDIR/gate" || die "cannot create $SWAPDIR/gate"
  if [ -f "$GATE_PARKED" ]; then
    info "gate already paused (parked drop-in present); re-confirming"
    gate_confirm_report_only || { gate_restore; die "gate is not report-only; drop-in reinstalled, swap aborted"; }
    return 0
  fi
  before="$(gate_state_line)"
  roster="$(roster_armed)"
  info "gate armed state BEFORE: $before"
  if [ -f "$GATE_DROPIN" ]; then
    grep -q -- '--execute' "$GATE_DROPIN" || die "$GATE_DROPIN has no --execute; unknown gate shape, refusing"
    printf '{"dropin": "present", "roster_armed": "%s", "paused_at": "%s"}\n' \
      "$roster" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | native_write_secure "$GATE_JSON" 0644 \
      || die "cannot record the gate state"
    native_write_secure "$GATE_PARKED" 0600 < "$GATE_DROPIN" && cmp -s "$GATE_PARKED" "$GATE_DROPIN" \
      || die "cannot park $GATE_DROPIN"
    rm -f "$GATE_DROPIN"
    [ ! -e "$GATE_DROPIN" ] || die "drop-in still present after removal"
    sysd daemon-reload || { gate_restore; die "daemon-reload failed; drop-in reinstalled"; }
  else
    printf '{"dropin": "absent", "roster_armed": "%s", "paused_at": "%s"}\n' \
      "$roster" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | native_write_secure "$GATE_JSON" 0644 \
      || die "cannot record the gate state"
    info "gate has no execute drop-in (already report-only); nothing to park"
  fi
  gate_confirm_report_only || { gate_restore; die "could not confirm execute=False; drop-in reinstalled, swap aborted"; }
}

# The armed state AFTER must equal the recorded BEFORE (never arm what was not).
gate_verify_restored() {
  local rec roster
  [ -f "$GATE_JSON" ] || { info "no gate state recorded (gate never paused)"; return 0; }
  rec="$(jget "$GATE_JSON" dropin)"
  roster="$(jget "$GATE_JSON" roster_armed)"
  [ ! -f "$GATE_PARKED" ] || { echo "drop-in still parked" >&2; return 1; }
  if [ "$rec" = present ]; then
    [ -f "$GATE_DROPIN" ] && grep -q -- '--execute' "$GATE_DROPIN" || { echo "drop-in missing" >&2; return 1; }
    gate_exec_line_has_execute || { echo "ExecStart lacks --execute" >&2; return 1; }
  else
    [ ! -f "$GATE_DROPIN" ] || { echo "a drop-in appeared that was absent before" >&2; return 1; }
  fi
  [ "$(roster_armed)" = "$roster" ] || { echo "roster armed changed: was $roster" >&2; return 1; }
  info "gate armed state AFTER: $(gate_state_line) (matches the recorded state)"
}

# Wait until the next gate timer run is at least GATE_GAP seconds away, so no
# gate run (which would page "Seerr unavailable") lands in the downtime.
wait_gate_gap() {
  local s
  s="$(gate_next_in)" || { info "next gate run unknown; proceeding (it is report-only)"; return 0; }
  if [ "$s" -lt "$GATE_GAP" ]; then
    info "next gate run in ${s}s (< ${GATE_GAP}s); waiting it out"
    [ "$s" -gt 0 ] && sleep "$s"
    sleep "$POLL"
    wait_until "$GATE_RUN_TIMEOUT" gate_idle || info "gate run still active; proceeding"
  fi
}

# --- modes ----------------------------------------------------------------------------
install_node() {
  local stage="$1" nd="$APPDIR/bin/node-v$NODE_VERSION" have bindir="$APPDIR/bin"
  mkdir -p "$bindir" || die "cannot create $bindir"
  if [ ! -x "$nd/bin/node" ]; then
    native_fetch_verify "$NODE_URL" "$WANT_NODE_SHA" "$stage/node.tgz" || die "Node fetch/sha256 verify failed"
    mkdir -p "$stage/n"
    untar "$stage/node.tgz" "$stage/n" || die "Node extraction failed"
    [ -f "$stage/n/node-v$NODE_VERSION-linux-x64/bin/node" ] || die "Node tarball has no bin/node"
    rm -rf "$nd.part.$$"
    mv "$stage/n/node-v$NODE_VERSION-linux-x64" "$nd.part.$$" && mv "$nd.part.$$" "$nd" || die "cannot place $nd"
  fi
  ln -sfn "node-v$NODE_VERSION" "$bindir/.node.$$" && mv -Tf "$bindir/.node.$$" "$bindir/node" || die "cannot link bin/node"
  have="$("$NODE_BIN" --version 2>/dev/null | tr -d '\r')"
  [ "$have" = "v$NODE_VERSION" ] || die "Node reports '$have', want v$NODE_VERSION"
}

do_install() {
  local stage tag
  [[ "$WANT_SHA" =~ ^[0-9a-f]{64}$ ]] || die "the Seerr artifact sha256 is not pinned; publish it (seerr-artifact workflow) and pin ARTIFACT_SHA256 first"
  mkdir -p "$APPS" || die "cannot create $APPS"
  [ -f "$SETTINGS" ] && [ -f "$DB" ] || die "$SETTINGS / $DB missing (the data is used in place)"
  [ -f "$PRELOAD_SRC" ] || die "$PRELOAD_SRC missing (run 240 first)"
  stage="$(mktemp -d "$APPS/.stage-$SLUG.XXXXXX")" || die "mktemp failed"
  CLEANUP_PATHS+=("$stage")
  install_node "$stage"
  native_fetch_verify "$ARTIFACT_URL" "$WANT_SHA" "$stage/s.tgz" || die "artifact fetch/sha256 verify failed"
  mkdir -p "$stage/x"
  untar "$stage/s.tgz" "$stage/x" || die "artifact extraction failed"
  [ -f "$stage/x/dist/index.js" ] && [ -d "$stage/x/.next" ] && [ -d "$stage/x/node_modules" ] \
    || die "artifact is not a Seerr build (dist/index.js, .next, node_modules)"
  [ "$(jget "$stage/x/package.json" version)" = "$VERSION" ] || die "artifact package.json is not $VERSION"
  tag="$(jget "$stage/x/committag.json" commitTag)"
  [ "$tag" = "$COMMIT" ] || die "artifact commitTag '$tag' != pinned $COMMIT"
  native_install_versioned "$SLUG" "$VERSION" "$stage/x" || die "install refused (see above)"
  native_write_secure "$PRELOAD" 0644 < "$PRELOAD_SRC" || die "cannot stage the listen preload"
  write_env "" "$COMMIT" || die "env file write failed"
  native_render_unit "$SLUG" "$FAMILY" "$EXE" "$EXEC_ARGS" "$WORKDIR" \
    | native_write_secure "$APPDIR/native/$UNIT" 0644 || die "unit staging failed"
  info "installed $VERSION (Node v$NODE_VERSION); unit staged at $APPDIR/native/$UNIT (not enabled). Next: --prove --execute"
}

installed() {
  [ -x "$NODE_BIN" ] && [ -f "$APPDIR/bin/current/dist/index.js" ] && [ -f "$ENV_FILE" ] \
    && [ -f "$PRELOAD" ] && [ -f "$APPDIR/native/$UNIT" ]
}

do_prove() {
  local before after delta ceiling pport out ver i
  installed || die "not installed; run --install --execute first"
  grep -qx "CONFIG_DIRECTORY=$APPDIR" "$ENV_FILE" || die "$ENV_FILE lacks CONFIG_DIRECTORY=$APPDIR"
  ceiling="$(hostpolicy task-ceiling)" || die "task ceiling unknown; refusing (G-2)"
  ceiling="${ceiling%$'\r'}"
  rm -rf "$PROVE"
  mkdir -p "$PROVE/db" || die "cannot create $PROVE"
  [ "${QFLIX_KEEP_PROOF:-0}" = 1 ] || CLEANUP_PATHS+=("$PROVE")
  "$PY" - "$DB" "$PROVE/db/db.sqlite3" <<'PY' || die "VACUUM INTO failed"
import sqlite3, sys
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1].replace("\\", "/"), uri=True)
con.execute("VACUUM INTO ?", (sys.argv[2],))
con.close()
PY
  cp "$SETTINGS" "$PROVE/settings.json" || die "cannot copy settings.json"
  out="$("$PY" "$SANITIZE" "$SLUG" "$PROVE")" || die "sanitize refused the proof copy: $out"
  "$PY" - "$out" <<'PY' || die "sanitize counts are not zero: $out"
import json, sys
c = json.loads(sys.argv[1])["counts"]
sys.exit(1 if any(c.values()) or "watchlist_sync" not in c else 0)
PY
  pport="$(free_port)" || die "no free loopback port"
  before="$(user_tasks)"
  [[ "$before" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  (
    set -a; . "$ENV_FILE"; set +a
    CONFIG_DIRECTORY="$PROVE"; PORT="$pport"; SEERR_LISTEN="127.0.0.1:$pport"
    export CONFIG_DIRECTORY PORT SEERR_LISTEN
    cd "$APPDIR/bin/current" || exit 1
    exec "$NODE_BIN" --disable-wasm-trap-handler --require "$PRELOAD" "$APPDIR/bin/current/dist/index.js" \
      >"$PROVE/stdout.log" 2>&1
  ) &
  PROOF_PID=$!
  sleep "$SETTLE"
  ver=""
  i=$SECONDS
  until ver="$(status_probe "$pport")" && [ -n "$ver" ]; do
    ver=""
    # SECONDS, not iterations x POLL: POLL may be fractional.
    if [ $((SECONDS - i)) -ge "$PROOF_TIMEOUT" ]; then
      tail -5 "$PROVE/stdout.log" 2>/dev/null >&2
      die "proof: /api/v1/status never answered 200 within ${PROOF_TIMEOUT}s"
    fi
    sleep "$POLL"
  done
  [ "$ver" = "$VERSION" ] || die "proof: status reports '$ver' != pin '$VERSION'"
  auth_probe "$PROVE/settings.json" "$pport" || die "proof: login (/api/v1/auth/me with the API key) failed"
  after="$(user_tasks)"
  [[ "$after" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  delta=$((after - before))
  kill "$PROOF_PID" 2>/dev/null; wait "$PROOF_PID" 2>/dev/null; PROOF_PID=""
  [ "$delta" -gt 0 ] || die "proof: measured task delta $delta; cannot gate the swap; refusing"
  if [ $(( (before + delta) * 100 )) -ge $(( 70 * ceiling )) ]; then
    die "thread gate: $before + $delta tasks reaches 70% of the ceiling $ceiling; refusing the swap"
  fi
  mkdir -p "$SWAPDIR"
  printf '{"ok": true, "version": "%s", "login": true, "before": %s, "delta": %s, "ceiling": %s, "at": "%s"}\n' \
    "$VERSION" "$before" "$delta" "$ceiling" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    | native_write_secure "$SWAPDIR/proof.json" 0644 || die "cannot record the proof"
  info "PROOF OK: status 200, version $ver, login ok, sanitized copy inert; before=$before delta=$delta ceiling=$ceiling. Next: --swap --execute"
}

verify_native() {
  local n ver vv start
  sleep "$SETTLE"
  sysd is-active "$UNIT" >/dev/null 2>&1 || { echo "unit not active" >&2; return 1; }
  [ -z "$(scan_pids container)" ] || { echo "a container process is running" >&2; return 1; }
  n="$(scan_pids native | wc -l | tr -d ' ')"
  [ "$n" -ge 1 ] || { echo "no native process" >&2; return 1; }
  native_listen_compare "$SLUG" >/dev/null || { echo "listen set differs from listen-set.before" >&2; return 1; }
  start=$SECONDS
  while :; do
    ver="$(status_probe "$PROBE_PORT")" && [ -n "$ver" ] && break
    ver=""
    [ $((SECONDS - start)) -ge "$STATUS_TIMEOUT" ] && { echo "/api/v1/status never answered 200" >&2; return 1; }
    sleep "$POLL"
  done
  [ "$ver" = "$VERSION" ] || { echo "status reports $ver, want $VERSION" >&2; return 1; }
  auth_probe "$SETTINGS" "$PROBE_PORT" || { echo "login (/api/v1/auth/me) failed" >&2; return 1; }
  vv="$(vhost_probe)" || { echo "the seerr vhost does not answer" >&2; return 1; }
  [ "$vv" = "$VERSION" ] || { echo "the seerr vhost reports '$vv', want $VERSION" >&2; return 1; }
}

# Request smoke (plan A12): the movie + anime canaries drive a REAL request
# through Seerr into Radarr/Sonarr and delete it again. Run directly: their
# pushes are suppressed, so `canary push` would skip them.
request_smoke() {
  local c
  for c in movie anime; do
    [ -f "$CANARIES/$c.sh" ] || { echo "canary $c.sh not found" >&2; return 1; }
    bash "$CANARIES/$c.sh" >"$SWAPDIR/smoke-$c.log" 2>&1 || { echo "canary $c failed (see $SWAPDIR/smoke-$c.log)" >&2; return 1; }
  done
}

abort_restore() {   # abort_restore MSG: container back, gate back, unsuppress
  "$APPCTL" start "$SLUG" >/dev/null 2>&1 || true
  gate_restore || echo "[311-seerr] WARNING: gate drop-in NOT reinstalled; run --rollback --execute" >&2
  suppression remove "${SUPPRESS[@]}" >/dev/null || true
  die "$1"
}

do_swap() {
  local st cls ss_state ver snap now soak listen sp hits
  [ -f "$SWAPDIR/proof.json" ] || die "no proof recorded; run --prove --execute first"
  installed || die "not installed; run --install --execute first"
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state _ <<<"$st"
  [ "$cls" = systemd ] && [ "$ss_state" = pending-swap ] \
    || die "the deployed manifest is not the pending-swap flip (class=$cls swap_state=$ss_state); merge + deploy it via 240 first"

  if sysd is-active "$UNIT" >/dev/null 2>&1 && [ -z "$(scan_pids container)" ]; then
    info "already swapped; verifying only"
    verify_native || die "native seerr fails parity; run --rollback --execute"
    info "verified"
    return 0
  fi

  if is_masked; then
    sysd unmask "$UNIT" || die "unmask $UNIT failed"
    rm -f "$ENV_DIR/parked-units/$UNIT"
  fi

  # Step 3: capture + audits (read-only).
  if [ -s "$SECRETS/seerr.port" ]; then
    sp="$(tr -d '[:space:]' < "$SECRETS/seerr.port")"
    [ "$sp" = "$PROBE_PORT" ] || die "secrets/seerr.port ($sp) != probe port ($PROBE_PORT)"
  fi
  ver="$(native_ucc_version "$SLUG")"
  [ "${ver#v}" = "$VERSION" ] || die "version parity: container=$ver native=$VERSION"
  SCOPE="$(container_scope)" || die "no running seerr container found (nothing to swap)"
  native_listen_capture "$SLUG" "$PROBE_PORT" >/dev/null || die "listen-set capture failed"
  listen="$(listen_env)" || die "listen set on :$PROBE_PORT unusable (must be non-empty, no wildcard)"
  swapstate set "$SLUG" "ucc_version=$VERSION" >/dev/null || die "cannot record ucc_version"
  hits="$(settings_audit "$SETTINGS")" || die "container path(s) in settings.json: $(echo "$hits" | head -3 | tr '\n' ' ')"
  vhost_url >/dev/null || die "settings.json has no applicationUrl; the vhost cannot be verified"

  # I-13: pause the entitlement gate with its REAL switch (never the rail).
  gate_pause

  # Step 4: suppress the app and its canaries together.
  suppression add "${SUPPRESS[@]}" --reason "QFLX-36 swap to native" >/dev/null \
    || { gate_restore; die "cannot suppress ${SUPPRESS[*]}; gate reinstalled, refusing to swap unsuppressed"; }

  wait_gate_gap

  # Step 6: stop the container; the STATE decides (no process, port free, db +
  # wal idle), not the exit code (asynchronous Docker behaviour).
  "$APPCTL" stop "$SLUG" >/dev/null 2>&1 || info "appctl stop returned non-zero; polling decides"
  if ! wait_until "$STOP_TIMEOUT" container_gone; then
    abort_restore "the container did not exit within ${STOP_TIMEOUT}s (pids: $(scan_pids container | tr '\n' ' ')); swap aborted, container start requested, gate reinstalled, suppression lifted"
  fi

  # Step 5, after the stop so the sqlite files are quiescent (WAL included).
  mkdir -p "$SWAPDIR"
  snap="$SWAPDIR/snapshot-$(date -u +%Y%m%dT%H%M%SZ).tgz"
  tar --force-local -czf "$snap" -C "$APPS" --exclude="$SLUG/bin" --exclude="$SLUG/native" \
      --exclude="$SLUG/logs" --exclude="$SLUG/cache" "$SLUG" \
    || abort_restore "snapshot failed; container start requested, gate reinstalled, suppression lifted"

  write_env "$listen" "$COMMIT" || die "env file write failed; run --rollback --execute"
  mkdir -p "$UNIT_DIR"
  native_write_secure "$UNIT_DIR/$UNIT" 0644 < "$APPDIR/native/$UNIT" || die "unit install failed; run --rollback --execute"
  sysd daemon-reload || die "daemon-reload failed; run --rollback --execute"
  sysd enable --now "$UNIT" || die "enable --now $UNIT failed; run --rollback --execute"

  if ! verify_native; then
    die "native seerr fails parity after the swap; gate PAUSED + suppression kept ON; run --rollback --execute"
  fi
  request_smoke || die "request smoke failed; gate PAUSED + suppression kept ON; run --rollback --execute"
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  soak="$(date -u -d '+14 days' +%Y-%m-%dT%H:%M:%SZ)" || die "cannot compute soak_until"
  swapstate set "$SLUG" "swap_date=$now" "soak_until=$soak" "rollback_window=open" >/dev/null \
    || die "cannot record swap state"
  info "SWAPPED to native $VERSION; vhost + login + request smoke ok; soak until $soak. elapsed=$((SECONDS - T0))s"
  info "Gate stays PAUSED (report-only) and suppression ON until --finish."
  info "Next: PR dropping swap_state: pending-swap, deploy via 240, then --finish --execute"
}

do_finish() {
  local st cls ss_state dormant
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state dormant <<<"$st"
  [ "$cls" = systemd ] && [ -z "$ss_state" ] && [ "$dormant" = 1 ] \
    || die "deployed manifest still says class=$cls swap_state=${ss_state:-none} dormant=$dormant; deploy the follow-up (no pending-swap) first"
  verify_native || die "native seerr fails parity; run --rollback --execute"
  gate_restore || die "cannot reinstall the gate drop-in; suppression kept ON"
  gate_verify_restored || die "gate armed state differs from the recorded one; suppression kept ON"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "FINISHED: gate re-armed as recorded; ${SUPPRESS[*]} unsuppressed; 14-day soak running"
}

do_rollback() {
  local isn
  suppression add "${SUPPRESS[@]}" --reason "QFLX-36 rollback to UCC" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to roll back unsuppressed"
  mask_unit
  sysd stop "$UNIT" >/dev/null 2>&1 || true
  wait_until "$STOP_TIMEOUT" native_gone || die "native seerr did not stop within ${STOP_TIMEOUT}s"
  isn="$("$APPCTL" is-native "$SLUG" 2>/dev/null)"
  if [ "${isn%$'\r'}" != ucc ]; then
    echo "[311-seerr] PAUSED: native stopped + masked; revert the deployed manifest (PR + 240) so appctl dispatches $SLUG as UCC, then re-run --rollback --execute" >&2
    exit 10
  fi
  "$APPCTL" start "$SLUG" >/dev/null 2>&1 || info "appctl start returned non-zero; polling decides"
  wait_until "$STOP_TIMEOUT" container_up || die "the container did not come back within ${STOP_TIMEOUT}s; still suppressed, gate still paused"
  gate_restore || die "cannot reinstall the gate drop-in; still suppressed"
  gate_verify_restored || die "gate armed state differs from the recorded one; still suppressed"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "ROLLED BACK to UCC; gate re-armed as recorded; $UNIT masked (unmasked by the next --swap). elapsed=$((SECONDS - T0))s"
}

do_post_upgrade() {
  local v="$UPGRADE_VER" d tag listen want_major have_major
  v="${v#v}"
  _native_valid_ver "$v" || die "bad version: $v"
  d="$APPDIR/bin/$v"
  [ -f "$d/dist/index.js" ] && [ -d "$d/.next" ] && [ -f "$d/package.json" ] || die "$d is not an extracted Seerr build"
  [ "$(jget "$d/package.json" version)" = "$v" ] || die "$d/package.json is not $v"
  want_major="$(jget "$d/package.json" engines.node | sed -n 's/^[^0-9]*\([0-9][0-9]*\).*/\1/p')"
  have_major="$("$NODE_BIN" --version 2>/dev/null | sed -n 's/^v\([0-9][0-9]*\).*/\1/p')"
  [ -n "$want_major" ] && [ "$want_major" = "$have_major" ] \
    || die "Seerr $v wants Node ${want_major:-?}, installed Node is ${have_major:-?}; a Node upgrade is a manual step"
  tag="$(jget "$d/committag.json" commitTag)" && [ -n "$tag" ] || die "$d has no committag.json"
  native_link_current "$SLUG" "$v" || die "cannot flip bin/current"
  [ -f "$ENV_FILE" ] || die "$ENV_FILE missing"
  listen="$(sed -n 's/^SEERR_LISTEN=//p' "$ENV_FILE" | tail -n 1)"
  write_env "$listen" "$tag" || die "env file rewrite failed"
  grep -qx "COMMIT_TAG=$tag" "$ENV_FILE" || die "COMMIT_TAG not written"
  info "post-upgrade $v: current flipped, COMMIT_TAG=$tag"
}

case "$MODE" in
  install)      do_install ;;
  prove)        do_prove ;;
  swap)         do_swap ;;
  finish)       do_finish ;;
  rollback)     do_rollback ;;
  post-upgrade) do_post_upgrade ;;
esac
