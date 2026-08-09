#!/usr/bin/env bash
# 35-sync-appdata.sh -- per-app IDENTITY data: blue -> green, then re-point.
#
# Spec: docs/superpowers/specs/2026-08-08-qflix-migration-blue-green-design.md (S2.3, S3)
# Plan: docs/superpowers/plans/2026-08-08-qflix-migration-plan.md (step 6)
#
# WHAT: Seerr db+settings, Tautulli db+config, Listmonk pg_dump|pg_restore,
# five *arr native backup zips (sonarr/sonarr2/radarr/radarr2/bazarr),
# roster/gate state. Then RE-POINT: Seerr's sonarr/radarr entries, Prowlarr's
# Applications sync targets, each *arr's download-client entries get PUT
# with green's own ports/keys -- none of that survives a blind file copy.
#
# bazarr2 is excluded from "the five": it's an hourly DERIVED copy of bazarr
# (bazarr2-sync.timer) with no independent identity to restore. Prowlarr
# isn't restored (its indexers come from 20-install-stack.sh's config-as-
# code) but IS re-pointed: its Applications entries carry blue's baked-in
# ports until we fix them.
#
# Seerr/Tautulli use `sqlite3 ... VACUUM INTO` rather than scp of the live
# file: both DBs are open and being written on blue the whole time (blue
# never stops -- I-2), and VACUUM INTO snapshots consistently with no lock
# the app would notice.
#
# Every *arr restore re-patches config.xml's <Port>/<UrlBase>/<ApiKey> back
# to green's own right after unzip: the backup zip carries BLUE's values,
# and restoring those verbatim would make green's *arr listen on blue's
# port internally and break its own nginx fragment. Bazarr's config.yaml is
# NOT patched (different schema) -- flagged loudly below; verify at
# 40-validate-green.sh. Seerr/Prowlarr re-point entries are matched to an
# *arr instance by `name`, reusing apps.yaml's existing Kuma display names
# ("Sonarr", "Sonarr Anime", "Radarr", "Radarr 2").
#
# green_secret() is a two-tier lookup: local secrets/green/<name> first (so
# --dry-run needs no live green), else green's own live ~/secrets/<name>
# over SSH.
#
# INVARIANTS: I-2 every blue touch is a read, except the one exception this
# task explicitly authorizes -- triggering a *arr backup via API, which only
# creates a new zip under blue's own Backups folder. I-3 inert by default:
# no --execute prints the plan only. I-4 idempotent: snapshot, pg_restore
# --clean, unzip-over and every re-point PUT are all safe to re-run.
#
# USAGE: 35-sync-appdata.sh NEW_HOST [--execute]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/log.sh"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/secrets.sh"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"   # gives sshm/scpm_from for BLUE (read-only)

usage() { echo "Usage: $0 NEW_HOST [--execute]" >&2; }

NEW_HOST="${1:-}"
if [ -z "$NEW_HOST" ] || [ "${NEW_HOST#--}" != "$NEW_HOST" ]; then
  usage; printf 'STAGE=usage msg=missing-or-flag-where-NEW_HOST-belongs\n' >&2; exit 2
fi
shift
EXECUTE=0
for arg in "$@"; do
  case "$arg" in
    --execute) EXECUTE=1 ;;
    *) usage; printf 'STAGE=usage msg=unknown-argument:%s\n' "$arg" >&2; exit 2 ;;
  esac
done

GSSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30)
sshg()    { ssh "${GSSH_OPTS[@]}" "$NEW_HOST" "$@"; }
scpg_to() { scp "${GSSH_OPTS[@]}" "$1" "$NEW_HOST:$2"; }

GREEN_SECRETS_DIR="$ROOT/secrets/green"
green_secret() {  # two-tier lookup, see header
  local f="$GREEN_SECRETS_DIR/$1"
  [ -f "$f" ] && { tr -d '[:space:]' < "$f"; return 0; }
  sshg "tr -d '[:space:]' < ~/secrets/$1 2>/dev/null" 2>/dev/null
}

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
FAILS=0
note_fail() { printf 'STAGE=%s msg=%s\n' "$1" "$2" >&2; FAILS=$((FAILS + 1)); }

MODE="dry-run"; [ "$EXECUTE" -eq 1 ] && MODE="EXECUTE"
log_info "35-sync-appdata $MODE -> $NEW_HOST"

# ---- generic: online-safe sqlite snapshot off blue, staged into TMPDIR ----
fetch_sqlite_snapshot() {  # $1=remote db path on blue  $2=local dest filename
  local remote="$1" dest="$TMPDIR/$2" tmp="/tmp/qflix-migrate-$$-$2"
  sshm "REMOTE=$(printf %q "$remote") TMP=$(printf %q "$tmp") bash -s" <<'EOF'
sqlite3 "$REMOTE" ".timeout 10000" "VACUUM INTO '$TMP'"
EOF
  scpm_from "$tmp" "$dest" && sshm "rm -f $(printf %q "$tmp")"
}

# --- A. Seerr -- sqlite db (VACUUM INTO snapshot) + settings.json ---------
step_seerr() {
  local label="Seerr: db.sqlite3 (snapshot) + settings.json  [blue -> green]"
  if [ "$EXECUTE" -eq 0 ]; then
    printf '  [dry] %s\n    stop green: app-seerr stop\n    start green: app-seerr start\n' "$label"; return
  fi
  log_info "$label"
  fetch_sqlite_snapshot "~/.apps/seerr/db/db.sqlite3" "seerr.sqlite3" \
    && scpm_from "~/.apps/seerr/settings.json" "$TMPDIR/seerr-settings.json" \
    || { note_fail seerr-fetch-failed "could-not-snapshot-blue-seerr"; return; }
  sshg "app-seerr stop" || true
  scpg_to "$TMPDIR/seerr.sqlite3" "~/.apps/seerr/db/db.sqlite3" \
    && scpg_to "$TMPDIR/seerr-settings.json" "~/.apps/seerr/settings.json" \
    || { note_fail seerr-push-failed "could-not-copy-onto-green"; return; }
  sshg "app-seerr start" || note_fail seerr-start-failed "app-seerr-start-nonzero"
}

# --- B. Tautulli -- db (snapshot) + config.ini -----------------------------
step_tautulli() {
  local label="Tautulli: tautulli.db (snapshot) + config.ini  [blue -> green]"
  if [ "$EXECUTE" -eq 0 ]; then
    printf '  [dry] %s\n    stop green: app-tautulli stop\n    start green: app-tautulli start\n' "$label"; return
  fi
  log_info "$label"
  fetch_sqlite_snapshot "~/.apps/tautulli/tautulli.db" "tautulli.db" \
    && scpm_from "~/.apps/tautulli/config.ini" "$TMPDIR/tautulli-config.ini" \
    || { note_fail tautulli-fetch-failed "could-not-snapshot-blue-tautulli"; return; }
  sshg "app-tautulli stop" || true
  scpg_to "$TMPDIR/tautulli.db" "~/.apps/tautulli/tautulli.db" \
    && scpg_to "$TMPDIR/tautulli-config.ini" "~/.apps/tautulli/config.ini" \
    || { note_fail tautulli-push-failed "could-not-copy-onto-green"; return; }
  sshg "app-tautulli start" || note_fail tautulli-start-failed "app-tautulli-start-nonzero"
}

# --- C. Listmonk -- pg_dump (blue) piped into pg_restore (green). Postgres
#     itself is never stopped either side; listmonk.service restarts after
#     so it re-reads (cheap insurance, not strictly required). -------------
step_listmonk() {
  local bport gport pgpass_expr dump restore
  bport="$(secret_read postgres.port 2>/dev/null || echo 42009)"
  gport="$(green_secret postgres.port)"; gport="${gport:-42009}"
  pgpass_expr='PGPASSWORD=$(base64 -d ~/.apps/postgres/.encoded.dat | head -c 24)'
  dump="$pgpass_expr pg_dump -h 127.0.0.1 -p $bport -U quadstronaut -Fc -d listmonk"
  restore="$pgpass_expr pg_restore -h 127.0.0.1 -p $gport -U quadstronaut -d listmonk --clean --if-exists --no-owner"
  if [ "$EXECUTE" -eq 0 ]; then
    printf '  [dry] listmonk: pg_dump (blue) | pg_restore (green)\n           blue:  %s\n           green: %s\n' "$dump" "$restore"
    return
  fi
  log_info "listmonk: pg_dump (blue) | pg_restore (green)"
  sshm "$dump" | sshg "$restore" || note_fail listmonk-restore-failed "pg_dump|pg_restore-nonzero"
  sshg "systemctl --user restart listmonk.service" || true
}

# --- D. Five *arr native backup zips: trigger on blue, fetch, restore on
#     green. columns: slug secret-prefix api-version(v3|plain) display-name -
ARR_TABLE="
sonarr   sonarr   v3    Sonarr
sonarr2  sonarr2  v3    Sonarr Anime
radarr   radarr   v3    Radarr
radarr2  radarr2  v3    Radarr 2
bazarr   bazarr   plain Bazarr
"

step_arr_restore() {  # $1=slug $2=secret-prefix $3=apiver $4=display(unused here)
  local slug="$1" pfx="$2" ver="$3" api key ub label
  api="/api/system/backup"; [ "$ver" = v3 ] && api="/api/v3/system/backup"
  label="$slug: trigger backup (blue) -> fetch -> restore (green) -> re-patch port/urlbase/apikey"
  if [ "$EXECUTE" -eq 0 ]; then
    printf '  [dry] %s\n           POST blue 127.0.0.1:<%s.port>%s  (X-Api-Key: <%s.key>)\n           stop green: app-%s stop ; unzip -o over ~/.apps/%s/\n' \
      "$label" "$pfx" "$api" "$pfx" "$slug" "$slug"
    [ "$ver" = plain ] && printf '           NOTE: bazarr uses config.yaml, not patched here -- verify at 40-validate-green.sh\n'
    return
  fi
  log_info "$label"
  key="$(secret_read "$pfx.key")"; local port="$(secret_read "$pfx.port")"
  ub="$(secret_read "${pfx}.urlbase" 2>/dev/null || echo "")"
  local zippath
  # Snapshot the backup list BEFORE triggering a new one (by its newest
  # .time), check the trigger POST's own curl exit + HTTP code, then poll
  # (~60s, 2s interval) until an entry strictly newer than the snapshot
  # shows up -- guards against silently grabbing a stale pre-existing zip if
  # the new backup takes longer than the old fixed `sleep 3` assumed.
  zippath="$(sshm "KEY=$(printf %q "$key") PORT=$(printf %q "$port") UB=$(printf %q "$ub") API=$(printf %q "$api") bash -s" <<'EOF'
set -uo pipefail
BASE="http://127.0.0.1:$PORT$UB$API"
BEFORE_MAX="$(curl -sf -H "X-Api-Key: $KEY" "$BASE" | jq -r 'if length==0 then "" else (sort_by(.time) | last | .time) end' 2>/dev/null)"
HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "X-Api-Key: $KEY" "$BASE")"
POST_RC=$?
if [ "$POST_RC" -ne 0 ] || { [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "201" ]; }; then
  echo "ERR:backup-trigger-post-failed:rc=$POST_RC:http=$HTTP_CODE"
  exit 1
fi
i=0
while :; do
  NEWEST="$(curl -sf -H "X-Api-Key: $KEY" "$BASE" | jq -c 'sort_by(.time) | last // {}' 2>/dev/null)"
  NT="$(printf '%s' "$NEWEST" | jq -r '.time // ""' 2>/dev/null)"
  NP="$(printf '%s' "$NEWEST" | jq -r '.path // ""' 2>/dev/null)"
  if [ -n "$NP" ] && { [ -z "$BEFORE_MAX" ] || [[ "$NT" > "$BEFORE_MAX" ]]; }; then
    printf '%s\n' "$NP"
    exit 0
  fi
  i=$((i + 1))
  [ "$i" -ge 30 ] && break
  sleep 2
done
echo "ERR:backup-poll-timeout-no-newer-backup-after-60s"
exit 1
EOF
)"
  if [ $? -ne 0 ] || [ -z "$zippath" ] || [ "$zippath" = null ]; then
    note_fail "$slug-backup-trigger-failed" "${zippath:-no-backup-path-returned-by-blue-api}"; return
  fi
  scpm_from "~/.apps/$slug/$zippath" "$TMPDIR/$slug-backup.zip" \
    || { note_fail "$slug-fetch-failed" "could-not-scp-backup-zip-off-blue"; return; }
  sshg "app-$slug stop" || true
  scpg_to "$TMPDIR/$slug-backup.zip" "/tmp/$slug-backup.zip" \
    && sshg "unzip -o /tmp/$slug-backup.zip -d ~/.apps/$slug/ >/dev/null && rm -f /tmp/$slug-backup.zip" \
    || { note_fail "$slug-restore-failed" "unzip-on-green-failed"; sshg "app-$slug start" || true; return; }
  if [ "$ver" = v3 ]; then
    local gp gu gk
    gp="$(green_secret "$pfx.port")"; gu="$(green_secret "${pfx}.urlbase")"; gk="$(green_secret "$pfx.key")"
    if [ -n "$gp" ] && [ -n "$gk" ]; then
      sshg "sed -i -E 's#<Port>[0-9]+</Port>#<Port>$gp</Port>#; s#<UrlBase>[^<]*</UrlBase>#<UrlBase>$gu</UrlBase>#; s#<ApiKey>[^<]*</ApiKey>#<ApiKey>$gk</ApiKey>#' ~/.apps/$slug/config.xml"
    else
      note_fail "$slug-repatch-skipped" "green-port-or-key-secret-unresolved"
    fi
  else
    log_warn "$slug: config.yaml not patched -- confirm port/apikey manually before 40-validate-green.sh"
  fi
  sshg "app-$slug start" || note_fail "$slug-start-failed" "app-$slug-start-nonzero"
}

# --- E. Roster + gate state -> green ~/secrets + ~/.opt/maint/entitlement/.
#     Straight copy; does NOT touch `armed:` in members.yaml -- disarming
#     green is 50-cutover.sh's job (spec S5, invariant I-5), not this file's.
step_roster() {
  local label="roster/gate state: members.yaml, state.json, declared-payers.json -> green"
  if [ "$EXECUTE" -eq 0 ]; then
    printf '  [dry] %s\n           members.yaml       -> ~/secrets/members.yaml\n           state.json         -> ~/.opt/maint/entitlement/state.json\n           declared-payers.json -> ~/.opt/maint/entitlement/declared-payers.json (if present)\n' "$label"
    return
  fi
  log_info "$label"
  scpm_from "~/secrets/members.yaml" "$TMPDIR/members.yaml" \
    && scpg_to "$TMPDIR/members.yaml" "~/secrets/members.yaml" \
    || note_fail roster-members-failed "members.yaml-copy-failed"
  sshg "mkdir -p ~/.opt/maint/entitlement"
  scpm_from "~/.opt/maint/entitlement/state.json" "$TMPDIR/state.json" \
    && scpg_to "$TMPDIR/state.json" "~/.opt/maint/entitlement/state.json" \
    || note_fail roster-state-failed "state.json-copy-failed"
  if sshm "test -f ~/.opt/maint/entitlement/declared-payers.json" 2>/dev/null; then
    scpm_from "~/.opt/maint/entitlement/declared-payers.json" "$TMPDIR/declared-payers.json" \
      && scpg_to "$TMPDIR/declared-payers.json" "~/.opt/maint/entitlement/declared-payers.json"
  else
    log_warn "declared-payers.json absent on blue -- skipping (not fatal, may not be a persisted file yet)"
  fi
}

# --- F. RE-POINT pass: Seerr sonarr/radarr entries, Prowlarr app-sync
#     targets, each *arr's qBittorrent/SABnzbd download-client entries ->
#     green values. Matched by display `name` (see header); runs on green
#     itself against loopback. -----------------------------------------
step_repoint_seerr() {
  local sk sp label="Seerr re-point: sonarr/radarr entries -> green ports+keys+urlbase"
  sk="$(green_secret seerr.key)"; sp="$(green_secret seerr.port)"
  if [ "$EXECUTE" -eq 0 ]; then
    printf '  [dry] %s\n' "$label"
    printf '           GET/PUT http://127.0.0.1:<seerr.port>/api/v1/settings/{sonarr,radarr}/{id}\n'
    printf '           body: port,apiKey,baseUrl <- {sonarr,sonarr2,radarr,radarr2}.{port,key,urlbase} matched by name in %s\n' "Sonarr/Sonarr Anime/Radarr/Radarr 2"
    return
  fi
  log_info "$label"
  [ -n "$sk" ] && [ -n "$sp" ] || { note_fail seerr-repoint-skipped "green-seerr-secrets-unresolved"; return; }
  sshg "SK=$(printf %q "$sk") SP=$(printf %q "$sp") ROOT=$(printf %q "$ROOT") bash -s" <<'EOF' || note_fail seerr-repoint-failed "put-nonzero"
declare -A NAME2PFX=( [Sonarr]=sonarr [Sonarr Anime]=sonarr2 [Radarr]=radarr [Radarr 2]=radarr2 )
for kind in sonarr radarr; do
  entries="$(curl -sf -H "X-Api-Key: $SK" "http://127.0.0.1:$SP/api/v1/settings/$kind")"
  echo "$entries" | jq -c '.[]' | while read -r e; do
    nm="$(echo "$e" | jq -r '.name')"
    pfx="${NAME2PFX[$nm]:-}"; [ -n "$pfx" ] || continue
    p="$(cat ~/secrets/$pfx.port 2>/dev/null | tr -d '[:space:]')"
    k="$(cat ~/secrets/$pfx.key 2>/dev/null | tr -d '[:space:]')"
    ub="$(cat ~/secrets/$pfx.urlbase 2>/dev/null | tr -d '[:space:]')"
    [ -n "$p" ] && [ -n "$k" ] || continue
    id="$(echo "$e" | jq -r '.id')"
    patched="$(echo "$e" | jq --arg p "$p" --arg k "$k" --arg ub "$ub" '.port=($p|tonumber) | .apiKey=$k | .baseUrl=(if $ub=="" then "/" else "/"+$ub end)')"
    curl -sf -X PUT -H "X-Api-Key: $SK" -H 'Content-Type: application/json' \
      -d "$patched" "http://127.0.0.1:$SP/api/v1/settings/$kind/$id" >/dev/null
  done
done
EOF
}

step_repoint_prowlarr() {
  local pk pp label="Prowlarr re-point: Applications sync targets -> green ports+keys"
  pk="$(green_secret prowlarr.key)"; pp="$(green_secret prowlarr.port)"
  if [ "$EXECUTE" -eq 0 ]; then
    printf '  [dry] %s\n           GET/PUT http://127.0.0.1:<prowlarr.port>/api/v1/applications/{id}\n           fields.baseUrl,fields.apiKey <- matched *arr by name\n' "$label"
    return
  fi
  log_info "$label"
  [ -n "$pk" ] && [ -n "$pp" ] || { note_fail prowlarr-repoint-skipped "green-prowlarr-secrets-unresolved"; return; }
  sshg "PK=$(printf %q "$pk") PP=$(printf %q "$pp") bash -s" <<'EOF' || note_fail prowlarr-repoint-failed "put-nonzero"
declare -A NAME2PFX=( [Sonarr]=sonarr [Sonarr Anime]=sonarr2 [Radarr]=radarr [Radarr 2]=radarr2 )
curl -sf -H "X-Api-Key: $PK" "http://127.0.0.1:$PP/api/v1/applications" | jq -c '.[]' | while read -r e; do
  nm="$(echo "$e" | jq -r '.name')"; pfx="${NAME2PFX[$nm]:-}"; [ -n "$pfx" ] || continue
  p="$(cat ~/secrets/$pfx.port 2>/dev/null | tr -d '[:space:]')"
  k="$(cat ~/secrets/$pfx.key 2>/dev/null | tr -d '[:space:]')"
  ub="$(cat ~/secrets/$pfx.urlbase 2>/dev/null | tr -d '[:space:]')"
  [ -n "$p" ] && [ -n "$k" ] || continue
  id="$(echo "$e" | jq -r '.id')"
  patched="$(echo "$e" | jq --arg base "http://172.17.0.1:$p${ub:+/$ub}" --arg k "$k" \
    '.fields = (.fields | map(if .name=="baseUrl" then .value=$base elif .name=="apiKey" then .value=$k else . end))')"
  curl -sf -X PUT -H "X-Api-Key: $PK" -H 'Content-Type: application/json' \
    -d "$patched" "http://127.0.0.1:$PP/api/v1/applications/$id" >/dev/null
done
EOF
}

step_repoint_downloadclients() {
  local label="*arr download-clients: qBittorrent/SABnzbd entries -> green ports+creds"
  if [ "$EXECUTE" -eq 0 ]; then
    printf '  [dry] %s\n           per sonarr/sonarr2/radarr/radarr2: GET/PUT .../api/v3/downloadclient/{id}\n           QBittorrent<-qbittorrent.{port,user,password}; Sabnzbd<-sabnzbd.{port,key}\n' "$label"
    return
  fi
  log_info "$label"
  local qp qu qw sp sk
  qp="$(green_secret qbittorrent.port)"; qu="$(green_secret qbittorrent.user)"; qw="$(green_secret qbittorrent.password)"
  sp="$(green_secret sabnzbd.port)"; sk="$(green_secret sabnzbd.key)"
  for pfx in sonarr sonarr2 radarr radarr2; do
    local ap ak
    ap="$(green_secret "$pfx.port")"; ak="$(green_secret "$pfx.key")"
    [ -n "$ap" ] && [ -n "$ak" ] || { note_fail "$pfx-dlclient-skipped" "green-secret-unresolved"; continue; }
    sshg "AP=$(printf %q "$ap") AK=$(printf %q "$ak") QP=$(printf %q "$qp") QU=$(printf %q "$qu") QW=$(printf %q "$qw") SP=$(printf %q "$sp") SK=$(printf %q "$sk") bash -s" <<'EOF' || note_fail "$pfx-dlclient-failed" "put-nonzero"
curl -sf -H "X-Api-Key: $AK" "http://127.0.0.1:$AP/api/v3/downloadclient" | jq -c '.[]' | while read -r e; do
  impl="$(echo "$e" | jq -r '.implementation')"; id="$(echo "$e" | jq -r '.id')"
  if [ "$impl" = "QBittorrent" ]; then
    p="$(echo "$e" | jq --arg v "$QP" --arg u "$QU" --arg w "$QW" \
      '.fields = (.fields | map(if .name=="port" then .value=($v|tonumber) elif .name=="username" then .value=$u elif .name=="password" then .value=$w else . end))')"
  elif [ "$impl" = "Sabnzbd" ]; then
    p="$(echo "$e" | jq --arg v "$SP" --arg k "$SK" \
      '.fields = (.fields | map(if .name=="port" then .value=($v|tonumber) elif .name=="apiKey" then .value=$k else . end))')"
  else
    continue
  fi
  curl -sf -X PUT -H "X-Api-Key: $AK" -H 'Content-Type: application/json' \
    -d "$p" "http://127.0.0.1:$AP/api/v3/downloadclient/$id" >/dev/null
done
EOF
  done
}

# ---- run it all, in order ----
step_seerr
step_tautulli
step_listmonk
while read -r slug pfx ver disp; do
  [ -z "$slug" ] && continue
  step_arr_restore "$slug" "$pfx" "$ver" "$disp"
done <<<"$ARR_TABLE"
step_roster
step_repoint_seerr
step_repoint_prowlarr
step_repoint_downloadclients

if [ "$FAILS" -gt 0 ]; then
  printf 'FAIL: sync-appdata -- %d step(s) failed (see STAGE= lines above)\n' "$FAILS"
  exit 1
fi
printf 'PASS: sync-appdata %s -> %s -- Seerr, Tautulli, Listmonk, 5x *arr restored + re-pointed, roster copied\n' "$MODE" "$NEW_HOST"
exit 0
