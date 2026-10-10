#!/usr/bin/env bash
# 302-native-tautulli-install.sh -- QFLX-34 (UCC divorce A10, convert tautulli).
#
# Moves tautulli off the Ultra.cc container manager (UCC) onto a user unit the
# repo owns: the upstream release tarball (git tag) plus a python venv, run as
# qflix-tautulli.service at EXACTLY the container's version, sha256-pinned.
# Spec: docs/superpowers/specs/2026-10-09-ucc-divorce-design.md 5.1-5.9, row 10.
# Same shape as the pilot, scripts/configure/300-native-unpackerr-install.sh.
#
# RUNS ON THE BOX. 240-maintenance-install.sh deploys it to ~/scripts/configure/
# with ~/scripts/lib/native.sh beside it. Started from the workstation it
# re-executes the deployed copy over ssh (scripts/lib/ssh.sh).
#
# DATA STAYS IN PLACE: ~/.apps/tautulli/{config.ini,tautulli.db} are the live
# files the container already bind-mounts as /config. The native unit points
# --datadir at that same directory. The pms_* settings are NEVER edited: the
# app keeps dialling the same Plex (pms_url_manual=1, the gateway pin that
# scripts/configure/50-tautulli-pms-url-fix.sh owns). The only config edits are
# the container-isms the swap must undo (see CONFIG EDITS) and they are
# reversed by --rollback.
#
# INERT BY DEFAULT (I-3). Every mode prints its plan (DRY-RUN) and touches
# nothing unless --execute is also given. Modes, in swap order:
#
#   --precheck  Fetch + sha256-verify the release into a scratch dir, check the
#               box python can build a venv. Installs nothing.
#   --install   5.9 step 1. Parity with `appctl version tautulli` (I-10), venv +
#               requirements built in a scratch dir, bin/<ver> + `current`, the
#               env file (TAUTULLI_PORT, TZ/LANG from the container) and the
#               STAGED unit in ~/.apps/tautulli/native/. NOT copied into
#               ~/.config/systemd/user and NOT enabled (WantedBy=default.target
#               would start it beside the live container, I-6).
#   --prove     5.9 step 2. VACUUM INTO copy of tautulli.db + a copy of
#               config.ini into ~/.apps/.prove/tautulli/data, native_sanitize
#               (notifiers deleted, newsletters off, GitHub update checks off,
#               counts must be 0), the copy booted on a FRESH 127.0.0.1 port
#               with no nginx fragment and no Kuma monitor; get_tautulli_info
#               must report v<VERSION>; the proof copy's pms_* must equal the
#               live ones; the process-tree task count is gated at 70% of the
#               task ceiling (G-2). Never touches the live files or port.
#               Writes swap/tautulli/proof.json. The copy is destroyed.
#   --swap      5.9 steps 3-6: needs the proof and the DEPLOYED pending-swap
#               manifest flip. Unmask, parity, ingress check (nginx must reach
#               it on 127.0.0.1), capture the listen set, path-audit the
#               config, suppress the app + canary-tautulli-plex-link, stop the
#               container through appctl, POLL until no container process, a
#               free port and no open db file (abort and restore the container
#               otherwise), snapshot config + db, apply the CONFIG EDITS,
#               enable --now, verify parity, record swap state (14-day soak).
#               Suppression stays ON.
#   --finish    5.9 step 9, after the follow-up PR dropped `swap_state` and 240
#               deployed it: verify, then lift every suppression together.
#   --rollback  Rollback 0-5. 0: re-suppress, park the unit and MASK it. 1: stop
#               it and wait for exit AND the port to free. 2: the DEPLOYED
#               manifest must dispatch tautulli as UCC again (otherwise exit
#               10; re-run after the revert PR + 240). 3: undo the CONFIG
#               EDITS, start the container through appctl, wait for the API.
#               5: nothing to restore (same version, no migration ran).
#
# LISTEN SET (I-7, spec 5.4). The container listens on loopback, the public IP
# and the docker gateway at once; Tautulli's web server takes ONE bind address.
# Resolution 2 of 5.4 (operator default D-4): drop the public-IP and gateway
# listeners because ALL ingress is nginx -> 127.0.0.1:<port> (and every
# on-box consumer dials 127.0.0.1). --swap refuses unless the nginx fragment
# proves that, binds 127.0.0.1 only (NEVER 0.0.0.0), and records the dropped
# addresses as listen-set EXCEPTIONS in the swap state so the runtime-parity
# leg stays exact: any extra or wider listener is still a finding.
#
# CONFIG EDITS (the only ones; each is reversed by --rollback):
#   http_host   <orig, 0.0.0.0>  ->  127.0.0.1     (orig recorded in swap dir)
#   cache_dir backup_dir log_dir newsletter_dir exports_dir ...  every value that
#   starts with /config (the container mount) -> ~/.apps/tautulli
# http_port is left alone (the unit passes --port <secret tautulli.port>).
# Any config value naming /data or /downloads refuses the swap (unknown path).
#
# Exit: 0 ok | 1 refused/failed | 3 BLOCKED (venv/pip/boot failure or task
# delta: stay UCC, convert on box 2) | 10 rollback paused for the manifest
# revert | 64 usage.
#
# Overrides (tests; resolved at call time): QFLIX_APPS_DIR QFLIX_UNIT_DIR
# QFLIX_ENV_DIR QFLIX_SWAP_DIR MANITOBA_STATE_DIR QFLIX_MANIFEST QFLIX_PROC
# QFLIX_PYTHON QFLIX_APPCTL QFLIX_SYSTEMCTL QFLIX_SS QFLIX_PS QFLIX_CURL
# (download) QFLIX_HTTP_CURL (prove) QFLIX_PROBE_CURL (live probe)
# QFLIX_PLEX_CURL (Plex /identity from the host)
# QFLIX_HOSTPOLICY QFLIX_HOST_ID_FILE QFLIX_TAUTULLI_SHA256 QFLIX_SECRETS_DIR
# QFLIX_NGINX_FRAGMENT QFLIX_VENV_CMD QFLIX_POLL_S QFLIX_SETTLE_S
# QFLIX_STOP_TIMEOUT_S QFLIX_PROOF_TIMEOUT_S QFLIX_TT_DELTA_MAX QFLIX_KEEP_PROOF.
set -uo pipefail

SLUG=tautulli
VERSION="2.18.2"             # == versions.env TAUTULLI_VERSION (test-pinned)
SHA256="cde285c9954bcdd7680f5d9d268ccde2751c8c84e2146db25f0c83e5d3e8f293"
URL="https://github.com/Tautulli/Tautulli/archive/refs/tags/v${VERSION}.tar.gz"
UNIT="qflix-${SLUG}.service"
FAMILY=python
EXE="venv/bin/python"
# ${TAUTULLI_PORT} is expanded by systemd from the EnvironmentFile (the port is
# a per-slot secret and never lives in the tracked unit).
EXEC_ARGS='%h/.apps/tautulli/bin/current/Tautulli.py --datadir %h/.apps/tautulli --port ${TAUTULLI_PORT} --nolaunch --quiet --nofork'
# The one bind the native app takes (see LISTEN SET). Never the wildcard.
BIND_HOST="127.0.0.1"
# Muted with the app (plan row A10). Keys follow cli.py: canary-<name>.
SUPPRESS=("$SLUG" "canary-tautulli-plex-link")
# cmdline marker of the app (container: python3 /app/tautulli/Tautulli.py).
PATTERN="Tautulli.py"
# Container env worth carrying over (names only; boring values only).
ENV_KEYS=(TZ LANG)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # .../scripts
ARGS=("$@")

info() { echo "[302-tautulli] $*"; }
die()  { echo "[302-tautulli] ERROR: $*" >&2; exit 1; }
blocked() {
  echo "[302-tautulli] BLOCKED (D-7): $*" >&2
  echo "[302-tautulli] tautulli stays a UCC app on this slot; it converts on box 2." >&2
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
NGINX_FRAGMENT="${QFLIX_NGINX_FRAGMENT:-$APPS/nginx/proxy.d/tautulli.conf}"
MAINT_LIB="$HERE/maint/lib"
SANITIZE="$HERE/maint/native_sanitize.py"
PY="${QFLIX_PYTHON:-python3}"
APPCTL="${QFLIX_APPCTL:-$HOME/bin/appctl}"
SYSTEMCTL="${QFLIX_SYSTEMCTL:-systemctl}"
SS="${QFLIX_SS:-ss}"
PS="${QFLIX_PS:-ps}"
HTTP_CURL="${QFLIX_HTTP_CURL:-curl}"
PROBE_CURL="${QFLIX_PROBE_CURL:-curl}"
PLEX_CURL="${QFLIX_PLEX_CURL:-curl}"
PROC="${QFLIX_PROC:-/proc}"
POLL="${QFLIX_POLL_S:-2}"
SETTLE="${QFLIX_SETTLE_S:-10}"
STOP_TIMEOUT="${QFLIX_STOP_TIMEOUT_S:-120}"
PROOF_TIMEOUT="${QFLIX_PROOF_TIMEOUT_S:-180}"
DELTA_MAX="${QFLIX_TT_DELTA_MAX:-100}"
WANT_SHA="${QFLIX_TAUTULLI_SHA256:-$SHA256}"

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
    precheck) info "would fetch $URL (sha256 $WANT_SHA) into a scratch dir, check the box python can build a venv, then delete the scratch dir" ;;
    install)  info "would check version parity, build a venv from the release requirements, lay out $APPDIR/bin/$VERSION, write $ENV_DIR/$SLUG.env (TAUTULLI_PORT), stage $APPDIR/native/$UNIT (not enabled)" ;;
    prove)    info "would VACUUM INTO + copy the data to $PROVE/data, sanitize it (counts must be 0), boot the native build on a fresh $BIND_HOST port, require get_tautulli_info = v$VERSION, gate the task count at 70% of the ceiling, then delete the copy" ;;
    swap)     info "would require the nginx fragment to reach $BIND_HOST, capture the listen set (record the dropped listeners as exceptions), path-audit the config, suppress ${SUPPRESS[*]}, stop the container, wait for exit, snapshot config + db, bind $BIND_HOST, enable --now $UNIT, verify, record swap state" ;;
    finish)   info "would verify the native unit and lift suppression for ${SUPPRESS[*]}" ;;
    rollback) info "would suppress ${SUPPRESS[*]}, park + mask $UNIT, stop it, wait for the manifest revert, undo the config edits, start the container via appctl, unsuppress" ;;
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
  sshm "~/scripts/configure/302-native-tautulli-install.sh $(printf '%q ' "${ARGS[@]}")"
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
  sleep 0.3
  local p
  for p in "${CLEANUP_PATHS[@]}"; do rm -rf "$p"; done
}
trap cleanup EXIT

TAR=(tar --force-local)

# --- helpers -------------------------------------------------------------------------
# The listen port is the recorded one (secret tautulli.port == the nginx
# upstream). Unknown fails closed.
tt_port() {
  local p
  p="$(tr -d '[:space:]' < "$SECRETS/tautulli.port" 2>/dev/null)"
  [[ "$p" =~ ^[0-9]{2,5}$ ]] || return 1
  printf '%s' "$p"
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

# One tool for every config.ini edit/read (a [General]-style ini, `key = value`).
#   cfgtool get FILE KEY                  value, quotes stripped (empty if absent)
#   cfgtool set FILE KEY VALUE            replace, or insert under [General]
#   cfgtool paths FILE FROM TO            rewrite values that start with FROM
#                                         (on a path boundary) to TO; prints keys
#   cfgtool audit FILE                    exit 1 + keys if any value is under
#                                         /data or /downloads (unknown container path)
cfgtool() {
  # MSYS_NO_PATHCONV: a no-op on the box; keeps Git Bash (the test host) from
  # rewriting the "/config" argument into a Windows path.
  MSYS_NO_PATHCONV=1 "$PY" - "$@" <<'PY' | tr -d '\r'
import re, sys
mode, path = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8", newline="") as fh:
    text = fh.read()
def line_re(key):
    return re.compile(r"^(%s[ \t]*=[ \t]*)(.*?)([ \t]*\r?)$" % re.escape(key), re.M)
if mode == "get":
    m = line_re(sys.argv[3]).search(text)
    print((m.group(2).strip('"') if m else ""))
elif mode == "set":
    key, val = sys.argv[3], sys.argv[4]
    r = line_re(key)
    if r.search(text):
        text = r.sub(lambda m: m.group(1) + val + ("\r" if m.group(3).endswith("\r") else ""),
                     text, count=1)
    elif re.search(r"^\[General\][ \t]*$", text, re.M):
        text = re.sub(r"^(\[General\][ \t]*)$", lambda m: m.group(1) + "\n%s = %s" % (key, val),
                      text, count=1, flags=re.M)
    else:
        text = "[General]\n%s = %s\n" % (key, val) + text
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
elif mode == "paths":
    src, dst = sys.argv[3].rstrip("/"), sys.argv[4].rstrip("/")
    rx = re.compile(r'^([A-Za-z0-9_]+[ \t]*=[ \t]*"?)' + re.escape(src) + r'(?=["/ \t\r]|$)', re.M)
    keys = [m.group(1).split("=")[0].strip() for m in rx.finditer(text)]
    if keys:
        text = rx.sub(lambda m: m.group(1) + dst, text)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    print(" ".join(keys))
elif mode == "audit":
    bad = []
    for m in re.finditer(r'^([A-Za-z0-9_]+)[ \t]*=[ \t]*"?(/(?:data|downloads)(?:/[^"\r\n]*)?)"?[ \t]*\r?$', text, re.M):
        bad.append(m.group(1))
    print(" ".join(bad))
    sys.exit(1 if bad else 0)
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
  local port; port="$(tt_port)" || return 1
  ! "$SS" -tlnH "sport = :$port" 2>/dev/null | grep -q ":$port\b"
}

# Does any process of ours still hold the db (or its -wal) open? Spec 5.9 step 6
# (fuser on the db and -wal). /proc/<pid>/fd links are resolved in the OPENER's
# mount namespace: a container sees /config/..., a host process the physical path
# ($HOME is a symlink on the slot, so compare against pwd -P as well).
db_unused() {
  local real l t
  real="$(cd "$APPDIR" 2>/dev/null && pwd -P)" || real="$APPDIR"
  for l in "$PROC"/[0-9]*/fd/*; do
    t="$(readlink "$l" 2>/dev/null)" || continue
    case "$t" in
      "$APPDIR/tautulli.db"|"$APPDIR/tautulli.db-wal"|"$real/tautulli.db"|"$real/tautulli.db-wal"|/config/tautulli.db|/config/tautulli.db-wal)
        return 1 ;;
    esac
  done
  return 0
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
container_gone() { [ -z "$(scan_pids container)" ] && port_free && db_unused; }
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

# GET get_tautulli_info on $1:$2 under http_root $3 with api key $4 (the key goes
# in via curl's stdin config, never on a command line another user can read).
# Prints the reported tautulli_version; non-zero when the API did not succeed.
api_version() {
  local host="$1" port="$2" root="$3" key="$4" curl="$5" body
  body="$(printf 'url = "http://%s:%s%s/api/v2?cmd=get_tautulli_info&apikey=%s"\n' "$host" "$port" "${root%/}" "$key" \
          | "$curl" -s -m 8 -K - 2>/dev/null)" || return 1
  printf '%s' "$body" | "$PY" -c '
import json, sys
r = json.load(sys.stdin)["response"]
if r.get("result") != "success":
    sys.exit(1)
print(r["data"]["tautulli_version"])' | tr -d '\r'
}

# Ready probe against the LIVE bind (the container's loopback listener before the
# swap and after a rollback; the native bind after the swap): the API answers
# and reports v<VERSION>.
probe_ready() {
  local port key root got
  port="$(tt_port)" || return 1
  key="$(cfgtool get "$APPDIR/config.ini" api_key)"; [ -n "$key" ] || return 1
  root="$(cfgtool get "$APPDIR/config.ini" http_root)"
  got="$(api_version "$BIND_HOST" "$port" "$root" "$key" "$PROBE_CURL")" || return 1
  [ "$got" = "v$VERSION" ]
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

# Build the venv: `$1` = destination, `$2` = requirements.txt. QFLIX_VENV_CMD
# replaces the real build in tests.
venv_build() {
  if [ -n "${QFLIX_VENV_CMD:-}" ]; then "$QFLIX_VENV_CMD" "$1" "$2"; return $?; fi
  "$PY" -m venv "$1" || return 1
  "$1/bin/python" -m pip install --disable-pip-version-check --no-cache-dir -q -r "$2"
}

fetch_extract() {
  local stage="$1"
  native_fetch_verify "$URL" "$WANT_SHA" "$stage/t.tgz" || die "fetch/sha256 verify failed"
  mkdir -p "$stage/src"
  # A GitHub tag archive wraps everything in Tautulli-<ver>/.
  "${TAR[@]}" -xzf "$stage/t.tgz" -C "$stage/src" --strip-components=1 || die "cannot extract the release"
  rm -f "$stage/t.tgz"
  [ -f "$stage/src/Tautulli.py" ] && [ -f "$stage/src/requirements.txt" ] \
    || die "release has no Tautulli.py / requirements.txt"
}

py_probe() {
  "$PY" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null \
    || blocked "the box python3 cannot build a venv (needs >= 3.9 with venv + ensurepip)"
}

# --- modes ----------------------------------------------------------------------------
do_precheck() {
  local stage
  mkdir -p "$APPS" || die "cannot create $APPS"
  stage="$(mktemp -d "$APPS/.stage-$SLUG.XXXXXX")" || die "mktemp failed"
  CLEANUP_PATHS+=("$stage")
  py_probe
  fetch_extract "$stage"
  info "PRECHECK OK (nothing installed)"
}

do_install() {
  local stage port
  port="$(tt_port)" || die "secret tautulli.port missing/invalid; refusing"
  # I-10: refuse before the (slow) venv build when the versions differ.
  native_check_parity "$SLUG" "$VERSION" || die "version parity failed (see above)"
  mkdir -p "$APPS" || die "cannot create $APPS"
  stage="$(mktemp -d "$APPS/.stage-$SLUG.XXXXXX")" || die "mktemp failed"
  CLEANUP_PATHS+=("$stage")
  py_probe
  fetch_extract "$stage"
  venv_build "$stage/src/venv" "$stage/src/requirements.txt" \
    || blocked "venv / pip install of the pinned requirements failed"
  "$stage/src/venv/bin/python" -c 'import cherrypy, plexapi, mako, apscheduler, requests' \
    || blocked "the venv cannot import the Tautulli dependencies"
  native_install_versioned "$SLUG" "$VERSION" "$stage/src" || die "install refused (see above)"
  local -a extra=("TAUTULLI_PORT=$port")
  local kv
  while IFS= read -r kv; do
    [ -n "$kv" ] && extra+=("$kv")
  done < <(container_env)
  native_render_env "$SLUG" "$FAMILY" "$VERSION" "${extra[@]}" \
    | native_write_secure "$ENV_DIR/$SLUG.env" 0600 || die "env file write failed"
  native_render_unit "$SLUG" "$FAMILY" "$EXE" "$EXEC_ARGS" | sed 's/[ \t]*$//' \
    | native_write_secure "$APPDIR/native/$UNIT" 0644 || die "unit staging failed"
  info "installed $VERSION; unit staged at $APPDIR/native/$UNIT (not enabled). Next: --prove --execute"
}

PMS_KEYS=(pms_ip pms_port pms_ssl pms_url pms_url_manual)

do_prove() {
  local before ceiling delta peak sample proofport key root got rc start cfg counts d k
  [ -x "$APPDIR/bin/current/venv/bin/python" ] && [ -f "$APPDIR/bin/current/Tautulli.py" ] \
    || die "not installed; run --install --execute first"
  [ -f "$APPDIR/tautulli.db" ] && [ -f "$APPDIR/config.ini" ] || die "no live tautulli.db / config.ini in $APPDIR"
  ceiling="$(hostpolicy task-ceiling)" || die "task ceiling unknown; refusing (G-2)"
  ceiling="${ceiling%$'\r'}"
  rm -rf "$PROVE"
  ( umask 077; mkdir -p "$PROVE/data" ) || die "cannot create $PROVE"
  [ "${QFLIX_KEEP_PROOF:-0}" = 1 ] || CLEANUP_PATHS+=("$PROVE")
  cfg="$PROVE/data/config.ini"
  # 5.9 step 2.1: VACUUM INTO a consistent copy (the source is opened read-only;
  # the live db and its -wal are never written), plus a copy of the config.
  "$PY" - "$APPDIR/tautulli.db" "$PROVE/data/tautulli.db" <<'PY' || die "cannot copy the database (VACUUM INTO failed)"
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1].replace("\\", "/"), uri=True)
src.execute("VACUUM INTO ?", (sys.argv[2],))
src.close()
PY
  cp "$APPDIR/config.ini" "$cfg" && chmod 0600 "$cfg" "$PROVE/data/tautulli.db" || die "cannot copy config.ini"
  # 5.9 step 2.2: sanitize, assert the zero counts (re-read from disk).
  counts="$("$PY" "$SANITIZE" "$SLUG" "$PROVE/data" 2>"$PROVE/sanitize.err" | tr -d '\r')" \
    || die "sanitize refused: $(head -c 300 "$PROVE/sanitize.err")"
  info "sanitized proof copy: $counts"
  # Make the copy self-contained and loopback-only: container paths -> the proof
  # dir, no startup refresh against plex.tv/Plex. pms_* stay EXACTLY as live.
  cfgtool paths "$cfg" /config "$PROVE/data" >/dev/null
  cfgtool set "$cfg" http_host "$BIND_HOST"
  cfgtool set "$cfg" refresh_users_on_startup 0
  cfgtool set "$cfg" refresh_libraries_on_startup 0
  for k in "${PMS_KEYS[@]}"; do
    [ "$(cfgtool get "$cfg" "$k")" = "$(cfgtool get "$APPDIR/config.ini" "$k")" ] \
      || die "proof copy changed $k; pms_* must stay pointed at the same Plex"
  done
  # The Plex Tautulli is configured for must answer from the host network
  # namespace (same probe as the tautulli-plex-link canary).
  local scheme=http
  [ "$(cfgtool get "$cfg" pms_ssl)" = 1 ] && scheme=https
  "$PLEX_CURL" -sk -m 10 "$scheme://$(cfgtool get "$cfg" pms_ip):$(cfgtool get "$cfg" pms_port)/identity" 2>/dev/null \
    | grep -q MediaContainer || die "the configured Plex (pms_ip:pms_port) does not answer /identity from the host; refusing"
  key="$(cfgtool get "$cfg" api_key)"; [ -n "$key" ] || die "config.ini has no api_key"
  root="$(cfgtool get "$cfg" http_root)"
  proofport="$("$PY" -c 'import socket
s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])' | tr -d '\r')"
  [[ "$proofport" =~ ^[0-9]+$ ]] || die "cannot pick a free loopback port"
  before="$(user_tasks)"
  [[ "$before" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  ( cd "$PROVE" && exec env -u DISPLAY -u XAUTHORITY HOME="$HOME" PATH="$(unit_path)" \
      MALLOC_ARENA_MAX=2 QFLIX_PROC="$PROC" QFLIX_PYTHON="$PY" \
      "$APPDIR/bin/current/venv/bin/python" "$APPDIR/bin/current/Tautulli.py" \
      --datadir "$PROVE/data" --port "$proofport" --nolaunch --quiet --nofork \
      >"$PROVE/stdout.log" 2>&1 ) &
  PROOF_PID=$!
  start=$SECONDS
  ready() { got="$(api_version 127.0.0.1 "$proofport" "$root" "$key" "$HTTP_CURL")"; [ -n "$got" ]; }
  if ! wait_until "$PROOF_TIMEOUT" ready; then
    tail -5 "$PROVE/stdout.log" >&2
    blocked "the native build never answered get_tautulli_info on 127.0.0.1:$proofport within ${PROOF_TIMEOUT}s"
  fi
  [ "$got" = "v$VERSION" ] || die "proof build reports $got, expected v$VERSION"
  sleep "$SETTLE"                                     # let the schedulers / websocket threads start
  peak="$(tree_tasks "$PROOF_PID")"
  sample="$(tree_tasks "$PROOF_PID")"; [ "${sample:-0}" -gt "${peak:-0}" ] && peak="$sample"
  delta="$peak"
  kill "$PROOF_PID" 2>/dev/null; wait "$PROOF_PID" 2>/dev/null; PROOF_PID=""
  [[ "$delta" =~ ^[0-9]+$ ]] && [ "$delta" -gt 0 ] || die "proof: cannot read the task count; refusing"
  if [ "$delta" -gt "$DELTA_MAX" ]; then
    blocked "task delta $delta exceeds $DELTA_MAX (before=$before ceiling=$ceiling)"
  fi
  if [ $(( (before + delta) * 100 )) -ge $(( 70 * ceiling )) ]; then
    die "thread gate: $before + $delta tasks reaches 70% of the ceiling $ceiling; refusing the swap"
  fi
  mkdir -p "$SWAPDIR"
  printf '{"ok": true, "version": "%s", "before": %s, "delta": %s, "ceiling": %s, "sanitize": %s, "at": "%s"}\n' \
    "$VERSION" "$before" "$delta" "$ceiling" "$counts" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    | native_write_secure "$SWAPDIR/proof.json" 0644 || die "cannot record the proof"
  info "PROOF OK: v$VERSION answered on a sanitized copy; before=$before delta=$delta ceiling=$ceiling elapsed=$((SECONDS - start))s. Next: --swap --execute"
}

verify_native() {
  local n host
  sleep "$SETTLE"
  sysd is-active "$UNIT" >/dev/null 2>&1 || { echo "unit not active" >&2; return 1; }
  [ -z "$(scan_pids container)" ] || { echo "a container process is running" >&2; return 1; }
  n="$(scan_pids native | wc -l | tr -d ' ')"
  [ "$n" -ge 1 ] || { echo "no native process" >&2; return 1; }
  host="$(cfgtool get "$APPDIR/config.ini" http_host)"
  [ "$host" = "$BIND_HOST" ] || { echo "http_host is '$host', not $BIND_HOST" >&2; return 1; }
  native_listen_compare "$SLUG" >/dev/null || { echo "listen set differs from listen-set.before (minus exceptions)" >&2; return 1; }
  wait_until 60 probe_ready || { echo "get_tautulli_info on $BIND_HOST did not report v$VERSION" >&2; return 1; }
}

# The nginx fragment is the ONLY ingress: it must proxy to the loopback bind.
ingress_is_nginx_only() {
  local port="$1"
  [ -f "$NGINX_FRAGMENT" ] && grep -Eq "proxy_pass[[:space:]]+http://127\.0\.0\.1:${port}\b" "$NGINX_FRAGMENT"
}

# Container -> native config edits (see CONFIG EDITS). Records the original
# http_host first so --rollback restores it exactly.
apply_config_edits() {
  local cfg="$APPDIR/config.ini" orig keys
  orig="$(cfgtool get "$cfg" http_host)"
  [ -n "$orig" ] || orig="0.0.0.0"
  [ -f "$SWAPDIR/orig-http-host" ] || printf '%s\n' "$orig" | native_write_secure "$SWAPDIR/orig-http-host" 0644 || return 1
  keys="$(cfgtool paths "$cfg" /config "$APPDIR")" || return 1
  cfgtool set "$cfg" http_host "$BIND_HOST" || return 1
  info "config edits: http_host -> $BIND_HOST; container paths rewritten for: ${keys:-(none)}"
  [ "$(cfgtool get "$cfg" http_host)" = "$BIND_HOST" ] || return 1
}

# Native -> container (rollback step 3). Only ever reverses what apply did.
undo_config_edits() {
  local cfg="$APPDIR/config.ini" orig keys
  [ -f "$cfg" ] || return 0
  keys="$(cfgtool paths "$cfg" "$APPDIR" /config)" || return 1
  if [ -f "$SWAPDIR/orig-http-host" ]; then
    orig="$(tr -d '[:space:]' < "$SWAPDIR/orig-http-host")"
    [ -n "$orig" ] || orig="0.0.0.0"
    cfgtool set "$cfg" http_host "$orig" || return 1
    [ "$(cfgtool get "$cfg" http_host)" = "$orig" ] || return 1
    info "config restored for the container: http_host -> $orig; paths restored for: ${keys:-(none)}"
  fi
}

snapshot_data() {
  local f
  mkdir -p "$SWAPDIR/snapshot" && chmod 0700 "$SWAPDIR/snapshot" || return 1
  for f in config.ini tautulli.db tautulli.db-wal tautulli.db-shm; do
    [ -f "$APPDIR/$f" ] || continue
    cp -p "$APPDIR/$f" "$SWAPDIR/snapshot/$f" || return 1
    cmp -s "$APPDIR/$f" "$SWAPDIR/snapshot/$f" || return 1
  done
}

do_swap() {
  local st cls ss_state ver port f cfg before others a
  [ -f "$SWAPDIR/proof.json" ] || die "no proof recorded; run --prove --execute first"
  [ -x "$APPDIR/bin/current/venv/bin/python" ] && [ -f "$APPDIR/native/$UNIT" ] && [ -f "$ENV_DIR/$SLUG.env" ] \
    || die "not installed; run --install --execute first"
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state _ <<<"$st"
  [ "$cls" = systemd ] && [ "$ss_state" = pending-swap ] \
    || die "the deployed manifest is not the pending-swap flip (class=$cls swap_state=$ss_state); merge + deploy it via 240 first"
  port="$(tt_port)" || die "secret tautulli.port missing/invalid; refusing"
  grep -qx "TAUTULLI_PORT=$port" "$ENV_DIR/$SLUG.env" \
    || die "$ENV_DIR/$SLUG.env does not carry TAUTULLI_PORT=$port; re-run --install --execute"
  cfg="$APPDIR/config.ini"

  if sysd is-active "$UNIT" >/dev/null 2>&1 && [ -z "$(scan_pids container)" ]; then
    info "already swapped; verifying only"
    verify_native || die "native tautulli fails parity; run --rollback --execute"
    info "verified"
    return 0
  fi

  if is_masked; then
    sysd unmask "$UNIT" || die "unmask $UNIT failed"
    rm -f "$ENV_DIR/parked-units/$UNIT"
  fi

  ver="$(native_ucc_version "$SLUG")"
  [ "${ver#v}" = "$VERSION" ] || die "version parity: container=$ver native=$VERSION"

  # D-4 precondition: all ingress is nginx -> the loopback bind we keep.
  ingress_is_nginx_only "$port" \
    || die "nginx fragment $NGINX_FRAGMENT does not proxy to 127.0.0.1:$port; the dropped public/gateway listeners may still be in use (D-4); refusing"

  # Step 3: capture + path audit.
  native_listen_capture "$SLUG" "$port" >/dev/null || die "listen-set capture failed"
  before="$(cat "$SWAPDIR/listen-set.before" 2>/dev/null)"
  printf '%s\n' "$before" | grep -qx "$BIND_HOST:$port" \
    || die "the container does not listen on $BIND_HOST:$port ($(echo "$before" | tr '\n' ' ')); nothing to reproduce; refusing"
  others=()
  while IFS= read -r a; do
    [ -n "$a" ] && [ "$a" != "$BIND_HOST:$port" ] || continue
    case "$a" in
      0.0.0.0:*|"[::]:"*|"*:"*) die "container listener $a is a wildcard; it cannot be reasoned about as a dropped address; refusing" ;;
    esac
    others+=("$a")
  done <<<"$before"
  if [ "${#others[@]}" -gt 0 ]; then
    swapstate add-exception "$SLUG" "${others[@]}" >/dev/null || die "cannot record the listen-set exceptions"
    info "listen-set exceptions recorded (D-4, nginx-only ingress): ${others[*]}"
  fi
  cfgtool audit "$cfg" >/dev/null || die "config.ini names container paths I cannot map (/data or /downloads): $(cfgtool audit "$cfg")"
  swapstate set "$SLUG" "ucc_version=$VERSION" >/dev/null || die "cannot record ucc_version"

  suppression add "${SUPPRESS[@]}" --reason "QFLX-34 swap to native" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to swap unsuppressed"

  "$APPCTL" stop "$SLUG" >/dev/null 2>&1 || info "appctl stop returned non-zero; polling decides"
  if ! wait_until "$STOP_TIMEOUT" container_gone; then
    "$APPCTL" start "$SLUG" >/dev/null 2>&1 || true
    suppression remove "${SUPPRESS[@]}" >/dev/null || true
    die "the container did not exit within ${STOP_TIMEOUT}s (pids: $(scan_pids container | tr '\n' ' '); port free: $(port_free && echo yes || echo no); db unused: $(db_unused && echo yes || echo no)); swap aborted, container start requested, suppression lifted"
  fi

  # Step 5: snapshot, then the config edits. A failure here restores the config
  # and brings the container back (nothing native has run yet).
  if ! snapshot_data; then
    "$APPCTL" start "$SLUG" >/dev/null 2>&1 || true
    suppression remove "${SUPPRESS[@]}" >/dev/null || true
    die "snapshot of config + db failed; swap aborted, container start requested, suppression lifted"
  fi
  if ! apply_config_edits; then
    cp -p "$SWAPDIR/snapshot/config.ini" "$cfg" 2>/dev/null
    "$APPCTL" start "$SLUG" >/dev/null 2>&1 || true
    suppression remove "${SUPPRESS[@]}" >/dev/null || true
    die "config edits failed; config.ini restored from the snapshot, container start requested, suppression lifted"
  fi

  mkdir -p "$UNIT_DIR"
  native_write_secure "$UNIT_DIR/$UNIT" 0644 < "$APPDIR/native/$UNIT" || die "unit install failed"
  sysd daemon-reload || die "daemon-reload failed"
  sysd enable --now "$UNIT" || die "enable --now $UNIT failed; run --rollback --execute"

  if ! verify_native; then
    die "native tautulli fails parity after the swap; suppression kept ON; run --rollback --execute"
  fi
  local now soak
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  soak="$(date -u -d '+14 days' +%Y-%m-%dT%H:%M:%SZ)" || die "cannot compute soak_until"
  swapstate set "$SLUG" "swap_date=$now" "soak_until=$soak" "rollback_window=open" >/dev/null \
    || die "cannot record swap state"
  info "SWAPPED to native $VERSION on $BIND_HOST:$port; soak until $soak. elapsed=$((SECONDS - T0))s"
  info "Next: tautulli-plex-link canary + pms_url check, PR dropping swap_state: pending-swap, deploy via 240, then --finish --execute"
}

do_finish() {
  local st cls ss_state dormant
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state dormant <<<"$st"
  [ "$cls" = systemd ] && [ -z "$ss_state" ] && [ "$dormant" = 1 ] \
    || die "deployed manifest still says class=$cls swap_state=${ss_state:-none} dormant=$dormant; deploy the follow-up (no pending-swap) first"
  verify_native || die "native tautulli fails parity; run --rollback --execute"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "FINISHED: ${SUPPRESS[*]} unsuppressed; 14-day soak running"
}

do_rollback() {
  local isn
  suppression add "${SUPPRESS[@]}" --reason "QFLX-34 rollback to UCC" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to roll back unsuppressed"
  mask_unit
  sysd stop "$UNIT" >/dev/null 2>&1 || true
  # Same port as the container: it must be free before the container can start.
  wait_until "$STOP_TIMEOUT" native_gone || die "native tautulli did not stop (or the port stayed bound) within ${STOP_TIMEOUT}s"
  isn="$("$APPCTL" is-native "$SLUG" 2>/dev/null)"
  if [ "${isn%$'\r'}" != ucc ]; then
    echo "[302-tautulli] PAUSED: native stopped + masked; revert the deployed manifest (PR + 240) so appctl dispatches $SLUG as UCC, then re-run --rollback --execute" >&2
    exit 10
  fi
  undo_config_edits || die "could not restore the container-side config.ini (snapshot: $SWAPDIR/snapshot); still suppressed"
  "$APPCTL" start "$SLUG" >/dev/null 2>&1 || info "appctl start returned non-zero; polling decides"
  wait_until "$STOP_TIMEOUT" container_up || die "the container did not come back within ${STOP_TIMEOUT}s; still suppressed"
  wait_until 60 probe_ready || die "container is up but get_tautulli_info on $BIND_HOST is not ready; still suppressed"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "ROLLED BACK to UCC; $UNIT masked (unmasked by the next --swap). elapsed=$((SECONDS - T0))s"
}

case "$MODE" in
  precheck) do_precheck ;;
  install)  do_install ;;
  prove)    do_prove ;;
  swap)     do_swap ;;
  finish)   do_finish ;;
  rollback) do_rollback ;;
esac
