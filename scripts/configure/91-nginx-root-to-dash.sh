#!/usr/bin/env bash
# CUTOVER + LOGGING — own the user-nginx root `location /` from this repo.
#
# Originally: point user-nginx root at the QFlix Dashboard, replacing the
# Homarr root 302 redirect (validated 2026-06-27 cutover). Now also the vehicle
# for QFlix-owned HTTP access/error logging, because that marked block is the
# ONLY part of the panel-templated default site QFlix is allowed to write.
#
# ---------------------------------------------------------------------------
# WHY the logging half exists (2026-08-19)
#
# Concrete failure: QFlix ran its entire public stack with zero HTTP access
# logging. Read off the box 2026-08-19: ~/.apps/nginx/logs/access.log was 0
# bytes with mtime 2026-05-08 (provisioning day) and error.log's last entry was
# 2026-06-27 — an [emerg] bind() failure this very script's cutover emitted.
# A fresh probe (GET https://<host>/qflix-xff-probe-<ts> -> 404) wrote nothing
# anywhere. Three months of traffic, no forensics: no answer to "who hit this",
# "when did the 404 storm start", "scanner or member".
#
# Root cause, verified not assumed: exactly one server block exists —
# sites-available/default, `listen 17040`, symlinked from sites-enabled/ — and
# it sets `access_log off;` + `error_log /dev/null;` at SERVER level. Those
# override the http-level `access_log logs/access.log;` in nginx.conf:25 and
# inherit into every location. The logs/{radarr,sonarr,tautulli}.access.log
# paths that appear "missing" are a red herring — those directives are
# COMMENTED OUT in the panel's proxy.d/*.conf and never produced files.
#
# Constraint honoured: sites-available/default and proxy.d/*.conf are Ultra.cc
# PANEL-TEMPLATED. Editing the panel's own lines is the qbittorrent.service
# mistake again. So this script does NOT touch `access_log off` — it overrides
# it from inside the QFlix-owned `# manitoba-qflix-dash-root` block, which
# nginx honours because the innermost level that defines a log directive wins.
# Coverage is therefore the catch-all `location /`: public root, dashboard,
# /api/*, /_app/immutable/* (the dash-build-without-restart 404 class), and all
# unmatched scan traffic. The panel's ^~ prefix locations stay dark by design.
#
# Two structural fixes that came with it:
#   1. The block is now RENDERED FROM scripts/qflix-dash/qflix-dash.nginx.conf.tmpl
#      instead of being duplicated inline here. The two had already drifted;
#      one source of truth means the next edit cannot half-land.
#   2. Idempotence changed from "marker present -> skip" to "render, diff, skip
#      only if byte-identical". The old skip made this script incapable of ever
#      UPGRADING an already-deployed block — the logging would have been
#      silently no-opped on the one box that has already had the cutover run.
#
# @@LOGFMT@@ is resolved against the LIVE nginx.conf: `main` if that http-level
# log_format still exists (it does — nginx.conf:28 — and it carries
# $http_x_forwarded_for, the real client IP behind the Ultra.cc proxy that
# terminates 443 in front of 17040), else the nginx built-in `combined`.
# Hardcoding `main` would let a future panel edit fail `nginx -t` and take every
# public service down on the next reload.
#
# Two other prefix locations are QFlix-owned and still dark: ^~ /faq and
# ^~ /images/ (scripts/data/qflix-faq.conf, scripts/data/qflix-images.conf,
# installed by phase 60-www-images.sh). Verified on the box 2026-08-19 that
# neither declares a log directive, so both inherit the server-level `off`.
# They need the same two lines, added in THEIR files -- not smuggled in here,
# because a proxy.d conf this script does not own is exactly the drift the
# deploy-drift canary exists to catch. Handed off as an operator action.
#
# Rotation gap (NOT closed here): ~/.config/logrotate.conf is written by
# scripts/configure/250-logrotate-install.sh and contains zero nginx entries as
# of 2026-08-19, so logs/qflix-dash.*.log grow unbounded. Phase 250 owns that
# file; the block it needs is an operator action, not this script's business.
# ---------------------------------------------------------------------------
#
# Safety: validates with `nginx -t -p <prefix>` (gating on "syntax is ok" + no
# emerg) BEFORE any reload, reloads via `app-nginx restart`, then verifies root
# 200 + dashboard marker + that the access log actually grew from the probe.
# AUTO-ROLLS-BACK on any failure — a bad reload takes down ALL public services,
# so nothing here is allowed to fail forward.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOST="$(tr -d '[:space:]' < "$HERE/secrets/seedbox.ssh-host" 2>/dev/null \
        || tr -d '[:space:]' < "$HERE/secrets/seedbox.host")"
PORT="$(tr -d '[:space:]' < "$HERE/secrets/qflix-dash.port" 2>/dev/null || echo 42020)"
TMPL="$HERE/scripts/qflix-dash/qflix-dash.nginx.conf.tmpl"
[ -r "$TMPL" ] || { echo "missing template: $TMPL"; exit 1; }

# Render @@PORT@@ locally; @@LOGFMT@@ needs the box and is resolved remotely.
# base64 -w0 so the whole block survives as one shell-safe env var.
BLOCK_B64="$(sed "s|@@PORT@@|$PORT|g" "$TMPL" | base64 -w0)"

ssh -o BatchMode=yes -o ConnectTimeout=20 "quadstronaut@$HOST" \
    PORT="$PORT" BLOCK_B64="$BLOCK_B64" 'bash -l -s' <<'EOS'
set -uo pipefail
PREFIX=$HOME/.apps/nginx
DEF=$PREFIX/sites-available/default
NGCONF=$PREFIX/nginx.conf
ACCESS=$PREFIX/logs/qflix-dash.access.log
[ -f "$DEF" ] || { echo "no default site at $DEF"; exit 1; }

NGINX=""
for b in $PREFIX/sbin/nginx $HOME/bin/nginx $(command -v nginx 2>/dev/null); do
  [ -x "$b" ] && NGINX="$b" && break
done
[ -n "$NGINX" ] || { echo "nginx binary not found"; exit 1; }

BLK=$(mktemp)
printf '%s' "$BLOCK_B64" | base64 -d > "$BLK" || { echo "block decode failed"; exit 1; }

# Only reference log_format `main` if the panel's nginx.conf still declares it.
if grep -qE '^[[:space:]]*log_format[[:space:]]+main[[:space:]]' "$NGCONF"; then
  LOGFMT=main
else
  LOGFMT=combined   # nginx built-in, always valid, but loses X-Forwarded-For
fi
sed -i "s|@@LOGFMT@@|$LOGFMT|g" "$BLK"
echo "log_format: $LOGFMT"

BAK=$DEF.bak.qflix.$(date +%s)
cp "$DEF" "$BAK"

python3 - "$DEF" "$BLK" <<'PY'
import pathlib, re, sys

target, blkfile = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
text = target.read_text()

# The .tmpl is repo documentation first: strip its comment preamble and keep
# only the directive body, so the deployed file stays readable on the box.
lines = blkfile.read_text().splitlines()
start = next(i for i, l in enumerate(lines) if l.startswith("location "))
body = "\n".join(("    " + l).rstrip() for l in lines[start:])

block = (
    "    # manitoba-qflix-dash-root\n"
    "    # QFlix-owned. Source of truth:\n"
    "    #   scripts/qflix-dash/qflix-dash.nginx.conf.tmpl (edit there, re-run phase 91)\n"
    "    # The access_log/error_log below intentionally override this server\n"
    "    # block's 'access_log off; error_log /dev/null;' -- those two lines are\n"
    "    # panel-templated, do NOT edit them.\n"
    + body
)

# The block is injected via a lambda, never as a replacement STRING. re.sub
# interprets backslash escapes in the replacement, so the day someone adds a
# regex-flavoured directive to the .tmpl (`location ~ \.php`, a `\$` in a
# rewrite) a plain-string replacement would silently mangle or raise
# "bad escape". A lambda is substituted verbatim. Cheap insurance on a file
# whose corruption reloads nginx into a 500 for every public service.
put = lambda _m: block

# Upgrade path: marker already present -> swap the whole marked block. Without
# this the script could never change an already-cut-over box. As of 2026-08-19
# the live box IS in that state: the marker is at sites-available/default:38
# with no log directives inside, so this is the path the logging fix takes.
marked = re.compile(
    r'[ \t]*# manitoba-qflix-dash-root\n(?:[ \t]*#.*\n)*[ \t]*location / \{[\s\S]*?\n[ \t]*\}'
)
if marked.search(text):
    text, n = marked.subn(put, text, count=1)
    assert n == 1, "marker present but block unparseable"
    print("mode=upgrade")
else:
    # First cutover: drop the Homarr root redirect, take over the autoindex root.
    text, n1 = re.subn(
        r'[ \t]*# manitoba-homarr-root-redirect\n[ \t]*location = / \{[\s\S]*?\n[ \t]*\}\n',
        '', text)
    text, n2 = re.subn(
        r'[ \t]*location / \{\n[ \t]*autoindex on;[\s\S]*?\n[ \t]*\}', put, text, count=1)
    assert n2 == 1, "cutover: homarr=%d root=%d" % (n1, n2)
    print("mode=cutover")

target.write_text(text)
PY
RC=$?
rm -f "$BLK"
if [ "$RC" -ne 0 ]; then
  echo "EDIT FAILED"; cp "$BAK" "$DEF"; rm -f "$BAK"; exit 1
fi

# Byte-identical render == genuine no-op. Cheaper and far more honest than the
# old "grep for the marker and skip", which could not upgrade anything.
if cmp -s "$BAK" "$DEF"; then
  echo "[skip] dash root already current (no diff)"; rm -f "$BAK"; exit 0
fi

# nginx -t MUST pass before any reload. It also creates/opens the two new log
# files, so an unwritable logs/ dir surfaces here instead of at reload time.
TOUT=$("$NGINX" -t -p "$PREFIX/" -c nginx.conf 2>&1)
if ! echo "$TOUT" | grep -q "syntax is ok" || echo "$TOUT" | grep -q emerg; then
  echo "CONFIG INVALID — not reloading:"; echo "$TOUT" | sed 's/^/  /'
  cp "$BAK" "$DEF"; exit 1
fi
echo "nginx -t: syntax ok"

# Baseline the access log so the post-reload check proves OUR probe landed,
# rather than that some file merely exists.
BEFORE=0; [ -f "$ACCESS" ] && BEFORE=$(stat -c %s "$ACCESS")

app-nginx restart >/dev/null 2>&1 || {
  echo "RELOAD FAILED"; cp "$BAK" "$DEF"; app-nginx restart >/dev/null 2>&1; exit 1; }
sleep 2

H="$(cat ~/secrets/seedbox.host)"
CODE=$(curl -sk -o /dev/null -w "%{http_code}" -H "Host: $H" http://127.0.0.1:17040/)
MARK=$(curl -sk -H "Host: $H" http://127.0.0.1:17040/ | grep -o data-qflix-dash | head -1)
AFTER=0; [ -f "$ACCESS" ] && AFTER=$(stat -c %s "$ACCESS")

if [ "$CODE" != "200" ] || [ "$MARK" != "data-qflix-dash" ]; then
  echo "VERIFY FAILED ($CODE/$MARK) — rollback"
  cp "$BAK" "$DEF"; app-nginx restart >/dev/null 2>&1; exit 1
fi
if [ "$AFTER" -le "$BEFORE" ]; then
  echo "LOGGING VERIFY FAILED: $ACCESS did not grow ($BEFORE -> $AFTER) — rollback"
  cp "$BAK" "$DEF"; app-nginx restart >/dev/null 2>&1; exit 1
fi

echo "OK: root 200 + dashboard marker + access log grew $BEFORE -> $AFTER bytes"
echo "backup: $BAK"
echo "logs:   $ACCESS"
echo "        $PREFIX/logs/qflix-dash.error.log"
EOS
