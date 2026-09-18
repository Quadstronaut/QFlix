"""lib/notify.py — Discord webhook operator-ping.

Reads webhook URL from secrets/discord-webhook.url. Sends a Discord-shaped
embed for auto-heal failures and other operator alerts. If the webhook URL
is missing, fails loud (logs to notify-fail.log) — there is no fallback.

Notifiarr passthrough was removed 2026-05-10 after the secret was purged
from the seedbox. Earlier versions had a Notifiarr legacy fallback for
gradual migration; that path is gone now.

Audit trail: EVERY attempt — sent or failed — is recorded to
MANITOBA_STATE_DIR/notify.log (default ~/.opt/maint/) so there is a durable
record of what paged and when, independent of whether the caller has Python
logging wired (the flaresolverr canary uses print()+journal; the pusher uses
logging). The webhook token is redacted from the audit line.

On failure: ALSO logs to MANITOBA_STATE_DIR/notify-fail.log (failures only,
for back-compat). Never raises — all errors are swallowed after logging.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

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


# Levels that should ping the operator. Embeds alone don't trigger a Discord
# push notification — the user mention has to be in the `content` field.
_OPERATOR_MENTION_LEVELS = {"error", "critical"}


def _operator_id() -> Optional[str]:
    """Discord user ID to ping for operator-needed events. Numeric snowflake
    in secrets/discord-operator.id, or None if not configured."""
    path = _secrets_dir() / "discord-operator.id"
    try:
        val = path.read_text(encoding="utf-8").strip()
        if val.isdigit():
            return val
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
    if level in _OPERATOR_MENTION_LEVELS:
        op_id = _operator_id()
        if op_id:
            payload["content"] = f"<@{op_id}>"
            payload["allowed_mentions"] = {"users": [op_id]}
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

from lib.secrets import secrets_dir as _secrets_dir  # noqa: F401
from lib.secrets import read_secret as _secret_read  # noqa: F401


def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


# Cap notify-fail.log at this many lines. Without a cap, an extended
# Discord outage during a mass-failure event could grow the file
# unboundedly with no operator-visible alert. 5000 lines ~= 1 MB.
_NOTIFY_FAIL_LOG_MAX_LINES = 5000


def _flatten_field(value: object) -> str:
    """Make a value safe for a TAB-DELIMITED, ONE-LINE-PER-RECORD log.

    CWE-117. Both audit logs here are hand-formatted as
    ``ts <TAB> level <TAB> outcome <TAB> message``, and much of what reaches
    ``message`` is UNTRUSTED: *arr release titles are chosen by whoever
    uploaded the release to a public indexer and are authenticated nowhere.
    A title carrying a newline plus its own tab-separated fields writes EXTRA
    physical lines indistinguishable from genuine records -- a forged
    "critical / sent" row for a page that never fired, in the one file an
    operator would use to reconstruct what the automation actually did.
    Demonstrated against this exact code by the Stage-2 security lens on
    2026-09-17, reachable through the newly destructive --unstick park path.

    Newlines and tabs become their visible escapes rather than being dropped,
    so an injection attempt stays legible in the log instead of silently
    losing the attacker's text. Other C0/C1 control characters (which can
    reposition a terminal cursor or truncate a line in a pager) become
    U+FFFD. The result is always exactly one physical line.
    """
    out = []
    for ch in str(value):
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F:
            out.append("�")
        else:
            out.append(ch)
    return "".join(out)


def _clip_flat(text: str, limit: int) -> str:
    """Truncate flattened text without splitting an escape in half.

    Escaping turns one character into two, so a plain ``[:limit]`` can cut
    ``\\n`` between the backslash and the ``n`` and leave a dangling backslash
    that a log parser may read as escaping the delimiter that follows. Drop a
    trailing ODD run of backslashes; an even run is intact escaped backslashes
    and is left alone.
    """
    clipped = text[:limit]
    trailing = len(clipped) - len(clipped.rstrip("\\"))
    if trailing % 2:
        clipped = clipped[:-1]
    return clipped


def _append_fail_log(level: str, message: str, error: str) -> None:
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "notify-fail.log"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # Every field flattened: the delimiter and the record separator must come
    # from HERE, never from the payload.
    line = (f"{_flatten_field(now)}\t{_flatten_field(level)}\t"
            f"{_flatten_field(message)}\t{_flatten_field(error)}\n")
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        # Cheap rotation: every ~256 appends, check size and rewind to
        # the last N lines if over cap. Reading + rewriting on every
        # append would be expensive; the imprecise cap is fine — the
        # goal is "doesn't grow without bound", not byte-precision.
        if (hash(now) & 0xFF) == 0:
            try:
                lines = log_path.read_text(encoding="utf-8").splitlines(keepends=True)
                if len(lines) > _NOTIFY_FAIL_LOG_MAX_LINES:
                    trimmed = lines[-_NOTIFY_FAIL_LOG_MAX_LINES:]
                    log_path.write_text("".join(trimmed), encoding="utf-8")
            except Exception as _exc:
                sys.stderr.write("notify.py: fail-log rotation failed (best-effort, continuing): "
                                 + repr(_exc) + "\n")
    except Exception as exc:
        print(f"WARNING: could not write notify-fail.log: {exc}", file=sys.stderr)


# Cap notify.log (the full send-audit trail) the same way as notify-fail.log.
_NOTIFY_AUDIT_LOG_MAX_LINES = 5000


def _append_audit_log(level: str, message: str, outcome: str) -> None:
    """Record EVERY operator alert — sent or failed — to notify.log, so there
    is a durable trail of what paged and when.

    WHY this is needed on top of notify-fail.log: previously only *failed*
    sends were recorded; a successfully-delivered page (the common case) left
    no trace, and callers don't all have Python logging configured to journal
    (flaresolverr-canary.py uses print(); the pusher uses logging). The file
    audit is caller-independent. Best-effort; never raises."""
    state_dir = _state_dir()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        log_path = state_dir / "notify.log"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        # Single tab-delimited line; message truncated so an alert flood can't
        # bloat any one row.
        # Flatten every field, then truncate, so an escape sequence added by
        # the flattener can never be cut in half by the length cap.
        line = (f"{_flatten_field(now)}\t{_flatten_field(level)}\t"
                f"{_flatten_field(outcome)}\t"
                f"{_clip_flat(_flatten_field(message), 300)}\n")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        # Cheap, imprecise rotation — same approach as _append_fail_log.
        if (hash(now) & 0xFF) == 0:
            try:
                lines = log_path.read_text(encoding="utf-8").splitlines(keepends=True)
                if len(lines) > _NOTIFY_AUDIT_LOG_MAX_LINES:
                    log_path.write_text(
                        "".join(lines[-_NOTIFY_AUDIT_LOG_MAX_LINES:]),
                        encoding="utf-8")
            except Exception as _exc:
                sys.stderr.write("notify.py: audit-log rotation failed (best-effort, continuing): "
                                 + repr(_exc) + "\n")
    except Exception as exc:
        print(f"WARNING: could not write notify.log: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify(message: str, level: str = "info") -> bool:
    """Send a notification to the operator's Discord. Returns False (and
    logs to notify-fail.log) if the webhook URL is missing or the POST
    fails — never raises. Every attempt (sent or failed) is recorded to
    notify.log for an audit trail."""
    webhook_url = _try_read_webhook_url()
    if not webhook_url:
        _append_fail_log(level, message, "no webhook: secrets/discord-webhook.url missing")
        _append_audit_log(level, message, "failed: no webhook configured")
        log.warning("alert NOT sent (no webhook configured): [%s] %s", level, message)
        return False
    ok, err = _post_discord(webhook_url, message, level)
    if ok:
        _append_audit_log(level, message, "sent")
        log.info("alert sent: [%s] %s", level, message)
        return True
    redacted = _redact_url(err)
    _append_fail_log(level, message, redacted)
    _append_audit_log(level, message, f"failed: {redacted}")
    log.warning("alert send FAILED: [%s] %s (%s)", level, message, redacted)
    return False
