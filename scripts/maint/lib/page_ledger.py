"""lib/page_ledger.py — the ONE cross-run page-dedup mechanism.

Extracted from lib/recovery.py's escalation-page cooldown (2026-09-02: the
Plex outage that produced 33 identical Discord pages in ten hours because a
re-arming recovery latch re-ran the loop and re-paged on every re-arm).
recovery.py now delegates _escalation_page_due/clear_escalation_page here
byte-for-byte; arr-housekeeping.py's --unstick sweep is the second caller
(2026-09-17: 12/13 Discord messages from one fault, hourly, no dedup).

Ledger file format: flat {key: unix_float_of_last_page}. Writes are a temp
file + os.replace (atomic on POSIX, best-effort but non-corrupting on
Windows — os.replace silently succeeds without the POSIX atomicity guarantee
there, which is fine for a maintenance ledger that fails open anyway).

FAILS OPEN on every error path: unreadable ledger, corrupt JSON, a
non-numeric stamp, an unwritable state dir. A bug in a noise suppressor must
never be able to swallow an operator page — the worst case of failing open
is one duplicate page, the worst case of failing closed is silence nobody
notices. Every fail-open path writes a stderr note so a cooldown eating real
pages is visible in journald rather than silent.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

DEFAULT_COOLDOWN_S: float = 24 * 3600


def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


def ledger_path(name: str) -> Path:
    """<MANITOBA_STATE_DIR or ~/.opt/maint>/<name>.json

    `name` may be given with or without a trailing '.json' — callers in this
    repo use both styles (recovery.py's own constant already carries the
    suffix; the Stage-0 interface names UNSTICK_PAGE_LEDGER the same way).
    Normalized so a caller can never double the suffix.
    """
    base = name[:-5] if name.endswith(".json") else name
    return _state_dir() / f"{base}.json"


def _read(path: Path) -> dict:
    """Raises on any read/parse failure — callers decide what "fails open"
    means for their own return shape (page_due: True; partition_due: every
    key due; prune: 0 removed)."""
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ledger root is not a JSON object")
    return data


def _write(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2)
    os.replace(tmp, path)


def _stamp_due(ledger: dict, key: str, cooldown_s: float, now: float) -> bool:
    """True iff `key` has no unexpired stamp in `ledger`. Mirrors the
    original recovery.py semantics exactly: a stamp in the future or exactly
    at `now` (clock skew, or two processes racing the same wall-clock
    second) is treated as due too — 0 < delta < cooldown is the only "still
    muted" window, not 0 <= delta."""
    stamp = ledger.get(key)
    if not isinstance(stamp, (int, float)):
        return True
    delta = now - stamp
    return not (0 < delta < cooldown_s)


def page_due(path: Path, key: str, cooldown_s: float = DEFAULT_COOLDOWN_S,
             now: Optional[float] = None) -> bool:
    """True iff `key` has no unexpired stamp. STAMPS on True.

    FAILS OPEN: any exception (unreadable/corrupt/unwritable) -> True, with
    a stderr note.
    """
    now = time.time() if now is None else now
    try:
        try:
            ledger = _read(path)
        except FileNotFoundError:
            ledger = {}
        due = _stamp_due(ledger, key, cooldown_s, now)
        if not due:
            return False
        ledger[key] = now
        _write(path, ledger)
        return True
    except Exception as exc:
        sys.stderr.write(
            f"page_ledger: page_due check failed for {path} key={key!r}, "
            f"paging anyway: {exc!r}\n"
        )
        return True


def partition_due(path: Path, keys: Sequence[str],
                   cooldown_s: float = DEFAULT_COOLDOWN_S,
                   now: Optional[float] = None) -> Tuple[List[str], List[str]]:
    """(due, muted), order-preserving, duplicates collapsed. Stamps every
    `due` key in ONE read-modify-write.

    FAILS OPEN: on any exception returns (list(keys) [deduped, ordered], []).
    """
    now = time.time() if now is None else now
    ordered: List[str] = []
    seen = set()
    for k in keys:
        if k not in seen:
            seen.add(k)
            ordered.append(k)

    try:
        try:
            ledger = _read(path)
        except FileNotFoundError:
            ledger = {}
        due: List[str] = []
        muted: List[str] = []
        for k in ordered:
            if _stamp_due(ledger, k, cooldown_s, now):
                due.append(k)
                ledger[k] = now
            else:
                muted.append(k)
        if due:
            _write(path, ledger)
        return due, muted
    except Exception as exc:
        sys.stderr.write(
            f"page_ledger: partition_due failed for {path}, treating all "
            f"{len(ordered)} key(s) as due: {exc!r}\n"
        )
        return ordered, []


def clear_page(path: Path, key: str) -> None:
    """Drop the stamp so the NEXT occurrence pages immediately. Never
    raises — this runs on the "app recovered" / "run had no cap-hit" happy
    path and must never itself become an outage."""
    try:
        try:
            ledger = _read(path)
        except FileNotFoundError:
            return
        if not isinstance(ledger, dict):
            return
        if ledger.pop(key, None) is None:
            return
        _write(path, ledger)
    except Exception as exc:
        sys.stderr.write(
            f"page_ledger: clear_page failed for {path} key={key!r} "
            f"(non-fatal, stamp may linger): {exc!r}\n"
        )


def prune(path: Path, cooldown_s: float = DEFAULT_COOLDOWN_S,
          now: Optional[float] = None) -> int:
    """Delete expired stamps, return count removed. Keeps the file bounded.
    Never raises. Non-numeric stamps are treated as garbage and pruned too."""
    now = time.time() if now is None else now
    try:
        try:
            ledger = _read(path)
        except FileNotFoundError:
            return 0
        if not isinstance(ledger, dict):
            return 0
        kept = {}
        removed = 0
        for k, v in ledger.items():
            if isinstance(v, (int, float)) and (now - v) < cooldown_s:
                kept[k] = v
            else:
                removed += 1
        if removed:
            _write(path, kept)
        return removed
    except Exception as exc:
        sys.stderr.write(f"page_ledger: prune failed for {path} (non-fatal): {exc!r}\n")
        return 0
