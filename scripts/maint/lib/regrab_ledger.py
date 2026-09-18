#!/usr/bin/env python3
"""regrab_ledger — the durable memory that turns a re-grab LOOP into one park.

WHY THIS MODULE EXISTS
----------------------
`arr-housekeeping.py --unstick` runs hourly. When it finds a stuck queue item
it issues `DELETE /queue/{id}?removeFromClient=true&blocklist=true`, and the
*arr answers a failed/blocklisted release by grabbing the next one. For a title
whose EVERY available release is poison (a repack carrying an executable, a
season pack nobody seeds, an indexer serving the same bad file under ten
names) that is a closed loop: grab -> stall -> blocklist -> grab. Measured over
seven days: **200 blocklist adds across 54 episodes; the worst single episode
took 13; the top 8 episodes accounted for 81.** Nothing in the sweep could see
that, because its only memory was keyed by qBittorrent download hash
(`_state_key(slug, download_id)`) and **every re-grab is a new hash**. The
script was, by construction, incapable of noticing it had done this before.

This ledger is that missing memory. It is keyed by the thing that does NOT
change across re-grabs — the *arr's own (instance, series, episodes) or
(instance, movie) identity — so the Nth blocklist add for one episode is
recognisable as the Nth.

WHY NOT `lib/ledger.py`
-----------------------
That is the money-in ledger for the entitlement gate: append-only JSONL, keyed
by household, and it **fails CLOSED on a malformed line**. Fail-closed is
correct there (never grant access off a ledger you cannot read) and is exactly
backwards here: a corrupt file must never be able to stop the stuck-queue
sweep from doing the job it did before this module existed. The convention
copied instead is `lib/recovery.py`'s escalation-page ledger (small JSON dict,
corrupt/missing reads as `{}`, tmp + `os.replace` on write, fails OPEN with a
stderr note). Same shape, separate module and separate lifecycle — the
compartmentalise law, so this can be tuned or ripped out without touching
recovery.

WHY N=3 IN 24h (ARR_REGRAB_MAX_ADDS / ARR_REGRAB_WINDOW_HOURS)
--------------------------------------------------------------
Against the measured week: one or two blocklist adds for an episode is the
system working — the first release was bad, the second was fine, and a guard
that fired there would park content that was about to arrive. Three adds
inside a day is the signature of a title with no good release at all: the
sweep runs hourly, so three adds in 24h means the *arr burned three distinct
releases and is still empty-handed. That threshold would have caught all 8 of
the top offenders (81 of the 200 adds) while leaving the long tail — 46
episodes averaging ~2.6 adds across a whole week — completely alone. Both
numbers are env knobs precisely because this is calibration, not physics.

HONEST COUNTING BOUNDARY (INV-11)
----------------------------------
This ledger counts blocklist adds **this script performs**. Sonarr and Radarr
also blocklist on their own (FailedDownloadService on an import failure), and
those are invisible here — the module never reads `/api/v3/blocklist`. So the
counts UNDER-report the true blocklist rate for a title, which means the guard
fires later than a census would, never earlier. No surface may describe this
file as a census of the *arr blocklist.

SHAPE ON DISK  (~/.opt/maint/arr-regrab-ledger.json)
----------------------------------------------------
    {
      "sonarr|1234|5678,5679": {
        "adds":     [1757000000.0, 1757003600.0, 1757007200.0],
        "parked":   true,
        "notified": true,
        "title":    "Some Show S01E01",
        "last":     1757007200.0
      },
      "radarr|441": { ... }
    }
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable

# Same state dir every maint module uses. Resolved on EVERY call, never
# captured at import: tests/unit/conftest.py sets MANITOBA_STATE_DIR from an
# autouse fixture that necessarily runs AFTER this module is imported, so an
# import-time constant silently ignored it and wrote fixture rows into the
# developer's real ~/.opt/maint (observed 2026-09-17 during the council round
# that produced this module). A module-level default is a test-isolation hole
# whenever the env var it reads is set by a fixture.
def state_dir() -> Path:
    return Path(os.environ.get("MANITOBA_STATE_DIR",
                               str(Path.home() / ".opt" / "maint")))


def ledger_path() -> Path:
    return state_dir() / "arr-regrab-ledger.json"


def lock_path() -> Path:
    return state_dir() / "arr-regrab-ledger.lock"


def __getattr__(name: str):  # PEP 562 — module-level lazy attribute
    if name == "LEDGER_PATH":
        return ledger_path()
    if name == "STATE_DIR":
        return state_dir()
    raise AttributeError(name)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# Park after this many blocklist adds ...
MAX_ADDS: int = _int_env("ARR_REGRAB_MAX_ADDS", 3)
# ... inside this rolling window.
WINDOW_HOURS: float = _float_env("ARR_REGRAB_WINDOW_HOURS", 24)
# Hard ceiling on the file size. See prune()/INV-8.
MAX_KEYS: int = _int_env("ARR_REGRAB_LEDGER_MAX_KEYS", 500)
# At most this many unmonitor writes per sweep (the rest defer to next hour).
MAX_PARKS_RUN: int = _int_env("ARR_REGRAB_MAX_PARKS_PER_RUN", 5)
# At most this many park-clearance GETs per sweep.
MAX_PARK_READS: int = _int_env("ARR_REGRAB_MAX_PARK_READS_PER_RUN", 25)
# Keys untouched for this long are dropped even below MAX_KEYS.
RETAIN_HOURS: float = _float_env("ARR_REGRAB_RETAIN_HOURS", 168)

# Per-key cap on the retained add timestamps. Only adds inside WINDOW_HOURS
# ever influence a decision; the rest are audit trail, and audit trail that
# grows without bound is how a state file becomes an incident.
_MAX_ADDS_RETAINED = 50


def _warn(msg: str) -> None:
    """One stderr line, never an exception. Every failure in this module is
    reported and then ignored — see the fail-open note in read()."""
    try:
        sys.stderr.write("regrab_ledger: " + msg + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def episode_key(slug: str, series_id, episode_ids: Iterable) -> str | None:
    """`sonarr|1234|5678,5679` — instance, series, sorted+deduped episode ids.

    Returns None (UNKEYABLE) when the series id is falsy or no episode id
    survives. An unkeyable item is NOT guessed at and NOT dropped: the caller
    proceeds exactly as it did before this module existed (INV-7). Sonarr
    queues rows with `includeUnknownSeriesItems=true`, and those genuinely
    carry no series identity to key on.
    """
    try:
        if not series_id:
            return None
        ids = set()
        for raw in episode_ids or ():
            if isinstance(raw, bool):
                continue
            try:
                val = int(raw)
            except (TypeError, ValueError):
                continue
            if val:
                ids.add(val)
        if not ids:
            return None
        return "{}|{}|{}".format(slug, int(series_id),
                                 ",".join(str(i) for i in sorted(ids)))
    except Exception as exc:
        _warn("episode_key failed, treating item as unkeyable: " + repr(exc))
        return None


def movie_key(slug: str, movie_id) -> str | None:
    """`radarr|441`. None when the movie id is falsy (UNKEYABLE, see INV-7)."""
    try:
        if not movie_id:
            return None
        return "{}|{}".format(slug, int(movie_id))
    except Exception as exc:
        _warn("movie_key failed, treating item as unkeyable: " + repr(exc))
        return None


def slug_of(key: str) -> str:
    """The *arr instance a ledger key belongs to. `""` if unparseable."""
    try:
        return key.split("|", 1)[0]
    except Exception:
        return ""


def is_movie_key(key: str) -> bool:
    return key.count("|") == 1


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def _blank(title: str = "?") -> dict:
    return {"adds": [], "parked": False, "notified": False,
            "title": title, "last": 0.0}


def read(path: Path | None = None) -> dict:
    """The ledger, or `{}`.

    FAILS OPEN, unconditionally. Missing file, unreadable file, truncated
    JSON, a payload that is a list instead of an object, a permissions error —
    every one of them returns `{}` plus one stderr line, and NEVER raises.

    The direction matters and is the opposite of `lib/ledger.py`'s: an empty
    read here degrades the sweep to precisely its pre-guard behaviour (it
    blocklists and re-searches, as it always did). A read that raised, or that
    refused to proceed, would let a corrupt 2 KB file stop the stuck-download
    repair for the whole stack. Entries that are not objects are dropped on
    the way in so no consumer below has to re-check.
    """
    p = Path(path) if path is not None else ledger_path()
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        _warn("unreadable ledger at " + str(p) + " (" + repr(exc)
              + ") — continuing WITHOUT the re-grab guard this run")
        return {}
    if not isinstance(data, dict):
        _warn("ledger at " + str(p) + " is a " + type(data).__name__
              + ", not an object — continuing WITHOUT the re-grab guard")
        return {}
    clean: dict = {}
    for key, val in data.items():
        if not isinstance(key, str) or not isinstance(val, dict):
            _warn("dropping malformed ledger entry " + repr(key)[:60])
            continue
        entry = _blank(str(val.get("title") or "?"))
        adds = val.get("adds")
        if isinstance(adds, (list, tuple)):
            entry["adds"] = [float(a) for a in adds
                             if isinstance(a, (int, float))
                             and not isinstance(a, bool)]
        entry["parked"] = bool(val.get("parked"))
        entry["notified"] = bool(val.get("notified"))
        last = val.get("last")
        entry["last"] = float(last) if isinstance(last, (int, float)) \
            and not isinstance(last, bool) else (max(entry["adds"]) if entry["adds"] else 0.0)
        clean[key] = entry
    return clean


def write(ledger: dict, path: Path | None = None) -> bool:
    """tmp + `os.replace`, so a crash mid-write cannot leave a half file.

    Returns False (plus a stderr line) on any failure and NEVER raises: an
    unwritable state dir must cost the guard its memory, not cost the stack
    its stuck-download repair.
    """
    p = Path(path) if path is not None else ledger_path()
    tmp_name = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # UNIQUE temp name, not a fixed "<name>.json.tmp": two writers sharing
        # one fixed temp path interleave their partial writes and os.replace
        # publishes whichever half won. mkstemp in the SAME directory keeps
        # os.replace atomic (same filesystem).
        fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp",
                                        dir=str(p.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2, sort_keys=True)
        os.replace(tmp_name, p)
        tmp_name = None
        return True
    except Exception as exc:
        _warn("could not write ledger at " + str(p) + " (" + repr(exc)
              + ") — this sweep is not remembered")
        return False
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------

def _entry(ledger: dict, key: str) -> dict | None:
    val = ledger.get(key)
    return val if isinstance(val, dict) else None


def record_blocklist_add(ledger: dict, key: str, title: str, now: float) -> dict:
    """One more blocklist add for `key`. In place; returns the ledger.

    Called ONLY after a DELETE that actually returned 200/204 with
    blocklist=true. A planned-but-not-performed add is not an add.
    """
    try:
        entry = _entry(ledger, key)
        if entry is None:
            entry = _blank(title)
            ledger[key] = entry
        entry["adds"].append(float(now))
        # Trim to the retention window, then to the retained-count cap.
        cutoff = float(now) - (max(WINDOW_HOURS, RETAIN_HOURS) * 3600.0)
        entry["adds"] = [a for a in entry["adds"] if a >= cutoff][-_MAX_ADDS_RETAINED:]
        if title:
            entry["title"] = str(title)[:120]
        entry["last"] = float(now)
    except Exception as exc:
        _warn("record_blocklist_add failed for " + repr(key)[:60] + ": " + repr(exc))
    return ledger


def adds_in_window(ledger: dict, key: str, now: float) -> int:
    try:
        entry = _entry(ledger, key)
        if entry is None:
            return 0
        cutoff = float(now) - (WINDOW_HOURS * 3600.0)
        return sum(1 for a in entry.get("adds", ()) if float(a) >= cutoff)
    except Exception as exc:
        _warn("adds_in_window failed for " + repr(key)[:60] + ": " + repr(exc))
        return 0


def should_park(ledger: dict, key: str, now: float) -> bool:
    """True when this key has burned MAX_ADDS releases inside WINDOW_HOURS and
    is not parked already. Any internal failure answers False — declining to
    park is the only way this module is allowed to change the sweep (INV-6)."""
    try:
        entry = _entry(ledger, key)
        if entry is None or entry.get("parked"):
            return False
        return adds_in_window(ledger, key, now) >= MAX_ADDS
    except Exception as exc:
        _warn("should_park failed for " + repr(key)[:60] + ": " + repr(exc))
        return False


def mark_parked(ledger: dict, key: str, now: float) -> None:
    """Stamp the park. Called only after the unmonitor write returned 2xx —
    `parked` is a record of a write that HAPPENED, never of one that was
    planned."""
    try:
        entry = _entry(ledger, key)
        if entry is None:
            entry = _blank()
            ledger[key] = entry
        entry["parked"] = True
        entry.setdefault("notified", False)
        entry["last"] = float(now)
    except Exception as exc:
        _warn("mark_parked failed for " + repr(key)[:60] + ": " + repr(exc))


def touch(ledger: dict, key: str, now: float) -> None:
    """Refresh `last` for a key still observed as parked, so prune()'s age
    rule only ever reaps parks whose item the sweep can no longer see."""
    try:
        entry = _entry(ledger, key)
        if entry is not None:
            entry["last"] = float(now)
    except Exception:
        pass


def should_notify_park(ledger: dict, key: str) -> bool:
    entry = _entry(ledger, key)
    return bool(entry) and bool(entry.get("parked")) and not entry.get("notified")


def mark_notified(ledger: dict, key: str) -> None:
    entry = _entry(ledger, key)
    if entry is not None:
        entry["notified"] = True


def clear(ledger: dict, key: str) -> bool:
    """Drop the key entirely: the item has a file or is monitored again, so
    the loop is over. Dropping (rather than flipping `parked`) is what makes a
    future re-park news again and notify once more (INV-9)."""
    return ledger.pop(key, None) is not None


def parked_keys(ledger: dict) -> list[str]:
    return sorted(k for k, v in ledger.items()
                  if isinstance(v, dict) and v.get("parked"))


def prune(ledger: dict, now: float) -> dict:
    """Keep the file bounded. Post-condition: `len(result) <= MAX_KEYS`.

    Order (INV-8):
      1. drop anything untouched for longer than max(WINDOW_HOURS, RETAIN_HOURS)
      2. if still over MAX_KEYS, evict NON-parked keys, oldest `last` first
      3. if still over MAX_KEYS, evict parked keys, oldest `last` first

    Parked keys go LAST because they carry the once-only notification flag:
    evicting one re-notifies the operator about a park that has not changed,
    which is how an alert channel gets muted. Step 1 still applies to them —
    but the sweep calls touch() on every park it can still see, so the only
    parks that age out are those whose item has vanished from the *arr
    entirely, at which point there is nothing left to say about them.
    """
    try:
        cutoff = float(now) - (max(WINDOW_HOURS, RETAIN_HOURS) * 3600.0)
        kept = {k: v for k, v in ledger.items()
                if isinstance(v, dict) and float(v.get("last", 0) or 0) >= cutoff}
        cap = MAX_KEYS if MAX_KEYS > 0 else 0
        if len(kept) > cap:
            def _age(item):
                return float(item[1].get("last", 0) or 0)
            unparked = sorted((kv for kv in kept.items() if not kv[1].get("parked")),
                              key=_age)
            parked = sorted((kv for kv in kept.items() if kv[1].get("parked")),
                            key=_age)
            for key, _ in unparked:
                if len(kept) <= cap:
                    break
                kept.pop(key, None)
            for key, _ in parked:
                if len(kept) <= cap:
                    break
                kept.pop(key, None)
        ledger.clear()
        ledger.update(kept)
    except Exception as exc:
        _warn("prune failed (" + repr(exc) + ") — ledger left as-is")
    return ledger


@contextlib.contextmanager
def run_lock(path: Path | None = None):
    """Exclusive, non-blocking flock guarding the whole read-modify-write.

    Yields True when this process owns the ledger for the duration, False when
    another sweep already holds it.

    Why the lock spans the WHOLE sweep and not just write(): the sweep reads
    the ledger once, mutates it across every queue item, and writes once at the
    end. Two overlapping sweeps (the hourly timer plus an operator running
    --unstick by hand) both read the same snapshot and the second write wins
    outright -- the first sweep's blocklist adds vanish, and a `notified` flag
    cleared by the loser re-pages a park that was already announced. Locking
    write() alone cannot fix that; the race lives in the gap between read and
    write, which is minutes wide.

    Refusing to run is the correct response to contention, not waiting for it:
    the losing sweep would act on a stale snapshot and issue duplicate
    DELETE+blocklist calls. The next hourly run picks the work up.

    FAILS OPEN. Where fcntl is unavailable (Windows workstation, and the unit
    tests that run there) or the lock file cannot be created, this yields True
    with a stderr note rather than blocking the stuck-download repair. The
    unique-temp write in write() keeps that degraded path from publishing a
    torn file; what is lost is only last-writer-wins protection.
    """
    p = Path(path) if path is not None else lock_path()
    handle = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        handle = p.open("a+")
    except Exception as exc:
        _warn("could not open ledger lock at " + str(p) + " (" + repr(exc)
              + ") - proceeding unlocked")
        yield True
        return

    try:
        import fcntl
    except ImportError:
        # No fcntl (Windows). Documented fail-open; see docstring.
        try:
            yield True
        finally:
            handle.close()
        return

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        yield False
        return

    try:
        yield True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()
