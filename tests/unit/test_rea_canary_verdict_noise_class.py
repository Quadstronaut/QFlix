"""tests/unit/test_rea_canary_verdict_noise_class.py — the
`canary-verdict-finding-echo` REA noise class (council round 2, cluster C).

Cluster C carries this rule forward from round 1: a canary that already
graded its own output FINDING has its Kuma monitor already red, so REA
reporting it again is one fault paged twice. BROKEN (the detector itself
failed) and unmarked lines still page — this class narrows to exactly the
finding-echo case. Cluster C ships it correctly INERT: no emitter for
'canary-verdict=' lands in this change set (that's Cluster B's job).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
YAML_PATH = ROOT / "manifest" / "rea-noise-classes.yaml"
CLASS_ID = "canary-verdict-finding-echo"


def _policy() -> dict:
    return yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))


def _class() -> dict:
    d = _policy()
    by_id = {c["id"]: c for c in d["classes"]}
    assert CLASS_ID in by_id, f"{CLASS_ID} missing from {YAML_PATH}"
    return by_id[CLASS_ID]


def test_class_present_and_well_formed():
    c = _class()
    assert c["field"] == "excerpt"
    assert str(c["added"]) == "2026-09-17"
    re.compile(c["rx"])  # must compile under Python re


def test_matches_finding():
    c = _class()
    line = "2026-09-17T04:00:12Z canary tdarr-healthcheck: canary-verdict=finding 3 stale rows"
    assert re.search(c["rx"], line)


def test_does_not_match_broken():
    c = _class()
    line = "2026-09-17T04:00:12Z canary tdarr-healthcheck: canary-verdict=broken probe exited 2"
    assert not re.search(c["rx"], line)


def test_does_not_match_unmarked_line():
    c = _class()
    assert not re.search(c["rx"], "ERROR plex: Failed to get a decision")
    assert not re.search(c["rx"], "this line merely contains the word finding")


def test_no_marker_fails_open():
    """A line with no canary-verdict marker at all is untouched by this
    rule — it is not this class's job to suppress it (fail-open by
    default, suppression only on the explicit finding marker)."""
    c = _class()
    assert not re.search(c["rx"], "manitoba-maint: plex recovery failed, operator needed")


def test_exactly_one_prompt_segment_claims_the_class():
    d = _policy()
    owners = [s for s in d["prompt_segments"] if CLASS_ID in (s.get("classes") or [])]
    assert len(owners) == 1, owners


def test_marker_is_byte_identical_to_prompt_clause():
    d = _policy()
    c = _class()
    seg = next(s for s in d["prompt_segments"] if CLASS_ID in (s.get("classes") or []))
    assert seg["marker"] == c["prompt_clause"]


def test_clause_contains_no_semicolon():
    c = _class()
    assert ";" not in c["prompt_clause"]


def test_no_python_incompatible_waiver():
    c = _class()
    assert not c.get("python_incompatible")


def test_cluster_c_adds_no_emitter():
    """Cluster B owns emission. If it has not landed, `canary-verdict=`
    appears in NO file under scripts/ — this test is the tripwire that
    announces the transition the day Cluster B lands (it will start
    failing, on purpose, and that failure IS the signal)."""
    hits = []
    for path in (ROOT / "scripts").rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "canary-verdict=" in text:
            hits.append(str(path.relative_to(ROOT)))
    assert hits == [], f"unexpected canary-verdict= emitter(s): {hits}"
