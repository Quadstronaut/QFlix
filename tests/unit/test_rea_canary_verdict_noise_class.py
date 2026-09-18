"""REA noise class `canary-verdict-finding-echo`.

The boundary this encodes: canaries read LIVE state and page through Kuma, REA
reads HISTORY and pages through Discord. A canary that stamps its own output
`canary-verdict=finding` has already detected the condition and already turned
its monitor red — REA reporting the same line again is one fault paged twice.

DIRECTION is the whole test. `canary-verdict=broken` means the DETECTOR failed,
so nobody else owns the condition and REA is the only surface that will say so:
it pages. A line with NO marker also pages — absence is never evidence of
ownership. Suppress only on an explicit, self-declared FINDING.

Enforcement lives in the yaml-derived rule table (Test-IsNoiseFinding runs
pre-consensus), never in the prompt alone. No ps1 edit is needed: the ps1 is
gitignored and derived, and Sync-ReaNoiseMirror regenerates it from this file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
YAML_PATH = REPO_ROOT / "manifest" / "rea-noise-classes.yaml"
CLASS_ID = "canary-verdict-finding-echo"


@pytest.fixture(scope="module")
def doc():
    return yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def klass(doc):
    matches = [c for c in doc["classes"] if c["id"] == CLASS_ID]
    assert len(matches) == 1, f"{CLASS_ID} must appear exactly once"
    return matches[0]


@pytest.fixture(scope="module")
def rx(klass):
    return re.compile(klass["rx"])


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_class_present_and_well_formed(klass):
    assert klass["field"] == "excerpt"
    assert str(klass["added"]) == "2026-09-17"
    assert klass["rx"] == r"(?i)\bcanary-verdict=finding\b"
    re.compile(klass["rx"])
    assert klass["title"].strip()
    assert klass["why"].strip()
    assert klass["prompt_clause"].strip()


def test_no_python_incompatible_waiver(klass):
    """The rx uses only (?i), \\b and literals — it compiles under .NET too,
    so no waiver is needed and claiming one would hide a real incompatibility."""
    assert "python_incompatible" not in klass


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------

def test_matches_finding(rx):
    line = ("2026-09-17T04:00:12Z canary tdarr-healthcheck: "
            "canary-verdict=finding 3 stale rows")
    assert rx.search(line)


def test_matches_regardless_of_case(rx):
    assert rx.search("CANARY-VERDICT=FINDING")


def test_does_not_match_broken(rx):
    """A broken DETECTOR is owned by nobody — REA is the only surface that
    will report it, so it must still page."""
    assert not rx.search("... canary-verdict=broken probe exited 2 ...")


def test_does_not_match_unmarked_line(rx):
    assert not rx.search("ERROR plex: Failed to get a decision for: /data/x.mkv")
    assert not rx.search("audit finding: 3 monitors drifted")


def test_no_marker_fails_open(rx):
    """Absence of the marker is not evidence of ownership. Any line without
    an explicit `canary-verdict=` is unaffected by this rule."""
    for line in [
        "canary tdarr-healthcheck: 3 stale rows",
        "verdict=finding",
        "canary-verdict = finding",
        "xcanary-verdict=finding",
    ]:
        assert not rx.search(line), line


def test_word_boundary_does_not_swallow_findings_suffix(rx):
    assert not rx.search("canary-verdict=findings-report")


# ---------------------------------------------------------------------------
# C-07 bijection preconditions
# ---------------------------------------------------------------------------

def test_exactly_one_prompt_segment_claims_the_class(doc):
    owners = [s for s in doc["prompt_segments"]
              if CLASS_ID in (s.get("classes") or [])]
    assert len(owners) == 1
    assert owners[0]["index"] == 40


def test_marker_is_byte_identical_to_prompt_clause(doc, klass):
    seg = [s for s in doc["prompt_segments"] if s["index"] == 40][0]
    assert seg["marker"] == klass["prompt_clause"]


def test_clause_contains_no_semicolon(klass):
    """C-07 splits the prompt on ';' to locate each segment; a ';' inside a
    clause straddles two chunks and corrupts the bijection."""
    assert ";" not in klass["prompt_clause"]


def test_segment_indices_remain_dense_and_unique(doc):
    idx = [s["index"] for s in doc["prompt_segments"]]
    assert idx == sorted(idx)
    assert len(set(idx)) == len(idx)
    assert idx[-1] == 40


# ---------------------------------------------------------------------------
# No emitter (Cluster B owns emission)
# ---------------------------------------------------------------------------

def test_cluster_c_adds_no_emitter():
    """Tripwire, not a wish: while Cluster B is unlanded the rule ships inert,
    and the day an emitter appears this test says so out loud instead of
    letting the two halves land unobserved."""
    hits = []
    for path in (REPO_ROOT / "scripts").rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "canary-verdict=" in text:
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == [], f"an emitter landed under scripts/: {hits}"
