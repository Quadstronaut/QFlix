"""bazarr2-sync `_ensure_frontend` — the missing-web-UI heal (2026-08-18).

WHY: bazarr2 is pinned by GIT TAG checkout, but upstream only ships the built
Vite frontend inside the release zip — so every checkout leaves bin/frontend/
as unbuilt source and the UI 500s with TemplateNotFound. It did exactly that,
silently, from the Jul 6 install until the 2026-08-18 post-storm audit found
it: the API stayed healthy, all automation is API-driven, and nothing looked
at the UI for six weeks.

These tests pin the extraction logic offline: prefix-agnostic member matching
(the zip's internal root has moved before), zip-slip refusal, idempotence, and
the degraded-not-fatal contract.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    """Load bazarr2-sync.py with BAZARR2_BIN pointed at a sandbox."""
    spec = importlib.util.spec_from_file_location(
        "bazarr2_sync_undertest", REPO_ROOT / "scripts" / "maint" / "bazarr2-sync.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    m.BAZARR2_BIN = tmp_path / "bin"
    m.BAZARR2_BIN.mkdir()
    return m


def _zip_bytes(members):
    """Build an in-memory zip; members = {name: content}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)
    return buf.getvalue()


def _serve_zip(monkeypatch, mod, payload):
    """Monkeypatch urlopen to hand back `payload` as the release asset."""
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=0):
        fake_urlopen.last_url = url
        return _Resp(payload)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    return fake_urlopen


def test_extracts_build_regardless_of_zip_root_prefix(mod, monkeypatch, tmp_path):
    """The release zip's internal layout has moved before; the installer must
    find frontend/build/ by content, not assume a root."""
    payload = _zip_bytes({
        "bazarr-1.5.3/frontend/build/index.html": "<html>ui</html>",
        "bazarr-1.5.3/frontend/build/assets/app.js": "js",
        "bazarr-1.5.3/bazarr.py": "not extracted",
    })
    _serve_zip(monkeypatch, mod, payload)
    assert mod._ensure_frontend("1.5.3") is True
    build = mod.BAZARR2_BIN / "frontend" / "build"
    assert (build / "index.html").read_text() == "<html>ui</html>"
    assert (build / "assets" / "app.js").exists()
    # Only the build tree lands — the rest of the zip stays out.
    assert not (mod.BAZARR2_BIN / "bazarr.py").exists()


def test_requests_the_matching_tag(mod, monkeypatch):
    payload = _zip_bytes({"frontend/build/index.html": "x"})
    fake = _serve_zip(monkeypatch, mod, payload)
    assert mod._ensure_frontend("1.5.3") is True
    assert "/download/v1.5.3/bazarr.zip" in fake.last_url


def test_idempotent_unless_forced(mod, monkeypatch):
    """An existing build short-circuits (no network); force=True refreshes —
    a version bump makes the old build stale."""
    build = mod.BAZARR2_BIN / "frontend" / "build"
    build.mkdir(parents=True)
    (build / "index.html").write_text("old")

    def explode(*a, **kw):
        raise AssertionError("network touched despite existing build")
    monkeypatch.setattr(mod.urllib.request, "urlopen", explode)
    assert mod._ensure_frontend("1.5.3") is True
    assert (build / "index.html").read_text() == "old"

    payload = _zip_bytes({"frontend/build/index.html": "new"})
    _serve_zip(monkeypatch, mod, payload)
    assert mod._ensure_frontend("1.5.4", force=True) is True
    assert (build / "index.html").read_text() == "new"


def test_zip_slip_members_are_refused(mod, monkeypatch, tmp_path):
    """A hostile member path must not escape BAZARR2_BIN."""
    payload = _zip_bytes({
        "../../evil/frontend/build/index.html": "evil",
    })
    _serve_zip(monkeypatch, mod, payload)
    # The only build-ish member is slip-shaped and gets filtered, so the
    # install FAILS (no members) rather than writing outside the sandbox.
    assert mod._ensure_frontend("1.5.3") is False
    assert not (tmp_path.parent / "evil").exists()


def test_zip_without_build_fails_cleanly(mod, monkeypatch):
    payload = _zip_bytes({"bazarr.py": "no frontend here"})
    _serve_zip(monkeypatch, mod, payload)
    assert mod._ensure_frontend("1.5.3") is False


def test_network_failure_is_degraded_not_fatal(mod, monkeypatch):
    """The UI is cosmetic relative to the subtitle pipeline; a GitHub outage
    must return False (caller logs WARN and continues), never raise."""
    def boom(*a, **kw):
        raise OSError("github unreachable")
    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    assert mod._ensure_frontend("1.5.3") is False


# ---------------------------------------------------------------------------
# COUNCIL 2026-08-18 (gen-opus-1 F-03 / gen-opus-2 QF-01): a failed heal used
# to push Kuma "up: in sync" - the six-week silent-UI class re-opened on the
# heal's own failure branch. A missing UI must be a signal, not a logfile WARN.
# ---------------------------------------------------------------------------

def _wire_main(mod, monkeypatch, *, heal_result):
    """Drive main() down the versions-match path with a missing build."""
    pushes = []
    monkeypatch.setattr(mod, "_read_apikey", lambda p: "k")
    monkeypatch.setattr(mod, "_api_version", lambda base, key: "1.6.0")
    monkeypatch.setattr(mod, "_ensure_frontend", lambda v, force=False: heal_result)
    monkeypatch.setattr(mod, "_systemctl", lambda *a: 0)
    monkeypatch.setattr(mod, "_wait_for_bazarr2", lambda key, timeout_s=60: True)
    monkeypatch.setattr(mod, "_push_kuma", lambda status, msg: pushes.append((status, msg)))
    return pushes


def test_failed_heal_pushes_down_not_up(mod, monkeypatch):
    pushes = _wire_main(mod, monkeypatch, heal_result=False)
    rc = mod.main()
    assert rc == 1
    assert pushes and pushes[-1][0] == "down"
    assert "UI" in pushes[-1][1], "the reason must name the UI, not a generic failure"


def test_successful_heal_pushes_up(mod, monkeypatch):
    pushes = _wire_main(mod, monkeypatch, heal_result=True)
    rc = mod.main()
    assert rc == 0
    assert pushes[-1] == ("up", "in sync at 1.6.0")


def test_present_build_stays_up_without_healing(mod, monkeypatch):
    build = mod.BAZARR2_BIN / "frontend" / "build"
    build.mkdir(parents=True)
    (build / "index.html").write_text("ui")
    pushes = _wire_main(mod, monkeypatch, heal_result=False)  # heal must not run
    rc = mod.main()
    assert rc == 0
    assert pushes[-1] == ("up", "in sync at 1.6.0")
