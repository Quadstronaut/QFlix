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
