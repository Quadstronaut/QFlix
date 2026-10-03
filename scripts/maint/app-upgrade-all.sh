#!/usr/bin/env bash
# app-upgrade-all.sh — sequentially upgrade every installed UCC app that
# supports `app-<name> upgrade`. Replaces cp_upgrade_clicker.py (Playwright
# UI automation against cp.ultra.cc).
#
# Discovery: walks ~/.apps/<name>/ dirs, checks that a matching `app-<name>`
# command exists AND lists `upgrade` in its --help, AND isn't in the skip
# list. Sequential per Ultra.cc FAQ (one upgrade at a time).
#
# Usage:
#   app-upgrade-all.sh                  # live sweep, default skip list
#   app-upgrade-all.sh --dry-run        # enumerate + show plan, do nothing
#   app-upgrade-all.sh --only seerr     # comma-separated substring filter
#   app-upgrade-all.sh --no-backup      # pass -n to each upgrade (saves disk)
#   app-upgrade-all.sh --include nginx  # comma-separated names to UN-skip
#
# Postgres (since 2026-10-02) is NOT upgraded inline: it runs FIRST, via the
# child ucc-postgres-upgrade.sh, which reads listmonk's DB password, tarballs
# ~/.apps/postgres, runs `app-postgres upgrade -p <pw>` (never -n, even under
# --no-backup: UCC's own backup is always kept) and verifies postgres +
# listmonk afterwards. A fail-closed skip there counts as a FAILURE (loud).
# Residual: app-postgres only takes the password in argv, so it is visible in
# ps for the upgrade's duration; see the child's header. This script never
# holds the password (and never reads the password key from config.toml); all
# failure text is redacted before echo/notify/RESULTS.
#
# The child runs under `timeout -k $PG_OUTER_KILL_GRACE_S[90] $OUTER`,
# OUTER = PG_BACKUP_TIMEOUT + PG_UPGRADE_TIMEOUT + PG_HEALTH_TIMEOUT_S +
# LISTMONK_HEALTH_TIMEOUT_S + 60 (~24 min at defaults). PG_OUTER_TIMEOUT_S
# (integer >= 1) replaces OUTER; both it and the grace are test seams. After an
# outer rc 124/137 THIS script probes postgres itself (uid-scoped checkpointer +
# TCP on the [db] port parsed from LISTMONK_CONFIG) and prints one line,
# "postgres probe after outer timeout: ok|down". down forces the summary notify
# to error and RESULTS[postgres]="error: postgres_unhealthy_after_timeout".
# BOUNDED RESIDUAL: a child SIGKILLed by the outer -k can leave app-postgres
# orphaned; it sits in its own process group under `timeout -k 30`, so its
# lifetime is capped (PG_UPGRADE_TIMEOUT + 30 s), and it keeps the child's flock
# (fd 9) while alive, so a second run cannot overlap it.
#
# Every env knob is validated by regex BEFORE arithmetic; an invalid value
# silently becomes its default (bad env never aborts the sweep).
#
# Exit codes:
#   0 — sweep complete, every targeted upgrade succeeded
#   1 — at least one upgrade failed or timed out
#   2 — fatal error before sweep (no installed apps, etc.)

set -u
# Never inherit a PW from the environment (redact() keys on it, and an exported
# PW would reach app-postgres's environment through the child).
unset PW
shopt -u patsub_replacement 2>/dev/null
shopt -s nullglob

# int_env NAME MIN MAX DEFAULT — NAME := validated base-10 integer or DEFAULT
# ("" = unset). The regex runs first, so no env value is ever evaluated by
# $(( )) / (( )) before it is proven to be digits.
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

# dur_env NAME DEFAULT_SECONDS — NAME := a `timeout`-style N[smhd] as integer
# seconds (^[0-9]{1,7}[smhd]?$, 1..604800; 0 would mean "no limit").
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
    printf -v "$name" '%s' "$n"
}

dur_env PG_UPGRADE_TIMEOUT 480
dur_env PG_BACKUP_TIMEOUT 600
int_env PG_HEALTH_TIMEOUT_S 0 86400 180
int_env LISTMONK_HEALTH_TIMEOUT_S 0 86400 120
int_env HEALTH_POLL_INTERVAL_S 1 3600 5
int_env PG_OUTER_TIMEOUT_S 1 604800 ""
int_env PG_OUTER_KILL_GRACE_S 1 3600 90
# Total sweep budget. Default 3h30m (12600 s) for a standalone run; the window
# orchestrator overrides it (MANITOBA_UPGRADE_BUDGET_S, ~2h30m) so the sweep +
# green-poll fit inside the 4h window. Bailed apps are recorded so the budget is
# observable.
int_env MANITOBA_UPGRADE_BUDGET_S 1 604800 12600
LISTMONK_CONFIG="${LISTMONK_CONFIG:-$HOME/.apps/listmonk/etc/config.toml}"

PER_APP_TIMEOUT="8m"
TOTAL_BUDGET_SECONDS="$MANITOBA_UPGRADE_BUDGET_S"
# Structured results file (the newsletter's "what we tuned" data source). The
# window orchestrator points this at ~/.opt/maint/last-upgrade.json.
RESULTS_FILE="${MANITOBA_UPGRADE_RESULTS:-$HOME/.opt/maint/last-upgrade.json}"

# Apps to never auto-upgrade by default — data risk, root-managed, or
# operator-sensitive. Override with --include name1,name2.
# postgres was removed 2026-10-02: skipping it let UCC age it into the
# "older build" start/restart gate (listmonk down 2 days). It now upgrades
# through ucc-postgres-upgrade.sh, which keeps its password stable.
DEFAULT_SKIP=(mariadb nginx tailscale openvpn wireguard)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PG_MODULE="$SCRIPT_DIR/ucc-postgres-upgrade.sh"

DRY_RUN=0
NO_BACKUP=0
ONLY_FILTERS=()
INCLUDE=()

die() { echo "FATAL: $*" >&2; exit 2; }

usage() {
    awk 'NR > 1 { if ($0 ~ /^#/) { sub(/^# ?/, ""); print } else exit }' "$0"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=1; shift ;;
        --no-backup)  NO_BACKUP=1; shift ;;
        --only)       [[ -n "${2:-}" ]] || die "--only needs a value"
                      IFS=',' read -ra ONLY_FILTERS <<<"$2"; shift 2 ;;
        --include)    [[ -n "${2:-}" ]] || die "--include needs a value"
                      IFS=',' read -ra INCLUDE <<<"$2"; shift 2 ;;
        -h|--help)    usage ;;
        *)            die "unknown arg: $1" ;;
    esac
done

# Build effective skip list (default minus --include items)
SKIP=()
for s in "${DEFAULT_SKIP[@]}"; do
    keep=1
    for inc in "${INCLUDE[@]}"; do
        [[ "$s" == "$inc" ]] && { keep=0; break; }
    done
    (( keep )) && SKIP+=("$s")
done

in_list() {
    local needle="$1"; shift
    for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
    return 1
}

# redact TEXT -> TEXT. (i) masks every JSON "password":"..." value: UCC's CLI
# echoes credentials in plaintext JSON, and failure text flows to stdout
# (journald), notify (Discord + notify.log) and RESULTS; the key is matched
# case-insensitively, plus the cut-off-at-end-of-line form. (ii)-(iv) mask
# literal $PW and its JSON-escaped / slash-escaped forms when set (this script
# never holds one; the child has the same function). Bash expansions, so a
# password never becomes a sed argv. Duplicated in ucc-postgres-upgrade.sh on
# purpose: no sourced helper to ship.
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

# UCC app-manager prints "Sub-commands:" (hyphen) since ~2026-08-18; matching
# only "Subcommands:" silently emptied every sweep for six weeks.
has_upgrade_verb() {
    "$1" --help 2>/dev/null \
        | awk '/^Sub-?commands:/{f=1;next} f && /^[[:space:]]+upgrade[[:space:]]/{found=1} END{exit !found}'
}

# Best-effort Discord notify via the canonical lib.notify helper. Notifiarr
# was retired 2026-05-10; the webhook lives in secrets/discord-webhook.url.
# Silent no-op if the secret/helper aren't present.
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

# Emit a structured results file the newsletter reads ("what we tuned"). App
# names are safe slugs (alnum + hyphen); result detail is collapsed to a fixed
# category token so the JSON is always well-formed (no escaping of free-text
# error messages). Written before every exit so a results file always exists.
write_results_json() {
    local out="$RESULTS_FILE"
    local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    mkdir -p "$(dirname "$out")" 2>/dev/null || true
    local apps_json="" up_json="" first=1 firstup=1 catv name
    for name in "${TARGETS[@]}"; do
        case "${RESULTS[$name]:-}" in
            upgraded*)      catv="upgraded" ;;
            timeout*)       catv="timeout" ;;
            would_upgrade*) catv="would_upgrade" ;;
            skipped*)       catv="skipped" ;;
            "")             catv="unknown" ;;
            *)              catv="error" ;;
        esac
        (( first )) && first=0 || apps_json+=","
        apps_json+="\"${name}\":\"${catv}\""
        if [[ "$catv" == "upgraded" ]]; then
            (( firstup )) && firstup=0 || up_json+=","
            up_json+="\"${name}\""
        fi
    done
    local mode_l="live"; (( DRY_RUN )) && mode_l="dry-run"
    printf '{"schema_version":1,"generated_at":"%s","mode":"%s","summary":{"upgraded":%d,"failed":%d,"bailed":%d,"total":%d,"skipped":%d},"apps":{%s},"upgraded":[%s]}\n' \
        "$ts" "$mode_l" "${upgraded:-0}" "${failed:-0}" "${bailed:-0}" "${#TARGETS[@]}" "${#SKIPPED[@]}" "$apps_json" "$up_json" \
        > "$out" 2>/dev/null || echo "WARN: could not write results to $out" >&2
}

# Discover installed apps
mapfile -t INSTALLED < <(
    for d in "$HOME"/.apps/*/; do
        [[ -d "$d" ]] && basename "$d"
    done | sort -u
)
(( ${#INSTALLED[@]} > 0 )) || die "no installed apps found under $HOME/.apps/"

TARGETS=()
SKIPPED=()
for name in "${INSTALLED[@]}"; do
    cmd="app-${name}"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        SKIPPED+=("$name: no app-* wrapper")
        continue
    fi
    if in_list "$name" "${SKIP[@]}"; then
        SKIPPED+=("$name: in skip list")
        continue
    fi
    if ! has_upgrade_verb "$cmd"; then
        SKIPPED+=("$name: no upgrade verb")
        continue
    fi
    if (( ${#ONLY_FILTERS[@]} > 0 )); then
        match=0
        lower_name="${name,,}"
        for f in "${ONLY_FILTERS[@]}"; do
            lower_f="${f,,}"
            [[ "$lower_name" == *"$lower_f"* ]] && { match=1; break; }
        done
        (( match )) || { SKIPPED+=("$name: filtered by --only"); continue; }
    fi
    TARGETS+=("$name")
done

# Postgres first: the budget can never starve it, and listmonk gets the most
# time to recover before the window's "Maintenance Window Complete" campaign.
if in_list postgres "${TARGETS[@]}"; then
    _rest=()
    for name in "${TARGETS[@]}"; do [[ "$name" == postgres ]] || _rest+=("$name"); done
    TARGETS=(postgres "${_rest[@]}")
fi

# Declared before the early-exit so write_results_json always has them.
declare -A RESULTS
upgraded=0; failed=0; bailed=0

mode="LIVE"; (( DRY_RUN )) && mode="DRY-RUN"
echo "[$mode] app-upgrade-all sweep starting"
echo "  installed=${#INSTALLED[@]} target=${#TARGETS[@]} skipped=${#SKIPPED[@]}"
echo "  budget=${TOTAL_BUDGET_SECONDS}s per_app_timeout=${PER_APP_TIMEOUT}"
echo "  skip_list=${SKIP[*]:-<empty>}"
if (( ${#TARGETS[@]} == 0 )); then
    echo "no apps to upgrade"
    for s in "${SKIPPED[@]}"; do echo "  skip: $s"; done
    write_results_json
    # Installed wrappers but nothing upgradeable, unfiltered, is a broken
    # probe (UCC help-format drift), never a quiet week: say so and fail.
    if (( ! DRY_RUN && ${#ONLY_FILTERS[@]} == 0 )); then
        notify warning "app-upgrade-all (${mode}): 0 upgradeable apps of ${#INSTALLED[@]} installed - upgrade-verb probe likely broken (UCC help format?); NOTHING was upgraded"
        exit 1
    fi
    exit 0
fi
echo "  targets: ${TARGETS[*]}"

upgrade_args=()
(( NO_BACKUP )) && upgrade_args+=(--no-backup)

start_epoch=$(date +%s)

# The only RESULT tokens the parent will believe. Anything else (an off-list
# token, a forged line, a missing line) is "no result".
PG_TOKEN_RE='^(upgraded|would_upgrade|timeout|skipped:(no_config|no_password|short_password|unparseable_password|backup_failed|no_wrapper|locked|no_lock)|error:(upgrade_rc[0-9]{1,3}(:older_build)?|postgres_unhealthy|listmonk_unhealthy|interrupted|internal|bad_usage))$'
PG_PROBE_DOWN=0   # set when the post-outer-timeout probe finds postgres down

# [db] port from listmonk's config.toml — regex-only, and ONLY the port key: this
# script never reads (or even matches) the password key. Default 42009.
read_pg_port() {
    local re_db='^[[:space:]]*\[db\][[:space:]]*(#.*)?$'
    local re_tbl='^[[:space:]]*\['
    local re_port='^[[:space:]]*port[[:space:]]*=[[:space:]]*([0-9]{1,5})[[:space:]]*(#.*)?$'
    local line in_db=0 p
    PG_PORT=42009
    [[ -r "$LISTMONK_CONFIG" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line=${line%$'\r'}
        if [[ $line =~ $re_db ]]; then in_db=1; continue; fi
        if [[ $line =~ $re_tbl ]]; then in_db=0; continue; fi
        if (( in_db )) && [[ $line =~ $re_port ]]; then
            p=$(( 10#${BASH_REMATCH[1]} ))
            (( p >= 1 && p <= 65535 )) && PG_PORT=$p
        fi
    done < "$LISTMONK_CONFIG"
}

# Parent-side postgres probe, independent of the child: uid-scoped checkpointer
# AND a TCP connect to the [db] port, polled for up to min(PG_HEALTH_TIMEOUT_S, 60).
pg_probe_after_timeout() {
    local budget=$PG_HEALTH_TIMEOUT_S now rem
    (( budget > 60 )) && budget=60
    read_pg_port
    local deadline=$(( $(date +%s) + budget ))
    while :; do
        if pgrep -u "$(id -u)" -f 'postgres: checkpointer' >/dev/null 2>&1 \
           && timeout 5 bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$1"' _ "$PG_PORT" >/dev/null 2>&1; then
            return 0
        fi
        now=$(date +%s)
        (( now >= deadline )) && return 1
        rem=$(( deadline - now ))
        sleep $(( rem < HEALTH_POLL_INTERVAL_S ? rem : HEALTH_POLL_INTERVAL_S ))
    done
}

# Postgres goes through the child module. Its stdout is never echoed raw: only
# the mapped result, plus its progress lines after redact().
run_postgres_module() {
    local pg_args=() pg_out pg_rc token last_line body outer tag="DO "
    (( DRY_RUN )) && { pg_args+=(--dry-run); tag="DRY"; }
    echo "  [$tag] postgres (via ucc-postgres-upgrade.sh)"
    if [[ ! -x "$PG_MODULE" ]]; then
        echo "      FAIL: postgres module missing or not executable"
        RESULTS[postgres]="error: postgres_module_missing"
        failed=$((failed + 1))
        return
    fi
    # Room for the tar backup + the upgrade + both health polls + slack.
    if [[ -n "$PG_OUTER_TIMEOUT_S" ]]; then
        outer=$PG_OUTER_TIMEOUT_S
    else
        outer=$(( PG_BACKUP_TIMEOUT + PG_UPGRADE_TIMEOUT + PG_HEALTH_TIMEOUT_S + LISTMONK_HEALTH_TIMEOUT_S + 60 ))
    fi
    pg_out=$(timeout -k "$PG_OUTER_KILL_GRACE_S" "${outer}s" "$PG_MODULE" "${pg_args[@]}" 2>&1)
    pg_rc=$?
    last_line="${pg_out##*$'\n'}"
    last_line="${last_line%$'\r'}"
    token=""
    if [[ "$last_line" == RESULT=* ]]; then
        token="${last_line#RESULT=}"
        [[ "$token" =~ $PG_TOKEN_RE ]] || token=""
    fi
    if [[ "$pg_out" == *$'\n'* ]]; then
        body="${pg_out%$'\n'*}"
        redact "$body" | sed 's/^/      | /'
    fi

    # Outer timeout (124) or kill (137): the child may have died mid-upgrade, so
    # ask postgres itself — never trust the child's last word.
    PG_PROBE_DOWN=0
    if (( pg_rc == 124 || pg_rc == 137 )); then
        if pg_probe_after_timeout; then
            echo "      postgres probe after outer timeout: ok"
        else
            echo "      postgres probe after outer timeout: down"
            PG_PROBE_DOWN=1
        fi
    fi

    if (( PG_PROBE_DOWN )); then
        RESULTS[postgres]="error: postgres_unhealthy_after_timeout"
        failed=$((failed + 1))
    else
        case "$token" in
            upgraded)      RESULTS[postgres]="upgraded"; upgraded=$((upgraded + 1)) ;;
            would_upgrade) RESULTS[postgres]="would_upgrade" ;;
            skipped:*)     RESULTS[postgres]="skipped: fail-closed ${token#skipped:}"; failed=$((failed + 1)) ;;
            timeout)       RESULTS[postgres]="timeout"; failed=$((failed + 1)) ;;
            error:*)       RESULTS[postgres]="error: ${token#error:}"; failed=$((failed + 1)) ;;
            *)
                if (( pg_rc == 124 || pg_rc == 137 )); then
                    RESULTS[postgres]="timeout"
                else
                    RESULTS[postgres]="error: postgres_no_result"
                fi
                failed=$((failed + 1)) ;;
        esac
    fi
    echo "      -> ${RESULTS[postgres]}"
}

for name in "${TARGETS[@]}"; do
    elapsed=$(( $(date +%s) - start_epoch ))
    if (( elapsed > TOTAL_BUDGET_SECONDS )); then
        RESULTS[$name]="skipped: budget"
        bailed=$((bailed + 1))
        continue
    fi
    cmd="app-${name}"
    if [[ "$name" == postgres ]]; then
        run_postgres_module
        continue
    fi
    if (( DRY_RUN )); then
        echo "  [DRY] $cmd upgrade ${upgrade_args[*]:-}"
        RESULTS[$name]="would_upgrade"
        continue
    fi
    printf "  [DO ] %-22s ... " "$name"
    out=$(timeout "$PER_APP_TIMEOUT" "$cmd" upgrade "${upgrade_args[@]}" 2>&1)
    rc=$?
    if (( rc == 0 )); then
        echo "OK ($(( $(date +%s) - start_epoch ))s elapsed)"
        RESULTS[$name]="upgraded"
        upgraded=$((upgraded + 1))
    elif (( rc == 124 )); then
        echo "TIMEOUT (>${PER_APP_TIMEOUT})"
        RESULTS[$name]="timeout"
        failed=$((failed + 1))
    else
        last=$(printf '%s\n' "$out" | tail -1)
        last=$(redact "$last")
        echo "FAIL rc=$rc: $last"
        RESULTS[$name]="error rc=$rc: ${last:0:120}"
        failed=$((failed + 1))
    fi
done

summary="app-upgrade-all (${mode}): upgraded=${upgraded} failed=${failed} bailed=${bailed} total=${#TARGETS[@]}"
echo
echo "$summary"
detail=""
for name in "${TARGETS[@]}"; do
    line="  ${name}: ${RESULTS[$name]:-?}"
    echo "$line"
    detail+="${name}: ${RESULTS[$name]:-?}\n"
done
for s in "${SKIPPED[@]}"; do echo "  skip: $s"; done

level="info"
(( failed > 0 || bailed > 0 )) && level="warning"
# Postgres down after an outer timeout is an outage, not a warning.
(( PG_PROBE_DOWN )) && level="error"
# Unconditional (matches master): dry-run summary notify behaviour is unchanged.
notify "$level" "${summary}"$'\n'"${detail}"

write_results_json

(( failed > 0 )) && exit 1
exit 0
