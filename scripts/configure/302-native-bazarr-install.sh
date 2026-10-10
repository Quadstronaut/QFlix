#!/usr/bin/env bash
# 302-native-bazarr-install.sh -- QFLX-27 (UCC divorce A3, convert bazarr).
#
# Moves bazarr-1 off the Ultra.cc container manager (UCC) onto a user unit the
# repo owns: the upstream release zip at EXACTLY the container's version,
# sha256-pinned, run by a python3.11 venv as qflix-bazarr.service. Same recipe as
# scripts/install/06-bazarr2.sh (venv + waitress thread patch), but from the
# release zip (it carries the prebuilt web UI; a git tag checkout does not).
# Spec: docs/superpowers/specs/2026-10-09-ucc-divorce-design.md 5.1-5.9, row 3
# of 6. Pilot shape: 300-native-unpackerr-install.sh.
#
# RUNS ON THE BOX. 240-maintenance-install.sh deploys it to ~/scripts/configure/
# with ~/scripts/lib/native.sh beside it and ~/scripts/maint/native_sanitize.py.
# Started from the workstation it re-executes the deployed copy over ssh.
#
# INERT BY DEFAULT (I-3). Every mode prints its plan (DRY-RUN) and touches
# nothing unless --execute is also given. Modes, in swap order:
#
#   --install   5.9 step 1. Fetch + sha256-verify bazarr.zip, refuse unless the
#               version equals `appctl version bazarr` (I-10), lay out
#               ~/.apps/bazarr/bin/<ver> + `current`, patch server.py (waitress
#               threads=100 -> 4, and bind to BAZARR_LISTEN: see below), build
#               ~/.apps/bazarr/venv (python3.11) + pip install -r, write the env
#               file (BAZARR_VERSION via the render_env hook) and STAGE the unit
#               in ~/.apps/bazarr/native/. NOT copied into the unit dir, NOT
#               enabled (WantedBy=default.target would start it beside the live
#               container on the next user-manager start, I-6).
#   --prove     5.9 step 2. VACUUM INTO copy of bazarr.db + config.yaml under
#               ~/.apps/.prove/bazarr, container paths and plex integration
#               rewritten off, native_sanitize (providers [], arr sync off,
#               notifications deleted) with ZERO counts asserted, booted on a
#               free 127.0.0.1 port: /api/system/status must be 200 with
#               bazarr_version == the pin; the user's task delta is measured and
#               the proof REFUSED when current + delta reaches 70% of the host
#               task ceiling (G-2). The copy is destroyed. Writes
#               swap/bazarr/proof.json.
#   --swap      5.9 steps 3-6 + 8: needs the proof and the DEPLOYED pending-swap
#               manifest flip. Unmask (rollback step 4), capture the listen set
#               (bazarr listens on THREE addresses, F-17: all are reproduced
#               through BAZARR_LISTEN; a wildcard is refused), path-audit
#               config.yaml (the only container path allowed is backup.folder,
#               which is rewritten), suppress the app + its canaries, stop the
#               container through appctl, POLL until no container process is
#               left, the port is free and nothing holds bazarr.db / -wal, then
#               snapshot the (now quiescent) data, rewrite backup.folder,
#               install + enable --now the unit, verify parity (unit active,
#               listen set equal, status 200, version equal), record swap state.
#               Suppression stays ON: the manifest still says pending-swap.
#   --finish    5.9 step 9, after the follow-up PR dropped `swap_state` and 240
#               deployed it: verify, then lift the app + canaries together.
#   --rollback  Rollback 0-5. 0: re-suppress, park the unit file and MASK it. 1:
#               stop it and wait for exit. config: put backup.folder back to the
#               container path (the container cannot write the host path). 2:
#               the DEPLOYED manifest must dispatch bazarr as UCC again (revert
#               PR + 240); otherwise exit 10 and re-run after the revert. 3:
#               start the container through appctl. 5: no snapshot restore
#               (versions equal, no migration). 4 (unmask) happens at the next
#               --swap.
#   --post-upgrade VER   the zip_swap post step (manifest upgrade.post_steps).
#               Runs INSIDE the Monday upgrade sweep, so it skips the window
#               gate. Patches bin/VER, pip-installs its requirements into the
#               venv, flips `current`, and rewrites BAZARR_VERSION in the env
#               file (bazarr reports its version from that variable; bazarr2-sync
#               reads bazarr-1's version from /api/system/status and pins bazarr2
#               to it). lifecycle restarts the unit afterwards.
#
# BAZARR_LISTEN: Server.configure_server in bazarr passes ONE host to waitress.
# The patch makes it pass `listen="a:p b:p c:p"` from this env var, and RAISES
# when the variable is unset (a stray start must never bind 0.0.0.0).
# --swap appends BAZARR_LISTEN to the env file from listen-set.before.
#
# Exit: 0 ok | 1 refused/failed | 10 rollback paused for the manifest revert |
# 64 usage.
#
# Overrides (tests; resolved at call time): QFLIX_APPS_DIR QFLIX_UNIT_DIR
# QFLIX_ENV_DIR QFLIX_SWAP_DIR QFLIX_SECRETS_DIR MANITOBA_STATE_DIR QFLIX_MANIFEST
# QFLIX_PROC QFLIX_PYTHON QFLIX_PY311 QFLIX_APPCTL QFLIX_SYSTEMCTL QFLIX_SS
# QFLIX_PS QFLIX_FUSER QFLIX_CURL QFLIX_HOSTPOLICY QFLIX_HOST_ID_FILE
# QFLIX_BAZARR_SHA256 QFLIX_BAZARR_PORT QFLIX_POLL_S QFLIX_SETTLE_S
# QFLIX_STOP_TIMEOUT_S QFLIX_PROOF_TIMEOUT_S QFLIX_STATUS_TIMEOUT_S
# QFLIX_KEEP_PROOF.
set -uo pipefail

SLUG=bazarr
VERSION="1.6.2"              # == versions.env BAZARR_VERSION (test-pinned)
SHA256="82d1c61ea8508b28503d820bd5924360cc503467e7de51fa29205cac34c2627b"
URL="https://github.com/morpheus65535/bazarr/releases/download/v${VERSION}/bazarr.zip"
UNIT="qflix-${SLUG}.service"
FAMILY=python
# The interpreter lives OUTSIDE bin/current (native_render_unit takes %h/...
# verbatim). --config is the SAME dir the container mounted at /config, used in
# place: <dir>/config/config.yaml, <dir>/db/bazarr.db, <dir>/log.
EXE="%h/.apps/${SLUG}/venv/bin/python"
EXEC_ARGS="%h/.apps/${SLUG}/bin/current/bazarr.py --no-update --config %h/.apps/${SLUG}"
# The container ran with TZ=Europe/Amsterdam (its /proc/<pid>/environ, box
# 2026-10-10). Keeping it keeps the log timestamps vlogs ingests unshifted
# (QFLX-44 class).
TZ_ENV="TZ=Europe/Amsterdam"
# Muted with the app (plan A-table row A3). Keys follow cli.py: canary-<name>.
# bazarr2-sync reads bazarr-1's version over its API every hour: unreachable =
# a non-zero oneshot, so it is muted for the swap too.
SUPPRESS=("$SLUG" "canary-bazarr-ingest" "canary-thread-ceiling" "bazarr2-sync")
PATTERN="bazarr"
PY311_DEFAULT="$HOME/.local/python311/bin/python3.11"   # the bazarr2 interpreter

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # .../scripts
ARGS=("$@")

info() { echo "[302-bazarr] $*"; }
die()  { echo "[302-bazarr] ERROR: $*" >&2; exit 1; }
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
PROVE="$APPS/.prove/$SLUG"
MANIFEST="${QFLIX_MANIFEST:-$HOME/.opt/maint/apps.yaml}"
MAINT_LIB="$HERE/maint/lib"
SANITIZE="$HERE/maint/native_sanitize.py"
PY="${QFLIX_PYTHON:-python3}"
PY311="${QFLIX_PY311:-$PY311_DEFAULT}"
APPCTL="${QFLIX_APPCTL:-$HOME/bin/appctl}"
SYSTEMCTL="${QFLIX_SYSTEMCTL:-systemctl}"
SS="${QFLIX_SS:-ss}"
PS="${QFLIX_PS:-ps}"
FUSER="${QFLIX_FUSER:-fuser}"
PROC="${QFLIX_PROC:-/proc}"
POLL="${QFLIX_POLL_S:-2}"
SETTLE="${QFLIX_SETTLE_S:-10}"
STOP_TIMEOUT="${QFLIX_STOP_TIMEOUT_S:-120}"
PROOF_TIMEOUT="${QFLIX_PROOF_TIMEOUT_S:-300}"
STATUS_TIMEOUT="${QFLIX_STATUS_TIMEOUT_S:-120}"
WANT_SHA="${QFLIX_BAZARR_SHA256:-$SHA256}"
# The container's published port (manifest health.port_secret bazarr.port).
PROBE_PORT="${QFLIX_BAZARR_PORT:-17031}"
DB="$APPDIR/db/bazarr.db"
CFG="$APPDIR/config/config.yaml"

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
    install)  info "would fetch $URL (sha256 $WANT_SHA), check version parity, lay out $APPDIR/bin/$VERSION, patch server.py, build $APPDIR/venv, write $ENV_FILE (BAZARR_VERSION=$VERSION), stage $APPDIR/native/$UNIT (not enabled)" ;;
    prove)    info "would VACUUM INTO a copy of $DB under $PROVE, sanitize it (zero counts), boot it on a free 127.0.0.1 port, check /api/system/status == $VERSION, gate the task delta at 70% of the ceiling, then delete the copy" ;;
    swap)     info "would capture the listen set (port $PROBE_PORT), path-audit $CFG, suppress ${SUPPRESS[*]}, stop the container, wait for exit + free port + idle db, snapshot, rewrite backup.folder, enable --now $UNIT with BAZARR_LISTEN, verify, record swap state" ;;
    finish)   info "would verify the native unit and lift suppression for ${SUPPRESS[*]}" ;;
    rollback) info "would suppress ${SUPPRESS[*]}, park + mask $UNIT, stop it, restore backup.folder, wait for the manifest revert, start the container via appctl, unsuppress" ;;
    post-upgrade) info "would patch $APPDIR/bin/$UPGRADE_VER, pip install its requirements into $APPDIR/venv, flip bin/current, set BAZARR_VERSION=$UPGRADE_VER in $ENV_FILE" ;;
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
  sshm "~/scripts/configure/302-native-bazarr-install.sh $(printf '%q ' "${ARGS[@]}")"
  exit $?
fi

# shellcheck source=/dev/null
source "$HERE/lib/native.sh" || die "cannot source $HERE/lib/native.sh (run 240 first)"

# post-upgrade runs inside the Monday upgrade sweep: the window gate would
# refuse exactly when it must run, and it touches no UCC container.
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

# One EXIT trap for every temp path / proof process.
CLEANUP_PATHS=()
PROOF_PID=""
cleanup() {
  [ -n "$PROOF_PID" ] && kill "$PROOF_PID" 2>/dev/null
  local p
  for p in "${CLEANUP_PATHS[@]}"; do rm -rf "$p"; done
}
trap cleanup EXIT

# --- python helpers ----------------------------------------------------------------
# server.py patch (idempotent, fails CLOSED when upstream's shape changed).
patch_server() {
  "$PY" - "$1" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p, encoding="utf-8", newline="").read()
if "_qflix_bind" in t:
    sys.exit(0)
call = re.compile(r"create_server\(app,\s*host=self\.address,\s*port=self\.port,\s*threads=100\)")
if len(call.findall(t)) != 1 or t.count("class Server:") != 1:
    sys.stderr.write("server.py: upstream shape changed; refusing to patch\n")
    sys.exit(1)
t = call.sub("create_server(app, **_qflix_bind(self.address, self.port), threads=4)", t)
helper = (
    "def _qflix_bind(address, port):\n"
    "    # QFlix (QFLX-27): bind exactly the recorded listen set. Unset = refuse,\n"
    "    # never fall back to the config ip ('*' = 0.0.0.0).\n"
    "    import os\n"
    "    listen = os.environ.get('BAZARR_LISTEN', '').strip()\n"
    "    if not listen:\n"
    "        raise RuntimeError('BAZARR_LISTEN is not set; refusing to bind')\n"
    "    return {'listen': listen}\n\n\n"
)
t = t.replace("class Server:", helper + "class Server:", 1)
open(p, "w", encoding="utf-8", newline="").write(t)
PY
}

# Text-level edits of config.yaml (keeps bazarr's own formatting).
#   cfg audit FILE             container paths other than backup.folder -> stdout, exit 1
#   cfg get FILE SECTION KEY   value (quotes stripped)
#   cfg set FILE SECTION KEY VALUE   rewrite the one line in place, exit 1 if absent
cfg() {
  # MSYS2_ARG_CONV_EXCL: Git Bash (workstation tests) would rewrite a container
  # path argv like /config/backup into C:/.../config/backup. No-op on the box.
  MSYS2_ARG_CONV_EXCL='*' "$PY" - "$@" <<'PY'
import os, re, sys, tempfile
mode, path = sys.argv[1], sys.argv[2]
lines = open(path, encoding="utf-8", newline="").read().split("\n")

def block(section):
    """(start, end) of the top-level block's body lines."""
    start = None
    for i, l in enumerate(lines):
        if re.match(r"^%s:\s*$" % re.escape(section), l):
            start = i + 1
            break
    if start is None:
        return None
    end = start
    while end < len(lines) and (not lines[end].strip() or lines[end][:1] in " \t-#"):
        end += 1
    return start, end

def find(section, key):
    b = block(section)
    if not b:
        return None
    for i in range(*b):
        m = re.match(r"^(\s+)%s:\s*(.*)$" % re.escape(key), lines[i])
        if m:
            return i, m
    return None

if mode == "get":
    hit = find(sys.argv[3], sys.argv[4])
    if not hit:
        sys.exit(1)
    print(hit[1].group(2).strip().strip("'\""))
elif mode == "set":
    sec, key, val = sys.argv[3], sys.argv[4], sys.argv[5]
    if not re.fullmatch(r"[A-Za-z0-9_./:@+-]*", val):
        sys.stderr.write("cfg: refusing an odd value\n")
        sys.exit(1)
    hit = find(sec, key)
    if not hit:
        sys.exit(1)
    i, m = hit
    lines[i] = "%s%s: %s" % (m.group(1), key, val)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines))
    os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    os.replace(tmp, path)
elif mode == "audit":
    allowed = find("backup", "folder")
    pat = re.compile(r"(^\s*-\s*|:\s+)['\"]?/(config|data|downloads)(/|['\"\s]|$)")
    bad = [(i + 1, l) for i, l in enumerate(lines)
           if not l.lstrip().startswith("#") and pat.search(l)
           and not (allowed and i == allowed[0])]
    for n, l in bad:
        print("%d:%s" % (n, l.strip()))
    sys.exit(1 if bad else 0)
PY
}

# status_probe CONFIG PORT: GET <base_url>/api/system/status with the config's
# own apikey (never on a command line). Prints bazarr_version, exit 0 only on 200.
status_probe() {
  "$PY" - "$1" "$2" <<'PY'
import json, re, sys, urllib.request
cfg, port = sys.argv[1], int(sys.argv[2])
text = open(cfg, encoding="utf-8").read().split("\n")
def val(section, key):
    on = False
    for l in text:
        if re.match(r"^%s:\s*$" % section, l):
            on = True
            continue
        if on and l and l[:1] not in " \t-#":
            on = False
        m = on and re.match(r"^\s+%s:\s*(.*)$" % key, l)
        if m:
            return m.group(1).strip().strip("'\"")
    return ""
base = "/" + val("general", "base_url").strip("/")
base = "" if base == "/" else base
req = urllib.request.Request("http://127.0.0.1:%d%s/api/system/status" % (port, base),
                             headers={"X-API-KEY": val("auth", "apikey")})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
        if r.status != 200:
            sys.exit(1)
except Exception:
    sys.exit(1)
print((body.get("data") or {}).get("bazarr_version") or "")
PY
}

# A free loopback port (the proof copy never claims a stored one: ports.claim is
# workstation-side and a proof port must not outlive the proof).
free_port() {
  "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
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

# PIDs under OUR uid whose cmdline carries the app pattern. kind=container: in
# a container cgroup (docker/libpod/...), not the unit's. kind=native: in the
# unit's cgroup (qflix-bazarr.service: bazarr2.service never contains it).
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

# Nothing may hold bazarr.db or its -wal (spec 5.9 step 6). fuser exits 0 when
# a process has the file open. A missing fuser is a refusal, not a pass.
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

# env file body (thread caps + BAZARR_VERSION hook + TZ) [+ BAZARR_LISTEN].
write_env() {
  local listen="${1:-}"
  if [ -n "$listen" ]; then
    native_render_env "$SLUG" "$FAMILY" "$2" "$TZ_ENV" "BAZARR_LISTEN=$listen"
  else
    native_render_env "$SLUG" "$FAMILY" "$2" "$TZ_ENV"
  fi | native_write_secure "$ENV_FILE" 0600
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

install_venv() {   # install_venv REQUIREMENTS
  if [ ! -x "$APPDIR/venv/bin/python" ]; then
    [ -x "$PY311" ] || die "python3.11 not found at $PY311 (QFLIX_PY311)"
    "$PY311" -m venv "$APPDIR/venv" || die "venv creation failed"
  fi
  "$APPDIR/venv/bin/python" -m pip install --quiet -r "$1" || die "pip install -r $1 failed"
}

# --- modes ----------------------------------------------------------------------------
do_install() {
  local stage inst
  mkdir -p "$APPS" || die "cannot create $APPS"
  [ -f "$CFG" ] && [ -f "$DB" ] || die "$CFG / $DB missing (the data is used in place)"
  stage="$(mktemp -d "$APPS/.stage-$SLUG.XXXXXX")" || die "mktemp failed"
  CLEANUP_PATHS+=("$stage")
  native_fetch_verify "$URL" "$WANT_SHA" "$stage/b.zip" || die "fetch/sha256 verify failed"
  mkdir -p "$stage/x"
  "$PY" - "$stage/b.zip" "$stage/x" <<'PY' || die "zip extraction failed"
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
for n in z.namelist():                       # zip-slip guard
    if n.startswith("/") or ".." in n.split("/"):
        sys.exit("unsafe member " + n)
z.extractall(sys.argv[2])
PY
  [ -f "$stage/x/bazarr.py" ] && [ -f "$stage/x/requirements.txt" ] || die "zip has no bazarr.py / requirements.txt"
  native_install_versioned "$SLUG" "$VERSION" "$stage/x" || die "install refused (see above)"
  inst="$APPDIR/bin/$VERSION"
  patch_server "$inst/bazarr/app/server.py" || die "server.py patch failed"
  install_venv "$inst/requirements.txt"
  write_env "" "$VERSION" || die "env file write failed"
  native_render_unit "$SLUG" "$FAMILY" "$EXE" "$EXEC_ARGS" \
    | native_write_secure "$APPDIR/native/$UNIT" 0644 || die "unit staging failed"
  info "installed $VERSION; unit staged at $APPDIR/native/$UNIT (not enabled). Next: --prove --execute"
}

do_prove() {
  local before after delta ceiling pport key out ver i
  [ -x "$APPDIR/venv/bin/python" ] && [ -f "$APPDIR/bin/current/bazarr.py" ] && [ -f "$ENV_FILE" ] \
    || die "not installed; run --install --execute first"
  grep -qx "BAZARR_VERSION=$VERSION" "$ENV_FILE" || die "$ENV_FILE lacks BAZARR_VERSION=$VERSION"
  ceiling="$(hostpolicy task-ceiling)" || die "task ceiling unknown; refusing (G-2)"
  ceiling="${ceiling%$'\r'}"
  rm -rf "$PROVE"
  mkdir -p "$PROVE/config" "$PROVE/db" "$PROVE/backup" "$PROVE/log" || die "cannot create $PROVE"
  [ "${QFLIX_KEEP_PROOF:-0}" = 1 ] || CLEANUP_PATHS+=("$PROVE")
  # VACUUM INTO a consistent copy (the live db is WAL: a cp could tear). Read-only
  # on the source.
  "$PY" - "$DB" "$PROVE/db/bazarr.db" <<'PY' || die "VACUUM INTO failed"
import sqlite3, sys
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1].replace("\\", "/"), uri=True)
con.execute("VACUUM INTO ?", (sys.argv[2],))
con.close()
PY
  cp "$CFG" "$PROVE/config/config.yaml" || die "cannot copy config.yaml"
  # Off the container paths and off the Plex integration, THEN sanitize.
  cfg set "$PROVE/config/config.yaml" backup folder "$PROVE/backup" || die "cannot repoint backup.folder in the copy"
  cfg set "$PROVE/config/config.yaml" general use_plex false || die "cannot switch use_plex off in the copy"
  out="$("$PY" "$SANITIZE" "$SLUG" "$PROVE")" || die "sanitize refused the proof copy: $out"
  "$PY" - "$out" <<'PY' || die "sanitize counts are not zero: $out"
import json, sys
c = json.loads(sys.argv[1])["counts"]
bad = {k: v for k, v in c.items() if v}
sys.exit(1 if bad else 0)
PY
  [ "$(cfg get "$PROVE/config/config.yaml" general use_plex)" = false ] || die "proof copy still has use_plex on"
  pport="$(free_port)" || die "no free loopback port"
  before="$(user_tasks)"
  [[ "$before" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  (
    set -a; . "$ENV_FILE"; set +a
    BAZARR_LISTEN="127.0.0.1:$pport"
    export BAZARR_LISTEN
    exec "$APPDIR/venv/bin/python" "$APPDIR/bin/current/bazarr.py" --no-update --config "$PROVE" \
      >"$PROVE/stdout.log" 2>&1
  ) &
  PROOF_PID=$!
  sleep "$SETTLE"
  ver=""
  i=0
  until ver="$(status_probe "$PROVE/config/config.yaml" "$pport")" && [ -n "$ver" ]; do
    ver=""
    i=$((i + 1))
    if [ $((i * POLL)) -ge "$PROOF_TIMEOUT" ]; then
      tail -5 "$PROVE/stdout.log" 2>/dev/null >&2
      die "proof: /api/system/status never answered 200 within ${PROOF_TIMEOUT}s"
    fi
    sleep "$POLL"
  done
  [ "$ver" = "$VERSION" ] || die "proof: bazarr_version '$ver' != pin '$VERSION' (BAZARR_VERSION env)"
  after="$(user_tasks)"
  [[ "$after" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  delta=$((after - before))
  kill "$PROOF_PID" 2>/dev/null; wait "$PROOF_PID" 2>/dev/null; PROOF_PID=""
  [ "$delta" -gt 0 ] || die "proof: measured task delta $delta; cannot gate the swap; refusing"
  if [ $(( (before + delta) * 100 )) -ge $(( 70 * ceiling )) ]; then
    die "thread gate: $before + $delta tasks reaches 70% of the ceiling $ceiling; refusing the swap"
  fi
  mkdir -p "$SWAPDIR"
  printf '{"ok": true, "version": "%s", "before": %s, "delta": %s, "ceiling": %s, "at": "%s"}\n' \
    "$VERSION" "$before" "$delta" "$ceiling" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    | native_write_secure "$SWAPDIR/proof.json" 0644 || die "cannot record the proof"
  info "PROOF OK: status 200, version $ver, sanitized copy inert; before=$before delta=$delta ceiling=$ceiling. Next: --swap --execute"
}

verify_native() {
  local n ver
  sleep "$SETTLE"
  sysd is-active "$UNIT" >/dev/null 2>&1 || { echo "unit not active" >&2; return 1; }
  [ -z "$(scan_pids container)" ] || { echo "a container process is running" >&2; return 1; }
  n="$(scan_pids native | wc -l | tr -d ' ')"
  [ "$n" -ge 1 ] || { echo "no native process" >&2; return 1; }
  native_listen_compare "$SLUG" >/dev/null || { echo "listen set differs from listen-set.before" >&2; return 1; }
  # bazarr takes a while to migrate/boot: poll the API.
  local start=$SECONDS
  while :; do
    ver="$(status_probe "$CFG" "$PROBE_PORT")" && [ -n "$ver" ] && break
    ver=""
    [ $((SECONDS - start)) -ge "$STATUS_TIMEOUT" ] && { echo "/api/system/status never answered 200" >&2; return 1; }
    sleep "$POLL"
  done
  [ "$ver" = "$VERSION" ] || { echo "status reports $ver, want $VERSION" >&2; return 1; }
}

do_swap() {
  local st cls ss_state ver snap now soak listen cur sp
  [ -f "$SWAPDIR/proof.json" ] || die "no proof recorded; run --prove --execute first"
  [ -x "$APPDIR/venv/bin/python" ] && [ -f "$APPDIR/native/$UNIT" ] && [ -f "$ENV_FILE" ] \
    || die "not installed; run --install --execute first"
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state _ <<<"$st"
  [ "$cls" = systemd ] && [ "$ss_state" = pending-swap ] \
    || die "the deployed manifest is not the pending-swap flip (class=$cls swap_state=$ss_state); merge + deploy it via 240 first"

  if sysd is-active "$UNIT" >/dev/null 2>&1 && [ -z "$(scan_pids container)" ]; then
    info "already swapped; verifying only"
    verify_native || die "native bazarr fails parity; run --rollback --execute"
    info "verified"
    return 0
  fi

  if is_masked; then
    sysd unmask "$UNIT" || die "unmask $UNIT failed"
    rm -f "$ENV_DIR/parked-units/$UNIT"
  fi

  # Step 3: capture + audits.
  if [ -s "$SECRETS/bazarr.port" ]; then
    sp="$(tr -d '[:space:]' < "$SECRETS/bazarr.port")"
    [ "$sp" = "$PROBE_PORT" ] || die "secrets/bazarr.port ($sp) != probe port ($PROBE_PORT)"
  fi
  ver="$(native_ucc_version "$SLUG")"
  [ "${ver#v}" = "$VERSION" ] || die "version parity: container=$ver native=$VERSION"
  native_listen_capture "$SLUG" "$PROBE_PORT" >/dev/null || die "listen-set capture failed"
  listen="$(listen_env)" || die "listen set on :$PROBE_PORT unusable (must be non-empty, no wildcard)"
  swapstate set "$SLUG" "ucc_version=$VERSION" >/dev/null || die "cannot record ucc_version"
  hits="$(cfg audit "$CFG")" || die "container path(s) in $CFG: $(echo "$hits" | head -3 | tr '\n' ' ')"
  cur="$(cfg get "$CFG" backup folder)" || die "no backup.folder in $CFG"

  # Step 4: suppress the app and its canaries together.
  suppression add "${SUPPRESS[@]}" --reason "QFLX-27 swap to native" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to swap unsuppressed"

  # Step 6: stop the container; its exit is asynchronous Docker behaviour, so
  # the STATE decides (no process, port free, db + wal idle), not the exit code.
  "$APPCTL" stop "$SLUG" >/dev/null 2>&1 || info "appctl stop returned non-zero; polling decides"
  if ! wait_until "$STOP_TIMEOUT" container_gone; then
    "$APPCTL" start "$SLUG" >/dev/null 2>&1 || true
    suppression remove "${SUPPRESS[@]}" >/dev/null || true
    die "the container did not exit within ${STOP_TIMEOUT}s (pids: $(scan_pids container | tr '\n' ' ')); swap aborted, container start requested, suppression lifted"
  fi

  # Step 5, after the stop so the sqlite files are quiescent (WAL included).
  mkdir -p "$SWAPDIR"
  snap="$SWAPDIR/snapshot-$(date -u +%Y%m%dT%H%M%SZ).tgz"
  tar --force-local -czf "$snap" -C "$APPS" --exclude="$SLUG/bin" --exclude="$SLUG/venv" \
      --exclude="$SLUG/native" --exclude="$SLUG/cache" --exclude="$SLUG/log" \
      --exclude="$SLUG/backup" "$SLUG" \
    || { "$APPCTL" start "$SLUG" >/dev/null 2>&1; suppression remove "${SUPPRESS[@]}" >/dev/null || true; die "snapshot failed; container start requested, suppression lifted"; }

  # Path rewrite: backup.folder is the one container path. Original recorded for
  # rollback (the container cannot write the host path).
  case "$cur" in
    /config/*|/config)
      printf '%s' "$cur" | native_write_secure "$SWAPDIR/backup-folder.orig" 0600 || die "cannot record the original backup.folder"
      cfg set "$CFG" backup folder "$APPDIR/backup" || die "cannot rewrite backup.folder"
      ;;
    "$APPDIR/backup") ;;                       # resumed after a rewrite
    *) die "backup.folder '$cur' is neither the container path nor $APPDIR/backup" ;;
  esac

  write_env "$listen" "$VERSION" || die "env file write failed"
  mkdir -p "$UNIT_DIR"
  native_write_secure "$UNIT_DIR/$UNIT" 0644 < "$APPDIR/native/$UNIT" || die "unit install failed"
  sysd daemon-reload || die "daemon-reload failed"
  sysd enable --now "$UNIT" || die "enable --now $UNIT failed; run --rollback --execute"

  if ! verify_native; then
    die "native bazarr fails parity after the swap; suppression kept ON; run --rollback --execute"
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
  verify_native || die "native bazarr fails parity; run --rollback --execute"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "FINISHED: ${SUPPRESS[*]} unsuppressed; 14-day soak running"
}

do_rollback() {
  local isn orig cur
  suppression add "${SUPPRESS[@]}" --reason "QFLX-27 rollback to UCC" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to roll back unsuppressed"
  mask_unit
  sysd stop "$UNIT" >/dev/null 2>&1 || true
  wait_until "$STOP_TIMEOUT" native_gone || die "native bazarr did not stop within ${STOP_TIMEOUT}s"
  # Give the container back its own path, but only if we rewrote it and nobody
  # has changed it since (an operator edit in the UI wins).
  if [ -s "$SWAPDIR/backup-folder.orig" ] && [ -f "$CFG" ]; then
    orig="$(cat "$SWAPDIR/backup-folder.orig")"
    cur="$(cfg get "$CFG" backup folder)"
    if [ "$cur" = "$APPDIR/backup" ]; then
      cfg set "$CFG" backup folder "$orig" || die "cannot restore backup.folder to $orig"
    fi
  fi
  isn="$("$APPCTL" is-native "$SLUG" 2>/dev/null)"
  if [ "${isn%$'\r'}" != ucc ]; then
    echo "[302-bazarr] PAUSED: native stopped + masked; revert the deployed manifest (PR + 240) so appctl dispatches $SLUG as UCC, then re-run --rollback --execute" >&2
    exit 10
  fi
  "$APPCTL" start "$SLUG" >/dev/null 2>&1 || info "appctl start returned non-zero; polling decides"
  wait_until "$STOP_TIMEOUT" container_up || die "the container did not come back within ${STOP_TIMEOUT}s; still suppressed"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "ROLLED BACK to UCC; $UNIT masked (unmasked by the next --swap). elapsed=$((SECONDS - T0))s"
}

do_post_upgrade() {
  local v="$UPGRADE_VER" d tmp cur listen
  v="${v#v}"
  _native_valid_ver "$v" || die "bad version: $v"
  d="$APPDIR/bin/$v"
  [ -f "$d/bazarr.py" ] && [ -f "$d/requirements.txt" ] || die "$d is not an extracted bazarr release"
  patch_server "$d/bazarr/app/server.py" || die "server.py patch failed"
  install_venv "$d/requirements.txt"
  native_link_current "$SLUG" "$v" || die "cannot flip bin/current"
  [ -f "$ENV_FILE" ] || die "$ENV_FILE missing"
  listen="$(sed -n 's/^BAZARR_LISTEN=//p' "$ENV_FILE" | tail -n 1)"
  write_env "$listen" "$v" || die "env file rewrite failed"
  grep -qx "BAZARR_VERSION=$v" "$ENV_FILE" || die "BAZARR_VERSION not written"
  info "post-upgrade $v: patched, venv updated, current flipped, BAZARR_VERSION set"
}

case "$MODE" in
  install)      do_install ;;
  prove)        do_prove ;;
  swap)         do_swap ;;
  finish)       do_finish ;;
  rollback)     do_rollback ;;
  post-upgrade) do_post_upgrade ;;
esac
