#!/usr/bin/env bash
# seerr-commit-tag canary (QFLX-58): the commitTag baked into Seerr's CLIENT
# bundle must equal the commitTag the SERVER reports at /api/v1/status.
#
# WHY THIS EXISTS
# Seerr's StatusChecker polls /api/v1/status every 60s and, for any build that
# is not a dev build, opens the "Seerr Updated - Reload Seerr" modal whenever
# the server's commitTag differs from the value compiled into the client. The
# compiled fragment is literally `"local"!==a.commitTag`: the left side is the
# client's baked tag (Next.js inlines process.env.commitTag at build time),
# the right side is the live server answer. Reloading fetches the same bundle,
# so the modal comes straight back: an endless loop for every member.
# QFLX-53 was exactly that, and it was REPORTED BY A MEMBER - nothing here
# saw it. The server reads its tag from committag.json at runtime
# (dist/utils/appVersion.js) while the client tag is frozen at `next build`,
# so any artifact assembled from two different builds, or a committag.json
# rewritten after the build, splits them. That only moves on a deploy or an
# upgrade, hence a cheap 6-hourly cadence.
#
# HOW
#   server tag: GET http://127.0.0.1:$(<secrets/seerr.port)/api/v1/status -> .commitTag
#   client tag: one grep over .next/static/**/*.js for a string literal
#               compared with `!==`/`!=` against `<id>.commitTag` (either
#               operand order, plus SWC's optional-chaining lowering
#               `"X"!==(null==a?void 0:a.commitTag)`). Every such literal IS
#               the baked value, except a hand-written `"local"` dev-build
#               check, so a non-"local" literal wins over "local"; two
#               DIFFERENT non-"local" literals is ambiguous and never green.
#   verdict:    client == server -> PASS; otherwise mismatch.
#
# Read-only: never touches Seerr. Runs LOCALLY on the box (Seerr is a loopback
# port on the host this timer fires on), so no sshm hop - which is also what
# lets the test drive the real script against fixture chunks + a fake server.
#
# Stage labels (stderr -> Kuma msg=):
#   seerr-commit-tag-mismatch           exit 1  client and server tags differ (modal loop)
#   seerr-commit-tag-status-unreadable  exit 2  no port file / status down / no sane commitTag
#   seerr-commit-tag-bundle-unreadable  exit 2  no .next/static, grep error, or no comparison found
#   seerr-commit-tag-bundle-ambiguous   exit 2  >1 distinct non-"local" literal in the bundle
# Exit 2 = could not assert. An unreadable side is never a clean pass.
#
# Overrides (tests): MANITOBA_SECRETS (default ~/secrets),
#   QFLIX_CANARY_SCT_APP_DIR (default ~/.apps/seerr/bin/current),
#   QFLIX_CANARY_SCT_URL (full status URL; skips the port file),
#   QFLIX_CANARY_SCT_TIMEOUT_S (default 10).
set -uo pipefail

MANITOBA_SECRETS="${MANITOBA_SECRETS:-$HOME/secrets}"
APP_DIR="${QFLIX_CANARY_SCT_APP_DIR:-$HOME/.apps/seerr/bin/current}"
STATUS_URL="${QFLIX_CANARY_SCT_URL:-}"
TIMEOUT_S="${QFLIX_CANARY_SCT_TIMEOUT_S:-10}"

# A tag is "sane" when it is short and shell/Kuma-safe: "local", a 40-hex
# commit, or a version-ish token. Anything else is treated as unreadable so a
# garbage body can never be compared (or echoed into msg=) as if it were a tag.
TAG_RE='^[A-Za-z0-9._-]{1,64}$'

fail() {  # fail <rc> <stage> <msg>
  printf 'STAGE=%s msg=%s\n' "$2" "$3" >&2
  exit "$1"
}

# --- 1. server tag ----------------------------------------------------------
if [ -z "$STATUS_URL" ]; then
  PORT=$(tr -d '[:space:]' < "$MANITOBA_SECRETS/seerr.port" 2>/dev/null)
  case "$PORT" in
    ''|*[!0-9]*) fail 2 seerr-commit-tag-status-unreadable "no-usable-port-in-$MANITOBA_SECRETS/seerr.port" ;;
  esac
  STATUS_URL="http://127.0.0.1:$PORT/api/v1/status"
fi

BODY=$(curl -fsS -m "$TIMEOUT_S" "$STATUS_URL" 2>/dev/null) \
  || fail 2 seerr-commit-tag-status-unreadable "status-endpoint-unreachable-or-non-2xx"

# json parse, not a regex: a body that merely CONTAINS "commitTag" somewhere
# (an error page, a nested field) must not pass for the top-level answer.
SERVER_TAG=$(printf '%s' "$BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
t = d.get("commitTag") if isinstance(d, dict) else None
print(t if isinstance(t, str) else "")
' 2>/dev/null)
printf '%s' "$SERVER_TAG" | grep -qE "$TAG_RE" \
  || fail 2 seerr-commit-tag-status-unreadable "status-has-no-sane-commitTag"

# --- 2. client (baked) tag --------------------------------------------------
STATIC="$APP_DIR/.next/static"
[ -d "$STATIC" ] || fail 2 seerr-commit-tag-bundle-unreadable "no-.next/static-under-app-dir"

LIT='"[A-Za-z0-9._-]{1,64}"'
ID='[A-Za-z_$][A-Za-z0-9_$]*'
P_FWD="$LIT!==?$ID\\.commitTag"                              # "X"!==a.commitTag
P_REV="$ID\\.commitTag!==?$LIT"                              # a.commitTag!=="X"
P_OPT="$LIT!==?\\(null==?$ID\\?void 0:$ID\\.commitTag\\)"    # "X"!==(null==a?void 0:a.commitTag)

# One pass over the bundle; -o keeps only the comparison, never minified code.
# grep rc: 0 matched, 1 nothing matched, >1 a read error. A read error is
# "could not assert" even if some files matched - a partial read could have
# missed the very chunk that disagrees.
MATCHES=$(grep -rhoE --include='*.js' -e "$P_FWD" -e "$P_REV" -e "$P_OPT" "$STATIC" 2>/dev/null)
GRC=$?
[ "$GRC" -le 1 ] || fail 2 seerr-commit-tag-bundle-unreadable "grep-rc-$GRC-reading-.next/static"
LITERALS=$(printf '%s\n' "$MATCHES" | grep -oE "$LIT" | tr -d '"' | sort -u)
[ -n "$LITERALS" ] \
  || fail 2 seerr-commit-tag-bundle-unreadable "no-commitTag-comparison-in-.next/static(upstream-minifier-shape-changed?)"

NON_LOCAL=$(printf '%s\n' "$LITERALS" | grep -vx 'local' || true)
N_NON_LOCAL=$(printf '%s' "$NON_LOCAL" | grep -c . || true)
if [ "$N_NON_LOCAL" -gt 1 ]; then
  fail 2 seerr-commit-tag-bundle-ambiguous "bundle-compares-commitTag-against-$(printf '%s' "$NON_LOCAL" | tr '\n' ',' | sed 's/,$//')"
elif [ "$N_NON_LOCAL" -eq 1 ]; then
  CLIENT_TAG="$NON_LOCAL"
else
  CLIENT_TAG="local"
fi

# --- 3. verdict -------------------------------------------------------------
if [ "$CLIENT_TAG" != "$SERVER_TAG" ]; then
  fail 1 seerr-commit-tag-mismatch "client-baked=$CLIENT_TAG,server-status=$SERVER_TAG(members-see-Seerr-Updated-modal-loop)"
fi
echo "PASS: seerr-commit-tag - client bundle and /api/v1/status agree on commitTag=$SERVER_TAG"
exit 0
