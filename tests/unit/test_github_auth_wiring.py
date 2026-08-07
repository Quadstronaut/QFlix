"""Every GitHub API call in a deployed script goes through the auth helper.

WHY THIS GUARD EXISTS
---------------------
GitHub's anonymous API allows 60 requests/hour PER IP. This is a SHARED
Ultra.cc seedbox, so that quota is consumed by other tenants and QFlix's own
usage is irrelevant to whether it is exhausted. Measured live 2026-08-07:
anonymous limit 60, authenticated limit 5000. Authenticating moves us out of
the shared pool entirely.

`scripts/lib/github.sh` holds the one place that knows how to attach the token.
The failure this file prevents is the obvious one: someone adds a FOURTH release
lookup with a bare `curl https://api.github.com/...`, it works fine on the day
they test it, and then fails months later during an install when a neighbour has
burned the shared 60. That failure is loud but badly timed — mid-install, on a
box rebuild, which is exactly when you least want to debug a 403.

Deliberately scoped to scripts that actually SHIP. The vendored
`scripts/qflix-newsletter/.venv-dev/**` contains uritemplate's docstrings, which
mention api.github.com as example URLs and are not calls at all.
"""
from __future__ import annotations

import os
import re

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
HELPER = os.path.join(REPO_ROOT, "scripts", "lib", "github.sh")

# Directories whose scripts are deployed / run for real.
SCAN_DIRS = [
    ("scripts", "configure"),
    ("scripts", "maint"),
    ("scripts", "canaries"),
    ("scripts", "ops"),
    ("scripts", "plex"),
    ("scripts", "lib"),
]


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _shipped_scripts():
    out = []
    for parts in SCAN_DIRS:
        d = os.path.join(REPO_ROOT, *parts)
        if not os.path.isdir(d):
            continue
        for root, dirs, files in os.walk(d):
            # Never walk into a vendored virtualenv.
            dirs[:] = [x for x in dirs
                       if not x.startswith(".venv") and x not in ("node_modules", "__pycache__")]
            for fn in files:
                if fn.endswith((".sh", ".py")):
                    out.append(os.path.join(root, fn))
    return out


def test_helper_exists_and_fails_open():
    """The helper must degrade to an unauthenticated call rather than erroring.
    An install that dies because an OPTIONAL rate-limit credential is missing
    is strictly worse than a slow one."""
    src = _read(HELPER)
    assert "gh_curl()" in src and "gh_latest_tag()" in src
    # The else branch is the fail-open path: a bare curl with no auth header.
    assert re.search(r"else\s*\n\s*curl -fsSL \"\$url\"", src), (
        "github.sh must fall back to an unauthenticated curl when no token is "
        "readable — fail-open is the whole safety property")


def test_no_shipped_script_calls_the_github_api_directly():
    """All GitHub API traffic goes through gh_curl/gh_latest_tag, so there is
    exactly ONE place that knows how to authenticate."""
    offenders = {}
    for path in _shipped_scripts():
        if os.path.abspath(path) == os.path.abspath(HELPER):
            continue  # the helper is allowed to name the API
        src = _read(path)
        for lineno, line in enumerate(src.splitlines(), 1):
            if "api.github.com" not in line:
                continue
            if re.match(r"\s*#", line):
                continue  # a comment explaining the limit is not a call
            offenders.setdefault(os.path.relpath(path, REPO_ROOT).replace("\\", "/"),
                                 []).append(lineno)
    assert offenders == {}, (
        "shipped scripts calling api.github.com directly instead of via "
        "scripts/lib/github.sh — these get the 60/h SHARED-IP anonymous limit "
        "and will 403 during an install when a neighbour has burned it: "
        + str(offenders))


def test_the_three_known_consumers_use_the_helper():
    """Pins the consumers that exist, so a refactor that quietly drops the
    `source` line is caught rather than silently reverting to anonymous."""
    expected = {
        "55-kometa-install.sh": "Kometa-Team/Kometa",
        "56-recyclarr-install.sh": "recyclarr/recyclarr",
        "59-python-plexapi-venv.sh": "pkkid/python-plexapi",
    }
    for fn, repo in expected.items():
        src = _read(os.path.join(REPO_ROOT, "scripts", "configure", fn))
        assert "lib/github.sh" in src, fn + " no longer sources the auth helper"
        assert "gh_latest_tag " + repo in src, (
            fn + " no longer resolves " + repo + " through gh_latest_tag")
