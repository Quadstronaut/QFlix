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
# Exit codes:
#   0 — sweep complete, every targeted upgrade succeeded
#   1 — at least one upgrade failed or timed out
#   2 — fatal error before sweep (no installed apps, etc.)

set -u
shopt -s nullglob

PER_APP_TIMEOUT="8m"
# Total sweep budget. Default 3h30m for a standalone run; the window orchestrator
# overrides it (MANITOBA_UPGRADE_BUDGET_S, ~2h30m) so the sweep + green-poll fit
# inside the 4h window. Bailed apps are recorded so the budget is observable.
TOTAL_BUDGET_SECONDS="${MANITOBA_UPGRADE_BUDGET_S:-$(( 3 * 3600 + 30 * 60 ))}"
# Structured results file (the newsletter's "what we tuned" data source). The
# window orchestrator points this at ~/.opt/maint/last-upgrade.json.
RESULTS_FILE="${MANITOBA_UPGRADE_RESULTS:-$HOME/.opt/maint/last-upgrade.json}"

# Apps to never auto-upgrade by default — data risk, root-managed, or
# operator-sensitive. Override with --include name1,name2.
DEFAULT_SKIP=(postgres mariadb nginx tailscale openvpn wireguard)

DRY_RUN=0
NO_BACKUP=0
ONLY_FILTERS=()
INCLUDE=()

die() { echo "FATAL: $*" >&2; exit 2; }

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
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

has_upgrade_verb() {
    "$1" --help 2>/dev/null \
        | awk '/^Subcommands:/{f=1;next} f && /^[[:space:]]+upgrade[[:space:]]/{found=1} END{exit !found}'
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
    exit 0
fi
echo "  targets: ${TARGETS[*]}"

upgrade_args=()
(( NO_BACKUP )) && upgrade_args+=(--no-backup)

start_epoch=$(date +%s)

for name in "${TARGETS[@]}"; do
    elapsed=$(( $(date +%s) - start_epoch ))
    if (( elapsed > TOTAL_BUDGET_SECONDS )); then
        RESULTS[$name]="skipped: budget"
        bailed=$((bailed + 1))
        continue
    fi
    cmd="app-${name}"
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
notify "$level" "${summary}"$'\n'"${detail}"

write_results_json

(( failed > 0 )) && exit 1
exit 0
