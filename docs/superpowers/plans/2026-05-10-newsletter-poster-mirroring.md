# Newsletter Poster Mirroring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror every TMDB/Tautulli newsletter poster to a local cache served at `https://<public_host>/images/newsletter/` so delivered newsletters survive upstream CDN rot.

**Architecture:** New `posters.py` module sits between `enrich_with_tmdb()` and `build_email_context()` in the existing pipeline. For each item it tries TMDB → Tautulli, mirrors the first successful source to `~/www/images/newsletter/<sha>.<ext>`, and rewrites `item.thumb_url` to the local URL. A daily systemd timer prunes files older than 30 days.

**Tech Stack:** Python 3.11, `requests`, `pathlib`, `hashlib`, `unittest.mock`, pytest, systemd user units, nginx (already configured at `/images/`).

**Spec:** `docs/superpowers/specs/2026-05-10-newsletter-poster-mirroring-design.md`

---

## File Structure

**Create:**
- `scripts/qflix-newsletter/qflix_newsletter/posters.py` — mirror logic
- `tests/unit/test_qflix_newsletter_posters.py` — unit tests
- `tests/unit/test_qflix_newsletter_config.py` — new config tests
- `scripts/maint/systemd/qflix-poster-cache-prune.timer`
- `scripts/maint/systemd/qflix-poster-cache-prune.service`
- `scripts/configure/49a-newsletter-poster-cache.sh`

**Modify:**
- `scripts/qflix-newsletter/qflix_newsletter/sources.py` — add `tautulli_thumb_url` field + assignment
- `scripts/qflix-newsletter/qflix_newsletter/config.py` — add `poster_cache_dir` attribute
- `scripts/qflix-newsletter/qflix_newsletter/main.py` — call `mirror_posters()`
- `tests/unit/test_qflix_newsletter_sources.py` — assert new field
- `tests/unit/test_qflix_newsletter_render.py` — integration assertion
- `inventory.md` — note the new cache dir + timer

**Key APIs (defined once, used everywhere):**

```python
# posters.py
def _sha_for(url: str) -> str                       # sha256(url)[:16]
def _ext_for(content_type: Optional[str]) -> Optional[str]
def _validate_magic_bytes(prefix: bytes, content_type: str) -> bool
def _cache_lookup(cache_dir: Path, sha: str) -> Optional[Path]
def _public_url(public_base: str, sha: str, ext: str) -> str
def _fetch_and_write_one(
    url: str, cache_dir: Path, sha: str, *,
    session: requests.Session, timeout_s: float, max_bytes: int,
) -> tuple[str, Optional[Path]]                     # returns ("ok"|"retry"|"fail", path_or_None)
def _try_one_source_with_retry(
    url: str, cache_dir: Path, sha: str, *,
    session: requests.Session, timeout_s: float, max_bytes: int,
) -> tuple[str, Optional[Path]]                     # wraps _fetch_and_write_one with 1 retry on "retry"
def mirror_posters(
    items: Sequence[RecentItem], *,
    cache_dir: Path, public_base: str,
    session: Optional[requests.Session] = None,
    timeout_s: float = 10.0, max_bytes: int = 2 * 1024 * 1024,
) -> Sequence[RecentItem]                           # public entry point; mutates items in place
```

---

## Task 1: Add `tautulli_thumb_url` field to `RecentItem`

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/sources.py`
- Test: `tests/unit/test_qflix_newsletter_sources.py`

The `enrich_with_tmdb()` step overwrites `thumb_url`. To make the Tautulli URL available as a fallback later, we capture it on a new field at parse time.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_qflix_newsletter_sources.py` (after the existing `test_fetch_recently_added_normalizes_movie_and_episode` test):

```python
def test_fetch_recently_added_preserves_tautulli_thumb_url(tmp_path):
    """tautulli_thumb_url survives enrich_with_tmdb so it can be a fallback source."""
    cfg = _config_stub(tmp_path)
    fixture = json.loads((FIXTURES / "recent.json").read_text())
    with patch.object(sources.requests, "get", return_value=_mock_response(fixture)):
        items = sources.fetch_recently_added(cfg, count=10)

    movie = next(i for i in items if i.title == "Dune: Part Two")
    assert movie.tautulli_thumb_url is not None
    assert movie.tautulli_thumb_url.startswith(
        "https://seedbox.example.com/tautulli/pms_image_proxy"
    )
    # Equal to thumb_url at this stage; enrich_with_tmdb will diverge them.
    assert movie.tautulli_thumb_url == movie.thumb_url
```

- [ ] **Step 2: Run test to verify it fails**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_sources.py::test_fetch_recently_added_preserves_tautulli_thumb_url -v
```

Expected: FAIL with `AttributeError: 'RecentItem' object has no attribute 'tautulli_thumb_url'`.

- [ ] **Step 3: Add the field to the dataclass and populate it**

In `scripts/qflix-newsletter/qflix_newsletter/sources.py`, modify the `RecentItem` dataclass — add `tautulli_thumb_url` right after `thumb_url`:

```python
@dataclass
class RecentItem:
    """One Tautulli `recently_added` row, normalized."""

    media_type: str
    title: str
    year: Optional[int]
    summary: str
    thumb_url: Optional[str]
    tautulli_thumb_url: Optional[str]
    added_at: int
    rating: Optional[float]
    show_title: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    library_name: Optional[str] = None
    tmdb_id: Optional[int] = None
    rating_key: Optional[str] = None
    grandparent_rating_key: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)
```

Modify `_recent_from_tautulli()` to set both fields:

```python
def _recent_from_tautulli(row: dict, tautulli_base: str) -> RecentItem:
    mt = row.get("media_type", "")
    is_episode = mt == "episode"
    thumb = row.get("thumb") or row.get("art")
    thumb_url = (
        f"{tautulli_base}/pms_image_proxy?img={thumb}&width=300&height=450&fallback=poster"
        if thumb
        else None
    )
    # ... existing rating/year/season/episode parsing unchanged ...
    return RecentItem(
        media_type=mt,
        title=row.get("title") or "",
        year=year,
        summary=row.get("summary") or "",
        thumb_url=thumb_url,
        tautulli_thumb_url=thumb_url,  # preserved for fallback after TMDB enrich
        added_at=int(row.get("added_at") or 0),
        rating=rating,
        show_title=row.get("grandparent_title") if is_episode else None,
        season=int(season_raw) if season_raw not in (None, "") else None,
        episode=int(episode_raw) if episode_raw not in (None, "") else None,
        library_name=row.get("library_name"),
        tmdb_id=_extract_tmdb_id(row),
        rating_key=str(rk_raw) if rk_raw not in (None, "") else None,
        grandparent_rating_key=str(grk_raw) if grk_raw not in (None, "") else None,
        raw=row,
    )
```

- [ ] **Step 4: Run all newsletter tests to find collateral breakage**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_*.py -v
```

Expected: All pass *except* any test that constructs `RecentItem(...)` directly will fail with `TypeError: missing required positional argument 'tautulli_thumb_url'`.

- [ ] **Step 5: Fix any direct constructors found**

Common location: `tests/unit/test_qflix_newsletter_render.py::_movie()` and `_episode()` helpers. Add `tautulli_thumb_url=thumb` (same value as `thumb_url`):

```python
def _movie(title: str, *, rating=None, year=2024, library="Movies", thumb="/x/thumb") -> RecentItem:
    return RecentItem(
        media_type="movie",
        title=title,
        year=year,
        summary="lorem ipsum dolor sit amet " * 20,
        thumb_url=thumb,
        tautulli_thumb_url=thumb,  # NEW
        added_at=1715212800,
        rating=rating,
        library_name=library,
    )
```

Re-run the suite until green:

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_*.py -v
```

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/sources.py tests/unit/test_qflix_newsletter_sources.py tests/unit/test_qflix_newsletter_render.py
git commit -m "feat: capture tautulli_thumb_url on RecentItem for fallback use"
```

---

## Task 2: Add `poster_cache_dir` to `Config`

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/config.py`
- Create: `tests/unit/test_qflix_newsletter_config.py`
- Modify: `tests/unit/test_qflix_newsletter_sources.py` (extend `_config_stub`)

`Config.poster_cache_dir: Path` with env override `QFLIX_POSTER_CACHE_DIR`, default `~/www/images/newsletter/`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_qflix_newsletter_config.py`:

```python
"""Config tests — covers the new poster_cache_dir attribute + env override."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from qflix_newsletter.config import Config


def _write_min_secrets(d: Path) -> None:
    """Minimum set of secrets for Config.from_env() to succeed."""
    for name, val in {
        "tautulli.key": "tk", "tautulli.port": "42000",
        "sonarr.key": "sk", "sonarr.port": "42010", "sonarr.urlbase": "sonarr",
        "radarr.key": "rk", "radarr.port": "42011", "radarr.urlbase": "radarr",
        "listmonk.api_user": "u", "listmonk.api_token": "tok", "listmonk.port": "42014",
        "seedbox.host": "seedbox.example.com",
    }.items():
        (d / name).write_text(val)


def test_poster_cache_dir_defaults_to_home_www(tmp_path):
    _write_min_secrets(tmp_path)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("QFLIX_POSTER_CACHE_DIR", None)
        cfg = Config.from_env(secrets_dir=tmp_path)
    assert cfg.poster_cache_dir == Path.home() / "www" / "images" / "newsletter"


def test_poster_cache_dir_env_override(tmp_path):
    _write_min_secrets(tmp_path)
    override = tmp_path / "custom-cache"
    with patch.dict(os.environ, {"QFLIX_POSTER_CACHE_DIR": str(override)}):
        cfg = Config.from_env(secrets_dir=tmp_path)
    assert cfg.poster_cache_dir == override
```

- [ ] **Step 2: Run test to verify it fails**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_config.py -v
```

Expected: FAIL with `AttributeError: 'Config' object has no attribute 'poster_cache_dir'`.

- [ ] **Step 3: Add the field**

In `scripts/qflix-newsletter/qflix_newsletter/config.py`, add `poster_cache_dir` to the `Config` dataclass (after `public_host`):

```python
@dataclass
class Config:
    # ... existing fields ...
    public_host: str
    poster_cache_dir: Path
```

In `Config.from_env()`, just before the `return cls(...)`, compute the default + env override:

```python
        poster_cache_override = os.environ.get("QFLIX_POSTER_CACHE_DIR")
        if poster_cache_override:
            poster_cache_dir = Path(poster_cache_override)
        else:
            poster_cache_dir = Path.home() / "www" / "images" / "newsletter"
```

Add to the `return cls(...)` kwargs:

```python
            public_host=(maybe_read_secret("seedbox.host", d) or "quadstronaut.seedbox.example.com"),
            poster_cache_dir=poster_cache_dir,
        )
```

- [ ] **Step 4: Update existing `_config_stub()` helpers**

Search for `Config(` constructor calls in tests. The main one is `_config_stub()` in `tests/unit/test_qflix_newsletter_sources.py`. Add `poster_cache_dir`:

```python
def _config_stub(tmp_path) -> Config:
    return Config(
        # ... existing kwargs unchanged ...
        public_host="seedbox.example.com",
        poster_cache_dir=tmp_path / "poster-cache",
    )
```

Apply the same edit in any other test helper that constructs `Config(...)` directly. Search with:

```
grep -rn "Config(" tests/unit/
```

- [ ] **Step 5: Run all newsletter tests**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_*.py -v
```

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/config.py tests/unit/test_qflix_newsletter_config.py tests/unit/test_qflix_newsletter_sources.py
git commit -m "feat: add Config.poster_cache_dir with QFLIX_POSTER_CACHE_DIR override"
```

---

## Task 3: Create `posters.py` skeleton with pure helpers

**Files:**
- Create: `scripts/qflix-newsletter/qflix_newsletter/posters.py`
- Create: `tests/unit/test_qflix_newsletter_posters.py`

Three pure functions to start: `_sha_for`, `_ext_for`, `_validate_magic_bytes`. No I/O — easy to TDD.

- [ ] **Step 1: Write failing tests for the three helpers**

Create `tests/unit/test_qflix_newsletter_posters.py`:

```python
"""Unit tests for the poster mirroring module."""
from __future__ import annotations

from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from qflix_newsletter import posters


# ── _sha_for ────────────────────────────────────────────────────────────────

def test_sha_for_is_deterministic():
    a = posters._sha_for("https://image.tmdb.org/t/p/w342/abc.jpg")
    b = posters._sha_for("https://image.tmdb.org/t/p/w342/abc.jpg")
    assert a == b


def test_sha_for_distinguishes_urls():
    a = posters._sha_for("https://image.tmdb.org/t/p/w342/abc.jpg")
    b = posters._sha_for("https://image.tmdb.org/t/p/w342/def.jpg")
    assert a != b


def test_sha_for_is_16_hex_chars():
    s = posters._sha_for("https://image.tmdb.org/t/p/w342/abc.jpg")
    assert len(s) == 16
    assert all(c in "0123456789abcdef" for c in s)


# ── _ext_for ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ct,ext", [
    ("image/jpeg", "jpg"),
    ("image/jpeg; charset=binary", "jpg"),
    ("image/png", "png"),
    ("image/webp", "webp"),
    ("image/gif", "gif"),
    ("IMAGE/JPEG", "jpg"),  # case-insensitive
])
def test_ext_for_known_types(ct, ext):
    assert posters._ext_for(ct) == ext


@pytest.mark.parametrize("ct", [
    "text/html",
    "application/octet-stream",
    "image/svg+xml",  # not in allowlist
    "image/bmp",       # not in allowlist
    "",
    None,
])
def test_ext_for_rejects_unknown(ct):
    assert posters._ext_for(ct) is None


# ── _validate_magic_bytes ───────────────────────────────────────────────────

def test_validate_magic_bytes_png():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4
    assert posters._validate_magic_bytes(png, "image/png") is True


def test_validate_magic_bytes_jpeg():
    jpeg = b"\xff\xd8\xff" + b"\x00" * 9
    assert posters._validate_magic_bytes(jpeg, "image/jpeg") is True


def test_validate_magic_bytes_gif87():
    gif = b"GIF87a" + b"\x00" * 6
    assert posters._validate_magic_bytes(gif, "image/gif") is True


def test_validate_magic_bytes_gif89():
    gif = b"GIF89a" + b"\x00" * 6
    assert posters._validate_magic_bytes(gif, "image/gif") is True


def test_validate_magic_bytes_webp():
    webp = b"RIFF\x00\x00\x00\x00WEBP"
    assert posters._validate_magic_bytes(webp, "image/webp") is True


def test_validate_magic_bytes_mismatch_html_as_png():
    html = b"<html><head" + b"\x00\x00"
    assert posters._validate_magic_bytes(html, "image/png") is False


def test_validate_magic_bytes_too_short():
    short = b"\x89P"
    assert posters._validate_magic_bytes(short, "image/png") is False
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py -v
```

Expected: All FAIL with `ModuleNotFoundError: No module named 'qflix_newsletter.posters'`.

- [ ] **Step 3: Create `posters.py` with the three helpers**

Create `scripts/qflix-newsletter/qflix_newsletter/posters.py`:

```python
"""Mirror newsletter posters to a local cache and rewrite URLs.

Inserted between enrich_with_tmdb() and build_email_context() in the
pipeline. For each item it tries TMDB then Tautulli; the first source
that yields a valid image is copied to <cache_dir>/<sha>.<ext> and the
item's thumb_url is rewritten to a public_base URL.

Every failure mode degrades to thumb_url=None (template hides the
<img>). The newsletter never fails to send because of a poster issue.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Content-Type → file extension. File extensions match the nginx allowlist
# in scripts/data/qflix-images.conf.
_EXT_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def _sha_for(url: str) -> str:
    """sha256 hex of url, first 16 chars. Stable namespace for cache keys."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _ext_for(content_type: Optional[str]) -> Optional[str]:
    """Return file extension if content_type is in the allowlist, else None."""
    if not content_type:
        return None
    primary = content_type.split(";", 1)[0].strip().lower()
    return _EXT_BY_TYPE.get(primary)


def _validate_magic_bytes(prefix: bytes, content_type: str) -> bool:
    """Match first 12 bytes of response body against claimed Content-Type.

    Defeats a server that mislabels HTML/text as an image.
    """
    if len(prefix) < 12:
        return False
    primary = content_type.split(";", 1)[0].strip().lower()
    if primary == "image/png":
        return prefix[:8] == b"\x89PNG\r\n\x1a\n"
    if primary == "image/jpeg":
        return prefix[:3] == b"\xff\xd8\xff"
    if primary == "image/gif":
        return prefix[:6] in (b"GIF87a", b"GIF89a")
    if primary == "image/webp":
        return prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/posters.py tests/unit/test_qflix_newsletter_posters.py
git commit -m "feat: posters.py skeleton with sha/ext/magic-byte helpers"
```

---

## Task 4: `_fetch_and_write_one` — happy path

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/posters.py`
- Modify: `tests/unit/test_qflix_newsletter_posters.py`

One source attempt: GET, validate, atomic write. Returns `("ok"|"retry"|"fail", path_or_None)`. The string-outcome return is what lets the caller distinguish *transient* (retry) from *permanent* (fail) failures without inspecting exceptions.

- [ ] **Step 1: Write the failing happy-path test**

Append to `tests/unit/test_qflix_newsletter_posters.py`:

```python
# ── _fetch_and_write_one ────────────────────────────────────────────────────

def _ok_response(content_type: str, body: bytes, content_length: Optional[int] = None):
    """Build a MagicMock that mimics a streamed requests response."""
    resp = MagicMock()
    resp.status_code = 200
    headers = {"Content-Type": content_type}
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    elif body is not None:
        headers["Content-Length"] = str(len(body))
    resp.headers = headers
    resp.iter_content = MagicMock(return_value=iter([body]))
    resp.raise_for_status = MagicMock()
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: None
    return resp


def test_fetch_and_write_one_happy_path(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    jpeg_body = b"\xff\xd8\xff\xe0" + b"\x00" * 1000

    session = MagicMock()
    session.get.return_value = _ok_response("image/jpeg", jpeg_body)

    sha = "abcdef0123456789"
    outcome, path = posters._fetch_and_write_one(
        "https://example/poster.jpg",
        cache_dir, sha,
        session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
    )

    assert outcome == "ok"
    assert path == cache_dir / f"{sha}.jpg"
    assert path.exists()
    assert path.read_bytes() == jpeg_body
    # No .tmp left behind
    assert not (cache_dir / f"{sha}.jpg.tmp").exists()
```

- [ ] **Step 2: Run to verify failure**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py::test_fetch_and_write_one_happy_path -v
```

Expected: FAIL with `AttributeError: module 'qflix_newsletter.posters' has no attribute '_fetch_and_write_one'`.

- [ ] **Step 3: Implement `_fetch_and_write_one`**

Add to `scripts/qflix-newsletter/qflix_newsletter/posters.py`:

```python
import os
import requests

_MAGIC_PREFIX_BYTES = 12
_DEFAULT_CHUNK_SIZE = 64 * 1024


def _fetch_and_write_one(
    url: str,
    cache_dir: Path,
    sha: str,
    *,
    session: requests.Session,
    timeout_s: float,
    max_bytes: int,
) -> tuple[str, Optional[Path]]:
    """One source attempt. Returns ('ok'|'retry'|'fail', target_path_or_None).

    The file extension is decided by the response Content-Type; the
    caller passes cache_dir + sha and we resolve the final filename.

    'retry' = transient (5xx or ConnectionError) — caller may sleep + retry.
    'fail'  = permanent (4xx, validation failure) — give up on this URL.
    """
    target_path: Optional[Path] = None
    tmp_path: Optional[Path] = None
    try:
        with session.get(url, timeout=timeout_s, stream=True) as resp:
            if 500 <= resp.status_code < 600:
                return "retry", None
            if resp.status_code != 200:
                return "fail", None

            content_type = resp.headers.get("Content-Type", "")
            ext = _ext_for(content_type)
            if ext is None:
                return "fail", None

            target_path = cache_dir / f"{sha}.{ext}"
            tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")

            cl_raw = resp.headers.get("Content-Length")
            if cl_raw is not None:
                try:
                    if int(cl_raw) > max_bytes:
                        return "fail", None
                except ValueError:
                    pass  # malformed header, fall back to streamed cap

            written = 0
            magic_prefix = b""
            magic_checked = False
            with tmp_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=_DEFAULT_CHUNK_SIZE):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        return "fail", None
                    if not magic_checked:
                        magic_prefix += chunk
                        if len(magic_prefix) >= _MAGIC_PREFIX_BYTES:
                            if not _validate_magic_bytes(magic_prefix, content_type):
                                return "fail", None
                            magic_checked = True
                    f.write(chunk)
                if not magic_checked:
                    return "fail", None

        os.replace(tmp_path, target_path)
        return "ok", target_path
    except requests.ConnectionError:
        return "retry", None
    except (requests.RequestException, OSError):
        return "fail", None
    finally:
        # Clean up any orphan .tmp on failure paths
        if tmp_path is not None and tmp_path.exists():
            if target_path is None or not target_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
```

- [ ] **Step 4: Run the test**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py::test_fetch_and_write_one_happy_path -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/posters.py tests/unit/test_qflix_newsletter_posters.py
git commit -m "feat: _fetch_and_write_one happy path with content-type + magic-byte + size validation"
```

---

## Task 5: `_fetch_and_write_one` — failure modes

**Files:**
- Modify: `tests/unit/test_qflix_newsletter_posters.py`

The implementation from Task 4 already handles 4xx, non-image Content-Type, magic-byte mismatch, header size cap, stream size cap, and ConnectionError. We add tests to lock the behavior in.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_qflix_newsletter_posters.py`:

```python
def _err_response(status: int, content_type: str = "text/html"):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Type": content_type}
    resp.iter_content = MagicMock(return_value=iter([b""]))
    resp.raise_for_status = MagicMock()
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: None
    return resp


def test_fetch_and_write_one_4xx_returns_fail(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    session = MagicMock()
    session.get.return_value = _err_response(404)

    outcome, path = posters._fetch_and_write_one(
        "https://example/poster.jpg", cache_dir, "abc1234567890def",
        session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
    )
    assert outcome == "fail"
    assert path is None
    # No files written
    assert list(cache_dir.iterdir()) == []


def test_fetch_and_write_one_5xx_returns_retry(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    session = MagicMock()
    session.get.return_value = _err_response(503)

    outcome, path = posters._fetch_and_write_one(
        "https://example/poster.jpg", cache_dir, "abc1234567890def",
        session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
    )
    assert outcome == "retry"
    assert path is None


def test_fetch_and_write_one_rejects_non_image_content_type(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    session = MagicMock()
    session.get.return_value = _ok_response("text/html", b"<html></html>" + b"\x00" * 12)

    outcome, path = posters._fetch_and_write_one(
        "https://example/poster.jpg", cache_dir, "abc1234567890def",
        session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
    )
    assert outcome == "fail"
    assert list(cache_dir.iterdir()) == []


def test_fetch_and_write_one_rejects_magic_byte_mismatch(tmp_path):
    """Content-Type says PNG but body is HTML."""
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    session = MagicMock()
    session.get.return_value = _ok_response("image/png", b"<html><head" + b"\x00\x00")

    outcome, path = posters._fetch_and_write_one(
        "https://example/poster.jpg", cache_dir, "abc1234567890def",
        session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
    )
    assert outcome == "fail"
    # Neither the .png nor the .png.tmp should exist
    assert list(cache_dir.iterdir()) == []


def test_fetch_and_write_one_refuses_oversized_via_content_length(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    session = MagicMock()
    body = b"\xff\xd8\xff" + b"\x00" * 100
    session.get.return_value = _ok_response("image/jpeg", body, content_length=3_000_000)

    outcome, path = posters._fetch_and_write_one(
        "https://example/poster.jpg", cache_dir, "abc1234567890def",
        session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
    )
    assert outcome == "fail"
    # No file written (we refused before opening the .tmp)
    assert list(cache_dir.iterdir()) == []


def test_fetch_and_write_one_aborts_when_stream_exceeds_max_bytes(tmp_path):
    """No Content-Length header; the streamed body exceeds the cap."""
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    jpeg_chunks = [b"\xff\xd8\xff" + b"\x00" * 13] + [b"\x00" * 1_000_000 for _ in range(3)]

    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": "image/jpeg"}  # no Content-Length
    resp.iter_content = MagicMock(return_value=iter(jpeg_chunks))
    resp.raise_for_status = MagicMock()
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: None

    session = MagicMock()
    session.get.return_value = resp

    outcome, path = posters._fetch_and_write_one(
        "https://example/poster.jpg", cache_dir, "abc1234567890def",
        session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
    )
    assert outcome == "fail"
    # The .tmp file was being written but the finally block cleans it up
    assert list(cache_dir.iterdir()) == []


def test_fetch_and_write_one_handles_connection_error(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    import requests as _r
    session = MagicMock()
    session.get.side_effect = _r.ConnectionError("boom")

    outcome, path = posters._fetch_and_write_one(
        "https://example/poster.jpg", cache_dir, "abc1234567890def",
        session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
    )
    assert outcome == "retry"
    assert path is None
```

- [ ] **Step 2: Run the new tests**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py -v -k fetch_and_write_one
```

Expected: All pass — Task 4's implementation already covers them.

- [ ] **Step 3: Run the full poster test file**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py -v
```

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_qflix_newsletter_posters.py
git commit -m "test: cover _fetch_and_write_one failure modes (4xx, 5xx, content-type, magic bytes, size, conn err)"
```

---

## Task 6: `_try_one_source_with_retry` — retry wrapper

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/posters.py`
- Modify: `tests/unit/test_qflix_newsletter_posters.py`

Wraps `_fetch_and_write_one` with a single 1s-backoff retry on the `"retry"` outcome. No retry on `"fail"` — no point.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_qflix_newsletter_posters.py`:

```python
# ── _try_one_source_with_retry ──────────────────────────────────────────────

def test_retry_succeeds_after_5xx(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    jpeg = b"\xff\xd8\xff" + b"\x00" * 13

    session = MagicMock()
    session.get.side_effect = [
        _err_response(503),                   # first attempt fails
        _ok_response("image/jpeg", jpeg),     # retry succeeds
    ]

    with patch("qflix_newsletter.posters.time.sleep") as mock_sleep:
        outcome, path = posters._try_one_source_with_retry(
            "https://example/poster.jpg", cache_dir, "abc1234567890def",
            session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
        )

    assert outcome == "ok"
    assert path is not None and path.exists()
    assert session.get.call_count == 2
    mock_sleep.assert_called_once_with(1.0)


def test_retry_succeeds_after_connection_error(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    jpeg = b"\xff\xd8\xff" + b"\x00" * 13
    import requests as _r

    session = MagicMock()
    session.get.side_effect = [
        _r.ConnectionError("boom"),
        _ok_response("image/jpeg", jpeg),
    ]

    with patch("qflix_newsletter.posters.time.sleep"):
        outcome, path = posters._try_one_source_with_retry(
            "https://example/poster.jpg", cache_dir, "abc1234567890def",
            session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
        )

    assert outcome == "ok"
    assert session.get.call_count == 2


def test_no_retry_on_fail_outcome(tmp_path):
    """4xx returns 'fail', which should NOT be retried."""
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    session = MagicMock()
    session.get.return_value = _err_response(404)

    with patch("qflix_newsletter.posters.time.sleep") as mock_sleep:
        outcome, path = posters._try_one_source_with_retry(
            "https://example/poster.jpg", cache_dir, "abc1234567890def",
            session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
        )

    assert outcome == "fail"
    assert session.get.call_count == 1
    mock_sleep.assert_not_called()


def test_retry_gives_up_after_one_attempt(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    session = MagicMock()
    session.get.return_value = _err_response(503)

    with patch("qflix_newsletter.posters.time.sleep"):
        outcome, path = posters._try_one_source_with_retry(
            "https://example/poster.jpg", cache_dir, "abc1234567890def",
            session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
        )

    assert outcome == "retry"  # bubble up final outcome
    assert session.get.call_count == 2  # original + 1 retry
```

- [ ] **Step 2: Run failing tests**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py -v -k "retry or no_retry"
```

Expected: FAIL with `AttributeError: module 'qflix_newsletter.posters' has no attribute '_try_one_source_with_retry'`.

- [ ] **Step 3: Implement the retry wrapper**

In `scripts/qflix-newsletter/qflix_newsletter/posters.py`, add `import time` near the top, then introduce the wrapper:

```python
import time

_RETRY_BACKOFF_S = 1.0


def _try_one_source_with_retry(
    url: str,
    cache_dir: Path,
    sha: str,
    *,
    session: requests.Session,
    timeout_s: float,
    max_bytes: int,
) -> tuple[str, Optional[Path]]:
    """Run _fetch_and_write_one once; on 'retry' outcome, sleep and try once more.

    Final outcome is bubbled up unchanged — 'ok', 'fail', or 'retry'.
    """
    outcome, path = _fetch_and_write_one(
        url, cache_dir, sha,
        session=session, timeout_s=timeout_s, max_bytes=max_bytes,
    )
    if outcome == "retry":
        time.sleep(_RETRY_BACKOFF_S)
        outcome, path = _fetch_and_write_one(
            url, cache_dir, sha,
            session=session, timeout_s=timeout_s, max_bytes=max_bytes,
        )
    return outcome, path
```

- [ ] **Step 4: Run the tests**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/posters.py tests/unit/test_qflix_newsletter_posters.py
git commit -m "feat: _try_one_source_with_retry — single retry on transient outcomes"
```

---

## Task 7: `mirror_posters` — public entry point (TMDB only)

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/posters.py`
- Modify: `tests/unit/test_qflix_newsletter_posters.py`

The public function. This task implements the TMDB-only flow (no fallback yet) plus the cache-hit short-circuit. Task 8 adds the Tautulli fallback.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_qflix_newsletter_posters.py`:

```python
# ── mirror_posters ──────────────────────────────────────────────────────────

from qflix_newsletter.sources import RecentItem


def _item(title="Test Movie", thumb="https://image.tmdb.org/t/p/w342/abc.jpg",
          tautulli="https://seedbox.example.com/tautulli/pms_image_proxy?img=x") -> RecentItem:
    return RecentItem(
        media_type="movie",
        title=title,
        year=2025,
        summary="",
        thumb_url=thumb,
        tautulli_thumb_url=tautulli,
        added_at=0,
        rating=None,
    )


def test_mirror_posters_happy_path_rewrites_url(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    jpeg = b"\xff\xd8\xff" + b"\x00" * 13

    session = MagicMock()
    session.get.return_value = _ok_response("image/jpeg", jpeg)

    items = [_item(thumb="https://image.tmdb.org/t/p/w342/abc.jpg")]
    expected_sha = posters._sha_for("https://image.tmdb.org/t/p/w342/abc.jpg")

    out = posters.mirror_posters(
        items,
        cache_dir=cache_dir,
        public_base="https://seedbox.example.com",
        session=session,
    )

    assert out is items  # mutated in place + returned
    assert items[0].thumb_url == f"https://seedbox.example.com/images/newsletter/{expected_sha}.jpg"
    assert (cache_dir / f"{expected_sha}.jpg").exists()


def test_mirror_posters_cache_hit_skips_network(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    url = "https://image.tmdb.org/t/p/w342/abc.jpg"
    sha = posters._sha_for(url)
    # Pre-seed the cache.
    (cache_dir / f"{sha}.jpg").write_bytes(b"\xff\xd8\xff\x00")

    session = MagicMock()
    session.get.side_effect = AssertionError("network should not be touched on cache hit")

    items = [_item(thumb=url)]
    posters.mirror_posters(
        items, cache_dir=cache_dir,
        public_base="https://seedbox.example.com", session=session,
    )

    assert items[0].thumb_url == f"https://seedbox.example.com/images/newsletter/{sha}.jpg"


def test_mirror_posters_skips_none_thumb(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    session = MagicMock()

    items = [_item(thumb=None, tautulli=None)]
    posters.mirror_posters(
        items, cache_dir=cache_dir,
        public_base="https://seedbox.example.com", session=session,
    )

    assert items[0].thumb_url is None
    session.get.assert_not_called()


def test_mirror_posters_creates_cache_dir_if_missing(tmp_path):
    cache_dir = tmp_path / "does" / "not" / "exist"
    jpeg = b"\xff\xd8\xff" + b"\x00" * 13

    session = MagicMock()
    session.get.return_value = _ok_response("image/jpeg", jpeg)

    items = [_item()]
    posters.mirror_posters(
        items, cache_dir=cache_dir,
        public_base="https://seedbox.example.com", session=session,
    )

    assert cache_dir.is_dir()
    assert items[0].thumb_url and "/images/newsletter/" in items[0].thumb_url


def test_mirror_posters_strips_trailing_slash_in_public_base(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    jpeg = b"\xff\xd8\xff" + b"\x00" * 13
    session = MagicMock()
    session.get.return_value = _ok_response("image/jpeg", jpeg)

    items = [_item()]
    posters.mirror_posters(
        items, cache_dir=cache_dir,
        public_base="https://seedbox.example.com/",  # trailing slash
        session=session,
    )

    # No double-slash in the URL
    assert items[0].thumb_url is not None
    assert "example.com//" not in items[0].thumb_url
    assert items[0].thumb_url.startswith("https://seedbox.example.com/images/newsletter/")
```

- [ ] **Step 2: Run failing tests**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py -v -k mirror_posters
```

Expected: FAIL with `AttributeError: module 'qflix_newsletter.posters' has no attribute 'mirror_posters'`.

- [ ] **Step 3: Implement `mirror_posters` (TMDB-only, with cache hit)**

Append to `scripts/qflix-newsletter/qflix_newsletter/posters.py`:

```python
from typing import Sequence
from .sources import RecentItem

_KNOWN_EXTS = ("jpg", "png", "webp", "gif")
_NEWSLETTER_URL_PATH = "/images/newsletter/"


def _cache_lookup(cache_dir: Path, sha: str) -> Optional[Path]:
    """Return an already-cached file for this sha, if one exists."""
    for ext in _KNOWN_EXTS:
        p = cache_dir / f"{sha}.{ext}"
        if p.exists():
            return p
    return None


def _public_url(public_base: str, sha: str, ext: str) -> str:
    return f"{public_base.rstrip('/')}{_NEWSLETTER_URL_PATH}{sha}.{ext}"


def mirror_posters(
    items: Sequence[RecentItem],
    *,
    cache_dir: Path,
    public_base: str,
    session: Optional[requests.Session] = None,
    timeout_s: float = 10.0,
    max_bytes: int = 2 * 1024 * 1024,
) -> Sequence[RecentItem]:
    """Mirror each item's poster to cache_dir and rewrite item.thumb_url.

    Failures cascade to thumb_url=None (template hides the <img>).
    Returns the (mutated) items for chainability.

    Tautulli fallback is added in Task 8.
    """
    if session is None:
        session = requests.Session()
    cache_dir.mkdir(parents=True, exist_ok=True)

    counts = {"tmdb_hit": 0, "tautulli_fallback": 0, "dead": 0, "cached": 0}

    for item in items:
        if item.thumb_url is None:
            continue

        sha = _sha_for(item.thumb_url)
        cached = _cache_lookup(cache_dir, sha)
        if cached is not None:
            item.thumb_url = _public_url(public_base, sha, cached.suffix.lstrip("."))
            counts["cached"] += 1
            continue

        outcome, path = _try_one_source_with_retry(
            item.thumb_url, cache_dir, sha,
            session=session, timeout_s=timeout_s, max_bytes=max_bytes,
        )
        if outcome == "ok" and path is not None:
            counts["tmdb_hit"] += 1
            item.thumb_url = _public_url(public_base, sha, path.suffix.lstrip("."))
            continue

        # Both source-failure outcomes ('fail' and final 'retry') drop to dead.
        # Task 8 inserts the Tautulli fallback before this point.
        item.thumb_url = None
        counts["dead"] += 1

    log.info(
        "mirror_posters: tmdb_hit=%d tautulli_fallback=%d dead=%d cached=%d",
        counts["tmdb_hit"], counts["tautulli_fallback"], counts["dead"], counts["cached"],
    )
    return items
```

- [ ] **Step 4: Run all poster tests**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/posters.py tests/unit/test_qflix_newsletter_posters.py
git commit -m "feat: mirror_posters TMDB-only with cache hit + URL rewrite"
```

---

## Task 8: `mirror_posters` — Tautulli fallback chain

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/posters.py`
- Modify: `tests/unit/test_qflix_newsletter_posters.py`

When TMDB fails (both `"fail"` and exhausted-retry `"retry"`), try `tautulli_thumb_url`. The SHA is computed over the URL that succeeded — so a Tautulli-sourced poster has a different filename than a TMDB-sourced one for the same content. Log a warning when both sources die.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_qflix_newsletter_posters.py`:

```python
def test_mirror_posters_falls_back_to_tautulli_on_tmdb_404(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    jpeg = b"\xff\xd8\xff" + b"\x00" * 13

    session = MagicMock()
    session.get.side_effect = [
        _err_response(404),                   # TMDB 404
        _ok_response("image/jpeg", jpeg),     # Tautulli succeeds
    ]

    tautulli_url = "https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/x"
    tmdb_url = "https://image.tmdb.org/t/p/w342/abc.jpg"
    expected_sha = posters._sha_for(tautulli_url)  # SHA over successful source

    items = [_item(thumb=tmdb_url, tautulli=tautulli_url)]
    posters.mirror_posters(
        items, cache_dir=cache_dir,
        public_base="https://seedbox.example.com", session=session,
    )

    assert items[0].thumb_url == f"https://seedbox.example.com/images/newsletter/{expected_sha}.jpg"
    assert (cache_dir / f"{expected_sha}.jpg").exists()
    assert session.get.call_count == 2


def test_mirror_posters_both_dead_sets_none(tmp_path, caplog):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()

    session = MagicMock()
    session.get.side_effect = [
        _err_response(404),  # TMDB 404
        _err_response(404),  # Tautulli 404
    ]

    items = [_item(title="Borked Title")]
    with caplog.at_level("WARNING", logger="qflix_newsletter.posters"):
        posters.mirror_posters(
            items, cache_dir=cache_dir,
            public_base="https://seedbox.example.com", session=session,
        )

    assert items[0].thumb_url is None
    assert any("Borked Title" in r.message for r in caplog.records)


def test_mirror_posters_summary_log_counts_tautulli_fallback(tmp_path, caplog):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    jpeg = b"\xff\xd8\xff" + b"\x00" * 13

    session = MagicMock()
    session.get.side_effect = [
        _err_response(404),
        _ok_response("image/jpeg", jpeg),
    ]

    items = [_item()]
    with caplog.at_level("INFO", logger="qflix_newsletter.posters"):
        posters.mirror_posters(
            items, cache_dir=cache_dir,
            public_base="https://seedbox.example.com", session=session,
        )

    summary = [r.message for r in caplog.records if "mirror_posters:" in r.message]
    assert summary
    assert "tautulli_fallback=1" in summary[-1]


def test_mirror_posters_skips_tautulli_if_none(tmp_path):
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    session = MagicMock()
    session.get.return_value = _err_response(404)

    items = [_item(tautulli=None)]
    posters.mirror_posters(
        items, cache_dir=cache_dir,
        public_base="https://seedbox.example.com", session=session,
    )

    assert items[0].thumb_url is None
    assert session.get.call_count == 1  # only TMDB attempted


def test_mirror_posters_tautulli_cache_hit(tmp_path):
    """Tautulli-keyed cache hit should also short-circuit network."""
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    tmdb_url = "https://image.tmdb.org/t/p/w342/abc.jpg"
    tautulli_url = "https://seedbox.example.com/tautulli/pms_image_proxy?img=/library/x"
    t_sha = posters._sha_for(tautulli_url)
    (cache_dir / f"{t_sha}.jpg").write_bytes(b"\xff\xd8\xff\x00")

    session = MagicMock()
    session.get.return_value = _err_response(404)  # TMDB still 404, but Tautulli is cached

    items = [_item(thumb=tmdb_url, tautulli=tautulli_url)]
    posters.mirror_posters(
        items, cache_dir=cache_dir,
        public_base="https://seedbox.example.com", session=session,
    )

    assert items[0].thumb_url == f"https://seedbox.example.com/images/newsletter/{t_sha}.jpg"
    # TMDB was attempted; Tautulli was a cache hit (no second GET)
    assert session.get.call_count == 1
```

- [ ] **Step 2: Run failing tests**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py -v -k "tautulli or both_dead or summary_log"
```

Expected: FAIL — current `mirror_posters` has no Tautulli fallback.

- [ ] **Step 3: Add the fallback to `mirror_posters`**

In `scripts/qflix-newsletter/qflix_newsletter/posters.py`, replace the body of `mirror_posters` with the version that handles Tautulli fallback:

```python
def mirror_posters(
    items: Sequence[RecentItem],
    *,
    cache_dir: Path,
    public_base: str,
    session: Optional[requests.Session] = None,
    timeout_s: float = 10.0,
    max_bytes: int = 2 * 1024 * 1024,
) -> Sequence[RecentItem]:
    """Mirror each item's poster to cache_dir and rewrite item.thumb_url.

    Tries item.thumb_url (TMDB after enrich) first, then
    item.tautulli_thumb_url. Both failures cascade to thumb_url=None.

    Logs one summary line per call:
      mirror_posters: tmdb_hit=X tautulli_fallback=Y dead=Z cached=W
    """
    if session is None:
        session = requests.Session()
    cache_dir.mkdir(parents=True, exist_ok=True)

    counts = {"tmdb_hit": 0, "tautulli_fallback": 0, "dead": 0, "cached": 0}

    for item in items:
        if item.thumb_url is None:
            continue

        # ── Source 1: current thumb_url (TMDB after enrich) ──────────────
        sha = _sha_for(item.thumb_url)
        cached = _cache_lookup(cache_dir, sha)
        if cached is not None:
            item.thumb_url = _public_url(public_base, sha, cached.suffix.lstrip("."))
            counts["cached"] += 1
            continue

        outcome, path = _try_one_source_with_retry(
            item.thumb_url, cache_dir, sha,
            session=session, timeout_s=timeout_s, max_bytes=max_bytes,
        )
        if outcome == "ok" and path is not None:
            counts["tmdb_hit"] += 1
            item.thumb_url = _public_url(public_base, sha, path.suffix.lstrip("."))
            continue

        # ── Source 2: tautulli_thumb_url ─────────────────────────────────
        if item.tautulli_thumb_url:
            t_sha = _sha_for(item.tautulli_thumb_url)
            t_cached = _cache_lookup(cache_dir, t_sha)
            if t_cached is not None:
                item.thumb_url = _public_url(public_base, t_sha, t_cached.suffix.lstrip("."))
                counts["cached"] += 1
                continue

            outcome, path = _try_one_source_with_retry(
                item.tautulli_thumb_url, cache_dir, t_sha,
                session=session, timeout_s=timeout_s, max_bytes=max_bytes,
            )
            if outcome == "ok" and path is not None:
                counts["tautulli_fallback"] += 1
                item.thumb_url = _public_url(public_base, t_sha, path.suffix.lstrip("."))
                continue

        # Both sources dead.
        log.warning("mirror_posters: both sources dead for %r", item.title)
        item.thumb_url = None
        counts["dead"] += 1

    log.info(
        "mirror_posters: tmdb_hit=%d tautulli_fallback=%d dead=%d cached=%d",
        counts["tmdb_hit"], counts["tautulli_fallback"], counts["dead"], counts["cached"],
    )
    return items
```

- [ ] **Step 4: Run all poster tests**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_posters.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/posters.py tests/unit/test_qflix_newsletter_posters.py
git commit -m "feat: mirror_posters Tautulli fallback chain + dead-source warning"
```

---

## Task 9: Wire `mirror_posters` into `main.py` + integration test

**Files:**
- Modify: `scripts/qflix-newsletter/qflix_newsletter/main.py`
- Modify: `tests/unit/test_qflix_newsletter_render.py`

The new call sits between `enrich_with_tmdb()` and `build_email_context()`.

- [ ] **Step 1: Write the integration test**

Append to `tests/unit/test_qflix_newsletter_render.py`. First make sure the `_movie()` helper is accessible at module level. Search for `def _movie(` — if it's nested inside another function, lift it to module scope. If it's already module-level, no change needed.

Add this test:

```python
import re
from unittest.mock import MagicMock, patch

from qflix_newsletter import main as nl_main


def test_render_pipeline_rewrites_images_to_local_cache(tmp_path):
    """Full pipeline dry-run: every <img> src that should be a poster
    must point at the local /images/newsletter/ path."""
    out_html = tmp_path / "out.html"
    cache_dir = tmp_path / "cache"

    # Stub secrets — the bare minimum for Config.from_env() to succeed.
    secrets = tmp_path / "secrets"; secrets.mkdir()
    for n, v in {
        "tautulli.key": "tk", "tautulli.port": "42000",
        "sonarr.key": "sk", "sonarr.port": "42010", "sonarr.urlbase": "sonarr",
        "radarr.key": "rk", "radarr.port": "42011", "radarr.urlbase": "radarr",
        "listmonk.api_user": "u", "listmonk.api_token": "tok", "listmonk.port": "42014",
        "seedbox.host": "seedbox.example.com",
    }.items():
        (secrets / n).write_text(v)

    jpeg = b"\xff\xd8\xff" + b"\x00" * 13

    with patch("qflix_newsletter.main.fetch_recently_added", return_value=[
        _movie("Bugonia", thumb="https://image.tmdb.org/t/p/w342/bugonia.jpg"),
    ]), patch("qflix_newsletter.main.enrich_with_tmdb", side_effect=lambda cfg, items: items), \
         patch("qflix_newsletter.main.fetch_all_calendars", return_value=[]), \
         patch("qflix_newsletter.main.fetch_libraries_table", return_value=[]), \
         patch("qflix_newsletter.main.fetch_ai_picks", return_value=[]), \
         patch("qflix_newsletter.posters.requests.Session") as mock_sess_cls, \
         patch.dict("os.environ", {"QFLIX_POSTER_CACHE_DIR": str(cache_dir)}):

        sess = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "image/jpeg", "Content-Length": str(len(jpeg))}
        resp.iter_content = MagicMock(return_value=iter([jpeg]))
        resp.__enter__ = lambda self: self
        resp.__exit__ = lambda self, *a: None
        sess.get.return_value = resp
        mock_sess_cls.return_value = sess

        rc = nl_main.run(dry_run=True, out_html=out_html, secrets_dir=secrets)

    assert rc == 0
    html = out_html.read_text(encoding="utf-8")
    imgs = re.findall(r'<img[^>]+src="([^"]+)"', html)
    assert imgs, "expected at least one <img> in rendered HTML"
    # Filter to just the newsletter posters — the template may also include
    # the Q.png brand logo, which we do NOT mirror.
    poster_srcs = [s for s in imgs if "/images/newsletter/" in s]
    assert poster_srcs, "expected at least one mirrored poster src"
    for src in poster_srcs:
        assert re.search(r"/images/newsletter/[0-9a-f]{16}\.(jpg|png|webp|gif)$", src), \
            f"unexpected poster src format: {src}"
```

- [ ] **Step 2: Run failing test**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_render.py::test_render_pipeline_rewrites_images_to_local_cache -v
```

Expected: FAIL — `mirror_posters` not yet called from `main.py`.

- [ ] **Step 3: Wire `mirror_posters` into `main.py`**

In `scripts/qflix-newsletter/qflix_newsletter/main.py`, add the import:

```python
from .posters import mirror_posters
```

In `run()`, insert the call between TMDB enrichment and the calendar fetch:

```python
    recent = fetch_recently_added(cfg, count=DEFAULT_RECENT_COUNT)
    recent = enrich_with_tmdb(cfg, recent)
    recent = mirror_posters(
        recent,
        cache_dir=cfg.poster_cache_dir,
        public_base=f"https://{cfg.public_host}",
    )
    coming = fetch_all_calendars(cfg, days=DEFAULT_CALENDAR_DAYS)
    # ... rest unchanged ...
```

- [ ] **Step 4: Run the integration test**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_render.py::test_render_pipeline_rewrites_images_to_local_cache -v
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/test_qflix_newsletter_*.py -v
```

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/qflix-newsletter/qflix_newsletter/main.py tests/unit/test_qflix_newsletter_render.py
git commit -m "feat: wire mirror_posters into newsletter render pipeline"
```

---

## Task 10: systemd timer + service for daily prune

**Files:**
- Create: `scripts/maint/systemd/qflix-poster-cache-prune.timer`
- Create: `scripts/maint/systemd/qflix-poster-cache-prune.service`

Static configuration files. No unit test — covered by the configure script smoke test in Task 11.

- [ ] **Step 1: Create the timer**

`scripts/maint/systemd/qflix-poster-cache-prune.timer`:

```ini
[Unit]
Description=QFlix newsletter poster cache 30-day prune

[Timer]
# Daily at 00:00 UTC; RandomizedDelaySec spreads load if other timers
# fire at the same time. Persistent=true catches up after seedbox reboots.
OnCalendar=*-*-* 00:00:00
RandomizedDelaySec=300
Persistent=true
Unit=qflix-poster-cache-prune.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 2: Create the service**

`scripts/maint/systemd/qflix-poster-cache-prune.service`:

```ini
[Unit]
Description=QFlix newsletter poster cache 30-day prune

[Service]
# `%h` expands to $HOME under user systemd. -mtime +30 deletes files
# whose mtime is older than 30 days (so a freshly cached poster lives
# exactly 30 days from write).
Type=oneshot
ExecStart=/usr/bin/find %h/www/images/newsletter -type f -mtime +30 -delete
ExecStart=/usr/bin/find %h/www/images/newsletter -type f -name "*.tmp" -mmin +60 -delete
```

The second `ExecStart` cleans up any orphan `.tmp` files older than 1 hour as a belt-and-braces measure (per the spec's Open Decisions).

- [ ] **Step 3: Sanity check the unit files parse (if Linux available)**

```bash
systemd-analyze verify scripts/maint/systemd/qflix-poster-cache-prune.timer
systemd-analyze verify scripts/maint/systemd/qflix-poster-cache-prune.service
```

If not on Linux, skip — `systemctl --user daemon-reload` in Task 11 will surface any errors.

- [ ] **Step 4: Commit**

```bash
git add scripts/maint/systemd/qflix-poster-cache-prune.timer scripts/maint/systemd/qflix-poster-cache-prune.service
git commit -m "feat: systemd timer for daily 30-day poster cache prune"
```

---

## Task 11: Configure script `49a-newsletter-poster-cache.sh`

**Files:**
- Create: `scripts/configure/49a-newsletter-poster-cache.sh`

Idempotent deploy: directory + systemd units + enable + smoke.

- [ ] **Step 1: Create the configure script**

`scripts/configure/49a-newsletter-poster-cache.sh`:

```bash
#!/usr/bin/env bash
# Phase 24a — qflix newsletter poster cache + daily prune timer. Idempotent.
#
# Stands up:
#   ~/www/images/newsletter/                 (mode 0755, served by /images/)
#   ~/.config/systemd/user/qflix-poster-cache-prune.{service,timer}
#
# Smoke-tests:
#   - cache dir is writable + served at https://<public_host>/images/newsletter/
#   - timer is enabled and active
#
# Depends on:
#   - scripts/configure/60-www-images.sh (provides the /images/ nginx route)
#   - scripts/configure/49-qflix-newsletter-install.sh (provides qflix-newsletter)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# ── Step 1: create the cache dir ────────────────────────────────────────────
log_info "creating ~/www/images/newsletter/"
sshm 'mkdir -p ~/www/images/newsletter && chmod 755 ~/www/images/newsletter'

# ── Step 2: deploy systemd units ────────────────────────────────────────────
log_info "deploying prune timer + service"
scpm_to "$REPO_ROOT/scripts/maint/systemd/qflix-poster-cache-prune.timer"   '~/.config/systemd/user/qflix-poster-cache-prune.timer'   >/dev/null
scpm_to "$REPO_ROOT/scripts/maint/systemd/qflix-poster-cache-prune.service" '~/.config/systemd/user/qflix-poster-cache-prune.service' >/dev/null
sshm 'systemctl --user daemon-reload'
sshm 'systemctl --user enable --now qflix-poster-cache-prune.timer'

# ── Step 3: smoke — write a probe and serve it ──────────────────────────────
log_info "smoke test: write probe + serve via nginx"
PUB_HOST=$(cat "$REPO_ROOT/secrets/seedbox.host" 2>/dev/null || echo "quadstronaut.seedbox.example.com")

# Recognizable 16-char hex probe filename matching the SHA pattern.
PROBE_SHA="deadbeefcafef00d"
sshm "printf '\xff\xd8\xff\xe0\x00\x00\x00\x00\x00\x00\x00\x00\x00' > ~/www/images/newsletter/${PROBE_SHA}.jpg"

HTTP=$(curl -s -o /dev/null -w '%{http_code}' "https://$PUB_HOST/images/newsletter/${PROBE_SHA}.jpg")
if [ "$HTTP" != "200" ]; then
  echo "FAIL: probe expected 200, got $HTTP" >&2
  exit 1
fi
echo "  PASS: GET /images/newsletter/${PROBE_SHA}.jpg → 200"

# Cache-Control immutable (inherited from /images/ config).
if ! curl -sI "https://$PUB_HOST/images/newsletter/${PROBE_SHA}.jpg" | grep -qi 'cache-control:.*immutable'; then
  echo "FAIL: Cache-Control immutable not present on probe" >&2
  exit 1
fi
echo "  PASS: Cache-Control immutable present"

# Clean up probe.
sshm "rm -f ~/www/images/newsletter/${PROBE_SHA}.jpg"

# ── Step 4: verify timer is enabled + active ────────────────────────────────
log_info "verifying timer state"
sshm 'systemctl --user list-timers --no-pager | grep -q qflix-poster-cache-prune || (echo "FAIL: timer not in list-timers" >&2 ; exit 1)'
echo "  PASS: timer is loaded"

log_info "Phase 24a complete — poster cache armed; next prune fires at 00:00 UTC"
log_info "Manual prune: ssh quadstronaut@seedbox 'systemctl --user start qflix-poster-cache-prune.service'"
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/configure/49a-newsletter-poster-cache.sh
```

- [ ] **Step 3: Deploy and verify (if seedbox access available)**

```bash
./scripts/configure/49a-newsletter-poster-cache.sh
```

Expected output ends with `Phase 24a complete`. All smoke checks PASS.

If no seedbox access, defer to manual run during deployment.

- [ ] **Step 4: Commit**

```bash
git add scripts/configure/49a-newsletter-poster-cache.sh
git commit -m "feat: 49a-newsletter-poster-cache.sh configure script"
```

---

## Task 12: Update `inventory.md`

**Files:**
- Modify: `inventory.md`

Document the new artifacts so the live inventory stays the source of truth.

- [ ] **Step 1: Find the right section**

Open `inventory.md` and search for `qflix-newsletter`. Identify where the newsletter timer is listed and where directories are documented.

- [ ] **Step 2: Add entries for the new artifacts**

Under the qflix-newsletter section, add (placement following the existing pattern):

```markdown
- **Poster cache:** `~/www/images/newsletter/` — served at
  `https://<public_host>/images/newsletter/<sha>.<ext>`. Mirrored at
  render time by `qflix_newsletter.posters.mirror_posters()`. Pruned
  daily by `qflix-poster-cache-prune.timer`.
- **Daily prune timer:** `qflix-poster-cache-prune.timer` (user-systemd)
  — fires at 00:00 UTC, deletes files older than 30 days. Probe via
  `lib/health.py systemd_oneshot`.
```

Match the exact phrasing/indentation of surrounding entries.

- [ ] **Step 3: Commit**

```bash
git add inventory.md
git commit -m "docs: inventory entries for poster cache + prune timer"
```

---

## Task 13: Final integration verification

**Files:** none modified — verification only.

- [ ] **Step 1: Run the full test suite**

```bash
cd scripts/qflix-newsletter
.venv/bin/pytest ../../tests/unit/ -v
```

Expected: All pass (including pre-existing tests).

- [ ] **Step 2: Run a real dry-run on the seedbox**

```bash
ssh quadstronaut@seedbox 'cd ~/.apps/qflix-newsletter && .venv/bin/python -m qflix_newsletter --dry-run --out-html /tmp/qflix-poster-mirror-smoke.html --verbose 2>&1 | tail -30'
```

Inspect the tail output for:

- `mirror_posters: tmdb_hit=N tautulli_fallback=M dead=Z cached=W` summary with non-zero `tmdb_hit`.
- No tracebacks.

Then check the rendered HTML:

```bash
ssh quadstronaut@seedbox 'grep -oE "src=\"[^\"]+/images/newsletter/[0-9a-f]{16}\\.(jpg|png|webp|gif)\"" /tmp/qflix-poster-mirror-smoke.html | head -5'
```

Expected: at least 5 results showing `src="https://<public_host>/images/newsletter/<sha>.<ext>"`.

- [ ] **Step 3: Curl one mirrored URL to confirm 200**

```bash
PUB_HOST=$(cat secrets/seedbox.host)
ONE=$(ssh quadstronaut@seedbox 'ls ~/www/images/newsletter | head -1')
curl -sI "https://${PUB_HOST}/images/newsletter/${ONE}" | head -5
```

Expected: `HTTP/2 200`, `Content-Type: image/jpeg` (or similar), `Cache-Control: ... immutable`.

- [ ] **Step 4: No commit — verification only**

If everything passed, the feature is shipped. Watch the next Monday 08:00 Phoenix send for the actual delivered newsletter; click through a mirrored poster URL to confirm.

---

## Acceptance criteria (recap from spec)

- [ ] 100% of `<img>` `src` in the rendered HTML resolve to `https://<public_host>/images/newsletter/...` (modulo the Q.png brand logo)
- [ ] `curl -I` on any mirrored URL returns 200 + `Cache-Control: ... immutable`
- [ ] Pipeline does not fail when TMDB + Tautulli are both unreachable (graceful degradation to `thumb_url=None`)
- [ ] 31 days after first send, `find ~/www/images/newsletter -type f -mtime +30` returns zero
