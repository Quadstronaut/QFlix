"""C-05 — the ad8198e class, generalised.

The regression test that matters is the SYNTHETIC one: it reconstructs the
pre-ad8198e shape (two siblings annotate, one does not) and asserts the
detector flags the odd one out. Asserting only "the real repo is clean today"
would pass equally well if the detector returned nothing at all.
"""
from __future__ import annotations

from lib.audit.detectors import c05_sibling_envelope as det
from lib.audit.model import FINDING, OK
from lib.audit.repo import Repo

_HELPER = (
    "def _cache():\n"
    "    return Cache(ROOT)\n"
    "\n"
    "def _freshness(snap):\n"
    "    if snap is None:\n"
    "        return {'captured_at': None, 'stale_warning': True}\n"
    "    return {'captured_at': snap.get('captured_at'), 'stale_warning': False}\n"
    "\n"
    "def tool_a():\n"
    "    snap = _cache().latest_snapshot()\n"
    "    return dict(_freshness(snap), items=snap['a'])\n"
    "\n"
    "def tool_b():\n"
    "    snap = _cache().latest_snapshot()\n"
    "    return dict(_freshness(snap), items=snap['b'])\n"
)

_SIBLING_OK = (
    "\ndef tool_c():\n"
    "    snap = _cache().latest_snapshot()\n"
    "    return dict(_freshness(snap), items=snap['c'])\n"
)

_SIBLING_BAD = (
    "\ndef tool_c():\n"
    "    snap = _cache().latest_snapshot()\n"
    "    return {'items': snap['c']}\n"
)

_WRITE_TOOL = (
    "\ndef tool_write(x):\n"
    "    return _cache().record(x)\n"
)


def _ctx_for(tmp_path, body):
    (tmp_path / "scripts" / "mcp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "mcp" / "m.py").write_text(body, encoding="utf-8")

    class _C:
        repo = Repo(tmp_path, tracked=["scripts/mcp/m.py"])
        ledgers = None
    return _C()


def test_flags_the_sibling_that_skipped_the_envelope(tmp_path):
    result = det.detect(_ctx_for(tmp_path, _HELPER + _SIBLING_BAD))
    findings = [v for v in result.verdicts if v.status == FINDING]
    assert len(findings) == 1
    assert findings[0].kind == "sibling-missing-envelope"
    assert "tool_c" in findings[0].instance_id


def test_clean_when_every_sibling_annotates(tmp_path):
    result = det.detect(_ctx_for(tmp_path, _HELPER + _SIBLING_OK))
    assert [v for v in result.verdicts if v.status == FINDING] == []


def test_write_side_tools_are_not_false_positives(tmp_path):
    """A tool that merely touches _cache() but never reads the snapshot must
    not be flagged. Requiring ALL shared accessors (not any) is what buys
    this — and a class whose findings are not believable gets ignored."""
    result = det.detect(_ctx_for(tmp_path, _HELPER + _SIBLING_OK + _WRITE_TOOL))
    findings = [v for v in result.verdicts if v.status == FINDING]
    assert findings == []
    write = [v for v in result.verdicts if "tool_write" in v.instance_id]
    assert write and write[0].kind == "not-a-snapshot-reader"


def test_modules_without_a_helper_are_out_of_boundary(tmp_path):
    body = ("def a():\n    return _cache().latest_snapshot()\n"
            "def b():\n    return _cache().latest_snapshot()\n"
            "def c():\n    return 1\n")
    result = det.detect(_ctx_for(tmp_path, body))
    assert result.boundary_size == 0


def test_real_repo_has_the_qflix_mcp_module_in_boundary(ctx):
    """The live instance the class came from must actually be inside the
    boundary — otherwise the class is closed over an empty set."""
    result = det.detect(ctx)
    assert result.metrics["modules_with_helper"] >= 1
    paths = {v.path for v in result.verdicts}
    assert any("qflix_mcp.py" in p for p in paths)
    assert [v for v in result.verdicts if v.status == FINDING] == []
    assert any(v.kind == "envelope-applied" and v.status == OK for v in result.verdicts)
