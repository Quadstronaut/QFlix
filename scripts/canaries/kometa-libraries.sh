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

# Pick the most robust YAML parser available. Prefer the kometa venv
# (ruamel.yaml is guaranteed to handle anything kometa itself accepts);
# fall back to system python3 if pyyaml is installed; fall back to a
# hand-rolled lexer otherwise. NB: apostrophes anywhere inside this
# sshm SINGLE-quoted block would terminate the outer string prematurely.
KOMETA_VENV_PY=$HOME/.apps/kometa/venv/bin/python
if [ -x "$KOMETA_VENV_PY" ] && "$KOMETA_VENV_PY" -c "import ruamel.yaml" >/dev/null 2>&1; then
  PARSER_PY="$KOMETA_VENV_PY"
  PARSER_KIND="ruamel"
elif python3 -c "import yaml" >/dev/null 2>&1; then
  PARSER_PY="python3"
  PARSER_KIND="pyyaml"
else
  PARSER_PY="python3"
  PARSER_KIND="lex"
fi

DRIFT=$("$PARSER_PY" - "$KOMETA_CFG" "$SECTIONS_FILE" "$PARSER_KIND" <<"PYEOF"
import json
import sys

cfg_path, sections_path, parser_kind = sys.argv[1], sys.argv[2], sys.argv[3]

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


def _libs_via_ruamel(path: str):
    from ruamel.yaml import YAML  # type: ignore
    y = YAML(typ="safe")
    with open(path, encoding="utf-8") as fh:
        doc = y.load(fh)
    libs = (doc or {}).get("libraries") or {}
    return list(libs.keys()) if isinstance(libs, dict) else []


def _libs_via_pyyaml(path: str):
    import yaml  # type: ignore
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    libs = (doc or {}).get("libraries") or {}
    return list(libs.keys()) if isinstance(libs, dict) else []


def _libs_via_lex(path: str):
    # Fallback when no YAML library is present. Reads the libraries:
    # block expecting kometa canonical 2-space indent. Handles common
    # edge cases: trailing space on `libraries:`, indented comments,
    # blank lines, simple quoted keys. (Docstring intentionally avoided
    # because the whole script is wrapped in single-quoted sshm.)
    libs = []
    in_libs = False
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Detect entry into the libraries: block, tolerating trailing
            # whitespace, comments, or `libraries: ` (any value form is
            # kometa-invalid but we should still handle it gracefully).
            if not in_libs:
                bare = line.split("#", 1)[0].rstrip()
                if bare == "libraries:":
                    in_libs = True
                continue
            # A top-level (non-indented) key ends the libraries: block.
            if line and not line[0].isspace():
                break
            # 2-space-indented entry: library key.
            if line.startswith("  ") and not line.startswith("   "):
                inner = line[2:].split("#", 1)[0].rstrip()
                if inner.endswith(":") and not inner.lstrip().startswith("-"):
                    name = inner[:-1].strip()
                    # Strip surrounding quotes if present. Use chr() so
                    # no literal apostrophe appears in this source — the
                    # outer sshm wrapper is single-quoted bash.
                    if len(name) >= 2 and name[0] == name[-1] and name[0] in (chr(34), chr(39)):
                        name = name[1:-1]
                    if name:
                        libs.append(name)
    return libs


try:
    if parser_kind == "ruamel":
        kometa_libs = _libs_via_ruamel(cfg_path)
    elif parser_kind == "pyyaml":
        kometa_libs = _libs_via_pyyaml(cfg_path)
    else:
        kometa_libs = _libs_via_lex(cfg_path)
except Exception as exc:
    print(f"STAGE=kometa-config-parse-fail msg=parse-error-{type(exc).__name__}-{exc}",
          file=sys.stderr)
    sys.exit(2)

if not kometa_libs:
    print(f"STAGE=kometa-config-parse-fail msg=no-libraries-keys-found-parser-{parser_kind}",
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
