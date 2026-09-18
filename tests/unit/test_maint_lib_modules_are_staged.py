"""Every lib module a STAGED maint script imports must itself be staged.

WHY THIS FILE EXISTS (2026-09-17)
---------------------------------
`scripts/configure/240-maintenance-install.sh` tars an EXPLICIT list of files
to the box — no globbing, on purpose, because the slot is shared. That makes
every new module under `scripts/maint/lib/` a deploy hazard until somebody
remembers to add a line.

This change added `lib/page_ledger.py` and made `lib/recovery.py` import it at
module scope. Forget the tar line and `manitoba-maint` does not start: the
pusher, every recovery, and the fleet heartbeat die on ImportError — a total
outage of the self-healing layer, caused by a file that looked fine in git and
passed every test.

tests/unit/test_scheduled_scripts_are_staged.py guards the other half (the
top-level scripts a timer or cron invokes). Nothing guarded the imports those
scripts pull in behind them. "It sat wrong for N days" means a detector is
missing, so here is the detector.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAINT_LIB = REPO / "scripts" / "maint" / "lib"

# `from lib import a, b` / `from lib.a import x` / `import lib.a`
_FROM_LIB_IMPORT = re.compile(r"^\s*from\s+lib\s+import\s+([^\n#]+)", re.M)
_FROM_LIB_DOT = re.compile(r"^\s*from\s+lib\.([A-Za-z_][A-Za-z0-9_]*)", re.M)
_IMPORT_LIB_DOT = re.compile(r"^\s*import\s+lib\.([A-Za-z_][A-Za-z0-9_]*)", re.M)


def _installer_text() -> str:
    out = []
    for d in ("scripts/configure", "scripts/install"):
        base = REPO / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file():
                try:
                    out.append(p.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    continue
    return "\n".join(out)


def _imported_lib_modules(text: str) -> set[str]:
    names: set[str] = set()
    for group in _FROM_LIB_IMPORT.findall(text):
        # "health, kuma, lifecycle as lc" -> health, kuma, lifecycle
        for part in group.replace("(", "").replace(")", "").split(","):
            part = part.strip().split(" as ")[0].strip()
            if part and part.isidentifier():
                names.add(part)
    names.update(_FROM_LIB_DOT.findall(text))
    names.update(_IMPORT_LIB_DOT.findall(text))
    return names


def test_every_imported_maint_lib_module_is_in_the_installer():
    installer = _installer_text()

    staged_files = sorted(
        p for p in (REPO / "scripts" / "maint").rglob("*.py")
        if f"scripts/maint/{p.relative_to(REPO / 'scripts' / 'maint').as_posix()}" in installer
    )
    assert staged_files, "no staged maint python files found — did the installer move?"

    missing: list[str] = []
    for path in staged_files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in _imported_lib_modules(text):
            module = MAINT_LIB / f"{name}.py"
            if not module.is_file():
                # Namespace-merged: the module physically lives in another
                # lib/ (scripts/mcp/lib, ...) and is not this installer's job.
                continue
            if f"scripts/maint/lib/{name}.py" not in installer:
                missing.append(
                    f"{path.relative_to(REPO).as_posix()} imports lib.{name}, "
                    f"but scripts/maint/lib/{name}.py is staged by no installer")
    assert not missing, (
        "unstaged lib module(s) — manitoba-maint will die on ImportError after "
        "the next deploy:\n  " + "\n  ".join(sorted(set(missing))))


def test_page_ledger_is_staged_beside_recovery():
    """The specific instance, pinned: deleting the tar line must go red."""
    installer = _installer_text()
    assert "scripts/maint/lib/page_ledger.py" in installer
    assert "scripts/maint/lib/recovery.py" in installer
