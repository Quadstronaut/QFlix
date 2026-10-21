#!/usr/bin/env bash
# ucc-uninstall.sh -- remove a migrated app's DORMANT Ultra.cc (UCC) container,
# keeping its data (QFLX-60, prep for QFLX-56).
#
# Operator ruling 2026-10-21: once the native unit is verified "functionally
# the same" (docs/ucc-uninstall-runbook.md), the dormant UCC app that was kept
# as the rollback target (I-8) is uninstalled. Unpackerr pilots; Seerr is last
# and is REFUSED here until its vhost has a replacement route.
#
# Usage:  ucc-uninstall.sh <app> [--execute]
#   Dry-run by default: every gate runs (they only read), every mutating step
#   is printed, nothing is written -- not even the log.
#
# THE ONE HAZARD. `app-<slug> uninstall` without --keep-config DELETES the
# config files, and ~/.apps/<slug> IS the live native data dir (the swap ran
# the native binary over the container's data in place). So the uninstall argv
# is built in exactly one place, uninstall_ucc(), with --keep-config literal in
# it; no variable, flag or caller can drop it (test pins the argv).
#
# Gates (exit 2, nothing touched): app resolves via `appctl lookup` (the same
# resolver every lifecycle call uses), class systemd, ucc_dormant truthy,
# swap_state != pending-swap, native unit active, app not seerr, a key-file
# table entry exists, ~/.apps/<slug> and app-<slug> exist, and not inside the
# Monday 11:00-15:00 UTC Ultra.cc maintenance window.
#
# Steps (--execute): backup tarball (verified by listing) -> proxy conf
# snapshot -> app-<slug> uninstall --keep-config -> post-checks. A failed
# post-check exits 1 with the reason; it NEVER reinstalls (a reinstall would
# put a second runtime on the live data dir -- that call is the operator's).
#
# Exit: 0 ok | 1 a step or post-check failed | 2 refused | 64 usage.
# Log: ~/.opt/maint/ucc-uninstall.log (execute mode only).
#
# Overrides (tests): UCC_UNINSTALL_APPCTL, UCC_UNINSTALL_APPS_DIR,
# UCC_UNINSTALL_BACKUP_DIR, UCC_UNINSTALL_LOG, UCC_UNINSTALL_MANIFEST,
# UCC_UNINSTALL_LIB, UCC_UNINSTALL_PYTHON, UCC_UNINSTALL_PROC,
# UCC_UNINSTALL_UID, UCC_UNINSTALL_SETTLE_S; QFLIX_NOW (ISO time for the window check).
set -uo pipefail

APPCTL="${UCC_UNINSTALL_APPCTL:-$HOME/bin/appctl}"
APPS="${UCC_UNINSTALL_APPS_DIR:-$HOME/.apps}"
BACKUP_DIR="${UCC_UNINSTALL_BACKUP_DIR:-$HOME/.opt/maint/ucc-uninstall}"
LOG="${UCC_UNINSTALL_LOG:-$HOME/.opt/maint/ucc-uninstall.log}"
MANIFEST="${UCC_UNINSTALL_MANIFEST:-$HOME/.opt/maint/apps.yaml}"
LIB="${UCC_UNINSTALL_LIB:-$HOME/scripts/maint/lib}"
PY="${UCC_UNINSTALL_PYTHON:-python3}"
PROC="${UCC_UNINSTALL_PROC:-/proc}"
SETTLE_S="${UCC_UNINSTALL_SETTLE_S:-5}"
PROXY_D="$APPS/nginx/proxy.d"
# GNU tar reads "C:/x" as host:path; --force-local keeps archive names local.
TAR=(tar --force-local)

EXECUTE=0
WANT=""
for a in "$@"; do
  case "$a" in
    --execute) EXECUTE=1 ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    -*) echo "usage: ucc-uninstall.sh <app> [--execute]" >&2; exit 64 ;;
    *) [ -z "$WANT" ] || { echo "usage: ucc-uninstall.sh <app> [--execute]" >&2; exit 64; }
       WANT="$a" ;;
  esac
done
[ -n "$WANT" ] || { echo "usage: ucc-uninstall.sh <app> [--execute]" >&2; exit 64; }

MODE=$([ "$EXECUTE" = 1 ] && echo execute || echo dry-run)
log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) [$MODE] ${WANT}: $*"
  echo "$line"
  # Dry-run touches nothing, the log included.
  if [ "$EXECUTE" = 1 ]; then
    mkdir -p "$(dirname "$LOG")" 2>/dev/null && echo "$line" >> "$LOG"
  fi
}
refuse() { log "REFUSED: $*"; echo "ucc-uninstall: refused: $*" >&2; exit 2; }
fail()   { log "FAILED: $*";  echo "ucc-uninstall: FAILED: $*" >&2; exit 1; }
# Print in dry-run, run in execute. Only for steps with no output we parse.
act() {
  if [ "$EXECUTE" = 1 ]; then log "run: $*"; "$@"; else log "would run: $*"; fi
}

# Files that prove the native data dir survived, relative to ~/.apps/<native_dir>
# (a leading ~/ means $HOME). Taken from each 30x native installer's in-place
# data layout. No entry = refuse: we will not uninstall what we cannot verify.
key_files() {
  case "$1" in
    unpackerr)          echo "unpackerr.conf" ;;
    sonarr|sonarr2)     echo "config.xml sonarr.db" ;;      # sonarr2 keeps sonarr.db
    radarr|radarr2)     echo "config.xml radarr.db" ;;      # radarr2 keeps radarr.db
    prowlarr)           echo "config.xml prowlarr.db" ;;
    bazarr)             echo "config/config.yaml db/bazarr.db" ;;
    sabnzbd)            echo "sabnzbd.ini" ;;
    tautulli)           echo "config.ini tautulli.db" ;;
    qbittorrent)        echo "~/.config/qBittorrent/qBittorrent.conf" ;;
    postgres)           echo "data/PG_VERSION" ;;           # in native_dir pg-native
    *) return 1 ;;
  esac
}
key_path() { case "$1" in "~/"*) echo "$HOME/${1#\~/}" ;; *) echo "$APPS/$NDIR/$1" ;; esac; }

# ---------------------------------------------------------------------------
# Gates. All read-only, so they run in dry-run too: a dry-run that would be
# refused says so.
# ---------------------------------------------------------------------------
log "start ($MODE)"

if [ "$EXECUTE" = 1 ]; then
  # Host maintenance window (Ultra: Monday 11:00-15:00 UTC): the panel and
  # containers are mid-flight; an uninstall racing that is how a half-removed
  # app is born. The window is policy, never re-implemented here (C-11):
  # hostpolicy in-window exits 0 inside, 1 outside; anything else fails closed.
  "$PY" "$LIB/hostpolicy.py" in-window >/dev/null 2>&1; wrc=$?
  [ "$wrc" -eq 1 ] || refuse "inside the host maintenance window (or window unresolvable, rc=$wrc)"
fi

row="$("$APPCTL" lookup "$WANT" 2>&1)" || refuse "appctl lookup failed: ${row:0:200}"
IFS='|' read -r NAME CLASS SLUG UNIT DORMANT _PSEC SWAPSTATE NDIR <<<"${row%$'\r'}"
NDIR="${NDIR:-$SLUG}"
log "resolved: name=$NAME class=$CLASS slug=$SLUG unit=$UNIT dormant=$DORMANT swap_state=${SWAPSTATE:-none} native_dir=$NDIR"

if [ "$NAME" = seerr ] || [ "$SLUG" = seerr ]; then
  refuse "seerr's member portal vhost seerr-<slot> is owned by the UCC seerr app; uninstalling it drops the route. Needs a replacement route first (QFLX-56)."
fi
[ "$CLASS" = systemd ] || refuse "class is '$CLASS', not systemd: the UCC container is still the live runtime"
[ "$DORMANT" = 1 ]     || refuse "ucc_dormant is not set: the swap is not recorded as done"
[ "$SWAPSTATE" != pending-swap ] || refuse "swap_state is pending-swap: the UCC container is still the live runtime"
[ -n "$UNIT" ]         || refuse "class systemd without a unit"
st="$(systemctl --user is-active "$UNIT" 2>/dev/null)"
[ "${st%$'\r'}" = active ] || refuse "native unit $UNIT is not active (${st:-no answer}); fix the native side first"
KEYS="$(key_files "$SLUG")" || refuse "no key-file table entry for $SLUG; add one to key_files() before uninstalling"
[ -d "$APPS/$SLUG" ] || refuse "$APPS/$SLUG missing; nothing to back up"
command -v "app-$SLUG" >/dev/null 2>&1 || refuse "app-$SLUG not installed (already uninstalled?)"
for k in $KEYS; do
  [ -e "$(key_path "$k")" ] || refuse "key file $(key_path "$k") missing BEFORE uninstall; parity is not proven"
done

pid_before="$(systemctl --user show -p MainPID --value "$UNIT" 2>/dev/null | tr -d '\r[:space:]')"
log "gates passed; $UNIT MainPID=${pid_before:-?}"

# ---------------------------------------------------------------------------
# The one uninstall call. --keep-config is a literal, not a parameter.
# ---------------------------------------------------------------------------
uninstall_ucc() {
  act "app-$SLUG" uninstall --keep-config </dev/null
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TGZ="$BACKUP_DIR/$SLUG-$TS.tgz"
PROXY="$PROXY_D/$SLUG.conf"
PROXY_SNAP="$BACKUP_DIR/$SLUG-$TS.proxy.conf"

# Step 1: backup. Same excludes as the swap snapshot (307 Step 5): binaries are
# re-downloadable, logs and caches are bulk with no restore value.
act mkdir -p "$BACKUP_DIR" || fail "cannot create $BACKUP_DIR"
act "${TAR[@]}" -czf "$TGZ" -C "$APPS" --exclude="$SLUG/bin" --exclude="$SLUG/native" \
    --exclude="$SLUG/logs" --exclude="$SLUG/log" --exclude="$SLUG/logs.db*" \
    --exclude="$SLUG/MediaCover" --exclude="$SLUG/Backups" --exclude="$SLUG/cache" \
    --exclude="$SLUG/*.prew-rectify" "$SLUG" || fail "backup tar failed"
if [ "$EXECUTE" = 1 ]; then
  # A tarball that does not list is not a backup. When the native data lives in
  # the slug dir, its key files must be IN the listing too.
  listing="$("${TAR[@]}" -tzf "$TGZ" 2>/dev/null)" || fail "backup $TGZ does not list"
  printf '%s\n' "$listing" | grep -q "^$SLUG/" || fail "backup $TGZ lists no $SLUG/ entries"
  if [ "$NDIR" = "$SLUG" ]; then
    for k in $KEYS; do
      case "$k" in "~/"*) continue ;; esac
      printf '%s\n' "$listing" | grep -qxF "$SLUG/$k" || fail "backup $TGZ is missing $SLUG/$k"
    done
  fi
  log "backup verified: $TGZ ($(printf '%s\n' "$listing" | wc -l | tr -d ' ') entries)"
fi

# Step 2: proxy conf snapshot (the panel uninstall may take the route with it).
if [ -f "$PROXY" ]; then
  act cp -p "$PROXY" "$PROXY_SNAP" || fail "cannot snapshot $PROXY"
else
  log "no $PROXY (app has no proxy route)"
fi

# Step 3: uninstall, keeping config.
uninstall_ucc || fail "app-$SLUG uninstall --keep-config exited non-zero; inspect before retrying"

[ "$EXECUTE" = 1 ] || { log "dry-run complete; re-run with --execute"; exit 0; }

# ---------------------------------------------------------------------------
# Post-checks. Collect every failure, report them all, never reinstall.
# ---------------------------------------------------------------------------
FAILS=()
st="$(systemctl --user is-active "$UNIT" 2>/dev/null)"
[ "${st%$'\r'}" = active ] || FAILS+=("native unit $UNIT is ${st:-unknown} after uninstall")
pid_after="$(systemctl --user show -p MainPID --value "$UNIT" 2>/dev/null | tr -d '\r[:space:]')"
if [ -n "$pid_before" ] && [ "$pid_after" != "$pid_before" ]; then
  # Not fatal on its own (unit is still active) but the uninstall touched it.
  log "WARN: $UNIT MainPID changed $pid_before -> ${pid_after:-none}"
fi
for k in $KEYS; do
  [ -e "$(key_path "$k")" ] || FAILS+=("data file $(key_path "$k") is GONE; restore from $TGZ")
done

if [ -f "$PROXY_SNAP" ] && [ ! -f "$PROXY" ]; then
  log "proxy conf $PROXY vanished; restoring snapshot"
  if cp -p "$PROXY_SNAP" "$PROXY" && "$APPCTL" proxy-reload; then
    FAILS+=("proxy conf $PROXY vanished (restored from snapshot + proxy-reload ok; verify the route answers)")
  else
    FAILS+=("proxy conf $PROXY vanished and restore/proxy-reload FAILED; snapshot at $PROXY_SNAP")
  fi
fi

# No container processes may remain. Reuses runtime_parity.woken_container (the
# same cgroup+cmdline scan the canary runs), polled briefly: the panel's stop is
# asynchronous.
container_scan() {
  "$PY" - "$LIB" "$MANIFEST" "$NAME" "$PROC" "${UCC_UNINSTALL_UID:-}" <<'PY'
import os, sys
from types import SimpleNamespace
lib, manifest, name, proc, uid = sys.argv[1:6]
sys.path.insert(0, os.path.dirname(lib))
import yaml
from lib import runtime_parity as rp
with open(manifest, encoding="utf-8") as fh:
    a = (yaml.safe_load(fh) or {})["apps"][name]
app = SimpleNamespace(raw=a, health=SimpleNamespace(raw=a.get("health") or {}))
hits = rp.woken_container(rp.Host(proc), app, int(uid) if uid else os.getuid())
print("\n".join(hits))
sys.exit(1 if hits else 0)
PY
}
scan=""
for _ in 1 2 3; do
  scan="$(container_scan 2>&1)"; rc=$?
  [ "$rc" -eq 0 ] && { scan=""; break; }
  [ "$rc" -eq 1 ] || { scan="container scan errored: ${scan:0:200}"; break; }
  sleep "$SETTLE_S"
done
[ -z "$scan" ] || FAILS+=("${scan%$'\r'}")

if [ "${#FAILS[@]}" -gt 0 ]; then
  for f in "${FAILS[@]}"; do log "POST-CHECK FAILED: $f"; done
  echo "ucc-uninstall: ${#FAILS[@]} post-check(s) failed for $NAME; NOT reinstalling. Backup: $TGZ" >&2
  exit 1
fi
log "done: app-$SLUG uninstalled, $UNIT active, data intact, no container processes. Backup: $TGZ"
exit 0
