# Newsletter "Behind the scenes" section + autonomous digest — design

**Date:** 2026-06-27 · **Status:** approved (verbal), building same session · **Author:** Quadstronaut + Claude

## Problem

The weekly newsletter (`qflix-newsletter`, Mon 08:00 America/Phoenix = 15:00 UTC, Listmonk
campaign to list 4 "Subscribers") has no section telling members what was *improved* for them
that week. Separately, the bottom "AI Picks" section — powered by Google Gemini — **has never
rendered in production**: the key is deployed, but every weekly run since 2026-05-11 hit
`HTTP 429 quota exceeded, limit: 0` for `gemini-2.0-flash` on the (now-deprecated) free tier.
The error is swallowed (`ai.py:54`) and the section silently vanishes.

## Decisions

1. **Add a "🔧 Behind the scenes" section** between *Coming Soon* and *Nerd Corner*.
2. **Retire Gemini / AI Picks entirely** — code, local + seedbox secret, dependency, config,
   template, install line, doc line. (Free tier is gone; not worth a paid key for movie recs.)
3. **Two content sources, override-then-fallback:**
   - **Preferred — Claude-authored blurb.** A scheduled cloud routine (runs in Anthropic cloud,
     **not** the operator's PC) fires Mon **14:00 UTC** (1 h pre-send), reads the week's commits
     from the **public** GitHub repo, writes a warm non-technical paragraph, and commits
     `digest/latest.json` to a dedicated **`newsletter-digest` branch**.
   - **Fallback — deterministic recap.** If the blurb is missing/stale, the newsletter builds a
     grouped ✨feat / 🔧fix list straight from the commits. Always works; never blocks the send.
4. **"Since last newsletter" = rolling 7-day window.** Matches the weekly timer; no state file.
5. **Reusable `--test-to` mode** sends a true-production render to one address via Listmonk's
   test endpoint (recipient must be a subscriber — operator is id=3). Used tonight to send a
   preview to operator@example.com without touching the 15-member prod list.

## Why this works with the operator's PC off

`Claude cloud routine (Anthropic)` → push `latest.json` → `public GitHub repo`
→ fetched by `seedbox newsletter (always-on Linux)`. The Windows dev box is never in the loop.
Only dependency: the routine needs **write access** to the repo (one-time GitHub auth in Claude).

## Components

| Unit | Responsibility | Failure contract |
|------|----------------|------------------|
| `changelog.py` (new) | `fetch_override()` (digest branch JSON) + `fetch_changelog()` (GitHub commits → grouped recap) + `parse_commit()` (conventional-commit parse, scope strip, `Newsletter:` trailer override) | any exception → empty → section hidden, email still sends |
| `weekly.html.j2` | delete AI Picks (199–221); add "Behind the scenes" card (blurb if present, else grouped list) | gated on `{% if ctx.behind_scenes ... %}` |
| `main.py` / `render.py` / `config.py` | swap `ai_picks` → `behind_scenes`; drop `gemini_api_key`; add `github_repo` (default `Quadstronaut/QFlix`) | — |
| `delivery.py` | add `send_test_campaign()` → Listmonk `POST /campaigns/{id}/test` | — |
| `tests/test_changelog.py` (new) + `conftest.py`/`test_template_render.py` (updated) | parser/grouper/override + mocked fetch + network-failure-hides-section; drop `AiPick` fixture | offline |
| `skills/qflix-digest/SKILL.md` (new) | editorial instructions: commits → benefit-framed subscriber blurb → commit `digest/latest.json` | — |
| cloud routine | run `/qflix-digest` Mon 14:00 UTC | newsletter falls back if it didn't run |
| decom | delete `ai.py`, `secrets/gemini.api_key` (local + seedbox), `google-generativeai` dep, install copy line, `secrets-convention.md:39` | — |

## Editorial rules for the blurb (encoded in the skill)

- Audience = non-technical Plex members. Translate, don't transcribe: "cap GOMAXPROCS=4" →
  "more reliable streaming". Lead with the benefit to *them*.
- Include user-facing improvements (feat/fix/perf). Skip pure docs/chore/refactor/CI.
- 2–4 sentences or ≤4 short bullets. Warm, brief, no jargon, no version numbers.
- Output `digest/latest.json`: `{ "week_of": "YYYY-MM-DD", "generated_at": ISO8601,
  "since": ISO8601, "html": "<friendly blurb as inline HTML>" }`.

## Digest freshness guard

Newsletter uses the blurb only if `week_of` is within the current send week; otherwise falls
back to the deterministic recap. Prevents showing a stale blurb if a routine run is missed.

## Out of scope (noted for later)

State-file exactness for "since last send"; paid Gemini; richer ops data (maint-window logs are
seedbox-side and not reachable from the cloud routine, so the blurb is commit-derived only).
