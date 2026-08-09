#!/usr/bin/env bash
# 00-preflight.sh — read-only inventory snapshot of blue.
#
# Spec:  docs/superpowers/specs/2026-08-08-qflix-migration-blue-green-design.md
# Plan:  docs/superpowers/plans/2026-08-08-qflix-migration-plan.md
#
# WHAT THIS DOES
#   SSHes into blue (via lib/ssh.sh's `sshm`, never a hardcoded FQDN) and
#   local secrets/, and writes a single evidence file:
#     scripts/migrate/migration-state.json
#   covering: *arr app versions, the port map, media directory sizes, the
#   live systemd --user timer count+list, the Kuma monitor count (via
#   `manitoba-maint kuma audit`), and current disk quota.
#
# WHY IT HAS NO --execute GATE
#   This is a probe, not a mutation. Per the plan's conventions table, 00
#   and 40 are the two read-only scripts in scripts/migrate/ that run live
#   today instead of shipping inert (I-3 only binds mutating scripts).
#   Nothing here writes to blue — every remote command is a read (curl GET,
#   du, systemctl show/list-units, quota, kuma audit). Invariant I-2 holds.
#
# USAGE
#   scripts/migrate/00-preflight.sh
#   (no NEW_HOST argument — this script only ever looks at blue)
#   Override the output path for a test run: QFLIX_PREFLIGHT_OUT=/tmp/x.json
#
# EXIT CODES (house style: STAGE=<token> msg=<detail> on stderr)
#   0 — snapshot written, every probe succeeded
#   1 — snapshot written, but >=1 sub-probe degraded (see stderr for which)
#   2 — blue is unreachable over SSH at all; nothing was written
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # -> scripts/
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

OUT="${QFLIX_PREFLIGHT_OUT:-$HERE/migrate/migration-state.json}"
DEGRADED=0

# Records a non-fatal probe failure: keeps going (this is inventory, not a
# gate) but flips the final exit code to 1 so the failure isn't silently
# swallowed into a JSON file full of nulls.
note_degraded() { # $1=STAGE token  $2=msg detail
  echo "STAGE=$1 msg=$2" >&2
  DEGRADED=$((DEGRADED + 1))
}

# --- connectivity gate: this is the ONLY hard-fail condition -------------
if ! sshm 'echo ok' >/dev/null 2>&1; then
  echo "STAGE=blue-unreachable msg=ssh-connect-to-blue-failed" >&2
  exit 2
fi

# --- tiny JSON helpers (no jq/python3 dependency on the LOCAL side) ------
json_esc() { local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; printf '%s' "$s"; }
jstr() { [ -z "$1" ] && printf 'null' || printf '"%s"' "$(json_esc "$1")"; }
jnum() { [ -z "$1" ] && printf 'null' || printf '%s' "$1"; }

# ===========================================================================
# 1. *arr app versions — hit each app's own status API on the loopback port,
#    keyed by the LOCAL secrets/ copies (bootstrap-discover.sh keeps these
#    in sync with what's actually on blue). One ssh round-trip per app;
#    read-only GET, matches the pattern every canary/smoke-test already uses.
# ===========================================================================
#   name      key-secret     port-secret     urlbase-secret     api path
ARR_SPECS="
  sonarr    sonarr.key     sonarr.port     sonarr.urlbase     /api/v3/system/status
  sonarr2   sonarr2.key    sonarr2.port    sonarr2.urlbase    /api/v3/system/status
  radarr    radarr.key     radarr.port     radarr.urlbase     /api/v3/system/status
  radarr2   radarr2.key    radarr2.port    radarr2.urlbase    /api/v3/system/status
  prowlarr  prowlarr.key   prowlarr.port   prowlarr.urlbase   /api/v1/system/status
  bazarr    bazarr.key     bazarr.port     bazarr.urlbase     /api/system/status
  bazarr2   bazarr2.key    bazarr2.port    bazarr2.urlbase    /api/system/status
"
VER_KEYS=(); VER_VALS=()
while read -r name key_s port_s urlbase_s api_path; do
  [ -z "$name" ] && continue
  ver=""
  if secret_exists "$key_s" && secret_exists "$port_s"; then
    KEY="$(secret_read "$key_s")"; PORT="$(secret_read "$port_s")"
    UB=""; secret_exists "$urlbase_s" && UB="$(secret_read "$urlbase_s")"
    # Two response shapes cover every app on this list: sonarr/radarr/
    # prowlarr return a top-level "version"; bazarr/bazarr2 nest it at
    # data.bazarr_version (see scripts/install/06-bazarr2.sh for precedent).
    ver=$(sshm "curl -sf -m 10 -H 'X-Api-Key: $KEY' 'http://127.0.0.1:$PORT/$UB$api_path' 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get(\"version\") or (d.get(\"data\") or {}).get(\"bazarr_version\") or \"\")'" 2>/dev/null)
  fi
  if [ -z "$ver" ]; then
    note_degraded "arr-version-probe-failed" "no-version-for-$name"
  fi
  VER_KEYS+=("$name"); VER_VALS+=("$ver")
done <<< "$ARR_SPECS"

VERSIONS_JSON="{"
for i in "${!VER_KEYS[@]}"; do
  [ "$i" -gt 0 ] && VERSIONS_JSON+=","
  VERSIONS_JSON+="\"${VER_KEYS[$i]}\": $(jstr "${VER_VALS[$i]}")"
done
VERSIONS_JSON+="}"

# ===========================================================================
# 2. Port map — every secrets/*.port file, LOCAL glob (these are the same
#    files 15-bootstrap-new.sh will diff green's discovered ports against).
# ===========================================================================
PORTS_JSON="{"; first=1
for f in "$SECRETS_DIR"/*.port; do
  [ -e "$f" ] || continue
  base="$(basename "$f" .port)"
  val="$(tr -d '[:space:]' < "$f")"
  [ "$first" -eq 0 ] && PORTS_JSON+=","
  PORTS_JSON+="\"$(json_esc "$base")\": $(jstr "$val")"
  first=0
done
PORTS_JSON+="}"

# ===========================================================================
# 3. Media directory sizes — one ssh round-trip, du -sb per top-level
#    library (the four the spec's media-to-move table names). A missing
#    directory reports 0 on the remote side, not a probe failure — only a
#    totally unreachable ~/media is treated as degraded.
# ===========================================================================
DU_RAW=$(sshm "cd ~/media 2>/dev/null && for d in 'TV Shows' 'Movies' 'Anime' 'Anime Movies'; do sz=\$(du -sb \"\$d\" 2>/dev/null | cut -f1); printf '%s\t%s\n' \"\$d\" \"\${sz:-0}\"; done" 2>/dev/null)
DU_TVSHOWS=""; DU_MOVIES=""; DU_ANIME=""; DU_ANIMEMOVIES=""
if [ -z "$DU_RAW" ]; then
  note_degraded "media-du-failed" "no-response-from-blue-for-~/media-du"
else
  while IFS=$'\t' read -r dname dsize; do
    case "$dname" in
      "TV Shows")     DU_TVSHOWS="$dsize" ;;
      "Movies")       DU_MOVIES="$dsize" ;;
      "Anime")        DU_ANIME="$dsize" ;;
      "Anime Movies") DU_ANIMEMOVIES="$dsize" ;;
    esac
  done <<< "$DU_RAW"
fi

# ===========================================================================
# 4. systemd --user timers — full unit list (not just active ones), so a
#    disabled-but-still-declared timer still counts. First field of
#    `list-units` is always the bare unit name, unlike list-timers' variable
#    -width timestamp columns.
# ===========================================================================
TIMER_RAW=$(sshm "systemctl --user list-units --all --type=timer --no-legend --no-pager 2>/dev/null | awk '{print \$1}'" 2>/dev/null)
TIMER_COUNT=""; TIMER_LIST_JSON="[]"
if [ -z "$TIMER_RAW" ]; then
  note_degraded "systemd-timers-failed" "no-response-from-blue-for-list-units"
else
  TIMER_COUNT=$(printf '%s\n' "$TIMER_RAW" | grep -c .)
  TIMER_LIST_JSON="["; first=1
  while IFS= read -r u; do
    [ -z "$u" ] && continue
    [ "$first" -eq 0 ] && TIMER_LIST_JSON+=","
    TIMER_LIST_JSON+="$(jstr "$u")"
    first=0
  done <<< "$TIMER_RAW"
  TIMER_LIST_JSON+="]"
fi

# ===========================================================================
# 5. Kuma monitor count — via `manitoba-maint kuma audit`, the same command
#    smoke-test.sh and 240-maintenance-install.sh already trust. Its own
#    exit code is 0=no-drift / 2=drift / 3=error; we record it but don't act
#    on it here (40-validate-green.sh is where drift becomes a gate).
# ===========================================================================
KUMA_RAW=$(sshm "MANITOBA_MANIFEST=~/.opt/maint/apps.yaml ~/bin/manitoba-maint kuma audit 2>&1; echo EXITCODE=\$?" 2>/dev/null)
KUMA_LINE=$(printf '%s\n' "$KUMA_RAW" | grep -m1 '^manifest monitors:')
KUMA_MANIFEST_CT=$(printf '%s' "$KUMA_LINE" | grep -oE 'manifest monitors: [0-9]+' | grep -oE '[0-9]+')
KUMA_LIVE_CT=$(printf '%s' "$KUMA_LINE" | grep -oE 'kuma monitors: [0-9]+' | grep -oE '[0-9]+')
KUMA_MATCHED_CT=$(printf '%s' "$KUMA_LINE" | grep -oE 'matched: [0-9]+' | grep -oE '[0-9]+')
KUMA_EXIT=$(printf '%s\n' "$KUMA_RAW" | grep -oE 'EXITCODE=[0-9]+' | tail -1 | cut -d= -f2)
if [ -z "$KUMA_LIVE_CT" ]; then
  note_degraded "kuma-audit-unparseable" "no-manifest-monitors-line-in-audit-output"
fi

# ===========================================================================
# 6. Disk quota — same `quota -p` parse as canaries/quota.sh, minus the
#    action thresholds (this is a snapshot, not the canary's autonomous
#    reclaim trigger).
# ===========================================================================
QLINE=$(sshm "quota -p 2>/dev/null | awk '\$1 ~ /^\/dev\//'" 2>/dev/null)
QUOTA_PCT=""; QUOTA_USED_GB=""; QUOTA_LIMIT_GB=""
if [ -n "$QLINE" ]; then
  QUSED=$(printf '%s' "$QLINE" | awk '{print $2}' | tr -d '*')
  QLIMIT=$(printf '%s' "$QLINE" | awk '{print $3}')
  if [ -n "$QUSED" ] && [ -n "$QLIMIT" ] && [ "$QLIMIT" -gt 0 ] 2>/dev/null; then
    QUOTA_PCT=$(awk -v u="$QUSED" -v l="$QLIMIT" 'BEGIN{printf "%.2f", (u/l)*100}')
    QUOTA_USED_GB=$(awk -v u="$QUSED" 'BEGIN{printf "%.1f", u/1048576}')
    QUOTA_LIMIT_GB=$(awk -v l="$QLIMIT" 'BEGIN{printf "%.1f", l/1048576}')
  fi
fi
if [ -z "$QUOTA_PCT" ]; then
  note_degraded "quota-unparseable" "no-dev-line-in-quota--p-output"
fi

# --- assemble + write atomically ------------------------------------------
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TMP="$OUT.tmp.$$"
mkdir -p "$(dirname "$OUT")"
{
  printf '{\n'
  printf '  "schema": 1,\n'
  printf '  "generated_at": %s,\n' "$(jstr "$TS")"
  printf '  "blue_reachable": true,\n'
  printf '  "app_versions": %s,\n' "$VERSIONS_JSON"
  printf '  "ports": %s,\n' "$PORTS_JSON"
  printf '  "media_du_bytes": {\n'
  printf '    "TV Shows": %s,\n' "$(jnum "$DU_TVSHOWS")"
  printf '    "Movies": %s,\n' "$(jnum "$DU_MOVIES")"
  printf '    "Anime": %s,\n' "$(jnum "$DU_ANIME")"
  printf '    "Anime Movies": %s\n' "$(jnum "$DU_ANIMEMOVIES")"
  printf '  },\n'
  printf '  "systemd_timers": { "count": %s, "units": %s },\n' "$(jnum "$TIMER_COUNT")" "$TIMER_LIST_JSON"
  printf '  "kuma": { "manifest_count": %s, "live_count": %s, "matched": %s, "audit_exit": %s },\n' \
    "$(jnum "$KUMA_MANIFEST_CT")" "$(jnum "$KUMA_LIVE_CT")" "$(jnum "$KUMA_MATCHED_CT")" "$(jnum "$KUMA_EXIT")"
  printf '  "quota": { "pct": %s, "used_gb": %s, "limit_gb": %s },\n' \
    "$(jnum "$QUOTA_PCT")" "$(jnum "$QUOTA_USED_GB")" "$(jnum "$QUOTA_LIMIT_GB")"
  printf '  "degraded_probe_count": %s\n' "$DEGRADED"
  printf '}\n'
} > "$TMP"
mv -f "$TMP" "$OUT"

if [ "$DEGRADED" -gt 0 ]; then
  echo "STAGE=preflight-degraded msg=$DEGRADED-probe(s)-failed-see-stderr-above-wrote=$OUT" >&2
  exit 1
fi
echo "PASS: preflight snapshot written to $OUT"
exit 0
