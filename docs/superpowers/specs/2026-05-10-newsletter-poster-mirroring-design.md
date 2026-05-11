# QFlix newsletter — poster mirroring + dead-image protection

**Date:** 2026-05-10
**Status:** Brainstormed → ready for plan
**Owner:** Quadstronaut

## Goal

Stop hot-linking TMDB and Tautulli poster URLs in delivered newsletters.
Mirror every poster to the seedbox-local `~/www/images/newsletter/` cache
at render time, and rewrite the email to reference QFlix-served URLs.
This protects recipients from:

- TMDB CDN rot (the specific failure mode that produced the borked card
  in the 2026-05-10 issue: poster pulled from Gmail archive weeks later
  returned 404).
- Future Tautulli outages or hostname changes.
- Any single point of failure between the recipient's mail client and
  the original poster source.

A daily prune keeps the cache at ~10 MB steady state.

## Background

- Newsletter pipeline: `qflix-newsletter` Python package on the seedbox,
  driven by `qflix-newsletter.timer`. Flow: Tautulli `recently_added` →
  `enrich_with_tmdb()` rewrites `thumb_url` to
  `https://image.tmdb.org/t/p/w342{poster_path}` → render Jinja2 →
  send via Listmonk.
- The 2026-05-10 issue contained a card with no title and no poster but
  a populated `★ 8.1 / 2025` line. Root cause not fully traced, but the
  symptom — a recipient seeing a dead poster — is the class of failure
  this design addresses.
- `/images/` is already nginx-served from `~/www/images/` with hardening
  in place: extension allowlist (png/jpg/jpeg/webp/gif/ico), 30-day
  immutable `Cache-Control`, autoindex off, error masking that preserves
  404 status. See `scripts/data/qflix-images.conf` and
  `scripts/configure/60-www-images.sh`.
- The qflix-newsletter process runs *on* the seedbox, so writing to
  `~/www/images/newsletter/` is a local filesystem operation — no SSH or
  separate transport needed.

## Scope

In:

- New module `qflix_newsletter/posters.py` to mirror posters and rewrite
  URLs.
- One new field on `RecentItem` (`tautulli_thumb_url`) so the original
  Tautulli URL survives `enrich_with_tmdb()` and is available as a
  fallback source.
- One call inserted in `main.py` between TMDB enrichment and context
  build.
- Daily systemd prune timer at 00:00 UTC.
- New configure script `49a-newsletter-poster-cache.sh` to stand up the
  directory, install/enable the timer, and smoke-test the path.
- Unit tests covering the source fallback chain and every safety rail.

Out:

- Backfill of past newsletters (only future issues get the mirror; older
  emails keep hot-linking TMDB).
- Changes to the email template — `thumb_url=None` already hides the
  `<img>` tag, which is the existing graceful path.
- Tautulli or TMDB API changes.
- Notifying recipients about dead-image replacements (silent rewrite is
  the design).
- Any change to the newsletter cadence or send time.

## Architecture

### Data flow

```
Tautulli recently_added
  → _recent_from_tautulli()                   # adds tautulli_thumb_url
  → enrich_with_tmdb()                        # rewrites thumb_url to TMDB
  → mirror_posters()  ← NEW
       for each item with a non-None thumb_url:
         try thumb_url (TMDB), validate, mirror, rewrite → done
         on failure: try tautulli_thumb_url, validate, mirror, rewrite
         on both failures: thumb_url=None (template hides image)
  → build_email_context() / render_html() / send
```

### Source fallback chain in `mirror_posters()`

For each item:

1. **TMDB** (`item.thumb_url`). GET, validate, mirror, rewrite. Stop.
2. **Tautulli** (`item.tautulli_thumb_url`). Same. Stop.
3. **Both dead**: `item.thumb_url = None`. Warn-log with the title so
   the post-run log surfaces it. The card still renders, just without an
   image — same UX as a current TMDB-miss.

### Cache layout

- Dir: `~/www/images/newsletter/`, mode 0755, owned by the seedbox user.
- Filename: `<sha>.<ext>` where `sha = sha256(<successful_source_url>)[:16]`
  and `ext` is derived from the response `Content-Type` against the
  allowlist (jpeg → `.jpg`, png → `.png`, webp → `.webp`, gif → `.gif`).
- Public URL: `https://<public_host>/images/newsletter/<sha>.<ext>`.
- The same poster URL always hashes to the same filename, so two items
  in the same issue that point at the same poster (rare — same show
  across two libraries) dedupe naturally, and a poster that's been
  cached in a prior week's run short-circuits the entire HTTP fetch.

### Why SHA over the source URL

- Stable: the email URL is reproducible from the source URL alone, so
  re-runs and dry-runs are idempotent.
- Collision-safe at 16 hex chars (64 bits): the namespace
  is ~10⁶ posters over the cache lifetime — collision probability
  negligible (~10⁻⁸).
- No path traversal: SHA hex + extension from a closed allowlist is
  inert input to the filesystem.

## Components

### `qflix_newsletter/posters.py` (new)

Single public function:

```python
def mirror_posters(
    items: list[RecentItem],
    *,
    cache_dir: Path,
    public_base: str,
    session: Optional[requests.Session] = None,
    timeout_s: float = 10.0,
    max_bytes: int = 2 * 1024 * 1024,
) -> list[RecentItem]:
    """Mirror each item's poster to cache_dir and rewrite item.thumb_url
    to a public_base URL. Mutates items in place; returns them for
    chainability. Failures cascade to thumb_url=None.

    Logs one summary line per call:
      mirror_posters: tmdb_hit=X tautulli_fallback=Y dead=Z cached=W
    """
```

Private helpers (module-internal, all testable):

- `_sha_for(url: str) -> str` — sha256 hex, first 16 chars.
- `_ext_for(content_type: str) -> Optional[str]` — allowlist lookup.
- `_validate_magic_bytes(prefix: bytes, content_type: str) -> bool` —
  match the first 12 bytes against the claimed MIME type.
- `_fetch_and_write(url, target_path, *, session, timeout_s, max_bytes)
  -> bool` — does one source attempt: GET, validate response, magic-byte
  check, atomic write. Returns True on success, False on any failure.
  Cleans up any partial `.tmp` file on failure.
- `_try_sources(item, cache_dir, ...) -> tuple[Outcome, Optional[Path]]`
  — runs the fallback chain for one item.

### `RecentItem.tautulli_thumb_url` (new field)

```python
@dataclass
class RecentItem:
    ...
    thumb_url: Optional[str]
    tautulli_thumb_url: Optional[str] = None   # NEW
    ...
```

Populated in `_recent_from_tautulli()`: the existing Tautulli
pms_image_proxy URL is assigned to **both** `thumb_url` and
`tautulli_thumb_url`. Then `enrich_with_tmdb()` overwrites only
`thumb_url`, leaving the Tautulli URL intact for fallback.

### `main.py` integration

One new call after enrichment, before context build:

```python
recent = enrich_with_tmdb(cfg, recent)
recent = mirror_posters(
    recent,
    cache_dir=cfg.poster_cache_dir,
    public_base=f"https://{cfg.public_host}",
)
```

Two new `Config` attributes:

- `poster_cache_dir: Path` — defaults to
  `Path.home() / "www" / "images" / "newsletter"`. Overridable via
  `QFLIX_POSTER_CACHE_DIR` env var.
- The existing `public_host` is reused — no new secret.

### Daily prune

`scripts/maint/systemd/qflix-poster-cache-prune.timer`:

```ini
[Unit]
Description=QFlix newsletter poster cache 30-day prune

[Timer]
OnCalendar=*-*-* 00:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

`scripts/maint/systemd/qflix-poster-cache-prune.service`:

```ini
[Unit]
Description=QFlix newsletter poster cache 30-day prune

[Service]
Type=oneshot
ExecStart=/usr/bin/find %h/www/images/newsletter -type f -mtime +30 -delete
```

`Persistent=true` ensures a missed run (seedbox reboot, container
restart) fires on next boot. `RandomizedDelaySec=300` spreads load if
00:00 collides with other timers.

Compatible with `lib/health.py · systemd_oneshot` for monitoring (per
the prior pattern documented in
`reference_systemd-oneshot-probe.md`).

### Configure script

`scripts/configure/49a-newsletter-poster-cache.sh` — idempotent, runs
under the existing `apply` flow. Numbered `49a` to slot directly after
`49-qflix-newsletter-install.sh`. Steps:

1. `mkdir -p ~/www/images/newsletter && chmod 755`.
2. Deploy the timer + service to `~/.config/systemd/user/`.
3. `systemctl --user daemon-reload`.
4. `systemctl --user enable --now qflix-poster-cache-prune.timer`.
5. Smoke: write a probe JPEG, curl
   `https://<public_host>/images/newsletter/<probe>.jpg`, assert 200 +
   `Cache-Control: immutable`. Remove probe.
6. Verify the timer is loaded and active.

## Safety rails

All of these must be present and tested:

| Rail | Trigger | Action |
|------|---------|--------|
| Per-source timeout | 10s connect+read | Treat as failure, try fallback |
| Size cap (header) | `Content-Length > 2 MB` | Refuse before download |
| Size cap (streamed) | bytes read > 2 MB mid-stream | Abort, clean partial, try fallback |
| Content-Type allowlist | not in `{jpeg, png, webp, gif}` | Treat as failure |
| Magic-byte sniff | first 12 bytes don't match claimed MIME | Treat as failure |
| Atomic write | always | `.tmp` then `os.replace()` |
| Path safety | always | filename is `sha256[:16] + ext` only |
| Cache short-circuit | target file exists | Skip both HTTP fetches |
| Retry on 5xx / conn error | once per source | 1s backoff |
| No retry on 4xx | always | One shot, then fallback |

Magic-byte signatures (first 12 bytes):

- PNG: `89 50 4E 47 0D 0A 1A 0A`
- JPEG: `FF D8 FF`
- GIF: `47 49 46 38 (37|39) 61`
- WEBP: bytes 0-3 = `52 49 46 46` and bytes 8-11 = `57 45 42 50`

## Error handling philosophy

Every failure mode degrades to "no image, card still renders" — same
visual outcome as a TMDB search miss today. The newsletter NEVER fails
to send because of a poster issue. The only observability surface is
the per-run log line and per-item warning, which the operator can scan
post-send.

## Testing

`tests/unit/test_qflix_newsletter_posters.py` (new file). Uses
`pytest` + `unittest.mock` (matches the existing convention across
`tests/unit/test_qflix_newsletter_*.py` — no `responses` or
`requests-mock` in the suite). HTTP responses faked with a `MagicMock`
on `requests.Session.get` returning canned status/headers/iter_content.

Required cases:

1. **TMDB happy path** — 200 + `image/jpeg` + valid JPEG bytes. Assert
   file exists at `<cache_dir>/<sha>.jpg`, URL rewritten to
   `<public_base>/images/newsletter/<sha>.jpg`. Log records `tmdb_hit=1`.
2. **Cache hit** — pre-write `<sha>.jpg` to cache_dir, mock both HTTP
   calls to raise (so any network attempt fails the test). Run. Assert
   no network call made, URL still rewritten correctly. Log records
   `cached=1`.
3. **TMDB 404 → Tautulli succeeds** — first GET returns 404, second
   returns valid JPEG. Assert file written, URL rewritten, log records
   `tautulli_fallback=1`.
4. **Both dead** — both GETs return 404. Assert `item.thumb_url=None`,
   no file written, log records `dead=1` and a warning is emitted with
   the item title.
5. **Non-image Content-Type** — server returns `text/html` with HTML
   bytes. Assert treated as failure, fallback tried, no file written for
   the first source.
6. **Magic-byte mismatch** — server returns `Content-Type: image/png`
   but body starts with `<html>`. Assert treated as failure.
7. **Content-Length too large** — server returns
   `Content-Length: 3000000`. Assert refused without reading body.
8. **Streamed too large** — server returns no `Content-Length` but
   streams 3 MB. Assert read aborts at 2 MB, partial `.tmp` cleaned up,
   fallback tried.
9. **5xx with retry** — first attempt 503, retry returns 200 valid.
   Assert mirrored on retry.
10. **4xx no retry** — first attempt 404. Assert exactly one call to
    TMDB before fallback (not two).
11. **Connection error with retry** — first attempt raises
    `ConnectionError`, retry returns 200. Assert mirrored on retry.
12. **SHA determinism** — same input URL twice, assert same filename.
13. **SHA over successful source** — TMDB 404, Tautulli 200. Assert
    filename hashes the Tautulli URL, not the TMDB URL.
14. **Atomic write** — simulate exception mid-write. Assert no
    `<sha>.jpg` in cache_dir, no `.tmp` left behind.
15. **Path safety** — pass a URL crafted to embed `../` in any
    surface-able way (it can't; SHA-derived filename). This is a
    sanity-check assertion only.

Integration test (extends `tests/unit/test_qflix_newsletter_render.py`
or a new file): run the full `main.run(dry_run=True, out_html=...)` flow
against a stubbed Tautulli + TMDB + filesystem, assert every `<img>`
src in the rendered HTML matches `^https://[^/]+/images/newsletter/[0-9a-f]{16}\.(jpg|png|webp|gif)$`.

## Open decisions (intentionally deferred to plan stage)

These were considered but kept out of the spec to avoid premature lock-in:

- **Whether to persist Outcome to a structured log file** for trend
  analysis, vs. relying on systemd journal. Probably YAGNI for v1.
- **Whether the prune timer should also delete `<sha>.tmp` files** older
  than 1 hour as a belt-and-braces cleanup. Probably yes but trivial,
  decide in plan.

## Deliverables

- `scripts/qflix-newsletter/qflix_newsletter/posters.py`
- `scripts/qflix-newsletter/qflix_newsletter/sources.py` (one field +
  one assignment)
- `scripts/qflix-newsletter/qflix_newsletter/main.py` (one call)
- `scripts/qflix-newsletter/qflix_newsletter/config.py` (one attribute
  + env override)
- `tests/unit/test_qflix_newsletter_posters.py`
- `tests/unit/test_qflix_newsletter_render.py` (integration assertion)
- `scripts/maint/systemd/qflix-poster-cache-prune.timer`
- `scripts/maint/systemd/qflix-poster-cache-prune.service`
- `scripts/configure/49a-newsletter-poster-cache.sh`
- `inventory.md` (note the new timer + cache dir)

## Acceptance

- Next weekly send: 100% of `<img>` `src` attributes resolve to
  `https://<public_host>/images/newsletter/...`.
- A manual `curl -I` on any one of those URLs returns 200 with
  `Cache-Control: ... immutable`.
- Killing the TMDB and Tautulli endpoints (e.g. firewall block during a
  dry-run) does not cause the newsletter to fail; affected items render
  without images and are listed in the warning log.
- 31 days after first send, `find ~/www/images/newsletter -type f -mtime
  +30` returns zero files (the prune timer ran).
