"""Read ~/secrets/* and assemble runtime endpoint config."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _secrets_dir() -> Path:
    override = os.environ.get("QFLIX_SECRETS_DIR")
    if override:
        return Path(override)
    return Path.home() / "secrets"


def read_secret(name: str, secrets_dir: Optional[Path] = None) -> str:
    p = (secrets_dir or _secrets_dir()) / name
    return p.read_text(encoding="utf-8").strip()


def maybe_read_secret(name: str, secrets_dir: Optional[Path] = None) -> Optional[str]:
    p = (secrets_dir or _secrets_dir()) / name
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()


@dataclass
class ArrEndpoint:
    base_url: str
    api_key: str


@dataclass
class Config:
    tautulli_url: str
    tautulli_key: str

    sonarr: ArrEndpoint
    sonarr_anime: Optional[ArrEndpoint]
    radarr: ArrEndpoint
    radarr_anime: Optional[ArrEndpoint]

    tmdb_read_token: Optional[str]
    gemini_api_key: Optional[str]

    listmonk_base_url: str
    listmonk_api_user: str
    listmonk_api_token: str
    listmonk_list_id: int
    listmonk_template_id: Optional[int]

    public_host: str
    poster_cache_dir: Path

    @classmethod
    def from_env(cls, secrets_dir: Optional[Path] = None) -> "Config":
        d = secrets_dir
        tautulli_port = read_secret("tautulli.port", d)
        sonarr_port = read_secret("sonarr.port", d)
        sonarr_urlbase = read_secret("sonarr.urlbase", d)
        radarr_port = read_secret("radarr.port", d)
        radarr_urlbase = read_secret("radarr.urlbase", d)

        sonarr2_port = maybe_read_secret("sonarr2.port", d)
        sonarr2_urlbase = maybe_read_secret("sonarr2.urlbase", d)
        sonarr2_key = maybe_read_secret("sonarr2.key", d)
        radarr2_port = maybe_read_secret("radarr2.port", d)
        radarr2_urlbase = maybe_read_secret("radarr2.urlbase", d)
        radarr2_key = maybe_read_secret("radarr2.key", d)

        listmonk_port = read_secret("listmonk.port", d)
        listmonk_list_id_raw = maybe_read_secret("listmonk.list_id", d) or "1"

        poster_cache_override = os.environ.get("QFLIX_POSTER_CACHE_DIR")
        if poster_cache_override:
            poster_cache_dir = Path(poster_cache_override)
        else:
            poster_cache_dir = Path.home() / "www" / "images" / "newsletter"

        return cls(
            tautulli_url=f"http://127.0.0.1:{tautulli_port}/tautulli",
            tautulli_key=read_secret("tautulli.key", d),
            sonarr=ArrEndpoint(
                base_url=f"http://127.0.0.1:{sonarr_port}/{sonarr_urlbase}",
                api_key=read_secret("sonarr.key", d),
            ),
            sonarr_anime=(
                ArrEndpoint(
                    base_url=f"http://127.0.0.1:{sonarr2_port}/{sonarr2_urlbase}",
                    api_key=sonarr2_key,
                )
                if sonarr2_port and sonarr2_urlbase and sonarr2_key
                else None
            ),
            radarr=ArrEndpoint(
                base_url=f"http://127.0.0.1:{radarr_port}/{radarr_urlbase}",
                api_key=read_secret("radarr.key", d),
            ),
            radarr_anime=(
                ArrEndpoint(
                    base_url=f"http://127.0.0.1:{radarr2_port}/{radarr2_urlbase}",
                    api_key=radarr2_key,
                )
                if radarr2_port and radarr2_urlbase and radarr2_key
                else None
            ),
            tmdb_read_token=maybe_read_secret("tmdb.read_token", d),
            gemini_api_key=maybe_read_secret("gemini.api_key", d),
            listmonk_base_url=f"http://127.0.0.1:{listmonk_port}",
            listmonk_api_user=read_secret("listmonk.api_user", d),
            listmonk_api_token=read_secret("listmonk.api_token", d),
            listmonk_list_id=int(listmonk_list_id_raw),
            listmonk_template_id=(
                int(maybe_read_secret("listmonk.template_id", d))
                if maybe_read_secret("listmonk.template_id", d)
                else None
            ),
            public_host=(maybe_read_secret("seedbox.host", d) or "quadstronaut.seedbox.example.com"),
            poster_cache_dir=poster_cache_dir,
        )
