"""tests/unit/test_rea_canary_verdict_owned_by_kuma.py — Stage-0 Cluster C
(alert hygiene, 2026-09-17): the REA noise class
`canary-finding-exit-owned-by-kuma`.

Root cause: manitoba-maint-canary-deploy-drift.service exits non-zero BY
DESIGN on drift; systemd logs "Failed to start" as a mechanical consequence,
and the canary's own Kuma push monitor already owns the signal. REA reads
history, canaries read live state -> the page is a duplicate in both
directions.

AC-15/16/17/18/21. AC-19/20 are covered by the existing generic pwsh
(tests/local-llm/test-rea-noise-classes.ps1) and Python (tests/unit/audit/
test_c07_rea_prompt_rule_bijection.py) harnesses, which read whatever the
yaml holds without hardcoding this rule's id — adding the class here is
what exercises them, not a change to either harness.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
YAML_PATH = ROOT / "manifest" / "rea-noise-classes.yaml"
CLASS_ID = "canary-finding-exit-owned-by-kuma"


@pytest.fixture(scope="module")
def policy() -> dict:
    return yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def classes(policy) -> dict:
    return {c["id"]: c for c in policy["classes"]}


@pytest.fixture(scope="module")
def rx(classes) -> "re.Pattern[str]":
    return re.compile(classes[CLASS_ID]["rx"])


# ---------------------------------------------------------------------------
# AC-15: the class exists, compiles, and matches its canonical shape
# ---------------------------------------------------------------------------

def test_class_exists_with_the_declared_id(classes):
    assert CLASS_ID in classes


def test_rx_compiles_under_python_re(classes):
    re.compile(classes[CLASS_ID]["rx"])  # must not raise


def test_field_is_scoped_to_excerpt(classes):
    """excerpt is collector-anchored; signature/summary are model prose and
    must never be able to mute a finding (the 2026-08-16 rule)."""
    assert classes[CLASS_ID]["field"] == "excerpt"


def test_matches_a_real_shaped_journald_excerpt(rx):
    line = ("Sep 17 08:00:01 seedbox.example.com systemd[1]: Failed to start "
            "manitoba-maint-canary-deploy-drift.service canary-verdict=finding")
    assert rx.search(line)


# ---------------------------------------------------------------------------
# AC-16: the six negative vectors
# ---------------------------------------------------------------------------

NEGATIVE_VECTORS = {
    "verdict-broken": (
        "Sep 17 08:00:01 seedbox.example.com systemd[1]: Failed to start "
        "manitoba-maint-canary-deploy-drift.service canary-verdict=broken"
    ),
    "no-verdict-marker": (
        "Sep 17 08:00:01 seedbox.example.com systemd[1]: Failed to start "
        "manitoba-maint-canary-deploy-drift.service"
    ),
    "non-canary-pusher-unit": (
        "Failed to start manitoba-maint-pusher.service canary-verdict=finding"
    ),
    "unrelated-service": (
        "Failed to start plexmediaserver.service"
    ),
    "result-line-without-failed-to-start": (
        "manitoba-maint-canary-deploy-drift.service: Failed with result 'exit-code'"
    ),
    "prose-mentioning-a-canary": (
        "the deploy-drift canary reported clean this run, no drift detected"
    ),
}


@pytest.mark.parametrize("name", sorted(NEGATIVE_VECTORS))
def test_rx_does_not_over_match(rx, name):
    assert not rx.search(NEGATIVE_VECTORS[name]), (
        f"canary-finding-exit-owned-by-kuma over-matched negative vector '{name}'"
    )


def test_fail_open_direction_is_documented_in_why(classes):
    """AC-16(b)/C4: the absent-marker case is the deliberate fail-open
    direction and must be stated in the class's `why`, not merely asserted
    by the test in isolation."""
    why = classes[CLASS_ID]["why"]
    assert "canary-verdict=broken" in why
    assert "fail-open" in why.lower() or "blind spot" in why.lower()


# ---------------------------------------------------------------------------
# AC-17: no existing class over-matches the new rule's negative vectors
# ---------------------------------------------------------------------------

def test_no_other_class_matches_the_negative_vectors(classes):
    offenders = []
    for cid, c in classes.items():
        if cid == CLASS_ID:
            continue
        try:
            pat = re.compile(c["rx"])
        except re.error:
            continue  # a different detector (C-07) owns "does every rx compile"
        for name, hay in NEGATIVE_VECTORS.items():
            if pat.search(hay):
                offenders.append(f"{cid} matches negative vector '{name}'")
    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# AC-18: single policy source
# ---------------------------------------------------------------------------

def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def test_rule_id_appears_in_no_second_policy_definition():
    """The rule id string itself must not be authored anywhere else — the
    yaml is the single source; scripts/local-llm/qflix-rea.ps1 is not hand-
    edited by this change (it is gitignored and untracked, so it cannot even
    appear in this scan)."""
    hits = []
    for rel in _tracked_files():
        if rel == "manifest/rea-noise-classes.yaml":
            continue
        if rel == "tests/unit/test_rea_canary_verdict_owned_by_kuma.py":
            continue  # this file names the id in prose/assertions, not policy
        p = ROOT / rel
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if CLASS_ID in text:
            hits.append(rel)
    assert not hits, f"'{CLASS_ID}' authored outside the single policy source: {hits}"


# ---------------------------------------------------------------------------
# AC-21: the canary-verdict= marker contract is single-sourced
# ---------------------------------------------------------------------------

def test_canary_verdict_marker_defined_in_at_most_one_emitter():
    """Cluster B (not this cluster) owns EMITTING 'canary-verdict='. Cluster C
    only owns the REA rule that CONSUMES it. This asserts Cluster B and
    Cluster C cannot both ship an emitter.

    manifest/rea-noise-classes.yaml is the declared CONSUMER (it matches the
    literal token in a regex) and is excluded here on that basis. Test files
    that use the literal string as a MATCHING fixture (proving the rx works)
    are excluded for the same reason — they consume/verify, they don't emit.
    Everything else that is tracked and contains the literal token is an
    emitter candidate, and there must be at most one such file.
    """
    consumer = "manifest/rea-noise-classes.yaml"
    test_fixture_files = {
        "tests/unit/test_rea_canary_verdict_owned_by_kuma.py",
    }
    emitters = []
    for rel in _tracked_files():
        if rel == consumer or rel in test_fixture_files:
            continue
        p = ROOT / rel
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "canary-verdict=" in text:
            emitters.append(rel)
    assert len(emitters) <= 1, (
        f"'canary-verdict=' token defined/emitted in multiple tracked files "
        f"(Cluster B and Cluster C must not both ship an emitter): {emitters}"
    )
