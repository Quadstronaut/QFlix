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
import os
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

_MAGIC_PREFIX_BYTES = 12
_DEFAULT_CHUNK_SIZE = 64 * 1024

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
                    pass

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
        if tmp_path is not None and tmp_path.exists():
            if target_path is None or not target_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


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
