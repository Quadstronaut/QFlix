#!/usr/bin/env bash
# entitlement-service canary: is the thing the money depends on actually alive,
# authenticated (both ways), honouring its contract, still carrying patron
# data, and has the money path EVER demonstrated a real success?
#
# WHY THIS EXISTS
# ---------------
# manitoba-maint-entitlement has a dead-man ("QFlix Entitlement Gate",
# 15-minute timer, 1h heartbeat override). That watches the GATE. Nothing
# watched the SERVICE the gate reads, and the gate is deliberately built so that
# a broken service is INVISIBLE from the gate's own health:
#
#   lib/entitlement.py grades every network failure, every 401, every 5xx, and
#   every malformed body as UNKNOWN -- and UNKNOWN neither grants nor revokes.
#   That asymmetry is the correct and load-bearing safety property (an API
#   outage must freeze this system, never drain it). But its consequence is that
#   the gate keeps running, keeps pushing green every 15 minutes, and quietly
#   stops provisioning anybody, forever, with no signal anywhere.
#
# "Frozen" is the safe direction, not a safe STATE. A frozen gate means new
# paying members are never granted access -- and nobody files a ticket saying
# "I paid and got in", they just don't get in and give up. This canary is the
# only thing that can say the freeze happened.
#
# FIVE LEGS, escalating from "is it there" to "has it EVER actually worked"
#
#   1. liveness    /healthz returns 200.
#   2. auth-neg     an UNAUTHENTICATED lookup must 401/403. Added 2026-08-07
#                  (SPEC AC-09). If the entitlement API ever fails open,
#                  anyone can read entitlement status for any address with no
#                  key at all -- a PII exposure with no local change to blame,
#                  and the mirror image of leg 3 below.
#   3. auth-pos     an AUTHENTICATED lookup does not 401/403. A rejected key
#                  is the single most likely silent killer here: it is
#                  indistinguishable, downstream, from a service outage, and
#                  key rotation on the Starhold side would cause it with no
#                  local change to blame.
#   4. contract    the 200 body is a JSON object carrying a BOOLEAN `entitled`.
#                  lib/entitlement.py refuses to grade a 200 that lacks the
#                  field -- "a 200 response has no 'entitled' field" is a
#                  contract violation, not a no -- precisely so a server-side
#                  refactor cannot silently revoke everybody. That refusal is
#                  correct and it is also SILENT: it just produces UNKNOWNs.
#                  This leg makes it audible.
#   5. oracle      has the money path EVER demonstrated a success, per SPEC
#                  section 3's verdict table (lib/payer_oracle.judge()).
#
# LEGS 2, 3 AND 4 USE A SYNTHETIC ADDRESS, NEVER A MEMBER'S. `.invalid` is
# reserved by RFC 2606 and can never be a real patron, so the probes prove
# auth and contract without putting a member's address into a script that
# lives in a PUBLIC repository, into this canary's Kuma message, or into
# journald. Leg 5 necessarily reads real declared-payer addresses at runtime
# (via qflix-entitlement.py --oracle-check); every one of them is masked
# before it can leave that process -- see lib/payer_oracle.py's PII
# discipline section and the never-publish-member-data operator directive.
#
# LEG 5, AND WHY IT IS THE ONLY LEG THAT CAN CATCH "NEVER WORKED"
# -----------------------------------------------------------------
# Legs 1-4 prove the HTTP service answers and honours its contract. They
# cannot prove the money path has ever actually moved a real person from
# "subscribed" to "has access" -- the service's answer for a patron it has
# never heard of is byte-identical to its answer for one it has lost:
#
#     {"entitled": false, "reason": "unknown"}
#
# lib/payer_oracle.judge() is the single implementation (SPEC section 3) of
# the layered discriminator that answers this: a declared-payer clock that
# works from day one with zero new credentials, a bulk cross-check
# (GET /v1/entitlements) once the operator grants the QFlix key the 'bulk'
# scope, and a forgotten-patron check strengthened to fire on ANY forgotten
# ever-entitled account, not only when every one of them is lost at once.
# This leg DELEGATES to that module (via `qflix-entitlement.py
# --oracle-check`) rather than re-implementing the table here, because a
# policy replicated across two surfaces drifts by default -- the exact lesson
# the REA prompt-rule bijection guard already exists to enforce elsewhere in
# this repo.
#
# Today this leg is RED from the very first run, and correctly so: the money
# path has never demonstrated a success and the bulk scope has not been
# granted yet. That is the true state of the system, not a bug in the canary.
#
# EXIT CODES
#   0 - service alive, authenticated both ways, honouring its contract; the
#       payer oracle reads a non-red verdict (PROVEN / PROVEN_UPSTREAM /
#       DORMANT / SETTLING)
#   1 - service down / auth failed either direction / contract violated / the
#       payer oracle reads a red verdict (DEAD / MISMATCH / UNPROVEN_BLIND /
#       UNPROVEN_EMPTY)
#   2 - could not assert: no key on the box, curl missing, or
#       qflix-entitlement.py --oracle-check itself could not run (bad roster,
#       no entitlement client)
#
# Lives on the seedbox at ~/scripts/canaries/entitlement-service.sh (deployed by
# 240-maintenance-install.sh). Invoked by manitoba-maint-canary-entitlement-
# service, which pushes status=up/down to Kuma monitor "Canary Entitlement
# Service".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail

command -v curl >/dev/null 2>&1 || {
  printf "STAGE=ent-no-curl msg=curl-not-on-PATH-cannot-probe\n" >&2; exit 2; }

KEYF="$HOME/secrets/entitlement.key"
[ -r "$KEYF" ] || {
  printf "STAGE=ent-no-key msg=%s-unreadable-gate-would-401-every-lookup\n" "$KEYF" >&2
  exit 2; }
KEY=$(tr -d "\r\n" < "$KEYF")
[ -n "$KEY" ] || {
  printf "STAGE=ent-empty-key msg=entitlement.key-is-empty\n" >&2; exit 2; }

BASE=$(tr -d "\r\n" < "$HOME/secrets/entitlement.url" 2>/dev/null)
[ -n "$BASE" ] && BASE="${BASE%/}" || BASE="https://entitlements.quadstronix.dev"

# --- leg 1: liveness ------------------------------------------------------
# /healthz takes no auth by design, so this leg isolates "service down" from
# "key rejected". Without the split, a rotated key reads as an outage and the
# operator restarts a service that was never broken.
HCODE=$(curl -s -o /dev/null -m 20 -w "%{http_code}" "$BASE/healthz" 2>/dev/null)
CURL_RC=$?

# A curl failure means no HTTP response ever arrived -- DNS, TCP or TLS died
# before the service was reached. That is NOT the same fault as "the service
# answered badly", and collapsing the two costs real time: on 2026-09-01 the
# app was healthy on 127.0.0.1:9200 the whole while, but the public hostname
# had been renamed out from under us, so the TLS handshake failed and this
# canary reported "service-down" for 19 hours. The operator would have
# restarted a service that never stopped. Name the transport, and say to check
# the URL BEFORE touching the service.
if [ "$CURL_RC" -ne 0 ]; then
  case "$CURL_RC" in
    6)     WHY="dns-cannot-resolve-host" ;;
    7)     WHY="tcp-connection-refused" ;;
    28)    WHY="timed-out" ;;
    35|60) WHY="tls-handshake-failed-hostname-probably-not-served-here" ;;
    *)     WHY="transport-error" ;;
  esac
  printf "STAGE=ent-unreachable msg=%s-curl-rc-%s-at-%s-verify-secrets-entitlement.url-before-restarting-anything\n" \
    "$WHY" "$CURL_RC" "$BASE" >&2
  exit 1
fi

if [ "$HCODE" != "200" ]; then
  printf "STAGE=ent-service-down msg=healthz-HTTP-%s-gate-freezes-silently-no-member-is-provisioned\n" \
    "${HCODE:-000}" >&2
  exit 1
fi

# RFC 2606 reserves .invalid. This address can never be a real patron, so a
# member address never enters this script, its Kuma message, or journald.
PROBE="qflix-canary-probe@qflix.invalid"

# --- leg 2: auth-negative control ------------------------------------------
# An UNAUTHENTICATED lookup must be REJECTED. If the service ever fails open
# here, entitlement status for any address is readable with no key at all --
# a PII exposure this repo has no other way to detect, and the mirror image
# of leg 3 (a good key wrongly rejected).
NOAUTH_CODE=$(curl -s -o /dev/null -m 20 -w "%{http_code}" \
  -H "Accept: application/json" "$BASE/v1/entitlement?email=$PROBE" 2>/dev/null)
case "$NOAUTH_CODE" in
  401|403) ;;
  *)
    printf "STAGE=ent-auth-not-enforced msg=unauthenticated-lookup-answered-HTTP-%s-not-401-or-403-entitlement-status-is-readable-with-no-key\n" \
      "${NOAUTH_CODE:-000}" >&2
    exit 1 ;;
esac

# --- leg 3: auth-positive, and leg 4: contract, on the same probe ---------
BODY=$(mktemp) || { printf "STAGE=ent-no-tmp msg=mktemp-failed\n" >&2; exit 2; }
trap "rm -f \"$BODY\"" EXIT
PCODE=$(curl -s -o "$BODY" -m 25 -w "%{http_code}" \
  -H "Authorization: Bearer $KEY" -H "Accept: application/json" \
  "$BASE/v1/entitlement?email=$PROBE" 2>/dev/null)

case "$PCODE" in
  401|403)
    printf "STAGE=ent-key-rejected msg=lookup-HTTP-%s-key-rejected-every-member-grades-UNKNOWN-gate-frozen\n" \
      "$PCODE" >&2
    exit 1 ;;
  200) ;;
  *)
    printf "STAGE=ent-lookup-http msg=lookup-HTTP-%s-on-a-healthy-healthz\n" "${PCODE:-000}" >&2
    exit 1 ;;
esac

CONTRACT=$(python3 - "$BODY" <<PY 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    print("BAD malformed-JSON-%s" % type(e).__name__); raise SystemExit(0)
if not isinstance(d, dict):
    print("BAD body-is-%s-expected-object" % type(d).__name__); raise SystemExit(0)
if "entitled" not in d:
    print("BAD 200-has-no-entitled-field"); raise SystemExit(0)
if not isinstance(d["entitled"], bool):
    print("BAD entitled-is-%s-expected-bool" % type(d["entitled"]).__name__); raise SystemExit(0)
print("OK")
PY
)
if [ "${CONTRACT%% *}" != "OK" ]; then
  printf "STAGE=ent-contract-violation msg=%s-lib/entitlement.py-would-grade-every-member-UNKNOWN-silently\n" \
    "${CONTRACT#BAD }" >&2
  exit 1
fi

# --- leg 5: the payer oracle -----------------------------------------------
# Delegates to lib/payer_oracle.judge() via qflix-entitlement.py
# --oracle-check -- see the module header for why this leg must not
# re-implement the SPEC section 3 verdict table in bash. --oracle-check is
# read-only: no Plex, no Seerr, no state mutation, no Kuma push of its own.
GATE="$HOME/scripts/maint/qflix-entitlement.py"
if [ ! -r "$GATE" ]; then
  printf "STAGE=ent-oracle-not-deployed msg=%s-missing-cannot-run-the-oracle-leg\n" "$GATE" >&2
  exit 2
fi

ORACLE_OUT=$(python3 "$GATE" --oracle-check --settle-days 2 2>&1)
ORACLE_RC=$?

case "$ORACLE_RC" in
  0)
    VERDICT_LINE=$(printf "%s\n" "$ORACLE_OUT" | grep "^VERDICT=" | head -1)
    printf "PASS: entitlement-service - alive, authenticated both ways, contract OK; oracle %s\n" \
      "${VERDICT_LINE#VERDICT=}"
    exit 0 ;;
  2)
    VERDICT_LINE=$(printf "%s\n" "$ORACLE_OUT" | grep "^VERDICT=" | head -1)
    DETAIL_LINE=$(printf "%s\n" "$ORACLE_OUT" | grep "^DETAIL=" | head -1)
    DETAIL_FLAT=$(printf "%s" "${DETAIL_LINE#DETAIL=}" | tr " " "-")
    printf "STAGE=ent-oracle-red msg=%s-%s\n" "${VERDICT_LINE#VERDICT=}" "$DETAIL_FLAT" >&2
    exit 1 ;;
  *)
    printf "STAGE=ent-oracle-could-not-assert msg=oracle-check-exited-%s-see-the-durable-log-at-.opt/maint/entitlement\n" \
      "$ORACLE_RC" >&2
    exit 2 ;;
esac
')
RC=$?
echo "$RES"
exit $RC
