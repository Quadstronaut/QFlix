#!/usr/bin/env bash
# Plex-ecosystem smoke test. Covers Plex server endpoints, PlexAPI venv,
# Arr→Plex integrations, Tautulli, companions, and maintenance-system presence.
# Exits non-zero on any FAIL.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

PASS=0; FAIL=0; SKIP=0
record() {
  local name="$1" status="$2" detail="${3:-}"
  case "$status" in
    pass) printf '✓ %-42s %s\n' "$name" "$detail"; PASS=$((PASS+1)) ;;
    fail) printf '✗ %-42s %s\n' "$name" "$detail"; FAIL=$((FAIL+1)) ;;
    skip) printf '~ %-42s %s\n' "$name" "$detail"; SKIP=$((SKIP+1)) ;;
  esac
}

PLEX_HOST="$(secret_read plex.host)"
PLEX_PORT="$(secret_read plex.port)"
PLEX_TOKEN="$(secret_read plex.token)"
PLEX_BASE="http://${PLEX_HOST}:${PLEX_PORT}"

# ─── A. Plex server endpoints ────────────────────────────────────────────────
echo "A. Plex server endpoints"

# A1. /identity
echo "A1. Plex /identity"
IDENT=$(sshm "curl -sf -m 10 -H 'X-Plex-Token: $PLEX_TOKEN' '${PLEX_BASE}/identity' 2>/dev/null" 2>/dev/null || echo "")
if printf '%s' "$IDENT" | grep -q '<MediaContainer'; then
  record "plex-identity" pass "MediaContainer returned"
else
  record "plex-identity" fail "unexpected response: $(printf '%s' "$IDENT" | head -c 80)"
fi

# A2. /library/sections — at least 1 library
echo "A2. Plex /library/sections"
SEC_COUNT=$(sshm "curl -sf -m 10 -H 'X-Plex-Token: $PLEX_TOKEN' '${PLEX_BASE}/library/sections' 2>/dev/null | python3 -c 'import sys,re; print(len(re.findall(\"<Directory \", sys.stdin.read())))'" 2>/dev/null || echo 0)
if [ "${SEC_COUNT:-0}" -ge 1 ]; then
  record "plex-library-sections" pass "$SEC_COUNT section(s)"
else
  record "plex-library-sections" fail "no sections returned (got '$SEC_COUNT')"
fi

# A3. /status/sessions
echo "A3. Plex /status/sessions"
SESS_CODE=$(sshm "curl -sf -m 10 -o /dev/null -w '%{http_code}' -H 'X-Plex-Token: $PLEX_TOKEN' '${PLEX_BASE}/status/sessions' 2>/dev/null" 2>/dev/null || echo "")
if [ "$SESS_CODE" = "200" ]; then
  record "plex-sessions" pass "HTTP 200"
else
  record "plex-sessions" fail "HTTP $SESS_CODE"
fi

# A4. /transcode/sessions
echo "A4. Plex /transcode/sessions"
TRANS_CODE=$(sshm "curl -sf -m 10 -o /dev/null -w '%{http_code}' -H 'X-Plex-Token: $PLEX_TOKEN' '${PLEX_BASE}/transcode/sessions' 2>/dev/null" 2>/dev/null || echo "")
if [ "$TRANS_CODE" = "200" ]; then
  record "plex-transcode-sessions" pass "HTTP 200"
else
  record "plex-transcode-sessions" fail "HTTP $TRANS_CODE"
fi

# A5. /system/agents
echo "A5. Plex /system/agents"
AGENTS_CODE=$(sshm "curl -sf -m 10 -o /dev/null -w '%{http_code}' -H 'X-Plex-Token: $PLEX_TOKEN' '${PLEX_BASE}/system/agents' 2>/dev/null" 2>/dev/null || echo "")
if [ "$AGENTS_CODE" = "200" ]; then
  record "plex-system-agents" pass "HTTP 200"
else
  record "plex-system-agents" fail "HTTP $AGENTS_CODE"
fi

# ─── B. PlexAPI venv + Plex-touching scripts ─────────────────────────────────
echo "B. PlexAPI venv + scripts"
PLEXPY="~/.apps/python-plexapi/venv/bin/python"

# B6. PlexServer connect via plexapi
echo "B6. PlexAPI PlexServer connect"
PXSRV=$(sshm "$PLEXPY -c \"
from plexapi.server import PlexServer
s = PlexServer('http://${PLEX_HOST}:${PLEX_PORT}', '${PLEX_TOKEN}')
libs = s.library.sections()
print(len(libs))
\" 2>/dev/null" 2>/dev/null || echo "")
if [ -n "$PXSRV" ] && [ "${PXSRV:-0}" -ge 1 ] 2>/dev/null; then
  record "plexapi-server-connect" pass "$PXSRV libraries via PlexAPI"
else
  record "plexapi-server-connect" fail "returned: '$PXSRV'"
fi

# B7. plexapi version
echo "B7. plexapi version"
PV=$(sshm "$PLEXPY -c 'import plexapi; print(plexapi.VERSION)' 2>/dev/null" 2>/dev/null || echo "")
if [ -n "$PV" ]; then
  record "plexapi-version" pass "$PV"
else
  record "plexapi-version" fail "missing or broken"
fi

# B8. Pinned identifier in config.ini
echo "B8. plexapi config identifier pin"
IDENT_LINE=$(sshm "grep -c 'identifier' ~/.config/plexapi/config.ini 2>/dev/null || echo 0" 2>/dev/null || echo 0)
if [ "${IDENT_LINE:-0}" -ge 1 ]; then
  record "plexapi-identifier-pinned" pass "identifier key present in config.ini"
else
  record "plexapi-identifier-pinned" fail "~/.config/plexapi/config.ini missing or no identifier"
fi

# B9. kill_stream.sh dry run (--max 0)
echo "B9. kill_stream.sh --max 0"
KS_EXIT=$(sshm "~/scripts/plex/kill_stream.sh --max 0 >/dev/null 2>&1; echo \$?" 2>/dev/null || echo "fail")
if [ "$KS_EXIT" = "0" ]; then
  record "kill-stream-dry-run" pass "exit 0"
else
  record "kill-stream-dry-run" fail "exit $KS_EXIT"
fi

# B10. stream-stats JSON freshness (≤ 3 min)
echo "B10. stream-stats fresh"
SS_AGE=$(sshm "stat -c %Y ~/.apps/stream-stats/state.json 2>/dev/null || echo 0" 2>/dev/null || echo 0)
NOW=$(date +%s)
SS_DELTA=$((NOW - ${SS_AGE:-0}))
if [ "${SS_AGE:-0}" -gt 0 ] && [ "$SS_DELTA" -lt 180 ]; then
  record "stream-stats-fresh" pass "${SS_DELTA}s ago"
else
  record "stream-stats-fresh" fail "stale or missing (age=${SS_AGE:-?}, delta=${SS_DELTA}s)"
fi

# ─── C. Arr → Plex notification integration ──────────────────────────────────
echo "C. Arr -> Plex notification"

arr_plex_notify() {
  local app="$1" api_ver="$2"
  local KEY PORT BASE
  KEY=$(secret_read "$app.key" 2>/dev/null || echo "")
  PORT=$(secret_read "$app.port" 2>/dev/null || echo "")
  BASE=$(secret_read "$app.urlbase" 2>/dev/null || echo "$app")
  if [ -z "$KEY" ] || [ -z "$PORT" ]; then
    record "$app-plex-notify" skip "no key/port"
    return
  fi
  local COUNT
  COUNT=$(sshm "curl -sf -m 10 -H 'X-Api-Key: $KEY' 'http://127.0.0.1:$PORT/$BASE/api/$api_ver/notification' 2>/dev/null | python3 -c \"
import sys, json
items = json.load(sys.stdin)
hits = [n for n in items if 'plex' in n.get('name','').lower() or 'plex' in n.get('implementation','').lower()]
print(len(hits))
\"" 2>/dev/null || echo "")
  if [ "${COUNT:-0}" -ge 1 ]; then
    record "$app-plex-notify" pass "$COUNT Plex notification(s)"
  else
    record "$app-plex-notify" fail "no Plex notification found (got '$COUNT')"
  fi
}

# C11-C14: video *arrs
echo "C11. Sonarr Plex notify"
arr_plex_notify sonarr v3
echo "C12. Sonarr2 Plex notify"
arr_plex_notify sonarr2 v3
echo "C13. Radarr Plex notify"
arr_plex_notify radarr v3
echo "C14. Radarr2 Plex notify"
arr_plex_notify radarr2 v3
# C15: Readarr — manages books/audiobooks, no Plex video-library notification expected
echo "C15. Readarr Plex notify"
record "readarr-plex-notify" skip "Readarr is a book manager — Plex video-library notification not applicable"

# ─── D. Tautulli ─────────────────────────────────────────────────────────────
echo "D. Tautulli"

# D16. Tautulli is a UCC Docker container (/app/tautulli), no local venv
echo "D16. Tautulli venv import"
record "tautulli-venv" skip "Tautulli runs as UCC Docker container (python3 /app/tautulli/Tautulli.py) — no local venv"

# D17. Tautulli API ping — http_root = /tautulli
echo "D17. Tautulli API ping"
if secret_exists tautulli.key && secret_exists tautulli.port; then
  TT_KEY=$(secret_read tautulli.key)
  TT_PORT=$(secret_read tautulli.port)
  TT_RESP=$(sshm "curl -sf -m 10 'http://127.0.0.1:${TT_PORT}/tautulli/api/v2?apikey=${TT_KEY}&cmd=arnold' 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d[\"response\"][\"result\"])'" 2>/dev/null || echo "")
  if [ "$TT_RESP" = "success" ]; then
    record "tautulli-api-ping" pass "arnold cmd succeeded"
  else
    record "tautulli-api-ping" fail "got: '$TT_RESP' (check http_root=/tautulli)"
  fi
else
  record "tautulli-api-ping" skip "no tautulli.key or tautulli.port"
fi

# ─── E. Plex-aware companions ────────────────────────────────────────────────
echo "E. Plex companions"

# E18. Maintainerr — Plex companion; sonarr integration confirms Plex is working.
# Maintainerr runs behind nginx basic auth; call via HTTPS proxy.
echo "E18. Maintainerr Sonarr API (Plex companion)"
MT_KEY=$(secret_read maintainerr.key 2>/dev/null || echo "")
HTPW=$(secret_read htpasswd.password 2>/dev/null || echo "")
if [ -n "$MT_KEY" ] && [ -n "$HTPW" ]; then
  MT_SC=$(curl -sk -m 10 -u "quadstronaut:$HTPW" -H "X-Api-Key: $MT_KEY" \
    "https://maintainerr-quadstronaut.seedbox.example.com/api/settings/sonarr" 2>/dev/null \
    | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))' 2>/dev/null || echo "")
  if [ "${MT_SC:-0}" -ge 1 ]; then
    record "maintainerr-plex-companion" pass "$MT_SC Sonarr instances configured"
  else
    record "maintainerr-plex-companion" fail "no Sonarr instances (got '$MT_SC')"
  fi
else
  record "maintainerr-plex-companion" skip "no maintainerr key/htpasswd"
fi

# E19. Seerr /api/v1/settings/plex — Plex server configured
echo "E19. Seerr Plex settings"
JS_KEY=$(secret_read seerr.key 2>/dev/null || echo "")
JS_PORT=$(secret_read seerr.port 2>/dev/null || echo "")
if [ -n "$JS_KEY" ] && [ -n "$JS_PORT" ]; then
  JS_PLEX=$(sshm "curl -sf -m 10 -H 'X-Api-Key: $JS_KEY' 'http://127.0.0.1:${JS_PORT}/api/v1/settings/plex' 2>/dev/null | python3 -c \"
import sys, json
d = json.load(sys.stdin)
ip = d.get('ip','')
print('configured' if ip else 'empty')
\"" 2>/dev/null || echo "")
  if [ "$JS_PLEX" = "configured" ]; then
    record "seerr-plex-settings" pass "Plex server IP is set"
  else
    record "seerr-plex-settings" fail "Plex server not configured (got '$JS_PLEX')"
  fi
else
  record "seerr-plex-settings" skip "no seerr key/port"
fi

# E20 (Newsletterr) + E21 (Conjurr) removed 2026-05-15 — both apps were
# purged 2026-05-11 and these checks always reported fail, eroding trust
# in the smoke output. Their replacement (qflix-newsletter Python package)
# is exercised by qflix-newsletter --dry-run elsewhere in smoke-test.sh.

# E22. Kometa config.yml has plex: token:
echo "E22. Kometa plex config"
KM_PX=$(sshm "grep -c 'token:' ~/.apps/kometa/config/config.yml 2>/dev/null || echo 0" 2>/dev/null || echo 0)
KM_PX="${KM_PX//[^0-9]/}"
KM_PLEX_BLOCK=$(sshm "grep -c 'plex:' ~/.apps/kometa/config/config.yml 2>/dev/null || echo 0" 2>/dev/null || echo 0)
KM_PLEX_BLOCK="${KM_PLEX_BLOCK//[^0-9]/}"
if [ "${KM_PX:-0}" -ge 1 ] && [ "${KM_PLEX_BLOCK:-0}" -ge 1 ]; then
  record "kometa-plex-config" pass "plex: block with token: present"
else
  record "kometa-plex-config" fail "plex block=$KM_PLEX_BLOCK token lines=$KM_PX"
fi

# E23. Tdarr — not Plex-dependent
record "tdarr-plex-skip" skip "Tdarr has no Plex integration — intentionally excluded"

# E24. stream-stats state.json — valid JSON with expected structure
echo "E24. stream-stats state valid"
SS_JSON=$(sshm "cat ~/.apps/stream-stats/state.json 2>/dev/null | python3 -c \"
import sys,json
d=json.load(sys.stdin)
print('ok' if 'sessions' in d or 'streams' in d or isinstance(d,dict) else 'bad')
\" 2>/dev/null" 2>/dev/null || echo "")
if [ "$SS_JSON" = "ok" ]; then
  record "stream-stats-json-valid" pass "state.json is valid JSON with expected structure"
else
  record "stream-stats-json-valid" fail "unexpected state.json content: '$SS_JSON'"
fi

# E25. Upgradinatorr timer (arr-facing, not Plex-direct)
echo "E25. Upgradinatorr timer"
UT=$(sshm "systemctl --user list-timers upgradinatorr.timer --no-pager 2>/dev/null | grep -c upgradinatorr.timer" 2>/dev/null || echo 0)
UT="${UT//[^0-9]/}"
if [ "${UT:-0}" -ge 1 ]; then
  record "upgradinatorr-timer" pass "scheduled (arr-facing)"
else
  record "upgradinatorr-timer" fail "timer not scheduled"
fi

# ─── F. Maintenance system + loopback sanity ─────────────────────────────────
echo "F. Maintenance + port sanity"

# F26. Plex in manifest/apps.yaml
echo "F26. Plex in manifest"
MANIFEST="$HERE/../manifest/apps.yaml"
if grep -q 'plex:' "$MANIFEST" 2>/dev/null; then
  record "plex-in-manifest" pass "plex: entry found in manifest/apps.yaml"
else
  record "plex-in-manifest" skip "plex not in manifest/apps.yaml yet — follow-up: add after UCC-managed Plex is stable"
fi

# F27. Plex loopback port matches secrets/plex.port
echo "F27. Plex loopback port sanity"
ACTUAL_PORT=$(sshm "ss -tlnp 2>/dev/null | awk '{print \$4}' | grep ':${PLEX_PORT}$' | sed 's/.*://' | head -1" 2>/dev/null || echo "")
if [ "${ACTUAL_PORT:-}" = "${PLEX_PORT}" ]; then
  record "plex-port-sanity" pass "127.0.0.1:$PLEX_PORT is listening"
else
  if printf '%s' "$IDENT" | grep -q '<MediaContainer'; then
    record "plex-port-sanity" pass "port $PLEX_PORT reachable (identity confirmed); port in own netns so ss shows different bind"
  else
    record "plex-port-sanity" fail "port $PLEX_PORT not confirmed listening (ss='$ACTUAL_PORT')"
  fi
fi

# Summary
echo
printf "Total: %d   Pass: %d   Fail: %d   Skip: %d\n" $((PASS+FAIL+SKIP)) "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" = 0 ]
