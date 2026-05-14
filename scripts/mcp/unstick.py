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


def _preflight(slug: str, *, state_file: Optional[Path],
               max_actions_per_day: int) -> Optional[dict]:
    """Returns a refusal dict if any guard trips, else None."""
    if slug not in ARR_VERSIONS:
        return {"status": "refused-unknown-slug", "slug": slug}
    if is_arr_red(slug, state_file=state_file):
        return {"status": "refused-arr-red", "slug": slug}
    used = _count_today()
    if used >= max_actions_per_day:
        return {"status": "refused-cap-hit", "count": used,
                "cap": max_actions_per_day}
    return None


def _resolve_queue_item(c: ArrClient, *, hash_: Optional[str],
                        queue_id: Optional[int]) -> dict:
    """Find the queue item by hash or id. Returns one of:
      {"status": "found", "queue_id": N, "title": "...", "hash": "..."}
      {"status": "already-removed"}
      {"status": "queue-fetch-failed", "code": N}
    """
    code, payload = c.get("/queue", timeout=15)
    if code != 200 or not isinstance(payload, dict):
        return {"status": "queue-fetch-failed", "code": code}
    records = payload.get("records") or []
    if hash_:
        target = hash_.lower()
        for q in records:
            if (q.get("downloadId") or "").lower() == target:
                return {"status": "found",
                        "queue_id": q.get("id"),
                        "title": q.get("title", "?"),
                        "hash": q.get("downloadId")}
        return {"status": "already-removed"}
    if queue_id is not None:
        for q in records:
            if q.get("id") == queue_id:
                return {"status": "found",
                        "queue_id": queue_id,
                        "title": q.get("title", "?"),
                        "hash": q.get("downloadId")}
        return {"status": "already-removed"}
    return {"status": "queue-fetch-failed", "code": 0}


def _execute_delete(c: ArrClient, *, queue_id: int, dry_run: bool) -> dict:
    """Single point of the destructive DELETE. Returns the action outcome."""
    if dry_run:
        return {"status": "dry-run"}
    code, _ = c.delete(f"/queue/{queue_id}",
                       query="removeFromClient=true&blocklist=true",
                       timeout=30)
    if code in (200, 204):
        return {"status": "deleted+blocklisted"}
    if code == 404:
        return {"status": "already-removed"}
    return {"status": "delete-failed", "code": code}


def _record_event(*, slug: str, queue_id: Optional[int], hash_: Optional[str],
                  title: str, reason: str, result_status: str) -> None:
    _append_event({
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "action": "unstick",
        "slug": slug,
        "queue_id": queue_id,
        "hash": hash_,
        "title": title,
        "reason": reason,
        "result": result_status,
        "post_action": ("sonarr-research-queued"
                         if result_status == "deleted+blocklisted" else None),
    })


def run(*, slug: str, queue_id: Optional[int] = None,
        hash_: Optional[str] = None, reason: str = "",
        dry_run: bool = False, max_actions_per_day: int = 10,
        state_file: Optional[Path] = None) -> dict:
    refusal = _preflight(slug, state_file=state_file,
                          max_actions_per_day=max_actions_per_day)
    if refusal is not None:
        _record_event(slug=slug, queue_id=queue_id, hash_=hash_,
                       title="?", reason=reason,
                       result_status=refusal["status"])
        return refusal

    c = ArrClient(slug, ARR_VERSIONS[slug], timeout=15)
    resolved = _resolve_queue_item(c, hash_=hash_, queue_id=queue_id)
    if resolved["status"] != "found":
        _record_event(slug=slug, queue_id=queue_id, hash_=hash_,
                       title="?", reason=reason,
                       result_status=resolved["status"])
        return resolved

    actual_qid = resolved["queue_id"]
    title = resolved["title"]
    hash_ = resolved.get("hash") or hash_

    action = _execute_delete(c, queue_id=actual_qid, dry_run=dry_run)
    final_status = action["status"]
    _record_event(slug=slug, queue_id=actual_qid, hash_=hash_,
                   title=title, reason=reason,
                   result_status=final_status)
    out = {"status": final_status,
           "pre": {"queue_id": actual_qid, "title": title, "hash": hash_}}
    if "code" in action:
        out["code"] = action["code"]
    return out


def diagnose(*, slug: str, hash_: str,
             state_file: Optional[Path] = None) -> dict:
    """Time each phase of unstick.py's pre-flight path. No DELETE, no event."""
    import time
    phases: dict = {}

    t0 = time.perf_counter()
    is_arr_red(slug, state_file=state_file)
    phases["state_read_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    if slug not in ARR_VERSIONS:
        return {"status": "diagnose", "slug": slug, "hash": hash_,
                "phases": phases, "error": "unknown-slug"}

    c = ArrClient(slug, ARR_VERSIONS[slug], timeout=30)

    t0 = time.perf_counter()
    code_p, payload_p = c.get("/queue",
                               query="pageSize=500&includeUnknownSeriesItems=true",
                               timeout=30)
    phases["queue_lookup_paged_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    queue_size_paged = len((payload_p or {}).get("records") or []) if isinstance(payload_p, dict) else 0

    t0 = time.perf_counter()
    code_d, payload_d = c.get("/queue", timeout=30)
    phases["queue_lookup_default_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    queue_size_default = len((payload_d or {}).get("records") or []) if isinstance(payload_d, dict) else 0

    t0 = time.perf_counter()
    resolved = _resolve_queue_item(c, hash_=hash_, queue_id=None)
    phases["hash_match_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    return {
        "status": "diagnose",
        "slug": slug, "hash": hash_,
        "phases": phases,
        "queue_size_paged": queue_size_paged,
        "queue_size_default": queue_size_default,
        "resolved_status": resolved.get("status"),
        "queue_lookup_paged_http_code": code_p,
        "queue_lookup_default_http_code": code_d,
    }


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
    ap.add_argument("--diagnose", action="store_true",
                    help="time pre-flight phases without DELETE")
    ap.add_argument("--max-actions-per-day", type=int, default=10)
    args = ap.parse_args()
    if args.diagnose:
        if not args.hash:
            ap.error("--hash required with --diagnose")
        res = diagnose(slug=args.slug, hash_=args.hash)
    else:
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
