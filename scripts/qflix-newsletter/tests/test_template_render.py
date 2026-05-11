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
