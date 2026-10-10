"""Call sites re-routed through ~/bin/appctl (QFLX-18, spec 5.2 + review G-3/O-3).

Two properties per call site:
  1. no executable line still calls the panel tool `app-<x>` directly for the
     verbs appctl now owns (comments may still mention it);
  2. every appctl reference is ABSOLUTE (`~/bin/appctl`, `$HOME/bin/appctl`,
     `%h/bin/appctl`). A bare `appctl` fails ENOENT under the systemd --user
     default PATH, which does not include ~/bin (review G-3).
"""
from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# file -> panel tools that must no longer be invoked from executable lines
ROUTED = {
    "scripts/ops/tautulli-gate-watch.sh": ["app-tautulli"],
    "scripts/configure/50-tautulli-pms-url-fix.sh": ["app-tautulli"],
    "scripts/configure/60-www-images.sh": ["app-nginx"],
    "scripts/configure/91-nginx-root-to-dash.sh": ["app-nginx"],
    "scripts/configure/31-unpackerr.sh": ["app-unpackerr"],
    "scripts/maint/flaresolverr-canary.py": ["app-flaresolverr"],
}

_ABS = re.compile(r"(?:~|\$HOME|\$\{HOME\}|%h)/bin/appctl")


def _code_lines(text: str):
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        yield ln


@pytest.mark.parametrize("rel,tools", sorted(ROUTED.items()))
def test_no_direct_panel_call_remains(rel, tools):
    text = (REPO / rel).read_text(encoding="utf-8")
    for ln in _code_lines(text):
        for tool in tools:
            # Python: only the docstring/comment may name it; f-strings and
            # defaults may not.
            assert not re.search(r"\b%s\b" % re.escape(tool), ln), (rel, ln)
    assert _ABS.search(text), "%s must call the absolute ~/bin/appctl" % rel


@pytest.mark.parametrize("rel", sorted(ROUTED) + [
    "scripts/install/lib/app-install.sh",
    "scripts/maint/lib/recovery.py",
])
def test_every_appctl_reference_is_absolute(rel):
    text = (REPO / rel).read_text(encoding="utf-8")
    hits = list(re.finditer(r"appctl", text))
    assert hits, rel
    for m in hits:
        ctx = text[max(0, m.start() - 30):m.end() + 10]
        assert text[m.start() - 4:m.start()] == "bin/", (rel, ctx)
        assert _ABS.search(text[max(0, m.start() - 12):m.end()]), (rel, ctx)


def test_flaresolverr_restart_default_is_absolute_appctl(monkeypatch):
    monkeypatch.delenv("FS_RESTART_CMD", raising=False)
    spec = importlib.util.spec_from_file_location(
        "fs_canary_callsite", REPO / "scripts" / "maint" / "flaresolverr-canary.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    argv = mod.FS_RESTART_CMD.split()
    assert argv[0] == os.path.expanduser("~/bin/appctl")
    assert argv[0].replace("\\", "/").endswith("/bin/appctl")
    assert os.path.isabs(argv[0])
    assert argv[1:] == ["restart", "flaresolverr"]


def test_app_install_guards_native_and_dormant():
    text = (REPO / "scripts" / "install" / "lib" / "app-install.sh").read_text(encoding="utf-8")
    guard = text.index("bin/appctl is-native")
    assert guard < text.index("install -p"), "guard must run before the UCC install"


def test_recovery_hint_names_appctl():
    text = (REPO / "scripts" / "maint" / "lib" / "recovery.py").read_text(encoding="utf-8")
    assert "~/bin/appctl upgrade" in text
    assert "`app-{slug} upgrade`" not in text


def test_240_deploys_appctl_and_ports():
    text = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    assert "scripts/lib/appctl \\" in text
    assert "scripts/maint/lib/ports.py \\" in text
    assert re.search(r'cp -f "\$STG"/scripts/lib/appctl ~/bin/appctl\.new', text)
    assert "mv -f ~/bin/appctl.new ~/bin/appctl" in text
    assert "chmod +x ~/bin/appctl" in text
