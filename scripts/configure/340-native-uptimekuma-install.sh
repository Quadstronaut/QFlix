#!/usr/bin/env bash
# 340-native-uptimekuma-install.sh - box-2 NATIVE Uptime Kuma as a user unit (QFLX-41, M3).
#
# GENERIC LINUX HOST ONLY. Ultra's Kuma stays panel-managed (R4: the observer must
# not move mid-migration); this installs the box-2 copy. Hostpolicy-gated: refuses
# unless host.profile resolves to `generic` (fail closed, I-12) - even in dry-run.
#
# INERT BY DEFAULT: without --execute it prints the plan and changes nothing.
#
# What --execute does (every download is sha256-pinned below; a mismatch aborts):
#   1. node tarball + uptime-kuma source + uptime-kuma dist -> ~/.apps/uptimekuma/bin/<ver>/
#      (node/, app/); `npm ci --omit=dev` inside app/; `bin/current` swapped atomically.
#   2. data dir ~/.apps/uptimekuma/data (0700); optional kuma.db IMPORT (below).
#   3. ~/.config/qflix/uptimekuma.env (0600) and the user unit qflix-uptimekuma.service
#      rendered by scripts/lib/native.sh (node family: thread caps, no NODE_OPTIONS,
#      --disable-wasm-trap-handler on the CLI), daemon-reload, `enable`. NEVER starts
#      it: start is a deliberate operator step after the db import.
#
# kuma.db IMPORT (--import-db PATH) - why: push tokens live in kuma.db, so a COPY of
# the Ultra db keeps every canary/pusher token valid on box 2 (no re-bootstrap).
#   * Take a consistent copy on Ultra FIRST (read-only for Ultra):
#       sqlite3 ~/.apps/uptimekuma/data/kuma.db ".backup /tmp/kuma-copy.db"
#     and scp it here. Never copy kuma.db + -wal files by hand.
#   * Refused when: a kuma.db already exists here, the unit is active, or
#     PRAGMA integrity_check != ok. Never dual-post: box 2 is not live before M1, and
#     the push base URL is flipped in ONE step later (I-1).
#
# Port: --port N, else secrets/uptimekuma.port. Binds 127.0.0.1 only (the proxy,
# 341-native-proxy-install.sh, fronts it).
#
# Test/bump hooks (tests inject fakes; an operator bumping a pin edits the
# constants below in a reviewed commit): QFLIX_CURL QFLIX_NPM QFLIX_SYSTEMCTL
# QFLIX_ARCH QFLIX_APPS_DIR QFLIX_KUMA_VER QFLIX_NODE_VER QFLIX_KUMA_SHA
# QFLIX_KUMA_DIST_SHA QFLIX_NODE_SHA_X64 QFLIX_NODE_SHA_ARM64 MANITOBA_SECRETS_DIR.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../lib/log.sh
source "$HERE/scripts/lib/log.sh"
# shellcheck source=../lib/native.sh
source "$HERE/scripts/lib/native.sh"

SLUG=uptimekuma
KUMA_VER="${QFLIX_KUMA_VER:-1.23.16}"
KUMA_SHA="${QFLIX_KUMA_SHA:-a4e5c226b443458ea69bc9766b5a88369700283c8eb03366090b8ce175754f14}"
KUMA_DIST_SHA="${QFLIX_KUMA_DIST_SHA:-6826ad0ff25661adea8b5d4d12ce98ec171d3dcfa9e35e74a4c3ef76b6c7dc86}"
NODE_VER="${QFLIX_NODE_VER:-20.18.1}"
NODE_SHA_X64="${QFLIX_NODE_SHA_X64:-c6fa75c841cbffac851678a472f2a5bd612fff8308ef39236190e1f8dbb0e567}"
NODE_SHA_ARM64="${QFLIX_NODE_SHA_ARM64:-44d1ffc5905c005ace4515ca6f8c090c4c7cfce3a9a67df0dba35c727590b8f6}"

EXECUTE=0; PORT=""; IMPORT_DB=""
while [ $# -gt 0 ]; do
  case "$1" in
    --execute) EXECUTE=1 ;;
    --port) PORT="${2:-}"; shift ;;
    --import-db) IMPORT_DB="${2:-}"; shift ;;
    -h|--help) sed -n '2,38p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

native_require_generic_host || exit 2

SECRETS="${MANITOBA_SECRETS_DIR:-$HERE/secrets}"
[ -n "$PORT" ] || PORT="$(tr -d '[:space:]' < "$SECRETS/uptimekuma.port" 2>/dev/null || true)"
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] \
  || die "need a valid port: --port N or secrets/uptimekuma.port"

ARCH="${QFLIX_ARCH:-$(uname -m)}"
case "$ARCH" in
  x86_64|x64)   NARCH=x64;   NODE_SHA="$NODE_SHA_X64" ;;
  aarch64|arm64) NARCH=arm64; NODE_SHA="$NODE_SHA_ARM64" ;;
  *) die "unsupported arch: $ARCH" ;;
esac

APPS="$(_native_apps_dir)"
BASE="$APPS/$SLUG"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ENVFILE="$HOME/.config/qflix/$SLUG.env"
NODE_URL="https://nodejs.org/dist/v$NODE_VER/node-v$NODE_VER-linux-$NARCH.tar.xz"
KUMA_URL="https://github.com/louislam/uptime-kuma/archive/refs/tags/$KUMA_VER.tar.gz"
DIST_URL="https://github.com/louislam/uptime-kuma/releases/download/$KUMA_VER/dist.tar.gz"

if [ -n "$IMPORT_DB" ] && [ ! -f "$IMPORT_DB" ]; then die "--import-db: $IMPORT_DB not found"; fi

echo "PLAN uptime-kuma $KUMA_VER on node $NODE_VER ($NARCH), 127.0.0.1:$PORT"
echo "PLAN install tree  $BASE/bin/$KUMA_VER (current -> it)"
echo "PLAN data dir      $BASE/data${IMPORT_DB:+  (import $IMPORT_DB)}"
echo "PLAN env + unit    $ENVFILE  $UNITDIR/qflix-$SLUG.service (enable only, never start)"
if [ "$EXECUTE" -ne 1 ]; then
  log_info "dry run: nothing changed (pass --execute)"; exit 0
fi

TMP="$(mktemp -d)" || die "mktemp failed"
trap 'rm -rf "$TMP"' EXIT
STAGE="$BASE/bin/$KUMA_VER.part.$$"
trap 'rm -rf "$TMP" "$STAGE"' EXIT

native_fetch_verify "$NODE_URL" "$NODE_SHA" "$TMP/node.tar.xz"     || exit 1
native_fetch_verify "$KUMA_URL" "$KUMA_SHA" "$TMP/kuma.tar.gz"     || exit 1
native_fetch_verify "$DIST_URL" "$KUMA_DIST_SHA" "$TMP/dist.tar.gz" || exit 1

mkdir -p "$STAGE/nodedist" "$STAGE/app" || exit 1
tar -xJf "$TMP/node.tar.xz" --strip-components=1 -C "$STAGE/nodedist" || die "node extract failed"
tar -xzf "$TMP/kuma.tar.gz" --strip-components=1 -C "$STAGE/app"      || die "kuma extract failed"
tar -xzf "$TMP/dist.tar.gz" -C "$STAGE/app"                           || die "dist extract failed"
ln -s nodedist/bin/node "$STAGE/node"
ln -s nodedist/bin/npm  "$STAGE/npm"
( cd "$STAGE/app" && PATH="$STAGE:$PATH" "${QFLIX_NPM:-$STAGE/npm}" ci --omit=dev ) \
  || die "npm ci failed"

if [ ! -d "$BASE/bin/$KUMA_VER" ]; then
  mv "$STAGE" "$BASE/bin/$KUMA_VER" || die "install rename failed"
fi
native_link_current "$SLUG" "$KUMA_VER" || exit 1

mkdir -p "$BASE/data" && chmod 0700 "$BASE/data" || exit 1
if [ -n "$IMPORT_DB" ]; then
  [ ! -e "$BASE/data/kuma.db" ] || die "import refused: $BASE/data/kuma.db already exists"
  if "${QFLIX_SYSTEMCTL:-systemctl}" --user is-active --quiet "qflix-$SLUG.service"; then
    die "import refused: qflix-$SLUG.service is active"
  fi
  chk="$("${QFLIX_PYTHON:-python3}" -c 'import sqlite3,sys;print(sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True).execute("PRAGMA integrity_check").fetchone()[0])' "$IMPORT_DB" 2>/dev/null)"
  [ "$chk" = "ok" ] || die "import refused: integrity_check = ${chk:-unreadable}"
  native_write_secure "$BASE/data/kuma.db" 0600 < "$IMPORT_DB" || die "import copy failed"
  log_info "imported kuma.db (push tokens preserved)"
fi

native_render_env "$SLUG" node "$KUMA_VER" \
  "UPTIME_KUMA_HOST=127.0.0.1" "UPTIME_KUMA_PORT=$PORT" "DATA_DIR=$BASE/data/" \
  | native_write_secure "$ENVFILE" 0600 || exit 1
native_render_unit "$SLUG" node node "%h/.apps/$SLUG/bin/current/app/server/server.js" \
  | native_write_secure "$UNITDIR/qflix-$SLUG.service" 0644 || exit 1

SC="${QFLIX_SYSTEMCTL:-systemctl}"
"$SC" --user daemon-reload || die "daemon-reload failed"
"$SC" --user enable "qflix-$SLUG.service" || die "enable failed"
log_info "installed uptime-kuma $KUMA_VER; start with: systemctl --user start qflix-$SLUG.service"
