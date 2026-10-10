#!/usr/bin/env bash
# 308-native-sabnzbd-install.sh -- QFLX-33 (UCC divorce A9, convert sabnzbd).
#
# Moves SABnzbd off the Ultra.cc container manager (UCC) onto a user unit the
# repo owns: the upstream SOURCE release at EXACTLY the container's version
# (sha256-pinned) in a python3 venv (requirements.txt pins sabctools & co), plus
# pinned helper binaries par2 (par2cmdline-turbo, static), unrar (rarlab) and
# 7zz (7-Zip static build) laid out in the SAME bin/<ver>/ dir, so the unit's
# Environment=PATH (bin/current first) is where SAB finds them.
# Spec: docs/superpowers/specs/2026-10-09-ucc-divorce-design.md 5.1-5.9, row 9.
# Same shape as the pilot (300-native-unpackerr-install.sh) and 301.
#
# RUNS ON THE BOX. 240-maintenance-install.sh deploys it to ~/scripts/configure/
# with ~/scripts/lib/{native.sh,qflix-tcpfwd.py} and ~/scripts/maint/
# native_sanitize.py beside it. Started from the workstation it re-executes the
# deployed copy over ssh (scripts/lib/ssh.sh).
#
# DATA IN PLACE: ~/.apps/sabnzbd/sabnzbd.ini + admin/ + logs/ stay where the
# container mounted them (/config). The container ran `--config-file /config
# --server ::` with `port = 8080` in the ini (its INTERNAL port; docker-proxy
# published it on <sabnzbd.port>). Native runs `--config-file <ini> --server
# <net.app_host>:<sabnzbd.port>`, and SAB WRITES that host/port back into the
# ini. Rollback therefore restores the recorded host/port keys before the
# container may start again (else it would listen on the wrong internal port).
#
# LISTEN SET (I-7). The container answered on THREE addresses (public IP,
# 172.17.0.1, 127.0.0.1; docker-proxy). SAB binds exactly ONE host. Resolution
# (spec 5.4 #2, operator-approved): SAB binds net.app_host (the containerised
# arrs dial it), and qflix-tcpfwd.py, a child of the unit's main process,
# re-creates 127.0.0.1:<port> (nginx, canaries, MCP dial it). Any OTHER
# recorded address (the public IP) cannot be reproduced without 0.0.0.0, so
# --swap refuses unless --approve-exception is given; the dropped addresses are
# then recorded as swap-state exceptions with the reason (D-4).
#
# INERT BY DEFAULT (I-3). Every mode prints its plan (DRY-RUN) and touches
# nothing unless --execute is also given. Modes, in swap order:
#
#   --install   5.9 step 1. Parity with `appctl version sabnzbd` (I-10), fetch +
#               sha256-verify the source and the 3 helpers, ldd them (fail
#               closed), venv + `pip install -r requirements.txt`, render the
#               ExecStart wrapper, lay out bin/<ver> + `current`, the env file
#               and the STAGED unit in ~/.apps/sabnzbd/native/. NOT enabled.
#               Also runs the ini audit in warn mode (early notice).
#   --prove     5.9 step 2. Copy the ini to ~/.apps/.prove/sabnzbd, sanitize it
#               (servers enable=0, rss off, data dirs into the copy, notifiers
#               off; zero counts asserted), boot it on a FRESH 127.0.0.1 port:
#               /sabnzbd/ must answer 200 and the API must report the version.
#               Task delta gated at 70% of the ceiling (G-2). Then, under
#               `systemd-run --user --pipe` with the unit's PATH (G-3):
#               `command -v par2 unrar 7zz` must resolve inside bin/current, and
#               a fixture is par2-repaired, unrar-extracted and 7zz-extracted.
#               Writes swap/sabnzbd/proof.json; the copy is deleted.
#   --swap      5.9 steps 3-6. Needs the proof and the DEPLOYED pending-swap
#               manifest flip. Strict ini audit (api_key == secret, username +
#               password set, API key not disabled = no local-address bypass;
#               no container paths), capture the listen set (+ exceptions),
#               suppress sabnzbd + canary-sab-stall + canary-thread-ceiling,
#               PAUSE the queue and re-poll (SAB APIs lie), wait out
#               post-processing, snapshot, stop the container through appctl,
#               poll until no container process, the port free and the db
#               unheld (abort, restart the container and resume otherwise),
#               enable --now, verify, record swap state (14-day soak), resume.
#               Suppression stays ON.
#   --finish    5.9 step 9, after the follow-up PR dropped `swap_state` and 240
#               deployed it: verify, then lift every suppression together.
#   --rollback  Rollback 0-5. 0: re-suppress, pause (best effort), park + MASK
#               the unit. 1: stop it, wait for exit AND the port to free. 2: the
#               DEPLOYED manifest must dispatch sabnzbd as UCC again (otherwise
#               exit 10). Restore the recorded ini host/port. 3: start the
#               container via appctl, ready probe, resume, unsuppress.
#
# Exit: 0 ok | 1 refused/failed | 10 rollback paused for the manifest revert | 64 usage.
#
# Overrides (tests; resolved at call time): QFLIX_APPS_DIR QFLIX_UNIT_DIR
# QFLIX_ENV_DIR QFLIX_SWAP_DIR MANITOBA_STATE_DIR QFLIX_MANIFEST QFLIX_PROC
# QFLIX_PYTHON QFLIX_SAB_PYTHON (venv base) QFLIX_APPCTL QFLIX_SYSTEMCTL
# QFLIX_SYSTEMD_RUN QFLIX_SS QFLIX_PS QFLIX_LDD QFLIX_FUSER QFLIX_CURL (download)
# QFLIX_HTTP_CURL (prove) QFLIX_API_CURL (live) QFLIX_HOSTPOLICY QFLIX_HOST_ID_FILE
# QFLIX_SABNZBD_SHA256 QFLIX_PAR2_SHA256 QFLIX_7ZIP_SHA256 QFLIX_UNRAR_SHA256
# QFLIX_SECRETS_DIR QFLIX_POLL_S QFLIX_SETTLE_S QFLIX_STOP_TIMEOUT_S
# QFLIX_PROOF_TIMEOUT_S QFLIX_PAUSE_TIMEOUT_S QFLIX_PP_TIMEOUT_S QFLIX_KEEP_PROOF.
set -uo pipefail

SLUG=sabnzbd
# == versions.env SABNZBD_VERSION == the container's reported version (test-pinned)
VERSION="5.1.3"
SHA256="12a01e30ce166297a375ffc3a761f98bf7d93260e040391497f643f8a3525fed"
URL="https://github.com/sabnzbd/sabnzbd/releases/download/${VERSION}/SABnzbd-${VERSION}-src.tar.gz"
# Helpers: the versions the container ships (par2 1.5.0 turbo, 7-Zip 26.01,
# UnRAR 7.23; read from its rootfs 2026-10-10), as glibc/static upstream builds.
PAR2_VERSION="1.5.0"
PAR2_SHA256="5a9f64386813456693c2ea1fb7649436fe7544bbdf97fd73b3483dfcc8aca464"
PAR2_URL="https://github.com/animetosho/par2cmdline-turbo/releases/download/v${PAR2_VERSION}/par2cmdline-turbo-${PAR2_VERSION}-linux-amd64.zip"
SEVENZIP_VERSION="26.01"
SEVENZIP_SHA256="8ea0fc8a135e7b848e80a4116fe22dff56c8c4518dde1f43cce67f4e340b437a"
SEVENZIP_URL="https://github.com/ip7z/7zip/releases/download/${SEVENZIP_VERSION}/7z2601-linux-x64.tar.xz"
UNRAR_VERSION="7.23"
UNRAR_SHA256="759b4b6aa0d9f77131882162951193f3a0e54bf60e1d8dc4255aa308accab588"
UNRAR_URL="https://www.rarlab.com/rar/rarlinux-x64-723.tar.gz"
UNIT="qflix-${SLUG}.service"
FAMILY=python
EXE=qflix-sabnzbd            # the ExecStart wrapper rendered into bin/<ver>/
EXEC_ARGS=""
LOOPBACK="127.0.0.1"
# Muted with the app (plan row A9). Keys follow cli.py: canary-<name>.
SUPPRESS=("$SLUG" "canary-sab-stall" "canary-thread-ceiling")
PATTERN="SABnzbd.py"         # container /app/sabnzbd/SABnzbd.py and native bin/<ver>/SABnzbd.py
ENV_KEYS=(TZ LANG PYTHONIOENCODING)
# Job states that mean post-processing is running (history API `status`).
PP_STATES="Verifying Repairing Extracting Moving Running QuickCheck Fetching"
# Paths that exist only inside the container. A SAB dir key pointing at one would
# break natively; the arrs' remote path mappings must change WITH it (spec 12).
# Top-level names WITHOUT the leading slash (the audit adds it), so no shell layer
# can mistake them for paths and rewrite them.
CONTAINER_PREFIXES="config downloads data app incomplete-downloads watch"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # .../scripts
ARGS=("$@")

info() { echo "[308-sabnzbd] $*"; }
die()  { echo "[308-sabnzbd] ERROR: $*" >&2; exit 1; }
usage() {
  echo "usage: $0 [--install|--prove|--swap|--finish|--rollback] [--execute] [--approve-exception]" >&2
  exit 64
}

MODE=install
EXECUTE=0
APPROVE_EXC=0
for a in "$@"; do
  case "$a" in
    --install|--prove|--swap|--finish|--rollback) MODE="${a#--}" ;;
    --execute) EXECUTE=1 ;;
    --approve-exception) APPROVE_EXC=1 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

APPS="${QFLIX_APPS_DIR:-$HOME/.apps}"
APPDIR="$APPS/$SLUG"
INI="$APPDIR/sabnzbd.ini"
UNIT_DIR="${QFLIX_UNIT_DIR:-$HOME/.config/systemd/user}"
ENV_DIR="${QFLIX_ENV_DIR:-$HOME/.config/qflix}"
SWAPDIR="${QFLIX_SWAP_DIR:-$HOME/.opt/maint/swap}/$SLUG"
PROVE="$APPS/.prove/$SLUG"
MANIFEST="${QFLIX_MANIFEST:-$HOME/.opt/maint/apps.yaml}"
SECRETS="${QFLIX_SECRETS_DIR:-$HOME/secrets}"
MAINT_LIB="$HERE/maint/lib"
PY="${QFLIX_PYTHON:-python3}"
SAB_PY="${QFLIX_SAB_PYTHON:-python3}"
APPCTL="${QFLIX_APPCTL:-$HOME/bin/appctl}"
SYSTEMCTL="${QFLIX_SYSTEMCTL:-systemctl}"
SYSTEMD_RUN="${QFLIX_SYSTEMD_RUN:-systemd-run}"
SS="${QFLIX_SS:-ss}"
PS="${QFLIX_PS:-ps}"
LDD="${QFLIX_LDD:-ldd}"
FUSER="${QFLIX_FUSER:-fuser}"
HTTP_CURL="${QFLIX_HTTP_CURL:-curl}"
API_CURL="${QFLIX_API_CURL:-curl}"
PROC="${QFLIX_PROC:-/proc}"
POLL="${QFLIX_POLL_S:-2}"
SETTLE="${QFLIX_SETTLE_S:-10}"
STOP_TIMEOUT="${QFLIX_STOP_TIMEOUT_S:-120}"
PROOF_TIMEOUT="${QFLIX_PROOF_TIMEOUT_S:-180}"
PAUSE_TIMEOUT="${QFLIX_PAUSE_TIMEOUT_S:-60}"
PP_TIMEOUT="${QFLIX_PP_TIMEOUT_S:-1800}"
WANT_SHA="${QFLIX_SABNZBD_SHA256:-$SHA256}"
WANT_PAR2_SHA="${QFLIX_PAR2_SHA256:-$PAR2_SHA256}"
WANT_7ZIP_SHA="${QFLIX_7ZIP_SHA256:-$SEVENZIP_SHA256}"
WANT_UNRAR_SHA="${QFLIX_UNRAR_SHA256:-$UNRAR_SHA256}"
BIND_HOST=""

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
    install)  info "would check version parity ($VERSION), fetch $URL + par2 $PAR2_VERSION + 7-Zip $SEVENZIP_VERSION + UnRAR $UNRAR_VERSION (sha256), ldd them, build the venv, lay out $APPDIR/bin/$VERSION, write $ENV_DIR/$SLUG.env, stage $APPDIR/native/$UNIT (not enabled)" ;;
    prove)    info "would copy + sanitize the ini into $PROVE, boot it on a fresh 127.0.0.1 port, check /sabnzbd/ + the API version, gate the task delta at 70% of the ceiling, run par2/unrar/7zz fixtures under systemd-run --user with the unit PATH, then delete the copy" ;;
    swap)     info "would audit the ini (auth + paths), capture the listen set, suppress ${SUPPRESS[*]}, pause the queue + wait out post-processing, snapshot, stop the container, wait for exit, enable --now $UNIT, verify, record swap state, resume" ;;
    finish)   info "would verify the native unit and lift suppression for ${SUPPRESS[*]}" ;;
    rollback) info "would suppress ${SUPPRESS[*]}, pause, park + mask $UNIT, stop it, wait for the manifest revert, restore the ini host/port, start the container via appctl, resume, unsuppress" ;;
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
  sshm "~/scripts/configure/308-native-sabnzbd-install.sh $(printf '%q ' "${ARGS[@]}")"
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
cleanup() {
  [ -n "$PROOF_PID" ] && { kill "$PROOF_PID" 2>/dev/null; wait "$PROOF_PID" 2>/dev/null; }
  local p
  for p in "${CLEANUP_PATHS[@]}"; do rm -rf "$p"; done
}
trap cleanup EXIT

TAR=(tar --force-local)

# --- helpers -------------------------------------------------------------------------
sab_port() {
  local p
  p="$(tr -d '[:space:]' < "$SECRETS/sabnzbd.port" 2>/dev/null)"
  [[ "$p" =~ ^[0-9]{2,5}$ ]] || return 1
  printf '%s' "$p"
}
sab_key() {
  local k
  k="$(tr -d '[:space:]' < "$SECRETS/sabnzbd.key" 2>/dev/null)"
  [[ "$k" =~ ^[A-Za-z0-9]{16,64}$ ]] || return 1
  printf '%s' "$k"
}
# net.app_host is SAB's one bind. A plain IPv4 bridge address only: never the
# wildcard, never loopback (the forwarder owns loopback), never a name.
check_bind_host() {
  local h
  h="$(tr -d '[:space:]' < "$SECRETS/net.app_host" 2>/dev/null)"
  [[ "$h" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] && [ "$h" != "0.0.0.0" ] && [[ "$h" != 127.* ]] \
    || die "secret net.app_host='$h' is not a bridge address (empty, wildcard, loopback or non-IPv4); refusing (never widen the bind)"
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

# Nothing LISTENs on the port, on any address.
port_free() {
  local port; port="$(sab_port)" || return 1
  ! "$SS" -tlnH "sport = :$port" 2>/dev/null | grep -q ":$port\b"
}
# No process holds SAB's history db (fuser exit 1 = nobody). G-4.
db_free() {
  local db="$APPDIR/admin/history1.db"
  [ -e "$db" ] || return 0
  if ! command -v "$FUSER" >/dev/null 2>&1; then
    info "WARN: $FUSER not found; relying on the process + port checks"; return 0
  fi
  ! "$FUSER" "$db" "$db-wal" >/dev/null 2>&1
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
container_gone() { [ -z "$(scan_pids container)" ] && port_free && db_free; }
container_up()   { [ -n "$(scan_pids container)" ]; }
native_gone()    { [ -z "$(scan_pids native)" ] && { container_up || port_free; }; }

user_tasks() { "$PS" -u "$(id -u)" -L --no-headers 2>/dev/null | wc -l | tr -d ' '; }

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

unit_path() { printf '%s' "$APPDIR/bin/current:$HOME/bin:/usr/local/bin:/usr/bin:/bin"; }

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

# --- live API (SAB's APIs lie: every state change is re-polled) ------------------
# Args: HOST MODE [extra query]. stdout: the JSON body.
sab_api() {
  local port key
  port="$(sab_port)" || return 1
  key="$(sab_key)" || return 1
  "$API_CURL" -s -m 10 "http://$1:$port/sabnzbd/api?mode=$2&output=json&apikey=$key${3:-}" 2>/dev/null
}
# Args: HOST PATH. stdout: the final HTTP status (redirects followed).
http_code() {
  local port; port="$(sab_port)" || return 1
  "$API_CURL" -s -o /dev/null -w '%{http_code}' -L -m 10 "http://$1:$port$2" 2>/dev/null
}
ready_on() { [ "$(http_code "$1" /sabnzbd/)" = 200 ]; }
api_version_on() {
  sab_api "$1" version | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null | tr -d '\r'
}
# stdout: true|false|unknown
queue_paused() {
  sab_api "$LOOPBACK" queue | "$PY" -c '
import json, sys
try:
    q = json.load(sys.stdin).get("queue") or {}
    p = q.get("paused")
    print("true" if p in (True, "true", "True", 1, "1") else "false")
except Exception:
    print("unknown")' 2>/dev/null | tr -d '\r'
}
paused_twice()   { [ "$(queue_paused)" = true ] && sleep "$POLL" && [ "$(queue_paused)" = true ]; }
unpaused_twice() { [ "$(queue_paused)" = false ] && sleep "$POLL" && [ "$(queue_paused)" = false ]; }
# Post-processing idle: no history job in a PP state. Unknown (API down) is NOT idle.
pp_idle() {
  sab_api "$LOOPBACK" history "&limit=50" | "$PY" -c '
import json, sys
states = set(sys.argv[1].split())
try:
    slots = (json.load(sys.stdin).get("history") or {}).get("slots") or []
except Exception:
    sys.exit(2)
busy = [s.get("status") for s in slots if s.get("status") in states]
sys.exit(1 if busy else 0)' "$PP_STATES" 2>/dev/null
}
pause_queue() {
  sab_api "$LOOPBACK" pause >/dev/null || return 1
  wait_until "$PAUSE_TIMEOUT" paused_twice
}
resume_queue() {
  sab_api "$LOOPBACK" resume >/dev/null || return 1
  wait_until "$PAUSE_TIMEOUT" unpaused_twice
}

# --- ini audit + host/port restore (python, stdlib only) ---------------------------
# Args: MODE(strict|warn) INI KEY_SECRET_FILE OUT_JSON. Strict exits 1 on any
# violation; warn prints them and exits 0. Writes the findings either way.
ini_audit() {
  "$PY" - "$@" "$CONTAINER_PREFIXES" <<'PY'
import json, os, re, sys
mode, ini, keyfile, out, prefixes = sys.argv[1:6]
prefixes = ["/" + p.strip("/") for p in prefixes.split()]
misc, cats, top, sub = {}, {}, "", None
kv = re.compile(r"^\s*(\w+)\s*=\s*(.*?)\s*$")
for line in open(ini, encoding="utf-8", errors="replace"):
    s = line.strip()
    if s.startswith("[[") and s.endswith("]]"):
        sub = s.strip("[]"); continue
    if s.startswith("[") and s.endswith("]"):
        top, sub = s.strip("[]"), None; continue
    m = kv.match(line)
    if not m:
        continue
    k, v = m.group(1), m.group(2)
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    if top == "misc" and sub is None:
        misc[k] = v
    elif top == "categories" and sub and k == "dir":
        cats[sub] = v
try:
    secret = open(keyfile, encoding="utf-8").read().strip()
except OSError:
    secret = ""
bad, warn = [], []
auth = {"api_key_matches_secret": bool(misc.get("api_key")) and misc.get("api_key") == secret,
        "username_set": bool(misc.get("username")),
        "password_set": bool(misc.get("password")),
        "api_key_disabled": misc.get("disable_api_key", "0") not in ("", "0")}
if not misc.get("api_key"):
    bad.append("api_key is empty (the API would be open)")
elif not auth["api_key_matches_secret"]:
    bad.append("api_key differs from secret sabnzbd.key (drift; spec 5.5)")
if not auth["username_set"] or not auth["password_set"]:
    bad.append("username/password not both set: local-address auth bypass (spec 5.9 step 3, G-1)")
if auth["api_key_disabled"]:
    bad.append("disable_api_key is on (the API would be open)")
paths = {}
for k in ("download_dir", "complete_dir", "script_dir", "dirscan_dir", "nzb_backup_dir",
          "admin_dir", "log_dir", "backup_dir", "password_file", "https_cert", "https_key",
          "https_chain"):
    v = misc.get(k, "")
    paths[k] = v
    if v and any(v == p or v.startswith(p + "/") for p in prefixes):
        bad.append("%s=%s is a container path; rewrite it together with the arr remote path mappings first" % (k, v))
for k in ("download_dir", "complete_dir"):
    v = misc.get(k, "")
    if v.startswith("/") and not any(v == p or v.startswith(p + "/") for p in prefixes) and not os.path.isdir(v):
        bad.append("%s=%s does not exist on this host" % (k, v))
for c, v in sorted(cats.items()):
    if v.startswith("/") and any(v == p or v.startswith(p + "/") for p in prefixes):
        bad.append("category %s dir=%s is a container path" % (c, v))
if misc.get("pause_on_post_processing", "0") not in ("", "0"):
    warn.append("pause_on_post_processing=%s (expected 0 since 2026-08-08); carried as-is, not changed by the swap"
                % misc.get("pause_on_post_processing"))
rec = {"auth": auth, "paths": paths, "categories": cats,
       "listen": {"host": misc.get("host", ""), "port": misc.get("port", "")},
       "pause_on_post_processing": misc.get("pause_on_post_processing", ""),
       "violations": bad, "warnings": warn}
if out != "-":
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=2, sort_keys=True)
    os.replace(tmp, out)
for w in warn:
    print("WARN: " + w)
for b in bad:
    print(("VIOLATION: " if mode == "strict" else "WARN: ") + b)
sys.exit(1 if (bad and mode == "strict") else 0)
PY
}

# Args: INI HOST PORT. Rewrites the top-level [misc] host/port keys in place
# (atomic); every other byte is kept.
ini_set_listen() {
  "$PY" - "$@" <<'PY'
import os, re, sys
ini, host, port = sys.argv[1:4]
lines = open(ini, encoding="utf-8").read().splitlines()
out, top, sub, done = [], "", False, set()
for line in lines:
    s = line.strip()
    if s.startswith("[[") and s.endswith("]]"):
        sub = True
    elif s.startswith("[") and s.endswith("]"):
        top, sub = s.strip("[]"), False
    m = re.match(r"^(\s*)(host|port)(\s*=\s*)(.*)$", line)
    if m and top == "misc" and not sub:
        line = m.group(1) + m.group(2) + m.group(3) + (host if m.group(2) == "host" else port)
        done.add(m.group(2))
    out.append(line)
if done != {"host", "port"}:
    sys.exit("ini has no [misc] host/port keys to restore")
tmp = ini + ".qflix-tmp"
with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(out) + "\n")
os.chmod(tmp, os.stat(ini).st_mode & 0o777)
os.replace(tmp, ini)
PY
}

# --- fetch + layout --------------------------------------------------------------------
# Args: ARCHIVE MEMBER DEST. Pulls ONE member out of a .zip / .tar.(gz|xz).
py_extract_member() {
  "$PY" - "$@" <<'PY'
import shutil, sys, tarfile, zipfile
arc, member, dest = sys.argv[1:4]
if zipfile.is_zipfile(arc):
    with zipfile.ZipFile(arc) as z, z.open(member) as src, open(dest, "wb") as dst:
        shutil.copyfileobj(src, dst)
else:
    with tarfile.open(arc) as t:
        src = t.extractfile(member)
        if src is None:
            sys.exit("member %s missing" % member)
        with open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
PY
}

# Every helper must resolve every shared library, or be static.
ldd_ok() {
  local f="$1" out rc
  out="$("$LDD" "$f" 2>&1)"; rc=$?
  case "$out" in *"not a dynamic executable"*|*"statically linked"*) return 0 ;; esac
  [ "$rc" = 0 ] || die "ldd failed on $f: $(echo "$out" | head -2 | tr '\n' ' ') (fail closed)"
  if echo "$out" | grep -q "not found"; then
    die "$f has missing libraries: $(echo "$out" | grep 'not found' | awk '{print $1}' | tr '\n' ' ')"
  fi
}

# Wrapper = ExecStart. SAB binds ONE host; the forwarder (started first, so it is
# a child of the unit's MainPID once the shell execs SAB) re-creates loopback.
render_wrapper() {
  cat <<'EOF'
#!/usr/bin/env bash
# qflix-sabnzbd -- ExecStart of qflix-sabnzbd.service (QFLX-33). Rendered by
# scripts/configure/308-native-sabnzbd-install.sh; never edit on the box.
# SAB binds ONE address (SAB_BIND = net.app_host). The forwarder below is started
# BEFORE the exec, so it stays a child of the unit's MainPID (runtime-parity
# port-owner) and dies with the unit's cgroup.
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
: "${SAB_BIND:?}" "${SAB_PORT:?}" "${SAB_INI:?}"
py="$here/venv/bin/python"
if [ -n "${SAB_LOOPBACK:-}" ]; then
  "$py" "$here/qflix-tcpfwd.py" --listen "$SAB_LOOPBACK:$SAB_PORT" --target "$SAB_BIND:$SAB_PORT" &
fi
exec "$py" -OO "$here/SABnzbd.py" --config-file "$SAB_INI" --server "$SAB_BIND:$SAB_PORT" --browser 0
EOF
}

# Args: STAGE. Builds STAGE/rel = what becomes bin/<ver>.
build_release() {
  local stage="$1" rel="$1/rel" req pin got
  native_fetch_verify "$URL" "$WANT_SHA" "$stage/sab.tgz" || die "SABnzbd fetch/sha256 verify failed"
  native_fetch_verify "$PAR2_URL" "$WANT_PAR2_SHA" "$stage/par2.zip" || die "par2 fetch/sha256 verify failed"
  native_fetch_verify "$SEVENZIP_URL" "$WANT_7ZIP_SHA" "$stage/7z.txz" || die "7-Zip fetch/sha256 verify failed"
  native_fetch_verify "$UNRAR_URL" "$WANT_UNRAR_SHA" "$stage/rar.tgz" || die "UnRAR fetch/sha256 verify failed"
  mkdir -p "$stage/x"
  "$PY" - "$stage/sab.tgz" "$stage/x" <<'PY' || die "cannot extract the SABnzbd source"
import sys, tarfile
with tarfile.open(sys.argv[1]) as t:
    kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
    t.extractall(sys.argv[2], **kw)
PY
  [ -f "$stage/x/SABnzbd-$VERSION/SABnzbd.py" ] || die "the source tarball has no SABnzbd-$VERSION/SABnzbd.py"
  mv "$stage/x/SABnzbd-$VERSION" "$rel" || die "cannot stage the source"
  py_extract_member "$stage/par2.zip" par2 "$rel/par2" || die "par2 missing from its zip"
  py_extract_member "$stage/7z.txz" 7zzs "$rel/7zz" || die "7zzs missing from the 7-Zip tarball"
  py_extract_member "$stage/rar.tgz" rar/unrar "$rel/unrar" || die "unrar missing from the UnRAR tarball"
  chmod 0755 "$rel/par2" "$rel/7zz" "$rel/unrar"
  rm -f "$stage/sab.tgz" "$stage/par2.zip" "$stage/7z.txz" "$stage/rar.tgz"
  ldd_ok "$rel/par2"; ldd_ok "$rel/7zz"; ldd_ok "$rel/unrar"
  "$SAB_PY" -m venv "$rel/venv" || die "python3 -m venv failed"
  "$rel/venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir --prefer-binary \
      -q -r "$rel/requirements.txt" || die "pip install -r requirements.txt failed"
  # sabctools is the C extension SAB will not download without: assert the pinned build imports.
  req="$(grep -E '^sabctools==' "$rel/requirements.txt" | head -1)"; pin="${req#sabctools==}"
  got="$("$rel/venv/bin/python" -c 'import sabctools, cherrypy, cheroot, configobj; print(sabctools.__version__)' 2>/dev/null | tr -d '\r')"
  [ -n "$pin" ] && [ "$got" = "$pin" ] || die "venv smoke import failed (sabctools '$got' != pinned '$pin')"
  "$rel/venv/bin/python" -m pip freeze --disable-pip-version-check > "$rel/.pip-freeze" 2>/dev/null || true
  render_wrapper > "$rel/$EXE" && chmod 0755 "$rel/$EXE" || die "cannot render the wrapper"
  cp -f "$HERE/lib/qflix-tcpfwd.py" "$rel/qflix-tcpfwd.py" || die "cannot copy qflix-tcpfwd.py (run 240 first)"
  info "built: SABnzbd $VERSION, sabctools $got, par2 $PAR2_VERSION, 7zz $SEVENZIP_VERSION, unrar $UNRAR_VERSION"
}

# --- modes ----------------------------------------------------------------------------
do_install() {
  local stage port
  port="$(sab_port)" || die "secret sabnzbd.port missing/invalid; refusing"
  sab_key >/dev/null || die "secret sabnzbd.key missing/invalid; refusing"
  check_bind_host
  [ -f "$INI" ] || die "$INI not found; nothing to convert"
  native_check_parity "$SLUG" "$VERSION" || die "version parity refused (see above)"
  ini_audit warn "$INI" "$SECRETS/sabnzbd.key" - || true
  mkdir -p "$APPS" || die "cannot create $APPS"
  stage="$(mktemp -d "$APPS/.stage-$SLUG.XXXXXX")" || die "mktemp failed"
  CLEANUP_PATHS+=("$stage")
  build_release "$stage"
  native_install_versioned "$SLUG" "$VERSION" "$stage/rel" || die "install refused (see above)"
  local -a extra=("SAB_BIND=$BIND_HOST" "SAB_PORT=$port" "SAB_LOOPBACK=$LOOPBACK" "SAB_INI=$INI")
  local kv
  local -A seen=()
  while IFS= read -r kv; do
    [ -n "$kv" ] || continue
    extra+=("$kv"); seen["${kv%%=*}"]=1
  done < <(container_env)
  [ -n "${seen[PYTHONIOENCODING]:-}" ] || extra+=("PYTHONIOENCODING=utf-8")
  native_render_env "$SLUG" "$FAMILY" "$VERSION" "${extra[@]}" \
    | native_write_secure "$ENV_DIR/$SLUG.env" 0600 || die "env file write failed"
  native_render_unit "$SLUG" "$FAMILY" "$EXE" "$EXEC_ARGS" | sed 's/[ \t]*$//' \
    | native_write_secure "$APPDIR/native/$UNIT" 0644 || die "unit staging failed"
  info "installed $VERSION; unit staged at $APPDIR/native/$UNIT (not enabled). Next: --prove --execute"
}

# The helper proof script, run under systemd-run with the unit's PATH (G-3).
render_fixture() {
  cat <<'EOF'
set -eu
rar="$1"
for b in par2 unrar 7zz; do
  p="$(command -v "$b")" || { echo "MISSING $b"; exit 3; }
  echo "$b=$p"
done
head -c 65536 /dev/urandom > f.bin
cp f.bin orig.bin
par2 create -q -q -r25 f.par2 f.bin >/dev/null
printf 'QFLIXQFLIXQFLIX' | dd of=f.bin bs=1 seek=4096 conv=notrunc 2>/dev/null
if cmp -s f.bin orig.bin; then echo "FIXTURE NOT DAMAGED"; exit 4; fi
par2 repair -q -q f.par2 f.bin >/dev/null
cmp -s f.bin orig.bin || { echo "PAR2 REPAIR FAILED"; exit 4; }
"$rar" a -inul -ep fx.rar orig.bin
mkdir -p u && unrar x -inul -o+ fx.rar u/
cmp -s u/orig.bin orig.bin || { echo "UNRAR FAILED"; exit 5; }
7zz a -bd fx.7z orig.bin >/dev/null
mkdir -p s && 7zz x -bd -y -os fx.7z >/dev/null
cmp -s s/orig.bin orig.bin || { echo "7ZZ FAILED"; exit 6; }
echo "FIXTURE OK"
EOF
}

do_prove() {
  local before ceiling peak sample pp key code ver out b line start
  [ -x "$APPDIR/bin/current/$EXE" ] && [ -x "$APPDIR/bin/current/venv/bin/python" ] \
    || die "not installed; run --install --execute first"
  [ -f "$INI" ] || die "$INI not found"
  key="$(sab_key)" || die "secret sabnzbd.key missing/invalid"
  ceiling="$(hostpolicy task-ceiling)" || die "task ceiling unknown; refusing (G-2)"
  ceiling="${ceiling%$'\r'}"
  rm -rf "$PROVE"
  mkdir -p "$PROVE/fx" || die "cannot create $PROVE"
  [ "${QFLIX_KEEP_PROOF:-0}" = 1 ] || CLEANUP_PATHS+=("$PROVE")
  ( umask 077; cp -f "$INI" "$PROVE/sabnzbd.ini" ) || die "cannot copy the ini"
  "$PY" "$HERE/maint/native_sanitize.py" "$SLUG" "$PROVE" >"$PROVE/sanitize.json" \
    || die "sanitize refused the proof copy; not booting it (I-11)"
  pp="$("$PY" -c 'import socket
s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])' | tr -d '\r')"
  [ -n "$pp" ] || die "cannot pick a free loopback port"
  before="$(user_tasks)"
  [[ "$before" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  # Loopback only, the copy's ini, no forwarder, no nginx fragment, no Kuma monitor.
  ( cd "$PROVE" && exec env -u DISPLAY HOME="$HOME" PATH="$(unit_path)" MALLOC_ARENA_MAX=2 \
      PYTHONIOENCODING=utf-8 QFLIX_PROC="$PROC" \
      "$APPDIR/bin/current/venv/bin/python" -OO "$APPDIR/bin/current/SABnzbd.py" \
      --config-file "$PROVE/sabnzbd.ini" --server "127.0.0.1:$pp" --browser 0 \
      >"$PROVE/stdout.log" 2>&1 ) &
  PROOF_PID=$!
  start=$SECONDS
  ready() { [ "$("$HTTP_CURL" -s -o /dev/null -w '%{http_code}' -L -m 5 "http://127.0.0.1:$pp/sabnzbd/" 2>/dev/null)" = 200 ]; }
  if ! wait_until "$PROOF_TIMEOUT" ready; then
    tail -5 "$PROVE/stdout.log" >&2
    die "proof: /sabnzbd/ never answered 200 on 127.0.0.1:$pp within ${PROOF_TIMEOUT}s"
  fi
  ver="$("$HTTP_CURL" -s -m 10 "http://127.0.0.1:$pp/sabnzbd/api?mode=version&output=json&apikey=$key" 2>/dev/null \
        | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null | tr -d '\r')"
  [ "$ver" = "$VERSION" ] || die "proof: the API reports version '$ver', expected $VERSION"
  peak="$(tree_tasks "$PROOF_PID")"
  local i
  for i in 1 2 3; do
    sleep "$POLL"
    sample="$(tree_tasks "$PROOF_PID")"
    [ "${sample:-0}" -gt "${peak:-0}" ] && peak="$sample"
  done
  kill "$PROOF_PID" 2>/dev/null; wait "$PROOF_PID" 2>/dev/null; PROOF_PID=""
  [[ "$peak" =~ ^[0-9]+$ ]] && [ "$peak" -gt 0 ] || die "proof: cannot read the task count; refusing"
  if [ $(( (before + peak) * 100 )) -ge $(( 70 * ceiling )) ]; then
    die "thread gate: $before + $peak tasks reaches 70% of the ceiling $ceiling; refusing the swap"
  fi

  # Helpers, as the UNIT will see them (systemd --user default PATH differs from a login shell).
  native_fetch_verify "$UNRAR_URL" "$WANT_UNRAR_SHA" "$PROVE/rar.tgz" || die "UnRAR fetch for the fixture failed"
  py_extract_member "$PROVE/rar.tgz" rar/rar "$PROVE/rar" && chmod 0755 "$PROVE/rar" || die "no rar in the UnRAR tarball"
  render_fixture > "$PROVE/fx/fx.sh"
  out="$("$SYSTEMD_RUN" --user --pipe --wait --quiet --working-directory="$PROVE/fx" \
          -p "Environment=PATH=$(unit_path)" bash "$PROVE/fx/fx.sh" "$PROVE/rar" 2>&1)"
  echo "$out" | grep -qx "FIXTURE OK" || { echo "$out" | tail -5 >&2; die "proof: helper fixture failed under systemd-run"; }
  for b in par2 unrar 7zz; do
    line="$(echo "$out" | grep -m1 "^$b=" | tr -d '\r')"
    # -ef: the very file we installed (bin/current/<b>), not a same-named system one.
    [ -n "${line#*=}" ] && [ "${line#*=}" -ef "$APPDIR/bin/current/$b" ] \
      || die "proof: under the unit PATH '$b' resolves to '${line#*=}', not $APPDIR/bin/current/$b"
  done

  mkdir -p "$SWAPDIR"
  printf '{"ok": true, "version": "%s", "before": %s, "delta": %s, "ceiling": %s, "helpers": "par2 %s, unrar %s, 7zz %s", "at": "%s"}\n' \
    "$VERSION" "$before" "$peak" "$ceiling" "$PAR2_VERSION" "$UNRAR_VERSION" "$SEVENZIP_VERSION" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | native_write_secure "$SWAPDIR/proof.json" 0644 || die "cannot record the proof"
  info "PROOF OK: /sabnzbd/ 200, API $ver, helpers + par2/unrar/7zz fixture OK under systemd-run; before=$before delta=$peak ceiling=$ceiling elapsed=$((SECONDS - start))s. Next: --swap --execute"
}

verify_native() {
  local n h
  sleep "$SETTLE"
  sysd is-active "$UNIT" >/dev/null 2>&1 || { echo "unit not active" >&2; return 1; }
  [ -z "$(scan_pids container)" ] || { echo "a container process is running" >&2; return 1; }
  n="$(scan_pids native | wc -l | tr -d ' ')"
  [ "$n" -ge 1 ] || { echo "no native process" >&2; return 1; }
  for h in "$LOOPBACK" "$BIND_HOST"; do
    wait_until 90 ready_on "$h" || { echo "/sabnzbd/ on $h did not answer 200" >&2; return 1; }
    [ "$(api_version_on "$h")" = "$VERSION" ] || { echo "API on $h does not report $VERSION" >&2; return 1; }
  done
  native_listen_compare "$SLUG" >/dev/null || { echo "listen set differs from listen-set.before (minus exceptions)" >&2; return 1; }
}

# Abort path before the unit was enabled: container back, queue as it was, unmuted.
abort_swap() {
  "$APPCTL" start "$SLUG" >/dev/null 2>&1 || true
  if wait_until "$STOP_TIMEOUT" ready_on "$LOOPBACK" && [ "$(cat "$SWAPDIR/queue-paused.before" 2>/dev/null)" = false ]; then
    resume_queue || info "WARN: queue resume not confirmed; check SAB by hand"
  fi
  suppression remove "${SUPPRESS[@]}" >/dev/null || true
  die "$1; swap aborted, container start requested, suppression lifted"
}

do_swap() {
  local st cls ss_state ver port addr extras=() was ts
  [ -f "$SWAPDIR/proof.json" ] || die "no proof recorded; run --prove --execute first"
  [ -x "$APPDIR/bin/current/$EXE" ] && [ -f "$APPDIR/native/$UNIT" ] && [ -f "$ENV_DIR/$SLUG.env" ] \
    || die "not installed; run --install --execute first"
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state _ <<<"$st"
  [ "$cls" = systemd ] && [ "$ss_state" = pending-swap ] \
    || die "the deployed manifest is not the pending-swap flip (class=$cls swap_state=$ss_state); merge + deploy it via 240 first"
  check_bind_host
  port="$(sab_port)" || die "secret sabnzbd.port missing/invalid; refusing"
  sab_key >/dev/null || die "secret sabnzbd.key missing/invalid; refusing"
  grep -qx "SAB_BIND=$BIND_HOST" "$ENV_DIR/$SLUG.env" && grep -qx "SAB_PORT=$port" "$ENV_DIR/$SLUG.env" \
    && grep -qx "SAB_LOOPBACK=$LOOPBACK" "$ENV_DIR/$SLUG.env" \
    || die "$ENV_DIR/$SLUG.env does not bind exactly $BIND_HOST:$port + $LOOPBACK:$port; re-run --install --execute"

  if sysd is-active "$UNIT" >/dev/null 2>&1 && [ -z "$(scan_pids container)" ]; then
    info "already swapped; verifying only"
    verify_native || die "native sabnzbd fails parity; run --rollback --execute"
    info "verified"
    return 0
  fi

  if is_masked; then
    sysd unmask "$UNIT" || die "unmask $UNIT failed"
    rm -f "$ENV_DIR/parked-units/$UNIT"
  fi

  ver="$(native_ucc_version "$SLUG")"
  [ "${ver#v}" = "$VERSION" ] || die "version parity: container=$ver native=$VERSION"
  mkdir -p "$SWAPDIR"
  # Step 3: ini audit (auth bypass off, no container paths) -- strict, recorded.
  ini_audit strict "$INI" "$SECRETS/sabnzbd.key" "$SWAPDIR/ini-audit.json" \
    || die "ini audit failed (see VIOLATION lines); nothing touched"
  # The container's own host/port keys: rollback writes these back.
  "$PY" -c 'import json,sys; l=json.load(open(sys.argv[1]))["listen"]; print("host=%s\nport=%s" % (l["host"], l["port"]))' \
    "$SWAPDIR/ini-audit.json" | tr -d '\r' > "$SWAPDIR/ini-listen.before.tmp" \
    && { [ -s "$SWAPDIR/ini-listen.before" ] || mv -f "$SWAPDIR/ini-listen.before.tmp" "$SWAPDIR/ini-listen.before"; } \
    || die "cannot record the ini host/port"
  rm -f "$SWAPDIR/ini-listen.before.tmp"

  native_listen_capture "$SLUG" "$port" >/dev/null || die "listen-set capture failed"
  grep -qx "$LOOPBACK:$port" "$SWAPDIR/listen-set.before" && grep -qx "$BIND_HOST:$port" "$SWAPDIR/listen-set.before" \
    || die "listen set lacks $LOOPBACK:$port or $BIND_HOST:$port ($(tr '\n' ' ' < "$SWAPDIR/listen-set.before")); is the container up?"
  while IFS= read -r addr; do
    [ -n "$addr" ] || continue
    case "$addr" in "$LOOPBACK:$port"|"$BIND_HOST:$port") ;; *) extras+=("$addr") ;; esac
  done < "$SWAPDIR/listen-set.before"
  if [ "${#extras[@]}" -gt 0 ]; then
    [ "$APPROVE_EXC" = 1 ] || die "listen set has ${extras[*]} which SAB cannot reproduce without 0.0.0.0; re-run with --approve-exception to drop them (operator decision D-4: ingress is nginx-only)"
    swapstate add-exception "$SLUG" "${extras[@]}" \
      --reason "QFLX-33 operator-approved: SAB binds one host; public listener dropped, ingress via nginx (D-4)" >/dev/null \
      || die "cannot record the listen-set exceptions"
    info "recorded listen-set exception(s): ${extras[*]}"
  fi
  swapstate set "$SLUG" "ucc_version=$VERSION" >/dev/null || die "cannot record ucc_version"
  container_env > "$SWAPDIR/container-env.before" 2>/dev/null || true

  # Step 4: suppress app + canaries BEFORE anything stops.
  suppression add "${SUPPRESS[@]}" --reason "QFLX-33 swap to native" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to swap unsuppressed"

  # Pause, re-poll, wait out post-processing (an unpack must not be cut).
  was="$(queue_paused)"
  [ "$was" = true ] || [ "$was" = false ] || { suppression remove "${SUPPRESS[@]}" >/dev/null || true; die "cannot read the queue state over the API; nothing stopped, suppression lifted"; }
  printf '%s\n' "$was" > "$SWAPDIR/queue-paused.before"
  if [ "$was" = false ]; then
    pause_queue || abort_swap "the queue did not report paused (re-polled) within ${PAUSE_TIMEOUT}s"
  fi
  wait_until "$PP_TIMEOUT" pp_idle || abort_swap "post-processing still running after ${PP_TIMEOUT}s"

  # Step 5: snapshot (config + admin; logs/downloads/binaries excluded).
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  "${TAR[@]}" -czf "$SWAPDIR/snapshot-$ts.tgz" -C "$APPS" \
      --exclude="$SLUG/logs" --exclude="$SLUG/Downloads" --exclude="$SLUG/bin" \
      --exclude="$SLUG/native" "$SLUG" 2>/dev/null || abort_swap "snapshot failed"
  cp -p "$INI" "$SWAPDIR/sabnzbd.ini.before" || abort_swap "cannot keep the ini copy"

  # Step 6: stop, then poll: no container process, port free, db unheld.
  "$APPCTL" stop "$SLUG" >/dev/null 2>&1 || info "appctl stop returned non-zero; polling decides"
  if ! wait_until "$STOP_TIMEOUT" container_gone; then
    abort_swap "the container did not exit within ${STOP_TIMEOUT}s (pids: $(scan_pids container | tr '\n' ' '))"
  fi
  mkdir -p "$UNIT_DIR"
  native_write_secure "$UNIT_DIR/$UNIT" 0644 < "$APPDIR/native/$UNIT" || die "unit install failed; run --rollback --execute"
  sysd daemon-reload || die "daemon-reload failed; run --rollback --execute"
  sysd enable --now "$UNIT" || die "enable --now $UNIT failed; run --rollback --execute"

  if ! verify_native; then
    die "native sabnzbd fails parity after the swap; queue left paused, suppression kept ON; run --rollback --execute"
  fi
  local now soak
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  soak="$(date -u -d '+14 days' +%Y-%m-%dT%H:%M:%SZ)" || die "cannot compute soak_until"
  swapstate set "$SLUG" "swap_date=$now" "soak_until=$soak" "rollback_window=open" >/dev/null \
    || die "cannot record swap state"
  if [ "$was" = false ]; then
    resume_queue || die "SWAPPED, but the queue did not report resumed (re-polled); resume it in SAB by hand"
  else
    info "the queue was paused before the swap; left paused"
  fi
  info "SWAPPED to native $VERSION on $BIND_HOST:$port (+ forwarder $LOOPBACK:$port); soak until $soak. elapsed=$((SECONDS - T0))s"
  info "Next: one real download end to end, PR dropping swap_state: pending-swap, deploy via 240, then --finish --execute"
}

do_finish() {
  local st cls ss_state dormant
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state dormant <<<"$st"
  [ "$cls" = systemd ] && [ -z "$ss_state" ] && [ "$dormant" = 1 ] \
    || die "deployed manifest still says class=$cls swap_state=${ss_state:-none} dormant=$dormant; deploy the follow-up (no pending-swap) first"
  verify_native || die "native sabnzbd fails parity; run --rollback --execute"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "FINISHED: ${SUPPRESS[*]} unsuppressed; 14-day soak running"
}

do_rollback() {
  local isn host port
  suppression add "${SUPPRESS[@]}" --reason "QFLX-33 rollback to UCC" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to roll back unsuppressed"
  # Best effort: an unpack must not be cut, but rollback is the emergency path.
  if [ -n "$(scan_pids native)" ] && [ "$(queue_paused)" = false ]; then
    pause_queue && wait_until "$PP_TIMEOUT" pp_idle || info "WARN: could not pause / drain the native queue; stopping anyway"
  fi
  mask_unit
  sysd stop "$UNIT" >/dev/null 2>&1 || true
  wait_until "$STOP_TIMEOUT" native_gone || die "native sabnzbd did not stop (or the port stayed bound) within ${STOP_TIMEOUT}s"
  isn="$("$APPCTL" is-native "$SLUG" 2>/dev/null)"
  if [ "${isn%$'\r'}" != ucc ]; then
    echo "[308-sabnzbd] PAUSED: native stopped + masked; revert the deployed manifest (PR + 240) so appctl dispatches $SLUG as UCC, then re-run --rollback --execute" >&2
    exit 10
  fi
  # SAB wrote --server back into the ini: restore the container's own keys.
  if [ -s "$SWAPDIR/ini-listen.before" ] && ! container_up; then
    host="$(sed -n 's/^host=//p' "$SWAPDIR/ini-listen.before")"
    port="$(sed -n 's/^port=//p' "$SWAPDIR/ini-listen.before")"
    [ -n "$port" ] || die "ini-listen.before has no port; restore $INI from $SWAPDIR/sabnzbd.ini.before by hand"
    cp -p "$INI" "$SWAPDIR/sabnzbd.ini.native-$(date -u +%Y%m%dT%H%M%SZ)" 2>/dev/null || true
    ini_set_listen "$INI" "$host" "$port" || die "cannot restore host/port in $INI"
    info "restored ini host=$host port=$port"
  fi
  "$APPCTL" start "$SLUG" >/dev/null 2>&1 || info "appctl start returned non-zero; polling decides"
  wait_until "$STOP_TIMEOUT" container_up || die "the container did not come back within ${STOP_TIMEOUT}s; still suppressed"
  wait_until 90 ready_on "$LOOPBACK" || die "container is up but /sabnzbd/ on $LOOPBACK is not ready; still suppressed"
  if [ "$(cat "$SWAPDIR/queue-paused.before" 2>/dev/null || echo false)" = false ] && [ "$(queue_paused)" = true ]; then
    resume_queue || die "container is back but the queue did not report resumed; still suppressed"
  fi
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "ROLLED BACK to UCC; $UNIT masked (unmasked by the next --swap). elapsed=$((SECONDS - T0))s"
}

case "$MODE" in finish|rollback) check_bind_host ;; esac

case "$MODE" in
  install)  do_install ;;
  prove)    do_prove ;;
  swap)     do_swap ;;
  finish)   do_finish ;;
  rollback) do_rollback ;;
esac
