"""tests/unit/test_rea_canary_verdict_noise_class.py — the REA half of
council round 2 (2026-09-17): a new noise class for a canary line the canary
itself already graded a FINDING (Cluster B's canary-verdict= marker).

This class SHIPS INERT: grep -rn "canary-verdict" over tracked files returns
zero hits as of this class's `added` date (Cluster B, which would emit the
marker, is unlanded). test_cluster_c_adds_no_emitter is the tripwire that
announces the transition when Cluster B lands.

No ps1 is read here — the whole point of manifest/rea-noise-classes.yaml
being the single tracked source (see its header comment and C-07).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
YAML_PATH = REPO_ROOT / "manifest" / "rea-noise-classes.yaml"
CLASS_ID = "canary-verdict-finding-echo"


def _load():
    return yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))


def _the_class(data) -> dict:
    by_id = {c["id"]: c for c in data["classes"]}
    assert CLASS_ID in by_id, f"{CLASS_ID} missing from {YAML_PATH}"
    return by_id[CLASS_ID]


def test_class_present_and_well_formed():
    data = _load()
    c = _the_class(data)
    assert c["field"] == "excerpt"
    assert str(c["added"]) == "2026-09-17"
    re.compile(c["rx"])  # must compile under Python re
    assert (c.get("why") or "").strip()
    assert (c.get("prompt_clause") or "").strip()


def test_matches_finding():
    data = _load()
    rx = re.compile(_the_class(data)["rx"])
    line = "2026-09-17T04:00:12Z canary tdarr-healthcheck: canary-verdict=finding 3 stale rows"
    assert rx.search(line)


def test_does_not_match_broken():
    data = _load()
    rx = re.compile(_the_class(data)["rx"])
    line = "2026-09-17T04:00:12Z canary tdarr-healthcheck: canary-verdict=broken probe exited 2"
    assert not rx.search(line)


def test_does_not_match_unmarked_line():
    data = _load()
    rx = re.compile(_the_class(data)["rx"])
    assert not rx.search("ERROR plex: Failed to get a decision for: /some/path")
    # merely containing the word "finding" elsewhere must not match either
    assert not rx.search("investigation finding: nothing wrong here")


def test_no_marker_fails_open():
    """A line carrying no canary-verdict marker at all makes no claim this
    rule can act on -- it is not suppressed by this class (fail-open
    direction: absence of the marker is not evidence of anything)."""
    data = _load()
    rx = re.compile(_the_class(data)["rx"])
    unmarked = "canary sab-stall: queue depth 12, no marker present here"
    assert not rx.search(unmarked)


def test_exactly_one_prompt_segment_claims_the_class():
    data = _load()
    owners = [seg for seg in data["prompt_segments"]
              if CLASS_ID in (seg.get("classes") or [])]
    assert len(owners) == 1, f"expected exactly 1 owning segment, got {len(owners)}"


def test_marker_is_byte_identical_to_prompt_clause():
    data = _load()
    c = _the_class(data)
    owners = [seg for seg in data["prompt_segments"]
              if CLASS_ID in (seg.get("classes") or [])]
    assert owners[0]["marker"] == c["prompt_clause"]
    assert owners[0]["index"] == 40


def test_clause_contains_no_semicolon():
    """C-07 splits the prompt's never-report sentence on ';' -- a semicolon
    inside a clause would corrupt the segment bijection."""
    data = _load()
    c = _the_class(data)
    assert ";" not in c["prompt_clause"]


def test_no_python_incompatible_waiver():
    data = _load()
    c = _the_class(data)
    assert "python_incompatible" not in c


def test_cluster_c_adds_no_emitter():
    """Cluster B owns emitting `canary-verdict=`; Cluster C only ships the
    suppression rule. As of this class landing, the string must appear in
    no file under scripts/ -- if it does, Cluster B has landed and this
    test is the tripwire that says so, out loud, in CI."""
    scripts_dir = REPO_ROOT / "scripts"
    hits = []
    for path in scripts_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "canary-verdict=" in text:
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == [], (
        "canary-verdict= now appears under scripts/ (Cluster B has landed): "
        + ", ".join(hits)
    )


def test_c07_bijection_still_green():
    """Not a re-implementation of C-07 -- just confirms this file's edits
    didn't knock the shared detector red, without importing pytest fixtures
    scoped to tests/unit/audit/."""
    import subprocess
    result = subprocess.run(
        ["python", "-m", "pytest", "-q",
         "tests/unit/audit/test_c07_rea_prompt_rule_bijection.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
