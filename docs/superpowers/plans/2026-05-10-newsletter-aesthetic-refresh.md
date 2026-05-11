# Newsletter Aesthetic Refresh — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply 8 stackable visual enhancements to the QFlix weekly newsletter template, plus stand up `~/www/images/` as a hardened self-hosted brand-asset path served by the existing user-nginx.

**Architecture:** Pure template + static-asset work. No data-fetch logic changes. Template (`weekly.html.j2`) is mutated in 8 focused passes, each with a render test. Image hosting is one nginx fragment + one idempotent deploy script that copies Q.png into place, hardens nginx (server_tokens off, allowlist, error mask), and smoke-tests the public URL.

**Tech Stack:** Jinja2 (already a dep), pytest (new dev dep), bash deploy script using existing `scripts/lib/ssh.sh`, nginx config served by user-nginx.

**Spec:** `docs/superpowers/specs/2026-05-10-newsletter-aesthetic-refresh-design.md`

**Refinement vs spec:** The spec said the Q.png URL would be hardcoded with the literal FQDN. The plan instead uses `{{ ctx.public_host }}` (already on `EmailContext`, sourced from `secrets/seedbox.host`) so an FQDN change doesn't require a template edit. Path stays fixed at `/images/Q.png`. Net effect identical; one less coupling.

---

## File Structure

**New files:**
- `scripts/qflix-newsletter/tests/__init__.py` — empty, marks tests as package
- `scripts/qflix-newsletter/tests/conftest.py` — pytest fixture providing a populated `EmailContext`
- `scripts/qflix-newsletter/tests/test_template_render.py` — render-output assertions for all 8 enhancements
- `scripts/qflix-newsletter/requirements-dev.txt` — pytest pin
- `scripts/data/qflix-images.conf` — nginx fragment with hardening directives
- `scripts/data/_blank.png` — 1×1 transparent PNG, used by nginx error_page
- `scripts/configure/60-www-images.sh` — idempotent deploy script

**Modified files:**
- `scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2` — all 8 enhancements
- `inventory.md` — add `~/www/images/` row to Section N

**Untouched (intentional):** `render.py`, `sources.py`, `ai.py`, `delivery.py`, `config.py`, `main.py`, `Q.png`. The refactor is template-only on the Python side.

---

## Task 1: Set up render test harness

**Files:**
- Create: `scripts/qflix-newsletter/requirements-dev.txt`
- Create: `scripts/qflix-newsletter/tests/__init__.py`
- Create: `scripts/qflix-newsletter/tests/conftest.py`
- Create: `scripts/qflix-newsletter/tests/test_template_render.py`

- [ ] **Step 1: Create dev requirements**

```
# scripts/qflix-newsletter/requirements-dev.txt
-r requirements.txt
pytest>=8.0
```

- [ ] **Step 2: Create empty tests package marker**

```python
# scripts/qflix-newsletter/tests/__init__.py
```
(empty file)

- [ ] **Step 3: Create conftest with sample EmailContext fixture**

```python
# scripts/qflix-newsletter/tests/conftest.py
"""Shared pytest fixtures for newsletter render tests."""
from __future__ import annotations

import datetime as _dt

import pytest

from qflix_newsletter.ai import AiPick
from qflix_newsletter.render import EmailContext, ShowGroup
from qflix_newsletter.sources import CalendarItem, RecentItem


def _movie(title: str, year: int, rating: float = 7.5) -> RecentItem:
    return RecentItem(
        media_type="movie",
        title=title,
        year=year,
        summary=f"Summary for {title}.",
        thumb_url=f"https://image.tmdb.org/t/p/w300/{title}.jpg",
        added_at=1700000000,
        rating=rating,
        library_name="Movies",
    )


def _episode(show: str, season: int, ep: int) -> RecentItem:
    return RecentItem(
        media_type="episode",
        title=f"Episode {ep}",
        year=2026,
        summary="",
        thumb_url=None,
        added_at=1700000000,
        rating=None,
        show_title=show,
        season=season,
        episode=ep,
        library_name="TV Shows",
    )


@pytest.fixture
def sample_ctx() -> EmailContext:
    pick = _movie("Suzume", 2022, rating=10.0)
    movies = [_movie("Inception", 2010, 9.0), _movie("Arrival", 2016, 8.5)]
    show_a = ShowGroup(
        show_title="Severance",
        episodes=[_episode("Severance", 2, 1), _episode("Severance", 2, 2)],
        thumb_url="https://image.tmdb.org/t/p/w300/sev.jpg",
    )
    coming = [
        CalendarItem(
            media_type="tv",
            title="Pilot",
            air_date=_dt.date(2026, 5, 20),
            show_title="New Show",
            season=1,
            episode=1,
        )
    ]
    ai_picks = [
        AiPick(
            if_you_liked="Spirited Away",
            try_this="The Tale of the Princess Kaguya",
            blurb="Same Ghibli emotional gut-punch, different visual register.",
        )
    ]
    return EmailContext(
        week_label="May 11, 2026",
        pick=pick,
        movies=movies,
        shows=[show_a],
        anime_movies=[],
        anime_shows=[],
        coming_soon=coming,
        ai_picks=ai_picks,
        nerd_corner={
            "total_items": 12345,
            "sections": [{"name": "Movies", "count": 1234}, {"name": "TV Shows", "count": 11111}],
        },
        subject="Test subject",
        public_host="quadstronaut.seedbox.example.com",
    )
```

- [ ] **Step 4: Create one passing baseline test**

```python
# scripts/qflix-newsletter/tests/test_template_render.py
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
```

- [ ] **Step 5: Install dev deps and run baseline test**

Run:
```
cd scripts/qflix-newsletter
python -m venv .venv-dev
.venv-dev/bin/pip install -r requirements-dev.txt   # Linux/macOS
# OR on Windows:
.venv-dev/Scripts/pip install -r requirements-dev.txt
.venv-dev/bin/python -m pytest tests/ -v             # Linux/macOS
# OR on Windows:
.venv-dev/Scripts/python -m pytest tests/ -v
```
Expected: `1 passed`.

- [ ] **Step 6: Commit**

```bash
git add scripts/qflix-newsletter/requirements-dev.txt \
        scripts/qflix-newsletter/tests/
git commit -m "newsletter tests: render-test harness (pytest + sample-context fixture)"
```

---

## Task 2: Header — title rename + Q.png + black→blue→bg gradient

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2:13-17`
- Modify: `scripts/qflix-newsletter/tests/test_template_render.py`

- [ ] **Step 1: Write failing test**

Append to `test_template_render.py`:

```python
def test_header_uses_qflix_white_title_with_qpng_and_gradient(sample_ctx):
    html = render_html(sample_ctx)
    # Title is the mixed-case "QFlix" in white, not the orange "QFLIX".
    assert ">QFlix<" in html
    assert "QFLIX" not in html  # old all-caps wordmark is gone
    # Q.png img tag points at our self-hosted asset (uses public_host).
    assert 'src="https://quadstronaut.seedbox.example.com/images/Q.png"' in html
    # Header gradient is the spec'd top-down black→blue→bg.
    assert "linear-gradient(180deg,#000 0%,#1e3a8a 50%,#0a1628 100%)" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/python -m pytest tests/test_template_render.py::test_header_uses_qflix_white_title_with_qpng_and_gradient -v`
Expected: FAIL — `assert '>QFlix<' in html` (current template still says `QFLIX`).

- [ ] **Step 3: Replace the header block**

In `weekly.html.j2`, replace lines 13-17:

```jinja
      {# Header — hero with Q.png on top-down black→blue→bg gradient #}
      <tr><td style="padding:0;">
        <div style="background:linear-gradient(180deg,#000 0%,#1e3a8a 50%,#0a1628 100%);padding:36px 16px 28px 16px;text-align:center;">
          <img src="https://{{ ctx.public_host }}/images/Q.png" alt="QFlix" width="96" height="96" style="display:inline-block;width:96px;height:96px;border-radius:50%;box-shadow:0 6px 28px rgba(255,255,255,.15),0 4px 24px rgba(30,64,175,.5);">
          <div style="font-size:38px;font-weight:600;color:#ffffff;letter-spacing:1px;margin-top:14px;">QFlix</div>
          <div style="width:48px;height:2px;background:#d4af37;margin:10px auto;line-height:0;font-size:0;">&nbsp;</div>
          <div style="font-size:13px;color:#7dd3fc;">{{ ctx.week_label }}</div>
        </div>
      </td></tr>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/python -m pytest tests/ -v`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2 \
        scripts/qflix-newsletter/tests/test_template_render.py
git commit -m "newsletter header: QFlix wordmark in white, Q.png hero, black→blue→bg gradient"
```

---

## Task 3: Filmstrip accent (top + bottom)

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2`
- Modify: `scripts/qflix-newsletter/tests/test_template_render.py`

- [ ] **Step 1: Write failing test**

Append to `test_template_render.py`:

```python
def test_filmstrip_accent_appears_twice(sample_ctx):
    html = render_html(sample_ctx)
    pattern = "repeating-linear-gradient(90deg,#0a0a0a 0,#0a0a0a 8px,#1a1a1a 8px,#1a1a1a 14px)"
    # Once below the header, once above the footer.
    assert html.count(pattern) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/python -m pytest tests/test_template_render.py::test_filmstrip_accent_appears_twice -v`
Expected: FAIL — pattern not found.

- [ ] **Step 3: Add filmstrip rows**

Define a Jinja macro at the top of `weekly.html.j2` (immediately after the `<title>` line):

```jinja
{% macro filmstrip() -%}
<tr><td style="padding:0;">
  <div style="height:18px;background:repeating-linear-gradient(90deg,#0a0a0a 0,#0a0a0a 8px,#1a1a1a 8px,#1a1a1a 14px);border-top:1px solid #2a2a2a;border-bottom:1px solid #2a2a2a;line-height:0;font-size:0;">&nbsp;</div>
</td></tr>
{%- endmacro %}
```

Then insert `{{ filmstrip() }}` in two places:
1. Immediately after the closing `</tr>` of the header (right after the block from Task 2)
2. Immediately before the footer block (look for the comment `{# Footer #}`)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/python -m pytest tests/ -v`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2 \
        scripts/qflix-newsletter/tests/test_template_render.py
git commit -m "newsletter filmstrip: repeating-gradient bar above footer + below header"
```

---

## Task 4: Pick of the Week gold corner ribbon

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2:19-44`
- Modify: `scripts/qflix-newsletter/tests/test_template_render.py`

- [ ] **Step 1: Write failing test**

Append:

```python
def test_pick_has_gold_diagonal_ribbon(sample_ctx):
    html = render_html(sample_ctx)
    # The label moves into the ribbon. Verify the ribbon CSS signature
    # appears on the Pick card and the old-style label row is gone.
    assert "transform:rotate(-30deg)" in html
    assert "linear-gradient(135deg,#e8c456,#b8941f)" in html
    # Old standalone label (uppercase letter-spacing block in its own <td>) is gone:
    # we just check that the new ribbon DIV appears within the Pick card markup.
    assert "Pick of the Week" in html  # text still present, but inside the ribbon
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/python -m pytest tests/test_template_render.py::test_pick_has_gold_diagonal_ribbon -v`
Expected: FAIL — ribbon CSS not present.

- [ ] **Step 3: Replace the Pick card block**

Locate the `{# Pick of the Week #}` block (lines 19-44 in the original template) and replace it with:

```jinja
      {# Pick of the Week — gold corner ribbon replaces standalone label row #}
      {% if ctx.pick %}
      <tr><td style="padding:20px 16px 24px 16px;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#0e1d33;border:1px solid rgba(212,175,55,.4);border-radius:8px;overflow:hidden;position:relative;">
          <tr><td style="padding:0;position:relative;">
            <div style="position:absolute;top:18px;left:-38px;background:linear-gradient(135deg,#e8c456,#b8941f);color:#0a1628;padding:4px 44px;transform:rotate(-30deg);font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;box-shadow:0 2px 8px rgba(0,0,0,.5);z-index:2;">Pick of the Week</div>
          </td></tr>
          {% if ctx.pick.thumb_url %}
          <tr><td align="center" style="padding:24px 16px 8px 16px;">
            <img src="{{ ctx.pick.thumb_url }}" alt="" width="220" style="display:block;max-width:100%;height:auto;border-radius:6px;box-shadow:0 8px 24px rgba(0,0,0,.7);">
          </td></tr>
          {% endif %}
          <tr><td style="padding:12px 16px 4px 16px;">
            <h2 style="margin:0;color:#f8fafc;font-size:22px;font-weight:600;">
              {{ ctx.pick.title }}{% if ctx.pick.year %} <span style="color:#7dd3fc;font-weight:400;">({{ ctx.pick.year }})</span>{% endif %}
            </h2>
            {% if ctx.pick.rating %}
            <div style="margin-top:6px;color:#d4af37;font-size:13px;">★ {{ '%.1f'|format(ctx.pick.rating) }}</div>
            {% endif %}
          </td></tr>
          <tr><td style="padding:8px 16px 16px 16px;color:#cbd5e1;font-size:14px;line-height:1.55;">
            {{ ctx.pick.summary | truncate(280) }}
          </td></tr>
        </table>
      </td></tr>
      {% endif %}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/python -m pytest tests/ -v`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2 \
        scripts/qflix-newsletter/tests/test_template_render.py
git commit -m "newsletter pick: gold diagonal corner ribbon (label moves into ribbon)"
```

---

## Task 5: Section dividers (◆ ornament)

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2`
- Modify: `scripts/qflix-newsletter/tests/test_template_render.py`

- [ ] **Step 1: Write failing test**

Append:

```python
def test_section_dividers_use_diamond_ornament(sample_ctx):
    html = render_html(sample_ctx)
    # New ornament: diamond char in gold between cyan hairlines.
    assert "&#9670;" in html or "◆" in html
    # Old solid border-bottom underline on h3 must be gone.
    assert "border-bottom:1px solid rgba(125,211,252,.18)" not in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/python -m pytest tests/test_template_render.py::test_section_dividers_use_diamond_ornament -v`
Expected: FAIL — old border-bottom still present.

- [ ] **Step 3: Add divider macro and apply to all section h3 elements**

Add a second macro alongside `filmstrip()` near the top of `weekly.html.j2`:

```jinja
{% macro section_divider() -%}
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:6px;">
  <tr>
    <td style="height:1px;background:#7dd3fc;opacity:.6;line-height:0;font-size:0;">&nbsp;</td>
    <td width="20" align="center" style="color:#d4af37;font-size:11px;line-height:1;padding:0 6px;">&#9670;</td>
    <td style="height:1px;background:#7dd3fc;opacity:.6;line-height:0;font-size:0;">&nbsp;</td>
  </tr>
</table>
{%- endmacro %}
```

Then for each `<h3>` in the template (5 total: New movies, New TV, New anime movies, New anime TV, Coming soon, plus the smaller `<h3>` for AI picks — that's 6 elements), do this transformation:

Before:
```jinja
<h3 style="margin:0 0 12px 0;color:#f8fafc;font-size:18px;font-weight:600;border-bottom:1px solid rgba(125,211,252,.18);padding-bottom:6px;">New movies</h3>
```

After:
```jinja
<h3 style="margin:0;color:#f8fafc;font-size:18px;font-weight:600;">New movies</h3>
{{ section_divider() }}
```

For the smaller AI section heading (currently uses font-size:14px and color:#cbd5e1), preserve the original font/color but apply the same border-removal + divider-append pattern.

The Nerd corner uses a `<div>` label inside its self-contained dark box — leave it untouched (no divider added).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/python -m pytest tests/ -v`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2 \
        scripts/qflix-newsletter/tests/test_template_render.py
git commit -m "newsletter dividers: ◆ ornament between cyan hairlines (replaces border-bottom)"
```

---

## Task 6: Emoji prefixes on section labels

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2`
- Modify: `scripts/qflix-newsletter/tests/test_template_render.py`

- [ ] **Step 1: Write failing test**

Append:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/python -m pytest tests/test_template_render.py::test_section_labels_have_emoji_prefixes -v`
Expected: FAIL — none of the emoji are present.

- [ ] **Step 3: Add emoji to each section label**

In `weekly.html.j2`, edit each section label inline:

| Find | Replace |
|---|---|
| `>New movies<` | `>🎬 New movies<` |
| `>New TV<` | `>📺 New TV<` |
| `>New anime movies<` | `>🌸 New anime movies<` |
| `>New anime TV<` | `>🍙 New anime TV<` |
| `>Coming soon<` | `>🗓 Coming soon<` |
| `>A few things you might like<` | `>✨ A few things you might like<` |
| `>Nerd corner<` | `>🤓 Nerd corner<` |

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/python -m pytest tests/ -v`
Expected: `6 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2 \
        scripts/qflix-newsletter/tests/test_template_render.py
git commit -m "newsletter labels: emoji prefixes per section (🎬 📺 🌸 🍙 🗓 ✨ 🤓)"
```

---

## Task 7: Count badges in section headers

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2`
- Modify: `scripts/qflix-newsletter/tests/test_template_render.py`

- [ ] **Step 1: Write failing test**

Append:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/python -m pytest tests/test_template_render.py::test_section_headers_have_count_badges -v`
Expected: FAIL — badge CSS not found.

- [ ] **Step 3: Add badge macro and apply to section headers**

Add macro near `filmstrip()` and `section_divider()`:

```jinja
{% macro count_badge(text) -%}
<span style="background:#1e40af;color:#fff;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:600;margin-left:6px;vertical-align:middle;">{{ text }}</span>
{%- endmacro %}
```

Update the section h3 elements (the ones with emoji from Task 6) to append a badge inline:

- **New movies:** `>🎬 New movies{{ count_badge(ctx.movies | length) }}<`
- **New TV:** `>📺 New TV{{ count_badge((ctx.shows | length | string) + ' shows · ' + (ctx.shows | sum(attribute='episode_count') | string) + ' eps') }}<`
- **New anime movies:** `>🌸 New anime movies{{ count_badge(ctx.anime_movies | length) }}<`
- **New anime TV:** `>🍙 New anime TV{{ count_badge((ctx.anime_shows | length | string) + ' shows · ' + (ctx.anime_shows | sum(attribute='episode_count') | string) + ' eps') }}<`
- **Coming soon:** `>🗓 Coming soon{{ count_badge(ctx.coming_soon[:12] | length) }}<`
- **A few things you might like:** no badge (always 3, not interesting)
- **Nerd corner:** no badge (the section IS the count)

Note: `>` and `<` here are inside the `<h3>...</h3>` element — the actual edit places the call between the existing label text and the closing `</h3>`. Example for the movies header:

```jinja
<h3 style="margin:0;color:#f8fafc;font-size:18px;font-weight:600;">🎬 New movies{{ count_badge(ctx.movies | length) }}</h3>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/python -m pytest tests/ -v`
Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2 \
        scripts/qflix-newsletter/tests/test_template_render.py
git commit -m "newsletter badges: blue count pill in each section header (movies/TV/anime/coming)"
```

---

## Task 8: Poster cards — gold border + drop shadow

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2`
- Modify: `scripts/qflix-newsletter/tests/test_template_render.py`

- [ ] **Step 1: Write failing test**

Append:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/python -m pytest tests/test_template_render.py::test_movie_poster_cards_have_gold_border_and_shadow -v`
Expected: FAIL — gold border not present on movie cards.

- [ ] **Step 3: Update poster card styles**

In `weekly.html.j2`, find both the `{# New Movies #}` and `{# Anime Movies #}` blocks. In each one, the inner per-card `<div>` currently looks like:

```html
<div style="background:#0e1d33;border:1px solid rgba(125,211,252,.12);border-radius:6px;padding:8px;text-align:center;">
```

Replace with:

```html
<div style="background:#0e1d33;border:1px solid rgba(212,175,55,.4);border-radius:6px;padding:8px;text-align:center;box-shadow:0 6px 16px rgba(0,0,0,.6);">
```

Apply this change in TWO places (movies + anime movies). The TV-row cards use a different inner markup (`<table>` with an image cell + text cell) — leave those alone; they don't have a poster to lift.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/python -m pytest tests/ -v`
Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2 \
        scripts/qflix-newsletter/tests/test_template_render.py
git commit -m "newsletter posters: gold border + drop shadow on movie/anime-movie cards"
```

---

## Task 9: Footer — gradient + 40px Q.png + corrected tagline

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2:212-219`
- Modify: `scripts/qflix-newsletter/tests/test_template_render.py`

- [ ] **Step 1: Write failing test**

Append:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/python -m pytest tests/test_template_render.py::test_footer_uses_corrected_tagline_and_bg_to_black_gradient -v`
Expected: FAIL — old footer copy still present.

- [ ] **Step 3: Replace the footer block**

Replace the `{# Footer #}` block (currently lines 212-219) with:

```jinja
      {# Footer — bg→black gradient, 40px Q monogram, operator-corrected tagline #}
      <tr><td style="padding:0;">
        <div style="background:linear-gradient(180deg,#0a1628 0%,#000 100%);padding:28px 16px 36px 16px;text-align:center;">
          <img src="https://{{ ctx.public_host }}/images/Q.png" alt="" width="40" height="40" style="display:inline-block;width:40px;height:40px;border-radius:50%;box-shadow:0 2px 12px rgba(30,64,175,.4);">
          <div style="color:#cbd5e1;font-size:12px;margin-top:10px;letter-spacing:.3px;">QFlix · Crafted with precision 🩷 Quadstronaut</div>
          <div style="color:#7dd3fc;font-size:11px;opacity:.5;margin-top:10px;">
            <a href="{{ '{{ UnsubscribeURL }}' }}" style="color:#7dd3fc;text-decoration:underline;">Unsubscribe</a>
            ·
            <a href="{{ '{{ MessageURL }}' }}" style="color:#7dd3fc;text-decoration:underline;">View in browser</a>
          </div>
        </div>
      </td></tr>
```

Note: the filmstrip macro call from Task 3 should already sit immediately above this block. If you put it below by mistake, move it up.

- [ ] **Step 4: Run all tests to verify everything still passes**

Run: `.venv-dev/bin/python -m pytest tests/ -v`
Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2 \
        scripts/qflix-newsletter/tests/test_template_render.py
git commit -m "newsletter footer: bg→black gradient, 40px Q monogram, 'Crafted with precision 🩷' tagline"
```

---

## Task 10: Generate the 1×1 transparent PNG asset

**Files:**
- Create: `scripts/data/_blank.png`

- [ ] **Step 1: Generate the PNG using Python (no external deps)**

Run from the repo root:

```bash
python -c "
import base64, pathlib
# 1x1 transparent PNG, base64-encoded.
data = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=')
pathlib.Path('scripts/data/_blank.png').write_bytes(data)
print(f'wrote scripts/data/_blank.png ({len(data)} bytes)')
"
```

Expected output: `wrote scripts/data/_blank.png (67 bytes)`.

- [ ] **Step 2: Verify it's a valid PNG**

Run:
```bash
python -c "
import struct, pathlib
b = pathlib.Path('scripts/data/_blank.png').read_bytes()
assert b[:8] == b'\\x89PNG\\r\\n\\x1a\\n', 'not a PNG'
assert b[12:16] == b'IHDR', 'no IHDR chunk'
w, h = struct.unpack('>II', b[16:24])
print(f'PNG OK: {w}x{h}')
"
```

Expected output: `PNG OK: 1x1`.

- [ ] **Step 3: Commit**

```bash
git add scripts/data/_blank.png
git commit -m "data: 1×1 transparent PNG for nginx error_page mask"
```

---

## Task 11: Nginx fragment for `/images/`

**Files:**
- Create: `scripts/data/qflix-images.conf`

- [ ] **Step 1: Write the nginx fragment**

```nginx
# scripts/data/qflix-images.conf
# QFlix self-hosted brand assets · public, no htpasswd, hardened.
#
# Listmonk's media uploader is restricted (size caps, no clean public URLs),
# so we host newsletter brand assets (Q.png, future images) here and reference
# them by absolute URL in the template.
#
# Deploy via scripts/configure/60-www-images.sh (idempotent).

location ^~ /images/ {
    auth_basic off;
    autoindex off;

    alias /home/quadstronaut/www/images/;

    # Allowlist image extensions only. Anything else falls through to the
    # outer `return 404` below.
    location ~ ^/images/.+\.(png|jpg|jpeg|webp|gif|ico)$ {
        if ($request_method !~ ^(GET|HEAD)$) { return 405; }

        add_header Cache-Control "public, max-age=2592000, immutable" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
    }

    # Anything that doesn't match the allowlist (e.g. /images/, /images/foo.txt,
    # /images/.htaccess, /images/../etc/passwd): 404 via the masked error page.
    return 404;
}

# Serve the 1×1 transparent PNG instead of branded error pages — no info leak,
# no useful signal to a probe. Applies user-nginx-wide.
error_page 403 404 = /images/_blank.png;
```

- [ ] **Step 2: Sanity-check syntax (optional, requires nginx locally)**

Skip on Windows. On Linux/macOS with nginx installed:
```bash
nginx -t -c <(echo "events{} http{ server{ $(cat scripts/data/qflix-images.conf) } }")
```
Expected: `syntax is ok`. (Skip if nginx isn't installed locally — the deploy script does a real `app-nginx restart` that will catch syntax errors.)

- [ ] **Step 3: Commit**

```bash
git add scripts/data/qflix-images.conf
git commit -m "nginx: ~/www/images/ fragment with allowlist + cache + error mask"
```

---

## Task 12: Idempotent deploy script for `~/www/images/`

**Files:**
- Create: `scripts/configure/60-www-images.sh`

- [ ] **Step 1: Write the deploy script**

```bash
#!/usr/bin/env bash
# Phase 25 — ~/www/images/ self-hosted brand-asset path. Idempotent.
#
# Stands up:
#   ~/www/images/             (mode 0755)
#   ~/www/images/Q.png        (mode 0644, copied from repo root)
#   ~/www/images/_blank.png   (mode 0644, error_page mask)
#   ~/.apps/nginx/proxy.d/qflix-images.conf
#
# Also patches ~/.apps/nginx/nginx.conf to add `server_tokens off;` inside the
# existing http {} block, suppressing the nginx version in headers + default
# error pages. This affects the entire user-nginx (right blast radius — no
# app currently relies on the version header).
#
# Smoke-tests the public URL after restart.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# ── Step 1: copy assets to seedbox ──────────────────────────────────────────
log "deploying brand assets to ~/www/images/"
sshm 'mkdir -p ~/www/images && chmod 755 ~/www/images'
scpm_to "$REPO_ROOT/Q.png"               "~/www/images/Q.png"
scpm_to "$REPO_ROOT/scripts/data/_blank.png" "~/www/images/_blank.png"
sshm 'chmod 644 ~/www/images/*.png'

# ── Step 2: deploy nginx fragment ───────────────────────────────────────────
log "deploying nginx fragment"
scpm_to "$REPO_ROOT/scripts/data/qflix-images.conf" "~/.apps/nginx/proxy.d/qflix-images.conf"

# ── Step 3: ensure server_tokens off in nginx.conf (idempotent) ─────────────
log "patching nginx.conf for server_tokens off"
sshm 'bash -s' <<'REMOTE'
set -euo pipefail
CFG=$HOME/.apps/nginx/nginx.conf
if grep -qE '^\s*server_tokens\s+off;' "$CFG"; then
  echo "server_tokens off already present — nothing to do"
else
  cp "$CFG" "$CFG.bak.$(date +%s)"
  # Insert at the top of the first http { block.
  awk '
    /^[[:space:]]*http[[:space:]]*\{/ && !done {
      print
      print "    server_tokens off;"
      done = 1
      next
    }
    { print }
  ' "$CFG" > "$CFG.new"
  mv "$CFG.new" "$CFG"
  echo "added server_tokens off to nginx.conf"
fi
REMOTE

# ── Step 4: restart user-nginx ──────────────────────────────────────────────
log "restarting user-nginx"
sshm 'app-nginx restart'
sleep 5

# ── Step 5: smoke tests ─────────────────────────────────────────────────────
log "smoke tests"
PUB_HOST=$(cat "$REPO_ROOT/secrets/seedbox.host" 2>/dev/null || echo "quadstronaut.seedbox.example.com")

# Positive: Q.png returns 200.
HTTP=$(curl -s -o /dev/null -w '%{http_code}' "https://$PUB_HOST/images/Q.png")
if [ "$HTTP" != "200" ]; then
  echo "FAIL: Q.png expected 200, got $HTTP" >&2
  exit 1
fi
echo "  PASS: GET /images/Q.png → 200"

# Cache-Control header present.
if ! curl -sI "https://$PUB_HOST/images/Q.png" | grep -qi 'cache-control:.*immutable'; then
  echo "FAIL: Cache-Control immutable header not present on Q.png" >&2
  exit 1
fi
echo "  PASS: Cache-Control immutable present"

# Negative #1: directory listing returns 404.
HTTP=$(curl -s -o /dev/null -w '%{http_code}' "https://$PUB_HOST/images/")
if [ "$HTTP" != "404" ]; then
  echo "FAIL: /images/ expected 404, got $HTTP" >&2
  exit 1
fi
echo "  PASS: GET /images/ → 404"

# Negative #2: non-image extension returns 404.
HTTP=$(curl -s -o /dev/null -w '%{http_code}' "https://$PUB_HOST/images/foo.txt")
if [ "$HTTP" != "404" ]; then
  echo "FAIL: /images/foo.txt expected 404, got $HTTP" >&2
  exit 1
fi
echo "  PASS: GET /images/foo.txt → 404"

# Hardening: nginx version not exposed in Server header.
if curl -sI "https://$PUB_HOST/images/Q.png" | grep -iE '^server:.*nginx/[0-9]'; then
  echo "FAIL: nginx version visible in Server header" >&2
  exit 1
fi
echo "  PASS: nginx version not exposed (server_tokens off)"

log "deploy + smoke complete"
```

- [ ] **Step 2: Make script executable and verify shellcheck (if available)**

```bash
chmod +x scripts/configure/60-www-images.sh
shellcheck scripts/configure/60-www-images.sh || true   # optional
```

Expected: chmod succeeds; shellcheck either passes or is not installed (don't block on it).

- [ ] **Step 3: Commit (without running yet — Task 14 is the live run)**

```bash
git add scripts/configure/60-www-images.sh
git commit -m "configure: 60-www-images.sh — idempotent ~/www/images/ + nginx hardening + smoke"
```

---

## Task 13: Update inventory.md

**Files:**
- Modify: `inventory.md` — Section N table

- [ ] **Step 1: Locate Section N**

Find the line containing `## N. Documentation served by user-nginx` (around line 139).

- [ ] **Step 2: Add a new row to the table**

Add this row immediately after the existing `~/www/qflix-faq/index.html` row:

```
| `~/www/images/Q.png` (4 KB) | static png | served by user-nginx | Brand asset for newsletter + future surfaces; Listmonk media uploader is restricted, so we self-host | NO (regenerate from repo `Q.png` via `scripts/configure/60-www-images.sh`) | Public | `https://quadstronaut.seedbox.example.com/images/Q.png` | n/a (static) | n/a | n/a (covered transitively by canary-mobile-ux: if nginx down, that canary goes red) | Deployed 2026-05-10. Nginx fragment `~/.apps/nginx/proxy.d/qflix-images.conf` allowlists image extensions only; `server_tokens off` set globally; `error_page 403 404 = /images/_blank.png` masks errors. GitHub raw `https://raw.githubusercontent.com/Quadstronaut/QFlix/master/Q.png` is the documented fallback. |
```

- [ ] **Step 3: Commit**

```bash
git add inventory.md
git commit -m "inventory: add ~/www/images/Q.png (Section N) — self-hosted brand assets"
```

---

## Task 14: End-to-end live deploy + visual verification

**Files:** none modified — this is validation.

- [ ] **Step 1: Run the full pytest suite one more time**

```bash
cd scripts/qflix-newsletter
.venv-dev/bin/python -m pytest tests/ -v   # or .venv-dev/Scripts/python on Windows
```
Expected: all 9 tests pass.

- [ ] **Step 2: Run the deploy script against the live seedbox**

From repo root:
```bash
bash scripts/configure/60-www-images.sh
```
Expected output: each smoke step prints `PASS:` and the script exits 0. If any smoke fails, the script exits non-zero — investigate and fix before proceeding.

- [ ] **Step 3: Render the newsletter against live data, dry-run**

SSH to the seedbox and run:
```bash
ssh quadstronaut@seedbox.example.com \
  "cd ~/.apps/qflix-newsletter && .venv/bin/python -m qflix_newsletter --dry-run --out-html /tmp/qflix-test.html"
scp quadstronaut@seedbox.example.com:/tmp/qflix-test.html /tmp/qflix-test.html
```

Note: this requires the **next** newsletter deploy (Task 12 only ships the image infra; the template change ships when `49-qflix-newsletter-install.sh` is re-run, which the operator does on their normal cadence). For an immediate test before that re-run, render locally:

```bash
cd scripts/qflix-newsletter
.venv-dev/bin/python -c "
from qflix_newsletter.render import render_html
import importlib, tests.conftest as c
ctx_factory = c.sample_ctx.__wrapped__  # unwrap pytest fixture
ctx = ctx_factory()
open('/tmp/qflix-test.html', 'w', encoding='utf-8').write(render_html(ctx))
print('wrote /tmp/qflix-test.html')
"
```

- [ ] **Step 4: Open the rendered HTML in a browser**

Open `/tmp/qflix-test.html` in your default browser. Visually confirm:

1. Header has Q.png logo on a black-top→blue→bg gradient
2. Title says "QFlix" (mixed case) in white with a gold underline
3. Filmstrip bar appears immediately below header
4. Pick of the Week card has a gold diagonal corner ribbon
5. Section titles have emoji prefixes and ◆ ornament dividers below
6. Section titles show count badges (e.g. "🎬 New movies [12]")
7. Movie cards have gold border + drop shadow
8. AI section ("✨ A few things you might like") renders the recommendation
9. Filmstrip bar appears immediately above footer
10. Footer has 40px Q.png + "QFlix · Crafted with precision 🩷 Quadstronaut" + bg→black gradient

If any item fails visually, capture a screenshot and re-open the relevant Task above to fix.

- [ ] **Step 5: Re-deploy newsletter package (only when operator is ready to ship)**

```bash
bash scripts/configure/49-qflix-newsletter-install.sh
```

This pushes the updated template to `~/.apps/qflix-newsletter/`. The next scheduled Mon 08:00 timer fire will use the new template against live Tautulli/arr/Gemini data.

- [ ] **Step 6: After first live send, verify email-client rendering**

Wait for the next Monday digest. In each of these clients, open the email and run through the same 10-item visual checklist:

- Gmail web
- Gmail iOS / Android
- Apple Mail (macOS / iOS)
- Outlook web
- ProtonMail web

If any client breaks a specific enhancement (e.g. Outlook strips `box-shadow`), document the degradation in a follow-up issue but don't block — the feature degrades gracefully (no shadow, but card still renders).

---

## Spec coverage check (planning self-review)

| Spec section | Covered by |
|---|---|
| Title rename QFLIX→QFlix white | Task 2 |
| Header gradient (black→blue→bg) | Task 2 |
| Filmstrip top + bottom | Task 3 |
| Pick gold ribbon (label moves into ribbon) | Task 4 |
| ◆ section dividers | Task 5 |
| Emoji prefixes per section | Task 6 |
| Count badges | Task 7 |
| Poster gold border + drop shadow | Task 8 |
| Footer gradient + 40px Q + corrected tagline | Task 9 |
| `~/www/images/` filesystem layout | Task 12 |
| `_blank.png` 1×1 PNG | Task 10 |
| Nginx fragment with allowlist + cache + masked errors | Task 11 |
| `server_tokens off` patch | Task 12, step 3 |
| Smoke tests (positive + negative + version-leak) | Task 12, step 5 |
| Inventory.md row in Section N | Task 13 |
| GitHub raw documented fallback | Task 13 (in Notes column) + spec doc itself |
| End-to-end visual verification | Task 14 |

**No spec gaps.** All 8 enhancements + the image-hosting infra + the operator's `~/www/images/` + Listmonk-restriction-workaround are covered.
