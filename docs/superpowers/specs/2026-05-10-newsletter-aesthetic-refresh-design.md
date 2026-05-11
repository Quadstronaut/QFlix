# QFlix newsletter — aesthetic refresh + self-hosted image plumbing

**Date:** 2026-05-10
**Status:** Brainstormed → ready for plan
**Owner:** Quadstronaut

## Goal

Lift the QFlix weekly digest from "functional dark template" to "branded
publication" with eight stackable visual enhancements, and stand up a
hardened, public, server-side image host (`~/www/images/`) so the
newsletter (and any future surface) doesn't have to rely on Listmonk's
restricted media uploader.

## Background

- Current template: `scripts/qflix-newsletter/qflix_newsletter/templates/weekly.html.j2`.
- Operator's reaction to the rendered preview (2026-05-10): the layout
  works but lacks visual identity. Title is `QFLIX` in orange; no logo;
  plain underlines as section dividers; AI section invisible because the
  test render had no Gemini picks.
- Listmonk's media uploader is too restricted to host arbitrary brand
  assets, so we need a separate static-asset path served by the existing
  user-nginx (same nginx that serves `/faq/`).
- Brand asset: `Q.png` exists at the repo root — circular blue gradient
  with white serif "Q" (512×512). Already on GitHub at
  `https://raw.githubusercontent.com/Quadstronaut/QFlix/master/Q.png`.

## Scope

In:
- 8 template enhancements (header, dividers, emoji prefixes, ribbon,
  filmstrip, poster shadows, count badges, footer)
- `~/www/images/` setup with hardened nginx fragment
- Q.png deployment to that folder
- Template rewrites against the new image URL
- Documented GitHub-raw fallback

Out:
- Any change to data-fetch logic (`sources.py`, `ai.py`)
- AI section design (already in template; refresh re-uses it as-is)
- Listmonk template/list config changes
- New cron/timer plumbing — newsletter cadence stays as-is

## Decisions (operator-approved)

### Title
`QFLIX` (orange) → `QFlix` (white, 38px, with a 48×2 gold underline beneath).

### Header (enhancement #1, with operator gradient correction)
Hero block above the existing header, full-width:
- Background: `linear-gradient(180deg, #000 0%, #1e3a8a 50%, #0a1628 100%)`
  — pure black at top, deep blue at center (where Q sits), fades into the
  body background at the bottom. Single direction (top→bottom). Operator
  rationale: makes the Q icon pop against a dark canvas above and a
  smooth fall-off below.
- Q.png centered, 96×96px, with a soft glow halo
  (`box-shadow: 0 6px 28px rgba(255,255,255,.15), 0 4px 24px rgba(30,64,175,.5)`).
- Title "QFlix" in white below the logo.
- 48×2 gold (`#d4af37`) underline.
- Date below in soft cyan (`#7dd3fc`), unchanged.

### Filmstrip accent (enhancement #5)
18px-tall horizontal bar built from
`repeating-linear-gradient(90deg, #0a0a0a 0, #0a0a0a 8px, #1a1a1a 8px, #1a1a1a 14px)`
with `border-top:1px solid #2a2a2a; border-bottom:1px solid #2a2a2a;`
hairlines. Placed twice: directly under the header hero, and directly
above the footer. CSS-only — no images, email-safe.

### Pick of the Week ribbon (enhancement #4)
Diagonal gold corner ribbon on the Pick card. CSS:
- `position: absolute; top: 18px; left: -38px; transform: rotate(-30deg);`
- Background: `linear-gradient(135deg, #e8c456, #b8941f)`
- Text: `Pick of the Week` (the existing label moves into the ribbon —
  no longer a separate row above the poster)
- `box-shadow: 0 2px 8px rgba(0,0,0,.5)` for lift

The card itself keeps its current gold-tinted border but gains
`overflow: hidden` so the ribbon clips cleanly.

### Section dividers (enhancement #2)
All section headings that currently use `<h3>` with a cyan
`border-bottom` (movies, TV, anime movies, anime TV, coming soon, AI
picks) lose the border and instead get an ornament row immediately
beneath. The Nerd corner uses a `<div>` label inside a self-contained
dark box and keeps its current treatment — no divider added there.

```
[ thin cyan hairline ──────── ◆ ──────── thin cyan hairline ]
```

`◆` is a unicode diamond in gold (`#d4af37`); hairlines are
`background:#7dd3fc; opacity:.6`. Renders in every email client we care
about.

### Emoji prefixes (enhancement #3)
Every section label gets a single leading emoji — applies whether the
label is an `<h3>` (most sections) or a `<div>` (Nerd corner):

| Section | Prefix |
|---|---|
| New movies | 🎬 |
| New TV | 📺 |
| New anime movies | 🌸 |
| New anime TV | 🍙 |
| Coming soon | 🗓 |
| A few things you might like | ✨ |
| Nerd corner | 🤓 |

### Count badges (enhancement #7)
Inline pill next to each section title showing how much is in the
section:

- Movies / anime movies: `<count>` (e.g., `12`)
- TV / anime TV: `<n_shows> shows · <n_eps> eps` (e.g., `8 shows · 47 eps`)
- Coming soon: `<count>` capped at the existing 12-item slice
- AI picks: omit (always 3, not interesting to badge)
- Nerd corner: omit (the section is itself the count)

Pill style: `background:#1e40af; color:#fff; padding:2px 10px;
border-radius:12px; font-size:11px; font-weight:600`.

### Poster cards (enhancement #6)
Both movie cards (3-up grid) and anime-movie cards: replace the cyan
`rgba(125,211,252,.12)` card border with `rgba(212,175,55,.4)` gold,
and add `box-shadow: 0 6px 16px rgba(0,0,0,.6)` on the inner poster
image container. The text-only TV-row cards keep the cyan border (they
don't have a poster to lift).

### Footer (enhancement #8, with operator-corrected tagline + gradient)
- Background: `linear-gradient(180deg, #0a1628 0%, #000 100%)` — fades
  the body background into pure black at the very bottom (mirrors the
  header's black-at-top).
- Q.png centered, 40×40px, with a small blue glow.
- Tagline row (replaces existing "Reply to this email if anything's
  broken"): exact text `QFlix · Crafted with precision 🩷 Quadstronaut`.
  - Color: `#cbd5e1`
  - Font size: 12px
  - Letter-spacing: .3px
- Below tagline: existing unsubscribe / view-in-browser links, unchanged.

## Image hosting plan

### Why server-side and not GitHub-only

Operator wants a self-hosted copy as the **primary** source so the
newsletter is whole even if GitHub is unreachable from a recipient's
network (corporate firewalls, school networks, country-level blocks),
and so brand assets stay under operator control. GitHub raw remains as
the **documented backup**.

### Filesystem layout

```
~/www/images/                  # 0755, owner=quadstronaut, group=quadstronaut
~/www/images/Q.png             # 0644, deployed from repo root
~/www/images/_blank.png        # 0644, 1×1 transparent PNG, used as error_page target
```

Sibling to `~/www/qflix-faq/`, served by the same user-nginx instance.

### Public URL

`https://quadstronaut.seedbox.example.com/images/Q.png`

The newsletter template hardcodes this URL (no env var) — it's a fixed
brand asset path, not a per-environment value.

### Backup plan (GitHub raw)

If Manitoba is hard-down for an extended period, swap one URL in
`weekly.html.j2`:

```
- https://quadstronaut.seedbox.example.com/images/Q.png
+ https://raw.githubusercontent.com/Quadstronaut/QFlix/master/Q.png
```

Documented inline as a comment immediately above the `<img>` tag so
future-operator can find it.

### Nginx fragment

New file: `scripts/data/qflix-images.conf` (repo source) → deployed to
`~/.apps/nginx/proxy.d/qflix-images.conf`.

```nginx
# QFlix self-hosted brand assets · public, no htpasswd, hardened.
#
# Deploy:
#   scp scripts/data/qflix-images.conf  $SEEDBOX:~/.apps/nginx/proxy.d/qflix-images.conf
#   scp Q.png                           $SEEDBOX:~/www/images/Q.png
#   ssh $SEEDBOX 'app-nginx restart'

location ^~ /images/ {
    auth_basic off;
    autoindex off;

    alias /home/quadstronaut/www/images/;

    # Allowlist image extensions only. Anything else returns 404.
    location ~ ^/images/.+\.(png|jpg|jpeg|webp|gif|ico)$ {
        # Read-only.
        if ($request_method !~ ^(GET|HEAD)$) { return 405; }

        # 30-day immutable cache (assets are content-addressed by name).
        add_header Cache-Control "public, max-age=2592000, immutable" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
    }

    # Anything that doesn't match the allowlist (e.g. /images/, /images/foo.txt,
    # /images/.htaccess): 404.
    return 404;
}

# Serve a 1×1 transparent PNG instead of branded error pages — no info leak,
# no nginx version footer, no useful signal to a probe.
error_page 403 404 = /images/_blank.png;
```

### Server-tokens hardening

The user-nginx already runs with the default `server_tokens on` —
visible in `Server: nginx/<version>` headers and default error pages.
Add to `~/.apps/nginx/nginx.conf` inside the existing `http {}` block:

```nginx
server_tokens off;
```

This affects the entire user-nginx, not just `/images/`. Operator
acknowledged this is the right blast radius (no app currently relies on
the version header).

### Repo layout

```
scripts/data/qflix-images.conf       # nginx fragment (new)
scripts/configure/60-www-images.sh   # one-shot deploy script (new)
scripts/data/_blank.png              # 1×1 transparent PNG (new, ~70 bytes)
Q.png                                # already exists at repo root, unchanged
```

The `60-www-images.sh` script:
1. `mkdir -p ~/www/images && chmod 755 ~/www/images`
2. `cp` Q.png and `_blank.png` into place; `chmod 644`
3. `cp` `qflix-images.conf` to `~/.apps/nginx/proxy.d/`
4. Add `server_tokens off;` to `~/.apps/nginx/nginx.conf` (idempotent
   grep-then-insert inside `http {}`)
5. `app-nginx restart`
6. Smoke: `curl -sI https://quadstronaut.seedbox.example.com/images/Q.png`
   → expect 200, `Cache-Control` header present, **no** `Server` header
   beyond `Server: nginx`
7. Negative smoke: `curl -sI .../images/` → 404; `.../images/foo.txt`
   → 404; `Server` header in response also stripped of version

## Inventory.md updates

Add row to **Section N. Documentation served by user-nginx**:

| Artifact | Type | Public/Internal | URL | Notes |
|---|---|---|---|---|
| `~/www/images/Q.png` (4 KB) | static png | Public | `https://quadstronaut.seedbox.example.com/images/Q.png` | Brand asset for newsletter + future surfaces. Listmonk media uploader is restricted, so we self-host. Repo source: `Q.png` (root) + `scripts/data/qflix-images.conf` + `scripts/configure/60-www-images.sh`. GitHub raw is the documented fallback. |

## Out-of-scope follow-ups (not this PR)

- Other brand assets (favicon, OG image, social cards) — same `/images/`
  path can host them, but they don't exist yet
- Light-mode email variant
- Per-recipient personalization in the header
- Replacing Q.png with a vector logo (SVG support in email is poor;
  PNG is the right call for now)

## Test plan

1. `python -m qflix_newsletter --dry-run --out-html /tmp/test.html`
   with no Listmonk send.
2. Open `/tmp/test.html` in a browser — verify all 8 enhancements
   render visually.
3. Litmus or equivalent: render in Gmail web, Gmail iOS, Outlook
   (web + 365 desktop), Apple Mail, ProtonMail.
4. Confirm the gold ribbon clips correctly inside the Pick card
   `overflow: hidden` boundary on each client.
5. Confirm filmstrip pattern doesn't degrade to a solid bar in
   `prefers-reduced-motion` clients (it shouldn't — no animation).
6. Confirm Q.png loads from the seedbox URL (200, correct
   `Content-Type: image/png`).
7. After deploying nginx fragment: hit `https://…/images/` (no file)
   and `https://…/images/Q.png?../etc/passwd` — both must return 404
   from the `_blank.png` error page, with no nginx version exposed.

## Open questions

None. Operator approved all 8 enhancements, the gradient corrections
on #1 and #8, and the `~/www/images/` hosting plan with GitHub raw as
documented backup.
