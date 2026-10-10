#!/usr/bin/env bash
# 20-install-stack.sh -- install the whole stack on green from the repo: 240 +
# every numbered configure phase, INCLUDING the 3NN-native-<slug> installers
# that replace Ultra's panel apps (spec 5.1, section 8 row 20).
#
# Runs against green only (NEW_HOST). Blue is only ASKED for its host profile.
#
# HOW A PHASE IS AIMED AT GREEN: the one override every configure script
# already honors -- SSHM_HOST=$NEW_HOST (lib/ssh.sh) -- plus
# SECRETS_DIR=secrets/green/ (lib/secrets.sh), so phases read GREEN's ports,
# keys and host.profile=generic, never blue's. Phases that cannot be retargeted
# that way are in SKIP with the reason.
#
# NATIVE INSTALLERS ARE GENERATED, NOT LISTED. `migrate_manifest.py
# native-installers` names one 3NN-native-<slug>-install.sh per ever-UCC app in
# manifest/apps.yaml. A missing installer is a precondition failure under
# --execute (exit 1, nothing run): box 2 has no panel to fall back on. Fresh
# installs on green get `QFLIX_INSTALL_MODE=fresh` (claim a port, mint a key,
# start the unit -- spec 5.5), the contract README.md documents for the A-tickets.
#
# I-1 SINGLE PAGER, enforced after EVERY phase (a phase such as 240 or
# 49-qflix-newsletter-install may re-enable something): green's comms jobs
# (newsletter timer, listmonk-sync cron; manifest/jobs.yaml via migrate.conf)
# are held and green's Discord webhook secret is parked. If green has a Kuma,
# its human channels are detached once all phases ran (kuma_channels.py mute).
#
# I-3 inert without --execute (plan only, no ssh). I-4 every phase is
# idempotent, so a re-run after a failure just continues.
#
# USAGE: 20-install-stack.sh NEW_HOST [--old-host HOST] [--execute]
# EXIT:  0 ok | 1 a phase failed / installer missing | 2 refused (usage,
#        unreachable, green secrets not bootstrapped, window)
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

usage() { echo "usage: $0 NEW_HOST [--old-host HOST] [--execute]" >&2; }
mig_args "" "$@"
need_new_host
need_tables
CONFIGURE_DIR="${QFLIX_CONFIGURE_DIR:-$ROOT/scripts/configure}"

declare -A SKIP=(
  [31-unpackerr.sh]="superseded by the native unpackerr installer (it runs app-unpackerr, a panel verb; I-9)"
  [49b-conjurr-newsletterr-decom.sh]="decommissions apps green never had"
  [60-www-images.sh]="writes an Ultra proxy.d fragment + app-nginx (F-11); box-2 proxy is QFLX-41"
  [90-qflix-dash-install.sh]="reads secrets/seedbox.ssh-host directly (ignores SSHM_HOST) -- it would install onto BLUE. Patch it to honor SSHM_HOST, then drop this skip"
  [90-sabnzbd-usenet-install.sh]="Ultra app-CLI install path; SAB server + arr wiring arrive with SAB/arr data in 35"
  [91-nginx-root-to-dash.sh]="front-door change (50-cutover step 7 / QFLX-41) and reads seedbox.ssh-host directly"
)

# Numbered phases in numeric-then-bytewise order (plain sort -V puts 04b
# before 04; see the stale branch's note). 3NN-native-* sort into place.
mapfile -t PHASES < <(
  for f in "$CONFIGURE_DIR"/[0-9]*.sh; do [ -e "$f" ] && basename "$f"; done \
  | awk '{ match($0, /^[0-9]+/); printf "%05d\t%s\t%s\n", substr($0,RSTART,RLENGTH), substr($0,RLENGTH+1), $0 }' \
  | LC_ALL=C sort -k1,1 -k2,2 | cut -f3)

mapfile -t NATIVE < <(mm native-installers --configure-dir "$CONFIGURE_DIR")
[ "${#NATIVE[@]}" -gt 0 ] || stage 2 manifest-table "no-ever-UCC-apps-in-manifest"
MISSING=()
for row in "${NATIVE[@]}"; do
  IFS=$'\t' read -r name slug inst <<<"$row"
  [ "$inst" = MISSING ] && MISSING+=("$slug")
done

echo "=== 20-install-stack: green = $NEW_HOST ($([ "$EXECUTE" -eq 1 ] && echo EXECUTE || echo dry-run)) ==="
echo "-- native installers (generated from manifest/apps.yaml) --"
for row in "${NATIVE[@]}"; do
  IFS=$'\t' read -r name slug inst <<<"$row"
  if [ "$inst" = MISSING ]; then printf '  [MISSING] %-14s scripts/configure/3NN-native-%s-install.sh\n' "$name" "$slug"
  else printf '  [native ] %-14s %s\n' "$name" "$inst"; fi
done
echo "-- configure phases (run order; SSHM_HOST=NEW_HOST SECRETS_DIR=secrets/green QFLIX_INSTALL_MODE=fresh) --"
for p in "${PHASES[@]}"; do
  if [ -n "${SKIP[$p]+x}" ]; then printf '  [SKIP] %-36s %s\n' "$p" "${SKIP[$p]}"
  else printf '  [ run] %s\n' "$p"; plan "after $p: hold comms jobs + park Discord webhook on green (I-1)"; fi
done
plan "after all phases: kuma_channels.py mute on green (if green has a Kuma)"
plan "after all phases: systemctl --user cat <unit> for every manifest app's green unit"

if [ "$EXECUTE" -ne 1 ]; then
  [ "${#MISSING[@]}" -eq 0 ] || log_warn "${#MISSING[@]} native installer(s) missing: ${MISSING[*]} (--execute will refuse)"
  echo "[dry-run] no ssh made, nothing changed. Re-run with --execute."
  exit 0
fi

[ "${#MISSING[@]}" -eq 0 ] || stage 1 native-installer-missing "${MISSING[*]}-(A-tickets-not-merged;-nothing-was-run)"
[ -s "$GREEN_SECRETS/host.profile" ] || stage 2 green-not-bootstrapped "secrets/green/host.profile-missing;-run-15-bootstrap-new.sh"
[ "$(tr -d '[:space:]' < "$GREEN_SECRETS/host.profile")" = generic ] \
  || stage 2 green-profile "secrets/green/host.profile-is-not-generic"
[ -s "$GREEN_SECRETS/seedbox.host" ] || stage 2 green-public-host "write-green's-public-hostname-to-secrets/green/seedbox.host"
resolve_old_host
sshg true >/dev/null 2>&1 || stage 2 green-unreachable "ssh-to-NEW_HOST-failed"
window_guard

HOLD="$(comms_cmd hold); $(pager_cmd mute)"
for p in "${PHASES[@]}"; do
  [ -n "${SKIP[$p]+x}" ] && continue
  log_info "running $p against green"
  if ! SSHM_HOST="$NEW_HOST" SECRETS_DIR="$GREEN_SECRETS" QFLIX_INSTALL_MODE=fresh \
       bash "$CONFIGURE_DIR/$p" </dev/null; then
    sshg "$HOLD" >/dev/null 2>&1
    stage 1 phase-failed "$p-exited-nonzero;-re-run-20-(completed-phases-are-idempotent)"
  fi
  sshg "$HOLD" >/dev/null 2>&1 || stage 1 comms-hold-failed "after-$p"
done

KUMA="$(on_box g kuma_channels.py mute 2>/dev/null)"; KRC=$?
case "$KRC" in
  0) log_info "green Kuma muted: $KUMA" ;;
  2) log_warn "green has no reachable Kuma yet ($KUMA) -- QFLX-41; 40-validate-green will fail until it does" ;;
  *) stage 1 kuma-mute-failed "$KUMA" ;;
esac

UNITS="$(mm apps | awk -F'\t' '$5!=""{print $5}' | sort -u | tr '\n' ' ')"
NOUNIT="$(sshg "for u in $UNITS; do systemctl --user cat \$u >/dev/null 2>&1 || echo \$u; done" 2>/dev/null | tr -d '\r' | tr '\n' ' ')"
[ -z "${NOUNIT// /}" ] || stage 1 units-missing "$NOUNIT"
log_info "20-install-stack complete: every manifest app has its unit on green; comms held; webhook parked."
log_info "Next: 30-sync-media.sh $NEW_HOST --execute (bulk), then 35-sync-appdata.sh."
exit 0
