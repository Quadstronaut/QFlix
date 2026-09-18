"""tests/unit/test_page_dedup.py — lib.page_dedup: cross-run page suppression
+ audit-log field hygiene (council round 2, 2026-09-17).

FAIL-OPEN is the load-bearing invariant throughout: every error path here
(corrupt ledger, unwritable dir, failed lock, contention) must return True,
never False. The two concurrency reproductions (16 threads / 8 processes)
are the actual regression tests for the storm this module closes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import lib.page_dedup as page_dedup

MAINT_DIR = str(Path(__file__).resolve().parents[2] / "scripts" / "maint")


# ---------------------------------------------------------------------------
# Basic decision semantics
# ---------------------------------------------------------------------------

def test_first_call_pages_and_stamps(tmp_path):
    ledger = tmp_path / "pages.json"
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True
    data = json.loads(ledger.read_text())
    assert isinstance(data["k"], float)


def test_second_call_within_window_is_suppressed(tmp_path):
    ledger = tmp_path / "pages.json"
    now = time.time()
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600, now=now) is True
    stamp_before = json.loads(ledger.read_text())["k"]
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600, now=now + 1) is False
    stamp_after = json.loads(ledger.read_text())["k"]
    assert stamp_before == stamp_after


def test_new_key_pages_immediately(tmp_path):
    ledger = tmp_path / "pages.json"
    now = time.time()
    page_dedup.page_due("k1", ledger_path=ledger, cooldown_s=3600, now=now)
    assert page_dedup.page_due("k2", ledger_path=ledger, cooldown_s=3600, now=now) is True


def test_window_expiry_pages_again(tmp_path):
    ledger = tmp_path / "pages.json"
    now = time.time()
    page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600, now=now)
    assert page_dedup.page_due(
        "k", ledger_path=ledger, cooldown_s=3600, now=now + 3600 + 1) is True


def test_future_stamp_pages(tmp_path):
    """Clock skew must not mute — a stamp AHEAD of now is not a valid cooldown."""
    ledger = tmp_path / "pages.json"
    now = time.time()
    ledger.write_text(json.dumps({"k": now + 10_000}))
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600, now=now) is True


def test_non_numeric_stamp_pages(tmp_path):
    ledger = tmp_path / "pages.json"
    ledger.write_text(json.dumps({"k": "yesterday"}))
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True


def test_zero_cooldown_always_pages(tmp_path):
    ledger = tmp_path / "pages.json"
    now = time.time()
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=0.0, now=now) is True
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=0.0, now=now) is True


def test_clear_makes_next_call_page(tmp_path):
    ledger = tmp_path / "pages.json"
    now = time.time()
    page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600, now=now)
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600, now=now + 1) is False
    page_dedup.clear("k", ledger_path=ledger)
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600, now=now + 2) is True


def test_clear_missing_key_is_a_silent_noop(tmp_path):
    ledger = tmp_path / "pages.json"
    page_dedup.clear("nope", ledger_path=ledger)  # must not raise
    assert page_dedup.read_ledger(ledger) == {}


def test_corrupt_ledger_fails_open_and_repairs(tmp_path):
    ledger = tmp_path / "pages.json"
    ledger.write_text("{not json")
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True
    # Fails open AND repairs the ledger on the way out.
    data = json.loads(ledger.read_text())
    assert "k" in data


@pytest.mark.skipif(sys.platform.startswith("win"), reason="chmod perms are POSIX-only")
def test_unwritable_dir_fails_open(tmp_path, capsys):
    unwritable = tmp_path / "locked"
    unwritable.mkdir()
    ledger = unwritable / "pages.json"
    unwritable.chmod(0o500)
    try:
        result = page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600,
                                      label="test-label")
    finally:
        unwritable.chmod(0o700)
    assert result is True
    captured = capsys.readouterr()
    assert "paging anyway" in captured.err


# ---------------------------------------------------------------------------
# D1 — in-process concurrency (16 threads, one key, exactly one True)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trial", range(15))
def test_sixteen_threads_one_key_page_once(tmp_path, trial):
    ledger = tmp_path / f"pages-{trial}.json"
    barrier = threading.Barrier(16)
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        r = page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 16
    assert sum(1 for r in results if r) == 1


def test_no_tmp_residue_after_thread_storm(tmp_path):
    ledger = tmp_path / "pages.json"
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert list(tmp_path.glob("*.tmp")) == []
    assert not (tmp_path / "pages.json.tmp").exists()
    json.loads(ledger.read_text())  # parses cleanly


# ---------------------------------------------------------------------------
# D2 — cross-process concurrency (8 processes, one key, exactly one True)
# ---------------------------------------------------------------------------

_EIGHT_PROC_SRC = """
import sys, time
sys.path.insert(0, sys.argv[3])
from lib import page_dedup
ledger = sys.argv[1]
start = float(sys.argv[2])
while time.time() < start:
    pass
result = page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600)
print(1 if result else 0)
"""


def test_eight_processes_one_key_page_once(tmp_path):
    pytest.importorskip(
        "fcntl",
        reason="cross-process flock is Linux-only; CI is ubuntu-latest, "
               "production is Linux",
    )
    ledger = tmp_path / "pages.json"
    start = time.time() + 1.0
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _EIGHT_PROC_SRC, str(ledger), str(start), MAINT_DIR],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(8)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=20)
        outs.append((p.returncode, out.strip()))

    assert all(rc == 0 for rc, _ in outs), outs
    results = [int(o) for _, o in outs]
    assert sum(results) == 1


def test_flock_failure_fails_open_without_stamping(tmp_path, monkeypatch):
    ledger = tmp_path / "pages.json"

    class _FailingFcntl:
        LOCK_EX = 2
        LOCK_UN = 8

        @staticmethod
        def flock(fd, op):
            if op == _FailingFcntl.LOCK_EX:
                raise OSError("simulated flock failure")

    monkeypatch.setattr(page_dedup, "_HAVE_FLOCK", True)
    monkeypatch.setattr(page_dedup, "fcntl", _FailingFcntl)

    result = page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600)
    assert result is True
    assert page_dedup.read_ledger(ledger) == {}


def test_flock_absent_platform_degrades_not_fails_open(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "pages.json"
    monkeypatch.setattr(page_dedup, "_HAVE_FLOCK", False)

    first = page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600)
    second = page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600)

    assert first is True
    assert second is False  # normal cooldown decision still applies
    captured = capsys.readouterr()
    assert "fcntl unavailable" in captured.err


# ---------------------------------------------------------------------------
# Atomicity + pruning
# ---------------------------------------------------------------------------

def test_write_ledger_uses_unique_temp(tmp_path, monkeypatch):
    ledger = tmp_path / "pages.json"
    calls = []
    orig_mkstemp = tempfile.mkstemp

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return orig_mkstemp(*args, **kwargs)

    monkeypatch.setattr(page_dedup.tempfile, "mkstemp", spy)
    page_dedup.write_ledger(ledger, {"a": 1.0})

    assert len(calls) == 1
    assert calls[0]["dir"] == ledger.parent


def test_expired_keys_pruned_on_write(tmp_path):
    ledger = tmp_path / "pages.json"
    now = time.time()
    cooldown = 86400.0
    old_ts = now - (cooldown * 3)  # 3 days ago, well past the 2-day prune bar
    page_dedup.write_ledger(ledger, {"old": old_ts, "recent": now - 10})

    page_dedup.page_due("new-key", ledger_path=ledger, cooldown_s=cooldown, now=now)

    data = json.loads(ledger.read_text())
    assert "old" not in data
    assert "recent" in data
    assert "new-key" in data


# ---------------------------------------------------------------------------
# D4 — sanitize_log_field
# ---------------------------------------------------------------------------

def test_sanitize_strips_crlf_tab_and_controls():
    import unicodedata
    hostile = "a\rb\nc\td\x00e\x1b[31mf‮g"
    result = page_dedup.sanitize_log_field(hostile)
    assert "\r" not in result and "\n" not in result and "\t" not in result
    assert not any(unicodedata.category(c) in ("Cc", "Cf") for c in result)
    assert result.endswith(" [sanitized]")


def test_sanitize_truncates():
    result = page_dedup.sanitize_log_field("x" * 5000, max_len=200)
    assert len(result) <= 200 + 13
    assert "…" in result


def test_sanitize_clean_input_is_identity():
    clean = "Yellowjackets S03E01"
    assert page_dedup.sanitize_log_field(clean) == clean
    assert not page_dedup.sanitize_log_field(clean).endswith("[sanitized]")


def test_sanitize_none_and_non_str():
    assert page_dedup.sanitize_log_field(None) == ""
    assert page_dedup.sanitize_log_field(12345) == "12345"
