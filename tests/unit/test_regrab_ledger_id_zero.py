"""Episode id 0 must not silently vanish from a ledger key.

Stage-2 boundaries lens, 2026-09-17. The truthiness filter inside the episode
id list dropped a 0, so a row whose real identity is {0, 5} keyed identically
to a row that is only {5}; the two then co-accumulated blocklist strikes
toward one park threshold -- a silent mis-park, the forbidden direction.

The SERIES id is deliberately different and that asymmetry is the point:
series_id=0 yields UNKEYABLE, because with no series identity there is no key
at all and the guard simply steps aside (INV-7), which is safe. A wrong key is
not safe. *arr ids start at 1 in practice, so both behaviours are defensive.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "maint"))


def _rl(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    import lib.regrab_ledger as rl  # type: ignore
    return importlib.reload(rl)


def test_episode_id_zero_survives_into_the_key(tmp_path, monkeypatch):
    rl = _rl(tmp_path, monkeypatch)
    assert rl.episode_key("sonarr", 100, [0, 5]) != rl.episode_key("sonarr", 100, [5])


def test_a_lone_zero_is_still_a_real_identity(tmp_path, monkeypatch):
    rl = _rl(tmp_path, monkeypatch)
    key = rl.episode_key("sonarr", 100, [0])
    assert key is not None, "a row covering episode 0 is keyable, not unknown"
    assert key.endswith("|0")


def test_series_id_zero_stays_unkeyable(tmp_path, monkeypatch):
    """The safe direction: no series identity -> no key -> guard steps aside."""
    rl = _rl(tmp_path, monkeypatch)
    assert rl.episode_key("sonarr", 0, [1]) is None


def test_distinct_rows_never_share_a_key(tmp_path, monkeypatch):
    rl = _rl(tmp_path, monkeypatch)
    keys = {
        rl.episode_key("sonarr", 100, [0]),
        rl.episode_key("sonarr", 100, [5]),
        rl.episode_key("sonarr", 100, [0, 5]),
        rl.episode_key("sonarr", 101, [0, 5]),
        rl.episode_key("sonarr2", 100, [0, 5]),
    }
    assert len(keys) == 5, f"identities collided: {keys}"


def test_ordering_and_duplicates_still_normalise(tmp_path, monkeypatch):
    """Keeping 0 must not break the sort/dedupe that makes keys canonical."""
    rl = _rl(tmp_path, monkeypatch)
    assert rl.episode_key("sonarr", 7, [5, 0, 5]) == rl.episode_key("sonarr", 7, [0, 5])


def test_row_episode_ids_keeps_zero_in_every_shape():
    """The three queue-row shapes must agree with the key builder."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "arrhk", REPO / "scripts" / "maint" / "arr-housekeeping.py")
    arrhk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(arrhk)

    assert arrhk._row_episode_ids({"episodeId": 0}) == [0]
    assert arrhk._row_episode_ids({"episodeIds": [0, 5]}) == [0, 5]
    assert arrhk._row_episode_ids({"episodes": [{"id": 0}, {"id": 5}]}) == [0, 5]
    # booleans are still rejected: True would otherwise read as episode 1
    assert arrhk._row_episode_ids({"episodeId": True}) == []
    assert arrhk._row_episode_ids({"episodeIds": [True, False]}) == []
