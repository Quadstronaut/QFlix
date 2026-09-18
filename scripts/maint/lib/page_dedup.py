"""lib/page_dedup.py — cross-run page suppression + audit-log field hygiene.

Extracted verbatim-in-behaviour from recovery.py's escalation-page cooldown
(2026-09-02, the 33-pings-in-ten-hours Plex outage). recovery.py now delegates.
Concurrency-hardened per council round 2: per-path threading lock (D1),
fcntl.flock across the read-modify-write (D2), unique mkstemp temp file (D1).
FAILS OPEN: every error path pages. A bug in the noise suppressor must never
be able to swallow "your media server is down".

Locking order (never violated): threading lock -> flock -> read -> decide ->
write -> release flock -> release threading lock -> return. The lock is NEVER
held across a network call (the Discord POST happens in the caller, outside
this module entirely).

Platform split (arbiter decision, council round 2): `fcntl` absent at import
(non-POSIX — this repo's local dev is Windows) is a PLATFORM CAPABILITY, not
a failure — degrade to the in-process lock only, keep making the normal
cooldown decision, note the degradation once per ledger path per process.
`fcntl` present but `flock()` itself raising IS the failure case the brief
means: fail open, return True, and — critically — do NOT stamp the ledger,
because a write made without holding the cross-process lock is exactly the
race this module exists to close.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Union

try:
    import fcntl  # type: ignore
    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - exercised on Windows dev, not CI
    fcntl = None  # type: ignore
    _HAVE_FLOCK = False

DEFAULT_COOLDOWN_S: float = 24 * 3600

_PathLike = Union[str, "os.PathLike[str]"]

# ---------------------------------------------------------------------------
# Per-ledger-path lock registry (D1) — scripts/maint/lib/state.py:63's
# _RECORD_LOCK pattern, generalized to one lock per distinct ledger path so
# unrelated ledgers (escalation-pages.json vs arr-unstick-pages.json) never
# contend on the same mutex.
# ---------------------------------------------------------------------------
_locks: dict[str, threading.Lock] = {}
_locks_mutex: threading.Lock = threading.Lock()

# One-per-process degradation notice, keyed by ledger path, so a platform
# without fcntl doesn't spam stderr on every call.
_degradation_warned: set[str] = set()
_degradation_warned_mutex: threading.Lock = threading.Lock()


def _abspath(ledger_path: _PathLike) -> str:
    return os.path.abspath(os.fspath(ledger_path))


def _get_lock(ledger_path: _PathLike) -> threading.Lock:
    key = _abspath(ledger_path)
    with _locks_mutex:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _warn_degraded_once(ledger_path: _PathLike, label: str) -> None:
    key = _abspath(ledger_path)
    with _degradation_warned_mutex:
        if key in _degradation_warned:
            return
        _degradation_warned.add(key)
    sys.stderr.write(
        f"{label}: fcntl unavailable on this platform — cross-process locking "
        f"disabled, falling back to in-process locking only for {ledger_path} "
        f"(this is expected on non-POSIX dev machines; production is Linux)\n"
    )


# ---------------------------------------------------------------------------
# Ledger I/O — read never raises, write is mkstemp+os.replace (D1 atomicity).
# ---------------------------------------------------------------------------

def read_ledger(ledger_path: _PathLike) -> dict:
    """Ledger as {key: unix_ts_of_last_page}. Missing, unreadable, non-JSON,
    or non-dict content all read as empty — the safe direction, since an
    empty ledger means "page now", never "stay silent"."""
    try:
        with Path(ledger_path).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_ledger(ledger_path: _PathLike, ledger: dict) -> None:
    """Atomic write: mkstemp in the ledger's own directory, then os.replace.
    No fixed `<name>.json.tmp` path — that was the pre-council bug (two
    concurrent writers could open the SAME fixed temp file and one write
    is lost silently)."""
    p = Path(ledger_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=p.parent, prefix=p.name + ".", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2)
        if os.name == "posix":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Cross-process locking (D2) — sidecar "<ledger>.lock", flock LOCK_EX, never
# unlinked (unlinking races a concurrent opener re-creating the window).
# ---------------------------------------------------------------------------

def _sidecar_path(ledger_path: _PathLike) -> str:
    return f"{os.fspath(ledger_path)}.lock"


def _acquire_flock(ledger_path: _PathLike) -> int:
    sidecar = _sidecar_path(ledger_path)
    Path(sidecar).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(sidecar, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # type: ignore[union-attr]
    except OSError:
        os.close(fd)
        raise
    return fd


def _release_flock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The whole contract
# ---------------------------------------------------------------------------

def page_due(
    key: str,
    *,
    ledger_path: _PathLike,
    cooldown_s: float = DEFAULT_COOLDOWN_S,
    label: str = "page-dedup",
    now: float | None = None,
) -> bool:
    """True iff the caller should page `key` NOW. Stamps the ledger when it
    returns True, so one call == one page decision. Never raises."""
    try:
        return _page_due_locked(
            key, ledger_path=ledger_path, cooldown_s=cooldown_s,
            label=label, now=now,
        )
    except Exception as exc:
        sys.stderr.write(f"{label} check failed, paging anyway: {exc!r}\n")
        return True


def _page_due_locked(key: str, *, ledger_path: _PathLike, cooldown_s: float,
                      label: str, now: float | None) -> bool:
    now_ts = time.time() if now is None else now
    tlock = _get_lock(ledger_path)
    with tlock:
        lock_fd: int | None = None
        if _HAVE_FLOCK:
            try:
                lock_fd = _acquire_flock(ledger_path)
            except OSError as exc:
                # The failure case the brief means: fcntl exists but flock()
                # itself raised. Fail open WITHOUT stamping — a write made
                # without the cross-process lock is exactly the race this
                # module exists to close.
                sys.stderr.write(
                    f"{label}: flock failed on {_sidecar_path(ledger_path)}, "
                    f"paging without stamping the ledger: {exc!r}\n"
                )
                return True
        else:
            _warn_degraded_once(ledger_path, label)

        try:
            ledger = read_ledger(ledger_path)
            last = ledger.get(key)
            due = True
            if isinstance(last, (int, float)) and 0 < (now_ts - last) < cooldown_s:
                due = False
            if due:
                ledger[key] = now_ts
                if cooldown_s > 0:
                    ledger = _prune(ledger, now_ts, cooldown_s)
                write_ledger(ledger_path, ledger)
            return due
        finally:
            if lock_fd is not None:
                _release_flock(lock_fd)


def _prune(ledger: dict, now: float, cooldown_s: float) -> dict:
    """Drop entries older than max(cooldown*2, cooldown+86400). A pruned key
    was already expired -- this bounds ledger growth without changing any
    decision. Never called when cooldown_s <= 0 (page_due guards the call)."""
    threshold = max(cooldown_s * 2, cooldown_s + 86400)
    out: dict = {}
    for k, v in ledger.items():
        if isinstance(v, (int, float)) and (now - v) >= threshold:
            continue
        out[k] = v
    return out


def clear(key: str, *, ledger_path: _PathLike) -> None:
    """Remove one key under the same locks page_due uses. Missing key and
    an unreadable ledger are silent no-ops — matches clear_escalation_page's
    existing behaviour. Best-effort; never raises."""
    try:
        tlock = _get_lock(ledger_path)
        with tlock:
            lock_fd: int | None = None
            if _HAVE_FLOCK:
                try:
                    lock_fd = _acquire_flock(ledger_path)
                except OSError:
                    return  # best-effort: can't safely RMW without the lock
            else:
                _warn_degraded_once(ledger_path, "page_dedup.clear")
            try:
                ledger = read_ledger(ledger_path)
                if ledger.pop(key, None) is not None:
                    write_ledger(ledger_path, ledger)
            finally:
                if lock_fd is not None:
                    _release_flock(lock_fd)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Audit-log field hygiene (D4)
# ---------------------------------------------------------------------------

def sanitize_log_field(value: object, *, max_len: int = 200) -> str:
    """Make an arbitrary (possibly attacker-chosen) value safe to embed as
    ONE field of a tab-delimited audit log line.

    Order matters (test-asserted):
      1. None -> ""; else str(value).
      2. CR/LF/TAB -> single space each (1:1, no collapsing).
      3. Every remaining Unicode category Cc/Cf character dropped (NUL, ESC,
         DEL, ZWSP, BOM, RLO bidi-spoofing — the same attack as newline
         forgery, just rendered instead of parsed).
      4. Truncate to max_len chars, appending an ellipsis if truncated.
      5. If the result differs from the original str(value) at all, append
         " [sanitized]" — silent mutation of an audit field is itself a
         trust defect.

    Postcondition: no \\r, \\n, \\t, no Cc/Cf survivor, len <= max_len + 13.
    """
    original = "" if value is None else str(value)
    s = original
    for ch in ("\r", "\n", "\t"):
        s = s.replace(ch, " ")
    s = "".join(c for c in s if unicodedata.category(c) not in ("Cc", "Cf"))
    if len(s) > max_len:
        s = s[:max_len] + "…"
    if s != original:
        s = s + " [sanitized]"
    return s
