#!/usr/bin/env bash
# Phase 80 — VictoriaLogs install on seedbox. Idempotent.
#
# Architecture: VictoriaLogs runs as a systemd-user service on the seedbox
# (same netns as the apps it indexes), with a sibling timer-driven ingest
# job that imports scripts/mcp/logs.py and POSTs JSON-line batches to the
# local server every 5 min. The workstation MCP queries via SSH-exec'd curl.
#
# Why on seedbox not workstation: the autonomy mandate wants log ingest
# to be 24/7, independent of the operator's PC being on. Earlier sprint
# put this on the workstation — wrong call, migrating here.
#
# Pre-flight:
#   - secrets/seedbox.ssh-host present
#   - VictoriaLogs binary fetchable (will download v1.50.0 by default)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"
source "$HERE/../lib/secrets.sh"

VLOGS_VERSION="${VLOGS_VERSION:-v1.50.0}"

log_info "Phase 80: VictoriaLogs install on seedbox"

# ── Step 1: claim a loopback port ───────────────────────────────────────────
if ! secret_exists vlogs.port; then
  USED_LOCAL=$(cat "$REPO_ROOT"/secrets/*.port 2>/dev/null | sort -u | paste -sd, -)
  USED_BOUND=$(sshm "ss -tln 2>/dev/null | grep -oE '127\\.0\\.0\\.1:[0-9]+' | cut -d: -f2 | sort -u" | paste -sd, -)
  PORT=$(sshm "app-ports free 2>/dev/null | grep -E '^[0-9]+\$'" | while read p; do
    case ",$USED_LOCAL,$USED_BOUND," in
      *",$p,"*) ;;
      *) echo "$p"; break ;;
    esac
  done)
  [ -n "$PORT" ] || die "no truly-free port from app-ports for vlogs"
  secret_write vlogs.port "$PORT"
  log_info "claimed vlogs port $PORT"
fi
VLOGS_PORT=$(secret_read vlogs.port)
log_info "vlogs port = $VLOGS_PORT (loopback only)"

# ── Step 2: install binary on seedbox ───────────────────────────────────────
# VictoriaLogs Linux release: a single tarball with one static binary.
log_info "ensuring vlogs binary at ~/.apps/vlogs/bin/victoria-logs-prod"
sshm bash -s <<INSTALL
set -euo pipefail
APP_DIR=\$HOME/.apps/vlogs
BIN_DIR=\$APP_DIR/bin
DATA_DIR=\$APP_DIR/data
mkdir -p "\$BIN_DIR" "\$DATA_DIR"

# Idempotent: skip download if binary already present.
if [ -x "\$BIN_DIR/victoria-logs-prod" ]; then
  echo "binary already present: \$(\$BIN_DIR/victoria-logs-prod -version 2>&1 | head -1)"
  exit 0
fi

ASSET=victoria-logs-linux-amd64-${VLOGS_VERSION}.tar.gz
URL=https://github.com/VictoriaMetrics/VictoriaLogs/releases/download/${VLOGS_VERSION}/\$ASSET
TMP=\$(mktemp -d)
trap "rm -rf \$TMP" EXIT

echo "downloading \$URL"
curl -fsSL "\$URL" -o "\$TMP/\$ASSET"
tar -xzf "\$TMP/\$ASSET" -C "\$TMP"

# The archive extracts as 'victoria-logs-prod' (single static binary) at the
# top level. Detect by glob to tolerate naming drift across releases.
FOUND=\$(find "\$TMP" -maxdepth 2 -type f -name 'victoria-logs-prod' -perm -u+x 2>/dev/null | head -1)
if [ -z "\$FOUND" ]; then
  FOUND=\$(find "\$TMP" -maxdepth 2 -type f -name 'victoria-logs*prod' 2>/dev/null | head -1)
fi
[ -n "\$FOUND" ] || { echo "could not locate victoria-logs-prod in archive" >&2; exit 1; }
install -m 0755 "\$FOUND" "\$BIN_DIR/victoria-logs-prod"
echo "installed: \$("\$BIN_DIR/victoria-logs-prod" -version 2>&1 | head -1)"
INSTALL

# ── Step 3: deploy unit files + ingester + canary ──────────────────────────
log_info "deploying systemd units, ingester, and canary to seedbox"
sshm 'mkdir -p ~/.opt/_maint_stage'
( cd "$REPO_ROOT" && tar -cf - \
    scripts/maint/systemd/victorialogs.service \
    scripts/maint/systemd/qflix-vlogs-ingest.service \
    scripts/maint/systemd/qflix-vlogs-ingest.timer \
    scripts/maint/systemd/manitoba-maint-canary-vlogs-stall.service \
    scripts/maint/systemd/manitoba-maint-canary-vlogs-stall.timer \
    scripts/maint/qflix-vlogs-ingest.py \
    scripts/canaries/vlogs-stall.sh \
) | sshm 'tar -xf - -C ~/.opt/_maint_stage'

sshm bash -s <<'STAGE'
set -euo pipefail
STG=~/.opt/_maint_stage
mkdir -p ~/scripts/maint/systemd ~/scripts/maint ~/scripts/canaries
cp -f "$STG"/scripts/maint/systemd/victorialogs.service                    ~/scripts/maint/systemd/
cp -f "$STG"/scripts/maint/systemd/qflix-vlogs-ingest.service              ~/scripts/maint/systemd/
cp -f "$STG"/scripts/maint/systemd/qflix-vlogs-ingest.timer                ~/scripts/maint/systemd/
cp -f "$STG"/scripts/maint/systemd/manitoba-maint-canary-vlogs-stall.service ~/scripts/maint/systemd/
cp -f "$STG"/scripts/maint/systemd/manitoba-maint-canary-vlogs-stall.timer   ~/scripts/maint/systemd/
cp -f "$STG"/scripts/maint/qflix-vlogs-ingest.py                           ~/scripts/maint/
chmod +x ~/scripts/maint/qflix-vlogs-ingest.py
cp -f "$STG"/scripts/canaries/vlogs-stall.sh                               ~/scripts/canaries/
chmod +x ~/scripts/canaries/vlogs-stall.sh
STAGE

# Push the port secret to the seedbox so the service unit can read it.
sshm "echo -n '$VLOGS_PORT' > ~/secrets/vlogs.port && chmod 600 ~/secrets/vlogs.port"

# ── Step 4: install + start systemd-user units ─────────────────────────────
sshm bash -s <<'UNITSCRIPT'
set -euo pipefail
mkdir -p ~/.config/systemd/user
for unit in \
    victorialogs.service \
    qflix-vlogs-ingest.service \
    qflix-vlogs-ingest.timer \
    manitoba-maint-canary-vlogs-stall.service \
    manitoba-maint-canary-vlogs-stall.timer; do
  cp -f ~/scripts/maint/systemd/$unit ~/.config/systemd/user/$unit
done
systemctl --user daemon-reload
systemctl --user enable --now victorialogs.service
# Wait for vlogs to come up before enabling the ingest timer.
sleep 3
systemctl --user enable --now qflix-vlogs-ingest.timer
systemctl --user enable --now manitoba-maint-canary-vlogs-stall.timer
# Restart server in case the unit file changed.
systemctl --user restart victorialogs.service
sleep 2
UNITSCRIPT

# ── Step 5: install-time smoke ─────────────────────────────────────────────
log_info "running install-time smoke gate"

PASS=0; FAIL=0
gate() {
  local name="$1" status="$2" detail="${3:-}"
  case "$status" in
    pass) printf '✓ %-40s %s\n' "$name" "$detail"; PASS=$((PASS+1)) ;;
    fail) printf '✗ %-40s %s\n' "$name" "$detail"; FAIL=$((FAIL+1)) ;;
  esac
}

# Smoke 1: HTTP /health responds 200
sleep 2
H=$(sshm "curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:$VLOGS_PORT/health" </dev/null 2>/dev/null || echo "000")
if [ "$H" = "200" ]; then
  gate "vlogs-health" pass "HTTP 200 on 127.0.0.1:$VLOGS_PORT/health"
else
  gate "vlogs-health" fail "HTTP $H — check journalctl --user -u victorialogs.service"
fi

# Smoke 2: service is active
ST=$(sshm "systemctl --user is-active victorialogs.service" </dev/null 2>/dev/null || echo "unknown")
if [ "$ST" = "active" ]; then
  gate "vlogs-service-active" pass
else
  gate "vlogs-service-active" fail "state=$ST"
fi

# Smoke 3: ingest timer scheduled
TM=$(sshm "systemctl --user list-timers qflix-vlogs-ingest.timer --no-pager 2>/dev/null | grep -c qflix-vlogs-ingest.timer" </dev/null 2>/dev/null || echo 0)
if [ "${TM:-0}" -ge 1 ]; then
  gate "ingest-timer-scheduled" pass
else
  gate "ingest-timer-scheduled" fail "timer not in systemctl list-timers"
fi

# Smoke 4: fire one ingest cycle and verify index has > 0 lines
log_info "triggering first ingest cycle (may take ~30s)..."
sshm "systemctl --user start qflix-vlogs-ingest.service" </dev/null 2>/dev/null || true
# Block until ingest service finishes (oneshot).
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 3
  IS=$(sshm "systemctl --user is-active qflix-vlogs-ingest.service" </dev/null 2>/dev/null || echo "unknown")
  case "$IS" in
    inactive|failed) break ;;
  esac
done

# Query vlogs for total count last 1h.
NIDX=$(sshm "curl -sf -m 10 --get \
  --data-urlencode 'query=* | stats count() as n' \
  --data-urlencode 'start=1h' \
  http://127.0.0.1:$VLOGS_PORT/select/logsql/query 2>/dev/null \
  | python3 -c 'import sys, json
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    try: d=json.loads(line); print(d.get(\"n\", 0)); break
    except: pass
else:
    print(0)'" </dev/null 2>/dev/null || echo 0)
if [ "${NIDX:-0}" -ge 1 ]; then
  gate "first-ingest-cycle" pass "$NIDX lines indexed"
else
  gate "first-ingest-cycle" fail "0 lines after first ingest cycle — check: journalctl --user -u qflix-vlogs-ingest.service"
fi

# Smoke 5: canary timer scheduled
CT=$(sshm "systemctl --user list-timers manitoba-maint-canary-vlogs-stall.timer --no-pager 2>/dev/null | grep -c manitoba-maint-canary-vlogs-stall.timer" </dev/null 2>/dev/null || echo 0)
if [ "${CT:-0}" -ge 1 ]; then
  gate "canary-timer-scheduled" pass
else
  gate "canary-timer-scheduled" fail "canary timer not scheduled"
fi

# Smoke 6: canary script runs green (uses local vlogs)
CN=$(sshm "bash ~/scripts/canaries/vlogs-stall.sh 2>&1" </dev/null 2>/dev/null) || CN="<failed>"
if echo "$CN" | grep -q "vlogs-flowing"; then
  gate "canary-dry-run" pass "$(echo "$CN" | grep vlogs-flowing | head -c 80)"
else
  gate "canary-dry-run" fail "$(echo "$CN" | tail -1 | head -c 120)"
fi

echo
TOTAL=$((PASS + FAIL))
printf "Install smoke: %d/%d pass\n" "$PASS" "$TOTAL"
[ "$FAIL" = 0 ] || die "install-time smoke failed — see output above"

log_info "Phase 80 complete — VictoriaLogs installed + smoke ${PASS}/${TOTAL}"
echo
echo "Query the index from the workstation:"
echo "  ssh -L $VLOGS_PORT:127.0.0.1:$VLOGS_PORT \$SEEDBOX_SSH_HOST -N"
echo "  open http://127.0.0.1:$VLOGS_PORT/select/vmui/"
echo "  (the MCP qflix_query_logs tool also works via SSH-exec'd curl — no tunnel needed)"
