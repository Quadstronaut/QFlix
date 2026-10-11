"""QFLX-50: every file the vlogs ingester imports from ~/scripts must be shipped
by the routine 240 deploy (and covered by deploy-drift), else a 240 run leaves
the box stale and the canary goes red.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INGESTER = REPO / "scripts" / "maint" / "qflix-vlogs-ingest.py"
INSTALL_240 = REPO / "scripts" / "configure" / "240-maintenance-install.sh"
INSTALL_80 = REPO / "scripts" / "configure" / "80-vlogs-install.sh"
DRIFT = REPO / "scripts" / "canaries" / "deploy-drift.sh"


def _repo_local_imports() -> set[str]:
    """Repo paths of modules the ingester imports from scripts/mcp or
    scripts/maint (everything else is stdlib)."""
    tree = ast.parse(INGESTER.read_text(encoding="utf-8"))
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            names.add(n.module.split(".")[0])
    out = set()
    for name in names:
        for d in ("mcp", "maint"):
            if (REPO / "scripts" / d / f"{name}.py").is_file():
                out.add(f"scripts/{d}/{name}.py")
    return out


def _staged(text: str) -> set[str]:
    return set(re.findall(r"^\s*(scripts/[\w./-]+?)\s*\\?$", text, re.M))


def _need() -> set[str]:
    return _repo_local_imports() | {"scripts/maint/qflix-vlogs-ingest.py"}


def test_ingester_imports_resolve_to_something():
    assert "scripts/mcp/logs.py" in _repo_local_imports()


def test_240_stages_and_copies_ingester_and_every_import():
    text = INSTALL_240.read_text(encoding="utf-8")
    missing = _need() - _staged(text)
    assert not missing, f"240 does not stage: {sorted(missing)}"
    for rel in _need():
        assert f'"$STG"/{rel} ' in text, f"240 stages but never copies {rel}"


def test_80_stages_every_import_for_first_install():
    missing = _need() - _staged(INSTALL_80.read_text(encoding="utf-8"))
    assert not missing, f"80 does not stage: {sorted(missing)}"


def test_240_does_not_restart_victorialogs():
    assert "restart victorialogs" not in INSTALL_240.read_text(encoding="utf-8")


def test_deploy_drift_covers_deployed_py_files():
    text = DRIFT.read_text(encoding="utf-8")
    assert "*.py" in text and "scripts" in text
