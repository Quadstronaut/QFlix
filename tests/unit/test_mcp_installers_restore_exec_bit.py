"""QFLX-72: an installer that tars scripts/mcp from the workstation must chmod.

A Windows checkout writes every file 644 regardless of the git index, and tar
ships the on-disk mode. 70-mcp-install.sh learned this on 2026-08-23; 73, 73b
and 74 copied its tar line but not its fix, so running any of them reddened
deploy-drift with deploy-mode-drift (hit 2026-10-23 after the TV-park deploy).
Offline: reads installer text only.
"""
from pathlib import Path

CONFIGURE = Path(__file__).resolve().parents[2] / "scripts" / "configure"
TAR = 'cd "$REPO/scripts/mcp" && tar'
CHMOD = "find ~/scripts/mcp -name '*.py' -exec chmod 755 {} +"


def _tarring_installers():
    return sorted(p for p in CONFIGURE.glob("*.sh") if TAR in p.read_text(encoding="utf-8"))


def test_the_known_installers_are_found():
    names = {p.name for p in _tarring_installers()}
    assert {"70-mcp-install.sh", "73-quality-fallback-install.sh"} <= names


def test_every_mcp_tar_restores_the_exec_bit_after_extracting():
    bad = []
    for p in _tarring_installers():
        s = p.read_text(encoding="utf-8")
        if CHMOD not in s or s.index(CHMOD) < s.index(TAR):
            bad.append(p.name)
    assert bad == []
