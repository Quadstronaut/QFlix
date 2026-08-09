"""Discord webhook poster. No @-pings. Reads webhook URL from secrets/discord-webhook.url."""
from __future__ import annotations

import json
import os
import urllib.error
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
        c = parent / "secrets" / "discord-webhook.url"
        if c.exists():
            return parent / "secrets"
    return Path.home() / ".qflix" / "secrets"


def post(message: str, *, secrets_dir: Optional[Path] = None,
         title: str = "QFlix MCP", color: int = 0x3498DB) -> bool:
    secrets = _resolve_secrets_dir(secrets_dir)
    webhook = _read(secrets / "discord-webhook.url")
    if not webhook:
        return False
    payload = {
        "embeds": [{
            "title": title,
            "description": message[:4000],
            "color": color,
        }],
        # Critical: no `content`, no `allowed_mentions` — guarantees no @-ping.
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(webhook, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError:
        return False
