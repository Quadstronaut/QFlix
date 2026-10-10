#!/usr/bin/env bash
# 305-native-sonarr2-install.sh -- QFLX-30 (UCC divorce A6, the anime Sonarr).
#
# Moves sonarr2 off the Ultra.cc container manager (UCC) onto user units the
# repo owns: the upstream Sonarr v4 linux-x64 tarball at EXACTLY the container's
# build, sha256-pinned, run as qflix-sonarr2.service, plus a socket-activated
# loopback forwarder (qflix-sonarr2-fwd.socket/.service). Spec:
# docs/superpowers/specs/2026-10-09-ucc-divorce-design.md 5.1-5.9, row 6 of 6.
# Shape copied from the pilot (300-native-unpackerr-install.sh, QFLX-25) and the
# first .NET arr (303-native-prowlarr-install.sh, QFLX-28). The data stays IN
# PLACE: ~/.apps/sonarr2 (sonarr.db, logs.db, config.xml, MediaCover) is used by
# the native binary exactly as the container used it.
#
# Two things differ from prowlarr and are the reason this file is not a rename:
#   * The primary sonarr container runs the SAME binary path (/app/sonarr/bin/
#     Sonarr -nobrowser -data=/config), so a cmdline scan cannot tell the two
#     containers apart. "Is the sonarr2 container still alive" is therefore
#     answered by what holds sonarr2's OWN db (fuser) and its port, never by a
#     process pattern. The native side is told apart by the unit's cgroup.
#   * The data dir keeps a 0-byte sonarr2.db stray (2026-05-10); the live db is
#     sonarr.db (Sonarr's fixed name). Only sonarr.db is copied/audited.
#
# RUNS ON THE BOX. 240-maintenance-install.sh deploys it to ~/scripts/configure/
# with ~/scripts/lib/native.sh and ~/scripts/maint/native_sanitize.py beside it.
# Started from the workstation it re-executes the deployed copy over ssh.
#
# INERT BY DEFAULT (I-3). Every mode prints its plan (DRY-RUN) and touches
# nothing unless --execute is also given. Modes, in swap order:
#
#   --install   5.9 step 1. Fetch + sha256-verify the release, refuse unless the
#               panel version is a dotted prefix of the pinned build (the panel
#               prints "4.0.20", the build is 4.0.20.3014) AND the live API says
#               the pinned build (I-10), lay out ~/.apps/sonarr2/bin/<ver> +
#               current, write the env file and STAGE the three units in
#               ~/.apps/sonarr2/native/. Deliberately NOT copied into
#               ~/.config/systemd/user and NOT enabled: WantedBy=default.target
#               would start it beside the live container (I-6).
#   --prove     5.9 step 2. VACUUM INTO a copy of sonarr.db under
#               ~/.apps/.prove/sonarr2, copy config.xml, run native_sanitize
#               (download clients + indexers + import lists off, notifications
#               deleted, auto-update off) plus the metadata consumers off (they
#               would write .nfo/images beside the media), re-read and counted.
#               Boot the NATIVE binary on a free loopback port with the
#               port/bind/update settings coming from the environment (proves
#               config.xml's Port 8989 is overridden), check status (build ==
#               pin, not docker), renameEpisodes == true (naming API), and
#               series-count parity between the live container and the copy.
#               Measure the task count (refused at 70% of the host ceiling),
#               then delete the copy. Never side by side against live queues.
#   --swap      5.9 steps 3-6: needs the proof and the DEPLOYED pending-swap
#               manifest flip. Unmask (rollback step 4), assert config.xml equals
#               the secrets (port/urlbase/key) and local-auth-bypass is off,
#               assert renameEpisodes is true on the live container, refuse while
#               an import is in flight, capture the listen set (must hold
#               loopback + the bridge; any other non-wildcard address is
#               recorded as a D-4 exception), path-audit config.xml + the db,
#               suppress the app + its canaries, snapshot, stop the container
#               through appctl, POLL until nothing holds the db and the port is
#               free (abort and restore the container otherwise), install +
#               enable --now the unit, then the loopback socket, verify parity
#               (pinned build, renameEpisodes still true, same series count),
#               record swap state (14-day soak). Suppression stays ON: the
#               manifest still says pending-swap.
#   --finish    5.9 step 9, after the follow-up PR dropped `swap_state` and 240
#               deployed it: verify, then lift the app + canaries together.
#               Then (operator, once, by hand) run the buildarr oneshot:
#               systemctl --user start buildarr.service (same port + key, so it
#               reconciles against the native app; renameEpisodes stays true
#               because the proof and the parity check both pin it).
#   --rollback  Rollback 0-5. 0: re-suppress, park the unit files and MASK all
#               three (without the mask, pusher recovery restarts even a
#               disabled unit: two runtimes on one SQLite file, I-6). 1: stop
#               them and wait for exit and a free port. 2: the DEPLOYED manifest
#               must dispatch sonarr2 as UCC again (revert PR + 240, or the
#               pending-swap state); otherwise exit 10 and re-run after the
#               revert. 3: start the container through appctl. 5: config.xml is
#               restored byte-for-byte if the native era changed it; no db
#               restore (versions equal, no migration). 4 (unmask) happens at the
#               next --swap.
#
# Settings that differ from the container live in the ENV FILE, never in
# config.xml (SONARR__SERVER__PORT / __BINDADDRESS, SONARR__UPDATE__MECHANISM
# = External). config.xml stays exactly what the container reads, so a rollback
# needs no config surgery.
#
# The swap and rollback print elapsed=<s>.
#
# Exit: 0 ok | 1 refused/failed | 10 rollback paused for the manifest revert |
# 64 usage.
#
# Overrides (tests; resolved at call time): QFLIX_APPS_DIR QFLIX_UNIT_DIR
# QFLIX_ENV_DIR QFLIX_SWAP_DIR QFLIX_SECRETS_DIR MANITOBA_STATE_DIR
# QFLIX_MANIFEST QFLIX_PROC QFLIX_PYTHON QFLIX_APPCTL QFLIX_SYSTEMCTL QFLIX_SS
# QFLIX_PS QFLIX_CURL QFLIX_FUSER QFLIX_HOSTPOLICY QFLIX_HOST_ID_FILE
# QFLIX_SONARR2_SHA256 QFLIX_POLL_S QFLIX_SETTLE_S QFLIX_STOP_TIMEOUT_S
# QFLIX_PROOF_TIMEOUT_S QFLIX_API_TIMEOUT_S QFLIX_KEEP_PROOF.
set -uo pipefail

SLUG=sonarr2
VERSION="4.0.20.3014"        # == versions.env SONARR2_VERSION (test-pinned)
SHA256="1fc48544b5a3401b2fc3df8d0642c1e6a73a28e41a351169edd9cd8f54770db5"
URL="https://github.com/Sonarr/Sonarr/releases/download/v${VERSION}/Sonarr.main.${VERSION}.linux-x64.tar.gz"
UNIT="qflix-${SLUG}.service"
FWD_SOCKET="qflix-${SLUG}-fwd.socket"
FWD_UNIT="qflix-${SLUG}-fwd.service"
ALL_UNITS=("$UNIT" "$FWD_SOCKET" "$FWD_UNIT")
FAMILY=dotnet
EXE=Sonarr
# %h stays literal: systemd expands it. -data is the container's /config seen
# from the host: the container mounted ~/.apps/sonarr2 at /config.
EXEC_ARGS="-nobrowser -data=%h/.apps/${SLUG}"
# The container ran with TZ=Europe/Amsterdam and COMPlus_EnableDiagnostics=0
# (its /proc/<pid>/environ, box 2026-10-10). Keeping TZ keeps the log timestamps
# vlogs ingests unshifted (QFLX-44 class).
TZ_ENV="TZ=Europe/Amsterdam"
PORT=17003                   # the UCC host port == secrets/sonarr2.port (asserted)
BRIDGE=""                    # secret net.app_host (docker0 on Ultra); the arrs / Seerr / FlareSolverr call here (F-17)
LOOPBACK=127.0.0.1           # nginx, the health probe and the canaries call here
# Behavioural canaries muted with the app (plan A-table row A6: anime,
# arr-plex-parity; seerr-arr-parity also reads sonarr2's API). Keys follow
# cli.py: canary-<name>.
SUPPRESS=("$SLUG" "canary-anime" "canary-arr-plex-parity" "canary-seerr-arr-parity"
          "canary-thread-ceiling")
# Native cmdline: .../bin/current/Sonarr -nobrowser -data=... The CONTAINER
# cmdline (/app/sonarr/bin/Sonarr) is identical for sonarr and sonarr2, so the
# container is never found by pattern (see scan_pids / container_pids).
PATTERN="/bin/current/Sonarr"
DBNAME=sonarr.db             # Sonarr's fixed db name (the 0-byte sonarr2.db is a stray)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # .../scripts
ARGS=("$@")

info() { echo "[305-sonarr2] $*"; }
die()  { echo "[305-sonarr2] ERROR: $*" >&2; exit 1; }
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
SECRETS="${QFLIX_SECRETS_DIR:-$HOME/secrets}"
PROVE="$APPS/.prove/$SLUG"
MANIFEST="${QFLIX_MANIFEST:-$HOME/.opt/maint/apps.yaml}"
MAINT_LIB="$HERE/maint/lib"
SANITIZE="$HERE/maint/native_sanitize.py"
PY="${QFLIX_PYTHON:-python3}"
APPCTL="${QFLIX_APPCTL:-$HOME/bin/appctl}"
SYSTEMCTL="${QFLIX_SYSTEMCTL:-systemctl}"
SS="${QFLIX_SS:-ss}"
PS="${QFLIX_PS:-ps}"
CURL="${QFLIX_CURL:-curl}"
FUSER="${QFLIX_FUSER:-fuser}"
PROC="${QFLIX_PROC:-/proc}"
POLL="${QFLIX_POLL_S:-2}"
SETTLE="${QFLIX_SETTLE_S:-10}"
STOP_TIMEOUT="${QFLIX_STOP_TIMEOUT_S:-120}"
PROOF_TIMEOUT="${QFLIX_PROOF_TIMEOUT_S:-300}"
API_TIMEOUT="${QFLIX_API_TIMEOUT_S:-120}"
WANT_SHA="${QFLIX_SONARR2_SHA256:-$SHA256}"

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
    install)  info "would fetch $URL (sha256 $WANT_SHA), check version parity (panel prefix + API build), lay out $APPDIR/bin/$VERSION, write $ENV_DIR/$SLUG.env, stage $APPDIR/native/{$UNIT,$FWD_SOCKET,$FWD_UNIT} (not enabled)" ;;
    prove)    info "would VACUUM INTO a copy of $DBNAME in $PROVE, sanitize it (arr flags + metadata consumers off), boot the native binary on a free loopback port with env-only port/bind, check status + renameEpisodes + series-count parity, gate the task delta at 70% of the ceiling, then delete the copy" ;;
    swap)     info "would capture the listen set (port $PORT: loopback + the net.app_host bridge required), assert config==secrets and auth-bypass off, path-audit, suppress ${SUPPRESS[*]}, snapshot, stop the container, wait for exit + free port + idle db, enable --now $UNIT then $FWD_SOCKET, verify, record swap state" ;;
    finish)   info "would verify the native units and lift suppression for ${SUPPRESS[*]}" ;;
    rollback) info "would suppress ${SUPPRESS[*]}, park + mask ${ALL_UNITS[*]}, stop them, wait for the manifest revert, start the container via appctl, unsuppress" ;;
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
  sshm "~/scripts/configure/305-native-sonarr2-install.sh $(printf '%q ' "${ARGS[@]}")"
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
    [ "$PROFILE" = ultra ] || die "mode $MODE swaps against a UCC container; host profile is '$PROFILE'"
    # No fuser = no way to tell the sonarr2 container from the primary sonarr
    # one (same cmdline): refuse rather than call the container "gone".
    command -v "${QFLIX_FUSER:-fuser}" >/dev/null 2>&1 || die "no fuser on this host; cannot tell the sonarr2 container from sonarr's" ;;
esac

# The bridge address comes from the net.app_host secret (QFLX-23), never a literal.
# The golden units under scripts/maint/systemd carry the Ultra value; a different
# secret renders different units and deploy-drift (deploy_parity) names the diff.
# Modes swap/finish/rollback already require profile=ultra.
BRIDGE="$(tr -d '[:space:]' < "$SECRETS/net.app_host" 2>/dev/null)"
[[ "$BRIDGE" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "secrets/net.app_host unreadable or not an IPv4 address; refusing"

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

# kind=native: PIDs under OUR uid in the unit's cgroup whose cmdline carries the
# native pattern. kind=container: PIDs holding sonarr2's OWN db files open that
# are NOT in the unit's cgroup. The sonarr and sonarr2 containers run the same
# /app/sonarr/bin/Sonarr cmdline, so a pattern scan would name the PRIMARY
# sonarr container as ours; the db path cannot be confused.
scan_pids() {
  local kind="$1" uid d pid cg cmd f
  if [ "$kind" = container ]; then
    command -v "$FUSER" >/dev/null 2>&1 || return 0
    for f in "$APPDIR/$DBNAME" "$APPDIR/$DBNAME-wal" "$APPDIR/logs.db" "$APPDIR/logs.db-wal"; do
      [ -e "$f" ] || continue
      for pid in $("$FUSER" "$f" 2>/dev/null); do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        cg="$(cat "$PROC/$pid/cgroup" 2>/dev/null)"
        case "$cg" in *"$UNIT"*) continue ;; esac
        echo "$pid"
      done
    done | sort -u
    return 0
  fi
  uid="$(id -u)"
  for d in "$PROC"/[0-9]*; do
    [ -d "$d" ] || continue
    pid="${d##*/}"
    [ "$(awk '/^Uid:/ {print $2; exit}' "$d/status" 2>/dev/null)" = "$uid" ] || continue
    cg="$(cat "$d/cgroup" 2>/dev/null)" || continue
    cmd="$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)" || continue
    case "$cmd" in *"$PATTERN"*) ;; *) continue ;; esac
    case "$cg" in *"$UNIT"*) echo "$pid" ;; esac
  done
}

port_free() { ! "$SS" -tlnH "sport = :$PORT" 2>/dev/null | grep -q ":$PORT\b"; }

# Nothing holds the db or its WAL (spec 5.9 step 6 / G-4). `fuser -s` exits 0
# when something has the file open; a missing fuser is a refusal, never "idle".
db_idle() {
  local f
  command -v "$FUSER" >/dev/null 2>&1 || { echo "no fuser: cannot prove the db idle" >&2; return 1; }
  for f in "$APPDIR/$DBNAME" "$APPDIR/$DBNAME-wal" "$APPDIR/logs.db" "$APPDIR/logs.db-wal"; do
    [ -e "$f" ] || continue
    if "$FUSER" -s "$f" >/dev/null 2>&1; then return 1; fi
  done
  return 0
}

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
container_gone() { [ -z "$(scan_pids container)" ] && port_free && db_idle; }
container_up()   { [ -n "$(scan_pids container)" ]; }
native_gone()    { [ -z "$(scan_pids native)" ]; }
# After the native units stop, the port must be free before the container starts;
# a container that is already up (nothing was swapped) legitimately holds it.
bind_clear()     { container_up || port_free; }

user_tasks() { "$PS" -u "$(id -u)" -L --no-headers 2>/dev/null | wc -l | tr -d ' '; }

# <Tag> value from the app's own config.xml (first match; CR stripped).
cfg_get() { sed -n "s#.*<$1>\(.*\)</$1>.*#\1#p" "$APPDIR/config.xml" 2>/dev/null | head -1 | tr -d '\r'; }
secret()  { tr -d '[:space:]' < "$SECRETS/$1" 2>/dev/null; }

# X-Api-Key travels in a 0600 header file (curl -H @file), never on a command
# line other tenants can read in `ps`.
HDR=""
make_hdr() {
  local key
  key="$(cfg_get ApiKey)"
  [ -n "$key" ] || die "no ApiKey in $APPDIR/config.xml"
  HDR="$APPS/.hdr-$SLUG.$$"
  CLEANUP_PATHS+=("$HDR")
  ( umask 077; printf 'X-Api-Key: %s\n' "$key" > "$HDR" ) || die "cannot write the header file"
}
# api_get <port> <path-under-/api/v3>
api_get() {
  local base
  base="$(cfg_get UrlBase)"; base="${base%/}"
  "$CURL" -fsS --max-time 60 -H "@$HDR" "http://$LOOPBACK:$1$base/api/v3/$2"
}
json_get() {
  "$PY" -c 'import sys, json
try:
    v = json.load(sys.stdin)[sys.argv[1]]
except Exception:
    sys.exit(1)
print(str(v).lower() if isinstance(v, bool) else v)' "$1"
}

# Number of series the app on <port> serves.
series_count() {
  api_get "$1" series 2>/dev/null \
    | "$PY" -c 'import sys, json; d = json.load(sys.stdin); assert isinstance(d, list); print(len(d))'
}
rename_is_true() {
  [ "$(api_get "$1" config/naming 2>/dev/null | json_get renameEpisodes)" = true ]
}
# Nothing mid-import: a stop during an import leaves a half-moved file.
queue_not_importing() {
  api_get "$PORT" "queue?pageSize=200" 2>/dev/null | "$PY" -c 'import sys, json
d = json.load(sys.stdin)
bad = [r for r in d.get("records", []) if str(r.get("trackedDownloadState", "")).lower() in ("importing", "importpending")]
sys.exit(1 if bad else 0)'
}

# The build the API reports on <port> must be exactly the pin (I-10); the panel
# tool only knows major.minor.patch.
api_build_is_pin() {
  local v
  v="$(api_get "$1" system/status 2>/dev/null | json_get version)" || return 1
  [ "${v%$'\r'}" = "$VERSION" ]
}

# Container paths (/config, /data, /downloads) mean nothing on the host (5.9 step 3).
path_audit() {
  local hits dbhits
  hits="$(grep -nE ">(/config|/data|/downloads)(/|<)" "$APPDIR/config.xml" || true)"
  [ -z "$hits" ] || die "container path(s) in $APPDIR/config.xml: $(echo "$hits" | head -3 | tr '\n' ' ')"
  dbhits="$("$PY" - "$APPDIR/$DBNAME" <<'PY'
import re, sqlite3, sys
from pathlib import Path
rx = re.compile(r"""(^|["'])(/config|/data|/downloads)(/|["']|$)""")
con = sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
# History-like tables keep paths from the container era on purpose.
skip = {"History", "Blocklist", "ScheduledTasks", "PendingReleases", "EpisodeFiles",
        "ExtraFiles", "SubtitleFiles", "MetadataFiles", "IndexerStatus", "DownloadClientStatus"}
hits = []
for (t,) in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'"):
    if t in skip:
        continue
    for (c, ty) in [(r[1], r[2]) for r in con.execute(f'pragma table_info("{t}")')]:
        if ty and ty.upper() not in ("TEXT", "VARCHAR", ""):
            continue
        for (v,) in con.execute(f'select "{c}" from "{t}" where "{c}" like \'%/config%\' or "{c}" like \'%/data%\' or "{c}" like \'%/downloads%\''):
            if isinstance(v, str) and rx.search(v):
                hits.append(f"{t}.{c}")
                break
print(" ".join(sorted(set(hits))))
PY
)" || die "db path audit failed"
  [ -z "$dbhits" ] || die "container path(s) in $DBNAME columns: $dbhits"
}

# systemd treats a unit linked to /dev/null OR an empty unit file as masked.
is_masked() {
  local f="$UNIT_DIR/$1"
  [ -L "$f" ] || { [ -f "$f" ] && [ ! -s "$f" ]; }
}

mask_unit() {
  local u="$1"
  if is_masked "$u"; then return 0; fi                       # already masked
  if [ -f "$UNIT_DIR/$u" ]; then
    sysd disable "$u" >/dev/null 2>&1 || true
    mkdir -p "$ENV_DIR/parked-units"
    mv -f "$UNIT_DIR/$u" "$ENV_DIR/parked-units/$u" || die "cannot park $u"
  fi
  # `systemctl mask` refuses while a real unit file sits in the same dir, hence
  # the park above.
  sysd mask "$u" || die "systemctl --user mask $u failed"
  is_masked "$u" || die "$u not masked after mask"
}
mask_all()   { local u; for u in "$FWD_SOCKET" "$FWD_UNIT" "$UNIT"; do mask_unit "$u"; done; sysd daemon-reload || true; }
unmask_all() {
  local u any=0
  for u in "${ALL_UNITS[@]}"; do
    if is_masked "$u"; then sysd unmask "$u" || die "unmask $u failed"; rm -f "$ENV_DIR/parked-units/$u"; any=1; fi
  done
  [ "$any" = 0 ] || sysd daemon-reload || true
}

# --- unit bodies (byte-identical to scripts/maint/systemd/qflix-sonarr2*; test-enforced)
render_fwd_socket() {
  cat <<EOF
[Unit]
Description=QFlix sonarr2 loopback listener (forwards to the docker-bridge bind)

[Socket]
# Kestrel binds ONE address; the UCC container listened on three (spec 5.4,
# F-17). The app binds $BRIDGE (the arrs, Seerr and FlareSolverr reach it
# there); nginx, the health probe and the canaries reach it on loopback through
# this socket. The public-IP listener is the recorded D-4 exception.
ListenStream=$LOOPBACK:$PORT

[Install]
WantedBy=sockets.target
EOF
}
render_fwd_service() {
  cat <<EOF
[Unit]
Description=QFlix sonarr2 loopback forwarder
Requires=$FWD_SOCKET
After=$FWD_SOCKET $UNIT

[Service]
Type=simple
ExecStart=/usr/lib/systemd/systemd-socket-proxyd $BRIDGE:$PORT
Nice=5
EOF
}

# --- modes ----------------------------------------------------------------------------
do_install() {
  local stage panel_port
  [ -f "$APPDIR/config.xml" ] || die "$APPDIR/config.xml missing (the config is used in place)"
  panel_port="$(secret sonarr2.port)"
  [ "$panel_port" = "$PORT" ] || die "secrets/sonarr2.port is '$panel_port', the pinned units use $PORT; refusing"
  mkdir -p "$APPS" || die "cannot create $APPS"
  # The API build is the real parity: the panel tool truncates (4.0.20).
  make_hdr
  api_build_is_pin "$PORT" || die "the live container's API build is not $VERSION; re-pin before installing"
  stage="$(mktemp -d "$APPS/.stage-$SLUG.XXXXXX")" || die "mktemp failed"
  CLEANUP_PATHS+=("$stage")
  native_fetch_verify "$URL" "$WANT_SHA" "$stage/p.tgz" || die "fetch/sha256 verify failed"
  mkdir -p "$stage/x"
  # The tarball's single top-level dir is Sonarr/; strip it so bin/<ver>/Sonarr is the apphost.
  "${TAR[@]}" -xzf "$stage/p.tgz" -C "$stage/x" --strip-components=1 || die "cannot unpack the tarball"
  [ -f "$stage/x/$EXE" ] && [ -f "$stage/x/Sonarr.dll" ] || die "tarball has no $EXE / Sonarr.dll"
  chmod 0755 "$stage/x/$EXE"
  rm -rf "$stage/x/Sonarr.Update"        # the self-updater is dead weight: UpdateMechanism=External (I-10)
  NATIVE_PARITY=prefix native_install_versioned "$SLUG" "$VERSION" "$stage/x" || die "install refused (see above)"
  native_render_env "$SLUG" "$FAMILY" "$VERSION" "$TZ_ENV" "COMPlus_EnableDiagnostics=0" \
      "SONARR__SERVER__BINDADDRESS=$BRIDGE" "SONARR__SERVER__PORT=$PORT" \
      "SONARR__UPDATE__MECHANISM=External" "SONARR__UPDATE__AUTOMATICALLY=false" \
    | native_write_secure "$ENV_DIR/$SLUG.env" 0600 || die "env file write failed"
  native_render_unit "$SLUG" "$FAMILY" "$EXE" "$EXEC_ARGS" \
    | native_write_secure "$APPDIR/native/$UNIT" 0644 || die "unit staging failed"
  render_fwd_socket  | native_write_secure "$APPDIR/native/$FWD_SOCKET" 0644 || die "socket staging failed"
  render_fwd_service | native_write_secure "$APPDIR/native/$FWD_UNIT" 0644 || die "forwarder staging failed"
  info "installed $VERSION; units staged in $APPDIR/native (not enabled). Next: --prove --execute"
}

do_prove() {
  local before ceiling delta pport live proofn naming status_json version docker
  [ -x "$APPDIR/bin/current/$EXE" ] || die "not installed; run --install --execute first"
  [ -f "$SANITIZE" ] || die "$SANITIZE missing (run 240 first)"
  ceiling="$(hostpolicy task-ceiling)" || die "task ceiling unknown; refusing (G-2)"
  ceiling="${ceiling%$'\r'}"
  pport="$("$APPCTL" ports-free 2>/dev/null | head -1 | tr -d '[:space:]')"
  [[ "$pport" =~ ^[0-9]+$ ]] && [ "$pport" != "$PORT" ] || die "no free proof port from appctl ports-free"
  make_hdr
  rm -rf "$PROVE"
  ( umask 077; mkdir -p "$PROVE" ) || die "cannot create $PROVE"
  [ "${QFLIX_KEEP_PROOF:-0}" = 1 ] || CLEANUP_PATHS+=("$PROVE")
  # Copy, never the live files: VACUUM INTO writes a consistent snapshot even
  # while the container holds the WAL open.
  "$PY" - "$APPDIR/$DBNAME" "$PROVE/$DBNAME" <<'PY' || die "VACUUM INTO failed"
import sqlite3, sys
from pathlib import Path
con = sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
con.execute("VACUUM INTO ?", (sys.argv[2],))
con.close()
PY
  cp -p "$APPDIR/config.xml" "$PROVE/config.xml" || die "cannot copy config.xml"
  # INERT (I-11): download clients, indexers and import lists off, notifications
  # gone, updates off. The sanitizer re-reads the files and refuses unless every
  # count is 0. Metadata consumers (kodi/plex nfo + images) would WRITE beside
  # the real media on a series refresh, so they go off in the copy as well.
  "$PY" "$SANITIZE" "$SLUG" "$PROVE" >"$PROVE/sanitize.out" 2>&1 \
    || { cat "$PROVE/sanitize.out" >&2; die "sanitize refused: the proof copy is not inert; not booting it"; }
  "$PY" - "$PROVE/$DBNAME" <<'PY' || die "metadata consumers not off in the proof copy; not booting it"
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
cols = [r[1] for r in con.execute('pragma table_info("Metadata")')]
if cols:
    if "Enable" not in cols:
        sys.exit("Metadata lacks Enable; cannot prove inert")
    con.execute('UPDATE "Metadata" SET "Enable"=0')
    con.commit()
    if con.execute('SELECT count(*) FROM "Metadata" WHERE "Enable"!=0').fetchone()[0]:
        sys.exit("metadata consumers still enabled")
con.close()
PY
  before="$(user_tasks)"
  [[ "$before" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  # Port/bind/update come from the ENVIRONMENT, exactly as in the real unit:
  # config.xml still says Port 8989, so a green status here proves the override.
  env DOTNET_PROCESSOR_COUNT=4 DOTNET_gcServer=0 MALLOC_ARENA_MAX=2 COMPlus_EnableDiagnostics=0 \
      "$TZ_ENV" SONARR__SERVER__BINDADDRESS="$LOOPBACK" SONARR__SERVER__PORT="$pport" \
      SONARR__UPDATE__MECHANISM=External SONARR__UPDATE__AUTOMATICALLY=false \
      "$APPDIR/bin/current/$EXE" -nobrowser "-data=$PROVE" >"$PROVE/stdout.log" 2>&1 &
  PROOF_PID=$!
  sleep "$SETTLE"
  status_json=""
  proof_up() { status_json="$(api_get "$pport" system/status 2>/dev/null)" && [ -n "$status_json" ]; }
  if ! wait_until "$PROOF_TIMEOUT" proof_up; then
    tail -5 "$PROVE/stdout.log" >&2
    die "proof: no status from the proof copy on :$pport within ${PROOF_TIMEOUT}s"
  fi
  version="$(printf '%s' "$status_json" | json_get version)"
  docker="$(printf '%s' "$status_json" | json_get isDocker)"
  [ "${version%$'\r'}" = "$VERSION" ] || die "proof: status reports build '$version', pinned $VERSION"
  [ "${docker%$'\r'}" = false ] || die "proof: status says isDocker=$docker; this is not the native binary"
  # renameEpisodes MUST be true (arr-plex-parity: sonarr2 once had it false and
  # Plex could not match anime). Proven through the API of the booted copy.
  naming="$(api_get "$pport" config/naming 2>/dev/null | json_get renameEpisodes)" \
    || die "proof: cannot read config/naming from the proof copy"
  [ "${naming%$'\r'}" = true ] || die "proof: renameEpisodes is '$naming' in the copy; it must stay true"
  # Series-count parity: the copy answers with the same library the container serves.
  live="$(series_count "$PORT")" || die "proof: cannot count series on the live container"
  proofn="$(series_count "$pport")" || die "proof: cannot count series on the proof copy"
  [ "$live" = "$proofn" ] || die "proof: series count differs (live $live, copy $proofn); retry if a series was just added"
  [ "$live" -gt 0 ] || die "proof: zero series; refusing an empty parity"
  delta="$(ls "$PROC/$PROOF_PID/task" 2>/dev/null | wc -l | tr -d ' ')"
  [ "${delta:-0}" -gt 0 ] || die "proof: cannot read the task count of pid $PROOF_PID; refusing"
  kill "$PROOF_PID" 2>/dev/null; wait "$PROOF_PID" 2>/dev/null; PROOF_PID=""
  if [ $(( (before + delta) * 100 )) -ge $(( 70 * ceiling )) ]; then
    die "thread gate: $before + $delta tasks reaches 70% of the ceiling $ceiling; refusing the swap"
  fi
  mkdir -p "$SWAPDIR"
  printf '{"ok": true, "version": "%s", "before": %s, "delta": %s, "ceiling": %s, "series": %s, "rename_episodes": true, "at": "%s"}\n' \
    "$VERSION" "$before" "$delta" "$ceiling" "$live" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    | native_write_secure "$SWAPDIR/proof.json" 0644 || die "cannot record the proof"
  info "PROOF OK: status build $VERSION, renameEpisodes true, $live series on both; before=$before delta=$delta ceiling=$ceiling. Next: --swap --execute"
}

verify_native() {
  local n
  sleep "$SETTLE"
  sysd is-active "$UNIT" >/dev/null 2>&1 || { echo "unit not active" >&2; return 1; }
  sysd is-active "$FWD_SOCKET" >/dev/null 2>&1 || { echo "loopback socket not active" >&2; return 1; }
  [ -z "$(scan_pids container)" ] || { echo "a container process is running" >&2; return 1; }
  n="$(scan_pids native | wc -l | tr -d ' ')"
  [ "$n" -ge 1 ] || { echo "no native process" >&2; return 1; }
  native_listen_compare "$SLUG" >/dev/null || { echo "listen set differs from listen-set.before (minus recorded exceptions)" >&2; return 1; }
  # The API answers through the loopback forwarder with the pinned build and is not docker.
  api_ready() { local s; s="$(api_get "$PORT" system/status 2>/dev/null)" \
      && [ "$(printf '%s' "$s" | json_get version)" = "$VERSION" ] \
      && [ "$(printf '%s' "$s" | json_get isDocker)" = false ]; }
  wait_until "$API_TIMEOUT" api_ready || { echo "no pinned-build, non-docker status through :$LOOPBACK:$PORT within ${API_TIMEOUT}s" >&2; return 1; }
  rename_is_true "$PORT" || { echo "renameEpisodes is not true on the native app" >&2; return 1; }
  if [ -s "$SWAPDIR/series.count" ]; then
    [ "$(series_count "$PORT")" = "$(tr -d '[:space:]' < "$SWAPDIR/series.count")" ] \
      || { echo "series count differs from the pre-swap count in series.count" >&2; return 1; }
  fi
}

# Listen-set policy (spec 5.4). Required: loopback + the docker bridge. Any other
# captured address must be a concrete IP (never a wildcard) and becomes a recorded
# D-4 exception: Kestrel binds one address, so the public-IP listener is dropped
# (all ingress is nginx on loopback).
check_listen_set() {
  local f="$SWAPDIR/listen-set.before" a extras=()
  [ -s "$f" ] || die "listen set on :$PORT is empty; the container is not serving? refusing"
  grep -qx "$LOOPBACK:$PORT" "$f" || die "listen set lacks $LOOPBACK:$PORT ($(tr '\n' ' ' < "$f"))"
  grep -qx "$BRIDGE:$PORT" "$f"   || die "listen set lacks $BRIDGE:$PORT ($(tr '\n' ' ' < "$f"))"
  while IFS= read -r a; do
    a="${a%$'\r'}"
    case "$a" in
      "$LOOPBACK:$PORT"|"$BRIDGE:$PORT"|"") ;;
      0.0.0.0:*|'*:'*|'[::]:'*|:::*) die "wildcard listener $a in the recorded set; refusing (0.0.0.0 is never an option)" ;;
      *) extras+=("$a") ;;
    esac
  done < "$f"
  EXCEPTIONS=""
  if [ "${#extras[@]}" -gt 0 ]; then EXCEPTIONS="$(IFS=,; echo "${extras[*]}")"; fi
}

do_swap() {
  local st cls ss_state ver snap now soak key ub live_series
  [ -f "$SWAPDIR/proof.json" ] || die "no proof recorded; run --prove --execute first"
  [ -x "$APPDIR/bin/current/$EXE" ] && [ -f "$APPDIR/native/$UNIT" ] && [ -f "$APPDIR/native/$FWD_SOCKET" ] \
    && [ -f "$APPDIR/native/$FWD_UNIT" ] && [ -f "$ENV_DIR/$SLUG.env" ] \
    || die "not installed; run --install --execute first"
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state _ <<<"$st"
  [ "$cls" = systemd ] && [ "$ss_state" = pending-swap ] \
    || die "the deployed manifest is not the pending-swap flip (class=$cls swap_state=$ss_state); merge + deploy it via 240 first"

  if sysd is-active "$UNIT" >/dev/null 2>&1 && [ -z "$(scan_pids container)" ]; then
    info "already swapped; verifying only"
    make_hdr
    verify_native || die "native sonarr2 fails parity; run --rollback --execute"
    info "verified"
    return 0
  fi

  # Rollback step 4: unmask only at the next forward swap.
  unmask_all

  # Step 3: captures + audits.
  make_hdr
  ver="$(native_ucc_version "$SLUG")"
  ver="${ver#v}"
  case "$VERSION." in "$ver."*) ;; *) die "version parity: panel=$ver pinned=$VERSION" ;; esac
  api_build_is_pin "$PORT" || die "version parity: the container's API build is not $VERSION"
  # 5.5: the app's own config must equal the secrets the whole stack reads.
  [ "$(secret sonarr2.port)" = "$PORT" ] || die "secrets/sonarr2.port is not $PORT"
  # config.xml says "/sonarr2", the secret says "sonarr2" (box, 2026-10-10).
  ub="$(cfg_get UrlBase)"; ub="${ub#/}"; ub="${ub%/}"
  [ -n "$ub" ] && [ "$ub" = "$(secret sonarr2.urlbase)" ] || die "config.xml UrlBase differs from secrets/sonarr2.urlbase; aborting"
  key="$(cfg_get ApiKey)"
  [ -n "$key" ] && [ "$key" = "$(secret sonarr2.key)" ] || die "config.xml ApiKey differs from secrets/sonarr2.key; aborting"
  # Local-address auth bypass must be off: the callers move from 172.17.0.1 to
  # loopback/the bridge, both private, so the behaviour only matches if it is explicit.
  [ "$(cfg_get AuthenticationRequired)" = Enabled ] \
    || die "config.xml AuthenticationRequired is '$(cfg_get AuthenticationRequired)', not Enabled (local-address bypass); fix it first"
  # renameEpisodes must be true BEFORE and AFTER (arr-plex-parity, 2026-08-16).
  rename_is_true "$PORT" || die "renameEpisodes is not true on the live container; fix it first (arr-plex-parity)"
  queue_not_importing || die "the queue has an import in flight; wait for it to finish and retry"
  live_series="$(series_count "$PORT")" || die "cannot count series on the live container"
  [ "$live_series" -gt 0 ] || die "zero series on the live container; refusing"
  native_listen_capture "$SLUG" "$PORT" >/dev/null || die "listen-set capture failed"
  mkdir -p "$SWAPDIR"
  printf '%s\n' "$live_series" | native_write_secure "$SWAPDIR/series.count" 0644 || die "cannot record series.count"
  check_listen_set
  swapstate set "$SLUG" "ucc_version=$VERSION" >/dev/null || die "cannot record ucc_version"
  swapstate set "$SLUG" "exceptions=$EXCEPTIONS" >/dev/null || die "cannot record the listen-set exceptions"
  path_audit

  # Step 4: suppress the app and its canaries together.
  suppression add "${SUPPRESS[@]}" --reason "QFLX-30 swap to native" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to swap unsuppressed"

  # Step 6: stop the container; its exit is asynchronous Docker behaviour, so
  # the STATE decides (no container pid, port free, db idle), not the exit code.
  "$APPCTL" stop "$SLUG" >/dev/null 2>&1 || info "appctl stop returned non-zero; polling decides"
  if ! wait_until "$STOP_TIMEOUT" container_gone; then
    "$APPCTL" start "$SLUG" >/dev/null 2>&1 || true
    suppression remove "${SUPPRESS[@]}" >/dev/null || true
    die "the container did not exit within ${STOP_TIMEOUT}s (pids: $(scan_pids container | tr '\n' ' '); port $PORT free: $(port_free && echo yes || echo no)); swap aborted, container start requested, suppression lifted"
  fi
  # Step 5 (after the stop, never on a live WAL db): snapshot config + db; logs
  # and caches excluded. A failed snapshot hands the container back.
  snap="$SWAPDIR/snapshot-$(date -u +%Y%m%dT%H%M%SZ).tgz"
  if ! "${TAR[@]}" -czf "$snap" -C "$APPS" --exclude="$SLUG/bin" --exclude="$SLUG/native" \
      --exclude="$SLUG/logs" --exclude="$SLUG/logs.db*" --exclude="$SLUG/MediaCover" \
      --exclude="$SLUG/Backups" --exclude="$SLUG/*.prew-rectify" "$SLUG"; then
    "$APPCTL" start "$SLUG" >/dev/null 2>&1 || true
    suppression remove "${SUPPRESS[@]}" >/dev/null || true
    die "snapshot failed after the stop; container start requested, suppression lifted"
  fi
  # config.xml as the container left it: the byte-exact rollback copy.
  cp -p "$APPDIR/config.xml" "$SWAPDIR/config.xml.pre-native" || die "cannot keep config.xml.pre-native"
  mkdir -p "$UNIT_DIR"
  native_write_secure "$UNIT_DIR/$UNIT" 0644 < "$APPDIR/native/$UNIT" || die "unit install failed"
  native_write_secure "$UNIT_DIR/$FWD_SOCKET" 0644 < "$APPDIR/native/$FWD_SOCKET" || die "socket install failed"
  native_write_secure "$UNIT_DIR/$FWD_UNIT" 0644 < "$APPDIR/native/$FWD_UNIT" || die "forwarder install failed"
  sysd daemon-reload || die "daemon-reload failed"
  sysd enable --now "$UNIT" || die "enable --now $UNIT failed; run --rollback --execute"
  sysd enable --now "$FWD_SOCKET" || die "enable --now $FWD_SOCKET failed; run --rollback --execute"

  # Step 8 (in-place part): parity.
  if ! verify_native; then
    die "native sonarr2 fails parity after the swap; suppression kept ON; run --rollback --execute"
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
  make_hdr
  verify_native || die "native sonarr2 fails parity; run --rollback --execute"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "FINISHED: ${SUPPRESS[*]} unsuppressed; 14-day soak running"
}

do_rollback() {
  local isn u
  # Step 0: re-suppress + mask BEFORE anything stops.
  suppression add "${SUPPRESS[@]}" --reason "QFLX-30 rollback to UCC" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to roll back unsuppressed"
  mask_all
  # Step 1: stop the socket first (it would re-spawn the forwarder), then both services.
  for u in "$FWD_SOCKET" "$FWD_UNIT" "$UNIT"; do sysd stop "$u" >/dev/null 2>&1 || true; done
  wait_until "$STOP_TIMEOUT" native_gone || die "native sonarr2 did not stop within ${STOP_TIMEOUT}s"
  wait_until "$STOP_TIMEOUT" bind_clear  || die "port $PORT is still bound after stopping the native units"
  # Step 2: the DEPLOYED manifest must dispatch sonarr2 as UCC again.
  isn="$("$APPCTL" is-native "$SLUG" 2>/dev/null)"
  if [ "${isn%$'\r'}" != ucc ]; then
    echo "[305-sonarr2] PAUSED: native stopped + masked; revert the deployed manifest (PR + 240) so appctl dispatches $SLUG as UCC, then re-run --rollback --execute" >&2
    exit 10
  fi
  # Step 5 (config half): the container reads config.xml; hand back the exact bytes it left.
  if [ -f "$SWAPDIR/config.xml.pre-native" ] && ! cmp -s "$SWAPDIR/config.xml.pre-native" "$APPDIR/config.xml"; then
    cp -p "$APPDIR/config.xml" "$SWAPDIR/config.xml.native-era" || die "cannot keep the native-era config.xml"
    cp -p "$SWAPDIR/config.xml.pre-native" "$APPDIR/config.xml" || die "cannot restore config.xml"
    info "config.xml changed in the native era; restored the pre-swap bytes (native-era copy kept in $SWAPDIR)"
  fi
  # Step 3.
  "$APPCTL" start "$SLUG" >/dev/null 2>&1 || info "appctl start returned non-zero; polling decides"
  wait_until "$STOP_TIMEOUT" container_up || die "the container did not come back within ${STOP_TIMEOUT}s; still suppressed"
  sleep "$SETTLE"                # let it bind before the canaries and pusher look again
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "ROLLED BACK to UCC; ${ALL_UNITS[*]} masked (unmasked by the next --swap). elapsed=$((SECONDS - T0))s"
}

case "$MODE" in
  install)  do_install ;;
  prove)    do_prove ;;
  swap)     do_swap ;;
  finish)   do_finish ;;
  rollback) do_rollback ;;
esac
