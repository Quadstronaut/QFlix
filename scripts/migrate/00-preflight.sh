#!/usr/bin/env bash
# 00-preflight.sh -- read-only inventory of blue (and green, if given).
#
# Spec: docs/superpowers/specs/2026-10-09-ucc-divorce-design.md section 8
# (row 00: "Keep, read-only. Add host.profile, quota, glibc and task-budget
# checks"). Re-cut from origin/feature/migration (QFLX-39).
#
# WHAT IT RECORDS -> secrets/migrate/migration-state.json (gitignored; it holds
# ports and versions, and this repo is public):
#   blue:  host.profile (fail-closed, I-12), every ever-UCC app's version via
#          ~/bin/appctl (the app list comes from manifest/apps.yaml), the
#          manitoba-maint status summary, media library sizes, the systemd
#          --user timer list (40-validate-green's baseline), the Kuma audit
#          line, `quota -p`, and the task budget (ulimit -u vs tasks in use).
#   green: (only with NEW_HOST) host.profile (want generic; "unprovisioned"
#          before 15-bootstrap-new.sh), glibc, task budget, linger, python3 +
#          PyYAML, free space.
#
# WHY NO --execute: it mutates nothing (every remote command is a read). I-3
# binds mutating scripts only. It still runs the window guard: a probe is a
# box operation, and "no box operations Monday 11:00-15:00 UTC" means none.
#
# USAGE: 00-preflight.sh [NEW_HOST] [--old-host HOST]
# EXIT:  0 snapshot written, every probe ok | 1 written, >=1 probe degraded |
#        2 blue unreachable / profile unresolved / inside the window (nothing written)
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

usage() { echo "usage: $0 [NEW_HOST] [--old-host HOST]" >&2; }
mig_args "" "$@"
resolve_old_host
need_tables

DEGRADED=0
degraded() { printf 'STAGE=%s msg=%s\n' "$1" "${2// /-}" >&2; DEGRADED=$((DEGRADED + 1)); }

sshb true >/dev/null 2>&1 || stage 2 blue-unreachable "ssh-to-OLD_HOST-failed"
window_guard

json_esc() { local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; printf '%s' "$s"; }
jstr() { if [ -z "$1" ]; then printf 'null'; else printf '"%s"' "$(json_esc "$1")"; fi; }
jnum() { if [[ "$1" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then printf '%s' "$1"; else printf 'null'; fi; }

# --- 1. versions of every ever-UCC app, through appctl (manifest-generated) --
APPS="$(mm apps | awk -F'\t' '$4=="true"{print $1}' | tr '\n' ' ')"
[ -n "$APPS" ] || stage 2 manifest-table "no-ever-UCC-apps-in-manifest"
VER_RAW="$(sshb "for a in $APPS; do v=\$(~/bin/appctl version \$a 2>/dev/null | tail -n 1); printf '%s\t%s\n' \"\$a\" \"\$v\"; done" 2>/dev/null)"
VERSIONS_JSON="{"; first=1
for a in $APPS; do
  v="$(printf '%s\n' "$VER_RAW" | awk -F'\t' -v a="$a" '$1==a{print $2}' | tr -d '\r')"
  [ -n "$v" ] || degraded panel-version-probe-failed "no-version-for-$a"
  [ "$first" -eq 1 ] || VERSIONS_JSON+=","
  VERSIONS_JSON+="\"$a\": $(jstr "$v")"; first=0
done
VERSIONS_JSON+="}"

# --- 2. health summary (the same JSON contract QuadstroNot reads) ------------
STATUS_RAW="$(sshb 'MANITOBA_MANIFEST=~/.opt/maint/apps.yaml ~/bin/manitoba-maint status --all --json' 2>/dev/null)"
STATUS_SUMMARY="$(printf '%s' "$STATUS_RAW" | "$PY" -c 'import json,sys
try: print(json.dumps(json.load(sys.stdin)["summary"]))
except Exception: print("null")' 2>/dev/null)"
[ "${STATUS_SUMMARY:-null}" != null ] || degraded status-unparseable "manitoba-maint-status--all--json"

# --- 3. port map: the manifest's port secrets, from LOCAL secrets/ -----------
PORTS_JSON="{"; first=1
while IFS=$'\t' read -r name _c _s _u _unit _st _d psec; do
  [ -n "$psec" ] || continue
  val=""; secret_exists "$psec" && val="$(secret_read "$psec")"
  [ "$first" -eq 1 ] || PORTS_JSON+=","
  PORTS_JSON+="\"$(json_esc "$name")\": $(jstr "$val")"; first=0
done < <(mm apps)
PORTS_JSON+="}"

# --- 4. media sizes ----------------------------------------------------------
LIBS_Q=""; for l in "${MEDIA_LIBRARIES[@]}"; do LIBS_Q="$LIBS_Q $(printf '%q' "$l")"; done
DU_RAW="$(sshb "cd ~/$MEDIA_ROOT_REL 2>/dev/null && for d in$LIBS_Q; do s=\$(du -sb \"\$d\" 2>/dev/null | cut -f1); printf '%s\t%s\n' \"\$d\" \"\${s:-0}\"; done" 2>/dev/null)"
[ -n "$DU_RAW" ] || degraded media-du-failed "no-du-from-blue"
MEDIA_JSON="{"; first=1
for l in "${MEDIA_LIBRARIES[@]}"; do
  v="$(printf '%s\n' "$DU_RAW" | awk -F'\t' -v l="$l" '$1==l{print $2}')"
  [ "$first" -eq 1 ] || MEDIA_JSON+=","
  MEDIA_JSON+="\"$(json_esc "$l")\": $(jnum "$v")"; first=0
done
MEDIA_JSON+="}"

# --- 5. timers (40's baseline) ------------------------------------------------
TIMER_RAW="$(sshb "systemctl --user list-units --all --type=timer --no-legend --no-pager 2>/dev/null | awk '{print \$1}'" 2>/dev/null)"
TIMER_COUNT="$(printf '%s\n' "$TIMER_RAW" | grep -c . )"
[ "$TIMER_COUNT" -gt 0 ] || degraded systemd-timers-failed "no-timer-list-from-blue"
TIMERS_JSON="[$(printf '%s\n' "$TIMER_RAW" | grep . | while read -r u; do printf '"%s",' "$(json_esc "$u")"; done | sed 's/,$//')]"

# --- 6. Kuma audit line ------------------------------------------------------
KUMA_LINE="$(sshb 'MANITOBA_MANIFEST=~/.opt/maint/apps.yaml ~/bin/manitoba-maint kuma audit 2>&1' 2>/dev/null | grep -m1 '^manifest monitors:')"
[ -n "$KUMA_LINE" ] || degraded kuma-audit-unparseable "no-manifest-monitors-line"

# --- 7. quota + task budget (blue) --------------------------------------------
QLINE="$(sshb "quota -p 2>/dev/null | awk '\$1 ~ /^\\/dev\\//'" 2>/dev/null)"
QPCT=""
if [ -n "$QLINE" ]; then
  QPCT="$(printf '%s' "$QLINE" | awk '{u=$2; gsub(/\*/,"",u); if ($3>0) printf "%.2f", (u/$3)*100}')"
fi
[ -n "$QPCT" ] || degraded quota-unparseable "no-dev-line-in-quota--p"
TASKS_B="$(sshb 'printf "%s %s\n" "$(ulimit -u)" "$(ps -L -u "$(id -u)" --no-headers 2>/dev/null | wc -l)"' 2>/dev/null)"
[ -n "$TASKS_B" ] || degraded task-budget-failed "blue"

# --- 8. green (optional) -------------------------------------------------------
GREEN_JSON="null"
if [ -n "$NEW_HOST" ]; then
  if ! sshg true >/dev/null 2>&1; then
    degraded green-unreachable "ssh-to-NEW_HOST-failed"
  else
    GRAW="$(sshg '
      p=$(python3 ~/scripts/maint/lib/hostpolicy.py preflight 2>/dev/null | tail -n 1); echo "profile=${p:-unprovisioned}"
      echo "glibc=$(ldd --version 2>/dev/null | head -n 1 | grep -oE "[0-9]+\.[0-9]+$")"
      echo "tasks=$(ulimit -u) $(ps -L -u "$(id -u)" --no-headers 2>/dev/null | wc -l)"
      echo "linger=$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)"
      python3 -c "import yaml" 2>/dev/null && echo pyyaml=yes || echo pyyaml=no
      echo "free_kb=$(df -Pk ~ 2>/dev/null | awk "NR==2{print \$4}")"' 2>/dev/null | tr -d '\r')"
    gv() { printf '%s\n' "$GRAW" | sed -n "s/^$1=//p" | head -n 1; }
    GPROF="$(gv profile)"
    case "$GPROF" in
      generic) ;;
      unprovisioned) log_warn "green host.profile not written yet (15-bootstrap-new.sh writes it)" ;;
      ultra) log_warn "green declares profile=ultra: its own Monday window will apply (D-6)" ;;
      *) degraded green-profile "unexpected:$GPROF" ;;
    esac
    [ "$(gv linger)" = yes ] || degraded green-linger "loginctl-Linger-is-not-yes-(user-units-die-at-logout)"
    [ "$(gv pyyaml)" = yes ] || degraded green-pyyaml "python3-yaml-missing-on-green"
    GREEN_JSON="{\"profile\": $(jstr "$GPROF"), \"glibc\": $(jstr "$(gv glibc)"), \"tasks\": $(jstr "$(gv tasks)"), \"linger\": $(jstr "$(gv linger)"), \"free_kb\": $(jnum "$(gv free_kb)")}"
  fi
fi

mkdir_state
OUT="${QFLIX_PREFLIGHT_OUT:-$MSTATE/migration-state.json}"
TMP="$OUT.tmp.$$"
{
  printf '{\n  "schema": 2,\n  "generated_at": %s,\n' "$(jstr "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
  printf '  "blue": {"profile": %s, "tasks": %s, "quota_pct": %s},\n' "$(jstr "$OLD_PROFILE")" "$(jstr "$TASKS_B")" "$(jnum "$QPCT")"
  printf '  "app_versions": %s,\n  "status_summary": %s,\n  "ports": %s,\n' "$VERSIONS_JSON" "${STATUS_SUMMARY:-null}" "$PORTS_JSON"
  printf '  "media_du_bytes": %s,\n' "$MEDIA_JSON"
  printf '  "systemd_timers": {"count": %s, "units": %s},\n' "$(jnum "$TIMER_COUNT")" "$TIMERS_JSON"
  printf '  "kuma_audit": %s,\n  "green": %s,\n  "degraded_probe_count": %s\n}\n' "$(jstr "$KUMA_LINE")" "$GREEN_JSON" "$DEGRADED"
} > "$TMP" && mv -f "$TMP" "$OUT"
[ -f "$OUT" ] || stage 1 write-failed "could-not-write-$OUT"

if [ "$DEGRADED" -gt 0 ]; then
  printf 'STAGE=preflight-degraded msg=%d-probe(s)-failed wrote=%s\n' "$DEGRADED" "$OUT" >&2
  exit 1
fi
echo "PASS: preflight snapshot written to $OUT"
exit 0
