"""C-06 — exhaustive numeric-claim enumeration over the doc surfaces.

tests/unit/test_doc_counts.py checks ~12 hand-picked anchors and is the reason
that class has stayed closed. This asserts the GENERALISATION: every numeric
claim is seen, not only the twelve somebody remembered to guard.
"""
from __future__ import annotations

import re

from lib.audit.detectors import c06_doc_count_drift as det
from lib.audit.model import FINDING, OK


def test_enumerates_every_claim_on_every_surface(ctx, repo):
    result = det.detect(ctx)
    ref = 0
    for surface in det.SURFACES:
        ref += len(det.CLAIM.findall(repo.read(surface)))
    assert ref > 0
    assert result.boundary_size == ref
    assert len(result.verdicts) == ref


def test_covers_more_than_the_hand_picked_anchors(ctx):
    """The whole point: strictly more claims than test_doc_counts guards."""
    assert det.detect(ctx).boundary_size > 12


def test_headline_counts_agree_with_the_manifest(ctx, repo):
    """The numbers the existing guards protect must come back `ok` here too —
    two independent implementations agreeing is the signal.

    The canary claim is located by SHAPE, not by a literal count. It used to be
    pinned as "inventory.md:19-canary-monitors", which made this test fail the
    moment a 20th canary shipped — the count changing is the normal case, and a
    guard that breaks on the thing it is meant to track is noise. What matters is
    that a canary-monitors claim EXISTS and reads `ok`; the detector itself is
    what compares it against the manifest.
    """
    import re

    result = det.detect(ctx)
    by_id = {v.instance_id: v for v in result.verdicts}

    assert "README.md:35-manifest-apps" in by_id, "expected claim missing: apps"
    assert by_id["README.md:35-manifest-apps"].status == OK

    canary_claims = [v for iid, v in by_id.items()
                     if re.fullmatch(r"inventory\.md:\d+-canary-monitors", iid)]
    assert canary_claims, (
        "no inventory.md canary-monitors claim was enumerated at all — the "
        "detector stopped seeing a claim it is supposed to guard"
    )
    for v in canary_claims:
        assert v.status == OK, f"{v.instance_id}: {v.detail}"


def test_every_verdict_is_ok_or_a_declared_finding_kind(ctx):
    result = det.detect(ctx)
    for v in result.verdicts:
        assert v.status in (OK, FINDING)
        if v.status == FINDING:
            assert v.kind == "unguarded-count-claim"
            # A finding must SAY what the live value is, or the operator has to
            # go and recompute it by hand, which is how these rot.
            assert "live computed values are" in v.detail


def test_instance_ids_are_stable_and_line_independent(ctx):
    """Line numbers move whenever a doc is edited; waivers keyed on them would
    evaporate. Instance ids are keyed on the matched TEXT instead."""
    result = det.detect(ctx)
    for v in result.verdicts:
        assert not re.search(r":\d+:", v.instance_id)
        assert v.instance_id.startswith(v.path + ":")
