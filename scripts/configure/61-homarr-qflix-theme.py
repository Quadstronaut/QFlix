#!/usr/bin/env python3
"""Apply the 'Qflix' theme to a Homarr board (default: public).

Sets:
- page_title / meta_title  → "Qflix"
- primary_color            → warm orange  (#ff8c42)
- secondary_color          → rich gold    (#d4af37)
- item_radius              → "lg" (already default; keep)
- icon_color               → light-blue   (#7dd3fc)
- logo_image_url           → Q.png (repo banner, served from GitHub raw)
- favicon_image_url        → Q.png (same — square 512×512 RGBA)
- background_image_url     → SVG gradient (midnight-blue → near-black)
- custom_css               → full Qflix theme (see _custom_css below)

Idempotent. Re-running just overwrites. Per-board via --board <name>
(default: public).

Run on the seedbox:  python3 61-homarr-qflix-theme.py
Or pipe via SSH:    sshm "python3 -" < scripts/configure/61-homarr-qflix-theme.py
"""
from __future__ import annotations

import argparse
import base64
import os
import sqlite3
import sys


# ---- Brand colors ---------------------------------------------------------
MIDNIGHT_DEEP = "#05101f"       # darkest — page edge
MIDNIGHT      = "#0a1628"       # primary background
MIDNIGHT_SOFT = "#0e1d33"       # card / tile background
NAVY_LINE     = "#1a2b4a"       # subtle border / divider
ORANGE_WARM   = "#ff8c42"       # primary accent (warm orange)
ORANGE_DEEP   = "#cc6a2a"       # primary accent dark variant
GOLD          = "#d4af37"       # secondary accent (rich gold)
GOLD_BRIGHT   = "#f5c842"       # gold highlight
SKY_BLUE      = "#7dd3fc"       # icon mark
INK           = "#f8fafc"       # body text on dark
INK_MUTED     = "#cbd5e1"       # secondary text


# ---- Brand assets ---------------------------------------------------------

# Logo + favicon: Q.png (repo banner, 512×512 RGBA) served from GitHub raw.
# Pinned to the main branch so the dashboard tracks the canonical brand mark
# automatically when the repo is updated.
Q_PNG_URL = "https://raw.githubusercontent.com/Quadstronaut/QFlix/main/Q.png"

# Background stays inline SVG — radial gradient, tiny payload, no need
# to round-trip GitHub on every page load.
BACKGROUND_SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" preserveAspectRatio="xMidYMid slice" width="1920" height="1080">
  <defs>
    <radialGradient id="vignette" cx="50%" cy="35%" r="80%">
      <stop offset="0%"  stop-color="{MIDNIGHT}"/>
      <stop offset="55%" stop-color="{MIDNIGHT_DEEP}"/>
      <stop offset="100%" stop-color="#02060d"/>
    </radialGradient>
    <radialGradient id="glow" cx="50%" cy="20%" r="40%">
      <stop offset="0%"  stop-color="{ORANGE_DEEP}" stop-opacity="0.12"/>
      <stop offset="100%" stop-color="{ORANGE_DEEP}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="1920" height="1080" fill="url(#vignette)"/>
  <rect width="1920" height="1080" fill="url(#glow)"/>
</svg>"""


def _data_uri_svg(svg: str) -> str:
    b = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b}"


# ---- Custom CSS -----------------------------------------------------------

CUSTOM_CSS = f"""/* ============================================================
   Qflix — Quadstronaut Flix
   Dark midnight blue · warm orange · golden accents
   ============================================================ */

:root {{
  /* Palette */
  --qf-midnight-deep: {MIDNIGHT_DEEP};
  --qf-midnight:      {MIDNIGHT};
  --qf-midnight-soft: {MIDNIGHT_SOFT};
  --qf-navy-line:     {NAVY_LINE};
  --qf-orange:        {ORANGE_WARM};
  --qf-orange-deep:   {ORANGE_DEEP};
  --qf-gold:          {GOLD};
  --qf-gold-bright:   {GOLD_BRIGHT};
  --qf-sky:           {SKY_BLUE};
  --qf-ink:           {INK};
  --qf-ink-muted:     {INK_MUTED};

  /* Override Mantine theme variables */
  --mantine-primary-color-filled:        var(--qf-orange);
  --mantine-primary-color-filled-hover:  var(--qf-orange-deep);
  --mantine-primary-color-light:         rgba(255, 140, 66, 0.10);
  --mantine-primary-color-light-hover:   rgba(255, 140, 66, 0.18);
  --mantine-color-bright:                var(--qf-ink);
  --mantine-color-anchor:                var(--qf-gold-bright);
  --mantine-color-text:                  var(--qf-ink);
  --mantine-color-body:                  transparent;
  --mantine-color-default-border:        var(--qf-navy-line);
  --mantine-color-dark-9:                var(--qf-midnight-deep);
  --mantine-color-dark-8:                var(--qf-midnight);
  --mantine-color-dark-7:                var(--qf-midnight-soft);
  --mantine-color-dark-6:                var(--qf-navy-line);
}}

/* ---- Page background ------------------------------------ */
html, body, .mantine-AppShell-root, .mantine-AppShell-main {{
  background-color: var(--qf-midnight-deep) !important;
  color: var(--qf-ink);
}}

body::before {{
  content: "";
  position: fixed; inset: 0; pointer-events: none; z-index: -1;
  background:
    radial-gradient(ellipse 80% 60% at 50% -10%,
      rgba(255, 140, 66, 0.08), transparent 70%),
    radial-gradient(ellipse 60% 80% at 100% 100%,
      rgba(212, 175, 55, 0.05), transparent 70%);
}}

/* ---- Top header / nav bar ------------------------------- */
header.mantine-AppShell-header,
.mantine-AppShell-header {{
  background: linear-gradient(180deg,
    rgba(10, 22, 40, 0.92), rgba(5, 16, 31, 0.85)) !important;
  border-bottom: 1px solid var(--qf-navy-line) !important;
  backdrop-filter: blur(12px) saturate(140%);
  -webkit-backdrop-filter: blur(12px) saturate(140%);
  box-shadow: 0 1px 0 rgba(212, 175, 55, 0.08),
              0 8px 24px rgba(0, 0, 0, 0.4);
}}

/* ---- Sidebar / side nav --------------------------------- */
nav.mantine-AppShell-navbar, aside.mantine-AppShell-aside {{
  background: linear-gradient(180deg,
    var(--qf-midnight) 0%, var(--qf-midnight-deep) 100%) !important;
  border-right: 1px solid var(--qf-navy-line) !important;
}}

/* ---- Cards / Tiles -------------------------------------- */
.mantine-Paper-root,
[data-with-border="true"] {{
  background: linear-gradient(180deg,
    rgba(14, 29, 51, 0.95) 0%, rgba(10, 22, 40, 0.95) 100%) !important;
  border: 1px solid var(--qf-navy-line) !important;
  box-shadow:
    0 1px 0 rgba(255, 255, 255, 0.04) inset,
    0 8px 24px rgba(0, 0, 0, 0.35);
  transition: transform 220ms ease, box-shadow 220ms ease,
              border-color 220ms ease;
}}

.mantine-Paper-root:hover {{
  transform: translateY(-2px);
  border-color: var(--qf-gold) !important;
  box-shadow:
    0 1px 0 rgba(245, 200, 66, 0.18) inset,
    0 12px 32px rgba(0, 0, 0, 0.55),
    0 0 0 1px rgba(212, 175, 55, 0.25);
}}

/* ---- Buttons -------------------------------------------- */
.mantine-Button-root[data-variant="filled"] {{
  background: linear-gradient(135deg,
    var(--qf-orange) 0%, var(--qf-orange-deep) 100%) !important;
  border: 0;
  color: #fff;
  box-shadow: 0 4px 12px rgba(255, 140, 66, 0.35);
  transition: transform 180ms ease, box-shadow 180ms ease;
}}
.mantine-Button-root[data-variant="filled"]:hover {{
  transform: translateY(-1px);
  box-shadow: 0 6px 18px rgba(255, 140, 66, 0.5);
}}
.mantine-Button-root[data-variant="default"],
.mantine-Button-root[data-variant="subtle"] {{
  background: rgba(14, 29, 51, 0.7) !important;
  border: 1px solid var(--qf-navy-line) !important;
  color: var(--qf-ink) !important;
}}
.mantine-Button-root[data-variant="default"]:hover {{
  border-color: var(--qf-gold) !important;
  color: var(--qf-gold-bright) !important;
}}

/* ---- Search bar ----------------------------------------- */
.mantine-TextInput-input,
.mantine-Input-input {{
  background: rgba(5, 16, 31, 0.6) !important;
  border: 1px solid var(--qf-navy-line) !important;
  color: var(--qf-ink) !important;
  transition: border-color 180ms ease, box-shadow 180ms ease;
}}
.mantine-TextInput-input:focus,
.mantine-Input-input:focus {{
  border-color: var(--qf-orange) !important;
  box-shadow: 0 0 0 3px rgba(255, 140, 66, 0.18) !important;
}}

/* ---- Anchors / links ------------------------------------ */
a, a:visited {{
  color: var(--qf-gold-bright);
  text-decoration: none;
  transition: color 160ms ease;
}}
a:hover {{ color: var(--qf-orange); }}

/* ---- Headings ------------------------------------------- */
.mantine-Title-root, h1, h2, h3 {{
  color: var(--qf-ink) !important;
  letter-spacing: -0.01em;
}}
.mantine-Title-root::first-letter {{
  /* Subtle drop accent on titles */
}}

/* ---- Section headers (board section labels) — scoped to main */
main.mantine-AppShell-main [class*="sectionTitle"],
main.mantine-AppShell-main .mantine-Stack-root > .mantine-Group-root > .mantine-Text-root {{
  color: var(--qf-gold-bright);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 600;
  font-size: 0.78rem;
}}

/* ---- Action icons (cog/menu/etc) ------------------------ */
.mantine-ActionIcon-root {{
  color: var(--qf-ink-muted);
  transition: color 160ms ease, background 160ms ease;
}}
.mantine-ActionIcon-root:hover {{
  color: var(--qf-gold-bright);
  background: rgba(212, 175, 55, 0.08);
}}

/* ---- Avatars / icons inside tiles ---------------------- */
.mantine-Avatar-root {{
  background: var(--qf-midnight) !important;
  border: 1px solid rgba(125, 211, 252, 0.18);
}}

/* ---- Loader ring --------------------------------------- */
.mantine-Loader-root {{
  --mantine-loader-color: var(--qf-orange) !important;
}}

/* ---- Scrollbars (Webkit) -------------------------------- */
::-webkit-scrollbar              {{ width: 12px; height: 12px; }}
::-webkit-scrollbar-track        {{ background: var(--qf-midnight-deep); }}
::-webkit-scrollbar-thumb        {{
  background: linear-gradient(180deg, var(--qf-navy-line), var(--qf-midnight));
  border-radius: 6px;
  border: 2px solid var(--qf-midnight-deep);
}}
::-webkit-scrollbar-thumb:hover  {{ background: var(--qf-gold); }}

/* ---- Selection ----------------------------------------- */
::selection {{
  background: var(--qf-orange);
  color: #fff;
}}

/* ============================================================
   Header logo — Q.png is 512×512 square; Homarr hard-codes
   width=32 height=32 on <img class="logo">. Render at 44×44
   with a subtle golden glow so it reads as a brand mark
   rather than a tile icon. The "Qflix" page-title text shows
   to the right of the logo (no wordmark in Q.png itself).
   ============================================================ */

img.logo,
header img.logo,
header.mantine-AppShell-header img.logo {{
  width: 44px !important;
  height: 44px !important;
  object-fit: contain;
  border-radius: 8px;
  filter: drop-shadow(0 1px 4px rgba(212, 175, 55, 0.35));
}}

/* ============================================================
   Tile content — base styles (scoped to <main> so the navbar
   is untouched). Text bumps inside @media blocks below.
   ============================================================ */

/* App tile titles (the visible app name under each icon) — scoped */
main.mantine-AppShell-main .mantine-Text-root {{
  font-weight: 500;
  letter-spacing: 0.01em;
}}
main.mantine-AppShell-main .mantine-Title-root {{
  font-weight: 700;
}}

/* App icon img — keep natural colors */
.mantine-Paper-root img,
.mantine-Avatar-root img {{
  filter: drop-shadow(0 2px 6px rgba(0, 0, 0, 0.45));
}}

/* ============================================================
   Responsive — make PC tiles compact, mobile tiles roomy
   ============================================================ */

/* PC (>=1200px). All zoom + text rules scoped to main.mantine-AppShell-main
   so the navbar (header + search) is unaffected. Visible text target ≈
   1.25rem; font-size compensates for zoom factor at each breakpoint. */
@media (min-width: 1200px) {{
  main.mantine-AppShell-main {{
    max-width: 1500px;
    margin-left: auto;
    margin-right: auto;
    padding-left: 32px !important;
    padding-right: 32px !important;
    zoom: 0.78;
  }}
  main.mantine-AppShell-main .mantine-Paper-root {{
    padding: 10px !important;
  }}
  /* Tile labels: 1.25 / 0.78 ≈ 1.6rem to look ~1.25× normal post-zoom */
  main.mantine-AppShell-main .mantine-Paper-root .mantine-Text-root,
  main.mantine-AppShell-main .mantine-Paper-root p,
  main.mantine-AppShell-main .mantine-Paper-root span:not([class*="Icon"]):not([class*="icon"]) {{
    font-size: 1.6rem !important;
    font-weight: 600 !important;
    line-height: 1.3 !important;
  }}
  /* Section labels (CINEMA, AUDIOBOOKS & BOOKS, REQUESTS MANAGEMENT,
     MEDIA SERVER) — back to ~1.0rem visible. 1.0 / 0.78 ≈ 1.28rem */
  main.mantine-AppShell-main .mantine-Title-root,
  main.mantine-AppShell-main h1,
  main.mantine-AppShell-main h2,
  main.mantine-AppShell-main h3,
  main.mantine-AppShell-main [class*="sectionTitle"],
  main.mantine-AppShell-main .mantine-Stack-root > .mantine-Group-root > .mantine-Text-root,
  main.mantine-AppShell-main .mantine-Group-root > .mantine-Text-root {{
    font-size: 1.28rem !important;
  }}
}}

@media (min-width: 1600px) {{
  main.mantine-AppShell-main {{
    max-width: 1700px;
    zoom: 0.72;
  }}
  /* Tile labels: 1.25 / 0.72 ≈ 1.74rem */
  main.mantine-AppShell-main .mantine-Paper-root .mantine-Text-root,
  main.mantine-AppShell-main .mantine-Paper-root p,
  main.mantine-AppShell-main .mantine-Paper-root span:not([class*="Icon"]):not([class*="icon"]) {{
    font-size: 1.74rem !important;
  }}
  /* Sections: 1.0 / 0.72 ≈ 1.39rem */
  main.mantine-AppShell-main .mantine-Title-root,
  main.mantine-AppShell-main h1,
  main.mantine-AppShell-main h2,
  main.mantine-AppShell-main h3,
  main.mantine-AppShell-main [class*="sectionTitle"],
  main.mantine-AppShell-main .mantine-Stack-root > .mantine-Group-root > .mantine-Text-root,
  main.mantine-AppShell-main .mantine-Group-root > .mantine-Text-root {{
    font-size: 1.39rem !important;
  }}
}}

@media (min-width: 1920px) {{
  main.mantine-AppShell-main {{
    zoom: 0.68;
  }}
  /* Tile labels: 1.25 / 0.68 ≈ 1.84rem */
  main.mantine-AppShell-main .mantine-Paper-root .mantine-Text-root,
  main.mantine-AppShell-main .mantine-Paper-root p,
  main.mantine-AppShell-main .mantine-Paper-root span:not([class*="Icon"]):not([class*="icon"]) {{
    font-size: 1.84rem !important;
  }}
  /* Sections: 1.0 / 0.68 ≈ 1.47rem */
  main.mantine-AppShell-main .mantine-Title-root,
  main.mantine-AppShell-main h1,
  main.mantine-AppShell-main h2,
  main.mantine-AppShell-main h3,
  main.mantine-AppShell-main [class*="sectionTitle"],
  main.mantine-AppShell-main .mantine-Stack-root > .mantine-Group-root > .mantine-Text-root,
  main.mantine-AppShell-main .mantine-Group-root > .mantine-Text-root {{
    font-size: 1.47rem !important;
  }}
}}

/* Tablet (600-1199px) keeps near-default sizing */
@media (min-width: 600px) and (max-width: 1199px) {{
  .mantine-Paper-root .mantine-Text-root {{
    font-size: 1.05rem;
  }}
}}

/* Mobile (<600px): roomier tiles, MUCH bigger text, no hover-lift.
   Triple-selector + !important to win against Mantine inline styles. */
@media (max-width: 600px) {{
  .mantine-AppShell-main {{
    padding-left: 12px !important;
    padding-right: 12px !important;
    font-size: 1.15rem !important;
  }}
  /* App tile labels — every text node inside a tile */
  .mantine-Paper-root .mantine-Text-root,
  .mantine-Paper-root p,
  .mantine-Paper-root span {{
    font-size: 1.35rem !important;
    font-weight: 600 !important;
    line-height: 1.3 !important;
  }}
  /* Section headers */
  .mantine-Title-root, h1, h2, h3 {{
    font-size: 1.5rem !important;
  }}
  /* Search bar input */
  .mantine-TextInput-input,
  .mantine-Input-input {{
    font-size: 1.15rem !important;
  }}
  .mantine-Paper-root {{
    padding: 14px !important;
  }}
  .mantine-Paper-root:hover {{
    transform: none;  /* no hover-lift on touch */
  }}
}}

/* ============================================================
   Tiny easter eggs
   ============================================================ */

/* Logo-area golden line under header */
.mantine-AppShell-header::after {{
  content: "";
  position: absolute;
  left: 0; right: 0; bottom: -1px; height: 1px;
  background: linear-gradient(90deg,
    transparent, var(--qf-gold) 40%, var(--qf-gold-bright) 50%,
    var(--qf-gold) 60%, transparent);
  opacity: 0.5;
}}
"""


def apply_to_board(db_path: str, board_name: str = "public") -> None:
    logo_uri       = Q_PNG_URL
    favicon_uri    = Q_PNG_URL
    background_uri = _data_uri_svg(BACKGROUND_SVG)

    db = sqlite3.connect(db_path)
    cur = db.execute("SELECT id FROM board WHERE name = ?", (board_name,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"board '{board_name}' not found in {db_path}")
    board_id = row[0]

    db.execute(
        """
        UPDATE board SET
          page_title              = ?,
          meta_title              = ?,
          primary_color           = ?,
          secondary_color         = ?,
          item_radius             = ?,
          icon_color              = NULL,
          logo_image_url          = ?,
          favicon_image_url       = ?,
          background_image_url    = ?,
          background_image_size   = ?,
          background_image_repeat = ?,
          background_image_attachment = ?,
          custom_css              = ?
        WHERE id = ?
        """,
        (
            "Qflix",
            "Qflix · Quadstronaut Flix",
            ORANGE_WARM,
            GOLD,
            "lg",
            logo_uri,
            favicon_uri,
            background_uri,
            "cover",
            "no-repeat",
            "fixed",
            CUSTOM_CSS,
            board_id,
        ),
    )
    db.commit()
    db.close()
    print(f"[ok] applied Qflix theme to board '{board_name}' (id={board_id})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.path.expanduser(
        "~/.apps/homarr-upstream/data/db/db.sqlite"))
    parser.add_argument("--board", default="public",
                        help="board name (default: public)")
    args = parser.parse_args()
    apply_to_board(args.db, args.board)
    return 0


if __name__ == "__main__":
    sys.exit(main())
