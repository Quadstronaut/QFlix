#!/usr/bin/env bash
# listmonk-cancel-running.sh — cancel any campaign stuck in `running` state
# BEFORE listmonk (re)starts, so a crash/restart mid-send cannot auto-resume
# and re-send the whole subscriber list.
#
# ROOT CAUSE this closes: Listmonk resumes `running` campaigns on boot and
# resends the full batch (no per-subscriber checkpoint commits for a regular
# campaign interrupted mid-send). That produced the 2026-05-18 Maintenance
# Window Start double-send (sent=22 to an 11-person list) and the 2026-05-11
# newsletter double-send (sent=19). Either systemd (`Restart=on-failure`,
# 5s) or scripts/ops/heartbeat-listmonk.sh can restart listmonk after a
# mid-send death; both paths go through unit start, so guarding start covers
# all of them.
#
# Wired as `ExecStartPre=-` on listmonk.service: the leading `-` means a
# failure here NEVER blocks listmonk from starting (mail must come back up
# even if this guard hiccups). On a NORMAL start there is no running campaign,
# so the UPDATE touches 0 rows and this is a silent no-op. On a crash-mid-send
# restart, the interrupted campaign becomes `cancelled` (a partial single
# send) instead of being resent to everyone — strictly better for "no
# duplicate customer emails". The cancel is logged so the operator can decide
# whether to re-fire deliberately.
set -uo pipefail

CONF="$HOME/.apps/listmonk/etc/config.toml"
[ -f "$CONF" ] || { logger -t listmonk-cancel-running "no config.toml at $CONF; skip"; exit 0; }

# Pull a value out of the [db] table. Handles both quoted ("listmonk") and
# bare (42009) TOML values, tolerates surrounding whitespace.
db_val() {
  sed -n '/^\[db\]/,/^\[/p' "$CONF" \
    | awk -F= -v k="$1" '
        $1 ~ "^[[:space:]]*"k"[[:space:]]*$" {
          sub(/^[^=]*=[[:space:]]*/, "")
          gsub(/^["[:space:]]+|["[:space:]]+$/, "")
          print; exit
        }'
}

DB_HOST=$(db_val host)
DB_PORT=$(db_val port)
DB_USER=$(db_val user)
DB_NAME=$(db_val database)
DB_PASS=$(db_val password)

if [ -z "$DB_HOST" ] || [ -z "$DB_PORT" ] || [ -z "$DB_USER" ] || [ -z "$DB_NAME" ]; then
  logger -t listmonk-cancel-running "incomplete [db] config (host/port/user/database); skip"
  exit 0
fi

command -v psql >/dev/null 2>&1 || { logger -t listmonk-cancel-running "psql not on PATH; skip"; exit 0; }

# -tA = tuples-only, unaligned → one campaign id per line. RETURNING lets us
# count what we cancelled for the operator log.
CANCELLED=$(PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc \
  "UPDATE campaigns SET status='cancelled', updated_at=now() WHERE status='running' RETURNING id;" \
  2>/dev/null | grep -c '^[0-9]')

if [ "${CANCELLED:-0}" -gt 0 ]; then
  logger -t listmonk-cancel-running \
    "cancelled ${CANCELLED} running campaign(s) pre-start to prevent resume-resend (see incident 2026-05-18)"
fi
exit 0
