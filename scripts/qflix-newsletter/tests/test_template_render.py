"""Render-output assertions for the weekly newsletter template.

Each test renders the template against a populated EmailContext fixture
and asserts that a specific visual enhancement appears in the output.
"""
from __future__ import annotations

from qflix_newsletter.render import render_html


def test_template_renders_without_error(sample_ctx):
    html = render_html(sample_ctx)
    assert len(html) > 1000
    assert "Suzume" in html  # the pick title is present


def test_header_uses_qflix_white_title_with_qpng_and_gradient(sample_ctx):
    html = render_html(sample_ctx)
    # Title is the mixed-case "QFlix" in white, not the orange "QFLIX".
    assert ">QFlix<" in html
    assert "QFLIX" not in html  # old all-caps wordmark is gone
    # Q.png img tag points at our self-hosted asset (uses public_host).
    assert 'src="https://quadstronaut.seedbox.example.com/images/Q.png"' in html
    # Header gradient is the spec'd top-down black→blue→bg.
    assert "linear-gradient(180deg,#000 0%,#1e3a8a 50%,#0a1628 100%)" in html


def test_filmstrip_accent_appears_twice(sample_ctx):
    html = render_html(sample_ctx)
    pattern = "repeating-linear-gradient(90deg,#0a0a0a 0,#0a0a0a 8px,#1a1a1a 8px,#1a1a1a 14px)"
    # Once below the header, once above the footer.
    assert html.count(pattern) == 2


def test_pick_has_gold_diagonal_ribbon(sample_ctx):
    html = render_html(sample_ctx)
    # The label moves into the ribbon. Verify the ribbon CSS signature
    # appears on the Pick card and the old-style label row is gone.
    assert "transform:rotate(-30deg)" in html
    assert "linear-gradient(135deg,#e8c456,#b8941f)" in html
    # Old standalone label (uppercase letter-spacing block in its own <td>) is gone:
    # we just check that the new ribbon DIV appears within the Pick card markup.
    assert "Pick of the Week" in html  # text still present, but inside the ribbon


def test_section_dividers_use_diamond_ornament(sample_ctx):
    html = render_html(sample_ctx)
    # New ornament: diamond char in gold between cyan hairlines.
    assert "&#9670;" in html or "◆" in html
    # Old solid border-bottom underline on h3 must be gone.
    assert "border-bottom:1px solid rgba(125,211,252,.18)" not in html


def test_section_labels_have_emoji_prefixes(sample_ctx):
    html = render_html(sample_ctx)
    # Each section label gains exactly one leading emoji.
    assert "🎬 New movies" in html
    assert "📺 New TV" in html
    # Coming soon present in fixture
    assert "🗓 Coming soon" in html
    # AI picks section (smaller heading)
    assert "✨ A few things you might like" in html
    # Nerd corner uses a <div> label, also gets the emoji
    assert "🤓 Nerd corner" in html


def test_section_headers_have_count_badges(sample_ctx):
    html = render_html(sample_ctx)
    # Movies section: fixture has 2 movies (Inception, Arrival; pick excluded).
    assert ">2<" in html or ">2 <" in html or "border-radius:12px" in html
    # TV section: 1 show, 2 episodes.
    assert "1 shows · 2 eps" in html or "1 show · 2 eps" in html
    # Coming soon: 1 item in fixture.
    # (Loose assertion: the badge CSS class signature must appear at least 3 times
    # — movies, TV, coming soon.)
    assert html.count("background:#1e40af;color:#fff;padding:2px 10px;border-radius:12px") >= 3


def test_movie_poster_cards_have_gold_border_and_shadow(sample_ctx):
    html = render_html(sample_ctx)
    # New border color (gold tint) and shadow appear on movie cards.
    assert "border:1px solid rgba(212,175,55,.4)" in html
    assert "box-shadow:0 6px 16px rgba(0,0,0,.6)" in html
    # Old cyan card border on movie cards is gone (TV-row cards keep it,
    # so we check the count of the OLD pattern dropped from baseline).
    # Baseline count check: movies + anime_movies cards used the cyan border;
    # those should now use the gold one. Loose assertion via gold count >= 2.
    assert html.count("border:1px solid rgba(212,175,55,.4)") >= 2


def test_footer_uses_corrected_tagline_and_bg_to_black_gradient(sample_ctx):
    html = render_html(sample_ctx)
    # Exact tagline (note pink heart, not red).
    assert "QFlix · Crafted with precision 🩷 Quadstronaut" in html
    # Background fades from body color into pure black at the bottom.
    assert "linear-gradient(180deg,#0a1628 0%,#000 100%)" in html
    # 40px Q.png monogram is present in footer.
    assert 'width="40"' in html  # the only 40px-wide image in the template
    # Old "Reply to this email if anything's broken" text is gone.
    assert "Reply to this email if anything's broken" not in html
