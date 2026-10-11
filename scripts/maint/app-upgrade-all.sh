#!/usr/bin/env bash
# app-upgrade-all.sh — sequentially upgrade every installed UCC app that
# supports `app-<name> upgrade`. Replaces cp_upgrade_clicker.py (Playwright
# UI automation against cp.ultra.cc).
#
# Discovery: walks ~/.apps/<name>/ dirs, checks that a matching `app-<name>`
# command exists AND lists `upgrade` in its --help, AND isn't in the skip
# list. Sequential per Ultra.cc FAQ (one upgrade at a time).
#
# Generated skip list (QFLX-17, UCC divorce spec I-9 / 5.7): a converted app
# keeps BOTH its ~/.apps/<slug> dir (the native unit's data dir) and its
# dormant app-<slug> wrapper, so discovery alone would `app-<slug> upgrade` a
# container that must stay stopped next to the native unit. Every run therefore
# generates, from the DEPLOYED manifest ($MANITOBA_MANIFEST, default
# ~/.opt/maint/apps.yaml, written by 240), every slug whose class is not `ucc`
# or that carries `ucc_dormant`, via lib/ucc_skip.py --list. It is joined to
# DEFAULT_SKIP, checked before anything else touches app-<slug>, and is NOT
# subject to --include/--only. If it cannot be generated the sweep fails closed
# (exit 2 + warning notify, nothing upgraded). Never restate it by hand.
#
# Usage:
#   app-upgrade-all.sh                  # live sweep, default skip list
#   app-upgrade-all.sh --dry-run        # enumerate + show plan, do nothing
#   app-upgrade-all.sh --only seerr     # comma-separated substring filter
#   app-upgrade-all.sh --no-backup      # pass -n to each upgrade (saves disk)
#   app-upgrade-all.sh --include nginx  # comma-separated names to UN-skip
#
# Postgres is NEVER upgraded here (QFLX-37). It runs native as
# qflix-postgres.service (UCC divorce A13), and the ucc-postgres-upgrade.sh
# child that drove `app-postgres upgrade -p <pw>` (password in argv, F-23) is
# retired. A UCC `app-postgres upgrade` without -p ROTATES the listmonk
# password, so the slug is refused before any probe, whatever the manifest,
# --include or --only say. Native minor upgrades go through the manifest
# upgrade block (lifecycle tarball_swap of the PGDG .deb); a major is a
# manual pg_upgrade runbook.
#
# Every env knob is validated by regex BEFORE arithmetic; an invalid value
# silently becomes its default (bad env never aborts the sweep).
#
# Exit codes:
#   0 — sweep complete, every targeted upgrade succeeded
#   1 — at least one upgrade failed or timed out
#   2 — fatal error before sweep (no installed apps, etc.)

set -u
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

# Total sweep budget. Default 3h30m (12600 s) for a standalone run; the window
# orchestrator overrides it (MANITOBA_UPGRADE_BUDGET_S, ~2h30m) so the sweep +
# green-poll fit inside the 4h window. Bailed apps are recorded so the budget is
# observable.
int_env MANITOBA_UPGRADE_BUDGET_S 1 604800 12600

PER_APP_TIMEOUT="8m"
TOTAL_BUDGET_SECONDS="$MANITOBA_UPGRADE_BUDGET_S"
# Structured results file (the newsletter's "what we tuned" data source). The
# window orchestrator points this at ~/.opt/maint/last-upgrade.json.
RESULTS_FILE="${MANITOBA_UPGRADE_RESULTS:-$HOME/.opt/maint/last-upgrade.json}"

# Apps to never auto-upgrade by default — data risk, root-managed, or
# operator-sensitive. Override with --include name1,name2.
# postgres is not listed: it is refused outright below (NEVER_UCC_UPGRADE),
# not merely skipped, so --include cannot un-skip it.
DEFAULT_SKIP=(mariadb nginx tailscale openvpn wireguard)

# QFLX-37: `app-postgres upgrade` rotates the listmonk DB password unless it
# is handed the old one in argv (F-23). Native postgres is upgraded by the
# manifest upgrade block instead; this sweep refuses the slug before any probe.
NEVER_UCC_UPGRADE=(postgres)

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

# redact TEXT -> TEXT. Masks every JSON "password":"..." value: UCC's CLI
# echoes credentials in plaintext JSON, and failure text flows to stdout
# (journald), notify (Discord + notify.log) and RESULTS; the key is matched
# case-insensitively, plus the cut-off-at-end-of-line form.
redact() {
    local s="$1"
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

# Generated skip list (see header). Fail closed: a sweep that cannot tell which
# apps are converted must not touch any of them.
MAINT_DIR="${MANITOBA_MAINT_DIR:-$HOME/scripts/maint}"
MANIFEST_FILE="${MANITOBA_MANIFEST:-$HOME/.opt/maint/apps.yaml}"
PYTHON_BIN="${MANITOBA_PYTHON:-python3}"
GEN_SKIP=()
skip_fail() {
    local msg="app-upgrade-all: cannot generate the converted/dormant skip list from ${MANIFEST_FILE} ($1) - NOTHING was upgraded (I-9: never wake a dormant app)"
    echo "FATAL: $msg" >&2
    notify warning "$msg"
    exit 2
}
gen_out=$("$PYTHON_BIN" "$MAINT_DIR/lib/ucc_skip.py" --manifest "$MANIFEST_FILE" --list)
gen_rc=$?
(( gen_rc == 0 )) || skip_fail "ucc_skip.py rc=$gen_rc"
while IFS= read -r line; do
    line=${line%$'\r'}
    [[ -z "$line" ]] && continue
    [[ "$line" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || skip_fail "unsafe slug in output"
    GEN_SKIP+=("$line")
done <<<"$gen_out"

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
    # First, before any app-<name> probe; --include/--only cannot override it.
    if in_list "$name" "${GEN_SKIP[@]}"; then
        SKIPPED+=("$name: converted/dormant (manifest)")
        continue
    fi
    if in_list "$name" "${NEVER_UCC_UPGRADE[@]}"; then
        SKIPPED+=("$name: never upgraded through app-$name (native, QFLX-37)")
        continue
    fi
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
echo "  generated_skip=${GEN_SKIP[*]:-<empty>} (manifest=${MANIFEST_FILE})"
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
# Unconditional (matches master): dry-run summary notify behaviour is unchanged.
notify "$level" "${summary}"$'\n'"${detail}"

write_results_json

(( failed > 0 )) && exit 1
exit 0
