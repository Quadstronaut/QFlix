"""Repo-wide gate: the REMOTE body of every `sshm '...'` canary must parse.

WHY THIS FILE EXISTS — two real breakages, one week apart, same root cause.

  1. 2026-08-03, commit 320b8cf. Two comment lines added inside
     prowlarr-indexer-health.sh's `sshm '...'` body contained apostrophes
     ("script's", "timer's"). The first closed the string, the second reopened
     it, and the file would not parse AT ALL on the box. Every unit test still
     passed, because the tests that cover that canary read it as TEXT and
     assert on numbers in it — a file that cannot execute stayed green.

  2. Same day, during the fix for the ingest-lag blindness: a case pattern
     written as `''|*[!0-9]*)`. `bash -n` on the outer FILE passes, because the
     outer file is valid; but two adjacent single quotes inside a single-quoted
     argument cancel, so the body that actually reaches the box starts that
     pattern with a bare `|` and is a syntax error. Demonstrated:
         f(){ printf '<%s>' "$1"; };  f 'aa''bb'   ->   <aabb>

Both are invisible to `bash -n <script>` and to every content assertion in this
suite. The only thing that catches them is extracting the string that is
actually shipped and parsing THAT. There is no shellcheck gate in this repo, so
this test is the gate.

It is deliberately repo-wide rather than per-canary: the defect is a property of
the `sshm '...'` idiom, not of any one canary, and the next person to add a
possessive apostrophe to a remote comment will be working in a different file.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY_DIR = REPO_ROOT / "scripts" / "canaries"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None,
                                reason="needs bash on PATH")


def _sshm_bodies():
    """(path, body) for every single-quoted sshm block in scripts/canaries."""
    out = []
    for path in sorted(CANARY_DIR.glob("*.sh")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"sshm '", text):
            end = text.find("\n')", m.end())
            assert end != -1, "%s: unterminated sshm '...' block" % path.name
            out.append((path, text[m.end():end]))
    return out


def test_there_is_at_least_one_sshm_body_to_check():
    """Guards the guard. If the idiom is ever renamed, this file would silently
    assert nothing at all -- the vacuity failure it exists to prevent."""
    assert _sshm_bodies(), "no sshm '...' blocks found; the extractor is stale"


@pytest.mark.parametrize("path,body", _sshm_bodies(),
                         ids=lambda v: v.name if isinstance(v, Path) else "")
def test_remote_body_contains_no_single_quote(path, body):
    """A single quote anywhere in the body either terminates the string (a
    parse error at the call site) or cancels against another one (a silent
    content change). Both are wrong; neither is detectable downstream."""
    offenders = [l for l in body.split("\n") if "'" in l]
    assert not offenders, (
        "%s: single quote(s) inside the sshm body -- the shipped string is not "
        "what is written here:\n  %s" % (path.name, "\n  ".join(offenders[:5])))


@pytest.mark.parametrize("path,body", _sshm_bodies(),
                         ids=lambda v: v.name if isinstance(v, Path) else "")
def test_remote_body_parses_as_bash(path, body, tmp_path):
    """`bash -n` on the SHIPPED string, not on the wrapper file. This is the
    assertion that would have caught 320b8cf before it reached the box."""
    script = tmp_path / (path.stem + ".remote.sh")
    script.write_text(body, encoding="utf-8", newline="\n")
    proc = subprocess.run(["bash", "-n", str(script)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        "%s: the remote body does not parse:\n%s" % (path.name, proc.stderr))


def test_the_gate_discriminates():
    """MUTATION PROOF. The exact two shapes that broke are run through the same
    predicates and must FAIL, so a green result above means something."""
    apostrophe = "echo do not touch the timer's schedule\n"
    assert "'" in apostrophe

    cancelling = "case \"$X\" in\n  ''|*[!0-9]*) exit 2 ;;\nesac\n"
    # Adjacent single quotes cancel inside a single-quoted argument, so what
    # ships is the pattern with a bare leading pipe.
    shipped = cancelling.replace("''", "")
    proc = subprocess.run(["bash", "-n", "/dev/stdin"], input=shipped,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0, (
        "the empty-pattern case survived quote cancellation; this gate would "
        "not have caught the 2026-08-03 fix regression")
