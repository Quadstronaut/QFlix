#!/usr/bin/env python3
"""scripts/mcp/unstick.py — DELETE *arr queue item w/ removeFromClient + blocklist.

The *arr will auto-research after a blocklist add. Idempotent: returns
already-removed if the queue item is already gone.

Modes:
  --emit-json   stdout JSON (MCP/PS1 callers)
  --cron        log only (not used in normal flow; included for symmetry)

Args:
  --slug <name> --queue-id <n> --reason <s>   (or)
  --hash <h>   --reason <s>                    (looks up queue-id by hash)
  --dry-run                                    (don't actually DELETE)
  --max-actions-per-day N                      (default 10)

Safety:
  - Refuses if the *arr's Kuma monitor is red (per ~/.opt/maint/state.json).
  - Refuses if today's events log already has --max-actions-per-day entries.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maint"))

from lib.arr_client import ArrClient  # noqa: E402
from lib.maint_state import is_arr_red       # noqa: E402

EVENTS_DIR = Path(os.environ.get(
    "QFLIX_MCP_EVENTS", str(Path.home() / "scripts" / "mcp" / "events")
))

ARR_VERSIONS = {
    "sonarr": "v3", "sonarr2": "v3", "radarr": "v3", "radarr2": "v3",
}


def _today_events_path() -> Path:
    return EVENTS_DIR / f"{dt.date.today().isoformat()}.jsonl"


def _count_today() -> int:
    p = _today_events_path()
    if not p.exists():
        return 0
    return sum(1 for _ in p.read_text().splitlines() if _.strip())


def _append_event(line: dict) -> None:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    with _today_events_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, default=str) + "\n")


def _lookup_queue_by_hash(c: ArrClient, target_hash: str) -> Optional[dict]:
    code, payload = c.get("/queue", query="pageSize=500&includeUnknownSeriesItems=true")
    if code != 200 or not isinstance(payload, dict):
        return None
    for q in payload.get("records") or []:
        if (q.get("downloadId") or "").lower() == target_hash.lower():
            return q
    return None


def run(*, slug: str, queue_id: Optional[int] = None,
        hash_: Optional[str] = None, reason: str = "",
        dry_run: bool = False, max_actions_per_day: int = 10,
        state_file: Optional[Path] = None) -> dict:
    if slug not in ARR_VERSIONS:
        return {"status": "refused-unknown-slug", "slug": slug}
    if is_arr_red(slug, state_file=state_file):
        return {"status": "refused-arr-red", "slug": slug}
    if _count_today() >= max_actions_per_day:
        return {"status": "refused-cap-hit", "count": _count_today(),
                "cap": max_actions_per_day}

    c = ArrClient(slug, ARR_VERSIONS[slug])

    # Resolve queue_id from hash if needed
    title = "?"
    actual_qid = queue_id
    if hash_ and not queue_id:
        item = _lookup_queue_by_hash(c, hash_)
        if item is None:
            return {"status": "already-removed", "slug": slug, "hash": hash_}
        actual_qid = item.get("id")
        title = item.get("title", "?")
    elif queue_id:
        # Verify the queue_id still exists
        code, payload = c.get("/queue", query="pageSize=500")
        if code == 200 and isinstance(payload, dict):
            found = next((q for q in payload.get("records", [])
                          if q.get("id") == queue_id), None)
            if found is None:
                return {"status": "already-removed", "slug": slug,
                        "queue_id": queue_id}
            title = found.get("title", "?")
            hash_ = found.get("downloadId", hash_)
        else:
            return {"status": "queue-fetch-failed", "code": code}

    pre = {"queue_id": actual_qid, "title": title, "hash": hash_}

    if dry_run:
        result = {"status": "dry-run", "pre": pre}
    else:
        code, _ = c.delete(f"/queue/{actual_qid}",
                           query="removeFromClient=true&blocklist=true")
        if code in (200, 204):
            result = {"status": "deleted+blocklisted", "pre": pre}
        elif code == 404:
            result = {"status": "already-removed", "pre": pre}
        else:
            result = {"status": "delete-failed", "pre": pre, "code": code}

    _append_event({
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "action": "unstick",
        "slug": slug,
        "queue_id": actual_qid,
        "hash": hash_,
        "title": title,
        "reason": reason,
        "result": result["status"],
        "post_action": ("sonarr-research-queued" if result["status"]
                        == "deleted+blocklisted" else None),
    })
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--cron", action="store_true")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--queue-id", type=int)
    ap.add_argument("--hash")
    ap.add_argument("--reason", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-actions-per-day", type=int, default=10)
    args = ap.parse_args()
    if not args.queue_id and not args.hash:
        ap.error("--queue-id or --hash required")
    res = run(slug=args.slug, queue_id=args.queue_id, hash_=args.hash,
              reason=args.reason, dry_run=args.dry_run,
              max_actions_per_day=args.max_actions_per_day)
    if args.emit_json:
        json.dump(res, sys.stdout, default=str)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
