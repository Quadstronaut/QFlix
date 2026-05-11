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
