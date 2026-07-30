"""A recovery worker must release the SAME semaphore it acquired.

WHY: `_worker`'s finally used to call `_RECOVERY_SEMAPHORE.release()`, reading
the module global at RELEASE time rather than the object acquired at START time.
Anything that rebinds that global while a worker is in flight then makes the
worker release a semaphore it never acquired, and BoundedSemaphore raises
`ValueError: Semaphore released too many times`.

test_kuma.py:489-518 does exactly that rebind -- it swaps in a
BoundedSemaphore(1) to prove the cap drops a second request, then restores the
original in a `finally` that races the still-running worker. The suite failed
roughly 1 run in 3 because of it, which made the whole gate untrustworthy: a
flaky test teaches you to ignore red.

Fixed by binding once (`sem = _RECOVERY_SEMAPHORE`) and releasing `sem`.
Asymmetric acquire/release is a defect independent of who triggers it.
"""
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "maint"))

from lib import recovery as recovery_module  # noqa: E402


class _App:
    def __init__(self, name):
        self.name = name
        self.kuma_monitor = name
        self.parked = False
        self.recovery_attempts = 1


@pytest.fixture(autouse=True)
def _clean_state():
    recovery_module._in_flight.clear()
    yield
    recovery_module._in_flight.clear()


def test_rebinding_the_global_mid_flight_does_not_over_release(monkeypatch):
    """The exact race in test_kuma.py, reproduced deterministically.

    Against the pre-fix code this raises
    `ValueError: Semaphore released too many times` on the worker thread and
    leaves the ORIGINAL semaphore over-released, which then corrupts every
    later test in the same process.
    """
    original = recovery_module._RECOVERY_SEMAPHORE
    patched = threading.BoundedSemaphore(1)
    recovery_module._RECOVERY_SEMAPHORE = patched

    in_worker = threading.Event()
    may_finish = threading.Event()
    errors = []

    def fake_run(app_name, manifest=None):
        in_worker.set()
        may_finish.wait(timeout=5)
        return {"event": "recovered"}

    monkeypatch.setattr(recovery_module, "run", fake_run)
    monkeypatch.setattr(recovery_module, "_is_recoverable", lambda app: True)
    monkeypatch.setattr(recovery_module, "is_permanently_failed", lambda name: False)

    def catch(args):
        errors.append(args.exc_value)

    old_hook = threading.excepthook
    threading.excepthook = catch
    try:
        assert recovery_module.trigger_async(_App("listmonk")) == "started"
        assert in_worker.wait(timeout=5), "worker never started"

        # The race: restore the global while the worker still holds `patched`.
        recovery_module._RECOVERY_SEMAPHORE = original

        may_finish.set()
        deadline = time.time() + 5
        while "listmonk" in recovery_module._in_flight and time.time() < deadline:
            time.sleep(0.01)
    finally:
        threading.excepthook = old_hook
        recovery_module._RECOVERY_SEMAPHORE = original

    assert not errors, f"worker raised on release: {errors}"

    # And the original must be untouched -- fully available, not over-released.
    acquired = [original.acquire(blocking=False) for _ in range(5)]
    assert all(acquired), "original semaphore lost capacity to a stray release"
    for _ in range(5):
        original.release()


def test_release_uses_the_bound_local_not_the_module_global():
    """Structural pin: the fix is one word and trivially revertible."""
    src = (REPO / "scripts" / "maint" / "lib" / "recovery.py").read_text(encoding="utf-8")
    body = src[src.index("def trigger_async("):src.index("def _load_default_manifest(")]
    assert "sem = _RECOVERY_SEMAPHORE" in body, "the semaphore is no longer bound once"
    assert "sem.release()" in body, "release no longer uses the bound local"
    assert "_RECOVERY_SEMAPHORE.release()" not in body, \
        "release reverted to reading the module global — the rebind race is back"


def test_cap_still_drops_the_second_concurrent_recovery(monkeypatch):
    """The behaviour the rebind exists to prove must still hold."""
    original = recovery_module._RECOVERY_SEMAPHORE
    recovery_module._RECOVERY_SEMAPHORE = threading.BoundedSemaphore(1)
    may_finish = threading.Event()
    started = threading.Event()

    def fake_run(app_name, manifest=None):
        started.set()
        may_finish.wait(timeout=5)
        return {"event": "recovered"}

    monkeypatch.setattr(recovery_module, "run", fake_run)
    monkeypatch.setattr(recovery_module, "_is_recoverable", lambda app: True)
    monkeypatch.setattr(recovery_module, "is_permanently_failed", lambda name: False)
    try:
        assert recovery_module.trigger_async(_App("alpha")) == "started"
        assert started.wait(timeout=5)
        assert recovery_module.trigger_async(_App("beta")) == "cap_exceeded"
        may_finish.set()
        deadline = time.time() + 5
        while recovery_module._in_flight and time.time() < deadline:
            time.sleep(0.01)
    finally:
        recovery_module._RECOVERY_SEMAPHORE = original
