"""tests/unit/test_page_dedup.py — lib/page_dedup.py: the single cross-run
page-dedup mechanism + audit-log field sanitizer (council round 2, cluster C).

Covers: D1 in-process concurrency (16-thread storm), D2 cross-process
concurrency (8-process storm, POSIX-only), D3-adjacent decision-expression
edge cases (future/non-numeric stamps, zero cooldown), atomicity (no fixed
.tmp path, no residue after a storm), the flock present-but-failing vs
fcntl-absent-platform split, and the log-injection sanitizer.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from lib import page_dedup


# ---------------------------------------------------------------------------
# Basic decision expression
# ---------------------------------------------------------------------------

def test_first_call_pages_and_stamps(tmp_path):
    ledger = tmp_path / "pages.json"
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True
    data = json.loads(ledger.read_text())
    assert isinstance(data["k"], (int, float))


def test_second_call_within_window_is_suppressed(tmp_path):
    ledger = tmp_path / "pages.json"
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True
    before = json.loads(ledger.read_text())["k"]
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is False
    after = json.loads(ledger.read_text())["k"]
    assert before == after


def test_new_key_pages_immediately(tmp_path):
    ledger = tmp_path / "pages.json"
    assert page_dedup.page_due("k1", ledger_path=ledger, cooldown_s=3600) is True
    assert page_dedup.page_due("k2", ledger_path=ledger, cooldown_s=3600) is True


def test_window_expiry_pages_again(tmp_path):
    ledger = tmp_path / "pages.json"
    t0 = 1_000_000.0
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=100, now=t0) is True
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=100, now=t0 + 101) is True


def test_future_stamp_pages(tmp_path):
    ledger = tmp_path / "pages.json"
    ledger.write_text(json.dumps({"k": time.time() + 10_000}))
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True


def test_non_numeric_stamp_pages(tmp_path):
    ledger = tmp_path / "pages.json"
    ledger.write_text(json.dumps({"k": "yesterday"}))
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True


def test_zero_cooldown_always_pages(tmp_path):
    ledger = tmp_path / "pages.json"
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=0.0) is True
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=0.0) is True


def test_clear_makes_next_call_page(tmp_path):
    ledger = tmp_path / "pages.json"
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is False
    page_dedup.clear("k", ledger_path=ledger)
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True


def test_corrupt_ledger_fails_open_and_repairs(tmp_path):
    ledger = tmp_path / "pages.json"
    ledger.write_text("{not json")
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True
    data = json.loads(ledger.read_text())
    assert "k" in data


@pytest.mark.skipif(os.name != "posix", reason="chmod 0o500 permission test is POSIX-only")
def test_unwritable_dir_fails_open(tmp_path, capsys):
    unwritable = tmp_path / "locked"
    unwritable.mkdir()
    ledger = unwritable / "pages.json"
    os.chmod(unwritable, 0o500)
    try:
        assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True
    finally:
        os.chmod(unwritable, 0o700)
    err = capsys.readouterr().err
    assert "paging anyway" in err


# ---------------------------------------------------------------------------
# D1 — in-process concurrency
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
        t.join()

    assert sum(results) == 1, f"trial {trial}: expected exactly 1 True, got {sum(results)}"


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
        t.join()

    assert list(tmp_path.glob("*.tmp")) == []
    assert not (tmp_path / "pages.json.tmp").exists()
    json.loads(ledger.read_text())  # must still parse


# ---------------------------------------------------------------------------
# D2 — cross-process concurrency (POSIX only: cross-process flock needs fcntl)
# ---------------------------------------------------------------------------

_CHILD_SRC = """
import sys, time
sys.path.insert(0, sys.argv[3])
from lib import page_dedup
ledger = sys.argv[1]
start = float(sys.argv[2])
while time.time() < start:
    pass
r = page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600)
print(1 if r else 0)
"""


def test_eight_processes_one_key_page_once(tmp_path):
    pytest.importorskip("fcntl", reason="cross-process flock is Linux-only; "
                                        "CI is ubuntu-latest, production is Linux")
    ledger = tmp_path / "pages.json"
    scripts_maint = str(Path(__file__).resolve().parents[2] / "scripts" / "maint")
    start = time.time() + 0.5
    procs = []
    for _ in range(8):
        p = subprocess.Popen(
            [sys.executable, "-c", _CHILD_SRC, str(ledger), str(start), scripts_maint],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        procs.append(p)
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
        outs.append(int(out.strip()))
    assert sum(outs) == 1, f"expected exactly 1 True across 8 processes, got {outs}"


def test_flock_failure_fails_open_without_stamping(tmp_path, monkeypatch, capsys):
    if not page_dedup._HAVE_FLOCK:
        pytest.skip("fcntl absent on this platform")
    ledger = tmp_path / "pages.json"

    def _boom(*a, **kw):
        raise OSError("simulated flock failure")

    monkeypatch.setattr(page_dedup.fcntl, "flock", _boom)
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600) is True
    assert "k" not in page_dedup.read_ledger(ledger)
    assert "paging anyway" in capsys.readouterr().err


def test_flock_absent_platform_degrades_not_fails_open(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "pages.json"
    monkeypatch.setattr(page_dedup, "_HAVE_FLOCK", False)
    monkeypatch.setattr(page_dedup, "_degradation_logged", False)

    first = page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600)
    second = page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600)

    assert first is True
    assert second is False, "normal cooldown must still apply when degraded"
    assert "fcntl unavailable" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------

def test_write_ledger_uses_unique_temp(tmp_path, monkeypatch):
    ledger = tmp_path / "pages.json"
    calls = []
    real_mkstemp = tempfile.mkstemp

    def spy(*a, **kw):
        calls.append(kw)
        return real_mkstemp(*a, **kw)

    monkeypatch.setattr(tempfile, "mkstemp", spy)
    page_dedup.write_ledger(ledger, {"k": 1.0})
    assert len(calls) == 1
    assert calls[0]["dir"] == ledger.parent


def test_expired_keys_pruned_on_write(tmp_path):
    ledger = tmp_path / "pages.json"
    cooldown = 100.0
    now = 1_000_000.0
    horizon = max(cooldown * 2, cooldown + 86400)
    ledger.write_text(json.dumps({
        "expired": now - horizon - 1,
        "fresh": now - 10,
    }))
    # An unrelated key write triggers the prune.
    assert page_dedup.page_due("other", ledger_path=ledger, cooldown_s=cooldown, now=now) is True
    data = page_dedup.read_ledger(ledger)
    assert "expired" not in data
    assert "fresh" in data
    assert "other" in data


# ---------------------------------------------------------------------------
# sanitize_log_field
# ---------------------------------------------------------------------------

def test_sanitize_strips_crlf_tab_and_controls():
    hostile = "a\rb\nc\td\x00e\x1b[31mf‮g"
    result = page_dedup.sanitize_log_field(hostile)
    assert "\r" not in result and "\n" not in result and "\t" not in result
    import unicodedata
    assert all(unicodedata.category(c) not in ("Cc", "Cf") for c in result)
    assert result.endswith(" [sanitized]")


def test_sanitize_truncates():
    result = page_dedup.sanitize_log_field("x" * 5000)
    assert len(result) <= 200 + 13
    assert "…" in result


def test_sanitize_clean_input_is_identity():
    clean = "Yellowjackets S03E01"
    result = page_dedup.sanitize_log_field(clean)
    assert result == clean
    assert not result.endswith(" [sanitized]")


def test_sanitize_none_and_non_str():
    assert page_dedup.sanitize_log_field(None) == ""
    assert page_dedup.sanitize_log_field(12345) == "12345"
