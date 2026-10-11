#!/usr/bin/env bash
# Phase 92 -- HTTPS ingress for SABnzbd: https://<slot>/sabnzbd/ (QFLX-33).
#
# Usage: 92-sabnzbd-vhost.sh [--execute]
#   (no flag)  DRY-RUN: render the fragment, show the diff against the live
#              copy and the host_whitelist verdict. Nothing on the box changes.
#   --execute  install ~/.apps/nginx/proxy.d/sabnzbd.conf, add the slot host to
#              SAB's host_whitelist if it is missing, reload nginx, prove it.
#
# WHY: SAB had no proxy.d fragment, so /sabnzbd/ on the slot's HTTPS host fell
# through to the dashboard catch-all and 404'd; outside clients only worked on
# the public listener <public-ip>:<port>. The native swap (308 --swap
# --approve-exception, operator decision D-4) drops that public listener, so
# this HTTPS path has to work FIRST. Fragment source of truth:
# scripts/data/sabnzbd.nginx.conf.tmpl (upstream = 127.0.0.1:<sabnzbd.port>,
# which the UCC container and the native unit both serve).
#
# Safety rails, in order:
#   * the fragment is rendered from the box's ~/secrets/sabnzbd.port and refused
#     unless the port is numeric and no @@ token survives;
#   * byte-identical live copy -> no write, no reload (idempotent);
#   * the previous fragment is backed up OUTSIDE proxy.d (nginx includes
#     proxy.d/*.conf, so a *.conf backup in there would load twice);
#   * `nginx -t` gates the reload; a failing test restores the previous state
#     and exits non-zero before nginx is touched;
#   * the reload is the sanctioned `~/bin/appctl proxy-reload` (app-nginx restart on
#     Ultra; app-nginx has no `reload` verb), never a kill;
#   * host_whitelist is changed only through SAB's own API, after an ini backup,
#     and re-read to confirm (SAB APIs can lie: verify, do not trust the ack);
#   * proof: nginx on its listen port, Host=<slot>, /sabnzbd/ answers 200/303
#     (SAB's login redirect), then -- from the workstation -- the public HTTPS
#     URL answers 200/303 and api?mode=version answers with the API key.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
# Sourced BEFORE set -e: ssh.sh resolves ../../secrets with a `cd` that fails
# (by design, it then falls back) when there is no secrets/ dir -- on CI and on
# the box -- and under set -e that failed assignment would exit silently.
# shellcheck source=/dev/null
source "$HERE/../lib/ssh.sh"
# shellcheck source=/dev/null
source "$HERE/../lib/log.sh"
set -euo pipefail

EXECUTE=0
for a in "$@"; do
  case "$a" in
    --execute) EXECUTE=1 ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument: $a (usage: 92-sabnzbd-vhost.sh [--execute])" ;;
  esac
done

TMPL_SRC="$REPO_ROOT/scripts/data/sabnzbd.nginx.conf.tmpl"
[ -s "$TMPL_SRC" ] || die "missing $TMPL_SRC"
PUB_HOST="${QFLIX_PUBLIC_HOST:-$(tr -d '[:space:]' < "$REPO_ROOT/secrets/seedbox.host" 2>/dev/null || true)}"
[ -n "$PUB_HOST" ] || PUB_HOST="quadstronaut.seedbox.example.com"
[[ "$PUB_HOST" =~ ^[A-Za-z0-9.-]+$ ]] || die "refusing odd public host '$PUB_HOST'"

MODE=dry; [ "$EXECUTE" = 1 ] && MODE=execute
log_info "phase 92 ($MODE): SAB vhost for https://$PUB_HOST/sabnzbd/"

scpm_to "$TMPL_SRC" "~/.apps/nginx/.qflix-sabnzbd.tmpl"

# Everything box-side runs in ONE ssh session (no fan-out: fail2ban).
sshm "bash -s -- $MODE $PUB_HOST" <<'REMOTE'
set -euo pipefail
MODE="$1"; PUB="$2"
NGX="$HOME/.apps/nginx"
FRAG="$NGX/proxy.d/sabnzbd.conf"
TMPL="$NGX/.qflix-sabnzbd.tmpl"
NEW="$NGX/.qflix-sabnzbd.conf.new"
SECRETS="${QFLIX_SECRETS_DIR:-$HOME/secrets}"
NGINX_BIN="${QFLIX_NGINX_BIN:-/usr/sbin/nginx}"
APPCTL="${QFLIX_APPCTL:-$HOME/bin/appctl}"
CURL="${QFLIX_CURL:-curl}"
PY="${QFLIX_PYTHON:-python3}"
SAB_INI="${QFLIX_SAB_INI:-$HOME/.apps/sabnzbd/sabnzbd.ini}"
SETTLE="${QFLIX_SETTLE_S:-10}"
TS="$(date +%Y%m%d%H%M%S)"
trap 'rm -f "$TMPL" "$NEW"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

port="$(tr -d '[:space:]' < "$SECRETS/sabnzbd.port" 2>/dev/null || true)"
[[ "$port" =~ ^[0-9]{2,5}$ ]] || fail "sabnzbd.port '$port' is not a port"
key="$(tr -d '[:space:]' < "$SECRETS/sabnzbd.key" 2>/dev/null || true)"
[ -n "$key" ] || fail "no $SECRETS/sabnzbd.key"
sed "s/@@PORT@@/$port/g" "$TMPL" > "$NEW"
! grep -q '@@' "$NEW" || fail "unrendered @@ token in the fragment"
grep -q "proxy_pass       http://127.0.0.1:$port;" "$NEW" || fail "rendered fragment lacks the loopback upstream"
listen="$(grep -m1 -oE 'listen[[:space:]]+[0-9]+' "$NGX/sites-enabled/default" | grep -oE '[0-9]+$' || true)"
[ -n "$listen" ] || fail "cannot read the nginx listen port from sites-enabled/default"

api() { "$CURL" -fsS --max-time 20 "http://127.0.0.1:$port/sabnzbd/api?output=json&apikey=$key&$1"; }
whitelist() {
  api "mode=get_config&section=misc&keyword=host_whitelist" | "$PY" -c '
import json, sys
v = json.load(sys.stdin)["config"]["misc"]["host_whitelist"]
if isinstance(v, str):
    v = [t for t in (s.strip() for s in v.split(",")) if t]
print(",".join(v))'
}
has_host() { case ",$1," in *",$PUB,"*) return 0 ;; esac; return 1; }

wl="$(whitelist)" || fail "SAB API get_config host_whitelist failed on 127.0.0.1:$port"
if cmp -s "$NEW" "$FRAG"; then frag_state=identical
elif [ -e "$FRAG" ]; then frag_state=differs
else frag_state=missing; fi
echo "fragment: $frag_state (upstream 127.0.0.1:$port, nginx listen $listen)"
if has_host "$wl"; then echo "host_whitelist: has $PUB"; else echo "host_whitelist: MISSING $PUB"; fi

if [ "$MODE" != execute ]; then
  case "$frag_state" in
    missing) echo "--- would install $FRAG:"; grep -v '^#' "$NEW" ;;
    differs) diff -u "$FRAG" "$NEW" || true ;;
  esac
  echo "DRY-RUN: nothing changed (re-run with --execute)"
  exit 0
fi

# --- host_whitelist (API, ini backup, re-read) ---------------------------------
if ! has_host "$wl"; then
  [ -f "$SAB_INI" ] && cp -p "$SAB_INI" "$SAB_INI.bak.qflx33.$TS" && echo "ini backup: $SAB_INI.bak.qflx33.$TS"
  want="$wl"; [ -n "$want" ] && want="$want, "; want="$want$PUB"
  enc="$("$PY" -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$want")"
  api "mode=set_config&section=misc&keyword=host_whitelist&value=$enc" >/dev/null \
    || fail "SAB API set_config host_whitelist failed"
  wl="$(whitelist)" || fail "SAB API re-read of host_whitelist failed"
  has_host "$wl" || fail "host_whitelist still lacks $PUB after set_config (SAB ignored it)"
  echo "host_whitelist: added $PUB (now: $wl)"
fi

# --- fragment (backup outside proxy.d, nginx -t gate, sanctioned reload) -------
if [ "$frag_state" != identical ]; then
  bak=""
  if [ -e "$FRAG" ]; then
    bak="$NGX/sabnzbd.conf.bak.qflx33.$TS"
    cp -p "$FRAG" "$bak"; echo "fragment backup: $bak"
  fi
  cp "$NEW" "$FRAG"; chmod 644 "$FRAG"
  if ! out="$("$NGINX_BIN" -t -p "$NGX/" -c nginx.conf 2>&1)" \
     || ! printf '%s\n' "$out" | grep -q 'test is successful'; then
    if [ -n "$bak" ]; then cp -p "$bak" "$FRAG"; else rm -f "$FRAG"; fi
    printf '%s\n' "$out" >&2
    fail "nginx -t rejected the fragment; previous state restored, nginx untouched"
  fi
  echo "nginx -t: ok"
  "$APPCTL" proxy-reload || fail "~/bin/appctl proxy-reload failed (fragment is in place; nginx -t passed)"
  echo "nginx: reloaded via ~/bin/appctl proxy-reload; settling ${SETTLE}s"
  sleep "$SETTLE"
fi

# --- on-box proof: nginx -> SAB with the slot Host -----------------------------
code=""
for _ in 1 2 3; do
  code="$("$CURL" -s -o /dev/null -w '%{http_code}' --max-time 20 -H "Host: $PUB" "http://127.0.0.1:$listen/sabnzbd/" || true)"
  case "$code" in 200|303) break ;; esac
  sleep "$SETTLE"
done
case "$code" in 200|303) echo "PROOF nginx:$listen Host=$PUB /sabnzbd/ -> $code" ;;
  *) fail "nginx:$listen Host=$PUB /sabnzbd/ -> '$code' (want 200 or 303)" ;; esac
REMOTE

# --- workstation proof over the public HTTPS path -----------------------------
if [ "$EXECUTE" = 1 ] && [ "${QFLIX_SKIP_PUBLIC_SMOKE:-0}" != 1 ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://$PUB_HOST/sabnzbd/" || true)"
  case "$code" in 200|303) log_info "PROOF https://$PUB_HOST/sabnzbd/ -> $code" ;;
    *) die "https://$PUB_HOST/sabnzbd/ -> '$code' (want 200 or 303)" ;; esac
  key="$(sshm 'tr -d "[:space:]" < ~/secrets/sabnzbd.key')"
  ver="$(curl -fsS --max-time 30 "https://$PUB_HOST/sabnzbd/api?mode=version&output=json&apikey=$key" || true)"
  case "$ver" in *'"version"'*) log_info "PROOF https api mode=version -> $ver" ;;
    *) die "https api mode=version did not answer a version: '$ver'" ;; esac
fi
log_info "phase 92 ($MODE) complete"
