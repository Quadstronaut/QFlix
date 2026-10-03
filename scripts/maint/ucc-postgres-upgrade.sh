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
#   ucc-postgres-upgrade.sh --dry-run   # read config only, print the plan
#
# Steps (live):
#   1. read the password from listmonk's config.toml [db] table (fail closed)
#   2. read [db] port (default 42009) and secrets/listmonk.port
#   3. tarball ~/.apps/postgres -> ~/.apps/backup/qflix-postgres-<UTC>.tar.gz
#   4. keep PG_BACKUP_KEEP qflix-postgres tarballs (UCC *.zip never touched)
#   5. timeout $PG_UPGRADE_TIMEOUT app-postgres upgrade -p "$PW"   (never -n:
#      UCC's own pre-upgrade backup is ALWAYS kept, even under --no-backup)
#   6. postgres health: checkpointer process + TCP on the [db] port
#   7. listmonk health: HTTP 200, else ONE `systemctl --user restart listmonk`
#   Steps 6-7 run after EVERY attempt (ok, failed, timed out): a failed or
#   killed upgrade can leave postgres down, and that must be loud.
#   Postgres itself is NEVER stopped/restarted here — start/stop is exactly the
#   verb UCC gates, so a pre-upgrade stop could recreate the outage.
#
# Stdout: progress lines (all redacted), then as the LAST line `RESULT=<token>`:
#   upgraded | would_upgrade | timeout
#   skipped:no_config|no_password|short_password|unparseable_password|backup_failed|no_wrapper
#   error:upgrade_rc<N>[:older_build] | error:postgres_unhealthy | error:listmonk_unhealthy
# Exit: 0 upgraded+healthy (or dry-run would_upgrade) · 3 fail-closed skip
#       (nothing mutated) · 1 attempted and failed/unhealthy, or timed out but
#       healthy · 124 timed out AND post-check unhealthy.
#
# Env (optional): LISTMONK_CONFIG PG_APP_DIR PG_BACKUP_DIR PG_BACKUP_KEEP
#   PG_UPGRADE_TIMEOUT PG_HEALTH_TIMEOUT_S LISTMONK_HEALTH_TIMEOUT_S
#   HEALTH_POLL_INTERVAL_S MANITOBA_SECRETS_DIR|MANITOBA_SECRETS
#   MANITOBA_STATE_DIR MANITOBA_MAINT_DIR — defaults below.
#
# KNOWN RESIDUAL — password in argv. app-postgres accepts the password ONLY as
#   an argv flag (`-p`); UCC's CLI offers no stdin or env alternative. While the
#   upgrade runs, the password is therefore readable in /proc/<pid>/cmdline
#   (`ps`) of app-postgres and its children by other tenants of this shared
#   host. Exposure is bounded to the upgrade's lifetime (<= PG_UPGRADE_TIMEOUT).
#   Everywhere else it is contained: it lives only in this process's memory, is
#   never exported, never printed, and every byte of app-postgres output (which
#   echoes the password in plaintext JSON) is redacted before it leaves.
#
# BACKUP CAVEAT. The QFlix tarball is taken from a LIVE data dir, so it is not
#   crash-atomic (tar rc 1 "file changed as we read it" is accepted). UCC's own
#   pre-upgrade backup (~/.apps/backup/postgres-*.zip) is the PRIMARY restore
#   point; the tarball is a secondary, best-effort copy.
#
# RESTORE (operator step, never automated): stop nothing; untar the archive
#   over ~/.apps/postgres per UCC docs:
#     tar -xzf ~/.apps/backup/qflix-postgres-<ts>.tar.gz -C ~/.apps
#   or restore UCC's zip via the UCP. Then check listmonk (HTTP 200).

set -u
# Never inherit a PW from the environment: an exported PW would hand the
# password to app-postgres via /proc/<pid>/environ as well as argv.
unset PW

LISTMONK_CONFIG="${LISTMONK_CONFIG:-$HOME/.apps/listmonk/etc/config.toml}"
PG_APP_DIR="${PG_APP_DIR:-$HOME/.apps/postgres}"
PG_BACKUP_DIR="${PG_BACKUP_DIR:-$HOME/.apps/backup}"
PG_BACKUP_KEEP="${PG_BACKUP_KEEP:-2}"
[[ "$PG_BACKUP_KEEP" =~ ^[0-9]+$ ]] && (( 10#$PG_BACKUP_KEEP >= 1 )) || PG_BACKUP_KEEP=2
PG_BACKUP_KEEP=$(( 10#$PG_BACKUP_KEEP ))
PG_UPGRADE_TIMEOUT="${PG_UPGRADE_TIMEOUT:-8m}"
PG_HEALTH_TIMEOUT_S="${PG_HEALTH_TIMEOUT_S:-180}"
LISTMONK_HEALTH_TIMEOUT_S="${LISTMONK_HEALTH_TIMEOUT_S:-120}"
HEALTH_POLL_INTERVAL_S="${HEALTH_POLL_INTERVAL_S:-5}"
[[ "$PG_HEALTH_TIMEOUT_S" =~ ^[0-9]+$ ]] || PG_HEALTH_TIMEOUT_S=180
[[ "$LISTMONK_HEALTH_TIMEOUT_S" =~ ^[0-9]+$ ]] || LISTMONK_HEALTH_TIMEOUT_S=120
[[ "$HEALTH_POLL_INTERVAL_S" =~ ^[0-9]+$ ]] || HEALTH_POLL_INTERVAL_S=5
SECRETS_DIR="${MANITOBA_SECRETS_DIR:-${MANITOBA_SECRETS:-$HOME/secrets}}"
STATE_DIR="${MANITOBA_STATE_DIR:-$HOME/.opt/maint}"

DRY_RUN=0
case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    "") ;;
    -h|--help) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1"; echo "RESULT=error:bad_usage"; exit 1 ;;
esac

PW=""
PW_FAIL=""
DB_PORT=42009
LM_PORT=""

# Same contract as app-upgrade-all.sh notify(). Messages are fixed text built
# from tokens only — the password can never reach Discord/notify.log.
notify() {
    local level="${1:-info}" msg="$2"
    local maint_dir="${MANITOBA_MAINT_DIR:-$HOME/scripts/maint}"
    [[ -f "$maint_dir/lib/notify.py" ]] || return 0
    PYTHONPATH="$maint_dir" python3 - "$level" "$msg" <<'PYEOF' 2>/dev/null || true
import sys
from lib.notify import notify as n
n(sys.argv[2], level=sys.argv[1])
PYEOF
}

# redact TEXT -> TEXT with (i) every JSON "password":"..." value masked and
# (ii) every literal occurrence of $PW masked. (ii) is a bash expansion, so the
# password never becomes an argv of sed. The trailing sed rule masks a
# "password":"... value that is cut off at end of line (truncated output).
redact() {
    local s="$1"
    if [[ -n "${PW:-}" ]]; then
        s=${s//"$PW"/<redacted>}
    fi
    printf '%s\n' "$s" | sed -E \
        -e 's/"password"[[:space:]]*:[[:space:]]*"([^"\\]|\\.)*"/"password":"<redacted>"/g' \
        -e 's/"password"[[:space:]]*:[[:space:]]*"[^"]*$/"password":"<redacted>/'
}

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

# [db] port = <int>; default 42009 when absent or not a plain integer.
read_db_port() {
    local re_db='^[[:space:]]*\[db\][[:space:]]*(#.*)?$'
    local re_tbl='^[[:space:]]*\['
    local re_port='^[[:space:]]*port[[:space:]]*=[[:space:]]*([0-9]+)[[:space:]]*(#.*)?$'
    local line in_db=0
    DB_PORT=42009
    while IFS= read -r line || [[ -n "$line" ]]; do
        line=${line%$'\r'}
        if [[ $line =~ $re_db ]]; then in_db=1; continue; fi
        if [[ $line =~ $re_tbl ]]; then in_db=0; continue; fi
        if (( in_db )) && [[ $line =~ $re_port ]]; then
            DB_PORT=$(( 10#${BASH_REMATCH[1]} ))
        fi
    done < "$LISTMONK_CONFIG"
}

read_listmonk_port() {
    LM_PORT=""
    local f="$SECRETS_DIR/listmonk.port" v=""
    [[ -r "$f" ]] || return 0
    IFS= read -r v < "$f" || true
    v="${v//[[:space:]]/}"
    [[ "$v" =~ ^[0-9]+$ ]] && LM_PORT="$v"
}

emit() {   # emit TOKEN EXIT — the single exit point that prints RESULT=
    echo "RESULT=$1"
    exit "$2"
}

skip() {   # fail-closed: nothing mutated, loud warning
    echo "ucc-postgres-upgrade: fail-closed skip ($1) — app-postgres NOT invoked"
    (( DRY_RUN )) || notify warning "ucc-postgres-upgrade: postgres upgrade SKIPPED fail-closed ($1). Postgres was not upgraded; UCC may gate it behind 'older build' — operator: fix the cause, or run 'app-postgres upgrade -p <listmonk db pw>' by hand."
    emit "skipped:$1" 3
}

tcp_open() {   # tcp_open PORT — bash /dev/tcp, bounded so it can never hang
    timeout 5 bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$1"' _ "$1" >/dev/null 2>&1
}

pg_healthy() {
    pgrep -f 'postgres: checkpointer' >/dev/null 2>&1 && tcp_open "$DB_PORT"
}

lm_healthy() {
    [[ -n "$LM_PORT" ]] || return 1
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${LM_PORT}/" 2>/dev/null)
    [[ "$code" == "200" ]]
}

poll() {   # poll BUDGET_S CHECK... — at least one check, then until deadline
    local budget="$1"; shift
    local deadline=$(( $(date +%s) + budget ))
    while :; do
        "$@" && return 0
        (( $(date +%s) >= deadline )) && return 1
        sleep "$HEALTH_POLL_INTERVAL_S"
    done
}

# ---- step 1-2: config reads (dry-run stops after these) --------------------
read_listmonk_db_password || skip "$PW_FAIL"
read_db_port
read_listmonk_port
command -v app-postgres >/dev/null 2>&1 || skip no_wrapper

ts=$(date -u +%Y-%m-%d_%H-%M-%S)
archive="$PG_BACKUP_DIR/qflix-postgres-${ts}.tar.gz"

if (( DRY_RUN )); then
    echo "  [DRY] backup: tar -czf $archive -C $(dirname "$PG_APP_DIR") $(basename "$PG_APP_DIR") (keep ${PG_BACKUP_KEEP})"
    echo "  [DRY] app-postgres upgrade -p <redacted>"
    echo "  [DRY] verify: checkpointer + tcp 127.0.0.1:${DB_PORT}; listmonk http 127.0.0.1:${LM_PORT:-<unreadable>}/"
    emit would_upgrade 0
fi

# ---- step 3: backup (umask 077 -> archive is 0600) -------------------------
umask 077
mkdir -p "$PG_BACKUP_DIR" 2>/dev/null
tar -czf "$archive" -C "$(dirname "$PG_APP_DIR")" "$(basename "$PG_APP_DIR")" >/dev/null 2>&1
tar_rc=$?
if (( tar_rc >= 2 )) || [[ ! -s "$archive" ]]; then
    rm -f -- "$archive"
    skip backup_failed
fi
chmod 600 "$archive" 2>/dev/null
echo "ucc-postgres-upgrade: backup ok ($(basename "$archive"), tar rc=$tar_rc)"

# ---- step 4: retention — newest-first, the new archive is never a candidate
kept=0
while IFS=$'\t' read -r _mt path; do
    [[ -n "$path" && "$path" != "$archive" ]] || continue
    kept=$((kept + 1))
    if (( kept >= PG_BACKUP_KEEP )); then
        rm -f -- "$path" && echo "ucc-postgres-upgrade: pruned $(basename "$path")"
    fi
done < <(find "$PG_BACKUP_DIR" -maxdepth 1 -type f -name 'qflix-postgres-*.tar.gz' -printf '%T@\t%p\n' 2>/dev/null | sort -rn)

# ---- step 5: the upgrade. Raw output never leaves this function's scope ----
echo "ucc-postgres-upgrade: app-postgres upgrade -p <redacted> (timeout ${PG_UPGRADE_TIMEOUT})"
out=$(timeout "$PG_UPGRADE_TIMEOUT" app-postgres upgrade -p "$PW" 2>&1)
rc=$?
older=""
shopt -s nocasematch
[[ "$out" == *"older build"* ]] && older=":older_build"
shopt -u nocasematch
result_false=0
[[ "$out" =~ \"result\"[[:space:]]*:[[:space:]]*false ]] && result_false=1

if (( rc == 124 )); then
    token="timeout"
elif (( rc == 0 && ! result_false )); then
    token="upgraded"
else
    token="error:upgrade_rc${rc}${older}"
fi

mkdir -p "$STATE_DIR" 2>/dev/null
{
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) app-postgres upgrade rc=$rc token=$token"
    redact "$out" | tail -n 20
} > "$STATE_DIR/postgres-upgrade.log" 2>/dev/null
chmod 600 "$STATE_DIR/postgres-upgrade.log" 2>/dev/null
out=""
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
        systemctl --user restart listmonk.service >/dev/null 2>&1
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
        notify error "ucc-postgres-upgrade: app-postgres upgrade succeeded but postgres is UNHEALTHY ($check_msg). Restore point: UCC pre-upgrade backup, then ${archive##*/}."
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
