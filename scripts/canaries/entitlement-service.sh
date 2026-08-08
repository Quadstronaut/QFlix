#!/usr/bin/env bash
# entitlement-service canary: is the thing the money depends on actually alive,
# authenticated, honouring its contract, and still carrying patron data?
#
# WHY THIS EXISTS
# ---------------
# manitoba-maint-entitlement has a dead-man (Kuma "QFlix Entitlement Gate",
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
# FOUR LEGS, escalating from "is it there" to "does it still know anything"
#
#   1. liveness   /healthz returns 200.
#   2. auth       an authenticated lookup does not 401/403. A rejected key is
#                 the single most likely silent killer here: it is indis-
#                 tinguishable, downstream, from a service outage, and key
#                 rotation on the Starhold side would cause it with no local
#                 change to blame.
#   3. contract   the 200 body is a JSON object carrying a BOOLEAN `entitled`.
#                 lib/entitlement.py refuses to grade a 200 that lacks the
#                 field -- "a 200 response has no 'entitled' field" is a
#                 contract violation, not a no -- precisely so a server-side
#                 refactor cannot silently revoke everybody. That refusal is
#                 correct and it is also SILENT: it just produces UNKNOWNs.
#                 This leg makes it audible.
#   4. oracle     has the service FORGOTTEN patrons it used to know?
#
# LEG 2 AND 3 USE A SYNTHETIC ADDRESS, NEVER A MEMBER'S. `.invalid` is reserved
# by RFC 2606 and can never be a real patron, so the probe proves auth and
# contract without putting a member's address into a script that lives in a
# PUBLIC repository, into this canary's Kuma message, or into journald. Leg 4
# necessarily reads real addresses out of the gate's state file at runtime; it
# masks every one of them before printing. No unmasked address may ever leave
# this script -- see the never-publish-member-data operator directive.
#
# LEG 4, AND WHY IT IS THE ONLY LEG THAT CAN CATCH A DEAD SYNC
# ------------------------------------------------------------
# Legs 1-3 prove the HTTP service answers. They cannot prove its Patreon
# projection still contains anyone, because the service's answer for a patron
# it has lost looks exactly like its answer for someone who never subscribed:
#
#     {"entitled": false, "reason": "unknown"}
#
# So the canary needs an oracle -- an address it KNOWS was entitled, whose
# sudden absence means the pipe broke rather than the person left. The gate
# already writes one down: lib/access_state.py persists `last_entitled_at` per
# account specifically because "the entitlement API is a projection of NOW with
# no history at all". Any account with that field set was, at some point,
# really entitled.
#
# The discrimination is the `reason` field, and it is exact:
#
#     status=former_patron, reason unset  -> they cancelled. Normal. Not a fault.
#     reason=unknown                      -> the service has NEVER HEARD of this
#                                            address. For an account that was
#                                            demonstrably entitled before, that
#                                            is not a lapse. Data was lost.
#
# One such account could be an upstream email change. ALL of them going
# `reason=unknown` at once is a dead or wiped projection, and if the gate were
# armed it would grade every one of them a revoke. That is the mass-eviction
# scenario lib/entitlement.py's whole three-valued design exists to prevent --
# except this failure mode arrives as a well-formed 200, so the UNKNOWN grading
# never engages. Hence a separate check.
#
# WHY A DORMANT ORACLE IS A PASS AND NOT AN exit-2
# ------------------------------------------------
# Until somebody has been entitled at least once, the ever-entitled set is
# empty and leg 4 asserts nothing. The house rule is that could-not-assert must
# never read as clean -- but the rule's target is a check made VACUOUSLY TRUE by
# a broken input, and that is not this. There is no patron to forget, so there
# is no fault this leg could miss. cron-liveness exits 2 on a zero-length
# declaration set because crontab jobs demonstrably DO run and an empty ledger
# means the ledger is broken; here, zero-ever-entitled is a true and expected
# fact about a system nobody has subscribed to yet.
#
# The distinction that DOES matter is broken-vs-empty on the state file itself:
# unreadable or unparseable state.json is exit 2, because then "zero ever
# entitled" is empty-because-broken and the oracle is not dormant, it is blind.
# Leg 4 announces which of the two it is in every PASS message, so a permanently
# dormant oracle is visible rather than mistaken for a passing check.
#
# EXIT CODES
#   0 - service alive, authenticated, honouring its contract; oracle either
#       satisfied or legitimately dormant (message says which)
#   1 - service down / key rejected / contract violated / every known-entitled
#       account forgotten
#   2 - could not assert: no key on the box, curl missing, or the gate's state
#       file exists but cannot be read or parsed
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
[ -n "$BASE" ] && BASE="${BASE%/}" || BASE="https://entitlements.starhold.app"

# --- leg 1: liveness ------------------------------------------------------
# /healthz takes no auth by design, so this leg isolates "service down" from
# "key rejected". Without the split, a rotated key reads as an outage and the
# operator restarts a service that was never broken.
HCODE=$(curl -s -o /dev/null -m 20 -w "%{http_code}" "$BASE/healthz" 2>/dev/null)
if [ "$HCODE" != "200" ]; then
  printf "STAGE=ent-service-down msg=healthz-HTTP-%s-gate-freezes-silently-no-member-is-provisioned\n" \
    "${HCODE:-000}" >&2
  exit 1
fi

# --- legs 2+3: auth and contract, on a synthetic address ------------------
# RFC 2606 reserves .invalid. This address can never be a real patron, so a
# member address never enters this script, its Kuma message, or journald.
PROBE="qflix-canary-probe@qflix.invalid"
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

# --- leg 4: the oracle ----------------------------------------------------
STATE="$HOME/.opt/maint/entitlement/state.json"
if [ ! -e "$STATE" ]; then
  # No state file at all is legitimate before the gate has ever completed a
  # run. Legs 1-3 already passed, so the service is provably fine; say so and
  # name the oracle as dormant rather than inventing a fault.
  printf "PASS: entitlement-service - alive, authenticated, contract OK; oracle DORMANT (no state file yet)\n"
  exit 0
fi

ORACLE=$(python3 - "$STATE" <<PY 2>&1
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    print("UNREADABLE %s" % type(e).__name__); raise SystemExit(0)
accts = d.get("accounts")
if not isinstance(accts, dict):
    print("UNREADABLE no-accounts-object"); raise SystemExit(0)
ever = sorted(e for e, v in accts.items()
              if isinstance(v, dict) and v.get("last_entitled_at"))
print("EVER %d %d" % (len(ever), len(accts)))
for e in ever:
    print(e)
PY
)
HEAD=$(printf "%s\n" "$ORACLE" | head -1)
if [ "${HEAD%% *}" = "UNREADABLE" ]; then
  # The file exists but will not parse. "Zero ever-entitled" is now empty-
  # because-broken, and the oracle is blind rather than dormant.
  printf "STAGE=ent-state-unreadable msg=%s-cannot-tell-dormant-oracle-from-blind-one\n" \
    "${HEAD#UNREADABLE }" >&2
  exit 2
fi

NEVER_ENT=$(printf "%s" "$HEAD" | awk "{print \$2}")
TRACKED=$(printf "%s" "$HEAD" | awk "{print \$3}")

if [ "${NEVER_ENT:-0}" -eq 0 ]; then
  printf "PASS: entitlement-service - alive, authenticated, contract OK; oracle DORMANT (0/%s accounts ever entitled - nobody has subscribed yet, so there is no patron the service could have forgotten)\n" \
    "${TRACKED:-0}"
  exit 0
fi

# At least one account was really entitled once. Ask about each: has the
# service forgotten it (reason=unknown), or did the person simply cancel?
FORGOTTEN=0
CHECKED=0
EG=""
while IFS= read -r addr; do
  [ -z "$addr" ] && continue
  case "$addr" in EVER*|UNREADABLE*) continue ;; esac
  CHECKED=$((CHECKED + 1))
  RB=$(curl -s -m 25 -H "Authorization: Bearer $KEY" -H "Accept: application/json" \
       "$BASE/v1/entitlement?email=$addr" 2>/dev/null)
  VERD=$(printf "%s" "$RB" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print(\"ERR\"); raise SystemExit(0)
if not isinstance(d,dict): print(\"ERR\"); raise SystemExit(0)
if d.get(\"entitled\") is True: print(\"YES\")
elif d.get(\"reason\")==\"unknown\": print(\"FORGOTTEN\")
else: print(\"LAPSED\")
" 2>/dev/null)
  if [ "$VERD" = "FORGOTTEN" ]; then
    FORGOTTEN=$((FORGOTTEN + 1))
    # Mask before it can reach Kuma, journald or a terminal. Two leading
    # characters and the domain is enough to act on and not enough to publish.
    [ -z "$EG" ] && EG="$(printf "%s" "$addr" | cut -c1-2)***@$(printf "%s" "$addr" | sed "s/.*@//")"
  fi
done <<< "$(printf "%s\n" "$ORACLE" | tail -n +2)"

if [ "$CHECKED" -gt 0 ] && [ "$FORGOTTEN" -eq "$CHECKED" ]; then
  printf "STAGE=ent-sync-forgot-everyone msg=all-%s-known-entitled-account(s)-now-reason=unknown-first=%s-projection-lost-not-a-lapse\n" \
    "$CHECKED" "$EG" >&2
  exit 1
fi

printf "PASS: entitlement-service - alive, authenticated, contract OK; oracle LIVE (%s/%s ever-entitled account(s) checked, %s forgotten)\n" \
  "$CHECKED" "$TRACKED" "$FORGOTTEN"
exit 0
')
RC=$?
echo "$RES"
exit $RC
