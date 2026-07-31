"""The move must land where we validated, not where the path resolves later.

COUNCIL FINDING 14 (TOCTOU). rehome() checked containment with _is_contained(),
which calls realpath(), and then called os.rename(), which resolves the path
AGAIN, independently. Between those two resolutions the answer can change: swap
a symlink anywhere in the destination's ancestry and the rename writes outside
the library root while every guard reports success. Media then sits somewhere
Plex does not scan, with the *arr believing the import succeeded.

The fix renames relative to an open directory FD. An fd names a directory INODE,
so there is no second resolution to poison -- a swapped symlink is simply never
consulted.

The central test here performs a REAL swap between check and rename (POSIX only,
via a fault-injection hook) and asserts the file landed inside the validated
root. A structural test could not distinguish the fixed code from the broken
code, because both call os.rename with the same arguments.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
JANITOR = REPO / "scripts" / "maint" / "qflix-anime-janitor.py"


def _load():
    sys.path.insert(0, str(REPO / "scripts" / "maint"))
    sys.path.insert(0, str(REPO / "scripts" / "mcp"))
    spec = importlib.util.spec_from_file_location("anime_janitor_pin", JANITOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def m():
    return _load()


def _layout(tmp_path):
    """root/Show/ as the destination, plus a source file to move."""
    root = tmp_path / "media" / "Anime"
    (root / "Show").mkdir(parents=True)
    src = tmp_path / "downloads" / "Show" / "ep.mkv"
    src.parent.mkdir(parents=True)
    src.write_text("payload", encoding="utf-8")
    return root, src


# --- it still does the ordinary job ----------------------------------------

def test_a_normal_move_succeeds(m, tmp_path):
    root, src = _layout(tmp_path)
    dst = root / "Show" / "ep.mkv"
    assert m._pinned_rename(str(src), str(dst), str(root)) is None
    assert dst.read_text(encoding="utf-8") == "payload"
    assert not src.exists()


def test_destination_directly_under_root_is_allowed(m, tmp_path):
    root, src = _layout(tmp_path)
    dst = root / "ep.mkv"
    assert m._pinned_rename(str(src), str(dst), str(root)) is None
    assert dst.exists()


# --- it refuses the things it should ---------------------------------------

def test_a_destination_outside_the_root_is_refused(m, tmp_path):
    root, src = _layout(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    err = m._pinned_rename(str(src), str(outside / "ep.mkv"), str(root))
    assert err and "not under" in err
    assert src.exists(), "refused the move but moved the file anyway"


def test_a_missing_destination_parent_is_reported_not_raised(m, tmp_path):
    """Wording differs by platform -- the dir-fd path fails at open(), the
    fallback fails at rename() -- so assert the behaviour, not the string: it
    returns an error rather than raising, and it does not move the file."""
    root, src = _layout(tmp_path)
    err = m._pinned_rename(str(src), str(root / "Nope" / "ep.mkv"), str(root))
    assert err, "a missing destination parent was reported as success"
    assert "cannot open destination parent" in err or "rename failed" in err
    assert src.exists()


def test_a_trailing_slash_destination_is_refused(m, tmp_path):
    """basename('') would rename to the directory itself."""
    root, src = _layout(tmp_path)
    err = m._pinned_rename(str(src), str(root / "Show") + os.sep, str(root))
    assert err and "no final component" in err


def test_a_failed_rename_is_returned_not_raised(m, tmp_path):
    root, _src = _layout(tmp_path)
    err = m._pinned_rename(str(tmp_path / "not-there.mkv"),
                           str(root / "Show" / "ep.mkv"), str(root))
    assert err and "rename failed" in err


# --- the actual finding ----------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="symlink swap + renameat are POSIX")
def test_a_symlink_swapped_after_validation_cannot_redirect_the_move(m, tmp_path):
    """THE REGRESSION, executed rather than asserted structurally.

    `root/link` is a symlink into a legitimate directory inside the root. It is
    re-pointed at an outside directory AFTER _pinned_rename has resolved and
    opened it, but BEFORE the rename -- the exact window the finding describes.

    Pre-fix (plain os.rename to the path) the file lands in `outside`. Post-fix
    the fd already names the validated inode, so it lands inside.
    """
    root = tmp_path / "media" / "Anime"
    good = root / "real"
    good.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    link.symlink_to(good, target_is_directory=True)

    src = tmp_path / "ep.mkv"
    src.write_text("payload", encoding="utf-8")

    real_open = os.open

    def swapping_open(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        # We are now holding the validated directory. Re-point the symlink.
        if os.path.basename(str(path)) == "real":
            link.unlink()
            link.symlink_to(outside, target_is_directory=True)
        return fd

    os.open = swapping_open
    try:
        err = m._pinned_rename(str(src), str(link / "ep.mkv"), str(root))
    finally:
        os.open = real_open

    assert err is None, err
    assert (good / "ep.mkv").exists(), (
        "the move followed the swapped symlink -- media landed outside the "
        "library root while containment reported OK"
    )
    assert not (outside / "ep.mkv").exists(), "media escaped the library root"


@pytest.mark.skipif(os.name != "posix", reason="dir-fd rename is POSIX")
def test_it_actually_uses_a_directory_fd_on_this_platform(m):
    """Guard against the fallback silently becoming the only path -- every test
    above passes under plain os.rename except the swap test, so if dir_fd support
    were mis-detected the hardening would quietly not exist."""
    assert os.rename in os.supports_dir_fd


def test_the_forward_move_goes_through_the_pin(m):
    """Wiring check: the helper is useless if rehome() still calls os.rename."""
    src = JANITOR.read_text(encoding="utf-8")
    body = src[src.index("    # 4. move files"):]
    body = body[:body.index("    # 5. rescan")]
    assert "_pinned_rename(from_path, to_path, to_root)" in body, \
        "rehome() no longer routes the forward move through the pin"
    assert "os.rename(from_path, to_path)" not in body, \
        "the unpinned rename came back"
