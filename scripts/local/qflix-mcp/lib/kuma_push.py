"""Kuma push-monitor wrapper. Reads token map from secrets/kuma-push-tokens.json,
host from secrets/uptimekuma.host (falls back to hardcoded 'kuma.<seedbox-host>').

Module is named `kuma_push` (not `kuma`) to avoid colliding with
`scripts/maint/lib/kuma.py` in the shared namespace package."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except FileNotFoundError:
        return ""


def _resolve_secrets_dir(override: Optional[Path]) -> Path:
    if override:
        return override
    env = os.environ.get("QFLIX_SECRETS_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "secrets" / "kuma-push-tokens.json").exists():
            return parent / "secrets"
    return Path.home() / ".qflix" / "secrets"


def push_up(monitor_name: str, *, msg: str = "OK",
            ping_ms: int = 0, secrets_dir: Optional[Path] = None) -> bool:
    secrets = _resolve_secrets_dir(secrets_dir)
    try:
        tokens = json.loads(_read(secrets / "kuma-push-tokens.json") or "{}")
    except json.JSONDecodeError:
        return False
    token = tokens.get(monitor_name)
    if not token:
        return False
    host = _read(secrets / "uptimekuma.host") or "kuma.seedbox.example.com"
    url = f"https://{host}/api/push/{token}?" + urllib.parse.urlencode({
        "status": "up", "msg": msg, "ping": ping_ms,
    })
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError:
        return False
