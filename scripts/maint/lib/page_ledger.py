"""lib/page_ledger.py — the repo's ONE cross-run page-dedup mechanism.

WHY THIS FILE EXISTS
--------------------
Retrying is never the bug. RE-PAGING is. Two incidents said the same thing:

  2026-09-02  the permanent-failure latch re-armed every 900s, re-ran the
              3-attempt recovery loop, and ended each one with a fresh
              error-level operator page: 33 identical Discord pings in ten
              hours for ONE unchanged fault.
  2026-09-17  arr-housekeeping's hourly unstick sweep notified on EVERY run
              that took any action: 12 of the 13 Discord messages in a 24h
              window came from one ongoing re-grab loop, and the single
              message that carried escalation value (the cap-hit @ping) was
              buried among eleven look-alikes.

recovery.py grew the fix first, privately. This module is that same code,
extracted so the second caller could not become a second MECHANISM — two
cooldown implementations drift, and the one that drifts is always the one
nobody is looking at. recovery.py now delegates here with byte-identical
behaviour; `tests/unit/test_recovery*.py` pass unmodified and are, in effect,
this module's oldest test suite.

DESIGN INVARIANTS (each one is load-bearing, none is decoration)

  WALL-CLOCK IN A FILE, not monotonic in memory. manitoba-maint restarts
  (deploy, upgrade, OOM) and hourly timers are separate PROCESSES — an
  in-memory cooldown would reset on every one of them and the storm would
  come straight back.

  ITS OWN FILE, beside state.json, never inside it. `state.record()` REPLACES
  a per-app entry wholesale, so a stamp parked there is wiped by the next
  record() call.

  FAILS OPEN, on every error path. An unreadable, corrupt, or unwritable
  ledger returns "page it". The worst case of failing open is the storm we
  already know how to survive; the worst case of failing closed is silence
  nobody notices. A bug in a noise suppressor must never be able to swallow
  "your media server is down".

  ATOMIC WRITES. tmp + os.replace, so a reader concurrent with a writer sees
  the old file or the new one, never half of either.

  NOT lib/ledger.py. That one is the entitlement gate's MONEY ledger
  (payments, grants). Different concern, different file, deliberately not
  extended.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

# One day. Long enough that an ongoing fault is ONE line in the channel;
# short enough that a fault still broken tomorrow is re-surfaced rather than
# forgotten. Silence-forever is the opposite failure to the one this fixes.
DEFAULT_COOLDOWN_S: float = 24 * 3600


def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


def ledger_path(name: str) -> Path:
    """<MANITOBA_STATE_DIR or ~/.opt/maint>/<name>.json"""
    if not name.endswith(".json"):
        name += ".json"
    return _state_dir() / name


def read_ledger(path: Path) -> dict:
    """Ledger as {key: unix_ts_of_last_page}. Missing or corrupt reads as
    empty — that direction pages, which is the safe one."""
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_ledger(path: Path, ledger: dict) -> None:
    """Atomic replace. Raises on a genuinely unwritable state dir — every
    caller in this module treats that as 'page anyway'."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2)
    os.replace(tmp, path)


def _unexpired(stamp, now: float, cooldown_s: float) -> bool:
    """True iff `stamp` is a live page-stamp.

    The `0 < delta` half is deliberate and is inherited byte-for-byte from
    recovery.py: a stamp in the FUTURE (clock skew, a restored backup, an NTP
    step) is not trusted to mute anything. A cooldown of 0 never mutes either,
    which is what makes `cooldown_s=0` a usable test lever.
    """
    return isinstance(stamp, (int, float)) and 0 < (now - stamp) < cooldown_s


def page_due(path: Path, key: str, cooldown_s: float = DEFAULT_COOLDOWN_S,
             now: Optional[float] = None) -> bool:
    """True iff `key` has no unexpired stamp — i.e. page it now.

    STAMPS the ledger when it returns True, so the caller pages exactly once
    per cooldown. FAILS OPEN: any exception returns True plus a stderr note.
    """
    try:
        ts = time.time() if now is None else now
        ledger = read_ledger(path)
        if _unexpired(ledger.get(key), ts, cooldown_s):
            return False
        ledger[key] = ts
        write_ledger(path, ledger)
        return True
    except Exception as exc:
        sys.stderr.write(
            "page_ledger.py: cooldown check failed for key %r, paging anyway: %r\n"
            % (key, exc))
        return True


def partition_due(path: Path, keys: Sequence[str],
                  cooldown_s: float = DEFAULT_COOLDOWN_S,
                  now: Optional[float] = None) -> tuple[list[str], list[str]]:
    """Split `keys` into (due, muted), order-preserving, duplicates collapsed.

    ONE read-modify-write for the whole batch: an hourly sweep with 40 actions
    must not do 40 read/replace cycles, and a partial batch (some stamped,
    some not, because the process died mid-loop) would re-page the tail.

    FAILS OPEN: on any exception every key comes back DUE.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    try:
        ts = time.time() if now is None else now
        ledger = read_ledger(path)
        due: list[str] = []
        muted: list[str] = []
        for k in unique:
            if _unexpired(ledger.get(k), ts, cooldown_s):
                muted.append(k)
            else:
                due.append(k)
                ledger[k] = ts
        if due:
            write_ledger(path, ledger)
        return due, muted
    except Exception as exc:
        sys.stderr.write(
            "page_ledger.py: partition failed, paging all %d key(s): %r\n"
            % (len(unique), exc))
        return unique, []


def clear_page(path: Path, key: str) -> None:
    """Drop `key`'s stamp so the NEXT occurrence pages immediately.

    This is what makes the cooldown per-OUTAGE rather than per-wall-clock-day:
    a condition that clears and comes back an hour later is news both times.
    Never raises — a failure here only costs a suppressed page.
    """
    try:
        ledger = read_ledger(path)
        if ledger.pop(key, None) is not None:
            write_ledger(path, ledger)
    except Exception:
        pass


def prune(path: Path, cooldown_s: float = DEFAULT_COOLDOWN_S,
          now: Optional[float] = None) -> int:
    """Delete expired stamps; return how many were removed.

    Keeps the file bounded. The arr-unstick keys are content-identity keys, so
    a library that churns mints new ones forever — without a prune the ledger
    is an append-only list of every title ever swept. Never raises.
    """
    try:
        ts = time.time() if now is None else now
        ledger = read_ledger(path)
        live = {k: v for k, v in ledger.items() if _unexpired(v, ts, cooldown_s)}
        removed = len(ledger) - len(live)
        if removed > 0:
            write_ledger(path, live)
        return removed
    except Exception:
        return 0
