"""lib/page_dedup.py — cross-run page suppression + audit-log field hygiene.

Extracted verbatim-in-behaviour from recovery.py's escalation-page cooldown
(2026-09-02, the 33-pings-in-ten-hours Plex outage). recovery.py now delegates.
Concurrency-hardened per council round 2: per-path threading lock, fcntl.flock
across the read-modify-write, unique mkstemp temp file.

FAILS OPEN: every error path pages. A bug in the noise suppressor must never
be able to swallow "your media server is down". The worst case of failing open
is the storm we already had; the worst case of failing closed is silence
nobody notices.

WALL-CLOCK IN A FILE, not monotonic in memory: the daemon restarts (deploy,
upgrade, OOM) and an in-memory cooldown would reset with it — the storm would
come straight back on the first restart, exactly when an operator is least
able to tell a new fault from an old one.

THE clear() ASYMMETRY, deliberate and load-bearing. recovery.py calls clear()
on every successful probe because an app that fails, recovers, and fails again
an hour later is news both times. arr-housekeeping's unstick loop does NOT,
and must not: an item is unstuck, the *arr re-searches, the re-grab sticks
again — clearing on success would re-page that identical loop every hour,
which IS the storm being fixed. The accepted limit: the same series re-sticking
inside its 24h window stays suppressed, and ~/.opt/maint/arr-unstick.log is
where that run remains visible. A genuinely NEW condition still pages on the
very next run, because its key is simply absent from the ledger.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import threading
import time
import unicodedata

try:  # POSIX only. Absence is a PLATFORM CAPABILITY, not a failure — see below.
    import fcntl
    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - exercised on Windows dev workstations
    fcntl = None  # type: ignore[assignment]
    _HAVE_FLOCK = False

DEFAULT_COOLDOWN_S: float = 24 * 3600

# ---------------------------------------------------------------------------
# In-process serialisation (one lock per ledger path)
# ---------------------------------------------------------------------------
# Same shape as state.py's _RECORD_LOCK: the on-disk write is already atomic,
# but page_due() is a read-modify-write and two threads interleaving it both
# observe "no stamp" and both page. The daemon runs a ThreadingHTTPServer
# webhook alongside the pusher thread, so that race is real, not theoretical.
#
# Two spellings of one path (symlink, relative vs absolute) get two locks;
# abspath() collapses the common case and flock still covers the rest.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_MUTEX = threading.Lock()

# One degradation note per process, not one per call — a per-call note would
# itself become the noise this module exists to remove.
_FLOCK_NOTE_EMITTED = False


def _lock_for(ledger_path) -> threading.Lock:
    key = os.path.abspath(os.fspath(ledger_path))
    with _LOCKS_MUTEX:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


class _FlockUnavailable(Exception):
    """flock() itself failed at runtime (fcntl present). Distinct from the
    platform simply not having fcntl — the two get different handling."""


@contextlib.contextmanager
def _cross_process_lock(ledger_path, label: str):
    """LOCK_EX on a sidecar '<ledger>.lock', held ONLY across the
    read-modify-write. Never held across a Discord POST.

    The sidecar is never unlinked: unlinking races a concurrent open() and
    re-opens the very window the lock closes.
    """
    global _FLOCK_NOTE_EMITTED
    if not _HAVE_FLOCK:
        # PLATFORM ABSENCE. Proceed with the in-process lock only and make the
        # normal cooldown decision. Failing open here would page on every call
        # on a machine that has no concurrency problem to protect against.
        if not _FLOCK_NOTE_EMITTED:
            _FLOCK_NOTE_EMITTED = True
            sys.stderr.write(
                label + ": fcntl unavailable on this platform, cross-process "
                "page dedup is degraded to in-process only\n")
        yield
        return

    lock_path = os.fspath(ledger_path) + ".lock"
    fd = None
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except Exception as exc:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        # RUNTIME FAILURE. This is the branch the fail-open rule means: we
        # cannot serialise, so we must not read-modify-write the ledger.
        raise _FlockUnavailable(repr(exc)) from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

def read_ledger(ledger_path) -> dict:
    """Ledger as {key: unix_ts_of_last_page}. Missing, unreadable, non-JSON
    and non-dict all read as empty — see page_due for why that direction is
    the safe one. Never raises."""
    try:
        with open(os.fspath(ledger_path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_ledger(ledger_path, ledger: dict) -> None:
    """Atomic replace via a UNIQUE temp file in the ledger's own directory.

    The old fixed-suffix temp name is forbidden: two processes writing the
    same predictable temp path interleave their bytes, and os.replace() then
    publishes the interleaving as the ledger.
    """
    path = os.fspath(ledger_path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _prune(ledger: dict, cooldown_s: float, now: float) -> dict:
    """Drop stamps so old they can no longer suppress anything.

    Bounds an otherwise unbounded file without changing ANY decision: a pruned
    key was already past its window, so its next page_due() returns True
    either way. Skipped when cooldown_s <= 0 (that mode always pages, so
    "expired" is meaningless and pruning would just churn the file).
    """
    if cooldown_s <= 0:
        return ledger
    horizon = now - max(cooldown_s * 2, cooldown_s + 86400)
    return {
        k: v for k, v in ledger.items()
        if not (isinstance(v, (int, float)) and v < horizon)
    }


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def page_due(
    key: str,
    *,
    ledger_path,
    cooldown_s: float = DEFAULT_COOLDOWN_S,
    label: str = "page-dedup",
    now: float | None = None,
) -> bool:
    """True iff `key` should page the operator NOW.

    Stamps the ledger when it returns True, so one call == one page decision.
    Callers must not call it twice for one message.

    The decision expression is preserved byte-for-byte from recovery.py's
    original, and its edge cases are contract, not accident:
      - a FUTURE-dated stamp (now - last <= 0) pages. Clock skew must not mute.
      - a non-numeric stamp pages.
      - cooldown_s <= 0 always pages and re-stamps.

    `now` exists for tests. Production never passes it.

    Locks are released before this returns. The Discord POST happens in the
    caller, outside every lock.
    """
    try:
        ts = time.time() if now is None else float(now)
        with _lock_for(ledger_path):
            try:
                with _cross_process_lock(ledger_path, label):
                    ledger = read_ledger(ledger_path)
                    last = ledger.get(key)
                    if isinstance(last, (int, float)) and 0 < (ts - last) < cooldown_s:
                        return False
                    ledger = _prune(ledger, cooldown_s, ts)
                    ledger[key] = ts
                    write_ledger(ledger_path, ledger)
                    return True
            except _FlockUnavailable as exc:
                # Fail open WITHOUT stamping: an unlockable ledger must not be
                # read-modify-written, or we publish a decision we could not
                # serialise. Paging twice beats corrupting the record.
                sys.stderr.write(
                    label + " lock failed, paging anyway (no stamp written): "
                    + str(exc) + "\n")
                return True
    except Exception as _exc:
        sys.stderr.write(
            label + " check failed, paging anyway: " + repr(_exc) + "\n")
        return True


def clear(key: str, *, ledger_path) -> None:
    """Drop `key`'s stamp so the NEXT occurrence pages immediately.

    For per-OUTAGE callers only (recovery.py). See the module docstring for
    why the unstick sweep deliberately does not call this. Missing key and
    unreadable ledger are no-ops."""
    try:
        with _lock_for(ledger_path):
            with _cross_process_lock(ledger_path, "page-dedup clear"):
                ledger = read_ledger(ledger_path)
                if ledger.pop(key, None) is not None:
                    write_ledger(ledger_path, ledger)
    except Exception as exc:
        sys.stderr.write("page-dedup clear failed (non-fatal): " + repr(exc) + "\n")


# ---------------------------------------------------------------------------
# Audit-log field hygiene
# ---------------------------------------------------------------------------

_SPACED = {"\r", "\n", "\t"}
_SUFFIX = " [sanitized]"


def sanitize_log_field(value: object, *, max_len: int = 200) -> str:
    """Make `value` safe to place in one tab-delimited field of one log line.

    The *arr queue `title` is attacker-chosen — whoever uploads a release to a
    public indexer, authenticated nowhere — and ~/.opt/maint/arr-unstick.log is
    the ONLY record of a suppressed run. A forgeable audit trail is worse than
    no audit trail, so:

      1. None -> ""; everything else -> str(value).
      2. CR / LF / TAB each become ONE space (1:1, no collapsing — column
         offsets in the original stay honest).
      3. every remaining Cc/Cf character is DROPPED. That is NUL, DEL, the
         ANSI ESC introducer, U+200B ZWSP, U+FEFF BOM, and U+202E RLO —
         bidi-reordering a rendered log line is the same attack as newline
         forgery, just aimed at the human instead of the parser.
      4. truncate to max_len, appending '…' when it bit.
      5. append ' [sanitized]' whenever anything above changed the value.
         Silent mutation of an audit field is itself a trust defect.
    """
    original = "" if value is None else str(value)
    kept = []
    for ch in original:
        if ch in _SPACED:
            kept.append(" ")
            continue
        if unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        kept.append(ch)
    out = "".join(kept)
    if len(out) > max_len:
        out = out[:max_len] + "…"
    if out != original:
        out += _SUFFIX
    return out
