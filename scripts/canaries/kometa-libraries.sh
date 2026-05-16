#!/usr/bin/env bash
# Kometa libraries config-drift canary.
#
# Pulls the live Plex library list and the kometa config.yml `libraries:`
# keys; FAILs if any kometa-configured library is missing from Plex.
# Catches the recurrence of the May-2026 incident where the Plex libraries
# were renamed (Pirate Movies → QFlix - Movies, etc.) and the kometa
# config was left out of sync, so kometa failed silently every run with
# "Plex Library 'Pirate TV Shows' not found".
#
# Stage labels (printed to stderr on failure → Kuma `msg=`):
#   kometa-config-missing      — config.yml not found on seedbox
#   plex-up-fail               — Plex /identity returned non-200
#   plex-libraries-fetch-fail  — /library/sections non-200 or unparseable
#   kometa-config-parse-fail   — could not extract libraries from config.yml
#   library-drift              — kometa references libraries not in Plex
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
KOMETA_CFG=$HOME/.apps/kometa/config/config.yml
if [ ! -f "$KOMETA_CFG" ]; then
  printf "STAGE=kometa-config-missing msg=no-config-at-%s\n" "$KOMETA_CFG" >&2
  exit 1
fi

PLEX_TOKEN=$(cat ~/secrets/plex.token 2>/dev/null)
PLEX_HOST=$(cat ~/secrets/plex.host 2>/dev/null)
PLEX_PORT=$(cat ~/secrets/plex.port 2>/dev/null)
if [ -z "$PLEX_TOKEN" ] || [ -z "$PLEX_HOST" ] || [ -z "$PLEX_PORT" ]; then
  printf "STAGE=plex-up-fail msg=missing-plex-secrets\n" >&2
  exit 1
fi

PLEX_BASE="http://${PLEX_HOST}:${PLEX_PORT}"
H=$(curl -s -o /dev/null -w "%{http_code}" -m 5 "${PLEX_BASE}/identity" 2>/dev/null || echo 000)
if [ "$H" != "200" ]; then
  printf "STAGE=plex-up-fail msg=identity-http-%s\n" "$H" >&2
  exit 1
fi

SECTIONS_FILE=$(mktemp -t kometa-canary-sections.XXXXXX)
trap "rm -f $SECTIONS_FILE" EXIT
if ! curl -sf -m 8 -H "Accept: application/json" \
    "${PLEX_BASE}/library/sections?X-Plex-Token=${PLEX_TOKEN}" \
    -o "$SECTIONS_FILE" 2>/dev/null; then
  printf "STAGE=plex-libraries-fetch-fail msg=sections-curl-failed\n" >&2
  exit 1
fi

# Hand kometa config + Plex sections JSON to python via two argv paths.
# Stdlib only; the seedbox python3 has no yaml package, so we lex the
# libraries: block by hand. The heredoc and stdin would collide if we
# piped JSON in — passing both inputs as files avoids that.
DRIFT=$(python3 - "$KOMETA_CFG" "$SECTIONS_FILE" <<"PYEOF"
import json
import sys

cfg_path, sections_path = sys.argv[1], sys.argv[2]

try:
    with open(sections_path, encoding="utf-8") as fh:
        data = json.load(fh)
except Exception as exc:
    print(f"STAGE=plex-libraries-fetch-fail msg=parse-json-failed-{exc}",
          file=sys.stderr)
    sys.exit(2)
mc = data.get("MediaContainer") or {}
directories = mc.get("Directory") or []
plex_titles = {d.get("title") for d in directories if d.get("title")}
if not plex_titles:
    print("STAGE=plex-libraries-fetch-fail msg=zero-libraries-from-plex",
          file=sys.stderr)
    sys.exit(2)

# 2. Kometa side — lex the libraries: block, collect 2-space-indented keys
# until we exit the block (top-level key or EOF). Simple, no yaml dep.
kometa_libs = []
in_libs = False
with open(cfg_path, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.rstrip("\n")
        if line.startswith("#") or not line.strip():
            continue
        if line == "libraries:":
            in_libs = True
            continue
        if not in_libs:
            continue
        # Exited libraries: a non-indented, non-comment line (next top-level key)
        if line and not line.startswith(" "):
            break
        # A 2-space-indented "Name:" is a library key.
        if line.startswith("  ") and not line.startswith("   "):
            inner = line[2:]
            if inner.endswith(":") and not inner.lstrip().startswith("-"):
                kometa_libs.append(inner[:-1].strip())

if not kometa_libs:
    print("STAGE=kometa-config-parse-fail msg=no-libraries-keys-found",
          file=sys.stderr)
    sys.exit(2)

missing = [lib for lib in kometa_libs if lib not in plex_titles]
# NB: every literal in this python block must use double-quotes — the
# whole heredoc is nested inside sshm "..." which is itself inside the
# outer sshm SINGLE-QUOTED bash string, and any apostrophe here would
# prematurely close that outer quote.
if missing:
    miss = ",".join(missing)
    have = ",".join(sorted(plex_titles))
    print(f"STAGE=library-drift msg=kometa-libs-not-in-plex-{miss}-plex-has-{have}",
          file=sys.stderr)
    sys.exit(1)

names = ",".join(kometa_libs)
print(f"PASS: kometa-libraries — {len(kometa_libs)} libs all match Plex ({names})")
PYEOF
)
EXIT=$?
echo "$DRIFT"
rm -f "$SECTIONS_FILE"
exit $EXIT
')
RC=$?
echo "$RES"
exit $RC
