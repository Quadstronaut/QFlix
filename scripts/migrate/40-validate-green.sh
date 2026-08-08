#!/usr/bin/env bash
# 40-validate-green.sh -- read-only smoke test of green. Runs LIVE, no
# --execute gate: nothing here mutates (spec section 4 row 40 / plan step 7).
#
# Checks: (1) every manifest/apps.yaml app, health probe by class, secrets
# read from GREEN's OWN ~/secrets/ over ssh, never local blue-scoped secrets/;
# (2) `manitoba-maint kuma audit`, manifest vs live Kuma, expect 78/78; (3)
# `qflix-entitlement.py --arm-check`, the read-only rehearsal spec section 5
# asks 40 to run; (4) hardlink spot-check on synced media -- asserts media
# EXISTS (zero samples = sync never happened); linkcount is reported, not
# the pass/fail signal, because smoke-test.sh already found nlink=1 is the
# correct post-janitor steady state on blue and 30-sync-media's `rsync -aH`
# only preserves hardlinks already present in the synced set; (5) live
# systemd --user timer count vs the blue baseline in migration-state.json
# (61 on blue per the spec), tolerant of being short by one for invariant
# I-1's held-back newsletter timer (also why check 1 SKIPs that one app).
#
# Checks 1-4 share ONE ssh session (round trip for ~35 apps, matching the
# deploy-drift.sh / hardlink-integrity.sh canary pattern).
#
# Usage: 40-validate-green.sh NEW_HOST -- ssh target for green (user@host or
# an ssh-config alias). Never a hardcoded FQDN.
#
# Exit: 0 all passed (SKIPs allowed) | 1 a check failed | 2 could-not-assert
# (no NEW_HOST, or green unreachable over ssh).
set -uo pipefail

NEW_HOST="${1:-}"
if [ -z "$NEW_HOST" ]; then
  printf "STAGE=no-new-host msg=usage:-%s-NEW_HOST\n" "$(basename "$0")" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

# Green is always reached through the explicit NEW_HOST argument, never the
# blue-hardcoded sshm() from lib/ssh.sh -- that helper resolves secrets/seedbox
# host, which is blue's identity, not green's.
GREEN_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30)
sshg() { ssh "${GREEN_OPTS[@]}" "$NEW_HOST" "$@"; }

if ! sshg true >/dev/null 2>&1; then
  printf "STAGE=green-unreachable msg=ssh-to-%s-failed\n" "$NEW_HOST" >&2
  exit 2
fi

echo "== 40-validate-green: $NEW_HOST =="
echo

# One remote session does checks 1-4, single-quoted end to end: no local
# variable leaks in, which also means no apostrophe below until the closing
# quote (comments included) or the string closes early.
RES=$(sshg '
set -uo pipefail
TIMEOUT=5
pass=0; fail=0; skip=0
rec() {
  case "$1" in
    pass) pass=$((pass+1)) ;;
    fail) fail=$((fail+1)) ;;
    skip) skip=$((skip+1)) ;;
  esac
  printf "%s|%s|%s\n" "${1^^}" "$2" "$3"
}

# name|kind|f1|f2|f3 -- mirrors manifest/apps.yaml apps: (35 entries; keep in
# sync if that file changes). http_api: f1=header f2=secret-basename f3=path
# (may hold {urlbase}). http_root: f1=secret-basename f2=path f3=hostname
# override (default 127.0.0.1). systemd_only/process_pattern: f1=unit/pattern.
# systemd_oneshot: f1=unit, timer must be armed + last Result not failed.
# skip: f1=reason -- used for tdarr-server (port lives in a json config, not
# a secret), python-plexapi (library, no runtime health surface) and, per
# I-1, qflix-newsletter (must stay inert on green pre-cutover).
APPS="
sonarr|http_api|X-Api-Key|sonarr|/{urlbase}/api/v3/system/status
sonarr2|http_api|X-Api-Key|sonarr2|/{urlbase}/api/v3/system/status
radarr|http_api|X-Api-Key|radarr|/{urlbase}/api/v3/system/status
radarr2|http_api|X-Api-Key|radarr2|/{urlbase}/api/v3/system/status
prowlarr|http_api|X-Api-Key|prowlarr|/{urlbase}/api/v1/system/status
bazarr|http_api|X-Api-Key|bazarr|/{urlbase}/api/system/status
bazarr2|http_api|X-Api-Key|bazarr2|/{urlbase}/api/system/status
seerr|http_api|X-Api-Key|seerr|/api/v1/status
kavita|http_api|Authorization|kavita|/api/health
qbittorrent|http_root|qbittorrent|/|
plex|http_root|plex|/identity|
tautulli|http_root|tautulli|/|
audiobookshelf|http_root|audiobookshelf|/healthcheck|
komga|http_root|komga|/komga/|
calibre-web|http_root|calibre-web|/|
qflix-dash|http_root|qflix-dash|/healthz|
flaresolverr|http_root|flaresolverr|/|172.17.0.1
sabnzbd|http_root|sabnzbd|/sabnzbd/|
listmonk|http_root|listmonk|/|
victorialogs|http_root|vlogs|/health|
tdarr-node|systemd_only|tdarr-node.service||
unpackerr|process_pattern|/unpackerr||
postgres|process_pattern|postgres: checkpointer||
recyclarr|systemd_oneshot|recyclarr.service||
qflix-newsletter|skip|held inert pre-cutover, I-1: exactly one side may page||
buildarr|systemd_oneshot|buildarr.service||
upgradinatorr|systemd_oneshot|upgradinatorr.service||
bazarr2-sync|systemd_oneshot|bazarr2-sync.service||
kometa|systemd_oneshot|kometa.service||
qflix-missing-search|systemd_oneshot|qflix-missing-search.service||
qflix-quality-fallback|systemd_oneshot|qflix-quality-fallback.service||
qflix-specials-policy|systemd_oneshot|qflix-specials-policy.service||
qflix-vlogs-ingest|systemd_oneshot|qflix-vlogs-ingest.service||
tdarr-server|skip|port sourced from a json config, not a secret; validate manually||
python-plexapi|skip|library app, no runtime health surface||
"

while IFS="|" read -r name kind f1 f2 f3; do
  [ -z "$name" ] && continue
  case "$kind" in
    http_api)
      key=$(cat "$HOME/secrets/$f2.key" 2>/dev/null | tr -d "[:space:]")
      port=$(cat "$HOME/secrets/$f2.port" 2>/dev/null | tr -d "[:space:]")
      if [ -z "$key" ] || [ -z "$port" ]; then rec skip "$name" "missing $f2.key/.port on green"; continue; fi
      path="$f3"
      if [ "${path#*{urlbase\}}" != "$path" ]; then
        ub=$(cat "$HOME/secrets/$f2.urlbase" 2>/dev/null | tr -d "[:space:]")
        [ -z "$ub" ] && ub="$f2"
        path="${path//\{urlbase\}/$ub}"
      fi
      code=$(curl -s -o /dev/null -w "%{http_code}" -m "$TIMEOUT" -H "$f1: $key" "http://127.0.0.1:$port$path")
      if [ "$code" = "200" ]; then rec pass "$name" "http $code $path"; else rec fail "$name" "http ${code:-timeout} $path"; fi
      ;;
    http_root)
      port=$(cat "$HOME/secrets/$f1.port" 2>/dev/null | tr -d "[:space:]")
      if [ -z "$port" ]; then rec skip "$name" "missing $f1.port on green"; continue; fi
      pth="${f2:-/}"; host="${f3:-127.0.0.1}"
      code=$(curl -sL -o /dev/null -w "%{http_code}" -m "$TIMEOUT" "http://$host:$port$pth")
      if [ "$code" = "200" ]; then rec pass "$name" "http $code $pth"; else rec fail "$name" "http ${code:-timeout} $pth"; fi
      ;;
    systemd_only)
      st=$(systemctl --user is-active "$f1" 2>/dev/null)
      if [ "$st" = "active" ]; then rec pass "$name" "unit=$st"; else rec fail "$name" "unit=${st:-unknown}"; fi
      ;;
    process_pattern)
      if pgrep -f "$f1" >/dev/null 2>&1; then rec pass "$name" "pgrep matched"; else rec fail "$name" "no process matching pattern"; fi
      ;;
    systemd_oneshot)
      base="${f1%.service}"
      tst=$(systemctl --user is-active "$base.timer" 2>/dev/null)
      res=$(systemctl --user show "$f1" -p Result --value 2>/dev/null)
      if [ "$tst" != "active" ]; then rec fail "$name" "timer=${tst:-unknown} not armed"
      elif [ "$res" = "success" ] || [ -z "$res" ]; then rec pass "$name" "timer=active result=${res:-never-run}"
      else rec fail "$name" "timer=active result=$res"; fi
      ;;
    skip) rec skip "$name" "$f1" ;;
  esac
done <<< "$APPS"

# --- kuma audit: manifest vs live Kuma, expects 78/78 (spec section 1) -----
KOUT=$(python3 "$HOME/scripts/maint/manitoba-maint" kuma audit 2>&1); KRC=$?
MATCHED=$(printf "%s" "$KOUT" | grep -o "matched: [0-9]*" | grep -o "[0-9]*")
MTOTAL=$(printf "%s" "$KOUT" | grep -o "manifest monitors: [0-9]*" | grep -o "[0-9]*")
if [ "$KRC" = "0" ]; then
  rec pass "kuma-audit" "matched=${MATCHED:-?}/${MTOTAL:-?}"
else
  rec fail "kuma-audit" "rc=$KRC matched=${MATCHED:-?}/${MTOTAL:-?}"
fi

# --- entitlement arm-check: report-only, mutates nothing (spec section 5) --
EOUT=$(python3 "$HOME/scripts/maint/qflix-entitlement.py" --arm-check 2>&1); ERC=$?
ELINE=$(printf "%s" "$EOUT" | grep "arm-check:" | tail -1)
if [ "$ERC" = "0" ]; then
  rec pass "entitlement-arm-check" "${ELINE:-verdict green}"
else
  rec fail "entitlement-arm-check" "rc=$ERC ${ELINE:-no arm-check line in output}"
fi

# --- hardlink spot-check: media must EXIST; linkcount is reported, not the
# pass/fail signal (see the file header for why) ----------------------------
N=0; LINKED=0
while IFS=" " read -r ts p; do
  [ -z "$p" ] && continue
  N=$((N+1))
  nl=$(stat -c "%h" "$p" 2>/dev/null || echo 0)
  [ "${nl:-0}" -ge 2 ] && LINKED=$((LINKED+1))
done < <(for d in "$HOME/media/Movies" "$HOME/media/TV Shows" "$HOME/media/Anime" "$HOME/media/Anime Movies"; do
  [ -d "$d" ] && find "$d" -type f \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.m4v" -o -iname "*.avi" \) -printf "%T@ %p\n" 2>/dev/null
done | sort -rn | head -20)
if [ "$N" -eq 0 ]; then
  rec fail "hardlink-spotcheck" "no video files under any media root - sync missing or path wrong"
else
  rec pass "hardlink-spotcheck" "linked=$LINKED/$N sampled (nlink=1 is normal once a seed is cleaned up)"
fi

TCOUNT=$(systemctl --user list-timers --all --no-legend 2>/dev/null | grep -c .)
printf "META|timer_count|%s\n" "$TCOUNT"
printf "META|counts|pass=%s fail=%s skip=%s\n" "$pass" "$fail" "$skip"
')

# Render the checklist locally + run check 5, which needs the LOCAL repo
# copy of migration-state.json and so cannot live in the remote session above.
PASS_N=0; FAIL_N=0; SKIP_N=0; FAILED_NAMES=""; GREEN_TIMER_COUNT=""

print_row() {
  local status="$1" name="$2" detail="$3"
  case "$status" in
    PASS) printf "  [PASS] %-28s %s\n" "$name" "$detail"; PASS_N=$((PASS_N+1)) ;;
    FAIL) printf "  [FAIL] %-28s %s\n" "$name" "$detail"; FAIL_N=$((FAIL_N+1)); FAILED_NAMES="$FAILED_NAMES,$name" ;;
    SKIP) printf "  [SKIP] %-28s %s\n" "$name" "$detail"; SKIP_N=$((SKIP_N+1)) ;;
  esac
}

while IFS="|" read -r a b c; do
  [ -z "$a" ] && continue
  case "$a" in
    PASS|FAIL|SKIP) print_row "$a" "$b" "$c" ;;
    META) [ "$b" = "timer_count" ] && GREEN_TIMER_COUNT="$c" ;;
  esac
done <<< "$RES"

# Check 5: green's live timer count vs blue's baseline. 00-preflight.sh
# writes its snapshot to $HERE/migration-state.json (HERE is already
# scripts/migrate in this file) under the top-level key "systemd_timers":
# {"count": N, "units": [...]} -- read that key directly instead of guessing.
STATE_FILE="$HERE/migration-state.json"
BASELINE=""
if [ -f "$STATE_FILE" ] && command -v python3 >/dev/null 2>&1; then
  BASELINE=$(STATE_FILE="$STATE_FILE" python3 -c '
import json, os
try: d = json.load(open(os.environ["STATE_FILE"]))
except Exception: d = {}
n = d.get("systemd_timers", {}).get("count")
print(n if isinstance(n, int) else "")
' 2>/dev/null)
fi

if [ -z "$BASELINE" ]; then
  print_row SKIP "timer-count-vs-baseline" "no usable baseline in migration-state.json (run 00-preflight.sh first)"
elif [ -n "$GREEN_TIMER_COUNT" ] && [ "$GREEN_TIMER_COUNT" -ge $((BASELINE - 1)) ] 2>/dev/null; then
  print_row PASS "timer-count-vs-baseline" "green=$GREEN_TIMER_COUNT blue_baseline=$BASELINE (>=N-1, tolerates the I-1 held-back newsletter timer)"
else
  print_row FAIL "timer-count-vs-baseline" "green=${GREEN_TIMER_COUNT:-0} blue_baseline=$BASELINE"
fi

echo
echo "== summary: $PASS_N pass, $FAIL_N fail, $SKIP_N skip =="
if [ "$FAIL_N" -gt 0 ]; then
  printf "STAGE=validate-fail msg=%d-of-%d-checks-failed failed=%s\n" \
    "$FAIL_N" "$((PASS_N+FAIL_N+SKIP_N))" "${FAILED_NAMES#,}" >&2
  exit 1
fi
exit 0
