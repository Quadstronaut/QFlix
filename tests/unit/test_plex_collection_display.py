"""Plex collection display policy: hide collections, show their items.

WHY THIS EXISTS
A member browsing QFlix on someone else's TV on 2026-08-24 saw a shelf of
franchise names -- Deadpool, Dune, Gladiator, Godzilla, Guardians of the
Galaxy, Mad Max, Moana, Sonic, Venom -- with NOTHING behind any of them. 14 of
17 collections in Movies held zero items; both TV collections held zero. A
collection tile is a promise about the library, and these promised films that
had been reaped months earlier.

The live fix was applied by hand. This module is why it will still be true next
month: a live change nothing re-asserts is exactly the shape that let
fix-release-posters.py run zero times in fifteen days.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "configure" / "63-plex-collection-display.py"


def _load():
    spec = importlib.util.spec_from_file_location("plex_collection_display", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def m():
    return _load()


def test_mode_is_hide_collections_show_items(m):
    """0 and not 1. Mode 1 hides the ITEMS that are in collections, which would
    hide real films from members -- the opposite of the fix."""
    assert m.COLLECTION_MODE == "0"


def test_welcome_is_in_scope_here(m):
    """Welcome is excluded from the poster janitor (deliberate custom art) but
    NOT from this policy: a collection tile in the entitlement floor advertises
    content to exactly the people who cannot play it."""
    assert "QFlix - Welcome" in m.SECTION_NAMES
    assert len(m.SECTION_NAMES) == 5


def test_absent_pref_is_skipped_not_guessed(m, monkeypatch):
    """An absent collectionMode means this Plex version does not expose the
    setting. Writing blind would be a guess; None must not be read as 0."""
    monkeypatch.setattr(m, "_req", lambda *a, **k: (200, "<MediaContainer/>"))
    assert m.current_mode("tok", "4") is None


def test_current_mode_reads_the_live_value(m, monkeypatch):
    xml = '<MediaContainer><Setting id="collectionMode" value="2"/></MediaContainer>'
    monkeypatch.setattr(m, "_req", lambda *a, **k: (200, xml))
    assert m.current_mode("tok", "4") == "2"


def test_unreachable_prefs_endpoint_is_skipped(m, monkeypatch):
    """Cannot read means cannot assert. Returning None routes to SKIP, never to
    a write against an unknown current state."""
    monkeypatch.setattr(m, "_req", lambda *a, **k: (0, "connection refused"))
    assert m.current_mode("tok", "4") is None


def test_threshold_is_deliberately_untouched(m):
    """autoCollectionThreshold is NOT managed here, on purpose. A genuine
    3-film franchise collection is not a false positive, and silencing creation
    would also disarm the reaper prune -- it needs husks to exist in order to
    be observed removing them. If this constant ever appears, that decision was
    reversed without the comment being updated."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "autoCollectionThreshold" not in src.split('"""', 2)[2], \
        "threshold enforcement crept into the code without revisiting the docstring"
