"""The reverse direction MOVES, and lands in the right root.

Reverse (main library -> anime library) was report-only. Per operator
instruction on 2026-07-30 it auto-moves: flagging a misfiled title and leaving
it in place is not a correction, and five titles had been flagged every day for
six days with nothing moved.

The sharp edge is NOT the move, it is WHERE it lands. sonarr2 and radarr2 each
expose TWO root folders and the FIRST is the main one:

    sonarr2 -> ['/home/.../media/TV Shows', '/home/.../media/Anime']
    radarr2 -> ['/home/.../media/Movies',   '/home/.../media/Anime Movies']

`_resolve_root` returned roots[0] and its result OVERRIDES the pair's declared
to_root, so a reverse move would have registered the title with the anime *arr
while leaving the files under the MAIN folder -- the arr would look correct and
Plex's Anime library would never see it. Verified against the live API before
writing the code.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
JANITOR = REPO / "scripts" / "maint" / "qflix-anime-janitor.py"


def _load():
    sys.path.insert(0, str(REPO / "scripts" / "maint"))
    sys.path.insert(0, str(REPO / "scripts" / "mcp"))
    spec = importlib.util.spec_from_file_location("anime_janitor_rev", JANITOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def m():
    return _load()


class FakeClient:
    """Stands in for an arr client; `roots` mirrors a real /rootfolder reply."""

    def __init__(self, roots):
        self._roots = roots

    def get(self, path):
        if path == "/rootfolder":
            return 200, [{"path": p} for p in self._roots]
        return 404, None


# The real reply from sonarr2 on 2026-07-30 — main root FIRST.
SONARR2_ROOTS = ["/home/quadstronaut/media/TV Shows",
                 "/home/quadstronaut/media/Anime"]
RADARR2_ROOTS = ["/home/quadstronaut/media/Movies",
                 "/home/quadstronaut/media/Anime Movies"]


def test_prefers_the_declared_root_over_roots_zero(m):
    """The regression. Without `prefer`, this returns 'TV Shows'."""
    got = m._resolve_root(FakeClient(SONARR2_ROOTS),
                          prefer="/home/quadstronaut/media/Anime")
    assert got == "/home/quadstronaut/media/Anime", (
        "reverse move would land in the MAIN folder while the arr showed it as "
        "anime — Plex's Anime library would never see the files"
    )


def test_movies_side_too(m):
    got = m._resolve_root(FakeClient(RADARR2_ROOTS),
                          prefer="/home/quadstronaut/media/Anime Movies")
    assert got == "/home/quadstronaut/media/Anime Movies"


def test_a_named_preference_that_is_absent_FAILS_CLOSED(m):
    """Reversed by council review: this used to assert the fail-OPEN behaviour.

    It originally expected paths[0] as a fallback. The council established that
    is wrong in the one direction that matters: on sonarr2/radarr2 paths[0] is
    the MAIN root, so a missing Anime root would send files back under the main
    tree while the record lived in the anime *arr -- and _is_contained() cannot
    catch it, because it compares against the ALREADY-RESOLVED root, so a
    wrong-but-consistent root passes containment.

    A caller that names a destination now gets that destination or nothing.
    Returning None aborts the move: one deferred title beats misfiled media.
    """
    assert m._resolve_root(FakeClient(["/only/one"]), prefer="/not/there") is None


def test_no_preference_still_falls_back(m):
    """The fail-closed rule applies only when a destination was NAMED."""
    assert m._resolve_root(FakeClient(["/only/one"])) == "/only/one"


def test_no_preference_keeps_the_old_behaviour(m):
    assert m._resolve_root(FakeClient(SONARR2_ROOTS)) == SONARR2_ROOTS[0]


def test_unreachable_arr_returns_none_not_a_guess(m):
    class Dead:
        def get(self, path):
            return 500, None

    assert m._resolve_root(Dead(), prefer="/x") is None


def test_reverse_pairs_are_the_exact_inverse_of_the_forward_pairs(m):
    """Roots and Plex sections must mirror, or a move goes somewhere silly."""
    fwd = {p["from_slug"]: p for p in m.ANIME_PAIRS}
    rev = {p["from_slug"]: p for p in m.REVERSE_PAIRS}
    assert set(rev) == {"sonarr", "radarr"}
    for r in m.REVERSE_PAIRS:
        f = fwd[r["to_slug"]]
        assert r["from_root"] == f["to_root"], "roots do not mirror"
        assert r["to_root"] == f["from_root"], "roots do not mirror"
        assert r["plex_from"] == f["plex_to"]
        assert r["plex_to"] == f["plex_from"]
        assert r["idkey"] == f["idkey"] and r["kind"] == f["kind"]


def test_series_moved_into_anime_switches_to_absolute_numbering(m):
    s = [p for p in m.REVERSE_PAIRS if p["kind"] == "series"][0]
    assert s["series_type"] == "anime"
    f = [p for p in m.ANIME_PAIRS if p["kind"] == "series"][0]
    assert f["series_type"] == "standard", "forward must still normalise to standard"


def test_reverse_uses_the_same_rehome_path_as_forward(m):
    """Not a parallel implementation — one code path, one safety envelope."""
    src = JANITOR.read_text(encoding="utf-8")
    assert src.count("def rehome(") == 1, "a second move implementation appeared"
    body = src[src.index("# --- main libraries: reverse"):]
    body = body[:body.index("# An anime-instance enumeration failure")]
    assert "auto_candidates.append((pair, rec))" in body, \
        "reverse hits no longer join the shared auto-move list"
    assert "flags.append" not in body, \
        "reverse reverted to flag-only"


def test_import_unverified_reverts_the_rename_instead_of_stranding(m):
    """COUNCIL FINDING 1 (undisputed, proven by executed test in two candidates).

    rehome() used to `os.rename()` the files to the destination, fail
    `_verify_import()`, and return WITHOUT reverting -- leaving files at the
    destination while the SOURCE *arr record still pointed at a path that no
    longer existed. The comment claimed this avoided orphaning; it created one.

    The cost is not cosmetic: the source *arr can drop its MediaFile rows on the
    next disk scan, mark the episodes missing, and hand them to the daily
    missing-search sweep to re-grab -- re-downloading media already on disk.

    Pinned structurally because the failing path needs a live *arr pair to
    exercise end-to-end; what must never regress is that the rename is undone.
    """
    src = JANITOR.read_text(encoding="utf-8")
    body = src[src.index("    # 5. rescan, then VERIFY"):]
    body = body[:body.index("    # 6. remove source")]
    assert "os.rename(to_path, from_path)" in body, (
        "import-unverified no longer reverts the rename -- files are left at the "
        "destination while the source record points at a vacated path"
    )
    assert "import-unverified AND revert failed" in body, (
        "the un-revertable case must be reported loudly with BOTH paths; it is "
        "the one state a human has to resolve"
    )
