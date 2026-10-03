"""59b creates Welcome by default and, with flags, the QFLX-4 Test library.
plexapi is NOT importable in CI, so only the argument surface is tested; the
script imports plexapi lazily inside main()."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "plex_welcome_59b", ROOT / "scripts" / "configure" / "59b-plex-welcome-library.py")
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)


def test_defaults_still_create_the_welcome_movie_library():
    a = M.build_parser().parse_args([])
    assert (a.title, a.agent, a.scanner) == ("QFlix - Welcome",
                                             "tv.plex.agents.movie", "Plex Movie")


def test_flags_select_personal_media_for_the_test_library():
    a = M.build_parser().parse_args([
        "--title", "QFlix - Test", "--path", "/x/Test",
        "--agent", "tv.plex.agents.none", "--scanner", "Plex Video Files Scanner"])
    assert (a.title, a.path, a.agent, a.scanner) == (
        "QFlix - Test", "/x/Test", "tv.plex.agents.none", "Plex Video Files Scanner")
