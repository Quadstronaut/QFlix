"""C-04 — mtime accessors, scored for freshness context."""
from __future__ import annotations

from lib.audit.detectors import c04_mtime_freshness as det
from lib.audit.model import FINDING, OK
from lib.audit.repo import Repo


def _fake(tmp_path, files):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    class _C:
        repo = Repo(tmp_path, tracked=sorted(files))
        ledgers = None
    return _C()


def test_enumerates_every_accessor_reference(ctx):
    result = det.detect(ctx)
    assert result.boundary_size == len(result.verdicts)
    assert result.metrics["mtime_references"] == result.boundary_size
    assert result.boundary_size > 0


def test_covers_bash_and_powershell_not_just_python(ctx):
    """A Python-only detector would have declared the class closed while two
    thirds of the surface went unlooked-at."""
    result = det.detect(ctx)
    suffixes = {v.path.rsplit(".", 1)[-1] for v in result.verdicts}
    assert "sh" in suffixes
    assert "py" in suffixes


def test_freshness_context_is_what_distinguishes_a_finding(tmp_path):
    c = _fake(tmp_path, {
        "scripts/a.py": (
            "def is_stale(p):\n"
            "    return p.stat().st_mtime < cutoff\n"
        ),
        "scripts/b.py": (
            "def copy_meta(src, dst):\n"
            "    os.utime(dst, (src.stat().st_mtime, src.stat().st_mtime))\n"
        ),
    })
    verdicts = {v.path: v for v in det.detect(c).verdicts}
    assert verdicts["scripts/a.py"].status == FINDING
    assert verdicts["scripts/a.py"].kind == "freshness-from-mtime"
    assert verdicts["scripts/b.py"].status == OK


def test_age_based_housekeeping_is_not_a_finding(tmp_path):
    """Deleting files older than N days is the one place the file clock IS the
    subject rather than a proxy for content."""
    c = _fake(tmp_path, {
        "scripts/prune.py": (
            "def prune(d, cutoff):\n"
            "    for old in d.glob('*.log'):\n"
            "        if old.stat().st_mtime < cutoff:\n"
            "            old.unlink()\n"
        ),
    })
    v = det.detect(c).verdicts[0]
    assert v.status == OK and v.kind == "mtime-housekeeping"
