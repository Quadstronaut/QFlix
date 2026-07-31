"""The file must not promise crash-resumability it does not implement.

COUNCIL FINDING 2. The inflight ledger is WRITE-ONLY: one call site (a write in
rehome) and zero readers. Nothing resumes from it. Recovery is the next
scheduled run re-deriving the plan from live *arr state -- self-healing, but not
resumption.

THREE surfaces described it, and the first correction fixed exactly one:

    docstring sentence   "...so a crash mid-move is resumable"      <- stale
    docstring NOTE       "WRITE-ONLY ... NOT resumed"               <- fixed
    section banner       "Ledger (durable, crash-resumable)"        <- stale

So the file asserted both readings at once, and the two stale surfaces were the
ones a reader hits FIRST. This is the repo's recurring failure shape: one policy
surface corrected, its duplicates left behind (cf. the REA prompt-vs-rule
incident). The cost is misplaced confidence during an incident -- someone reading
"crash-resumable" at 3am concludes a half-finished move gets picked up.

These tests pin the two AUTHORITATIVE surfaces exactly rather than scanning prose
for the word, because the surrounding text now legitimately discusses the old
wording and a keyword scan cannot tell a claim from its own post-mortem.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JANITOR = REPO / "scripts" / "maint" / "qflix-anime-janitor.py"

SRC = JANITOR.read_text(encoding="utf-8")


def _module_body() -> str:
    """Source with the module docstring removed, so prose is not mistaken for
    code when counting call sites."""
    first = SRC.index('"""')
    second = SRC.index('"""', first + 3)
    return SRC[second + 3:]


def _banner_title() -> str:
    """The single line a reader skimming for the section actually sees."""
    for line in SRC.splitlines():
        if line.startswith("# Ledger ("):
            return line
    raise AssertionError("the ledger section banner is gone entirely")


def _sequence_preamble() -> str:
    """The docstring sentence introducing the re-home sequence, up to the NOTE
    that qualifies it. This is what a reader hits before any correction."""
    start = SRC.index("RE-HOME SEQUENCE")
    return SRC[start:SRC.index("0. NOTE", start)]


# --- the premise -----------------------------------------------------------

def test_the_ledger_really_is_write_only():
    """If a READER is ever added, this whole file's wording becomes wrong in the
    other direction -- fail loudly rather than keep asserting 'write-only'."""
    body = _module_body()
    uses = [l.strip() for l in body.splitlines() if "_ledger_path()" in l]
    assert len(uses) == 2, (
        "expected exactly the definition and one write; found %d:\n  %s"
        % (len(uses), "\n  ".join(uses))
    )
    assert uses[0].startswith("def _ledger_path()")
    assert uses[1].startswith("_append_json_list(_ledger_path()"), (
        "the single call site is no longer the append-write these tests assume: "
        + uses[1]
    )


# --- the two surfaces that were left stale ---------------------------------

def test_the_section_banner_does_not_claim_resumability():
    """THE REGRESSION, surface 1."""
    title = _banner_title().lower()
    assert "resumable" not in title, \
        "the banner reverted to claiming crash-resumability: " + title
    assert "write-only" in title, \
        "the banner no longer states what the ledger actually is: " + title


def test_the_docstring_preamble_does_not_claim_resumability():
    """THE REGRESSION, surface 2 -- the sentence read BEFORE the NOTE."""
    pre = _sequence_preamble().lower()
    assert not re.search(r"is resum|crash-resum", pre), (
        "the re-home preamble promises resumability again:\n" + pre.strip()
    )
    assert "audit trail" in pre or "not a recovery" in pre, (
        "the preamble no longer tells the reader the ledger is an audit trail, "
        "so the correction depends entirely on them reading further:\n" + pre.strip()
    )


# --- and the disclosure must survive --------------------------------------

def test_the_write_only_disclosure_is_still_present():
    """Deleting the NOTE would satisfy the tests above while leaving a reader
    with no statement of what the ledger IS."""
    assert "WRITE-ONLY" in SRC
    assert "NOT resumed" in SRC
