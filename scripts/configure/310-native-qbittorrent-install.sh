#!/usr/bin/env bash
# 310-native-qbittorrent-install.sh -- QFLX-35 (UCC divorce A11, ADOPT qbittorrent).
#
# qbittorrent is the one "UCC" app that never ran in a container: the Ultra.cc
# panel runs it as the user unit `qbittorrent.service` with ExecStart
# %h/bin/qbittorrent-nox (a symlink into the panel's /opt/qbittorrent-<ver>/).
# The panel owns that unit file and its Upgrade & Repair may rewrite it, so we
# ADOPT the app onto a unit the repo owns (review O-7):
#
#   * the static userdocs qbittorrent-nox at EXACTLY the running version, pinned
#     by sha256 (the panel's own binary IS that userdocs build: the installer
#     refuses unless the running binary hashes to the same pin), laid out in
#     ~/.apps/qbittorrent/bin/<ver> + `current`;
#   * the tracked unit qflix-qbittorrent.service (a NEW name; the panel unit is
#     stopped + disabled, backed up, NEVER uninstalled: it is the rollback target);
#   * the SAME profile (~/.config/qBittorrent + ~/.local/share/qBittorrent, used in
#     place, never moved or regenerated) and therefore the SAME listen set.
# Spec: docs/superpowers/specs/2026-10-09-ucc-divorce-design.md 5.1-5.9, row 11
# of section 6. Same shape as the pilot, 300-native-unpackerr-install.sh.
#
# RUNS ON THE BOX. 240-maintenance-install.sh deploys it to ~/scripts/configure/
# with ~/scripts/lib/native.sh beside it. Started from the workstation it
# re-executes the deployed copy over ssh (scripts/lib/ssh.sh).
#
# INERT BY DEFAULT (I-3). Every mode prints its plan (DRY-RUN) and touches
# nothing unless --execute is also given. Modes, in swap order:
#
#   --install   5.9 step 1. Fetch + sha256-verify the static build, refuse unless
#               the version equals `appctl version qbittorrent` (I-10) AND the
#               running panel binary hashes to the same pin, lay out bin/<ver> +
#               `current`, write the env file and STAGE the unit in
#               ~/.apps/qbittorrent/native/. NOT copied into ~/.config/systemd/user
#               and NOT enabled (WantedBy=default.target would start a second
#               engine on the live profile at the next user-manager start, I-6).
#   --prove     5.9 step 2. `--version` must print the pin; then a SECOND engine
#               on a SCRATCH --profile (and scratch HOME/TMPDIR) under
#               ~/.apps/.prove/qbittorrent, WebUI + BitTorrent port on fresh
#               127.0.0.1 ports, DHT/PeX/LSD/port-forwarding off, no torrents.
#               GET /api/v2/app/version must answer v<pin>. Its task count is
#               gated at 70% of the host task ceiling (G-2). Never touches the
#               live profile or ports. Writes swap/qbittorrent/proof.json.
#   --swap      5.9 steps 3-6: needs the proof and the DEPLOYED pending-swap
#               manifest flip. Unmask (rollback step 4); parity (appctl version,
#               panel binary sha256, API version); capture the WebUI listen set
#               (swapstate, audited by runtime parity) and the BitTorrent listen
#               rows; path-audit qBittorrent.conf for container paths; assert +
#               record local-auth bypass OFF (WebUI\LocalHostAuth=true, subnet
#               whitelist off; G-1); suppress qbittorrent + canary-qbit-stall +
#               canary-thread-ceiling; back up the panel unit file; PAUSE the
#               active torrents via the API (the exact set is recorded so only
#               those resume: ratio-paused torrents stay paused); `disable --now`
#               the panel unit; POLL until no panel process is left and both
#               ports are free (abort + re-enable the panel otherwise); enable
#               --now the tracked unit; wait out the WebUI boot-bind race
#               (memory qbit-webui-boot-bind-race: the WebUI binds ONCE at boot,
#               so a not-answering WebUI gets a restart per backoff step, 30/90/
#               180s like the manifest recovery_backoff_s); verify parity;
#               record swap state (14-day soak); resume the recorded set.
#               Suppression stays ON: the manifest still says pending-swap.
#   --finish    5.9 step 9, after the follow-up PR dropped `swap_state` and 240
#               deployed it: verify, then lift every suppression together.
#   --rollback  Rollback 0-5. 0: re-suppress, pause the active set, park the
#               tracked unit and MASK it (without the mask pusher recovery
#               restarts even a disabled unit: two engines on one profile, I-6).
#               1: stop it, wait for exit AND both ports free. 2: the DEPLOYED
#               manifest must dispatch qbittorrent as UCC again (otherwise exit
#               10; re-run after the revert PR + 240). 3: restore the backed-up
#               panel unit file if it is gone, `enable --now` the panel unit,
#               wait for its WebUI, resume the recorded set. 5: nothing to
#               restore (same binary, same profile, no migration).
#
# The panel CLI tool is never called: the panel unit is driven
# with systemctl directly, and the version comes through ~/bin/appctl.
#
# Exit: 0 ok | 1 refused/failed | 10 rollback paused for the manifest revert |
# 64 usage.
#
# Overrides (tests; resolved at call time): QFLIX_APPS_DIR QFLIX_UNIT_DIR
# QFLIX_ENV_DIR QFLIX_SWAP_DIR MANITOBA_STATE_DIR QFLIX_MANIFEST QFLIX_PROC
# QFLIX_PYTHON QFLIX_APPCTL QFLIX_SYSTEMCTL QFLIX_SS QFLIX_PS QFLIX_CURL
# (download) QFLIX_HTTP_CURL (prove) QFLIX_PROBE_CURL (live WebUI probe)
# QFLIX_HOSTPOLICY QFLIX_HOST_ID_FILE QFLIX_QBITTORRENT_SHA256 QFLIX_SECRETS_DIR
# QFLIX_QBIT_CONF QFLIX_QBIT_DATA QFLIX_QBIT_PANEL_BIN QFLIX_QBIT_URL
# QFLIX_BIND_BACKOFF_S QFLIX_POLL_S QFLIX_SETTLE_S QFLIX_STOP_TIMEOUT_S
# QFLIX_PROOF_TIMEOUT_S QFLIX_KEEP_PROOF.
set -uo pipefail

SLUG=qbittorrent
VERSION="5.0.3"              # == versions.env QBITTORRENT_VERSION (test-pinned)
LIBTORRENT="1.2.19"          # the running build's libtorrent (strings, box 2026-10-10)
SHA256="0c6be8354f7d0ef4971e1a1abba2bf667b1aa5e6a8670d831b03910fafe8df92"
URL="https://github.com/userdocs/qbittorrent-nox-static/releases/download/release-${VERSION}_v${LIBTORRENT}/x86_64-qbittorrent-nox"
UNIT="qflix-${SLUG}.service"
PANEL_UNIT="${SLUG}.service" # the panel's unit: stopped + disabled, never removed
FAMILY=static
EXE=qbittorrent-nox
# No args: the default profile (~/.config/qBittorrent, ~/.local/share/qBittorrent)
# IS the live one, used in place.
EXEC_ARGS=""
# Muted with the app (plan row A11; thread-ceiling is named for every A-ticket).
# Keys follow cli.py: canary-<name>.
SUPPRESS=("$SLUG" "canary-qbit-stall" "canary-thread-ceiling")
PATTERN="qbittorrent-nox"    # cmdline of both the panel and the tracked engine

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # .../scripts
ARGS=("$@")

info() { echo "[310-qbittorrent] $*"; }
die()  { echo "[310-qbittorrent] ERROR: $*" >&2; exit 1; }
usage() {
  echo "usage: $0 [--install|--prove|--swap|--finish|--rollback] [--execute]" >&2
  exit 64
}

MODE=install
EXECUTE=0
for a in "$@"; do
  case "$a" in
    --install|--prove|--swap|--finish|--rollback) MODE="${a#--}" ;;
    --execute) EXECUTE=1 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

APPS="${QFLIX_APPS_DIR:-$HOME/.apps}"
APPDIR="$APPS/$SLUG"
UNIT_DIR="${QFLIX_UNIT_DIR:-$HOME/.config/systemd/user}"
ENV_DIR="${QFLIX_ENV_DIR:-$HOME/.config/qflix}"
SWAPDIR="${QFLIX_SWAP_DIR:-$HOME/.opt/maint/swap}/$SLUG"
PROVE="$APPS/.prove/$SLUG"
MANIFEST="${QFLIX_MANIFEST:-$HOME/.opt/maint/apps.yaml}"
SECRETS="${QFLIX_SECRETS_DIR:-$HOME/secrets}"
CONF="${QFLIX_QBIT_CONF:-$HOME/.config/qBittorrent/qBittorrent.conf}"
DATA_DIR="${QFLIX_QBIT_DATA:-$HOME/.local/share/qBittorrent}"
PANEL_BIN="${QFLIX_QBIT_PANEL_BIN:-$HOME/bin/qbittorrent-nox}"
MAINT_LIB="$HERE/maint/lib"
PY="${QFLIX_PYTHON:-python3}"
APPCTL="${QFLIX_APPCTL:-$HOME/bin/appctl}"
SYSTEMCTL="${QFLIX_SYSTEMCTL:-systemctl}"
SS="${QFLIX_SS:-ss}"
PS="${QFLIX_PS:-ps}"
HTTP_CURL="${QFLIX_HTTP_CURL:-curl}"
PROBE_CURL="${QFLIX_PROBE_CURL:-curl}"
PROC="${QFLIX_PROC:-/proc}"
POLL="${QFLIX_POLL_S:-2}"
SETTLE="${QFLIX_SETTLE_S:-10}"
STOP_TIMEOUT="${QFLIX_STOP_TIMEOUT_S:-180}"
PROOF_TIMEOUT="${QFLIX_PROOF_TIMEOUT_S:-120}"
# WebUI boot-bind race: wait this long, then restart, per step (manifest
# recovery_backoff_s for qbittorrent is the same 30/90/180).
read -r -a BIND_BACKOFF <<<"${QFLIX_BIND_BACKOFF_S:-30 90 180}"
WANT_SHA="${QFLIX_QBITTORRENT_SHA256:-$SHA256}"
QUIESCED="$SWAPDIR/quiesced.hashes"

hostpolicy() {
  if [ -n "${QFLIX_HOSTPOLICY:-}" ]; then "$QFLIX_HOSTPOLICY" "$@"
  else "$PY" "$MAINT_LIB/hostpolicy.py" "$@"; fi
}
swapstate()   { "$PY" "$MAINT_LIB/swapstate.py" "$@"; }
suppression() { "$PY" "$MAINT_LIB/suppression.py" "$@"; }
sysd()        { "$SYSTEMCTL" --user "$@"; }

# --- plan (DRY-RUN) ------------------------------------------------------------
plan() {
  info "DRY-RUN mode=$MODE (nothing touched; add --execute to run)"
  case "$MODE" in
    install)  info "would fetch $URL (sha256 $WANT_SHA), check version parity + the running panel binary's sha256, lay out $APPDIR/bin/$VERSION, write $ENV_DIR/$SLUG.env, stage $APPDIR/native/$UNIT (not enabled)" ;;
    prove)    info "would check --version, boot a second engine on a scratch --profile in $PROVE on fresh 127.0.0.1 ports (no torrents), GET /api/v2/app/version, gate the task count at 70% of the ceiling, then delete the copy" ;;
    swap)     info "would capture the listen sets, audit paths + local auth, suppress ${SUPPRESS[*]}, back up $PANEL_UNIT, pause the active torrents, disable --now $PANEL_UNIT, wait for exit, enable --now $UNIT, wait out the WebUI bind race, verify, record swap state, resume the paused set" ;;
    finish)   info "would verify $UNIT and lift suppression for ${SUPPRESS[*]}" ;;
    rollback) info "would suppress ${SUPPRESS[*]}, pause the active torrents, park + mask $UNIT, stop it, wait for the manifest revert, re-enable --now $PANEL_UNIT (restored from backup if gone), resume, unsuppress" ;;
  esac
  exit 0
}
[ "$EXECUTE" = 1 ] || plan

# --- run on the box --------------------------------------------------------------
_on_host() {
  local m="${QFLIX_HOST_ID_FILE:-$HOME/.config/qflix/host.id}"
  [ -s "$m" ] && return 0
  [ "$(hostname 2>/dev/null)" = "manitoba" ]
}
if ! _on_host; then
  # shellcheck source=/dev/null
  source "$HERE/lib/ssh.sh"
  info "not on the box: running the deployed copy there"
  sshm "~/scripts/configure/310-native-qbittorrent-install.sh $(printf '%q ' "${ARGS[@]}")"
  exit $?
fi

# shellcheck source=/dev/null
source "$HERE/lib/native.sh" || die "cannot source $HERE/lib/native.sh (run 240 first)"

PROFILE="$(hostpolicy preflight)" || die "host profile unresolved; refusing (I-12)"
PROFILE="${PROFILE%$'\r'}"
if hostpolicy in-window; then
  die "inside the Monday maintenance window; no box operations"
fi
case "$MODE" in
  swap|finish|rollback)
    [ "$PROFILE" = ultra ] || die "mode $MODE adopts the Ultra panel's qbittorrent; host profile is '$PROFILE'" ;;
esac

T0=$SECONDS

CLEANUP_PATHS=()
PROOF_PID=""
cleanup() {
  [ -n "$PROOF_PID" ] && { kill "$PROOF_PID" 2>/dev/null; wait "$PROOF_PID" 2>/dev/null; }
  local p
  for p in "${CLEANUP_PATHS[@]}"; do rm -rf "$p"; done
}
trap cleanup EXIT

# --- helpers -------------------------------------------------------------------------
# qBittorrent.conf value of KEY (keys carry a backslash, e.g. WebUI\Port, so the
# key goes through ENVIRON: awk -v would eat the escape).
conf_get() {
  K="$1=" awk 'index($0, ENVIRON["K"]) == 1 { print substr($0, length(ENVIRON["K"]) + 1); exit }' \
    "$CONF" 2>/dev/null | tr -d '\r'
}

# The WebUI port: the recorded secret, which must equal the live config (5.5).
webui_port() {
  local p c
  p="$(tr -d '[:space:]' < "$SECRETS/qbittorrent.port" 2>/dev/null)"
  [[ "$p" =~ ^[0-9]{2,5}$ ]] || { echo "secret qbittorrent.port missing/invalid" >&2; return 1; }
  c="$(conf_get 'WebUI\Port')"
  [ "$c" = "$p" ] || { echo "WebUI\\Port in $CONF is '$c', secret qbittorrent.port is '$p'" >&2; return 1; }
  printf '%s' "$p"
}

# The BitTorrent (peer) listen port from the live config.
bt_port() {
  local p
  p="$(conf_get 'Session\Port')"
  [ -n "$p" ] || p="$(conf_get 'Connection\PortRangeMin')"
  [[ "$p" =~ ^[0-9]{2,5}$ ]] || { echo "no BitTorrent port in $CONF" >&2; return 1; }
  printf '%s' "$p"
}

manifest_state() {
  "$PY" - "$MANIFEST" "$SLUG" <<'PY'
import sys
try:
    import yaml
    with open(sys.argv[1], encoding="utf-8") as fh:
        a = ((yaml.safe_load(fh) or {}).get("apps") or {}).get(sys.argv[2]) or {}
except Exception as exc:
    sys.stderr.write("manifest unreadable: %s\n" % exc)
    sys.exit(2)
print("|".join([str(a.get("class") or ""), str(a.get("swap_state") or ""),
                "0" if a.get("ucc_dormant") in (None, False) else "1"]))
PY
}

# PIDs under OUR uid whose cmdline carries qbittorrent-nox. kind=panel: in the
# panel unit's cgroup (.../qbittorrent.service). kind=native: in the tracked
# unit's (.../qflix-qbittorrent.service). The segment match keeps the two apart
# ("qbittorrent.service" is a substring of "qflix-qbittorrent.service").
scan_pids() {
  local kind="$1" uid d pid cg cmd re
  uid="$(id -u)"
  if [ "$kind" = native ]; then re="/qflix-qbittorrent\.service(/|$|[[:space:]])"
  else re="/qbittorrent\.service(/|$|[[:space:]])"; fi
  for d in "$PROC"/[0-9]*; do
    [ -d "$d" ] || continue
    pid="${d##*/}"
    [ "$(awk '/^Uid:/ {print $2; exit}' "$d/status" 2>/dev/null)" = "$uid" ] || continue
    cg="$(cat "$d/cgroup" 2>/dev/null)" || continue
    cmd="$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)" || continue
    case "$cmd" in *"$PATTERN"*) ;; *) continue ;; esac
    [[ "$cg" =~ $re ]] && echo "$pid"
  done
}

# LISTEN rows (local addr:port) on port $1, sorted.
listen_rows() {
  local p=":$1"
  "$SS" -tlnH "sport = $p" 2>/dev/null \
    | awk -v p="$p" '$1 == "LISTEN" && substr($4, length($4) - length(p) + 1) == p { print $4 }' \
    | sort -u
}
port_free() { [ -z "$(listen_rows "$1")" ]; }

wait_until() {
  local limit="$1"; shift
  local start=$SECONDS
  while :; do
    "$@" && return 0
    [ $((SECONDS - start)) -ge "$limit" ] && return 1
    sleep "$POLL"
  done
}
panel_up()   { [ -n "$(scan_pids panel)" ]; }
panel_gone() { ! panel_up && port_free "$WPORT" && port_free "$BPORT"; }
# A live panel engine legitimately holds the ports (nothing was adopted);
# otherwise the tracked engine's listeners must be gone before the panel starts.
native_gone() { [ -z "$(scan_pids native)" ] && { panel_up || { port_free "$WPORT" && port_free "$BPORT"; }; }; }

user_tasks() { "$PS" -u "$(id -u)" -L --no-headers 2>/dev/null | wc -l | tr -d ' '; }

# The live WebUI answers HTTP 200 on loopback (the manifest http_root probe).
probe_webui() {
  local code
  code="$("$PROBE_CURL" -s -o /dev/null -m 8 -w '%{http_code}' "http://127.0.0.1:$WPORT/" 2>/dev/null)"
  [ "${code%$'\r'}" = 200 ]
}

# WebUI boot-bind race (memory qbit-webui-boot-bind-race): qBit binds its WebUI
# ONCE at startup and never retries. A running engine with a silent WebUI gets
# restarted after each backoff step; the last step only waits.
webui_ready() {   # $1 = unit to restart between steps
  local i n=${#BIND_BACKOFF[@]}
  for ((i = 0; i < n; i++)); do
    wait_until "${BIND_BACKOFF[$i]}" probe_webui && return 0
    if [ $((i + 1)) -lt "$n" ]; then
      info "WebUI on :$WPORT not answering after ${BIND_BACKOFF[$i]}s (boot bind race?); restarting $1"
      sysd restart "$1" >/dev/null 2>&1 || true
      sleep "$SETTLE"
    fi
  done
  return 1
}

# qBittorrent WebUI API (login with the qbittorrent.user/.password secrets; the
# WebUI requires auth on localhost too, which is the point of the G-1 assert).
#   version         print the app version (v5.0.3)
#   active          print the hashes of every torrent that is NOT stopped/paused
#   stop|start F    stop/start exactly the hashes listed in file F (5.x verbs,
#                   falling back to the 4.x pause/resume on 404)
qbit_api() {
  QBIT_URL="${QFLIX_QBIT_URL:-http://127.0.0.1:$WPORT}" QBIT_SECRETS="$SECRETS" \
    "$PY" - "$@" <<'PY'
import json, os, sys, urllib.error, urllib.parse, urllib.request
base = os.environ["QBIT_URL"].rstrip("/")
sec = os.environ["QBIT_SECRETS"]
def rd(n):
    with open(os.path.join(sec, n), encoding="utf-8") as fh:
        return fh.read().strip()
try:
    user, pw = rd("qbittorrent.user"), rd("qbittorrent.password")
except OSError as exc:
    sys.exit("qbit api: credentials unreadable: %s" % exc)
def call(path, data=None, sid=None):
    hdr = {"Referer": base}
    if sid:
        hdr["Cookie"] = "SID=" + sid
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    return urllib.request.urlopen(urllib.request.Request(base + path, data=body, headers=hdr), timeout=30)
try:
    r = call("/api/v2/auth/login", {"username": user, "password": pw})
    ok = r.read().decode().strip()
    sid = next((p.split("=", 1)[1] for p in (r.headers.get("Set-Cookie") or "").split(";")
                if p.strip().startswith("SID=")), "")
except (urllib.error.URLError, OSError) as exc:
    sys.exit("qbit api: login failed: %s" % exc)
if ok != "Ok." or not sid:
    sys.exit("qbit api: login refused (%s)" % ok[:40])
verb = sys.argv[1]
try:
    if verb == "version":
        print(call("/api/v2/app/version", sid=sid).read().decode().strip())
    elif verb == "active":
        rows = json.loads(call("/api/v2/torrents/info", sid=sid).read().decode())
        stopped = ("stoppedUP", "stoppedDL", "pausedUP", "pausedDL")
        for t in rows:
            if t.get("state") not in stopped and t.get("hash"):
                print(t["hash"])
    elif verb in ("stop", "start"):
        with open(sys.argv[2], encoding="utf-8") as fh:
            hashes = [h.strip() for h in fh if h.strip()]
        if hashes:
            old = {"stop": "pause", "start": "resume"}[verb]
            data = {"hashes": "|".join(hashes)}
            try:
                call("/api/v2/torrents/" + verb, data, sid).read()
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                call("/api/v2/torrents/" + old, data, sid).read()
        print(len(hashes))
    else:
        sys.exit("qbit api: unknown verb %s" % verb)
except (urllib.error.URLError, OSError, ValueError) as exc:
    sys.exit("qbit api: %s failed: %s" % (verb, exc))
PY
}

# Pause exactly the torrents that are running now; the set is MERGED into the
# record so a re-run never forgets torrents an earlier attempt paused.
quiesce() {
  local cur n
  mkdir -p "$SWAPDIR"
  cur="$(qbit_api active)" || return 1
  { [ -f "$QUIESCED" ] && cat "$QUIESCED"; printf '%s\n' "$cur"; } | sed '/^$/d' | sort -u \
    | native_write_secure "$QUIESCED" 0600 || return 1
  n="$(qbit_api stop "$QUIESCED")" || return 1
  info "paused ${n%$'\r'} active torrent(s) (recorded in $QUIESCED)"
}
# Resume the recorded set, then forget it. No record = nothing to do.
unquiesce() {
  local n
  [ -f "$QUIESCED" ] || return 0
  n="$(qbit_api start "$QUIESCED")" || return 1
  rm -f "$QUIESCED"
  info "resumed ${n%$'\r'} torrent(s)"
}

# The running panel binary must be the very build we pin (same bytes).
panel_sha_ok() {
  local got
  [ -e "$PANEL_BIN" ] || { echo "running panel binary $PANEL_BIN missing" >&2; return 1; }
  got="$(sha256sum "$PANEL_BIN" 2>/dev/null | cut -d' ' -f1)"
  [ "$got" = "$WANT_SHA" ] || { echo "panel binary $PANEL_BIN sha256 $got != pinned $WANT_SHA" >&2; return 1; }
}

# Container paths (/config, /data, /downloads) mean nothing on the host (5.9
# step 3). Matched as a VALUE prefix, so /home/<u>/downloads/... passes.
path_audit() {
  local hits
  hits="$(grep -nE '=(/config|/data|/downloads)(/|$)' "$CONF" || true)"
  [ -z "$hits" ] || die "container path(s) in $CONF: $(echo "$hits" | head -3 | tr '\n' ' ')"
}

# G-1: localhost must NOT bypass WebUI auth (qBit "bypass_local_auth=false" is
# WebUI\LocalHostAuth=true; absent = qBit default = true), and the subnet
# whitelist must be off. Recorded in swap state.
auth_audit() {
  local lha wl
  lha="$(conf_get 'WebUI\LocalHostAuth')"
  wl="$(conf_get 'WebUI\AuthSubnetWhitelistEnabled')"
  [ "${lha:-true}" = true ] || die "WebUI\\LocalHostAuth=$lha: localhost bypasses auth (bypass_local_auth must be false; G-1)"
  [ "${wl:-false}" = false ] || die "WebUI\\AuthSubnetWhitelistEnabled=$wl: a subnet bypasses auth (G-1)"
  printf 'bypass_local_auth=false\nauth_subnet_whitelist=false\nrecorded_at=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | native_write_secure "$SWAPDIR/local-auth" 0644 \
    || die "cannot record the local-auth assertion"
}

is_masked() {
  local f="$UNIT_DIR/$UNIT"
  [ -L "$f" ] || { [ -f "$f" ] && [ ! -s "$f" ]; }
}

mask_unit() {
  if is_masked; then return 0; fi
  if [ -f "$UNIT_DIR/$UNIT" ]; then
    sysd disable "$UNIT" >/dev/null 2>&1 || true
    mkdir -p "$ENV_DIR/parked-units"
    mv -f "$UNIT_DIR/$UNIT" "$ENV_DIR/parked-units/$UNIT" || die "cannot park $UNIT"
  fi
  sysd mask "$UNIT" || die "systemctl --user mask $UNIT failed"
  is_masked || die "$UNIT not masked after mask"
  sysd daemon-reload || true
}

# --- modes ----------------------------------------------------------------------------
do_install() {
  local stage
  [ -f "$CONF" ] || die "$CONF missing (the live profile is used in place)"
  panel_sha_ok || die "the running build is not the pinned userdocs build; refusing (I-10)"
  mkdir -p "$APPS" || die "cannot create $APPS"
  stage="$(mktemp -d "$APPS/.stage-$SLUG.XXXXXX")" || die "mktemp failed"
  CLEANUP_PATHS+=("$stage")
  native_fetch_verify "$URL" "$WANT_SHA" "$stage/$EXE" || die "fetch/sha256 verify failed"
  chmod 0755 "$stage/$EXE"
  native_install_versioned "$SLUG" "$VERSION" "$stage/$EXE" || die "install refused (see above)"
  # The panel unit's environment, carried over (TMPDIR made absolute: an
  # EnvironmentFile does not expand %h). PrivateTmp is not carried: TMPDIR points
  # qBit at its own data dir already.
  native_render_env "$SLUG" "$FAMILY" "$VERSION" "TMPDIR=$DATA_DIR" "QT_BEARER_POLL_TIMEOUT=-1" \
    | native_write_secure "$ENV_DIR/$SLUG.env" 0600 || die "env file write failed"
  native_render_unit "$SLUG" "$FAMILY" "$EXE" "$EXEC_ARGS" | sed 's/[ \t]*$//' \
    | native_write_secure "$APPDIR/native/$UNIT" 0644 || die "unit staging failed"
  info "installed $VERSION; unit staged at $APPDIR/native/$UNIT (not enabled). Next: --prove --execute"
}

do_prove() {
  local before ceiling delta webport btport ver out start
  [ -x "$APPDIR/bin/current/$EXE" ] || die "not installed; run --install --execute first"
  out="$("$APPDIR/bin/current/$EXE" --version 2>&1 | head -1 | tr -d '\r')"
  [ "$out" = "qBittorrent v$VERSION" ] || die "proof: --version printed '$out', want 'qBittorrent v$VERSION'"
  ceiling="$(hostpolicy task-ceiling)" || die "task ceiling unknown; refusing (G-2)"
  ceiling="${ceiling%$'\r'}"
  rm -rf "$PROVE"
  mkdir -p "$PROVE/profile/qBittorrent/config" "$PROVE/downloads" "$PROVE/home" "$PROVE/tmp" \
    || die "cannot create $PROVE"
  [ "${QFLIX_KEEP_PROOF:-0}" = 1 ] || CLEANUP_PATHS+=("$PROVE")
  read -r webport btport < <("$PY" -c 'import socket
a,b=socket.socket(),socket.socket()
a.bind(("127.0.0.1",0)); b.bind(("127.0.0.1",0))
print(a.getsockname()[1], b.getsockname()[1])' | tr -d '\r')
  [ -n "$webport" ] && [ -n "$btport" ] || die "cannot pick free loopback ports"
  # Never two engines on one profile: a scratch profile, loopback-only, no
  # torrents, nothing that announces or maps a port.
  cat >"$PROVE/profile/qBittorrent/config/qBittorrent.conf" <<EOF
[LegalNotice]
Accepted=true

[BitTorrent]
Session\\Port=$btport
Session\\InterfaceAddress=127.0.0.1
Session\\DHTEnabled=false
Session\\PeXEnabled=false
Session\\LSDEnabled=false
Session\\DefaultSavePath=$PROVE/downloads

[Network]
PortForwardingEnabled=false

[Preferences]
WebUI\\Address=127.0.0.1
WebUI\\Port=$webport
WebUI\\LocalHostAuth=false
WebUI\\UseUPnP=false
General\\UseRandomPort=false
EOF
  before="$(user_tasks)"
  [[ "$before" =~ ^[0-9]+$ ]] || die "cannot count tasks"
  ( cd "$PROVE" && exec env HOME="$PROVE/home" TMPDIR="$PROVE/tmp" MALLOC_ARENA_MAX=2 \
      "$APPDIR/bin/current/$EXE" --profile="$PROVE/profile" --webui-port="$webport" \
      >"$PROVE/stdout.log" 2>&1 ) &
  PROOF_PID=$!
  start=$SECONDS
  ready() {
    ver="$("$HTTP_CURL" -s -m 5 "http://127.0.0.1:$webport/api/v2/app/version" 2>/dev/null | tr -d '\r')"
    [ "$ver" = "v$VERSION" ]
  }
  if ! wait_until "$PROOF_TIMEOUT" ready; then
    tail -5 "$PROVE/stdout.log" >&2
    die "proof: the scratch engine never answered v$VERSION on 127.0.0.1:$webport within ${PROOF_TIMEOUT}s (got '${ver:-}')"
  fi
  delta="$(ls "$PROC/$PROOF_PID/task" 2>/dev/null | wc -l | tr -d ' ')"
  kill "$PROOF_PID" 2>/dev/null; wait "$PROOF_PID" 2>/dev/null; PROOF_PID=""
  [ "${delta:-0}" -gt 0 ] || die "proof: cannot read the task count; refusing"
  if [ $(( (before + delta) * 100 )) -ge $(( 70 * ceiling )) ]; then
    die "thread gate: $before + $delta tasks reaches 70% of the ceiling $ceiling; refusing the swap"
  fi
  mkdir -p "$SWAPDIR"
  printf '{"ok": true, "version": "%s", "before": %s, "delta": %s, "ceiling": %s, "at": "%s"}\n' \
    "$VERSION" "$before" "$delta" "$ceiling" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    | native_write_secure "$SWAPDIR/proof.json" 0644 || die "cannot record the proof"
  info "PROOF OK: scratch engine answered v$VERSION; before=$before delta=$delta ceiling=$ceiling elapsed=$((SECONDS - start))s. Next: --swap --execute"
}

verify_native() {
  local n
  sleep "$SETTLE"
  sysd is-active "$UNIT" >/dev/null 2>&1 || { echo "unit not active" >&2; return 1; }
  [ -z "$(scan_pids panel)" ] || { echo "a panel qbittorrent-nox is running (two engines, one profile)" >&2; return 1; }
  n="$(scan_pids native | wc -l | tr -d ' ')"
  [ "$n" -ge 1 ] || { echo "no native process" >&2; return 1; }
  webui_ready "$UNIT" || { echo "the WebUI on 127.0.0.1:$WPORT never answered 200 (boot bind race outlasted every restart)" >&2; return 1; }
  native_listen_compare "$SLUG" >/dev/null || { echo "WebUI listen set differs from listen-set.before" >&2; return 1; }
  [ "$(listen_rows "$BPORT")" = "$(cat "$SWAPDIR/bt-listen.before" 2>/dev/null)" ] \
    || { echo "BitTorrent listen rows on :$BPORT differ from bt-listen.before" >&2; return 1; }
}

do_swap() {
  local st cls ss_state ver api now soak
  [ -f "$SWAPDIR/proof.json" ] || die "no proof recorded; run --prove --execute first"
  [ -x "$APPDIR/bin/current/$EXE" ] && [ -f "$APPDIR/native/$UNIT" ] && [ -f "$ENV_DIR/$SLUG.env" ] \
    || die "not installed; run --install --execute first"
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state _ <<<"$st"
  [ "$cls" = systemd ] && [ "$ss_state" = pending-swap ] \
    || die "the deployed manifest is not the pending-swap flip (class=$cls swap_state=$ss_state); merge + deploy it via 240 first"

  if sysd is-active "$UNIT" >/dev/null 2>&1 && ! panel_up; then
    info "already swapped; verifying only"
    verify_native || die "native qbittorrent fails parity; run --rollback --execute"
    unquiesce || die "cannot resume the torrents recorded in $QUIESCED; re-run --swap --execute"
    info "verified"
    return 0
  fi

  # Rollback step 4: unmask only at the next forward swap.
  if is_masked; then
    sysd unmask "$UNIT" || die "unmask $UNIT failed"
    rm -f "$ENV_DIR/parked-units/$UNIT"
  fi

  # Step 3: parity, capture, audits. Nothing is muted or stopped yet.
  panel_up || die "the panel $PANEL_UNIT is not running; nothing to adopt (start it, then re-run)"
  ver="$(native_ucc_version "$SLUG")"
  [ "${ver#v}" = "$VERSION" ] || die "version parity: panel=$ver native=$VERSION"
  panel_sha_ok || die "the running build is not the pinned userdocs build; refusing (I-10)"
  api="$(qbit_api version)" || die "the WebUI API refused (credentials?); refusing before muting anything"
  [ "${api%$'\r'}" = "v$VERSION" ] || die "API version '$api' != v$VERSION"
  native_listen_capture "$SLUG" "$WPORT" >/dev/null || die "listen-set capture failed"
  [ -s "$SWAPDIR/listen-set.before" ] || die "nothing listens on the WebUI port :$WPORT; refusing"
  listen_rows "$BPORT" | native_write_secure "$SWAPDIR/bt-listen.before" 0644 \
    || die "cannot record the BitTorrent listen rows"
  [ -s "$SWAPDIR/bt-listen.before" ] || die "nothing listens on the BitTorrent port :$BPORT; refusing"
  swapstate set "$SLUG" "ucc_version=$VERSION" >/dev/null || die "cannot record ucc_version"
  path_audit
  auth_audit

  # Step 4: suppress the app and its canaries together.
  suppression add "${SUPPRESS[@]}" --reason "QFLX-35 adopt qbittorrent" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to swap unsuppressed"

  # Step 5: the panel unit file is the rollback target. The FIRST backup is
  # the original; a re-run never overwrites it.
  [ -f "$UNIT_DIR/$PANEL_UNIT" ] || { suppression remove "${SUPPRESS[@]}" >/dev/null; die "$UNIT_DIR/$PANEL_UNIT missing; refusing (no rollback target)"; }
  mkdir -p "$SWAPDIR/panel-unit"
  if [ ! -s "$SWAPDIR/panel-unit/$PANEL_UNIT" ]; then
    cp -p "$UNIT_DIR/$PANEL_UNIT" "$SWAPDIR/panel-unit/$PANEL_UNIT" \
      && cmp -s "$UNIT_DIR/$PANEL_UNIT" "$SWAPDIR/panel-unit/$PANEL_UNIT" \
      || { suppression remove "${SUPPRESS[@]}" >/dev/null; die "cannot back up $PANEL_UNIT"; }
  fi

  # Step 6: quiesce, then stop the panel engine. Its exit is polled: the STATE
  # decides, not the exit code.
  if ! quiesce; then
    unquiesce || true
    suppression remove "${SUPPRESS[@]}" >/dev/null || true
    die "cannot pause the active torrents; swap aborted before anything stopped"
  fi
  sysd disable --now "$PANEL_UNIT" >/dev/null 2>&1 || info "disable --now $PANEL_UNIT returned non-zero; polling decides"
  if ! wait_until "$STOP_TIMEOUT" panel_gone; then
    sysd enable --now "$PANEL_UNIT" >/dev/null 2>&1 || true
    wait_until 60 probe_webui || true
    unquiesce || true
    suppression remove "${SUPPRESS[@]}" >/dev/null || true
    die "the panel engine did not exit (or a port stayed bound) within ${STOP_TIMEOUT}s (pids: $(scan_pids panel | tr '\n' ' ')); swap aborted, panel unit re-enabled, torrents resumed, suppression lifted"
  fi
  mkdir -p "$UNIT_DIR"
  native_write_secure "$UNIT_DIR/$UNIT" 0644 < "$APPDIR/native/$UNIT" || die "unit install failed; run --rollback --execute"
  sysd daemon-reload || die "daemon-reload failed; run --rollback --execute"
  sysd enable --now "$UNIT" || die "enable --now $UNIT failed; run --rollback --execute"

  # Step 8 (in-place part): parity, including the WebUI bind race.
  if ! verify_native; then
    die "native qbittorrent fails parity after the swap; suppression kept ON, torrents left paused (recorded); run --rollback --execute"
  fi
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  soak="$(date -u -d '+14 days' +%Y-%m-%dT%H:%M:%SZ)" || die "cannot compute soak_until"
  swapstate set "$SLUG" "swap_date=$now" "soak_until=$soak" "rollback_window=open" >/dev/null \
    || die "cannot record swap state"
  unquiesce || die "swapped, but the torrents recorded in $QUIESCED did not resume; re-run --swap --execute"
  info "SWAPPED to $UNIT ($VERSION, same profile, WebUI :$WPORT); soak until $soak. elapsed=$((SECONDS - T0))s"
  info "Next: arr download-client test, PR dropping swap_state: pending-swap, deploy via 240, then --finish --execute"
}

do_finish() {
  local st cls ss_state dormant
  st="$(manifest_state)" || die "deployed manifest unreadable"
  IFS='|' read -r cls ss_state dormant <<<"$st"
  [ "$cls" = systemd ] && [ -z "$ss_state" ] && [ "$dormant" = 1 ] \
    || die "deployed manifest still says class=$cls swap_state=${ss_state:-none} dormant=$dormant; deploy the follow-up (no pending-swap) first"
  verify_native || die "native qbittorrent fails parity; run --rollback --execute"
  unquiesce || die "cannot resume the torrents recorded in $QUIESCED"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "FINISHED: ${SUPPRESS[*]} unsuppressed; 14-day soak running"
}

do_rollback() {
  local isn
  # Step 0: re-suppress, quiesce, mask BEFORE anything stops.
  suppression add "${SUPPRESS[@]}" --reason "QFLX-35 rollback to the panel unit" >/dev/null \
    || die "cannot suppress ${SUPPRESS[*]}; refusing to roll back unsuppressed"
  if [ -n "$(scan_pids native)" ]; then
    quiesce || info "WARN: could not pause torrents before stopping $UNIT (graceful SIGTERM still saves resume data)"
  fi
  mask_unit
  # Step 1: stop it; the panel engine needs both ports.
  sysd stop "$UNIT" >/dev/null 2>&1 || true
  wait_until "$STOP_TIMEOUT" native_gone || die "native qbittorrent did not stop (or a port stayed bound) within ${STOP_TIMEOUT}s"
  # Step 2: the DEPLOYED manifest must dispatch qbittorrent as UCC again.
  isn="$("$APPCTL" is-native "$SLUG" 2>/dev/null)"
  if [ "${isn%$'\r'}" != ucc ]; then
    echo "[310-qbittorrent] PAUSED: native stopped + masked; revert the deployed manifest (PR + 240) so appctl dispatches $SLUG as UCC, then re-run --rollback --execute" >&2
    exit 10
  fi
  # Step 3: the panel unit, restored from the backup if the file is gone.
  if [ ! -f "$UNIT_DIR/$PANEL_UNIT" ]; then
    [ -s "$SWAPDIR/panel-unit/$PANEL_UNIT" ] || die "$PANEL_UNIT is gone and no backup exists in $SWAPDIR/panel-unit; restore it by hand"
    cp -p "$SWAPDIR/panel-unit/$PANEL_UNIT" "$UNIT_DIR/$PANEL_UNIT" || die "cannot restore $PANEL_UNIT"
    info "restored $PANEL_UNIT from the swap backup"
  fi
  sysd daemon-reload || true
  sysd enable --now "$PANEL_UNIT" >/dev/null 2>&1 || info "enable --now $PANEL_UNIT returned non-zero; polling decides"
  wait_until "$STOP_TIMEOUT" panel_up || die "the panel engine did not come back within ${STOP_TIMEOUT}s; still suppressed"
  webui_ready "$PANEL_UNIT" || die "the panel engine is up but its WebUI on :$WPORT is not answering; still suppressed"
  # Step 5: nothing to restore (same binary, same profile).
  unquiesce || die "the panel is back but the torrents recorded in $QUIESCED did not resume; still suppressed"
  suppression remove "${SUPPRESS[@]}" >/dev/null || die "cannot lift suppression"
  info "ROLLED BACK to $PANEL_UNIT; $UNIT masked (unmasked by the next --swap). elapsed=$((SECONDS - T0))s"
}

# Every mode that touches the live engine resolves + validates both ports first.
case "$MODE" in
  swap|finish|rollback)
    WPORT="$(webui_port)" || die "WebUI port unresolved; refusing"
    BPORT="$(bt_port)" || die "BitTorrent port unresolved; refusing" ;;
esac

case "$MODE" in
  install)  do_install ;;
  prove)    do_prove ;;
  swap)     do_swap ;;
  finish)   do_finish ;;
  rollback) do_rollback ;;
esac
