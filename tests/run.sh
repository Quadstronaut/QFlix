#!/usr/bin/env bash
# tests/run.sh — run unit tests in a local venv. Idempotent.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

if [ ! -x "$VENV/bin/python" ] && [ ! -x "$VENV/Scripts/python.exe" ]; then
  python -m venv "$VENV"
fi

PY="$VENV/bin/python"
[ ! -x "$PY" ] && PY="$VENV/Scripts/python.exe"

"$PY" -m pip install -q pytest pyyaml requests

"$PY" -m pytest "$HERE/unit/" "$@"
