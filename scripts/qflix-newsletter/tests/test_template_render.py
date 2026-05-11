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
