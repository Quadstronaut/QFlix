"""lib/page_dedup.py — cross-run page suppression + audit-log field hygiene.

Extracted verbatim-in-behaviour from recovery.py's escalation-page cooldown
(2026-09-02, the 33-pings-in-ten-hours Plex outage). recovery.py now delegates.
Concurrency-hardened per council round 2: per-path threading lock (D1),
fcntl.flock across the read-modify-write (D2), unique mkstemp temp file (D1).

FAILS OPEN: every error path pages. A bug in the noise suppressor must never
be able to swallow "your media server is down". The worst case of failing
open is the storm we already had; the worst case of failing closed is
silence nobody notices.

THIS IS THE ONLY CROSS-RUN PAGE-DEDUP MECHANISM IN THE REPO. Do not add a
second cooldown or a second ledger format — extend this one.
`scripts/maint/lib/ledger.py` is the unrelated MONEY ledger; do not confuse
the two or extend one to cover the other's job.

Platform note: `fcntl` (LOCK_EX cross-process locking) exists only on POSIX.
Local dev on this repo happens on Windows, where `fcntl` is simply absent —
that is a PLATFORM CAPABILITY, not a failure, and is handled by degrading to
the in-process lock only (see _HAVE_FLOCK below). CI is ubuntu-latest and
production is Linux, so the flock path is exercised there. A `flock()` call
that raises on a platform that DOES have fcntl is the actual failure case,
and that one fails open with NO stamp written (see page_due docstring).
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
from typing import Optional

try:
    import fcntl  # type: ignore
    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - exercised on Windows dev boxes
    fcntl = None  # type: ignore
    _HAVE_FLOCK = False


DEFAULT_COOLDOWN_S: float = 24 * 3600

# ---------------------------------------------------------------------------
# D1 — per-ledger-path in-process lock registry (mirrors state.py:_RECORD_LOCK,
# but keyed per path since multiple ledgers — escalation-pages.json,
# arr-unstick-pages.json — can be in play in the same process at once).
# ---------------------------------------------------------------------------
_locks: dict[str, threading.Lock] = {}
_locks_mutex: threading.Lock = threading.Lock()

# Emitted at most once per process: the fcntl-absent branch is a platform
# capability gap (Windows dev box), not a per-call failure, so it does not
# deserve a stderr line on every single page_due() invocation.
_degradation_logged: bool = False
_degradation_logged_mutex: threading.Lock = threading.Lock()


def _log_degradation_once(label: str) -> None:
    global _degradation_logged
    with _degradation_logged_mutex:
        if _degradation_logged:
            return
        _degradation_logged = True
    sys.stderr.write(
        f"{label}: fcntl unavailable on this platform, "
        f"cross-process locking degraded to in-process only\n")


def _lock_for(ledger_path: str | os.PathLike) -> threading.Lock:
    key = os.path.abspath(os.fspath(ledger_path))
    with _locks_mutex:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


class _Flock:
    """fcntl.flock(LOCK_EX) on a sidecar '<ledger>.lock' file, held only
    across read-modify-write. Never unlinked (unlink races re-open the
    window between unlink and a concurrent open). Opened O_CREAT|O_RDWR
    mode 0o600."""

    def __init__(self, ledger_path: str | os.PathLike):
        self._lock_path = str(ledger_path) + ".lock"
        self._fd: Optional[int] = None

    def __enter__(self):
        Path(self._lock_path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # may raise OSError — caller handles
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        return False


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

def read_ledger(ledger_path: str | os.PathLike) -> dict:
    """Return the ledger as {key: unix_ts}. Missing, unreadable, non-JSON, or
    non-dict content all read as {} — never raises."""
    try:
        p = Path(ledger_path)
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _prune(ledger: dict, now: float, cooldown_s: float) -> dict:
    """Drop entries older than max(cooldown*2, cooldown+86400). Bounds
    unbounded ledger growth without changing any decision — a pruned key
    was already expired under the cooldown, so it would have paged again
    regardless of whether its old stamp stuck around. Skipped entirely for
    cooldown_s <= 0 (every call always pages there; pruning has no bearing)."""
    if cooldown_s <= 0:
        return ledger
    horizon = max(cooldown_s * 2, cooldown_s + 86400)
    out = {}
    for k, v in ledger.items():
        if isinstance(v, (int, float)) and (now - v) > horizon:
            continue
        out[k] = v
    return out


def write_ledger(ledger_path: str | os.PathLike, ledger: dict) -> None:
    """Atomic write: unique mkstemp temp file in the ledger's own directory,
    then os.replace. NEVER a fixed '<ledger-name>' + dot + 'tmp' path — that path racing
    under concurrent writers is exactly the bug this module exists to fix."""
    p = Path(ledger_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
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
# The decision
# ---------------------------------------------------------------------------

def page_due(
    key: str,
    *,
    ledger_path: str | os.PathLike,
    cooldown_s: float = DEFAULT_COOLDOWN_S,
    label: str = "page-dedup",
    now: Optional[float] = None,
) -> bool:
    """True iff the caller should page NOW for `key`. Stamps the ledger when
    returning True, so one call == one page decision.

    Decision, preserved exactly from recovery.py's original cooldown check:
        if isinstance(last, (int, float)) and 0 < (now - last) < cooldown_s:
            return False
        # else: stamp and return True
    A future-dated stamp (now - last <= 0) pages. A non-numeric stamp pages.
    cooldown_s <= 0 always pages and re-stamps.

    Locking: threading lock -> flock (if available) -> read -> decide ->
    write -> release flock -> release threading lock -> return. The lock is
    ALWAYS released before this function returns; the caller's Discord POST
    (if any) happens entirely outside this function.

    NEVER RAISES. Any exception anywhere writes
    f"{label} check failed, paging anyway: {exc!r}\\n" to stderr and returns
    True.
    """
    try:
        eff_now = time.time() if now is None else now
        with _lock_for(ledger_path):
            if _HAVE_FLOCK:
                try:
                    with _Flock(ledger_path):
                        return _decide_and_stamp(key, ledger_path, cooldown_s, eff_now)
                except OSError as exc:
                    # flock() itself failed on a platform that HAS fcntl —
                    # this is the true failure case: fail open, write NO
                    # stamp (an unlockable ledger must not be read-modify-
                    # written; another writer may be mid-flight).
                    sys.stderr.write(
                        f"{label}: flock unavailable, paging anyway "
                        f"(no stamp written): {exc!r}\n")
                    return True
            else:
                # No fcntl at all on this platform (Windows dev box). This
                # is a platform capability gap, not a runtime failure: the
                # in-process threading.Lock above still serialises same-
                # process callers, and the NORMAL cooldown decision still
                # applies (this call can legitimately return False).
                _log_degradation_once(label)
                return _decide_and_stamp(key, ledger_path, cooldown_s, eff_now)
    except Exception as exc:
        sys.stderr.write(f"{label} check failed, paging anyway: {exc!r}\n")
        return True


def _decide_and_stamp(key: str, ledger_path, cooldown_s: float, now: float) -> bool:
    ledger = read_ledger(ledger_path)
    last = ledger.get(key)
    if isinstance(last, (int, float)) and 0 < (now - last) < cooldown_s:
        return False
    ledger = _prune(ledger, now, cooldown_s)
    ledger[key] = now  # fresh stamp always survives pruning (age 0 < horizon)
    write_ledger(ledger_path, ledger)
    return True


def clear(key: str, *, ledger_path: str | os.PathLike) -> None:
    """Remove one key so the NEXT call pages immediately. Missing key and an
    unreadable ledger are silent no-ops (matches clear_escalation_page)."""
    try:
        with _lock_for(ledger_path):
            if _HAVE_FLOCK:
                try:
                    with _Flock(ledger_path):
                        _clear_locked(key, ledger_path)
                except OSError:
                    pass  # best-effort; a failed clear just means the next
                          # page_due call still sees the old stamp — it will
                          # still page once the cooldown naturally expires.
            else:
                _clear_locked(key, ledger_path)
    except Exception:
        pass


def _clear_locked(key: str, ledger_path) -> None:
    ledger = read_ledger(ledger_path)
    if ledger.pop(key, None) is not None:
        write_ledger(ledger_path, ledger)


# ---------------------------------------------------------------------------
# Audit-log field hygiene
# ---------------------------------------------------------------------------

def sanitize_log_field(value: object, *, max_len: int = 200) -> str:
    """Make `value` safe to join into one tab-delimited physical log line.

    Algorithm, in order:
      1. None -> ""; otherwise str(value).
      2. \\r, \\n, \\t each -> a single space (1:1, no collapsing — column
         offsets stay honest for anyone counting tabs).
      3. Every remaining Unicode category Cc or Cf char is DROPPED (NUL,
         ESC/ANSI introducers, DEL, ZWSP, BOM, U+202E RLO bidi-spoofing).
      4. Truncate to max_len chars; append "…" if truncated.
      5. If the result differs from the original str(value), append
         " [sanitized]" — silent mutation of an audit field is itself a
         trust defect.

    The *arr queue `title` field is attacker-controlled (anyone can upload a
    release to a public indexer) and this is the only durable record of a
    suppressed run, so a forgeable trail here is worse than no trail.
    """
    original = "" if value is None else str(value)
    working = original
    for ch in ("\r", "\n", "\t"):
        working = working.replace(ch, " ")
    working = "".join(
        c for c in working
        if unicodedata.category(c) not in ("Cc", "Cf")
    )
    truncated = False
    if len(working) > max_len:
        working = working[:max_len] + "…"
        truncated = True
    result = working
    if truncated or result != original:
        result += " [sanitized]"
    return result
