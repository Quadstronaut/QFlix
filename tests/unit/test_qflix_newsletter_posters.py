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
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
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


def _err_response(status: int, content_type: str = "text/html"):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Type": content_type}
    resp.iter_content = MagicMock(return_value=iter([b""]))
    resp.raise_for_status = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
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
    assert list(cache_dir.iterdir()) == []


def test_fetch_and_write_one_aborts_when_stream_exceeds_max_bytes(tmp_path):
    """No Content-Length header; the streamed body exceeds the cap."""
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    jpeg_chunks = [b"\xff\xd8\xff" + b"\x00" * 13] + [b"\x00" * 1_000_000 for _ in range(3)]

    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": "image/jpeg"}
    resp.iter_content = MagicMock(return_value=iter(jpeg_chunks))
    resp.raise_for_status = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)

    session = MagicMock()
    session.get.return_value = resp

    outcome, path = posters._fetch_and_write_one(
        "https://example/poster.jpg", cache_dir, "abc1234567890def",
        session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
    )
    assert outcome == "fail"
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


# ── Task 8: Tautulli fallback chain ─────────────────────────────────────────

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
    expected_sha = posters._sha_for(tautulli_url)

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


def test_fetch_and_write_one_oserror_during_write_cleans_up(tmp_path):
    """If f.write raises (e.g., disk full), no .jpg and no .tmp should remain."""
    cache_dir = tmp_path / "cache"; cache_dir.mkdir()
    jpeg = b"\xff\xd8\xff" + b"\x00" * 13
    session = MagicMock()
    session.get.return_value = _ok_response("image/jpeg", jpeg)
    sha = "abcdef0123456789"

    with patch("pathlib.Path.open", side_effect=OSError("disk full")):
        outcome, path = posters._fetch_and_write_one(
            "https://example/poster.jpg", cache_dir, sha,
            session=session, timeout_s=10.0, max_bytes=2 * 1024 * 1024,
        )

    assert outcome == "fail"
    assert not (cache_dir / f"{sha}.jpg").exists()
    assert not (cache_dir / f"{sha}.jpg.tmp").exists()


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
