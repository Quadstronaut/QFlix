"""Behind-the-scenes recap tests — the commit-driven newsletter section that
replaced the retired Gemini AI Picks.

Covers three layers: the changelog parser/builder (`parse_commit`,
`build_behind_scenes`), the `BehindScenes` view-model properties, and the
render integration (both the Claude-authored blurb path and the deterministic
commit-recap path) — closing the coverage gap flagged in the 2026-07-09 audit
where `behind_scenes` was only ever exercised as `None`.
"""
from __future__ import annotations

import datetime as _dt

from qflix_newsletter.changelog import (
    BehindScenes,
    Commit,
    build_behind_scenes,
    parse_commit,
)
from qflix_newsletter.render import build_email_context, render_html


def _commit(type_: str, summary: str, *, friendly=None, sha="abc1234", scope=None) -> Commit:
    return Commit(sha=sha, type=type_, scope=scope, summary=summary, friendly=friendly)


# ---- parse_commit -------------------------------------------------------

def test_parse_commit_splits_conventional_subject():
    c = parse_commit("deadbee", "feat(newsletter): add poster mirroring")
    assert c.type == "feat"
    assert c.scope == "newsletter"
    assert c.summary == "add poster mirroring"
    assert c.friendly is None


def test_parse_commit_non_conventional_keeps_whole_subject():
    c = parse_commit("deadbee", "random uncategorized subject")
    assert c.type == ""
    assert c.summary == "random uncategorized subject"


def test_parse_commit_extracts_friendly_trailer():
    msg = "fix(plex): raise per-user kill cap\n\nNewsletter: Streams stop cleaner now."
    c = parse_commit("deadbee", msg)
    assert c.friendly == "Streams stop cleaner now."
    assert c.display == "Streams stop cleaner now."  # friendly overrides the raw subject


# ---- build_behind_scenes ------------------------------------------------

def test_build_behind_scenes_buckets_by_type():
    commits = [
        _commit("feat", "new thing A"),
        _commit("feat", "new thing B"),
        _commit("fix", "squash bug"),
        _commit("perf", "faster path"),   # perf is bucketed as a fix
        _commit("docs", "tweak readme"),  # other
        _commit("chore", "bump dep"),     # other
        _commit("", "uncategorized"),     # other
    ]
    bs = build_behind_scenes(commits)
    assert [c.summary for c in bs.features] == ["new thing A", "new thing B"]
    assert {c.summary for c in bs.fixes} == {"squash bug", "faster path"}
    assert bs.feature_count == 2
    assert bs.fix_count == 2
    assert bs.other_count == 3


def test_build_behind_scenes_caps_bullets_but_reports_full_count():
    commits = [_commit("feat", f"feature {i}") for i in range(9)]
    bs = build_behind_scenes(commits, max_bullets=6)
    assert len(bs.features) == 6      # visible list capped
    assert bs.feature_count == 9      # full count preserved
    assert bs.feature_overflow == 3   # 9 - 6 rendered as "+3 more"


def test_commit_display_prefers_friendly_over_summary():
    assert _commit("feat", "raw subject", friendly="Member-friendly line").display == "Member-friendly line"
    assert _commit("feat", "raw subject").display == "raw subject"


# ---- BehindScenes view-model properties --------------------------------

def test_has_items_false_when_empty():
    assert BehindScenes().has_items is False


def test_has_items_true_with_any_content():
    assert BehindScenes(features=[_commit("feat", "x")]).has_items is True
    assert BehindScenes(blurb_html="<p>hi</p>").has_items is True
    assert BehindScenes(upgrade_total=1).has_items is True


def test_upgrade_phrase_grammar_by_arity():
    assert BehindScenes(upgrade_named=["Plex"], upgrade_total=1).upgrade_phrase == \
        "Updated Plex for speed, stability, and the latest features."
    assert BehindScenes(upgrade_named=["Plex", "Sonarr"], upgrade_total=2).upgrade_phrase == \
        "Updated Plex and Sonarr for speed, stability, and the latest features."
    assert BehindScenes(upgrade_named=["Plex", "Sonarr", "Radarr"], upgrade_total=3).upgrade_phrase == \
        "Updated Plex, Sonarr, and Radarr for speed, stability, and the latest features."


def test_upgrade_phrase_buckets_unnamed_apps_with_plural_agreement():
    bs = BehindScenes(upgrade_named=["Plex"], upgrade_other_count=4, upgrade_total=5)
    assert bs.upgrade_phrase == \
        "Updated Plex and 4 behind-the-scenes apps for speed, stability, and the latest features."
    bs1 = BehindScenes(upgrade_other_count=1, upgrade_total=1)  # singular "app"
    assert "1 behind-the-scenes app for" in bs1.upgrade_phrase


def test_upgrade_phrase_empty_when_nothing_upgraded():
    assert BehindScenes().upgrade_phrase == ""


# ---- render integration -------------------------------------------------

def _ctx_with(bs):
    return build_email_context(
        recent=[],
        coming=[],
        behind_scenes=bs,
        library_stats={"total_items": 1, "sections": []},
        public_host="seedbox.example.com",
        kuma_public_host="kuma.seedbox.example.com",
        now=_dt.datetime(2026, 6, 27, 12, 0, 0),
    )


# Section-presence sentinel: this subtitle appears only inside the rendered
# section (Jinja comments are stripped), so it cleanly distinguishes present
# from omitted.
_SECTION_MARKER = "What we improved for you this week"


def test_render_deterministic_path_shows_commits_and_tuneups():
    bs = BehindScenes(
        features=[_commit("feat", "Added usenet search")],
        fixes=[_commit("fix", "Fixed stuck downloads")],
        feature_count=1,
        fix_count=1,
        upgrade_named=["Plex"],
        upgrade_total=1,
    )
    html = render_html(_ctx_with(bs))
    assert _SECTION_MARKER in html
    assert "Added usenet search" in html
    assert "Fixed stuck downloads" in html
    assert "This week's tune-ups" in html                 # deterministic upgrade line shown
    assert "Updated Plex for speed" in html


def test_render_blurb_path_renders_html_and_suppresses_upgrade_line():
    bs = BehindScenes(
        blurb_html="<p>We made streaming smoother this week.</p>",
        upgrade_named=["Plex"],
        upgrade_total=1,
    )
    html = render_html(_ctx_with(bs))
    assert _SECTION_MARKER in html
    assert "We made streaming smoother this week." in html  # blurb rendered as trusted HTML
    assert "This week's tune-ups" not in html               # blurb suppresses the duplicate line
    assert "Updated Plex" not in html


def test_render_omits_section_when_behind_scenes_none():
    html = render_html(_ctx_with(None))
    assert _SECTION_MARKER not in html


def test_render_escapes_untrusted_commit_text():
    bs = BehindScenes(
        features=[_commit("feat", "add <script>alert(1)</script> & more")],
        feature_count=1,
    )
    html = render_html(_ctx_with(bs))
    assert "<script>alert(1)</script>" not in html  # commit text is not injected raw
    assert "&lt;script&gt;" in html                 # it is HTML-escaped
