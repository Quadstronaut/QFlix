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

# jinja2 is needed for the qflix_newsletter render tests (test_qflix_newsletter_render.py);
# without it the tests fail with ModuleNotFoundError rather than skip. The newsletter package
# itself runs in its own venv (~/.apps/qflix-newsletter/.venv on the seedbox), but the test
# suite is run from this top-level venv to stay one-command.
"$PY" -m pip install -q pytest pyyaml requests jinja2

"$PY" -m pytest "$HERE/unit/" "$@"

# The newsletter package ships its OWN pytest suite next to its source. It was
# tracked, passing, and executed by nothing — defect class C-10 (test-not-in-CI)
# found it the first time the detector ran. Two invocations, not one: both dirs
# contain a `tests/conftest.py`, and pytest rejects the pair in a single process
# with ImportPathMismatchError.
"$PY" -m pytest "$HERE/../scripts/qflix-newsletter/tests" "$@"
