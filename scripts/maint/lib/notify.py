"""lib/notify.py — Discord webhook operator-ping.

Reads webhook URL from secrets/discord-webhook.url. Sends a Discord-shaped
embed for auto-heal failures and other operator alerts. If the webhook URL
is missing, fails loud (logs to notify-fail.log) — there is no fallback.

Notifiarr passthrough was removed 2026-05-10 after the secret was purged
from the seedbox. Earlier versions had a Notifiarr legacy fallback for
gradual migration; that path is gone now.

On failure: logs to MANITOBA_STATE_DIR/notify-fail.log (default
~/.opt/maint/). Never raises — all errors are swallowed after logging.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Color map by level
# ---------------------------------------------------------------------------

# Discord webhook color codes are decimal ints (the hex<->int mapping below
# matches Discord's embed-color expectations).
_COLORS = {
    "info":    3498003,   # blue
    "warning": 15976736,  # warm orange (Qflix accent)
    "error":   15158332,  # red
    "critical": 9109504,  # dark red
}


def _redact_url(url: str) -> str:
    """Redact key/secret path segments so they don't land in failure logs."""
    if "/webhooks/" in url:
        # Discord webhook: https://discord.com/api/webhooks/<id>/<token>
        parts = url.split("/webhooks/", 1)
        return parts[0] + "/webhooks/<redacted>"
    parts = url.rsplit("/", 1)
    if len(parts) == 2 and len(parts[1]) >= 8:
        return f"{parts[0]}/<redacted-{len(parts[1])}-char-key>"
    return url


def _try_read_webhook_url() -> Optional[str]:
    """Return the operator's Discord webhook URL, or None if not configured."""
    path = _secrets_dir() / "discord-webhook.url"
    try:
        url = path.read_text(encoding="utf-8").strip()
        if url.startswith("https://"):
            return url
    except FileNotFoundError:
        pass
    return None


def _post_discord(webhook_url: str, message: str, level: str) -> tuple[bool, str]:
    """POST a Discord-shaped embed payload directly to a webhook URL.
    Returns (ok, error_msg). Never raises."""
    color = _COLORS.get(level, _COLORS["info"])
    payload = {
        "username": "manitoba-maint",
        "embeds": [{
            "title": f"[{level.upper()}]" if level != "info" else "Notification",
            "description": message[:4000],
            "color": color,
        }],
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=5)
        resp.raise_for_status()
        return True, ""
    except requests.Timeout as exc:
        return False, f"timeout: {exc}"
    except requests.ConnectionError as exc:
        return False, f"connection error: {exc}"
    except requests.HTTPError as exc:
        return False, f"http error: {exc}"
    except Exception as exc:
        return False, f"unexpected error: {exc}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _secrets_dir() -> Path:
    env = os.environ.get("MANITOBA_SECRETS_DIR")
    if env:
        return Path(env)
    repo_root = Path(__file__).parent.parent.parent.parent
    return repo_root / "secrets"


def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


def _secret_read(name: str) -> str:
    path = _secrets_dir() / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise FileNotFoundError(f"Secret not found: {path}")


def _append_fail_log(level: str, message: str, error: str) -> None:
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "notify-fail.log"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    line = f"{now}\t{level}\t{message}\t{error}\n"
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        print(f"WARNING: could not write notify-fail.log: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify(message: str, level: str = "info") -> bool:
    """Send a notification to the operator's Discord. Returns False (and
    logs to notify-fail.log) if the webhook URL is missing or the POST
    fails — never raises."""
    webhook_url = _try_read_webhook_url()
    if not webhook_url:
        _append_fail_log(level, message, "no webhook: secrets/discord-webhook.url missing")
        return False
    ok, err = _post_discord(webhook_url, message, level)
    if ok:
        return True
    _append_fail_log(level, message, _redact_url(err))
    return False
