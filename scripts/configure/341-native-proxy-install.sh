#!/usr/bin/env bash
# 341-native-proxy-install.sh - box-2 OWN reverse proxy + TLS as a user unit (QFLX-41, M3, D-2).
#
# GENERIC LINUX HOST ONLY. Ultra's user-nginx (panel-templated, TLS terminated by the
# Ultra front proxy) is untouched (R4). Hostpolicy-gated: refuses unless host.profile
# resolves to `generic` (fail closed, I-12), even in dry-run.
#
# INERT BY DEFAULT: without --execute it prints the plan and changes nothing.
#
# One flag, the two D-2 options, ONE routing table (scripts/maint/lib/proxygen.py
# renders both FROM manifest/apps.yaml + secrets/<port_secret> + urlbase secrets):
#   --flavor caddy   user-space Caddy, automatic ACME TLS (pinned binary, sha256-verified)
#   --flavor nginx   the host's nginx run as a user process; TLS certs are YOURS
#                    (--cert-dir, default ~/.config/qflix/proxy/certs/<name>/{fullchain,privkey}.pem)
# Routes: the dashboard at `/`, seerr on its own vhost seerr.<domain>, urlbase apps
# passed through at /<urlbase>/, the rest at /<slug>/ (prefix stripped). A missing port
# secret aborts the render: no half-config is ever installed.
#
# Usage: 341-native-proxy-install.sh --flavor caddy|nginx --domain D [--email E]
#                                    [--cert-dir P] [--execute]
#
# Unit: qflix-proxy.service (scripts/lib/native.sh). Installed + `enable`d, NEVER
# started. Binding 80/443 as a user needs `net.ipv4.ip_unprivileged_port_start<=80`
# (or setcap on the caddy binary) - checked and warned about, not changed here.
#
# Test/bump hooks: QFLIX_CURL QFLIX_NGINX QFLIX_SYSTEMCTL QFLIX_ARCH QFLIX_APPS_DIR
# QFLIX_CADDY_VER QFLIX_CADDY_SHA_X64 QFLIX_CADDY_SHA_ARM64 QFLIX_PORT_START_FILE
# MANITOBA_SECRETS_DIR.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../lib/log.sh
source "$HERE/scripts/lib/log.sh"
# shellcheck source=../lib/native.sh
source "$HERE/scripts/lib/native.sh"

SLUG=proxy
CADDY_VER="${QFLIX_CADDY_VER:-2.8.4}"
CADDY_SHA_X64="${QFLIX_CADDY_SHA_X64:-a7e8306c54138cf88e371c5ec0caf7baf142ecc1d60a30897dfb67d65d3748c8}"
# arm64 sha is not pinned yet: refuse rather than install an unverified binary.
CADDY_SHA_ARM64="${QFLIX_CADDY_SHA_ARM64:-}"

EXECUTE=0; FLAVOR=""; DOMAIN=""; EMAIL=""; CERTDIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --execute) EXECUTE=1 ;;
    --flavor) FLAVOR="${2:-}"; shift ;;
    --domain) DOMAIN="${2:-}"; shift ;;
    --email) EMAIL="${2:-}"; shift ;;
    --cert-dir) CERTDIR="${2:-}"; shift ;;
    -h|--help) sed -n '2,27p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

native_require_generic_host || exit 2
case "$FLAVOR" in caddy|nginx) ;; *) die "--flavor caddy|nginx is required" ;; esac
[ -n "$DOMAIN" ] || die "--domain is required"

SECRETS="${MANITOBA_SECRETS_DIR:-$HERE/secrets}"
CFGDIR="$HOME/.config/qflix/proxy"
APPS="$(_native_apps_dir)"
BASE="$APPS/$SLUG"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PY="${QFLIX_PYTHON:-python3}"
[ -n "$CERTDIR" ] || CERTDIR="$CFGDIR/certs"

# Render FIRST: a bad domain/missing secret aborts before anything is touched.
CONF="$("$PY" "$HERE/scripts/maint/lib/proxygen.py" render --flavor "$FLAVOR" \
        --domain "$DOMAIN" --email "$EMAIL" --manifest "$HERE/manifest/apps.yaml" \
        --secrets-dir "$SECRETS")" || die "proxy config render failed"

ARCH="${QFLIX_ARCH:-$(uname -m)}"
if [ "$FLAVOR" = caddy ]; then
  case "$ARCH" in
    x86_64|x64)    CARCH=amd64 CSHA="$CADDY_SHA_X64" ;;
    aarch64|arm64) CARCH=arm64 CSHA="$CADDY_SHA_ARM64" ;;
    *) die "unsupported arch: $ARCH" ;;
  esac
  [ -n "$CSHA" ] || die "no pinned sha256 for caddy on $ARCH; refusing"
  CURL_URL="https://github.com/caddyserver/caddy/releases/download/v$CADDY_VER/caddy_${CADDY_VER}_linux_${CARCH}.tar.gz"
  VER="$CADDY_VER"
else
  NGINX="${QFLIX_NGINX:-$(command -v nginx || true)}"
  [ -n "$NGINX" ] && [ -x "$NGINX" ] || die "nginx flavor: no nginx binary on this host"
  VER=system
fi

echo "PLAN proxy flavor=$FLAVOR domain=$DOMAIN (seerr.$DOMAIN vhost, dashboard at /)"
echo "PLAN config  $CFGDIR/$([ "$FLAVOR" = caddy ] && echo Caddyfile || echo 'nginx.conf + qflix.conf')"
echo "PLAN unit    $UNITDIR/qflix-$SLUG.service (enable only, never start)"
PSF="${QFLIX_PORT_START_FILE:-/proc/sys/net/ipv4/ip_unprivileged_port_start}"
if [ -r "$PSF" ] && [ "$(cat "$PSF")" -gt 80 ]; then
  log_warn "unprivileged ports start at $(cat "$PSF"): a user unit cannot bind 80/443 until you lower it (or setcap caddy)"
fi
if [ "$EXECUTE" -ne 1 ]; then
  log_info "dry run: nothing changed (pass --execute)"; exit 0
fi

TMP="$(mktemp -d)" || die "mktemp failed"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$BASE/bin/$VER" "$BASE/logs" "$CFGDIR" || exit 1

if [ "$FLAVOR" = caddy ]; then
  native_fetch_verify "$CURL_URL" "$CSHA" "$TMP/caddy.tar.gz" || exit 1
  mkdir -p "$TMP/x" && tar -xzf "$TMP/caddy.tar.gz" -C "$TMP/x" caddy || die "caddy extract failed"
  install -m 0755 "$TMP/x/caddy" "$BASE/bin/$VER/caddy" || exit 1
  printf '%s\n' "$CONF" | native_write_secure "$CFGDIR/Caddyfile" 0644 || exit 1
  "$BASE/bin/$VER/caddy" validate --config "$CFGDIR/Caddyfile" --adapter caddyfile >/dev/null \
    || die "caddy validate failed; not enabling"
  ARGS="run --config %h/.config/qflix/proxy/Caddyfile --adapter caddyfile"
  FAM=go; EXE=caddy
else
  ln -sfn "$NGINX" "$BASE/bin/$VER/nginx" || exit 1
  mkdir -p "$BASE/tmp" "$CERTDIR" || exit 1
  printf '%s\n' "$CONF" | sed "s|@@CERTDIR@@|$CERTDIR|g" | native_write_secure "$CFGDIR/qflix.conf" 0644 || exit 1
  {
    echo "worker_processes 2;"
    echo "pid $BASE/nginx.pid;"
    echo "error_log $BASE/logs/error.log warn;"
    echo "events { worker_connections 512; }"
    echo "http {"
    echo "    access_log $BASE/logs/access.log;"
    for t in client_body proxy fastcgi uwsgi scgi; do echo "    ${t}_temp_path $BASE/tmp/$t;"; done
    echo "    include $CFGDIR/qflix.conf;"
    echo "}"
  } | native_write_secure "$CFGDIR/nginx.conf" 0644 || exit 1
  mkdir -p "$BASE/tmp" && "$NGINX" -t -c "$CFGDIR/nginx.conf" >/dev/null 2>&1 \
    || die "nginx -t failed; not enabling"
  ARGS="-c %h/.config/qflix/proxy/nginx.conf -g 'daemon off;'"
  FAM=python; EXE=nginx
fi

native_link_current "$SLUG" "$VER" || exit 1
native_render_env "$SLUG" "$FAM" "$VER" | native_write_secure "$HOME/.config/qflix/$SLUG.env" 0600 || exit 1
native_render_unit "$SLUG" "$FAM" "$EXE" "$ARGS" \
  | native_write_secure "$UNITDIR/qflix-$SLUG.service" 0644 || exit 1

SC="${QFLIX_SYSTEMCTL:-systemctl}"
"$SC" --user daemon-reload || die "daemon-reload failed"
"$SC" --user enable "qflix-$SLUG.service" || die "enable failed"
log_info "installed $FLAVOR proxy; start with: systemctl --user start qflix-$SLUG.service"
