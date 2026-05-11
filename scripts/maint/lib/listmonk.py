"""lib/listmonk.py — fire a saved Listmonk template as a one-shot campaign.

Used by lib/window.py to email the subscriber list at maintenance-window
open + close. Templates are pre-uploaded by `qflix_newsletter.sync` — this
module just looks one up by name and creates a campaign that uses it.

Template name convention: ``"<env> · <title>"`` where env defaults to
``"Prod"`` (override via ``secrets/listmonk.maint_env``). The two titles
the window orchestrator fires are:
  ``Maintenance Window Start``     — open ping
  ``Maintenance Window Complete``  — close ping

Never raises. Failures log to ``MANITOBA_STATE_DIR/notify-fail.log``
(same file the Discord notify path uses) and return False so the
maintenance window keeps running.

Secrets read (all under ~/secrets/):
  listmonk.port         — required
  listmonk.api_user     — required
  listmonk.api_token    — required
  listmonk.list_id      — required (target list, e.g. 4 for Subscribers)
  listmonk.maint_env    — optional ('Prod' if missing)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests


_DEFAULT_TIMEOUT_S = 10


def _secrets_dir() -> Path:
    env = os.environ.get("MANITOBA_SECRETS_DIR")
    if env:
        return Path(env)
    repo_root_guess = Path(__file__).parent.parent.parent.parent
    repo_secrets = repo_root_guess / "secrets"
    if repo_secrets.is_dir():
        return repo_secrets
    return Path.home() / "secrets"


def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


def _secret_read(name: str) -> Optional[str]:
    path = _secrets_dir() / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None


def _append_fail_log(target: str, error: str) -> None:
    state_dir = _state_dir()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        line = f"{now}\tlistmonk\t{target}\t{error}\n"
        with (state_dir / "notify-fail.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        print(f"WARNING: could not write notify-fail.log: {exc}", file=sys.stderr)


def _base_url() -> Optional[str]:
    port = _secret_read("listmonk.port")
    if not port:
        return None
    return f"http://127.0.0.1:{port}"


def _auth() -> Optional[tuple[str, str]]:
    user = _secret_read("listmonk.api_user")
    token = _secret_read("listmonk.api_token")
    if not user or not token:
        return None
    return (user, token)


def _env_prefix() -> str:
    return _secret_read("listmonk.maint_env") or "Prod"


def _find_template_id(name: str, *, base: str, auth: tuple[str, str]) -> Optional[int]:
    try:
        r = requests.get(f"{base}/api/templates", auth=auth, timeout=_DEFAULT_TIMEOUT_S)
        r.raise_for_status()
        for t in r.json().get("data", []):
            if t.get("name") == name:
                return int(t["id"])
    except Exception as exc:
        _append_fail_log(f"template-lookup {name!r}", str(exc))
    return None


def fire_template_campaign(
    *,
    template_title: str,
    subject: str,
    list_id: Optional[int] = None,
    env_prefix: Optional[str] = None,
) -> bool:
    """POST a Listmonk campaign that wraps the named saved template, then
    set its status to ``running``. Returns True iff both API calls succeed.

    The campaign body is a single space — it ends up inside the
    ``{{ template "content" . }}`` HTML comment that sync.py appends to
    every saved template, so the rendered email is purely the template.

    template_title:
        Human title without env prefix, e.g. ``"Maintenance Window Start"``.
        The function prepends env_prefix (or secrets/listmonk.maint_env, or
        ``"Prod"``) to produce the full Listmonk name.
    subject:
        Email subject line.
    list_id:
        Target list id. Default: read from secrets/listmonk.list_id.
    """
    base = _base_url()
    auth = _auth()
    if not base or not auth:
        _append_fail_log(f"campaign {template_title!r}", "listmonk creds missing")
        return False

    prefix = env_prefix or _env_prefix()
    full_name = f"{prefix} · {template_title}"
    template_id = _find_template_id(full_name, base=base, auth=auth)
    if template_id is None:
        _append_fail_log(f"campaign {full_name!r}", "template not found in Listmonk")
        return False

    if list_id is None:
        raw = _secret_read("listmonk.list_id")
        try:
            list_id = int(raw) if raw else None
        except ValueError:
            list_id = None
    if not list_id:
        _append_fail_log(f"campaign {full_name!r}", "list_id missing")
        return False

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "name": f"{full_name} · {now_utc}",
        "subject": subject,
        "lists": [list_id],
        "type": "regular",
        "content_type": "html",
        "body": " ",
        "template_id": template_id,
    }
    try:
        r = requests.post(
            f"{base}/api/campaigns", json=payload, auth=auth, timeout=_DEFAULT_TIMEOUT_S
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        cid = int(data.get("id") or 0)
        if not cid:
            _append_fail_log(f"campaign {full_name!r}", f"no id in response: {data}")
            return False
        r2 = requests.put(
            f"{base}/api/campaigns/{cid}/status",
            json={"status": "running"},
            auth=auth,
            timeout=_DEFAULT_TIMEOUT_S,
        )
        r2.raise_for_status()
        return True
    except Exception as exc:
        _append_fail_log(f"campaign {full_name!r}", str(exc))
        return False
