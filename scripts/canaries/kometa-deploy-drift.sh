#!/usr/bin/env bash
# Kometa deploy-drift canary.
#
# Compares two sources both resident on the host this canary runs on:
#   - $ROOT/scripts/configure/55-kometa-install.sh — what the install
#     script would render today
#   - $HOME/.apps/kometa/config/config.yml — what is actually deployed
#
# Reports drift when their `libraries:` keysets differ. Detects:
#   - operator hand-edited the deployed config (added/removed libraries)
#   - install script updated locally but not redeployed
#
# Complements scripts/canaries/kometa-libraries.sh — that canary catches
# "kometa lib doesn't exist in Plex" (semantic); this one catches
# "kometa lib set doesn't match the install-script canonical set"
# (textual). Together they cover both directions of drift.
#
# Stage labels (stderr → Kuma `msg=`):
#   kometa-config-missing      — deployed config.yml not found
#   install-script-parse-fail  — install script not found or yields zero libraries
#   deploy-drift               — deployed lib set differs from install-script set
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

INSTALL_SCRIPT="$ROOT/scripts/configure/55-kometa-install.sh"
DEPLOYED_CFG="$HOME/.apps/kometa/config/config.yml"

if [ ! -f "$INSTALL_SCRIPT" ]; then
  printf "STAGE=install-script-parse-fail msg=no-install-script-at-%s\n" "$INSTALL_SCRIPT" >&2
  exit 1
fi
if [ ! -f "$DEPLOYED_CFG" ]; then
  printf "STAGE=kometa-config-missing msg=no-config-at-%s\n" "$DEPLOYED_CFG" >&2
  exit 1
fi

# Single python pass over both files — keeps the lexer logic in one
# place and avoids one of the two reads via sshm-to-localhost.
DRIFT=$(python3 - "$INSTALL_SCRIPT" "$DEPLOYED_CFG" <<"PYEOF"
import sys


def libs_from_yaml(path):
    """Extract the libraries: block 2-space-indented keys. Same lexer as
    scripts/canaries/kometa-libraries.sh's fallback path — tolerant of
    trailing whitespace, indented comments, blank lines, simple quoted
    keys. (No docstring inside this heredoc — the whole script lives
    inside a single-quoted sshm string at deploy time so apostrophes
    would terminate it. chr() avoids embedded quote literals.)"""
    out = []
    in_libs = False
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not in_libs:
                bare = line.split("#", 1)[0].rstrip()
                if bare == "libraries:":
                    in_libs = True
                continue
            if line and not line[0].isspace():
                break
            if line.startswith("  ") and not line.startswith("   "):
                inner = line[2:].split("#", 1)[0].rstrip()
                if inner.endswith(":") and not inner.lstrip().startswith("-"):
                    name = inner[:-1].strip()
                    if len(name) >= 2 and name[0] == name[-1] and name[0] in (chr(34), chr(39)):
                        name = name[1:-1]
                    if name:
                        out.append(name)
    return sorted(set(out))


install_path, deployed_path = sys.argv[1], sys.argv[2]
expected = libs_from_yaml(install_path)
deployed = libs_from_yaml(deployed_path)

if not expected:
    print("STAGE=install-script-parse-fail msg=zero-libraries-from-install-script",
          file=sys.stderr)
    sys.exit(1)
if not deployed:
    print("STAGE=kometa-config-missing msg=deployed-config-has-zero-libraries",
          file=sys.stderr)
    sys.exit(1)

if expected == deployed:
    print(f"PASS: kometa-deploy-drift — libs match ({','.join(expected)})")
    sys.exit(0)

print(f"STAGE=deploy-drift msg=install-script-has-{','.join(expected)}-deployed-has-{','.join(deployed)}",
      file=sys.stderr)
sys.exit(1)
PYEOF
)
RC=$?
echo "$DRIFT"
exit $RC
