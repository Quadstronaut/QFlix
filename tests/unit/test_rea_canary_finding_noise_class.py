"""tests/unit/test_rea_canary_finding_noise_class.py

THE 2026-09-17 REA PAGE, pinned.

`manitoba-maint-canary-deploy-drift.service` exits NON-ZERO BY DESIGN when it
finds drift — the non-zero exit IS the finding — and systemd logs
"Failed to start ..." as a mechanical consequence. The canary's own Kuma push
monitor already owned that signal and had already gone red. REA reads history,
canaries read live state, so the page was a duplicate in BOTH directions:
green monitor means already resolved, red monitor means already paged.

The rule therefore suppresses EXACTLY ONE SHAPE: a canary unit, a
"Failed to start" line, AND the marker `canary-verdict=finding`. Every
neighbouring shape still pages, and the most important of them is the one with
NO marker at all — that is the deliberate fail-open direction. A suppressor
that guessed "probably a finding" would convert the canary fleet into a blind
spot, which is the opposite of what a canary is for.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = REPO_ROOT / "manifest" / "rea-noise-classes.yaml"
RULE_ID = "canary-finding-exit-owned-by-kuma"

# A real-shaped journald excerpt. Host is the sanitized placeholder: this repo
# is public and `seedbox.example.com` is the only host string permitted.
_PREFIX = "Sep 17 04:00:11 seedbox systemd[1]: "

POSITIVE = (
    _PREFIX + "Failed to start manitoba-maint-canary-deploy-drift.service - "
              "QFlix canary deploy-drift. canary-verdict=finding "
              "reason=repo-vs-box commit mismatch"
)

NEGATIVE_VECTORS = {
    # (a) the canary could not RUN. Real outage of the detector itself.
    "broken": _PREFIX + "Failed to start manitoba-maint-canary-deploy-drift.service - "
                        "QFlix canary deploy-drift. canary-verdict=broken "
                        "reason=ssh connection refused",
    # (b) NO marker: the emitter has not shipped, or the wrapper died before
    #     emitting, or the log was truncated. MUST page. C4's answer.
    "unmarked": _PREFIX + "Failed to start manitoba-maint-canary-deploy-drift.service - "
                          "QFlix canary deploy-drift.",
    # (c) a non-canary unit, even carrying a verdict token.
    "pusher": _PREFIX + "Failed to start manitoba-maint-pusher.service canary-verdict=finding",
    # (d) an app unit.
    "app": _PREFIX + "Failed to start plexmediaserver.service.",
    # (e) the result line on its own - no "Failed to start" shape.
    "result-line": _PREFIX + "manitoba-maint-canary-deploy-drift.service: "
                             "Failed with result 'exit-code'.",
    # (f) prose about a canary with neither shape.
    "prose": "the deploy-drift canary has been red since yesterday afternoon",
}


def _policy() -> dict:
    return yaml.safe_load(POLICY.read_text(encoding="utf-8"))


def _rule() -> dict:
    matches = [c for c in _policy()["classes"] if c["id"] == RULE_ID]
    assert len(matches) == 1, f"expected exactly one {RULE_ID} class, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# AC-15 — the rule exists, compiles, and matches the page that happened
# ---------------------------------------------------------------------------

def test_rule_exists_and_compiles_under_python_re():
    rx = re.compile(_rule()["rx"])
    assert rx.search(POSITIVE), "the 2026-09-17 page shape is not suppressed"


def test_rule_is_scoped_to_the_collector_anchored_excerpt():
    """signature/summary are MODEL PROSE. Letting prose mute a canary finding
    is the 2026-08-16 rule; this class must never be able to."""
    assert _rule()["field"] == "excerpt"


def test_rule_carries_written_evidence_and_the_sibling_that_still_pages():
    why = _rule()["why"]
    assert "2026-09-17" in why
    assert "canary-verdict=broken" in why, "the sibling shape must be named"
    assert "NO verdict marker" in why, "the fail-open direction must be stated"


# ---------------------------------------------------------------------------
# AC-16 — it does not over-match
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(NEGATIVE_VECTORS))
def test_rule_does_not_match_any_neighbouring_shape(name):
    rx = re.compile(_rule()["rx"])
    line = NEGATIVE_VECTORS[name]
    assert not rx.search(line), f"{name!r} would be silently suppressed: {line}"


def test_a_broken_verdict_on_the_same_line_never_wins():
    """Belt and braces for the negative lookahead: a line that somehow carries
    BOTH tokens is a broken canary and must page."""
    rx = re.compile(_rule()["rx"])
    both = POSITIVE + " canary-verdict=broken"
    assert not rx.search(both)


def test_multiline_blob_suppresses_only_the_finding_line():
    """REA hands models a multi-source blob. The rx is line-anchored under
    (?im), so a finding line next to a broken line must not drag it along."""
    rx = re.compile(_rule()["rx"])
    blob = "\n".join([NEGATIVE_VECTORS["broken"], POSITIVE, NEGATIVE_VECTORS["unmarked"]])
    hits = rx.findall(blob)
    assert len(hits) == 1
    assert "canary-verdict=finding" in hits[0]


# ---------------------------------------------------------------------------
# AC-17 — no OTHER class already covers (or over-covers) these shapes
# ---------------------------------------------------------------------------

def test_no_pre_existing_class_matches_the_vectors_that_must_page():
    """Two failures at once would hide here: a vector already suppressed by an
    older rule (so this 'new page path' was never open), and an older rule
    that over-matches the new shape."""
    offenders = []
    for cls in _policy()["classes"]:
        if cls["id"] == RULE_ID or cls.get("python_incompatible"):
            continue
        rx = re.compile(cls["rx"])
        for name, line in NEGATIVE_VECTORS.items():
            if rx.search(line):
                offenders.append(f"{cls['id']} matches {name}")
    assert offenders == [], offenders


def test_no_pre_existing_class_already_suppressed_the_finding_line():
    offenders = [c["id"] for c in _policy()["classes"]
                 if c["id"] != RULE_ID and not c.get("python_incompatible")
                 and re.search(c["rx"], POSITIVE)]
    assert offenders == [], f"the finding shape was already muted by {offenders}"


# ---------------------------------------------------------------------------
# AC-20 (yaml half) — C-07 bijection for the new class
# ---------------------------------------------------------------------------

def test_exactly_one_prompt_segment_claims_the_new_class():
    policy = _policy()
    owners = [s for s in policy["prompt_segments"] if RULE_ID in (s.get("classes") or [])]
    assert len(owners) == 1
    seg = owners[0]
    assert seg["index"] == 40, "index 40 was the next free one"
    assert seg["marker"] == _rule()["prompt_clause"], \
        "the segment marker IS the literal prompt text the clause promises"


def test_prompt_segment_indices_are_dense_and_unique():
    idx = [s["index"] for s in _policy()["prompt_segments"]]
    assert idx == sorted(idx) == list(range(len(idx)))


def test_prompt_clause_carries_no_segment_delimiter():
    """C-07 splits the prompt sentence on ';'. A clause containing one would
    split into a segment nothing claims."""
    assert ";" not in _rule()["prompt_clause"]


# ---------------------------------------------------------------------------
# AC-18 / AC-21 — single policy source, single marker emitter
# ---------------------------------------------------------------------------

def _tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True)
    return [REPO_ROOT / ln for ln in out.stdout.splitlines() if ln.strip()]


def _files_containing(token: str) -> list[str]:
    hits = []
    for path in _tracked_files():
        try:
            if token in path.read_text(encoding="utf-8"):
                hits.append(path.relative_to(REPO_ROOT).as_posix())
        except (OSError, UnicodeDecodeError):
            continue
    return hits


def test_the_rule_id_lives_in_exactly_one_policy_file():
    """qflix-rea.ps1 is gitignored and REGENERATED from the yaml by
    Sync-ReaNoiseMirror. A second tracked definition is a second policy."""
    hits = [h for h in _files_containing(RULE_ID) if not h.startswith("tests/")]
    assert hits == ["manifest/rea-noise-classes.yaml"], hits


def test_the_verdict_marker_has_at_most_one_emitter():
    """Cluster B owns EMISSION of `canary-verdict=`; this change owns only the
    rule that keys on it. Two emitters would mean two contracts."""
    emitters = [h for h in _files_containing("canary-verdict=")
                if h.startswith("scripts/")
                and not h.startswith("scripts/local-llm/")]
    assert len(emitters) <= 1, f"more than one emitter of the marker: {emitters}"


# ---------------------------------------------------------------------------
# README count drift — "it sat wrong for N days" means a detector is missing
# ---------------------------------------------------------------------------

def test_readme_enforced_class_count_matches_the_policy():
    """The prose said 39 while the yaml held 44. Nothing compared them, so the
    drift was invisible; this is the comparison."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"\*\*Noise policy\*\* \S+ (\d+) enforced classes from "
                  r"`manifest/rea-noise-classes\.yaml`", readme)
    assert m, "README's REA noise-policy sentence moved (update this guard)"
    assert int(m.group(1)) == len(_policy()["classes"])
