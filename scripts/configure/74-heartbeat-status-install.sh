#!/usr/bin/env bash
# scripts/configure/74-heartbeat-status-install.sh
# Heartbeat v2 (Android) — installer for the seedbox side.
#  - Deploy scripts/mcp/ (incl. app_status.py + lib/) to ~/scripts/mcp/
#  - Mint a forced-command-only ed25519 key for the phone (box-side only,
#    only if missing)
#  - Append a restricted authorized_keys entry that runs ONLY app_status.py
#    (SAFETY CRITICAL: append-only, dated backup taken first, never
#    rewritten/sorted/deduped)
#  - Clean up the `~/nul` recon artifact left over from live-recon
#  - Install-time gate: JSON contract sanity + authorized_keys hygiene +
#    proof the admin channel still works
#
# 71/72/73 were already taken (mcp-manifest-update, workstation-kuma-monitor,
# quality-fallback-install) at write time — this is 74, the next free slot.
#
# Idempotent: safe to re-run. No step rewrites/destroys prior state; every
# mutation is append-only or a no-op if already applied.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# shellcheck disable=SC1091
source "$REPO/scripts/lib/ssh.sh"   # provides $SSHM_HOST + sshm/scpm_to
# shellcheck disable=SC1091
source "$REPO/scripts/lib/log.sh"   # provides log_info/log_warn/log_error/die

PHONE_KEY_COMMENT="qflix-heartbeat-phone"
PHONE_KEY_PATH="~/.ssh/heartbeat_phone_ed25519"
FORCED_CMD="python3 /home/quadstronaut/scripts/mcp/app_status.py"

# ── Step 1: deploy scripts/mcp/ (tar-over-ssh, same convention as 70) ───────
log_info "Phase 74: heartbeat status installer"
log_info "deploying scripts/mcp/ to ${SSHM_HOST}:~/scripts/mcp/"
sshm "mkdir -p ~/scripts/mcp"
( cd "$REPO/scripts/mcp" && tar --exclude='__pycache__' --exclude='*.pyc' -cf - . ) \
  | sshm "tar -C scripts/mcp -xf -"

# ── Step 2: mint the phone's forced-command key ON THE BOX, only if missing ─
log_info "checking for existing phone key (${PHONE_KEY_PATH})"
KEY_OUT=$(sshm 'bash -s' <<KEYSCRIPT
set -euo pipefail
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if [ ! -f ${PHONE_KEY_PATH} ]; then
  ssh-keygen -t ed25519 -N '' -C ${PHONE_KEY_COMMENT} -f ${PHONE_KEY_PATH} -q
  echo "KEY_MINTED=1"
else
  echo "KEY_MINTED=0"
fi
KEYSCRIPT
)
KEY_MINTED=$(echo "$KEY_OUT" | grep '^KEY_MINTED=' | cut -d= -f2)
if [ "$KEY_MINTED" = "1" ]; then
  log_info "minted new phone key: ${PHONE_KEY_PATH}"
else
  log_info "phone key already present: ${PHONE_KEY_PATH} (skipped mint)"
fi

# ── Step 3: authorized_keys patch — SAFETY CRITICAL ──────────────────────────
# Dated backup first (only if none taken today), then append-only with a
# grep -qF guard on the raw pubkey material. NEVER rewrite/sort/dedupe the
# file — a stray sort/rewrite here can lock the operator's own admin key
# out of the box.
log_info "patching authorized_keys (backup + append-only, guarded)"
AK_OUT=$(sshm 'bash -s' <<AKSCRIPT
set -euo pipefail
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys
BACKUP=~/.ssh/authorized_keys.bak-\$(date +%F)
if [ ! -f "\$BACKUP" ]; then
  cp ~/.ssh/authorized_keys "\$BACKUP"
  echo "BACKUP_MADE=1 \$BACKUP"
else
  echo "BACKUP_MADE=0 \$BACKUP"
fi
PUBLINE=\$(cat ${PHONE_KEY_PATH}.pub)
PUBKEY=\$(awk '{print \$1, \$2}' <<<"\$PUBLINE")
ENTRY="command=\"${FORCED_CMD}\",restrict \${PUBKEY}"
if grep -qF "\$PUBKEY" ~/.ssh/authorized_keys; then
  echo "ENTRY_APPENDED=0"
else
  printf '%s\n' "\$ENTRY" >> ~/.ssh/authorized_keys
  echo "ENTRY_APPENDED=1"
fi
chmod 600 ~/.ssh/authorized_keys
AKSCRIPT
)
BACKUP_LINE=$(echo "$AK_OUT" | grep '^BACKUP_MADE=')
ENTRY_APPENDED=$(echo "$AK_OUT" | grep '^ENTRY_APPENDED=' | cut -d= -f2)
log_info "authorized_keys backup: ${BACKUP_LINE#BACKUP_MADE=* }"
if [ "$ENTRY_APPENDED" = "1" ]; then
  log_info "appended forced-command entry for phone key"
else
  log_info "forced-command entry already present (skipped append)"
fi

# ── Step 4: cleanup recon artifact ───────────────────────────────────────────
sshm "rm -f ~/nul"
log_info "cleaned up ~/nul recon artifact (if present)"

# ── Step 5: install-time gate ────────────────────────────────────────────────
log_info "running install-time gate"
PASS=0; FAIL=0
gate() {
  local name="$1" status="$2" detail="${3:-}"
  case "$status" in
    pass) printf '✓ %-32s %s\n' "$name" "$detail"; PASS=$((PASS+1)) ;;
    fail) printf '✗ %-32s %s\n' "$name" "$detail"; FAIL=$((FAIL+1)) ;;
  esac
}

# Gate (a): box-side run of app_status.py (normal admin channel, NOT the
# forced-command key — that's proven separately from the workstation after
# this script exits) emits valid JSON with meta.version==1. Per-section ok
# status is captured and reported in the detail string, but does not itself
# fail this gate — section failure isolation is a designed behavior (a dead
# source degrades that section, not the whole doc); a red section here is a
# signal for the operator to investigate app_status.py against live data,
# not an installer defect.
GATE_OUT=$(sshm 'bash -s' <<'GATESCRIPT'
set -uo pipefail
python3 ~/scripts/mcp/app_status.py > /tmp/qflix-heartbeat-gate.json 2>/tmp/qflix-heartbeat-gate.err
echo "APP_STATUS_EXIT=$?"
python3 - <<'PYEOF'
import json
try:
    d = json.load(open("/tmp/qflix-heartbeat-gate.json"))
    print("JSON_VALID=1")
    print("VERSION=" + str(d.get("meta", {}).get("version")))
    for s in ("quota", "kuma", "streams", "top5", "downloads"):
        sec = d.get(s) or {}
        print("SECTION_{}={}".format(s.upper(), sec.get("ok")))
except Exception as e:
    print("JSON_VALID=0")
    print("PARSE_ERROR=" + str(e).replace("\n", " "))
PYEOF
GATESCRIPT
)
APP_STATUS_EXIT=$(echo "$GATE_OUT" | grep '^APP_STATUS_EXIT=' | cut -d= -f2)
JSON_VALID=$(echo "$GATE_OUT" | grep '^JSON_VALID=' | cut -d= -f2)
VERSION=$(echo "$GATE_OUT" | grep '^VERSION=' | cut -d= -f2)
SECTION_MAP=$(echo "$GATE_OUT" | grep '^SECTION_' | tr '\n' ' ')
if [ "$APP_STATUS_EXIT" = "0" ] && [ "$JSON_VALID" = "1" ] && [ "$VERSION" = "1" ]; then
  gate "app-status-live-json" pass "version=${VERSION} ${SECTION_MAP}"
else
  ERRDETAIL=$(sshm "tail -3 /tmp/qflix-heartbeat-gate.err" </dev/null 2>/dev/null || echo "?")
  gate "app-status-live-json" fail "exit=${APP_STATUS_EXIT} json_valid=${JSON_VALID} version=${VERSION} err='${ERRDETAIL}'"
fi

# Gate (b): authorized_keys contains exactly one heartbeat entry.
AK_COUNT=$(sshm "PUB=\$(cat ${PHONE_KEY_PATH}.pub | awk '{print \$2}'); grep -cF \"\$PUB\" ~/.ssh/authorized_keys" </dev/null 2>/dev/null || echo "?")
if [ "$AK_COUNT" = "1" ]; then
  gate "authorized-keys-single-entry" pass "count=${AK_COUNT}"
else
  gate "authorized-keys-single-entry" fail "count=${AK_COUNT} (expected exactly 1)"
fi

# Gate (c): plain admin channel still works — proves authorized_keys wasn't
# corrupted by the patch in Step 3.
ALIVE=$(sshm 'echo alive' </dev/null 2>/dev/null || echo "")
if [ "$ALIVE" = "alive" ]; then
  gate "admin-channel-alive" pass
else
  gate "admin-channel-alive" fail "got '${ALIVE}' (expected 'alive') — authorized_keys may be corrupted, check ${BACKUP_LINE#BACKUP_MADE=* } on the box"
fi

echo
TOTAL=$((PASS + FAIL))
printf "Install gate: %d/%d pass\n" "$PASS" "$TOTAL"
[ "$FAIL" = 0 ] || die "install-time gate failed — see output above"

log_info "Phase 74 complete — heartbeat status installed + gate ${PASS}/${TOTAL}"
