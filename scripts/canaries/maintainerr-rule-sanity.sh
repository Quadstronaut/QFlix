#!/usr/bin/env bash
# Maintainerr rule sanity canary.
#
# Why: an in-place API edit (or a corrupted backup restore) can flip a
# rule's threshold to 0 seconds + keepFor to 0 days — silently making
# Maintainerr's next daily cron mass-delete every item in every covered
# library. The 2026-05-21 incident sequence (operator-driven 60d→45d
# edit, partial-restore, rule body left at days=0/keep=0) is exactly
# the failure mode this canary catches.
#
# Detection (any of these fails the canary):
#   - Fewer than 4 active rules (operator expected 4: Movies, TV, Anime,
#     Anime Movies — one per Plex library)
#   - Rule threshold < MIN_THRESHOLD_DAYS (default 1)
#   - Collection deleteAfterDays < MIN_KEEP_DAYS (default 1)
#   - Rule name does NOT match expected "QFlix .* -<N>d" pattern (drift
#     detector — alerts on operator-renamed-but-not-recreated rules)
#
# Pure alert. No autonomous fix (recreating rules from a script is
# operator-decision: deletes existing rules + their collections, which
# may have queued items mid-flight).
#
# Stage labels:
#   STAGE=maintainerr-down          — API unreachable / auth failed
#   STAGE=mt-rules-count            — wrong number of active rules
#   STAGE=mt-rules-threshold-low    — at least one rule threshold below floor
#   STAGE=mt-rules-keep-low         — at least one collection keepFor below floor
#   STAGE=mt-rules-name-drift       — rule name doesn't match canonical scheme
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

EXPECTED_RULES=${QFLIX_MT_EXPECTED_RULES:-4}
MIN_THRESHOLD_DAYS=${QFLIX_MT_MIN_THRESHOLD_DAYS:-1}
MIN_KEEP_DAYS=${QFLIX_MT_MIN_KEEP_DAYS:-1}

RES=$(sshm "
set -uo pipefail
EXPECTED=${EXPECTED_RULES}; MIN_DAYS=${MIN_THRESHOLD_DAYS}; MIN_KEEP=${MIN_KEEP_DAYS}
HTPW=\$(cat ~/secrets/htpasswd.password)
MTKEY=\$(cat ~/secrets/maintainerr.key)
HOST=\$(cat ~/secrets/seedbox.host)
USERPART=\${HOST%%.*}; DOMAIN=\${HOST#*.}
BASE=\"https://maintainerr-\${USERPART}.\${DOMAIN}\"
BASIC=\$(printf 'quadstronaut:%s' \"\$HTPW\" | base64 -w0)

TMPF=\$(mktemp -t mt-sanity-XXXX.json)
trap 'rm -f \"\$TMPF\"' EXIT
curl -sk -m 10 -H \"X-Api-Key: \$MTKEY\" -H \"Authorization: Basic \$BASIC\" \"\$BASE/api/rules\" -o \"\$TMPF\"
if [ ! -s \"\$TMPF\" ] || head -c 1 \"\$TMPF\" | grep -q '<'; then
  echo \"STAGE=maintainerr-down msg=api-unreachable-or-html-body\" >&2
  exit 1
fi

# Parse via python — produce one line per check failure on stderr.
# Pass thresholds via env (heredoc-as-program with '\\''PYEND'\\'' = no shell expansion).
export EXPECTED MIN_DAYS MIN_KEEP TMPF
python3 <<'PYEND'
import json, sys, os, re
RAW = open(os.environ['TMPF']).read()
try:
    rules = json.loads(RAW)
except Exception as e:
    print(f'STAGE=maintainerr-down msg=json-parse-error-{e}', file=sys.stderr)
    sys.exit(1)

EXPECTED = int(os.environ.get('EXPECTED', '4'))
MIN_DAYS = int(os.environ.get('MIN_DAYS', '1'))
MIN_KEEP = int(os.environ.get('MIN_KEEP', '1'))

active = [g for g in rules if g.get('isActive')]
fails = []

if len(active) != EXPECTED:
    fails.append(('mt-rules-count', f'active={len(active)}-expected={EXPECTED}'))

# Canonical rule name pattern: 'QFlix <short>-<N>d' (e.g. QFlix Movies-60d)
name_re = re.compile(r'^QFlix\\s+.+-\\d+d\$')
bad_names = [g.get('name', '') for g in active if not name_re.match(g.get('name', ''))]
if bad_names:
    fails.append(('mt-rules-name-drift', 'names=' + '|'.join(bad_names)[:80]))

low_threshold = []
low_keep = []
for g in active:
    name = g.get('name', '<unnamed>')
    rs = g.get('rules', [])
    # Maintainerr stores rules as ruleJson string; parse the first one.
    secs = None
    for r in rs:
        try:
            rj = json.loads(r.get('ruleJson', '{}'))
            cv = rj.get('customVal', {})
            secs = int(cv.get('value', '0'))
            break
        except Exception:
            pass
    days = (secs or 0) // 86400
    if days < MIN_DAYS:
        low_threshold.append(f'{name}@{days}d')
    keep = (g.get('collection') or {}).get('deleteAfterDays', 0)
    if (keep or 0) < MIN_KEEP:
        low_keep.append(f'{name}@keep={keep}d')

if low_threshold:
    fails.append(('mt-rules-threshold-low', 'below-floor-' + ','.join(low_threshold)[:80]))
if low_keep:
    fails.append(('mt-rules-keep-low', 'below-floor-' + ','.join(low_keep)[:80]))

if fails:
    stage, msg = fails[0]
    extra = ';'.join(f'{s}:{m}' for s, m in fails[1:])
    if extra:
        msg = f'{msg};also-{extra}'
    print(f'STAGE={stage} msg={msg}', file=sys.stderr)
    sys.exit(1)

print(f'PASS: {len(active)} rules active, all names canonical, all thresholds >={MIN_DAYS}d, all keep >={MIN_KEEP}d')
PYEND
RC=\$?
exit \$RC
") || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC
