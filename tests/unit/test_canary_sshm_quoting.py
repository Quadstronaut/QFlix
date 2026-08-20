"""Repo-wide gate: the REMOTE body of every single-quoted sshm canary must parse.

WHY THIS FILE EXISTS - two real breakages, one week apart, same root cause.

  1. 2026-08-03, commit 320b8cf. Two comment lines added inside
     prowlarr-indexer-health.sh's `sshm '...'` body contained apostrophes
     ("script's", "timer's"). The first closed the string, the second reopened
     it, and the file would not parse AT ALL on the box. Every unit test still
     passed, because the tests that cover that canary read it as TEXT and
     assert on numbers in it - a file that cannot execute stayed green.

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
the single-quoted sshm idiom, not of any one canary, and the next person to add a
possessive apostrophe to a remote comment will be working in a different file.

WHY THE EXTRACTOR MATCHES TWO SHAPES (2026-08-19)
The original extractor searched for the literal `sshm '` and nothing else. That
is not the only single-quoted shape in this directory: a canary that has to
interpolate local config into the remote environment writes

    RES=$(sshm "
    export FOO='...'
    "'
    <body>
    ')

- a double-quoted PRELUDE carrying the config, concatenated with a single-quoted
BODY. bash sees one word; the gate saw nothing at all, because the text contains
`sshm "` rather than `sshm '`. plex-playback.sh shipped in exactly that shape and
was silently unchecked - the vacuity failure this file's own guard-the-guard test
exists to prevent, just one level up. Both shapes are matched now.

The prelude is deliberately NOT parsed: it is double-quoted, so apostrophes in it
are ordinary characters and the quote-cancellation defect cannot occur there. It
is the single-quoted half that is fragile, and the single-quoted half is what is
extracted.

Both patterns require the body to open on a NEWLINE and close with a line that
begins `')`, which is what every multi-line canary body in this directory looks
like. Bodies that are a single-line argument (tdarr-transcode-error.sh ships a
python heredoc that way) are out of scope by construction rather than by
accident, and `test_the_gate_discriminates` pins the extractor against the shapes
that actually broke.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY_DIR = REPO_ROOT / "scripts" / "canaries"

# Shape 1: the plain idiom, `sshm '` at end of line.
# Shape 2: `sshm "<prelude>"'` - a double-quoted prelude glued to the body.
#          `[^"]*` cannot cross a double quote, so this can never run away over a
#          fully double-quoted sshm block (quota.sh, thread-ceiling.sh, ...); it
#          simply fails to match there, which is correct - those bodies are not
#          single-quoted and are not what this gate is about.
_SSHM_OPENERS = (
    re.compile(r"sshm '\n"),
    re.compile(r"sshm \"[^\"]*\"'\n"),
)

pytestmark = pytest.mark.skipif(shutil.which("bash") is None,
                                reason="needs bash on PATH")


def _sshm_bodies(directory=CANARY_DIR):
    """(path, body) for every single-quoted sshm block in `directory`.

    Parameterised on the directory so the mutation proof below can point it at a
    synthetic file: a gate that cannot be run against a known-bad input is a gate
    nobody has checked.
    """
    out = []
    for path in sorted(Path(directory).glob("*.sh")):
        text = path.read_text(encoding="utf-8")
        for pattern in _SSHM_OPENERS:
            for m in pattern.finditer(text):
                # m.end() sits just past the newline that opens the body.
                end = text.find("\n')", m.end() - 1)
                assert end != -1, "%s: unterminated sshm block" % path.name
                out.append((path, text[m.end():end]))
    return out


def test_there_is_at_least_one_sshm_body_to_check():
    """Guards the guard. If the idiom is ever renamed, this file would silently
    assert nothing at all -- the vacuity failure it exists to prevent."""
    assert _sshm_bodies(), "no sshm blocks found; the extractor is stale"


def test_the_prelude_shape_is_actually_matched():
    """The second half of the same guard, added with the second shape.

    A pattern that matches nothing is indistinguishable from a pattern that is
    wrong, and that is precisely how plex-playback.sh escaped this gate. Assert
    by NAME that the file which introduced the prelude shape is now extracted, so
    deleting the second regex fails here rather than going quiet.
    """
    names = {p.name for p, _ in _sshm_bodies()}
    assert "plex-playback.sh" in names, (
        "the `sshm \"<prelude>\"'<body>'` shape is no longer extracted; any "
        "canary using it is unchecked")


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


def test_the_prelude_shape_catches_a_broken_body(tmp_path):
    """MUTATION PROOF for the SECOND shape, which is the whole point of adding
    it: extend an extractor without proving it discriminates and you have only
    widened the vacuity.

    A synthetic canary in the exact `sshm "<prelude>"'<body>'` shape, carrying
    one deliberately broken body, is run through the real extractor and the real
    predicates. Both must fire.
    """
    bad = tmp_path / "synthetic-canary.sh"
    bad.write_text(
        "#!/usr/bin/env bash\n"
        "RES=$(sshm \"\nexport FOO='bar'\n\"'\n"
        "# do not touch the timer's schedule\n"
        "if [ -n \"$FOO\" ]; then\n"
        "echo unterminated-if\n"
        "')\n",
        encoding="utf-8", newline="\n")

    bodies = _sshm_bodies(tmp_path)
    assert len(bodies) == 1, (
        "the prelude-shape extractor did not find the synthetic body: " +
        repr(bodies))
    _, body = bodies[0]

    # (a) the apostrophe defect - the 320b8cf class
    assert [l for l in body.split("\n") if "'" in l], (
        "extractor returned a body with the apostrophe stripped; it is not "
        "returning the shipped string")

    # (b) the parse defect - an `if` with no `fi`
    script = tmp_path / "synthetic.remote.sh"
    script.write_text(body, encoding="utf-8", newline="\n")
    proc = subprocess.run(["bash", "-n", str(script)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0, (
        "the broken synthetic body PARSED; the extended gate asserts nothing")
