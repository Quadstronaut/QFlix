#!/usr/bin/env bash
# 20-install-stack.sh NEW_HOST [--execute]
#
# Runs against: green (NEW_HOST). Touches blue: never.
# Precondition: 15-bootstrap-new.sh already ran — ~/.opt/qflix-src exists on
# green, ~/scripts is seeded, and secrets/ in THIS repo has been re-pointed at
# green's slot-specific values (ports/urlbases via bootstrap-discover.sh) with
# identity secrets copied over. secrets/seedbox.host and .ssh-host are NOT
# touched by that step and keep pointing at blue for the whole migration —
# every non-migration script (canaries, smoke tests, deploy-drift...) still
# needs blue as its default target right up to cutover.
#
# WHAT THIS DOES
#   1. Runs every numbered scripts/configure/*.sh phase, in order, against
#      green — via the ONE override lib/ssh.sh already supports:
#      `SSHM_HOST="$NEW_HOST" bash <phase>`. Each phase sources lib/ssh.sh
#      itself and `sshm`'s `${SSHM_HOST:-<blue default>}` honors our export,
#      so 30+ existing scripts retarget at green with zero edits. (.py-only
#      phases, e.g. the retired Maintainerr scripts, are not enumerated —
#      each numbered phase that still needs Python ships a same-numbered .sh
#      wrapper; see the loop below.)
#   2. Runs scripts/maint/bootstrap-kuma-monitors.py against green's Kuma
#      (over a throwaway SSH tunnel — same pattern its own docstring uses for
#      blue) to create all 78 monitors + push tokens.
#   3. Detaches every DEFAULT (human-facing) notification channel — Discord
#      today — from every monitor bootstrap-kuma-monitors.py just wired up,
#      per I-1: exactly one side may page the operator, and that's blue until
#      cutover. The auto-heal WEBHOOK channel is deliberately left attached:
#      it POSTs to green's own loopback maint daemon, not to a human, so
#      leaving it on lets green's own lib.recovery auto-heal function from
#      day one without violating I-1.
#
#      WHY A POST-STEP AND NOT A FLAG: bootstrap-kuma-monitors.py has no
#      "skip notifications" option (checked: zero add_argument calls in the
#      file — it takes no CLI flags at all, only KUMA_URL/MANITOBA_MANIFEST
#      env vars). Worse, it HARD-FAILS its own run if any active monitor ends
#      up with zero channels (_mute_monitor_names / the "FATAL: ... have NO
#      notification channels" branch) — that's a deliberate invariant of that
#      script (a monitor that can go red in total silence is the exact class
#      of bug its own history section describes), so we cannot ask it to ship
#      monitors muted. We let it attach its full default set, then detach
#      only the human-facing one ourselves, immediately after.
#
# SKIPPED PHASES (see SKIP below) are numbered .sh files that DO exist in
# scripts/configure/ but must not run here, each with a concrete reason.
# provision-admin-key.sh has no leading digit — it is not a numbered phase at
# all and is out of scope for this script on that basis alone.
#
# Idempotent / resumable (I-4): every configure/*.sh phase documents its own
# idempotency, so re-running this whole script after a mid-run failure simply
# re-runs the phases that already succeeded (cheap no-ops) and continues past
# the one that broke. The Discord-detach step is naturally idempotent too —
# safe to re-run any time before cutover, including if something else
# re-attaches Discord in the meantime.
#
# Inert without --execute (I-3): prints the full phase plan + Kuma plan and
# exits 0. No SSH, no tunnel, no mutation happens in dry-run mode.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
CONFIGURE_DIR="$ROOT/scripts/configure"

# shellcheck source=/dev/null
source "$ROOT/scripts/lib/log.sh"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/secrets.sh"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"   # only used here for the $SSHM_OPTS array;
                                     # we never call sshm() ourselves — NEW_HOST
                                     # goes to configure/ children via SSHM_HOST=

usage() { echo "usage: $0 NEW_HOST [--execute]" >&2; }

EXECUTE=0
NEW_HOST=""
for arg in "$@"; do
  case "$arg" in
    --execute) EXECUTE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) [ -z "$NEW_HOST" ] && NEW_HOST="$arg" ;;
  esac
done
if [ -z "$NEW_HOST" ]; then
  usage
  printf 'STAGE=usage msg=missing-NEW_HOST\n' >&2
  exit 2
fi

# --- explicit SKIP list: numbered configure/*.sh phases that do NOT run here ---
declare -A SKIP=(
  [49b-conjurr-newsletterr-decom.sh]="decommissions Conjurr+Newsletterr, both purged from blue on 2026-05-11 — long before green exists. Green never installed them; there is nothing to decommission."

  [90-qflix-dash-install.sh]="reads secrets/seedbox.ssh-host / seedbox.host DIRECTLY (HOST=\$(cat ...); SSH=(ssh ... \"quadstronaut@\$HOST\")) — it does NOT go through lib/ssh.sh's sshm(), so our SSHM_HOST=\$NEW_HOST override has no effect and running it here would install onto BLUE. Needs a one-line patch (read \${SSHM_HOST:-<seedbox.ssh-host>} like every other configure script) before it can join this loop. Until then: install qflix-dash on green by hand."

  [91-nginx-root-to-dash.sh]="same seedbox.ssh-host hardcode as 90 above (no override hook — would target blue), AND it is a public-traffic CUTOVER action (flips the nginx root), which belongs at cutover time under operator control, not a blind stack install. Leave green's nginx root alone here; point it at dash as part of the cutover sequence once 90 is patched/run."
)

# --- enumerate numbered configure/*.sh phases, numeric+lexical order ---
# Plain `sort -V` mis-orders this filename scheme — it puts "04b-..." and
# "59a-..." BEFORE their bare "04-..."/"59-..." counterparts (verified: GNU
# version-sort compares "04"/"04b" by scanning digit-runs across the whole
# name, not by splitting at the first non-digit byte), which would run 04b's
# indexer test before 04 has pushed the indexer manifest, and 59a's plex
# crons before 59's venv they depend on exists. So we sort explicitly on
# (leading numeric prefix, remaining filename) instead — zero-padded numeric
# key first, then a byte-wise (LC_ALL=C) tiebreak on the rest, which is what
# actually reproduces the intended phase order.
# "50-buildarr-install.sh" / "50-tautulli-pms-url-fix.sh" / "50-tdarr-install.sh"
# and "60-buildarr-patches.sh" / "60-www-images.sh" share a numeric prefix —
# each pair/triple is functionally independent (different apps, no
# cross-dependency), so whichever way the tiebreak lands is safe.
mapfile -t PHASES < <(
  for f in "$CONFIGURE_DIR"/*.sh; do
    base="$(basename "$f")"
    case "$base" in
      [0-9]*) printf '%s\n' "$base" ;;
    esac
  done | awk '{ match($0, /^[0-9]+/); n=substr($0,RSTART,RLENGTH); r=substr($0,RLENGTH+1); printf "%05d\t%s\t%s\n", n, r, $0 }' \
       | LC_ALL=C sort -k1,1 -k2,2 \
       | cut -f3
)

echo "=== 20-install-stack.sh: green = $NEW_HOST ==="
echo
echo "-- configure/ phases (in run order) --"
for p in "${PHASES[@]}"; do
  if [ -n "${SKIP[$p]+x}" ]; then
    printf '  [SKIP] %-40s %s\n' "$p" "${SKIP[$p]}"
  else
    printf '  [ run] %s\n' "$p"
  fi
done
echo
echo "-- after phases: --"
echo "  [ run] scripts/maint/bootstrap-kuma-monitors.py (against green's Kuma, via tunnel)"
echo "  [ run] detach default (Discord) notification channels from every monitor it created;"
echo "         auto-heal webhook stays attached (I-1: green pages nobody until cutover)"

if [ "$EXECUTE" -ne 1 ]; then
  echo
  echo "[dry-run] no SSH made, nothing changed. Re-run with --execute to apply."
  exit 0
fi

echo
log_info "checking SSH reachability of $NEW_HOST..."
if ! ssh "${SSHM_OPTS[@]}" "$NEW_HOST" true 2>/dev/null; then
  printf 'STAGE=green-unreachable msg=ssh-to-%s-failed\n' "$NEW_HOST" >&2
  exit 2
fi

# --- run each phase, retargeted at green via the existing override hook ---
for p in "${PHASES[@]}"; do
  [ -n "${SKIP[$p]+x}" ] && { log_warn "skip $p — ${SKIP[$p]}"; continue; }
  log_info "running $p against $NEW_HOST..."
  if ! SSHM_HOST="$NEW_HOST" bash "$CONFIGURE_DIR/$p"; then
    printf 'STAGE=phase-failed msg=%s-exited-nonzero-against-%s\n' "$p" "$NEW_HOST" >&2
    echo "Re-run this script — completed phases before $p are idempotent no-ops (I-4)." >&2
    exit 1
  fi
done
log_info "all configure/ phases installed on green."

# --- Kuma: bootstrap monitors, then detach Discord (leave auto-heal on) ---
KUMA_PORT="$(secret_read uptimekuma.port)"   # slot-specific — never hardcode
KUMA_URL="http://127.0.0.1:${KUMA_PORT}"

PYTHON=""
for cand in "$ROOT/tests/.venv/Scripts/python.exe" "$ROOT/tests/.venv/bin/python" python3; do
  if command -v "$cand" >/dev/null 2>&1; then PYTHON="$cand"; break; fi
done
if [ -z "$PYTHON" ]; then
  printf 'STAGE=python-missing msg=no-interpreter-with-uptime_kuma_api-found\n' >&2
  exit 2
fi

log_info "opening tunnel to green Kuma ($NEW_HOST:$KUMA_PORT -> 127.0.0.1:$KUMA_PORT)..."
ssh "${SSHM_OPTS[@]}" -N -L "${KUMA_PORT}:127.0.0.1:${KUMA_PORT}" "$NEW_HOST" &
TUNNEL_PID=$!
trap 'kill "$TUNNEL_PID" 2>/dev/null' EXIT
sleep 2

log_info "bootstrap-kuma-monitors.py against green..."
if ! KUMA_URL="$KUMA_URL" "$PYTHON" "$ROOT/scripts/maint/bootstrap-kuma-monitors.py"; then
  printf 'STAGE=kuma-bootstrap-failed msg=bootstrap-kuma-monitors-exited-nonzero\n' >&2
  exit 1
fi

log_info "detaching Discord (default human-facing channels) from green's monitors..."
if ! KUMA_URL="$KUMA_URL" "$PYTHON" - <<PYEOF
import sys
from pathlib import Path
REPO_ROOT = Path(r"$ROOT")

def read_secret(name):
    return (REPO_ROOT / "secrets" / name).read_text().strip()

from uptime_kuma_api import UptimeKumaApi
import os

api = UptimeKumaApi(os.environ["KUMA_URL"])
USER = "quadstronaut"
logged_in = False
for pw_name in ("htpasswd.password", "shared-admin.password"):
    try:
        api.login(USER, read_secret(pw_name))
        logged_in = True
        break
    except FileNotFoundError:
        continue
    except Exception:
        continue
if not logged_in:
    print("FATAL: could not log in to green Kuma", file=sys.stderr)
    sys.exit(1)

# The one channel that must stay attached: it posts to green's OWN loopback
# maint daemon (lib.recovery auto-heal), never to a human. Every other
# default channel — Discord — is a page and must come off per I-1.
AUTOHEAL = "Manitoba auto-heal webhook"
notifications = api.get_notifications()
detach_ids = {n["id"] for n in notifications if n.get("isDefault") and n.get("name") != AUTOHEAL}
if not detach_ids:
    print("no default human-facing channels found — nothing to detach")
    api.disconnect()
    sys.exit(0)

fixed = 0
for m in api.get_monitors():
    raw = m.get("notificationIDList") or {}
    current = {int(k) for k, v in raw.items() if v} if isinstance(raw, dict) else set(raw)
    keep = current - detach_ids
    if keep == current:
        continue
    api.edit_monitor(m["id"], notificationIDList={str(i): True for i in keep})
    print(f"  [mute]{m['name']:32s} removed {sorted(current & detach_ids)}")
    fixed += 1
print(f"detached Discord from {fixed} monitor(s); auto-heal webhook left attached on all.")
api.disconnect()
PYEOF
then
  printf 'STAGE=discord-detach-failed msg=post-step-exited-nonzero\n' >&2
  exit 1
fi

echo
log_info "20-install-stack.sh complete: units installed, green pusher pushing, zero Discord."
echo "Still manual before this box is fully caught up (see SKIP reasons above):"
echo "  - qflix-dash (90) and the nginx-root cutover (91) — patch their host"
echo "    resolution or run them by hand against $NEW_HOST."
echo "50-cutover.sh is what re-attaches Discord on green and disarms it on blue."
