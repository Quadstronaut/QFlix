#!/usr/bin/env bash
# 312-native-postgres-install.sh -- QFLX-37 (UCC divorce A13, convert postgres).
#
# Moves postgres (listmonk's database) off the Ultra.cc container manager (UCC)
# onto a user unit the repo owns: the PGDG debs of EXACTLY the container's
# build (the container's PG_VERSION env, 17.11-1.pgdg13+2 on 2026-10-10),
# sha256-pinned, unpacked with `dpkg-deb -x` into ~/.apps/pg-native/bin/<ver>,
# run as qflix-postgres.service on a fresh C.UTF-8 cluster in
# ~/.apps/pg-native/data. The data is COPIED (dump/restore), never used in
# place: the UCC data dir stays untouched as the rollback target.
# Spec: docs/superpowers/specs/2026-10-09-ucc-divorce-design.md 5.1-5.9, row 13
# of 6 and the "A13 postgres sequence". Pilot shape: 300-native-unpackerr.
#
# RUNS ON THE BOX. 240-maintenance-install.sh deploys it to ~/scripts/configure/
# with ~/scripts/lib/native.sh and ~/scripts/maint/pg_native.py (the tested
# text/decision helpers). Started from the workstation it re-executes the
# deployed copy over ssh.
#
# INERT BY DEFAULT (I-3). Every mode prints its plan (DRY-RUN) and touches
# nothing unless --execute is also given. Modes, in swap order:
#
#   --install   5.9 step 1. Fetch + sha256-verify the server and client debs,
#               refuse unless the RUNNING container's build equals the pin
#               (app-postgres has no `version` verb, box 2026-10-10: the
#               container postmaster's PG_VERSION env is the authority, read
#               from /proc, never a password), dpkg-deb -x both into
#               bin/<ver>, gate on ldd (no "not found") and on `postgres
#               --version`, flip `current`, write the env file and STAGE the
#               unit in ~/.apps/pg-native/native/. NOT enabled (I-6).
#   --prove     5.9 step 2 (A13 shape). From the RUNNING UCC postgres:
#               per-table row counts + sequence values, pg_dumpall
#               --globals-only + pg_dump -Fc of every database, counts again
#               (must be unchanged: the dump is then exactly those rows).
#               initdb a scratch C.UTF-8 cluster under ~/.apps/.prove/postgres
#               on a free 127.0.0.1 port, restore, compare counts + sequences,
#               SANITIZE its listmonk db (SMTP, messengers, bounce mailboxes
#               off; nothing running/scheduled) with ZERO asserted, boot a
#               scratch listmonk on another free loopback port against it and
#               read /api/campaigns (total == the restored campaigns count).
#               Task delta gated at 70% of the host ceiling (G-2). Everything
#               is destroyed afterwards. Writes swap/postgres/proof.json.
#   --swap      The A13 sequence (replaces 5.9 step 6). Needs the proof, the
#               DEPLOYED pending-swap flip, and refuses within 24h of the
#               Monday newsletter or while a campaign is running.
#                 capture  listen set on :42009 (all three binds are kept:
#                          postgres takes a list; a wildcard is refused) and
#                          check listmonk's [db] target is one of them
#                 suppress postgres + listmonk + their canaries
#                 hold     the heartbeat-listmonk + listmonk-sync crontab lines
#                          (the writers, with listmonk.service and its public
#                          subscribe form) and wait out a running sync
#                 stop     listmonk.service (state: inactive + port free)
#                 dump     counts, globals + -Fc of every db, counts again
#                          (unchanged = no writer left); the dumps ARE the
#                          snapshot (5.9 step 5)
#                 stop     UCC postgres via appctl; poll: no container pid,
#                          port free
#                 restore  fresh initdb, socket-only boot, globals (the
#                          bootstrap role filtered) + every db, counts and
#                          sequences compared to the dump, stop
#                 cutover  the recorded listen set, enable --now the unit,
#                          verify (unit, listen set, counts), start listmonk,
#                          /health 200, release the crontab hold
#               Any failure BEFORE the cutover puts UCC postgres + listmonk
#               back automatically (no data moved). After it: suppression
#               stays ON, run --rollback --execute.
#   --finish    5.9 step 9 after the follow-up PR dropped `swap_state` and 240
#               deployed it: verify, then lift the app + canaries together.
#   --rollback  0: re-suppress, park + MASK the unit. Hold the writers, stop
#               listmonk, dump the native databases (post-swap writes are
#               KEPT in swap/postgres/rollback-*, never silently dropped), stop
#               the unit. 2: the DEPLOYED manifest must dispatch postgres as UCC
#               (exit 10 otherwise). 3: appctl start postgres, wait for its
#               port; put listmonk's [db] host back if the swap re-pointed it;
#               start listmonk, /health, release the hold, unsuppress.
#   --post-upgrade VER   the tarball_swap (.deb) post step of the manifest
#               upgrade block: lifecycle unpacked the server deb into
#               bin/VER; check its major equals the cluster's PG_VERSION
#               (a major is a manual runbook: pg_upgrade, never this), ldd, the
#               binary runs, flip `current`. Runs INSIDE the upgrade sweep,
#               so it skips the window gate.
#
# The listmonk DB password is read from ~/.apps/listmonk/etc/config.toml by
# pg_native.py into a 0600 pgpass file under the swap dir; it never reaches
# argv, the environment of a long-lived process, stdout or a log.
#
# Exit: 0 ok | 1 refused/failed | 10 rollback paused for the manifest revert |
# 64 usage.
#
# Overrides (tests; resolved at call time): QFLIX_APPS_DIR QFLIX_UNIT_DIR
# QFLIX_ENV_DIR QFLIX_SWAP_DIR QFLIX_SECRETS_DIR MANITOBA_STATE_DIR QFLIX_MANIFEST
# QFLIX_PROC QFLIX_PYTHON QFLIX_APPCTL QFLIX_SYSTEMCTL QFLIX_SS QFLIX_PS
# QFLIX_PGREP QFLIX_CURL QFLIX_DPKG_DEB QFLIX_LDD QFLIX_CRONTAB QFLIX_HTTP
# QFLIX_PG_BIN QFLIX_LISTMONK_BIN QFLIX_LISTMONK_CONFIG QFLIX_HOSTPOLICY
# QFLIX_HOST_ID_FILE QFLIX_PG_SERVER_SHA256 QFLIX_PG_CLIENT_SHA256
# QFLIX_PG_PORT QFLIX_NOW QFLIX_POLL_S QFLIX_SETTLE_S QFLIX_STOP_TIMEOUT_S
# QFLIX_PROOF_TIMEOUT_S QFLIX_KEEP_PROOF.
set -uo pipefail

SLUG=postgres                # manifest app / ucc_slug / swap-state key
NDIR=pg-native               # ~/.apps/<NDIR>: the native tree (spec 5.1 exception)
PG_MAJOR=17
VERSION="17.11-1.pgdg13+2"   # == versions.env POSTGRES_VERSION (test-pinned)
SERVER_SHA256="4f8b42bd3202d24953996743afbc90b609d3337c90bb740568b52d06ebfdcb15"
CLIENT_SHA256="c36408bb62178bc9193c113da65e30fc6a5237648de5e9db1ea594214df9ae4b"
POOL="https://apt.postgresql.org/pub/repos/apt/pool/main/p/postgresql-${PG_MAJOR}"
SERVER_URL="${POOL}/postgresql-${PG_MAJOR}_${VERSION}_amd64.deb"
CLIENT_URL="${POOL}/postgresql-client-${PG_MAJOR}_${VERSION}_amd64.deb"
UNIT="qflix-${SLUG}.service"
FAMILY=db
EXE="usr/lib/postgresql/${PG_MAJOR}/bin/postgres"
EXEC_ARGS="-D %h/.apps/${NDIR}/data"
# Muted with the app (plan A-table row A13). Keys follow cli.py: canary-<name>.
# cron-liveness: the crontab hold comments two declared lines out for minutes.
SUPPRESS=("$SLUG" "listmonk" "canary-thread-ceiling" "canary-cron-liveness")
HOLD_RE='heartbeat-listmonk\.sh|listmonk-sync\.py'
HOLD_MARK='#QFLX-37-HOLD# '
LM_UNIT=listmonk.service
PATTERN="postgres"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # .../scripts
ARGS=("$@")

info() { echo "[312-postgres] $*"; }
die()  { echo "[312-postgres] ERROR: $*" >&2; exit 1; }
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
APPDIR="$APPS/$NDIR"
DATA="$APPDIR/data"
RUN="$APPDIR/run"
UNIT_DIR="${QFLIX_UNIT_DIR:-$HOME/.config/systemd/user}"
ENV_DIR="${QFLIX_ENV_DIR:-$HOME/.config/qflix}"
ENV_FILE="$ENV_DIR/$NDIR.env"
SWAPDIR="${QFLIX_SWAP_DIR:-$HOME/.opt/maint/swap}/$SLUG"
SECRETS="${QFLIX_SECRETS_DIR:-$HOME/secrets}"
PROVE="$APPS/.prove/$SLUG"
MANIFEST="${QFLIX_MANIFEST:-$HOME/.opt/maint/apps.yaml}"
MAINT_LIB="$HERE/maint/lib"
PGN="$HERE/maint/pg_native.py"
PY="${QFLIX_PYTHON:-python3}"
APPCTL="${QFLIX_APPCTL:-$HOME/bin/appctl}"
SYSTEMCTL="${QFLIX_SYSTEMCTL:-systemctl}"
SS="${QFLIX_SS:-ss}"
PS="${QFLIX_PS:-ps}"
PGREP="${QFLIX_PGREP:-pgrep}"
DPKG_DEB="${QFLIX_DPKG_DEB:-dpkg-deb}"
LDD="${QFLIX_LDD:-ldd}"
CRONTAB="${QFLIX_CRONTAB:-crontab}"
PROC="${QFLIX_PROC:-/proc}"
PGBIN="${QFLIX_PG_BIN:-$APPDIR/bin/current/usr/lib/postgresql/$PG_MAJOR/bin}"
LM_BIN="${QFLIX_LISTMONK_BIN:-$APPS/listmonk/bin/listmonk}"
LM_CONFIG="${QFLIX_LISTMONK_CONFIG:-$APPS/listmonk/etc/config.toml}"
POLL="${QFLIX_POLL_S:-2}"
SETTLE="${QFLIX_SETTLE_S:-10}"
STOP_TIMEOUT="${QFLIX_STOP_TIMEOUT_S:-120}"
PROOF_TIMEOUT="${QFLIX_PROOF_TIMEOUT_S:-300}"
WANT_SERVER_SHA="${QFLIX_PG_SERVER_SHA256:-$SERVER_SHA256}"
WANT_CLIENT_SHA="${QFLIX_PG_CLIENT_SHA256:-$CLIENT_SHA256}"
# The container's published port (manifest postgres.port secret; listmonk [db]).
PORT="${QFLIX_PG_PORT:-42009}"
PGPASS="$SWAPDIR/.pgpass"

hostpolicy() {
  if [ -n "${QFLIX_HOSTPOLICY:-}" ]; then "$QFLIX_HOSTPOLICY" "$@"
  else "$PY" "$MAINT_LIB/hostpolicy.py" "$@"; fi
}
swapstate()   { "$PY" "$MAINT_LIB/swapstate.py" "$@"; }
suppression() { "$PY" "$MAINT_LIB/suppression.py" "$@"; }
pgn()         { "$PY" "$PGN" "$@"; }
sysd()        { "$SYSTEMCTL" --user "$@"; }

# --- plan (DRY-RUN) ------------------------------------------------------------
plan() {
  info "DRY-RUN mode=$MODE (nothing touched; add --execute to run)"
  case "$MODE" in
    install)  info "would fetch postgresql-$PG_MAJOR + postgresql-client-$PG_MAJOR $VERSION (sha256 $WANT_SERVER_SHA / $WANT_CLIENT_SHA), check the container build, dpkg-deb -x into $APPDIR/bin/$VERSION, ldd gate, write $ENV_FILE, stage $APPDIR/native/$UNIT (not enabled)" ;;
    prove)    info "would count + dump the RUNNING UCC postgres (globals + -Fc per db), restore into a scratch cluster under $PROVE on a free 127.0.0.1 port, compare counts + sequences, sanitize its listmonk db (zero), read /api/campaigns from a scratch listmonk, gate the task delta at 70%, then delete everything" ;;
    swap)     info "would gate on the newsletter + running campaigns, capture the listen set (:$PORT), suppress ${SUPPRESS[*]}, hold the listmonk crontab lines, stop $LM_UNIT, count + dump, stop UCC postgres, initdb + restore + compare in $DATA, enable --now $UNIT on the recorded listen set, start $LM_UNIT, release the hold, record swap state" ;;
    finish)   info "would verify the native unit + listmonk and lift suppression for ${SUPPRESS[*]}" ;;
    rollback) info "would suppress ${SUPPRESS[*]}, park + mask $UNIT, hold the writers, stop $LM_UNIT, dump the native dbs into $SWAPDIR, stop $UNIT, wait for the manifest revert, start UCC postgres via appctl, start $LM_UNIT, release, unsuppress" ;;
    post-upgrade) info "would check $APPDIR/bin/$UPGRADE_VER (same major as $DATA/PG_VERSION, ldd, --version) and flip bin/current" ;;
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
  sshm "~/scripts/configure/312-native-postgres-install.sh $(printf '%q ' "${ARGS[@]}")"
  exit $?
fi

# shellcheck source=/dev/null
source "$HERE/lib/native.sh" || die "cannot source $HERE/lib/native.sh (run 240 first)"
[ -f "$PGN" ] || die "$PGN missing (run 240 first)"

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

# One EXIT trap for every temp path / proof process / held crontab.
CLEANUP_PATHS=()
PROOF_PG_DATA=""
PROOF_LM_PID=""
proof_survivors() { "$PGREP" -u "$(id -u)" -f -- "$PROVE/" 2>/dev/null; }
stop_proof() {
  local i=0 pids
  if [ -n "$PROOF_LM_PID" ]; then
    kill -TERM -- "-$PROOF_LM_PID" 2>/dev/null || kill -TERM "$PROOF_LM_PID" 2>/dev/null
    wait "$PROOF_LM_PID" 2>/dev/null
    PROOF_LM_PID=""
  fi
  if [ -n "$PROOF_PG_DATA" ]; then
    "$PGBIN/pg_ctl" -D "$PROOF_PG_DATA" -m fast -w -t 60 stop >/dev/null 2>&1
    PROOF_PG_DATA=""
  fi
  while pids="$(proof_survivors)" && [ -n "$pids" ]; do
    i=$((i + 1))
    # shellcheck disable=SC2086
    if [ "$i" -ge 10 ]; then kill -KILL $pids 2>/dev/null; else kill -TERM $pids 2>/dev/null; fi
    if [ "$i" -ge 20 ]; then
      # shellcheck disable=SC2086
      echo "[312-postgres] ERROR: proof processes survived: "$pids >&2
      return 1
    fi
    sleep 1
  done
}
cleanup() {
  stop_proof
  local p
  for p in "${CLEANUP_PATHS[@]}"; do rm -rf "$p"; done
}
trap cleanup EXIT

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

# PIDs under OUR uid whose cmdline starts with the app pattern. kind=container:
# in a container cgroup (docker/libpod/...). kind=native: in the unit's cgroup.
scan_pids() {
  local kind="$1" uid d pid cg cmd
  uid="$(id -u)"
  for d in "$PROC"/[0-9]*; do
    [ -d "$d" ] || continue
    pid="${d##*/}"
    [ "$(awk '/^Uid:/ {print $2; exit}' "$d/status" 2>/dev/null)" = "$uid" ] || continue
    cg="$(cat "$d/cgroup" 2>/dev/null)" || continue
    cmd="$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)" || continue
    case "$cmd" in "$PATTERN"*|*"/bin/$PATTERN "*) ;; *) continue ;; esac
    if [ "$kind" = native ]; then
      case "$cg" in *"$UNIT"*) echo "$pid" ;; esac
    else
      case "$cg" in *"$UNIT"*) continue ;; esac
      if [[ "$cg" =~ docker|libpod|podman|containerd|crio ]]; then echo "$pid"; fi
    fi
  done
}

# The container's build, from its postmaster's environment (cmdline exactly
# "postgres": the children rename themselves "postgres: <role>"). Only the
# PG_VERSION variable is read.
ucc_build() {
  local pid first
  for pid in $(scan_pids container); do
    first="$(tr '\0' '\n' < "$PROC/$pid/cmdline" 2>/dev/null | head -n 1)"
    [ "$first" = postgres ] || continue
    tr '\0' '\n' < "$PROC/$pid/environ" 2>/dev/null | sed -n 's/^PG_VERSION=//p' | head -n 1
    return 0
  done
}

port_free() { ! "$SS" -tlnH "sport = :$1" 2>/dev/null | grep -q ":$1\b"; }
port_up()   { "$SS" -tlnH "sport = :$1" 2>/dev/null | grep -q ":$1\b"; }

wait_until() {
  local limit="$1"; shift
  local start=$SECONDS
  while :; do
    "$@" && return 0
    [ $((SECONDS - start)) -ge "$limit" ] && return 1
    sleep "$POLL"
  done
}
container_gone() { [ -z "$(scan_pids container)" ] && port_free "$PORT"; }
container_up()   { [ -n "$(scan_pids container)" ] && port_up "$PORT"; }
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

# HTTP GET on loopback; prints the body, rc 0 only on 200. With a secrets dir,
# listmonk's API token is read from files inside python (never argv).
http_get() {
  if [ -n "${QFLIX_HTTP:-}" ]; then "$QFLIX_HTTP" "$@"; return $?; fi
  "$PY" - "$@" <<'PY'
import base64, os, sys, urllib.request
url = sys.argv[1]
req = urllib.request.Request(url)
if len(sys.argv) > 2:
    d = sys.argv[2]
    u = open(os.path.join(d, "listmonk.api_user"), encoding="utf-8").read().strip()
    t = open(os.path.join(d, "listmonk.api_token"), encoding="utf-8").read().strip()
    req.add_header("Authorization", "token %s:%s" % (u, t))
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode("utf-8", "replace")
        if r.status != 200:
            sys.exit(1)
except Exception:
    sys.exit(1)
sys.stdout.write(body)
PY
}

free_port() {
  "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

# psql against the UCC postgres (TCP, password via the 0600 pgpass) or the
# native one (its private unix socket). -X: no ~/.psqlrc. Output: -tA -F'|'.
ucc_psql() {   # DB SQL
  PGPASSFILE="$PGPASS" "$PGBIN/psql" -X -q -tA -F'|' -v ON_ERROR_STOP=1 \
    -h 127.0.0.1 -p "$PORT" -U "$DBUSER" -d "$1" -c "$2"
}
sock_psql() {  # SOCKDIR PORT DB SQL
  "$PGBIN/psql" -X -q -tA -F'|' -v ON_ERROR_STOP=1 -h "$1" -p "$2" -U "$DBUSER" -d "$3" -c "$4"
}

# Database names become file names and SQL identifiers: plain only.
valid_db() { [[ "$1" =~ ^[A-Za-z0-9_]{1,63}$ ]]; }

# snapshot OUTJSON PSQLFN [ARGS...]: counts + sequences of every database.
snapshot() {
  local out="$1"; shift
  local dir dbs db
  dir="$(mktemp -d "$SWAPDIR/.snap.XXXXXX")" || return 1
  dbs="$("$@" postgres "$(pgn sql dbs)")" || { rm -rf "$dir"; echo "cannot list databases" >&2; return 1; }
  for db in $dbs; do
    db="${db%$'\r'}"
    valid_db "$db" || { rm -rf "$dir"; echo "odd database name '$db'; refusing" >&2; return 1; }
    "$@" "$db" "$(pgn sql counts)" > "$dir/$db.counts" || { rm -rf "$dir"; return 1; }
    "$@" "$db" "$(pgn sql seqs)" > "$dir/$db.seqs" || { rm -rf "$dir"; return 1; }
  done
  pgn collect "$dir" > "$out"; local rc=$?
  rm -rf "$dir"
  return $rc
}

# dump_ucc DIR: globals + one -Fc per database from the RUNNING UCC postgres.
dump_ucc() {
  local dir="$1" db
  mkdir -p "$dir" && chmod 0700 "$dir" || return 1
  PGPASSFILE="$PGPASS" "$PGBIN/pg_dumpall" -h 127.0.0.1 -p "$PORT" -U "$DBUSER" \
    --globals-only -f "$dir/globals.sql" || { echo "pg_dumpall --globals-only failed" >&2; return 1; }
  ucc_psql postgres "$(pgn sql dbowners)" > "$dir/databases" || return 1
  while IFS='|' read -r db _; do
    db="${db%$'\r'}"; [ -n "$db" ] || continue
    valid_db "$db" || { echo "odd database name '$db'; refusing" >&2; return 1; }
    PGPASSFILE="$PGPASS" "$PGBIN/pg_dump" -h 127.0.0.1 -p "$PORT" -U "$DBUSER" \
      -Fc -d "$db" -f "$dir/$db.dump" || { echo "pg_dump $db failed" >&2; return 1; }
  done < "$dir/databases"
  [ -s "$dir/databases" ] || { echo "no databases listed" >&2; return 1; }
}

# fresh_cluster DATADIR SOCKDIR PORT LISTEN: initdb (C.UTF-8, the bootstrap
# superuser = listmonk's role), our conf block, our hba. Never over a cluster.
fresh_cluster() {
  local d="$1" sock="$2" port="$3" listen="$4"
  [ ! -e "$d/PG_VERSION" ] || { echo "$d already holds a cluster" >&2; return 1; }
  mkdir -p "$sock" && chmod 0700 "$sock" || return 1
  "$PGBIN/initdb" -D "$d" -U "$DBUSER" -E UTF8 --locale=C.UTF-8 \
    --auth-local=trust --auth-host=scram-sha-256 >/dev/null || { echo "initdb failed" >&2; return 1; }
  pgn conf "$listen" "$port" "$sock" >> "$d/postgresql.conf" || return 1
  pgn hba > "$d/pg_hba.conf" || return 1
}

# restore_into SOCKDIR PORT DUMPDIR: globals (bootstrap role filtered) then every
# database: CREATE DATABASE ... OWNER as the source's pg_database recorded it
# (DUMPDIR/databases, "db|owner"), then pg_restore.
restore_into() {
  local sock="$1" port="$2" dir="$3" db owner
  pgn globals-filter "$DBUSER" "$dir/globals.sql" "$dir/globals.filtered.sql" || return 1
  "$PGBIN/psql" -X -q -v ON_ERROR_STOP=1 -h "$sock" -p "$port" -U "$DBUSER" -d postgres \
    -f "$dir/globals.filtered.sql" >/dev/null || { echo "globals restore failed" >&2; return 1; }
  while IFS='|' read -r db owner; do
    db="${db%$'\r'}"; owner="${owner%$'\r'}"; [ -n "$db" ] || continue
    valid_db "$db" || return 1
    valid_db "$owner" || { echo "odd owner '$owner' of $db; refusing" >&2; return 1; }
    if [ "$db" != postgres ]; then
      sock_psql "$sock" "$port" postgres \
        "/*qflix:createdb*/ create database \"$db\" owner \"$owner\" template template0 encoding 'UTF8'" \
        >/dev/null || { echo "create database $db failed" >&2; return 1; }
    fi
    "$PGBIN/pg_restore" -h "$sock" -p "$port" -U "$DBUSER" -d "$db" \
      --exit-on-error --single-transaction "$dir/$db.dump" || { echo "pg_restore $db failed" >&2; return 1; }
  done < "$dir/databases"
}

# The crontab writers (heartbeat-listmonk restarts a stopped listmonk within
# 5 min; listmonk-sync writes subscribers at 04:00). Commented out with a
# marker, put back verbatim by release. crontab.before keeps the original.
cron_hold() {
  local cur
  cur="$("$CRONTAB" -l 2>/dev/null)" || { echo "crontab -l failed" >&2; return 1; }
  mkdir -p "$SWAPDIR"
  [ -s "$SWAPDIR/crontab.before" ] || printf '%s\n' "$cur" | native_write_secure "$SWAPDIR/crontab.before" 0600 || return 1
  printf '%s\n' "$cur" | awk -v re="$HOLD_RE" -v mark="$HOLD_MARK" \
    'index($0, mark) != 1 && $0 !~ /^[[:space:]]*#/ && $0 ~ re { print mark $0; next } { print }' \
    | "$CRONTAB" - || { echo "crontab install failed" >&2; return 1; }
  # Guard the STATE, not the exit code: no live line may still match.
  if "$CRONTAB" -l 2>/dev/null | grep -v '^[[:space:]]*#' | grep -Eq "$HOLD_RE"; then
    echo "crontab still runs a listmonk writer after the hold" >&2; return 1
  fi
}
cron_release() {
  local cur
  cur="$("$CRONTAB" -l 2>/dev/null)" || return 1
  printf '%s\n' "$cur" | awk -v mark="$HOLD_MARK" \
    'index($0, mark) == 1 { print substr($0, length(mark) + 1); next } { print }' \
    | "$CRONTAB" - || return 1
  ! "$CRONTAB" -l 2>/dev/null | grep -qF "$HOLD_MARK"
}
sync_idle() { ! "$PGREP" -u "$(id -u)" -f 'listmonk-sync\.py' >/dev/null 2>&1; }

lm_port()  { local a; a="$(pgn lmaddr "$LM_CONFIG")" || return 1; printf '%s' "${a##*:}"; }
lm_down()  { ! sysd is-active "$LM_UNIT" >/dev/null 2>&1 && port_free "$LMPORT"; }
lm_healthy() { http_get "http://127.0.0.1:$LMPORT/health" >/dev/null 2>&1; }

# Read listmonk's [db] target and write the pgpass. Sets DBHOST DBPORT DBUSER DBNAME.
load_lmconf() {
  local row
  row="$(pgn lmconf "$LM_CONFIG")" || die "cannot read [db] from $LM_CONFIG"
  IFS='|' read -r DBHOST DBPORT DBUSER DBNAME <<<"${row%$'\r'}"
  [ "$DBPORT" = "$PORT" ] || die "listmonk [db] port $DBPORT != postgres port $PORT"
  mkdir -p "$SWAPDIR" || die "cannot create $SWAPDIR"
  pgn pgpass "$LM_CONFIG" "$PGPASS" || die "cannot write the pgpass file"
  CLEANUP_PATHS+=("$PGPASS")
}

ldd_gate() {   # DIR: every binary we run must resolve all its libraries
  local b out
  for b in postgres initdb pg_ctl psql pg_dump pg_dumpall pg_restore; do
    [ -x "$1/$b" ] || { echo "$b missing in $1" >&2; return 1; }
    out="$("$LDD" "$1/$b" 2>&1)" || { echo "ldd $b failed" >&2; return 1; }
    if printf '%s\n' "$out" | grep -q 'not found'; then
      echo "ldd $b: $(printf '%s\n' "$out" | grep 'not found' | head -3 | tr '\n' ' ')" >&2; return 1
    fi
  done
  out="$("$1/postgres" --version 2>&1)" || { echo "postgres --version failed" >&2; return 1; }
  case "$out" in *"PostgreSQL) ${2%%-*}"*) ;; *) echo "postgres --version says '$out', want ${2%%-*}" >&2; return 1 ;; esac
}

# --- modes ----------------------------------------------------------------------------
do_install() {
  local stage build
  mkdir -p "$APPS" || die "cannot create $APPS"
  build="$(ucc_build)"
  [ -n "$build" ] || die "cannot read the running container's PG_VERSION; is UCC postgres up?"
  [ "$build" = "$VERSION" ] || die "version parity: container build $build != pin $VERSION (I-10)"
  stage="$(mktemp -d "$APPS/.stage-$NDIR.XXXXXX")" || die "mktemp failed"
  CLEANUP_PATHS+=("$stage")
  native_fetch_verify "$SERVER_URL" "$WANT_SERVER_SHA" "$stage/server.deb" || die "server deb fetch/sha256 verify failed"
  native_fetch_verify "$CLIENT_URL" "$WANT_CLIENT_SHA" "$stage/client.deb" || die "client deb fetch/sha256 verify failed"
  mkdir -p "$stage/x"
  "$DPKG_DEB" -x "$stage/server.deb" "$stage/x" || die "dpkg-deb -x server failed"
  "$DPKG_DEB" -x "$stage/client.deb" "$stage/x" || die "dpkg-deb -x client failed"
  ldd_gate "$stage/x/usr/lib/postgresql/$PG_MAJOR/bin" "$VERSION" || die "binary gate failed (see above)"
  if [ ! -d "$APPDIR/bin/$VERSION" ]; then
    mkdir -p "$APPDIR/bin" || die "cannot create $APPDIR/bin"
    mv "$stage/x" "$APPDIR/bin/$VERSION" || die "cannot place bin/$VERSION"
  fi
  native_link_current "$NDIR" "$VERSION" || die "cannot flip bin/current"
  native_render_env "$NDIR" "$FAMILY" "$VERSION" "LANG=C.UTF-8" \
    | native_write_secure "$ENV_FILE" 0600 || die "env file write failed"
  native_render_unit "$NDIR" "$FAMILY" "$EXE" "$EXEC_ARGS" \
    | native_write_secure "$APPDIR/native/$UNIT" 0644 || die "unit staging failed"
  info "installed $VERSION; unit staged at $APPDIR/native/$UNIT (not enabled). Next: --prove --execute"
}

do_prove() {
  local before after delta ceiling pport lport src dump body total want left diffs
  [ -x "$PGBIN/postgres" ] && [ -f "$ENV_FILE" ] && [ -f "$APPDIR/native/$UNIT" ] \
    || die "not installed; run --install --execute first"
  [ -x "$LM_BIN" ] || die "listmonk binary $LM_BIN missing"
  ceiling="$(hostpolicy task-ceiling)" || die "task ceiling unknown; refusing (G-2)"
  ceiling="${ceiling%$'\r'}"
  load_lmconf
  rm -rf "$PROVE"
  mkdir -p "$PROVE" && chmod 0700 "$PROVE" || die "cannot create $PROVE"
  [ "${QFLIX_KEEP_PROOF:-0}" = 1 ] || CLEANUP_PATHS+=("$PROVE")

  # Source snapshot, dump, snapshot again: equal = the dump holds exactly
  # these rows (listmonk keeps running during a proof).
  src="$PROVE/source.json"
  snapshot "$src" ucc_psql || die "cannot count the UCC databases"
  dump="$PROVE/dump"
  dump_ucc "$dump" || die "dump of the running UCC postgres failed"
  snapshot "$PROVE/source.after.json" ucc_psql || die "cannot re-count the UCC databases"
  diffs="$(pgn compare "$src" "$PROVE/source.after.json")" \
    || die "the source changed during the dump (listmonk wrote); re-run --prove: $(echo "$diffs" | head -3 | tr '\n' ' ')"

  pport="$(free_port)" || die "no free loopback port"
  fresh_cluster "$PROVE/data" "$PROVE/run" "$pport" "127.0.0.1" || die "scratch initdb failed"
  before="$(user_tasks)"
  [[ "$before" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  PROOF_PG_DATA="$PROVE/data"
  "$PGBIN/pg_ctl" -D "$PROVE/data" -l "$PROVE/pg.log" -w -t 120 start >/dev/null \
    || { tail -5 "$PROVE/pg.log" >&2 2>/dev/null; die "scratch postgres did not start"; }
  restore_into "$PROVE/run" "$pport" "$dump" || die "restore into the scratch cluster failed"
  snapshot "$PROVE/restored.json" sock_psql "$PROVE/run" "$pport" || die "cannot count the scratch cluster"
  diffs="$(pgn compare "$src" "$PROVE/restored.json")" \
    || die "restored counts/sequences differ: $(echo "$diffs" | head -5 | tr '\n' ' ')"

  # Inert BEFORE anything can read it (I-11): no SMTP, messenger, mailbox, and
  # nothing running or scheduled.
  sock_psql "$PROVE/run" "$pport" "$DBNAME" "$(pgn sql sanitize)" >/dev/null || die "sanitize failed"
  left="$(sock_psql "$PROVE/run" "$pport" "$DBNAME" "$(pgn sql sanitize-check)")" || die "sanitize check failed"
  [ "${left%$'\r'}" = 0 ] || die "sanitize left $left sender/campaign switches on; not booting listmonk"
  want="$(sock_psql "$PROVE/run" "$pport" "$DBNAME" "$(pgn sql campaigns)")" || die "cannot count campaigns"
  want="${want%$'\r'}"

  lport="$(free_port)" || die "no free loopback port"
  pgn scratch-toml "$LM_CONFIG" "$PROVE/listmonk.toml" "$pport" "127.0.0.1:$lport" || die "cannot write the scratch listmonk config"
  (
    cd "$PROVE" || exit 1
    SETSID=()
    command -v setsid >/dev/null 2>&1 && SETSID=(setsid)
    exec "${SETSID[@]}" "$LM_BIN" --config "$PROVE/listmonk.toml" >"$PROVE/listmonk.log" 2>&1
  ) &
  PROOF_LM_PID=$!
  sleep "$SETTLE"
  local deadline=$((SECONDS + PROOF_TIMEOUT))
  until http_get "http://127.0.0.1:$lport/health" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      tail -5 "$PROVE/listmonk.log" >&2 2>/dev/null
      die "proof: scratch listmonk /health never answered 200 within ${PROOF_TIMEOUT}s"
    fi
    sleep "$POLL"
  done
  body="$(http_get "http://127.0.0.1:$lport/api/campaigns?per_page=1" "$SECRETS")" \
    || die "proof: scratch listmonk /api/campaigns did not answer 200"
  total="$(printf '%s' "$body" | "$PY" -c 'import json,sys; print((json.load(sys.stdin).get("data") or {}).get("total", ""))' 2>/dev/null)"
  [ "$total" = "$want" ] || die "proof: listmonk reads $total campaigns, the restored db has $want"
  after="$(user_tasks)"
  [[ "$after" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  delta=$((after - before))
  stop_proof || die "proof: processes survived under $PROVE; not recording a proof"
  [ "$delta" -gt 0 ] || die "proof: measured task delta $delta; cannot gate the swap; refusing"
  if [ $(( (before + delta) * 100 )) -ge $(( 70 * ceiling )) ]; then
    die "thread gate: $before + $delta tasks reaches 70% of the ceiling $ceiling; refusing the swap"
  fi
  printf '{"ok": true, "version": "%s", "before": %s, "delta": %s, "ceiling": %s, "campaigns": %s, "at": "%s"}\n' \
    "$VERSION" "$before" "$delta" "$ceiling" "$want" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    | native_write_secure "$SWAPDIR/proof.json" 0644 || die "cannot record the proof"
  info "PROOF OK: restored counts + sequences equal, sanitized copy inert, listmonk reads $total campaigns; before=$before delta=$delta ceiling=$ceiling. Next: --swap --execute"
}

verify_native() {
  local n
  sleep "$SETTLE"
  sysd is-active "$UNIT" >/dev/null 2>&1 || { echo "unit not active" >&2; return 1; }
  [ -z "$(scan_pids container)" ] || { echo "a container process is running" >&2; return 1; }
  n="$(scan_pids native | wc -l | tr -d ' ')"
  [ "$n" -ge 1 ] || { echo "no native process" >&2; return 1; }
  native_listen_compare "$SLUG" >/dev/null || { echo "listen set differs from listen-set.before" >&2; return 1; }
  sock_psql "$RUN" "$PORT" postgres "select 1" >/dev/null || { echo "native postgres does not answer on $RUN" >&2; return 1; }
}

# Put UCC postgres + listmonk back after a failure BEFORE the cutover. Nothing
# was written anywhere (the UCC data dir was never opened by us).
abort_swap() {
  local why="$1"
  if [ -n "${CUT_PG_STARTED:-}" ]; then
    "$PGBIN/pg_ctl" -D "$DATA" -m fast -w -t 60 stop >/dev/null 2>&1 || true
  fi
  if [ -n "${UCC_STOPPED:-}" ]; then
    "$APPCTL" start "$SLUG" >/dev/null 2>&1 || true
    wait_until "$STOP_TIMEOUT" container_up || why="$why; UCC postgres did NOT come back"
  fi
  if [ -n "${LM_STOPPED:-}" ]; then
    sysd start "$LM_UNIT" >/dev/null 2>&1 || true
  fi
  cron_release >/dev/null 2>&1 || why="$why; crontab hold NOT released (lines marked '$HOLD_MARK')"
  suppression remove "${SUPPRESS[@]}" >/dev/null || true
  die "$why; swap aborted, UCC postgres + listmonk restarted, hold released, suppression lifted"
}

do_swap() {
  local st cls ss_state build listen sp diffs dumpdir now soak running
  [ -f "$SWAPDIR/proof.json" ] || die "no proof recorded; run --prove --execute first"
  [ -x "$PGBIN/postgres" ] && [ -f "$APPDIR/native/$UNIT" ] && [ -f "$ENV_FILE" ] \
    || die "not installed; run --install --execute first"
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state _ <<<"$st"
  [ "$cls" = systemd ] && [ "$ss_state" = pending-swap ] \
    || die "the deployed manifest is not the pending-swap flip (class=$cls swap_state=$ss_state); merge + deploy it via 240 first"
  load_lmconf
  LMPORT="$(lm_port)" || die "cannot read listmonk's [app] address"

  if sysd is-active "$UNIT" >/dev/null 2>&1 && [ -z "$(scan_pids container)" ]; then
    info "already swapped; verifying only"
    verify_native || die "native postgres fails parity; run --rollback --execute"
    info "verified"
    return 0
  fi

  # Gates: never within 24h of the newsletter, never under a running campaign.
  if [ -n "${QFLIX_NOW:-}" ]; then pgn newsletter-gate --now "$QFLIX_NOW" >/dev/null
  else pgn newsletter-gate >/dev/null; fi
  case $? in
    0) ;;
    1) die "within 24h of the Monday newsletter (Mon 15:00 UTC); swap refused" ;;
    *) die "newsletter gate unreadable; refusing" ;;
  esac
  running="$(ucc_psql "$DBNAME" "$(pgn sql running)")" || die "cannot read campaign state from UCC postgres"
  [ "${running%$'\r'}" = 0 ] || die "$running listmonk campaign(s) running; swap refused"

  if is_masked; then
    sysd unmask "$UNIT" || die "unmask $UNIT failed"
    rm -f "$ENV_DIR/parked-units/$UNIT"
  fi

  # Step 3: capture + checks.
  if [ -s "$SECRETS/postgres.port" ]; then
    sp="$(tr -d '[:space:]' < "$SECRETS/postgres.port")"
    [ "$sp" = "$PORT" ] || die "secrets/postgres.port ($sp) != probe port ($PORT)"
  fi
  build="$(ucc_build)"
  [ "$build" = "$VERSION" ] || die "version parity: container build '${build:-unreadable}' != pin $VERSION"
  native_listen_capture "$SLUG" "$PORT" >/dev/null || die "listen-set capture failed"
  listen="$(pgn listen-addrs "$SWAPDIR/listen-set.before" "$PORT")" \
    || die "listen set on :$PORT unusable (must be non-empty, no wildcard)"
  swapstate set "$SLUG" "ucc_version=$VERSION" >/dev/null || die "cannot record ucc_version"
  REPOINT=""
  if ! pgn in-listen "$SWAPDIR/listen-set.before" "$DBHOST" "$PORT"; then
    REPOINT=127.0.0.1
    pgn in-listen "$SWAPDIR/listen-set.before" "$REPOINT" "$PORT" \
      || die "listmonk [db] host $DBHOST is not in the listen set and 127.0.0.1 is not either"
  fi

  # Step 4: suppress the app + its dependants together.
  suppression add "${SUPPRESS[@]}" --reason "QFLX-37 swap to native" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to swap unsuppressed"

  # A13.1: stop the writers.
  cron_hold || abort_swap "cannot hold the listmonk crontab lines"
  wait_until "$STOP_TIMEOUT" sync_idle || abort_swap "listmonk-sync.py is still running"
  LM_STOPPED=1
  sysd stop "$LM_UNIT" >/dev/null 2>&1 || info "systemctl stop $LM_UNIT returned non-zero; polling decides"
  wait_until "$STOP_TIMEOUT" lm_down || abort_swap "$LM_UNIT did not stop"

  # A13.2: counts, dump (the snapshot), counts again: equal = no writer left.
  dumpdir="$SWAPDIR/dump-$(date -u +%Y%m%dT%H%M%SZ)"
  snapshot "$SWAPDIR/counts.before.json" ucc_psql || abort_swap "cannot count the UCC databases"
  dump_ucc "$dumpdir" || abort_swap "dump failed"
  snapshot "$SWAPDIR/counts.after-dump.json" ucc_psql || abort_swap "cannot re-count the UCC databases"
  diffs="$(pgn compare "$SWAPDIR/counts.before.json" "$SWAPDIR/counts.after-dump.json")" \
    || abort_swap "something still writes during the dump: $(echo "$diffs" | head -3 | tr '\n' ' ')"

  # A13.3: stop UCC postgres; the STATE decides (no container pid, port free).
  UCC_STOPPED=1
  "$APPCTL" stop "$SLUG" >/dev/null 2>&1 || info "appctl stop returned non-zero; polling decides"
  wait_until "$STOP_TIMEOUT" container_gone \
    || abort_swap "the container did not exit within ${STOP_TIMEOUT}s (pids: $(scan_pids container | tr '\n' ' '))"

  # A13.4: fresh cluster, socket-only boot, restore, compare.
  if [ -e "$DATA" ]; then
    mv "$DATA" "$APPDIR/data.aborted-$(date -u +%Y%m%dT%H%M%SZ)" || abort_swap "cannot move the old $DATA aside"
  fi
  fresh_cluster "$DATA" "$RUN" "$PORT" "" || abort_swap "initdb failed"
  CUT_PG_STARTED=1
  "$PGBIN/pg_ctl" -D "$DATA" -l "$APPDIR/restore.log" -w -t 120 start >/dev/null \
    || abort_swap "native postgres (socket-only) did not start"
  restore_into "$RUN" "$PORT" "$dumpdir" || abort_swap "restore failed"
  snapshot "$SWAPDIR/counts.restored.json" sock_psql "$RUN" "$PORT" || abort_swap "cannot count the native cluster"
  diffs="$(pgn compare "$SWAPDIR/counts.before.json" "$SWAPDIR/counts.restored.json")" \
    || abort_swap "restored counts/sequences differ: $(echo "$diffs" | head -5 | tr '\n' ' ')"
  "$PGBIN/pg_ctl" -D "$DATA" -m fast -w -t 120 stop >/dev/null || abort_swap "socket-only native postgres did not stop"
  CUT_PG_STARTED=""

  # A13.5: the recorded listen set, then the unit.
  pgn conf "$listen" "$PORT" "$RUN" >> "$DATA/postgresql.conf" || abort_swap "cannot write the listen set"
  mkdir -p "$UNIT_DIR"
  native_write_secure "$UNIT_DIR/$UNIT" 0644 < "$APPDIR/native/$UNIT" || abort_swap "unit install failed"
  sysd daemon-reload || abort_swap "daemon-reload failed"
  sysd enable --now "$UNIT" || die "enable --now $UNIT failed; suppression + hold kept ON; run --rollback --execute"
  if ! verify_native; then
    die "native postgres fails parity after the cutover; suppression + hold kept ON; run --rollback --execute"
  fi
  snapshot "$SWAPDIR/counts.native.json" sock_psql "$RUN" "$PORT" \
    && pgn compare "$SWAPDIR/counts.before.json" "$SWAPDIR/counts.native.json" >/dev/null \
    || die "native counts differ after the cutover; run --rollback --execute"

  if [ -n "$REPOINT" ]; then
    pgn repoint "$LM_CONFIG" "$REPOINT" | native_write_secure "$SWAPDIR/listmonk-db-host.orig" 0600 \
      || die "cannot re-point listmonk at $REPOINT; run --rollback --execute"
  fi
  sysd start "$LM_UNIT" || die "start $LM_UNIT failed; run --rollback --execute"
  wait_until "$PROOF_TIMEOUT" lm_healthy || die "listmonk /health never answered 200 on native postgres; run --rollback --execute"
  cron_release || die "crontab hold NOT released (lines marked '$HOLD_MARK'); remove the marker by hand"

  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  soak="$(date -u -d '+14 days' +%Y-%m-%dT%H:%M:%SZ)" || die "cannot compute soak_until"
  swapstate set "$SLUG" "swap_date=$now" "soak_until=$soak" "rollback_window=open" >/dev/null \
    || die "cannot record swap state"
  info "SWAPPED to native $VERSION; counts + sequences equal; listmonk healthy; soak until $soak. elapsed=$((SECONDS - T0))s"
  info "Next: PR dropping swap_state: pending-swap, deploy via 240, then --finish --execute"
}

do_finish() {
  local st cls ss_state dormant
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state dormant <<<"$st"
  [ "$cls" = systemd ] && [ -z "$ss_state" ] && [ "$dormant" = 1 ] \
    || die "deployed manifest still says class=$cls swap_state=${ss_state:-none} dormant=$dormant; deploy the follow-up (no pending-swap) first"
  load_lmconf
  LMPORT="$(lm_port)" || die "cannot read listmonk's [app] address"
  verify_native || die "native postgres fails parity; run --rollback --execute"
  lm_healthy || die "listmonk /health is not 200; run --rollback --execute"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "FINISHED: ${SUPPRESS[*]} unsuppressed; 14-day soak running"
}

do_rollback() {
  local isn keep orig db
  # Step 0: re-suppress + mask BEFORE anything stops.
  suppression add "${SUPPRESS[@]}" --reason "QFLX-37 rollback to UCC" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to roll back unsuppressed"
  mask_unit
  load_lmconf
  LMPORT="$(lm_port)" || die "cannot read listmonk's [app] address"
  cron_hold || die "cannot hold the listmonk crontab lines"
  sysd stop "$LM_UNIT" >/dev/null 2>&1 || true
  wait_until "$STOP_TIMEOUT" lm_down || die "$LM_UNIT did not stop"
  # Writes made on native since the cutover are KEPT, never silently dropped.
  if [ -n "$(scan_pids native)" ]; then
    keep="$SWAPDIR/rollback-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$keep" && chmod 0700 "$keep" || die "cannot create $keep"
    sock_psql "$RUN" "$PORT" postgres "$(pgn sql dbs)" > "$keep/databases" \
      || die "cannot list the native databases; not stopping it (run again or dump by hand)"
    while IFS= read -r db; do
      db="${db%$'\r'}"; [ -n "$db" ] || continue
      valid_db "$db" || continue
      "$PGBIN/pg_dump" -h "$RUN" -p "$PORT" -U "$DBUSER" -Fc -d "$db" -f "$keep/$db.dump" \
        || die "pg_dump $db from native failed; not stopping it"
    done < "$keep/databases"
    info "native databases kept in $keep (post-swap writes; restore by hand if wanted)"
  fi
  # Step 1.
  sysd stop "$UNIT" >/dev/null 2>&1 || true
  wait_until "$STOP_TIMEOUT" native_gone || die "native postgres did not stop within ${STOP_TIMEOUT}s"
  # Step 2: the DEPLOYED manifest must dispatch postgres as UCC again.
  isn="$("$APPCTL" is-native "$SLUG" 2>/dev/null)"
  if [ "${isn%$'\r'}" != ucc ]; then
    echo "[312-postgres] PAUSED: native stopped + masked, listmonk stopped, writers held; revert the deployed manifest (PR + 240) so appctl dispatches $SLUG as UCC, then re-run --rollback --execute" >&2
    exit 10
  fi
  # Step 3.
  "$APPCTL" start "$SLUG" >/dev/null 2>&1 || info "appctl start returned non-zero; polling decides"
  wait_until "$STOP_TIMEOUT" container_up || die "UCC postgres did not come back within ${STOP_TIMEOUT}s; still suppressed, writers held"
  if [ -s "$SWAPDIR/listmonk-db-host.orig" ]; then
    orig="$(tr -d '[:space:]' < "$SWAPDIR/listmonk-db-host.orig")"
    pgn repoint "$LM_CONFIG" "$orig" >/dev/null || die "cannot put listmonk [db] host back to $orig"
    rm -f "$SWAPDIR/listmonk-db-host.orig"
  fi
  sysd start "$LM_UNIT" >/dev/null 2>&1 || die "start $LM_UNIT failed"
  wait_until "$PROOF_TIMEOUT" lm_healthy || die "listmonk /health not 200 on UCC postgres; still suppressed, writers held"
  cron_release || die "crontab hold NOT released (lines marked '$HOLD_MARK')"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "ROLLED BACK to UCC; $UNIT masked (unmasked by the next --swap). elapsed=$((SECONDS - T0))s"
}

do_post_upgrade() {
  local v="${UPGRADE_VER#v}" d major
  _native_valid_ver "$v" || die "bad version: $v"
  d="$APPDIR/bin/$v/usr/lib/postgresql/$PG_MAJOR/bin"
  [ -x "$d/postgres" ] || die "$d/postgres missing (a different major is a manual runbook: pg_upgrade)"
  major="$(tr -d '[:space:]' < "$DATA/PG_VERSION" 2>/dev/null)"
  [ "$major" = "$PG_MAJOR" ] || die "cluster PG_VERSION '$major' != $PG_MAJOR; refusing"
  [ "${v%%.*}" = "$PG_MAJOR" ] || die "version $v is not major $PG_MAJOR; a major is a manual runbook"
  # The client tools of the swap are not needed to RUN the server; only the
  # server binaries are gated here.
  local b out
  for b in postgres pg_ctl initdb; do
    out="$("$LDD" "$d/$b" 2>&1)" || die "ldd $b failed"
    printf '%s\n' "$out" | grep -q 'not found' && die "ldd $b: missing libraries"
  done
  out="$("$d/postgres" --version 2>&1)" || die "postgres --version failed"
  case "$out" in *"PostgreSQL) ${v%%-*}"*) ;; *) die "postgres --version says '$out', want ${v%%-*}" ;; esac
  # lifecycle unpacked only the SERVER deb. psql/pg_dump/pg_restore (verify,
  # rollback dumps) come along from the previous build: same major, so they
  # read and dump the new minor exactly as before.
  local cur="$APPDIR/bin/current/usr/lib/postgresql/$PG_MAJOR/bin"
  for b in psql pg_dump pg_dumpall pg_restore; do
    if [ ! -e "$d/$b" ] && [ -x "$cur/$b" ]; then
      cp -a "$cur/$b" "$d/$b" || die "cannot carry $b into bin/$v"
    fi
  done
  native_link_current "$NDIR" "$v" || die "cannot flip bin/current"
  info "post-upgrade $v: gated, current flipped (lifecycle restarts $UNIT)"
}

case "$MODE" in
  install)      do_install ;;
  prove)        do_prove ;;
  swap)         do_swap ;;
  finish)       do_finish ;;
  rollback)     do_rollback ;;
  post-upgrade) do_post_upgrade ;;
esac
