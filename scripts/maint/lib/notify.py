"""lib/notify.py — Discord webhook operator-ping (Notifiarr-free).

Reads webhook URL from secrets/discord-webhook.url. Notifiarr passthrough
was deprecated 2026-05-10 after multiple integration-disabled failures
on operator's Notifiarr account; direct Discord webhooks remove the
middleware with no functional loss for this stack's "ping operator on
auto-heal failure" use case.

Falls back to the legacy Notifiarr key if discord-webhook.url is absent
(eases gradual migration; will be removed when Notifiarr secret is
purged from the seedbox).

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

_NOTIFIARR_ENDPOINT = "https://notifiarr.com/api/v1/notification/passthrough/{key}"


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


def _post_notifiarr_legacy(message: str, level: str) -> tuple[bool, str]:
    """Legacy fallback if discord-webhook.url is absent. Will be removed
    after the Notifiarr secret is purged."""
    try:
        key = _secret_read("notifiarr.key")
    except Exception as exc:
        return False, f"no fallback: {exc}"

    color_map = {
        "info":    "3498db",
        "warning": "f39c12",
        "error":   "e74c3c",
        "critical": "8b0000",
    }
    payload = {
        "notification": {"name": "manitoba-maint", "event": "auto_heal"},
        "discord": {
            "color": color_map.get(level, "3498db"),
            "text": {"description": message[:1900]},
        },
    }
    url = _NOTIFIARR_ENDPOINT.format(key=key)
    try:
        resp = requests.post(url, json=payload, timeout=5)
        resp.raise_for_status()
        return True, ""
    except Exception as exc:
        return False, _redact_url(str(exc))


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
    """Send a notification to the operator's Discord. Tries the direct
    webhook first (preferred). Falls back to Notifiarr passthrough only if
    discord-webhook.url is absent, for migration safety."""
    webhook_url = _try_read_webhook_url()
    if webhook_url:
        ok, err = _post_discord(webhook_url, message, level)
        if ok:
            return True
        _append_fail_log(level, message, _redact_url(err))
        return False

    # Fallback path — Notifiarr passthrough. Will be removed in a follow-up
    # commit after the Notifiarr secret is purged from the seedbox.
    ok, err = _post_notifiarr_legacy(message, level)
    if ok:
        return True
    _append_fail_log(level, message, _redact_url(err))
    return False
