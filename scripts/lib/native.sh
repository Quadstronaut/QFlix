#!/usr/bin/env bash
# native.sh - shared helpers for the per-app native installers
# (scripts/configure/3NN-native-<slug>-install.sh), QFLX-21, spec 5.3 / 5.7.
#
# SOURCE it (needs log.sh first for log_*; falls back to plain stderr).
# Every function either prints its result on stdout or returns non-zero.
# Nothing here starts, stops or restarts an app: installers `enable` only.
#
# Overrides (tests; all resolved at call time):
#   QFLIX_APPS_DIR  (default ~/.apps)     QFLIX_CURL   (default curl)
#   QFLIX_APPCTL    (default ~/bin/appctl) QFLIX_SS    (default ss)
#   QFLIX_PYTHON    (default python3)     QFLIX_SWAP_DIR (swapstate.py)
#
# Functions:
#   native_fetch_verify URL SHA256 DEST    download + sha256 check (DEST removed on mismatch)
#   native_check_parity SLUG VERSION       refuse unless VERSION == `appctl version SLUG`
#   native_install_versioned SLUG VER SRC  parity, then bin/<VER> + atomic `current` symlink
#   native_render_env SLUG FAMILY VER [K=V..]   env-file body (thread caps + family hook)
#   native_render_unit SLUG FAMILY EXE ARGS [WORKDIR]  qflix-<slug>.service body (EXE %h/... or /... = verbatim)
#   native_write_secure PATH MODE          stdin -> PATH atomically, chmod MODE
#   native_listen_capture SLUG PORT        record listen set (swapstate.py)
#   native_listen_compare SLUG             diff live vs recorded (exit 1 = drift)
#   native_soak_check SLUG                 exit 1 = inside the 14-day soak
#   native_close_window SLUG               first native upgrade closes rollback-to-UCC
#   native_require_generic_host            refuse unless host.profile resolves to generic (QFLX-41)
#   native_link_current SLUG VER           atomic bin/current -> bin/VER, no UCC parity (QFLX-41)
#
# Families: dotnet (arrs, prowlarr) | go (unpackerr) | node (seerr) | python | db
# Self-update stays OFF (I-10): arr UpdateMechanism=External and the bazarr
# no-update flag are per-app config, set by each 3NN installer.
# TasksMax= is deliberately NEVER emitted (G-2) until the F6 box proof shows the
# user manager delegates pids.

_NATIVE_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_NATIVE_SWAPSTATE_PY="$_NATIVE_SELF_DIR/../maint/lib/swapstate.py"

_native_err() {
  if declare -F log_error >/dev/null 2>&1; then log_error "$*"; else echo "[x] $*" >&2; fi
}

_native_apps_dir() { printf '%s' "${QFLIX_APPS_DIR:-$HOME/.apps}"; }

# Slug / version become path components: no slashes, no dot-dot, no spaces.
_native_valid_slug() { [[ "${1:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ && "$1" != *..* ]]; }
_native_valid_ver()  { [[ "${1:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$ && "$1" != *..* ]]; }

_native_swap() { "${QFLIX_PYTHON:-python3}" "$_NATIVE_SWAPSTATE_PY" "$@"; }

native_fetch_verify() {
  local url="$1" want="$2" dest="$3" tmp got
  [[ "$want" =~ ^[0-9a-fA-F]{64}$ ]] || { _native_err "fetch_verify: sha256 must be 64 hex chars"; return 1; }
  tmp="$dest.part.$$"
  if ! "${QFLIX_CURL:-curl}" -fsSL --retry 2 -o "$tmp" "$url"; then
    rm -f "$tmp"; _native_err "fetch_verify: download failed"; return 1
  fi
  got="$(sha256sum "$tmp" | cut -d' ' -f1)"
  if [ "${got,,}" != "${want,,}" ]; then
    rm -f "$tmp"; _native_err "fetch_verify: sha256 mismatch (got $got)"; return 1
  fi
  mv -f "$tmp" "$dest"
}

# The UCC container's version, from `appctl version <slug>` (a JSON blob or a
# bare line). Empty on failure.
native_ucc_version() {
  local out ver
  out="$("${QFLIX_APPCTL:-$HOME/bin/appctl}" version "$1" 2>/dev/null)" || return 0
  ver="$(printf '%s' "$out" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' | tail -n 1)"
  [ -n "$ver" ] || ver="$(printf '%s' "$out" | tail -n 1 | tr -d '\r')"
  printf '%s' "$ver"
}

# I-10 exact-version parity. Fails CLOSED: an unreadable version is a refusal.
native_check_parity() {
  local slug="$1" want="${2#v}" have
  have="$(native_ucc_version "$slug")"; have="${have#v}"
  if [ -z "$have" ]; then
    _native_err "parity: cannot read 'appctl version $slug'; refusing"; return 1
  fi
  if [ "$have" != "$want" ]; then
    _native_err "parity: target $want != app-$slug version $have; refusing"; return 1
  fi
}

native_install_versioned() {
  local slug="$1" ver="$2" src="$3" bindir tgt
  _native_valid_slug "$slug" || { _native_err "bad slug: $slug"; return 1; }
  _native_valid_ver "$ver"   || { _native_err "bad version: $ver"; return 1; }
  [ -e "$src" ] || { _native_err "install_versioned: source $src missing"; return 1; }
  native_check_parity "$slug" "$ver" || return 1
  bindir="$(_native_apps_dir)/$slug/bin"
  tgt="$bindir/$ver"
  mkdir -p "$bindir" || return 1
  if [ ! -d "$tgt" ]; then
    # Stage beside the target then rename: a half-copied bin/<ver> never exists.
    mkdir -p "$tgt.part.$$" || return 1
    if [ -d "$src" ]; then cp -a "$src"/. "$tgt.part.$$"/; else cp -a "$src" "$tgt.part.$$"/; fi \
      || { rm -rf "$tgt.part.$$"; return 1; }
    mv "$tgt.part.$$" "$tgt" || { rm -rf "$tgt.part.$$"; return 1; }
  fi
  # Relative link, swapped atomically (rename over the old symlink).
  ln -sfn "$ver" "$bindir/.current.$$" && mv -Tf "$bindir/.current.$$" "$bindir/current"
}

# stdout: env-file body. Thread caps live HERE because ulimit -u 2000 is shared
# by every process on the slot (spec 5.3). NODE_OPTIONS is never used.
native_render_env() {
  local slug="$1" fam="$2" ver="$3"; shift 3
  _native_valid_slug "$slug" || { _native_err "bad slug: $slug"; return 1; }
  case "$fam" in
    dotnet) printf 'DOTNET_PROCESSOR_COUNT=4\nDOTNET_gcServer=0\n' ;;
    go)     printf 'GOMAXPROCS=4\n' ;;
    node)   printf 'UV_THREADPOOL_SIZE=4\n' ;;
    python|db) ;;
    *) _native_err "unknown family: $fam"; return 1 ;;
  esac
  printf 'MALLOC_ARENA_MAX=2\n'
  # Per-family extra-env hook (O-7). bazarr reports its version from this var.
  case "$slug" in
    bazarr|bazarr-1|bazarr1) printf 'BAZARR_VERSION=%s\n' "$ver" ;;
  esac
  local kv
  for kv in "$@"; do printf '%s\n' "$kv"; done
}

# stdout: the unit (spec 5.3). ARGS is a raw string; %h specifiers pass through.
native_render_unit() {
  local slug="$1" fam="$2" exe="$3" args="${4:-}" wd="${5:-}" stop=60 pre="" cmd
  _native_valid_slug "$slug" || { _native_err "bad slug: $slug"; return 1; }
  # WORKDIR (QFLX-36): defaults to the data dir. Seerr's Next.js server resolves
  # its .next build from the CWD, so it runs from %h/.apps/seerr/bin/current.
  # Only a path under %h/.apps/<slug> is accepted.
  case "$wd" in
    "") wd="%h/.apps/$slug" ;;
    "%h/.apps/$slug"|"%h/.apps/$slug/"*)
      [[ "$wd" != *..* && "$wd" != *[[:space:]]* ]] || { _native_err "bad workdir: $wd"; return 1; } ;;
    *) _native_err "workdir must be under %h/.apps/$slug: $wd"; return 1 ;;
  esac
  case "$fam" in
    dotnet|go|python) ;;
    node) pre="--disable-wasm-trap-handler " ;;   # CLI flag, never NODE_OPTIONS
    db)   stop=120 ;;
    *) _native_err "unknown family: $fam"; return 1 ;;
  esac
  # EXE is normally a file under bin/current. A %h/... or /... EXE is used
  # VERBATIM (QFLX-27: bazarr runs `venv/bin/python bin/current/bazarr.py`, the
  # interpreter lives outside bin/current).
  case "$exe" in
    %h/*|/*) cmd="$exe" ;;
    *)       cmd="%h/.apps/$slug/bin/current/$exe" ;;
  esac
  cat <<EOF
[Unit]
Description=QFlix $slug (native)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$wd
Environment=PATH=%h/.apps/$slug/bin/current:%h/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=%h/.config/qflix/$slug.env
ExecStart=${cmd} ${pre}${args}
Restart=on-failure
RestartSec=15
StartLimitIntervalSec=600
StartLimitBurst=5
TimeoutStopSec=$stop
Nice=5
UMask=0002

[Install]
WantedBy=default.target
EOF
}

native_write_secure() {
  local path="$1" mode="${2:-0600}" tmp
  tmp="$path.tmp.$$"
  mkdir -p "$(dirname "$path")" || return 1
  ( umask 077; cat >"$tmp" ) && chmod "$mode" "$tmp" && mv -f "$tmp" "$path"
}

# --- listen set (reuses swapstate.py from QFLX-20; nothing duplicated) -------------
native_listen_capture() {
  "${QFLIX_SS:-ss}" -tlnH 2>/dev/null | _native_swap capture "$1" --port "$2" --ss-file -
}
native_listen_compare() {
  "${QFLIX_SS:-ss}" -tlnH 2>/dev/null | _native_swap diff "$1" --ss-file -
}

# --- soak gate (swapstate.py) ---------------------------------------------------------
native_soak_check()   { _native_swap soak-check "$1" >/dev/null; }
native_close_window() { _native_swap close-window "$1" >/dev/null; }

# --- generic-host gate + current-link (QFLX-41) --------------------------------------
# Installers that must NEVER run on a shared Ultra slot (box-2 Kuma, the box-2
# reverse proxy) call this first. Fails CLOSED: a missing/unknown/mismatched
# host.profile secret is a refusal, exactly like an `ultra` one (I-12).
native_require_generic_host() {
  local out
  out="$("${QFLIX_PYTHON:-python3}" "$_NATIVE_SELF_DIR/../maint/lib/hostpolicy.py" preflight 2>/dev/null)" \
    || { _native_err "hostpolicy: host.profile unresolved; refusing"; return 1; }
  [ "$out" = "generic" ] || { _native_err "hostpolicy: host.profile=$out; generic-host installer; refusing"; return 1; }
}

# Atomically point bin/current at bin/<ver> (the swap half of native_install_versioned,
# without the UCC parity check: a box-2 app has no UCC twin to match).
native_link_current() {
  local slug="$1" ver="$2" bindir
  _native_valid_slug "$slug" && _native_valid_ver "$ver" || { _native_err "bad slug/version"; return 1; }
  bindir="$(_native_apps_dir)/$slug/bin"
  [ -e "$bindir/$ver" ] || { _native_err "link_current: $bindir/$ver missing"; return 1; }
  ln -sfn "$ver" "$bindir/.current.$$" && mv -Tf "$bindir/.current.$$" "$bindir/current"
}
