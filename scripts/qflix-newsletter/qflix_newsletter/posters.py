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
