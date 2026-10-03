#!/usr/bin/env bash
# ucc-postgres-upgrade.sh — upgrade UCC postgres WITHOUT rotating its password,
# then prove postgres + listmonk came back. Called by app-upgrade-all.sh as a
# child process (one per weekly window sweep); runnable by hand too.
#
# WHY THIS EXISTS (2026-10-01): postgres sat in app-upgrade-all's DEFAULT_SKIP,
# so it never upgraded. UCC eventually gated `app-postgres start/restart` behind
# "older build ... Upgrade & Repair" (exit 2); postgres stopped, could not be
# restarted, and listmonk crash-looped for two days. A bare `app-postgres
# upgrade` is NOT safe either: without `-p` UCC may set a random password and
# listmonk (whose config.toml holds the old one) loses its database.
#
# Usage:
#   ucc-postgres-upgrade.sh             # live: backup, upgrade -p <pw>, verify
#   ucc-postgres-upgrade.sh --dry-run   # lock + read config, print the plan
#
# Steps (live):
#   0. SINGLE INSTANCE: flock -n on fd 9 over $STATE_DIR/ucc-postgres-upgrade.lock,
#      taken BEFORE the password is read (live and dry-run). Held for the whole
#      run, health checks included. Held elsewhere -> RESULT=skipped:locked;
#      no flock binary / unopenable lockfile -> RESULT=skipped:no_lock (never
#      proceeds unlocked). The lockfile (0600) is never deleted. fd 9 is
#      inherited by app-postgres ON PURPOSE: an orphaned upgrade keeps the lock,
#      so a second run cannot start while the first is still going.
#   1. read the password from listmonk's config.toml [db] table (fail closed)
#   2. read [db] port (default 42009) and secrets/listmonk.port
#   3. tarball ~/.apps/postgres -> ~/.apps/backup/qflix-postgres-<UTC>-<pid>-<rand>.tar.gz
#      (mktemp, 0600 from creation, collision-free) under
#      `timeout -k 10 $PG_BACKUP_TIMEOUT`; timeout / rc>=2 / empty archive ->
#      own archive removed, RESULT=skipped:backup_failed, no upgrade
#   4. retention (only after OUR backup succeeded, only under the lock): consider
#      qflix-postgres-*.tar.gz files whose mtime is BEFORE this run started (so
#      in-flight archives and future-dated clock-skew files are never touched),
#      never the current archive, delete the oldest beyond PG_BACKUP_KEEP (the
#      new archive counts as one). UCC's *.zip files are never touched.
#   5. timeout -k 30 $PG_UPGRADE_TIMEOUT app-postgres upgrade -p "$PW"
#      (never -n: UCC's own pre-upgrade backup is ALWAYS kept, even under
#      --no-backup)
#   6. postgres health: checkpointer process (this uid only) + TCP on [db] port
#   7. listmonk health: HTTP 200, else ONE `systemctl --user restart listmonk`
#   Steps 6-7 run after EVERY attempt (ok, failed, timed out): a failed or
#   killed upgrade can leave postgres down, and that must be loud.
#   Postgres itself is NEVER stopped/restarted here — start/stop is exactly the
#   verb UCC gates, so a pre-upgrade stop could recreate the outage.
#
# Stdout: progress lines (all redacted), then as the LAST line `RESULT=<token>`.
#   This holds for EVERY invocation except --help: bad env, bad usage, internal
#   error, signals and `set -u` violations all end with a RESULT line (an EXIT
#   trap emits error:internal if nothing else did). Never exits 2, never FATAL.
#   upgraded | would_upgrade | timeout
#   skipped:no_config|no_password|short_password|unparseable_password|
#           backup_failed|no_wrapper|locked|no_lock
#   error:upgrade_rc<N>[:older_build] | error:postgres_unhealthy |
#   error:listmonk_unhealthy | error:interrupted | error:internal | error:bad_usage
# Exit: 0 upgraded+healthy (or dry-run would_upgrade) · 3 any skipped:*
#       (nothing mutated) · 1 any error:*, or timed out but healthy ·
#       124 timed out AND post-check unhealthy.
#
# Env (optional; ANY invalid value silently becomes its default, never aborts):
#   durations  PG_UPGRADE_TIMEOUT [8m]  PG_BACKUP_TIMEOUT [10m]
#              ^[0-9]{1,7}[smhd]?$, 1..604800 s (0 would mean "no limit")
#   integers   PG_HEALTH_TIMEOUT_S [180] 0..86400 · LISTMONK_HEALTH_TIMEOUT_S [120]
#              0..86400 · HEALTH_POLL_INTERVAL_S [5] 1..3600 · PG_BACKUP_KEEP [2] 1..1000
#   paths      LISTMONK_CONFIG PG_APP_DIR PG_BACKUP_DIR MANITOBA_SECRETS_DIR|
#              MANITOBA_SECRETS MANITOBA_STATE_DIR MANITOBA_MAINT_DIR
#   Env values are matched against a regex BEFORE any arithmetic, so a value
#   like 'a[$(cmd)]' can never be evaluated by $(( )).
#
# BUDGET (the caller, app-upgrade-all.sh, wraps this script in
#   timeout -k $PG_OUTER_KILL_GRACE_S[90] $OUTER, OUTER = backup + upgrade +
#   PG_HEALTH_TIMEOUT_S + LISTMONK_HEALTH_TIMEOUT_S + 60; PG_OUTER_TIMEOUT_S
#   (integer >= 1) replaces OUTER — both are test seams).
#
# KILL SEMANTICS. TERM/INT/HUP are trapped. The tar and the upgrade run as
#   background jobs + `wait` (a foreground $(...) would defer the trap until the
#   command finished). On a signal: before the upgrade started -> own partial
#   archive removed, RESULT=error:interrupted. During the upgrade/health phase
#   -> TERM to the inner `timeout` (which forwards to app-postgres's process
#   group and escalates to KILL after 30 s), wait up to 35 s, probe postgres
#   (budget min(PG_HEALTH_TIMEOUT_S,30)), send an error notify naming the
#   postgres state, RESULT=error:interrupted, exit 1. The inner timeout runs in
#   its OWN process group, so a SIGKILLed child (outer -k escalation) can leave
#   app-postgres orphaned. BOUNDED RESIDUAL: its lifetime is capped by the inner
#   `timeout -k 30`, and it keeps fd 9 (the lock) while it lives. The parent
#   always probes postgres itself after an outer 124/137. Measured on Linux
#   (council r3): an outer GROUP signal also kills the output reader, so the
#   orphan usually dies of SIGPIPE at its next write (seconds, mid-upgrade)
#   rather than at the inner bound, and the redacted log is not written.
#
# KNOWN RESIDUAL — password in argv. app-postgres accepts the password ONLY as
#   an argv flag (`-p`); UCC's CLI offers no stdin or env alternative. While the
#   upgrade runs, the password is therefore readable in /proc/<pid>/cmdline
#   (`ps`) of app-postgres and its children by other tenants of this shared
#   host. Exposure is bounded to the upgrade's lifetime (<= PG_UPGRADE_TIMEOUT
#   + 30 s). Everywhere else it is contained: it lives only in this process's
#   memory, is never exported (PW is unset on entry and never `export`ed, so it
#   is not in app-postgres's environment either), never printed, and the raw
#   output (which echoes the password in plaintext JSON) lives only in a pipe
#   and in memory; only a redacted tail is ever written to disk. redact() masks
#   every JSON "password" value, the literal password, its JSON-escaped form and
#   its slash-escaped form.
#
# BACKUP CAVEAT. The QFlix tarball is taken from a LIVE data dir, so it is NOT
#   crash-atomic (tar rc 1 "file changed as we read it" is accepted). UCC's own
#   pre-upgrade backup (~/.apps/backup/postgres-*.zip) is the PRIMARY restore
#   point; the tarball is a secondary, best-effort copy.
#
# RESTORE (operator step, never automated): stop nothing; untar the archive
#   over ~/.apps/postgres per UCC docs:
#     tar -xzf ~/.apps/backup/qflix-postgres-<ts>-<pid>-<rand>.tar.gz -C ~/.apps
#   or restore UCC's zip via the UCP. Then check listmonk (HTTP 200).

set -u
# Never inherit a PW from the environment: an exported PW would hand the
# password to app-postgres via /proc/<pid>/environ as well as argv.
unset PW
shopt -u patsub_replacement 2>/dev/null
umask 077                       # log, lockfile, archive, status: all 0600

# ---- the RESULT contract ---------------------------------------------------
RESULT_EMITTED=0
emit() {   # emit TOKEN EXIT — the single exit point that prints RESULT=
    RESULT_EMITTED=1
    echo "RESULT=$1"
    exit "$2"
}
on_exit() {   # EXIT trap: no path may end without a RESULT line
    if (( RESULT_EMITTED == 0 )); then
        RESULT_EMITTED=1
        echo "RESULT=error:internal"
        exit 1
    fi
}
trap on_exit EXIT

# ---- globals (all defined before any signal trap is armed) -----------------
PW=""
PW_FAIL=""
DB_PORT=42009
LM_PORT=""
DRY_RUN=0
STAGE=pre            # pre | tar | upgrade | post — what a signal must undo
ARCHIVE=""
TAR_PID=""
UP_TP=""
NAP_PID=""
STATUS_FILE=""

# int_env NAME MIN MAX DEFAULT — NAME := validated base-10 integer or DEFAULT.
# The regex runs first; nothing is ever arithmetic-evaluated before it.
int_env() {
    local name="$1" min="$2" max="$3" def="$4" v
    v="${!name-}"
    if [[ "$v" =~ ^[0-9]{1,7}$ ]]; then
        v=$(( 10#$v ))
        (( v >= min && v <= max )) || v="$def"
    else
        v="$def"
    fi
    printf -v "$name" '%s' "$v"
}

# dur_env NAME DEFAULT_SECONDS — NAME := "<N>s" (N in 1..604800). 0 is rejected:
# `timeout 0` means "no limit".
dur_env() {
    local name="$1" def="$2" v n
    v="${!name-}"
    if [[ "$v" =~ ^([0-9]{1,7})([smhd]?)$ ]]; then
        n=$(( 10#${BASH_REMATCH[1]} ))
        case "${BASH_REMATCH[2]}" in
            m) n=$(( n * 60 )) ;;
            h) n=$(( n * 3600 )) ;;
            d) n=$(( n * 86400 )) ;;
        esac
        (( n >= 1 && n <= 604800 )) || n="$def"
    else
        n="$def"
    fi
    printf -v "$name" '%ss' "$n"
}

LISTMONK_CONFIG="${LISTMONK_CONFIG:-$HOME/.apps/listmonk/etc/config.toml}"
PG_APP_DIR="${PG_APP_DIR:-$HOME/.apps/postgres}"
PG_BACKUP_DIR="${PG_BACKUP_DIR:-$HOME/.apps/backup}"
SECRETS_DIR="${MANITOBA_SECRETS_DIR:-${MANITOBA_SECRETS:-$HOME/secrets}}"
STATE_DIR="${MANITOBA_STATE_DIR:-$HOME/.opt/maint}"
dur_env PG_UPGRADE_TIMEOUT 480
dur_env PG_BACKUP_TIMEOUT 600
int_env PG_HEALTH_TIMEOUT_S 0 86400 180
int_env LISTMONK_HEALTH_TIMEOUT_S 0 86400 120
int_env HEALTH_POLL_INTERVAL_S 1 3600 5
int_env PG_BACKUP_KEEP 1 1000 2
PG_LOG="$STATE_DIR/postgres-upgrade.log"
LOCK_FILE="$STATE_DIR/ucc-postgres-upgrade.lock"
START_EPOCH=$(printf '%(%s)T' -1)

# Lockfile exists from the first line of every run (bad usage included), so
# its presence never depends on argv. Locking itself happens in step 0.
mkdir -p "$STATE_DIR" 2>/dev/null
( : >> "$LOCK_FILE" ) 2>/dev/null

case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    "") ;;
    -h|--help)
        RESULT_EMITTED=1   # --help is the one invocation without a RESULT line
        awk 'NR > 1 { if ($0 ~ /^#/) { sub(/^# ?/, ""); print } else exit }' "$0"
        exit 0 ;;
    *) echo "unknown argument (see --help)"; emit error:bad_usage 1 ;;
esac
(( $# <= 1 )) || { echo "too many arguments (see --help)"; emit error:bad_usage 1; }

# ---- output helpers --------------------------------------------------------

# Same contract as app-upgrade-all.sh notify(). Messages are fixed text built
# from tokens only — the password can never reach Discord/notify.log.
notify() {
    local level="${1:-info}" msg="$2"
    local maint_dir="${MANITOBA_MAINT_DIR:-$HOME/scripts/maint}"
    [[ -f "$maint_dir/lib/notify.py" ]] || return 0
    # Bounded: the parent's outer kill grace (90s) must cover notify too.
    PYTHONPATH="$maint_dir" timeout 20 python3 - "$level" "$msg" <<'PYEOF' >/dev/null 2>&1 || true
import sys
from lib.notify import notify as n
n(sys.argv[2], level=sys.argv[1])
PYEOF
}

# redact TEXT -> TEXT with (i) every JSON "password" value masked
# (case-insensitive key; plus the cut-off-at-end-of-line form) and the literal
# $PW masked in (ii) raw, (iii) JSON-escaped (\ -> \\, " -> \") and (iv)
# slash-escaped (iii plus / -> \/) form. (ii)-(iv) are bash expansions, so the
# password never becomes a sed/awk argv. Longest form first.
redact() {
    local s="$1" e f bs='\' dq='"' sl='/'
    if [[ -n "${PW:-}" ]]; then
        e=${PW//"$bs"/"$bs$bs"}
        e=${e//"$dq"/"$bs$dq"}
        f=${e//"$sl"/"$bs$sl"}
        s=${s//"$f"/<redacted>}
        s=${s//"$e"/<redacted>}
        s=${s//"$PW"/<redacted>}
    fi
    printf '%s\n' "$s" | sed -E \
        -e 's/"password"[[:space:]]*:[[:space:]]*"([^"\\]|\\.)*"/"password":"<redacted>"/gI' \
        -e 's/"password"[[:space:]]*:[[:space:]]*"[^"]*$/"password":"<redacted>/I'
}

say() { redact "$*"; }   # every dynamic progress line goes through redact

skip() {   # fail-closed: nothing mutated, loud warning (none in dry-run)
    say "ucc-postgres-upgrade: fail-closed skip ($1) — app-postgres NOT invoked"
    (( DRY_RUN )) || notify warning "ucc-postgres-upgrade: postgres upgrade SKIPPED fail-closed ($1). Postgres was not upgraded; UCC may gate it behind 'older build' — operator: fix the cause, or run 'app-postgres upgrade -p <listmonk db pw>' by hand."
    emit "skipped:$1" 3
}

# ---- step 0: single instance (BEFORE any password read) --------------------
mkdir -p "$STATE_DIR" 2>/dev/null
command -v flock >/dev/null 2>&1 || skip no_lock
( : >> "$LOCK_FILE" ) 2>/dev/null || skip no_lock
exec 9>>"$LOCK_FILE" || skip no_lock
chmod 600 "$LOCK_FILE" 2>/dev/null
flock -n 9 || skip locked

# ---- process helpers -------------------------------------------------------
alive() {   # a zombie counts as gone
    kill -0 "$1" 2>/dev/null || return 1
    local st=""
    [[ -r "/proc/$1/status" ]] && st=$(awk '/^State:/{print $2; exit}' "/proc/$1/status" 2>/dev/null)
    [[ "$st" != Z ]]
}

# reap_wait SECONDS PID... — until all are gone; KILL stragglers at the limit.
reap_wait() {
    local limit="$1" i p any; shift
    for (( i = 0; i < limit * 4; i++ )); do
        any=0
        for p in "$@"; do
            [[ -n "$p" ]] && alive "$p" && any=1
        done
        (( any )) || return 0
        sleep 0.25
    done
    for p in "$@"; do
        [[ -n "$p" ]] && kill -KILL "$p" 2>/dev/null
    done
    return 0
}

# kill_children SIG — TERM every direct child of this shell (race fallback when
# a signal lands between launching the upgrade and recording its pid).
kill_children() {
    local f st
    for f in /proc/[0-9]*/stat; do
        read -r st < "$f" 2>/dev/null || continue
        st=${st##*) }
        set -- $st
        [[ "${2:-}" == "$$" ]] || continue
        f=${f#/proc/}; f=${f%/stat}
        kill -TERM "$f" 2>/dev/null
    done
}

# nap SECONDS — a sleep a trapped signal can interrupt (no foreground sleep,
# no inherited stdout: an orphaned sleep must not hold the caller's pipe).
nap() {
    sleep "$1" >/dev/null 2>&1 &
    NAP_PID=$!
    wait "$NAP_PID" 2>/dev/null
    NAP_PID=""
}

tcp_open() {   # tcp_open PORT — bash /dev/tcp, bounded so it can never hang
    timeout 5 bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$1"' _ "$1" >/dev/null 2>&1
}

pg_healthy() {   # uid-scoped: another tenant's postgres must never count
    pgrep -u "$(id -u)" -f 'postgres: checkpointer' >/dev/null 2>&1 && tcp_open "$DB_PORT"
}

lm_healthy() {
    [[ -n "$LM_PORT" ]] || return 1
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${LM_PORT}/" 2>/dev/null)
    [[ "$code" == "200" ]]
}

poll() {   # poll BUDGET_S CHECK... — at least one check, then until deadline
    local budget="$1" now rem; shift
    local deadline=$(( $(printf '%(%s)T' -1) + budget ))
    while :; do
        "$@" && return 0
        now=$(printf '%(%s)T' -1)
        (( now >= deadline )) && return 1
        rem=$(( deadline - now ))
        nap $(( rem < HEALTH_POLL_INTERVAL_S ? rem : HEALTH_POLL_INTERVAL_S ))
    done
}

# ---- signals ---------------------------------------------------------------
on_signal() {
    local st="$STAGE" pg_state="down" b
    trap '' TERM INT HUP          # a second signal must not re-enter
    echo "ucc-postgres-upgrade: signal received during stage '$st'"
    case "$st" in
        pre|tar)
            [[ -n "$TAR_PID" ]] && kill -TERM "$TAR_PID" 2>/dev/null
            reap_wait 35 "$TAR_PID"
            [[ -n "$ARCHIVE" ]] && rm -f -- "$ARCHIVE"
            emit error:interrupted 1 ;;
        *)
            [[ -n "$NAP_PID" ]] && kill -TERM "$NAP_PID" 2>/dev/null
            if [[ -n "$UP_TP" ]]; then
                kill -TERM "$UP_TP" 2>/dev/null
                reap_wait 35 "$UP_TP"
            elif [[ "$st" == upgrade ]]; then
                kill_children
                sleep 1
            fi
            [[ -n "$STATUS_FILE" ]] && rm -f -- "$STATUS_FILE" "$STATUS_FILE.tmp"
            b=$PG_HEALTH_TIMEOUT_S; (( b > 30 )) && b=30
            poll "$b" pg_healthy && pg_state="ok"
            say "ucc-postgres-upgrade: interrupted; postgres=$pg_state"
            (( DRY_RUN )) || notify error "ucc-postgres-upgrade: INTERRUPTED while the postgres upgrade was in stage '$st'; postgres is ${pg_state} after the interrupt. Restore point: UCC pre-upgrade backup, then ${ARCHIVE##*/}."
            emit error:interrupted 1 ;;
    esac
}
trap 'on_signal' TERM INT HUP

# ---- config readers (defined after the lock on purpose) --------------------

# read_listmonk_db_password: pure bash (no subprocess ever sees the value).
# Sets PW on success (return 0) or PW_FAIL=<reason> (return 1).
# Only the [db] table is read; only a single-line TOML basic string with no
# backslash/quote, or a literal string with no apostrophe, is accepted.
# Anything else fails closed — guessing at a password is how creds rotate.
read_listmonk_db_password() {
    PW=""; PW_FAIL=""
    local f="$LISTMONK_CONFIG"
    if [[ ! -f "$f" || ! -r "$f" ]]; then PW_FAIL=no_config; return 1; fi
    local re_db='^[[:space:]]*\[db\][[:space:]]*(#.*)?$'
    local re_tbl='^[[:space:]]*\['
    local re_key='^[[:space:]]*password[[:space:]]*='
    local re_basic='^[[:space:]]*password[[:space:]]*=[[:space:]]*"([^"\\]*)"[[:space:]]*(#.*)?$'
    local re_lit="^[[:space:]]*password[[:space:]]*=[[:space:]]*'([^']*)'[[:space:]]*(#.*)?$"
    local line val="" in_db=0 found=0 outside=0 bad=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line=${line%$'\r'}
        if [[ $line =~ $re_db ]]; then in_db=1; continue; fi
        if [[ $line =~ $re_tbl ]]; then in_db=0; continue; fi
        [[ $line =~ $re_key ]] || continue
        if (( ! in_db )); then outside=1; continue; fi
        found=$((found + 1))
        if [[ $line =~ $re_basic ]]; then
            val=${BASH_REMATCH[1]}
        elif [[ $line =~ $re_lit ]]; then
            val=${BASH_REMATCH[1]}
        else
            bad=1
        fi
    done < "$f"
    if (( found == 0 )); then
        (( outside )) && PW_FAIL=unparseable_password || PW_FAIL=no_password
        return 1
    fi
    if (( found > 1 || bad )); then PW_FAIL=unparseable_password; return 1; fi
    if (( ${#val} < 8 )); then PW_FAIL=short_password; return 1; fi
    PW="$val"
    return 0
}

# [db] port = <int>; default 42009 when absent, not a plain integer, or > 65535.
read_db_port() {
    local re_db='^[[:space:]]*\[db\][[:space:]]*(#.*)?$'
    local re_tbl='^[[:space:]]*\['
    local re_port='^[[:space:]]*port[[:space:]]*=[[:space:]]*([0-9]{1,5})[[:space:]]*(#.*)?$'
    local line in_db=0 p
    DB_PORT=42009
    while IFS= read -r line || [[ -n "$line" ]]; do
        line=${line%$'\r'}
        if [[ $line =~ $re_db ]]; then in_db=1; continue; fi
        if [[ $line =~ $re_tbl ]]; then in_db=0; continue; fi
        if (( in_db )) && [[ $line =~ $re_port ]]; then
            p=$(( 10#${BASH_REMATCH[1]} ))
            (( p >= 1 && p <= 65535 )) && DB_PORT=$p
        fi
    done < "$LISTMONK_CONFIG"
}

read_listmonk_port() {
    LM_PORT=""
    local f="$SECRETS_DIR/listmonk.port" v=""
    [[ -r "$f" ]] || return 0
    IFS= read -r v < "$f" || true
    v="${v//[[:space:]]/}"
    [[ "$v" =~ ^[0-9]{1,5}$ ]] && LM_PORT="$v"
}

# Runs inside a process substitution that receives app-postgres's stdout+stderr
# through a PIPE. The raw output (it contains the password in plaintext JSON)
# exists only in this process's memory; only the redacted tail and two flags
# ever reach disk. Its own stdout/stderr go to /dev/null (set at the call site)
# so an orphan can never hold the caller's capture pipe open.
capture_upgrade_output() {
    trap - EXIT TERM INT HUP
    local out="" line older=0 rf=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        out+="$line"$'\n'
    done
    shopt -s nocasematch
    [[ "$out" == *"older build"* ]] && older=1
    [[ "$out" =~ \"result\"[[:space:]]*:[[:space:]]*false ]] && rf=1
    shopt -u nocasematch
    {
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) app-postgres upgrade output (redacted, last 20 lines)"
        redact "${out%$'\n'}" | tail -n 20
    } > "$PG_LOG" 2>/dev/null
    chmod 600 "$PG_LOG" 2>/dev/null
    out=""
    printf 'older=%s rf=%s\n' "$older" "$rf" > "$STATUS_FILE.tmp" 2>/dev/null \
        && mv -f "$STATUS_FILE.tmp" "$STATUS_FILE" 2>/dev/null
    return 0
}

# ---- step 1-2: config reads (dry-run stops after these) --------------------
read_listmonk_db_password || skip "$PW_FAIL"
read_db_port
read_listmonk_port
command -v app-postgres >/dev/null 2>&1 || skip no_wrapper

if (( DRY_RUN )); then
    say "  [DRY] backup: timeout -k 10 ${PG_BACKUP_TIMEOUT} tar -czf ${PG_BACKUP_DIR}/qflix-postgres-<UTC>-$$-XXXXXX.tar.gz -C $(dirname "$PG_APP_DIR") $(basename "$PG_APP_DIR") (keep ${PG_BACKUP_KEEP})"
    echo "  [DRY] app-postgres upgrade -p <redacted>"
    say "  [DRY] verify: checkpointer + tcp 127.0.0.1:${DB_PORT}; listmonk http 127.0.0.1:${LM_PORT:-<unreadable>}/"
    emit would_upgrade 0
fi

# ---- step 3: backup (umask 077 -> archive is 0600 from creation) -----------
mkdir -p "$PG_BACKUP_DIR" 2>/dev/null
ts=$(date -u +%Y-%m-%d_%H-%M-%S)
ARCHIVE=$(mktemp --suffix=.tar.gz "$PG_BACKUP_DIR/qflix-postgres-${ts}-$$-XXXXXX" 2>/dev/null) || ARCHIVE=""
[[ -n "$ARCHIVE" && -f "$ARCHIVE" ]] || { ARCHIVE=""; skip backup_failed; }
STAGE=tar
timeout -k 10 "$PG_BACKUP_TIMEOUT" tar -czf "$ARCHIVE" -C "$(dirname "$PG_APP_DIR")" "$(basename "$PG_APP_DIR")" >/dev/null 2>&1 &
TAR_PID=$!
wait "$TAR_PID"
tar_rc=$?
TAR_PID=""
if (( tar_rc == 124 || tar_rc == 137 || tar_rc >= 2 )) || [[ ! -s "$ARCHIVE" ]]; then
    rm -f -- "$ARCHIVE"
    ARCHIVE=""
    skip backup_failed
fi
chmod 600 "$ARCHIVE" 2>/dev/null
say "ucc-postgres-upgrade: backup ok (${ARCHIVE##*/}, tar rc=$tar_rc)"

# ---- step 4: retention — under the lock, after OUR backup succeeded --------
# Candidates: qflix-postgres-*.tar.gz strictly older than this run's start
# (in-flight archives and future-dated clock-skew files are never touched), not
# the current archive. Newest first; the new archive counts as one of the KEEP.
kept=1
while IFS=$'\t' read -r -d '' mt path; do
    [[ -n "$path" && "$path" != "$ARCHIVE" && "${mt%%.*}" =~ ^[0-9]+$ ]] || continue
    (( 10#${mt%%.*} >= START_EPOCH )) && continue
    kept=$((kept + 1))
    if (( kept > PG_BACKUP_KEEP )); then
        rm -f -- "$path" && say "ucc-postgres-upgrade: pruned ${path##*/}"
    fi
done < <(find "$PG_BACKUP_DIR" -maxdepth 1 -type f -name 'qflix-postgres-*.tar.gz' -printf '%T@\t%p\0' 2>/dev/null | sort -z -rn)

# ---- step 5: the upgrade. Raw output never touches a file ------------------
STAGE=upgrade
STATUS_FILE="$STATE_DIR/.postgres-upgrade.status.$$"
rm -f -- "$STATUS_FILE" "$STATUS_FILE.tmp"
echo "ucc-postgres-upgrade: app-postgres upgrade -p <redacted> (timeout -k 30 ${PG_UPGRADE_TIMEOUT})"
timeout -k 30 "$PG_UPGRADE_TIMEOUT" app-postgres upgrade -p "$PW" > >(capture_upgrade_output >/dev/null 2>&1) 2>&1 &
UP_TP=$!
wait "$UP_TP"
rc=$?
UP_TP=""
STAGE=post

# The reader finishes when the pipe closes (milliseconds). Wait up to 15 s.
for (( i = 0; i < 150; i++ )); do
    [[ -f "$STATUS_FILE" ]] && break
    nap 0.1
done
older=0; rf=0; st_line=""
if [[ -f "$STATUS_FILE" ]]; then
    IFS= read -r st_line < "$STATUS_FILE" || true
    [[ "$st_line" =~ ^older=([01])\ rf=([01])$ ]] && { older=${BASH_REMATCH[1]}; rf=${BASH_REMATCH[2]}; }
    rm -f -- "$STATUS_FILE"
fi
older_s=""; (( older )) && older_s=":older_build"

if (( rc == 124 || rc == 137 )); then
    token="timeout"
elif (( rc == 0 )); then
    if (( rf )); then token="error:upgrade_rc0"; else token="upgraded"; fi
else
    token="error:upgrade_rc${rc}${older_s}"
fi
printf '%s rc=%s token=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc" "$token" >> "$PG_LOG" 2>/dev/null
chmod 600 "$PG_LOG" 2>/dev/null
echo "ucc-postgres-upgrade: upgrade rc=$rc -> $token"

# ---- step 6-7: health, after EVERY attempt ---------------------------------
pg_state="down"; lm_state="not checked"
if poll "$PG_HEALTH_TIMEOUT_S" pg_healthy; then
    pg_state="ok"
    if [[ -z "$LM_PORT" ]]; then
        lm_state="unknown (listmonk.port unreadable)"
    elif lm_healthy; then
        lm_state="ok"
    else
        echo "ucc-postgres-upgrade: listmonk not 200 — systemctl --user restart listmonk.service (once)"
        timeout 60 systemctl --user restart listmonk.service >/dev/null 2>&1
        if poll "$LISTMONK_HEALTH_TIMEOUT_S" lm_healthy; then
            lm_state="ok (after restart)"
        else
            lm_state="down"
        fi
    fi
fi
echo "ucc-postgres-upgrade: post-check postgres=$pg_state listmonk=$lm_state"
healthy=0
[[ "$pg_state" == ok && "$lm_state" == ok* ]] && healthy=1

check_msg="post-check: postgres=${pg_state}, listmonk=${lm_state}"
if [[ "$token" == upgraded ]]; then
    if (( healthy )); then
        emit upgraded 0
    elif [[ "$pg_state" != ok ]]; then
        notify error "ucc-postgres-upgrade: app-postgres upgrade succeeded but postgres is UNHEALTHY ($check_msg). Restore point: UCC pre-upgrade backup, then ${ARCHIVE##*/}."
        emit error:postgres_unhealthy 1
    else
        notify error "ucc-postgres-upgrade: app-postgres upgrade succeeded but listmonk is UNHEALTHY ($check_msg)."
        emit error:listmonk_unhealthy 1
    fi
fi

notify error "ucc-postgres-upgrade: postgres upgrade FAILED ($token); $check_msg. Detail (redacted): ~/.opt/maint/postgres-upgrade.log."
if [[ "$token" == timeout ]]; then
    (( healthy )) && emit timeout 1
    emit timeout 124
fi
emit "$token" 1
