"""SABnzbd API client — no login, apikey query param. Stateless, host-loopback.

Reads creds from ~/secrets/sabnzbd.{port,key}. Mirrors qbit_client.py's shape
(list/normalize-friendly raw dicts out, bool-returning mutations) so
collect.py and unstick.py can treat both download clients uniformly — same
detection/remediation pipeline, second client, per the compartmentalization
reading in the sab-stuck-parity spec (2026-07-19).

Unlike qbit_client, which swallows transport errors and returns False/[]
(qBit needs a login step to fail gracefully on), SabClient methods RAISE on
transport error. Research grounding: SAB's `mode=resume` returns
`{"status": true}` while no-oping on a wedged queue object — a return value
can't be trusted, and neither can a caught-and-hidden exception. Callers
(collect.py's _collect_sab, qflix-collect.py's escalation path) decide what
"the request itself failed" means for their own error shape; this client
never guesses on their behalf.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# SAB's "mb"/"mbleft" queue-slot fields are MiB (1024*1024 bytes) despite the
# decimal-sounding name — confirmed against SAB source / API docs. Used by
# collect.py's normalize_sab_slot to turn them into raw byte counts.
MIB = 1024 * 1024


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


class SabClient:
    def __init__(self, secrets_dir: Optional[Path] = None):
        self.secrets = secrets_dir or Path(
            os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets"))
        )
        port = _read(self.secrets / "sabnzbd.port")
        self.host = f"http://127.0.0.1:{port}/api" if port else ""
        self.apikey = _read(self.secrets / "sabnzbd.key")

    def _get(self, mode: str, *, extra: Optional[dict] = None,
              timeout: int = 15) -> dict:
        """GET ?mode=<mode>&output=json&apikey=... -> parsed JSON body.

        No try/except here on purpose (see module docstring): URLError,
        timeouts, and JSONDecodeError all propagate to the caller.
        """
        params = {"mode": mode, "output": "json", "apikey": self.apikey}
        if extra:
            params.update(extra)
        url = f"{self.host}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")

    def list_slots(self) -> list[dict]:
        """mode=queue -> queue.slots raw dicts (SAB Status strings verbatim,
        numeric fields as strings — normalize_sab_slot in collect.py coerces)."""
        data = self._get("queue")
        return (data.get("queue") or {}).get("slots") or []

    def queue_meta(self) -> dict:
        """mode=queue -> {"paused": bool, "kbpersec": float, "status": str}."""
        q = (self._get("queue").get("queue") or {})
        try:
            kbps = float(q.get("kbpersec", 0) or 0)
        except (TypeError, ValueError):
            kbps = 0.0
        return {
            "paused": bool(q.get("paused", False)),
            "kbpersec": kbps,
            "status": q.get("status", ""),
        }

    def list_history(self, limit: int = 60) -> list[dict]:
        """mode=history -> history.slots raw dicts."""
        data = self._get("history", extra={"limit": limit})
        return (data.get("history") or {}).get("slots") or []

    def delete_slot(self, nzo_id: str, del_files: bool = True) -> bool:
        """mode=queue&name=delete[&del_files=1].

        SAB's own {"status": true} is NOT proof the job is actually gone for
        a wedged queue object (research: GH #802/#1104/#3106 — resume/delete
        no-op on wedged objects while still reporting success). Callers that
        need certainty (unstick.py's orphan fallback) must re-poll
        list_slots() themselves; this just reports what SAB's response said.
        """
        extra = {"name": "delete", "value": nzo_id}
        if del_files:
            extra["del_files"] = "1"
        data = self._get("queue", extra=extra)
        return bool(data.get("status", False))

    def restart_repair(self) -> bool:
        """mode=restart_repair — SAB restart + queue rebuild from disk. The
        only documented remedy for wedged queue objects and hung par2/unrar.

        30s timeout (double the default): SAB restarts mid-response, so the
        connection dropping here is an EXPECTED outcome of a successful call,
        not a failure. This method still raises on transport error per the
        class contract — the escalation caller (qflix-collect.py,
        escalate_sab_if_pinned) is the one that must catch it and treat
        timeout/connection-reset as success-pending, verifying by re-polling
        queue_meta() after 60s rather than trusting a return value.
        """
        data = self._get("restart_repair", timeout=30)
        return bool(data.get("status", False))
