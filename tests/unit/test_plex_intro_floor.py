"""plex-intro-markers: the coverage floor is one number, stated the same everywhere.

QFLX-105 (2026-11-11): operator lowered the default floor 55 -> 33 as a
PROVISIONAL value ("we don't know what this number should be yet"). The number
lives in the canary code, its own tunables table, the manifest comment and the
canaries README; a later change that moves one and not the others would leave
the docs lying about what pages. This pins them together and keeps the
provisional marker attached to the number until someone recalibrates it.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CANARY = (REPO / "scripts" / "canaries" / "plex-intro-markers.sh").read_text(encoding="utf-8")
FLOOR = 33


def test_code_default_is_the_operator_floor():
    m = re.search(r'MIN_PCT = float\(os\.environ\.get\("PLEX_INTRO_MIN_PCT"\) or (\d+)\)', CANARY)
    assert m, "PLEX_INTRO_MIN_PCT default not found"
    assert int(m.group(1)) == FLOOR


def test_tunables_table_matches_code():
    assert re.search(rf"PLEX_INTRO_MIN_PCT\s+default {FLOOR}\b", CANARY)


def test_docs_state_the_same_floor_and_that_it_is_provisional():
    readme = (REPO / "scripts" / "canaries" / "README.md").read_text(encoding="utf-8")
    apps = (REPO / "manifest" / "apps.yaml").read_text(encoding="utf-8")
    assert f"`PLEX_INTRO_MIN_PCT` (default **{FLOOR}, provisional**" in readme
    assert f"default {FLOOR}%, PROVISIONAL" in apps
    assert "PROVISIONAL floor" in CANARY
    for text in (readme, apps, CANARY):
        assert "default 55" not in text and "default **55" not in text
