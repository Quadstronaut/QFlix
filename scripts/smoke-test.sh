#!/usr/bin/env bash
# Phase 14: Manitoba smoke test. Idempotent. Exits non-zero on critical failures.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

PASS=0; FAIL=0; SKIP=0
record() {
  local name="$1" status="$2" detail="${3:-}"
  case "$status" in
    pass) printf '✓ %-38s %s\n' "$name" "$detail"; PASS=$((PASS+1)) ;;
    fail) printf '✗ %-38s %s\n' "$name" "$detail"; FAIL=$((FAIL+1)) ;;
    skip) printf '~ %-38s %s\n' "$name" "$detail"; SKIP=$((SKIP+1)) ;;
  esac
}

# 1. Prowlarr indexer count + at least 80% test pass-rate (already audited in Phase 3)
echo "1. Prowlarr indexers + reachability"
PROW_KEY="$(secret_read prowlarr.key)"
PROW_PORT="$(secret_read prowlarr.port)"
PROW_BASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"
COUNT=$(sshm "curl -sf -m 10 -H 'X-Api-Key: $PROW_KEY' http://127.0.0.1:$PROW_PORT/$PROW_BASE/api/v1/indexer 2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))'" 2>/dev/null)
if [ "${COUNT:-0}" -ge 5 ]; then
  record "indexer-count" pass "$COUNT indexers in Prowlarr"
else
  record "indexer-count" fail "only $COUNT indexers"
fi

# 2. Indexer search round-trip
echo "2. Indexer search"
# Retry once — Prowlarr search occasionally times out under load even though the next call is fast.
for attempt in 1 2; do
  RESULTS=$(sshm "curl -sf -m 120 -H 'X-Api-Key: $PROW_KEY' 'http://127.0.0.1:$PROW_PORT/$PROW_BASE/api/v1/search?query=ubuntu' 2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))'" 2>/dev/null)
  [ "${RESULTS:-0}" -ge 1 ] && break
done
if [ "${RESULTS:-0}" -ge 1 ]; then
  record "indexer-search" pass "$RESULTS results for ubuntu"
else
  record "indexer-search" fail "0 results after retry"
fi

# 3. *arr → qBit reachability (testall endpoint)
echo "3. *arr -> qBit"
for app in sonarr radarr sonarr2 radarr2 readarr; do
  KEY=$(secret_read $app.key 2>/dev/null || echo "")
  PORT=$(secret_read $app.port 2>/dev/null || echo "")
  BASE=$(secret_read $app.urlbase 2>/dev/null || echo $app)
  if [ -z "$KEY" ] || [ -z "$PORT" ]; then
    record "$app-qbit" skip "no key/port"
    continue
  fi
  V=v3; [ "$app" = "readarr" ] && V=v1
  CODE=$(sshm "curl -s -m 15 -X POST -H 'X-Api-Key: $KEY' http://127.0.0.1:$PORT/$BASE/api/$V/downloadclient/testall -d '{}' -H 'Content-Type: application/json' -o /dev/null -w '%{http_code}'")
  if [ "$CODE" = "200" ]; then
    record "$app-qbit" pass
  else
    record "$app-qbit" fail "HTTP $CODE"
  fi
done

# 4. Notifiarr round-trip
echo "4. Notifiarr API"
NOTIF=$(curl -sS -m 10 -H "X-API-Key: $(secret_read notifiarr.key)" "https://notifiarr.com/api/v1/user/validate" 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("result","?"))' 2>/dev/null)
if [ "$NOTIF" = "success" ]; then
  record "notifiarr-validate" pass
else
  record "notifiarr-validate" fail "got '$NOTIF'"
fi

# 5. Hardlink sanity
echo "5. Hardlinks"
HLINKS=$(sshm "find ~/media/Movies -type f -name '*.mkv' 2>/dev/null | head -5 | xargs -I{} stat -c '%h' {} 2>/dev/null | grep -c '^2'" 2>/dev/null || echo 0)
if [ "${HLINKS:-0}" -ge 4 ]; then
  record "hardlink-sanity" pass "$HLINKS/5 sample files have linkcount >= 2"
elif [ "${HLINKS:-0}" -ge 1 ]; then
  record "hardlink-sanity" pass "$HLINKS/5 (some, not all)"
else
  record "hardlink-sanity" skip "no .mkv samples in ~/media/Movies"
fi

# 6. Disk quota (best-effort — find the largest mount under home)
echo "6. Disk usage"
USAGE=$(sshm "df -h ~ | tail -1 | awk '{print \$3 \" / \" \$2 \" (\" \$5 \" used)\"}'")
record "disk-usage" pass "$USAGE"

# 7. Landing page (skip if no homarr port configured yet — Phase 13)
echo "7. Landing page"
if secret_exists homarr.port; then
  HTPW=$(secret_read htpasswd.password 2>/dev/null || echo "")
  if [ -n "$HTPW" ]; then
    HITS=$(curl -sk -L -m 15 -u "quadstronaut:$HTPW" "https://quadstronaut.seedbox.example.com/" 2>/dev/null | grep -c -i homarr || echo 0)
    if [ "${HITS:-0}" -ge 1 ]; then
      record "landing-page" pass "/ -> Homarr public board ($HITS hits)"
    else
      record "landing-page" fail "no Homarr in response"
    fi
  else
    record "landing-page" skip "no htpasswd.password"
  fi
else
  record "landing-page" skip "Phase 13 Homarr not deployed yet"
fi

# 8. Unpackerr daemon
echo "8. Unpackerr"
UNPACK=$(sshm "ps -ef | grep '/app/unpackerr' | grep -v grep | wc -l")
if [ "${UNPACK:-0}" -ge 1 ]; then
  record "unpackerr-running" pass
else
  record "unpackerr-running" fail "no daemon process"
fi

# 9. Prune cron dry-run
echo "9. Prune cron"
PRUNE=$(sshm "~/scripts/post-import/prune-text-libraries.sh --dry-run 2>&1 | tail -1")
if printf '%s' "$PRUNE" | grep -q "deletions="; then
  record "prune-cron-dry-run" pass "$PRUNE"
else
  record "prune-cron-dry-run" fail "$PRUNE"
fi

# 10. Library rescan helper smoke
echo "10. Library rescan helper"
for tgt in komga kavita audiobookshelf; do
  OUT=$(sshm "~/scripts/post-import/library-rescan-${tgt}.sh 2>&1" || echo "fail")
  if printf '%s' "$OUT" | grep -qE "(triggered|HTTP 2)"; then
    record "rescan-$tgt" pass
  else
    record "rescan-$tgt" fail "$(printf '%s' "$OUT" | head -c 80)"
  fi
done

# 11. Maintainerr API alive
echo "11. Maintainerr"
MT_KEY=$(secret_read maintainerr.key 2>/dev/null || echo "")
HTPW=$(secret_read htpasswd.password 2>/dev/null || echo "")
if [ -n "$MT_KEY" ] && [ -n "$HTPW" ]; then
  CODE=$(curl -sk -m 10 -u "quadstronaut:$HTPW" -H "X-Api-Key: $MT_KEY" -o /dev/null -w "%{http_code}" "https://maintainerr-quadstronaut.seedbox.example.com/api/settings" 2>/dev/null)
  if [ "$CODE" = "200" ]; then
    SC=$(curl -sk -m 10 -u "quadstronaut:$HTPW" -H "X-Api-Key: $MT_KEY" "https://maintainerr-quadstronaut.seedbox.example.com/api/settings/sonarr" 2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))' 2>/dev/null)
    record "maintainerr-api" pass "$SC sonarr instances configured"
  else
    record "maintainerr-api" fail "HTTP $CODE"
  fi
else
  record "maintainerr-api" skip "no key/htpasswd"
fi

# 12. qBittorrent health + seeding count
echo "12. qBittorrent"
QC=$(sshm "C=\$(mktemp); curl -sS -c \$C --data-urlencode 'username=$(secret_read qbittorrent.user)' --data-urlencode 'password=$(secret_read qbittorrent.password)' http://127.0.0.1:17041/api/v2/auth/login >/dev/null; curl -sS -b \$C 'http://127.0.0.1:17041/api/v2/sync/maindata' | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d.get(\"torrents\",{})))'" 2>/dev/null)
if [ "${QC:-0}" -ge 1 ]; then
  record "qbit-torrents" pass "$QC torrents tracked"
else
  record "qbit-torrents" fail
fi

# 13b. Conjurr (internal-only, no nginx — admin via SSH tunnel)
echo "13b. Conjurr"
CJ_PORT=$(sshm 'grep -oP "^PORT=\K[0-9]+" ~/.apps/conjurr/repo/env/.env 2>/dev/null' 2>/dev/null)
if [ -n "$CJ_PORT" ]; then
  CJ_HTTP=$(sshm "curl -sk -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:${CJ_PORT}/" 2>/dev/null)
  case "$CJ_HTTP" in
    200|302) record "conjurr-up" pass "HTTP $CJ_HTTP on 127.0.0.1:$CJ_PORT" ;;
    *)       record "conjurr-up" fail "HTTP $CJ_HTTP" ;;
  esac
else
  record "conjurr-up" skip "no env/.env"
fi

# 13c. Newsletterr (internal-only)
echo "13c. Newsletterr"
NL_PORT=$(sshm 'grep -oP "^PORT=\K[0-9]+" ~/.apps/newsletterr/repo/env/.env 2>/dev/null' 2>/dev/null)
if [ -n "$NL_PORT" ]; then
  NL_HTTP=$(sshm "curl -sk -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:${NL_PORT}/" 2>/dev/null)
  case "$NL_HTTP" in
    200|302) record "newsletterr-up" pass "HTTP $NL_HTTP on 127.0.0.1:$NL_PORT" ;;
    *)       record "newsletterr-up" fail "HTTP $NL_HTTP" ;;
  esac
else
  record "newsletterr-up" skip "no .env"
fi

# 13g. python-plexapi venv healthy
echo "13g. python-plexapi venv"
PV=$(sshm "~/.apps/python-plexapi/venv/bin/python -c 'import plexapi; print(plexapi.VERSION)' 2>/dev/null" 2>/dev/null)
if [ -n "$PV" ]; then
  record "plexapi-venv" pass "$PV"
else
  record "plexapi-venv" fail "missing or broken"
fi

# 13h. stream-stats JSON is current and valid
echo "13h. stream-stats JSON"
SS_AGE=$(sshm "stat -c %Y ~/.apps/stream-stats/state.json 2>/dev/null || echo 0" 2>/dev/null)
NOW=$(date +%s)
if [ -n "$SS_AGE" ] && [ "$SS_AGE" -gt 0 ] && [ $((NOW - SS_AGE)) -lt 180 ]; then
  record "stream-stats-fresh" pass "$((NOW - SS_AGE))s ago"
else
  record "stream-stats-fresh" fail "stale or missing (${SS_AGE:-?})"
fi

# 13i. Upgradinatorr timer scheduled
echo "13i. Upgradinatorr timer"
UT=$(sshm "systemctl --user list-timers upgradinatorr.timer --no-pager 2>/dev/null | grep -c upgradinatorr.timer" 2>/dev/null)
if [ "${UT:-0}" -ge 1 ]; then
  record "upgradinatorr-timer" pass "scheduled"
else
  record "upgradinatorr-timer" fail "timer not scheduled"
fi

# 13l. Tdarr server reachable on loopback + node registered
# Tdarr is loopback-only by design (see scripts/configure/50-tdarr-install.sh);
# public nginx proxy is intentionally disabled, so probe 127.0.0.1 directly.
echo "13l. Tdarr"
TD_PORT=$(secret_read tdarr.server_port 2>/dev/null || echo "")
if [ -n "$TD_PORT" ]; then
  TD_HTTP=$(sshm "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$TD_PORT/api/v2/status" 2>/dev/null)
  case "$TD_HTTP" in
    200) record "tdarr-up" pass "HTTP $TD_HTTP" ;;
    *)   record "tdarr-up" fail "HTTP $TD_HTTP on 127.0.0.1:$TD_PORT/api/v2/status" ;;
  esac
  TD_NODES=$(sshm "curl -sf -m 5 http://127.0.0.1:$TD_PORT/api/v2/get-nodes 2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))'" 2>/dev/null)
  if [ "${TD_NODES:-0}" -ge 1 ]; then
    record "tdarr-node-registered" pass "$TD_NODES node(s)"
  else
    record "tdarr-node-registered" fail "no nodes registered"
  fi
else
  record "tdarr-up" skip "no server_port"
  record "tdarr-node-registered" skip "no server_port"
fi

# 13j. Kometa timer scheduled
echo "13j. Kometa timer"
KT=$(sshm "systemctl --user list-timers kometa.timer --no-pager 2>/dev/null | grep -c kometa.timer" 2>/dev/null)
if [ "${KT:-0}" -ge 1 ]; then
  record "kometa-timer" pass "scheduled"
else
  record "kometa-timer" fail "timer not scheduled"
fi

# 13k. Kometa last run result
echo "13k. Kometa last run"
KR=$(sshm "systemctl --user show kometa.service -p Result --value 2>/dev/null" 2>/dev/null)
case "$KR" in
  success|"") record "kometa-last-run" pass "result=${KR:-pending}" ;;
  *) record "kometa-last-run" fail "result=$KR" ;;
esac

# 13e. Recyclarr timer scheduled
echo "13e. Recyclarr timer"
RT=$(sshm "systemctl --user list-timers recyclarr.timer --no-pager 2>/dev/null | grep -c recyclarr.timer" 2>/dev/null)
if [ "${RT:-0}" -ge 1 ]; then
  record "recyclarr-timer" pass "scheduled"
else
  record "recyclarr-timer" fail "timer not scheduled"
fi

# 13f. No 4K policy — count any 2160p quality entries marked allowed across *arr instances
echo "13f. Recyclarr no-4k policy"
UHD_COUNT=0
for app in sonarr sonarr2 radarr radarr2; do
  KEY=$(secret_read $app.key 2>/dev/null || echo "")
  PORT=$(secret_read $app.port 2>/dev/null || echo "")
  BASE=$(secret_read $app.urlbase 2>/dev/null || echo $app)
  [ -z "$KEY" ] && continue
  V=v3
  N=$(sshm "curl -sf -m 10 -H 'X-Api-Key: $KEY' http://127.0.0.1:$PORT/$BASE/api/$V/qualityprofile 2>/dev/null | python3 -c 'import sys,json; q=json.load(sys.stdin); print(sum(1 for p in q for i in p.get(\"items\",[]) if i.get(\"allowed\") and \"2160\" in (i.get(\"quality\",{}).get(\"name\",\"\") if isinstance(i.get(\"quality\"),dict) else \"\")))'" 2>/dev/null)
  UHD_COUNT=$((UHD_COUNT + ${N:-0}))
done
if [ "$UHD_COUNT" = 0 ]; then
  record "recyclarr-no-4k" pass "no UHD profiles enabled (per policy)"
else
  record "recyclarr-no-4k" fail "$UHD_COUNT UHD entries found — policy violation"
fi

# 13d. Bridge sync count: Newsletterr's auto-list >= Listmonk subscribers
echo "13d. Newsletterr <- Listmonk bridge"
NL_RECIP=$(sshm 'sqlite3 ~/.apps/newsletterr/repo/database/data.db "SELECT (length(emails) - length(replace(emails, '"'"'@'"'"', '"'"''"'"'))) FROM email_lists WHERE name = '"'"'Manitoba (auto)'"'"'"' 2>/dev/null)
if [ -n "${NL_RECIP:-}" ] && [ "${NL_RECIP:-0}" -ge 13 ]; then
  record "newsletterr-recipients" pass "$NL_RECIP recipients in Manitoba (auto)"
else
  record "newsletterr-recipients" fail "expected >=13, got '${NL_RECIP:-}' — bridge sync may not have run"
fi

# 13. Listmonk health + subscribers (mass-comms Phase 19+20)
echo "13. Listmonk"
LM_API_USER=$(secret_read listmonk.api_user 2>/dev/null || echo "")
LM_API_TOKEN=$(secret_read listmonk.api_token 2>/dev/null || echo "")
if [ -n "$LM_API_USER" ] && [ -n "$LM_API_TOKEN" ]; then
  LM_HEALTH=$(curl -sfk -m 5 -u "$LM_API_USER:$LM_API_TOKEN" "https://quadstronaut.seedbox.example.com/listmonk/api/health" 2>/dev/null)
  if echo "$LM_HEALTH" | grep -q '"data":true'; then
    record "listmonk-health" pass
  else
    record "listmonk-health" fail "$LM_HEALTH"
  fi
  LM_SUB=$(curl -sfk -m 10 -u "$LM_API_USER:$LM_API_TOKEN" "https://quadstronaut.seedbox.example.com/listmonk/api/dashboard/counts" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["subscribers"]["total"])' 2>/dev/null)
  if [ "${LM_SUB:-0}" -ge 13 ]; then
    record "listmonk-subscribers" pass "$LM_SUB subscribers"
  else
    record "listmonk-subscribers" fail "expected >=13, got $LM_SUB"
  fi
else
  record "listmonk-health" skip "no api_user/api_token"
  record "listmonk-subscribers" skip "no api_user/api_token"
fi

# 14. Maintenance system (Phase 240)
echo "14. Maintenance system"
M_PORT=$(sshm "cat ~/.opt/maint/maintenance.port 2>/dev/null" </dev/null 2>/dev/null)
if [ -n "$M_PORT" ]; then
  M_BODY=$(sshm "curl -sf -m 5 http://127.0.0.1:${M_PORT}/health 2>/dev/null" </dev/null 2>/dev/null || echo "")
  if [ "$M_BODY" = "ok" ]; then
    record "maint-webhook-up" pass "127.0.0.1:$M_PORT body=ok"
  else
    record "maint-webhook-up" fail "body='$M_BODY'"
  fi
  M_TIMER=$(sshm "systemctl --user list-timers manitoba-maint-window.timer --no-pager 2>/dev/null | grep -c manitoba-maint-window.timer" </dev/null 2>/dev/null)
  if [ "${M_TIMER:-0}" -ge 1 ]; then
    record "maint-window-timer" pass "scheduled"
  else
    record "maint-window-timer" fail "timer not scheduled"
  fi
  M_VAL=$(sshm "MANITOBA_MANIFEST=~/.opt/maint/apps.yaml ~/bin/manitoba-maint manifest validate 2>&1; echo exit=\$?" </dev/null 2>/dev/null)
  if echo "$M_VAL" | grep -q "exit=0"; then
    record "maint-manifest-valid" pass
  else
    record "maint-manifest-valid" fail "$(echo "$M_VAL" | tail -3 | head -1)"
  fi
  # Pusher service alive
  M_PUSHER=$(sshm "systemctl --user is-active manitoba-maint-pusher.service 2>/dev/null" </dev/null 2>/dev/null || echo "unknown")
  if [ "$M_PUSHER" = "active" ]; then
    record "maint-pusher-up" pass
  else
    record "maint-pusher-up" fail "state=$M_PUSHER"
  fi
  # Kuma drift — manifest's kuma_monitor names should all match live monitors.
  M_AUDIT=$(sshm "MANITOBA_MANIFEST=~/.opt/maint/apps.yaml ~/bin/manitoba-maint kuma audit 2>&1; echo exit=\$?" </dev/null 2>/dev/null)
  if echo "$M_AUDIT" | grep -q "exit=0"; then
    M_MATCH=$(echo "$M_AUDIT" | grep -oP 'matched: \K[0-9]+')
    record "maint-kuma-no-drift" pass "matched=${M_MATCH:-?}"
  else
    record "maint-kuma-no-drift" fail "$(echo "$M_AUDIT" | tail -3 | tr '\n' ' ' | head -c 100)"
  fi
  # All Kuma monitors UP — counts monitor_status{...} lines with value 1.
  K_PORT=$(sshm "cat ~/secrets/uptimekuma.port 2>/dev/null" </dev/null 2>/dev/null)
  K_KEY=$(sshm "cat ~/secrets/uptimekuma.key 2>/dev/null" </dev/null 2>/dev/null)
  if [ -n "$K_PORT" ] && [ -n "$K_KEY" ]; then
    K_TOTAL=$(sshm "curl -s -m 5 -u ':$K_KEY' http://127.0.0.1:$K_PORT/metrics 2>/dev/null | grep -c '^monitor_status'" </dev/null 2>/dev/null)
    K_UP=$(sshm "curl -s -m 5 -u ':$K_KEY' http://127.0.0.1:$K_PORT/metrics 2>/dev/null | grep -E '^monitor_status.* 1$' | wc -l" </dev/null 2>/dev/null)
    if [ "${K_TOTAL:-0}" -ge 1 ] && [ "$K_UP" = "$K_TOTAL" ]; then
      record "maint-kuma-all-up" pass "$K_UP/$K_TOTAL monitors UP"
    else
      record "maint-kuma-all-up" fail "$K_UP/$K_TOTAL UP — see Kuma UI for which are down"
    fi
  else
    record "maint-kuma-all-up" skip "no uptimekuma.{port,key}"
  fi
else
  record "maint-webhook-up" skip "no maintenance.port"
  record "maint-window-timer" skip "no maintenance.port"
  record "maint-manifest-valid" skip "no maintenance.port"
  record "maint-pusher-up" skip "no maintenance.port"
  record "maint-kuma-no-drift" skip "no maintenance.port"
  record "maint-kuma-all-up" skip "no maintenance.port"
fi

# 15. Phase-15 canaries (movie / anime / deletion / mobile-ux)
echo "15. Phase-15 canaries"
HERE="$(cd "$(dirname "$0")" && pwd)"
for canary in movie anime deletion mobile-ux; do
  if bash "$HERE/canaries/$canary.sh" >/dev/null 2>&1; then
    record "canary-$canary" pass
  else
    OUT=$(bash "$HERE/canaries/$canary.sh" 2>&1 | tail -3 | tr '\n' '|')
    record "canary-$canary" fail "$OUT"
  fi
done

# 15b. Canary Kuma push-monitor verification
# For each canary, verify its Kuma Push monitor exists in /metrics with status=1 (up).
# A missing or down monitor means the cron-driven canary path is broken.
echo "15b. Canary Kuma push monitors"
K_PORT=$(sshm "cat ~/secrets/uptimekuma.port 2>/dev/null" </dev/null 2>/dev/null)
K_KEY=$(sshm "cat ~/secrets/uptimekuma.key 2>/dev/null" </dev/null 2>/dev/null)
if [ -n "$K_PORT" ] && [ -n "$K_KEY" ]; then
  K_METRICS=$(sshm "curl -s -m 5 -u ':$K_KEY' http://127.0.0.1:$K_PORT/metrics 2>/dev/null" </dev/null 2>/dev/null)
  for canary_name in "Canary Movie" "Canary Anime" "Canary Deletion" "Canary Mobile-UX"; do
    slug=$(echo "$canary_name" | tr ' ' '-' | tr '[:upper:]' '[:lower:]')
    STATUS=$(echo "$K_METRICS" | grep "monitor_name=\"${canary_name}\"" | grep -oE '[0-9]+$' | head -1)
    if [ "${STATUS:-}" = "1" ]; then
      record "canary-kuma-${slug}" pass "monitor_status=1 (up)"
    elif [ -z "${STATUS:-}" ]; then
      record "canary-kuma-${slug}" fail "monitor '${canary_name}' not found in Kuma /metrics — needs Kuma bootstrap"
    else
      record "canary-kuma-${slug}" fail "monitor_status=${STATUS} (expected 1=up)"
    fi
  done
else
  for canary_name in movie anime deletion mobile-ux; do
    record "canary-kuma-canary-${canary_name}" skip "no uptimekuma.{port,key}"
  done
fi

# Summary
echo
printf "Total: %d   Pass: %d   Fail: %d   Skip: %d\n" $((PASS+FAIL+SKIP)) "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" = 0 ]
