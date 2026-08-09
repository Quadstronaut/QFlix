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
from lib.qbit_client import QbitClient       # noqa: E402
from lib.sab_client import SabClient         # noqa: E402

EVENTS_DIR = Path(os.environ.get(
    "QFLIX_MCP_EVENTS", str(Path.home() / "scripts" / "mcp" / "events")
))

ARR_VERSIONS = {
    "sonarr": "v3", "sonarr2": "v3", "radarr": "v3", "radarr2": "v3",
}

# Results that actually consumed an *arr/qBit/SAB action slot. The daily cap
# counts only these — refusals (cap-hit, arr-red, unknown-slug) are still
# appended to the events file for audit but must not gate the next attempt,
# or a single orphan that fires hourly self-traps the counter the moment
# the real cap is hit (each refusal append re-counts and re-refuses).
# "sab-orphan-removed" is the usenet twin of "qbit-orphan-removed" (C5, SAB
# stuck-parity spec 2026-07-19). qflix-collect.py keeps its OWN mirror tuples
# (_EFFECTIVE_RESULTS / _TERMINAL_STATUSES) per the compartmentalization law
# and carries "sab-orphan-removed" too — kept in sync there, not edited here.
_EFFECTIVE_STATUSES = frozenset({
    "deleted+blocklisted",
    "qbit-orphan-removed",
    "sab-orphan-removed",
})


def _today_events_path() -> Path:
    return EVENTS_DIR / f"{dt.date.today().isoformat()}.jsonl"


def _count_today() -> int:
    p = _today_events_path()
    if not p.exists():
        return 0
    count = 0
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("result") in _EFFECTIVE_STATUSES:
            count += 1
    return count


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
    """Find the queue item by hash or id. Walks paginated /queue results."""
    target_hash = hash_.lower() if hash_ else None
    page = 1
    seen = 0
    while True:
        query = f"page={page}" if page > 1 else ""
        code, payload = c.get("/queue", query=query, timeout=15)
        if code != 200 or not isinstance(payload, dict):
            return {"status": "queue-fetch-failed", "code": code}
        records = payload.get("records") or []
        for q in records:
            if target_hash is not None:
                if (q.get("downloadId") or "").lower() == target_hash:
                    return {"status": "found",
                            "queue_id": q.get("id"),
                            "title": q.get("title", "?"),
                            "hash": q.get("downloadId")}
            elif queue_id is not None and q.get("id") == queue_id:
                return {"status": "found",
                        "queue_id": queue_id,
                        "title": q.get("title", "?"),
                        "hash": q.get("downloadId")}
        seen += len(records)
        total = payload.get("totalRecords", seen)
        if seen >= total or not records:
            return {"status": "already-removed"}
        page += 1
        if page > 50:  # hard safety cap
            return {"status": "already-removed"}


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


_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def _id_kind(s: Optional[str]) -> str:
    """Classify a download-client identifier by SHAPE alone — no network
    call, just string inspection. Two client id formats never collide:
    SAB's nzo_id is always `SABnzbd_nzo_<random>`; qBit's torrent hash is
    always a 40-char hex string. Anything else (garbage input, or no id at
    all) is "unknown" — callers probe both clients rather than guess wrong
    (see _auto_detect_slug / _try_client_orphan_cleanup below)."""
    if not s:
        return "unknown"
    if s.startswith("SABnzbd_nzo"):
        return "sab"
    if len(s) == 40 and all(ch in _HEX_CHARS for ch in s):
        return "qbit"
    return "unknown"


def _auto_detect_slug_qbit(hash_: str) -> Optional[str]:
    """Look up the qBit torrent for `hash_` and return its category as the
    candidate *arr slug. Returns None if qBit doesn't have the hash or the
    category isn't a known *arr. (Unchanged qBit-only path — see
    _auto_detect_slug for the id-shape dispatch that now wraps it.)"""
    c = QbitClient()
    if not c.login():
        return None
    target = hash_.lower()
    hit = next((t for t in c.list_torrents()
                if (t.get("hash") or "").lower() == target), None)
    if not hit:
        return None
    cat = (hit.get("category") or "").strip().lower()
    return cat if cat in ARR_VERSIONS else None


def _auto_detect_slug_sab(nzo_id: str) -> Optional[str]:
    """SAB twin of _auto_detect_slug_qbit: look up the SAB slot for `nzo_id`
    via SabClient.list_slots() and return its `cat` as the candidate *arr
    slug. Returns None if SAB has no secrets configured, list_slots() raises
    (SabClient raises on transport error by design — this is the one place
    in unstick.py that catches it for this lookup), SAB doesn't have the
    id, or the cat isn't a known *arr."""
    c = SabClient()
    if not c.host or not c.apikey:
        return None
    try:
        slots = c.list_slots()
    except Exception:
        return None
    hit = next((s for s in slots if s.get("nzo_id") == nzo_id), None)
    if not hit:
        return None
    cat = (hit.get("cat") or "").strip().lower()
    return cat if cat in ARR_VERSIONS else None


def _auto_detect_slug(hash_: Optional[str]) -> Optional[str]:
    """Dispatch by id shape (C5): a sab-shaped id resolves via SAB's slot
    cat, a qbit-shaped id via qBit's category (the original, untouched
    lookup), and an ambiguous/unknown-shaped id probes qBit first then SAB
    — same probe order as _try_client_orphan_cleanup. Used when callers
    don't pass --slug (autonomous qflix-collect path)."""
    if not hash_:
        return None
    kind = _id_kind(hash_)
    if kind == "sab":
        return _auto_detect_slug_sab(hash_)
    if kind == "qbit":
        return _auto_detect_slug_qbit(hash_)
    slug = _auto_detect_slug_qbit(hash_)
    if slug is not None:
        return slug
    return _auto_detect_slug_sab(hash_)


def _try_qbit_orphan_cleanup(hash_: Optional[str], *, dry_run: bool) -> dict:
    """Fallback for the case where the *arr's queue no longer holds the hash
    (already-removed from *arr) but qBit still does. Without this, the
    candidate fires hourly forever — *arr has nothing to delete, so qBit's
    orphan torrent stays put. Returns one of:
      - qbit-orphan-removed       qBit had it, we deleted it
      - already-fully-removed     neither *arr nor qBit had it
      - qbit-login-failed         qBit auth refused (creds wrong / qBit down)
      - qbit-delete-failed        qBit auth ok but DELETE returned non-2xx
      - no-hash-for-qbit-lookup   caller invoked us without a hash to match
    """
    if not hash_:
        return {"status": "no-hash-for-qbit-lookup"}
    c = QbitClient()
    if not c.login():
        return {"status": "qbit-login-failed"}
    target = hash_.lower()
    hit = next((t for t in c.list_torrents()
                if (t.get("hash") or "").lower() == target), None)
    if not hit:
        return {"status": "already-fully-removed"}
    if dry_run:
        return {"status": "dry-run-qbit-orphan",
                "qbit_title": hit.get("name", "?")[:80]}
    ok = c.delete_torrent(target, delete_files=True)
    return {"status": "qbit-orphan-removed" if ok else "qbit-delete-failed",
            "qbit_title": hit.get("name", "?")[:80]}


def _try_sab_orphan_cleanup(nzo_id: Optional[str], *, dry_run: bool) -> dict:
    """SAB twin of _try_qbit_orphan_cleanup: fallback for the case where the
    *arr's queue no longer holds the nzo_id but SAB still does. Without
    this, a SAB-backed candidate whose *arr entry vanished fires every hour
    forever — *arr has nothing to delete, so SAB's orphan slot stays put.
    Returns one of:
      - sab-orphan-removed    SAB had the slot, delete_slot() reported success
      - already-fully-removed neither *arr nor SAB had it (also the answer
                              for a falsy nzo_id — nothing to look up)
      - sab-unreachable       no secrets configured, or SabClient raised on
                              list_slots()/delete_slot() (SAB down / transport
                              error — SabClient raises by design, see its
                              module docstring; this is the one place in
                              unstick.py that catches it for this cleanup)
      - sab-delete-failed     SAB had the slot but delete_slot() reported
                              {"status": false} (a wedged object — see the
                              GH #802/#1104/#3106 research note; the
                              escalation circuit-breaker is the next line
                              of defense for that case, not this function)
      - dry-run-sab-orphan    would have deleted; --dry-run suppressed it
    """
    if not nzo_id:
        return {"status": "already-fully-removed"}
    c = SabClient()
    if not c.host or not c.apikey:
        return {"status": "sab-unreachable"}
    try:
        slots = c.list_slots()
    except Exception:
        return {"status": "sab-unreachable"}
    hit = next((s for s in slots if s.get("nzo_id") == nzo_id), None)
    if not hit:
        return {"status": "already-fully-removed"}
    title = (hit.get("filename") or "?")[:80]
    if dry_run:
        return {"status": "dry-run-sab-orphan", "sab_title": title}
    try:
        ok = c.delete_slot(nzo_id, del_files=True)
    except Exception:
        return {"status": "sab-unreachable"}
    return {"status": "sab-orphan-removed" if ok else "sab-delete-failed",
            "sab_title": title}


def _try_client_orphan_cleanup(id_: Optional[str], *, dry_run: bool) -> dict:
    """Dispatch the orphan-fallback probe by id shape (C5): a sab-shaped id
    goes straight to _try_sab_orphan_cleanup, a qbit-shaped id straight to
    _try_qbit_orphan_cleanup, and an ambiguous/absent id probes qBit first
    (the original single-client behavior, preserved) then SAB — same probe
    order as _auto_detect_slug. Replaces the direct
    _try_qbit_orphan_cleanup() calls at every orphan-fallback call site in
    run() so a SAB-backed candidate gets SAB cleanup instead of a qBit-only
    "not found" answer."""
    kind = _id_kind(id_)
    if kind == "sab":
        return _try_sab_orphan_cleanup(id_, dry_run=dry_run)
    if kind == "qbit":
        return _try_qbit_orphan_cleanup(id_, dry_run=dry_run)
    # Unknown shape: try qBit first, then SAB. A falsy id can't be found in
    # either client — qBit's own no-id guard ("no-hash-for-qbit-lookup") is
    # already the correct final answer there, so don't bother asking SAB
    # the same unanswerable question.
    result = _try_qbit_orphan_cleanup(id_, dry_run=dry_run)
    if not id_ or result["status"] != "already-fully-removed":
        return result
    return _try_sab_orphan_cleanup(id_, dry_run=dry_run)


def _orphan_title(fallback: dict) -> str:
    """Both client-specific fallback shapes stash a display title under a
    client-tagged key (qbit_title / sab_title) — pull whichever is present
    for the event-log line."""
    return fallback.get("qbit_title") or fallback.get("sab_title") or "?"


def _record_event(*, slug: str, queue_id: Optional[int], hash_: Optional[str],
                  title: str, reason: str, result_status: str) -> None:
    _append_event({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
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


def run(*, slug: Optional[str] = None, queue_id: Optional[int] = None,
        hash_: Optional[str] = None, reason: str = "",
        dry_run: bool = False, max_actions_per_day: int = 10,
        state_file: Optional[Path] = None) -> dict:
    # If caller didn't tell us which *arr to consult, use qBit's category tag
    # on the torrent. The autonomous qflix-collect path uses this hatch.
    if slug is None:
        slug = _auto_detect_slug(hash_)
        if slug is None:
            # No slug, no hit in either client plane. Dispatch by id shape
            # (C5) so a SAB-shaped id gets SAB's own orphan check instead of
            # qBit's qbit-only "not found" answer.
            fallback = _try_client_orphan_cleanup(hash_, dry_run=dry_run)
            _record_event(slug="<auto>", queue_id=queue_id, hash_=hash_,
                           title=_orphan_title(fallback), reason=reason,
                           result_status=fallback["status"])
            return fallback

    refusal = _preflight(slug, state_file=state_file,
                          max_actions_per_day=max_actions_per_day)
    if refusal is not None:
        _record_event(slug=slug, queue_id=queue_id, hash_=hash_,
                       title="?", reason=reason,
                       result_status=refusal["status"])
        return refusal

    c = ArrClient(slug, ARR_VERSIONS[slug], timeout=15)
    resolved = _resolve_queue_item(c, hash_=hash_, queue_id=queue_id)

    if resolved["status"] == "already-removed":
        # *arr has nothing to delete. If the download client still has the
        # id, it's an orphan we have to clean up directly — otherwise the
        # candidate will fire every hour with no effect. Dispatch by id
        # shape (C5) so this reaches SAB for a SAB-backed candidate.
        fallback = _try_client_orphan_cleanup(hash_, dry_run=dry_run)
        _record_event(slug=slug, queue_id=queue_id, hash_=hash_,
                       title=_orphan_title(fallback), reason=reason,
                       result_status=fallback["status"])
        return fallback

    if resolved["status"] != "found":
        _record_event(slug=slug, queue_id=queue_id, hash_=hash_,
                       title="?", reason=reason,
                       result_status=resolved["status"])
        return resolved

    actual_qid = resolved["queue_id"]
    title = resolved["title"]
    hash_ = resolved.get("hash") or hash_

    # Durability: record the intent BEFORE the destructive call. If the SSH
    # session that invoked us is killed mid-DELETE (the 2026-05 120s-timeout
    # case), the *arr still processes removeFromClient+blocklist, but the
    # terminal _record_event below never runs — the action then vanishes from
    # the events log, the daily-cap accounting, and the audit trail. The
    # in-flight marker guarantees a durable trace from the moment we commit, so
    # a reader (operator, MCP reconcile) can see the DELETE was issued even if
    # we die before writing the outcome. "delete-in-flight" is NOT an effective
    # status, so it never double-counts against the daily cap.
    if not dry_run:
        _record_event(slug=slug, queue_id=actual_qid, hash_=hash_,
                       title=title, reason=reason,
                       result_status="delete-in-flight")

    action = _execute_delete(c, queue_id=actual_qid, dry_run=dry_run)
    final_status = action["status"]

    if final_status == "already-removed":
        # Race: queue_item disappeared between lookup and DELETE. Try the
        # client-side fallback (dispatched by id shape, C5) so we don't
        # leave a candidate stuck in limbo.
        fallback = _try_client_orphan_cleanup(hash_, dry_run=dry_run)
        _record_event(slug=slug, queue_id=actual_qid, hash_=hash_,
                       title=title, reason=reason,
                       result_status=fallback["status"])
        return fallback

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
    ap.add_argument("--slug",
                    help="*arr to consult. If omitted, auto-detected from "
                         "qBit's category field for the given hash.")
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
        if not args.slug:
            ap.error("--slug required with --diagnose")
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
