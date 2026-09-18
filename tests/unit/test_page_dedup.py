"""lib/page_dedup — the cross-run page suppressor, under contention.

The mechanism that does not dedup under contention IS the bug being fixed, so
the concurrency cases here are the point of the file, not garnish: 16 threads
racing one key over 15 trials, and 8 separate OS processes racing one key.

The other half is direction of failure. Every error path must PAGE. A
suppressor that can swallow "your media server is down" is worse than the
33-pings-in-ten-hours storm it replaces, so corrupt ledgers, unwritable dirs
and failed locks each get their own test asserting True, not False.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

import pytest

from lib import page_dedup

REPO_ROOT = Path(__file__).resolve().parents[2]
MAINT_DIR = REPO_ROOT / "scripts" / "maint"


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "pages.json"


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def test_first_call_pages_and_stamps(ledger):
    assert page_dedup.page_due("k", ledger_path=ledger) is True
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert isinstance(data["k"], (int, float))


def test_second_call_within_window_is_suppressed(ledger):
    assert page_dedup.page_due("k", ledger_path=ledger) is True
    stamp = json.loads(ledger.read_text(encoding="utf-8"))["k"]
    assert page_dedup.page_due("k", ledger_path=ledger) is False
    # The stamp is NOT refreshed by a suppressed call: the window is measured
    # from the last PAGE, not from the last occurrence, or a condition that
    # recurs every hour would never re-surface.
    assert json.loads(ledger.read_text(encoding="utf-8"))["k"] == stamp


def test_new_key_pages_immediately(ledger):
    assert page_dedup.page_due("a", ledger_path=ledger) is True
    assert page_dedup.page_due("b", ledger_path=ledger) is True


def test_window_expiry_pages_again(ledger):
    t0 = 1_000_000.0
    cooldown = 3600.0
    assert page_dedup.page_due("k", ledger_path=ledger,
                               cooldown_s=cooldown, now=t0) is True
    assert page_dedup.page_due("k", ledger_path=ledger,
                               cooldown_s=cooldown, now=t0 + 10) is False
    assert page_dedup.page_due("k", ledger_path=ledger,
                               cooldown_s=cooldown, now=t0 + cooldown + 1) is True


def test_future_stamp_pages(ledger):
    """Clock skew must never mute. (now - last) <= 0 pages."""
    ledger.write_text(json.dumps({"k": time.time() + 10_000}), encoding="utf-8")
    assert page_dedup.page_due("k", ledger_path=ledger) is True


def test_non_numeric_stamp_pages(ledger):
    ledger.write_text(json.dumps({"k": "yesterday"}), encoding="utf-8")
    assert page_dedup.page_due("k", ledger_path=ledger) is True


def test_zero_cooldown_always_pages(ledger):
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=0.0) is True
    assert page_dedup.page_due("k", ledger_path=ledger, cooldown_s=0.0) is True


def test_clear_makes_next_call_page(ledger):
    assert page_dedup.page_due("k", ledger_path=ledger) is True
    assert page_dedup.page_due("k", ledger_path=ledger) is False
    page_dedup.clear("k", ledger_path=ledger)
    assert page_dedup.page_due("k", ledger_path=ledger) is True


def test_clear_missing_key_is_a_noop(ledger):
    page_dedup.clear("never-seen", ledger_path=ledger)      # must not raise
    page_dedup.clear("never-seen", ledger_path=ledger.parent / "absent.json")


def test_read_ledger_tolerates_everything(tmp_path):
    assert page_dedup.read_ledger(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert page_dedup.read_ledger(bad) == {}
    lst = tmp_path / "list.json"
    lst.write_text("[1, 2, 3]", encoding="utf-8")
    assert page_dedup.read_ledger(lst) == {}


# ---------------------------------------------------------------------------
# Direction of failure: every error path pages
# ---------------------------------------------------------------------------

def test_corrupt_ledger_fails_open_and_repairs(ledger):
    ledger.write_text("{not json", encoding="utf-8")
    assert page_dedup.page_due("k", ledger_path=ledger) is True
    repaired = json.loads(ledger.read_text(encoding="utf-8"))
    assert "k" in repaired


@pytest.mark.skipif(os.name != "posix", reason="chmod 0o500 is POSIX-only")
def test_unwritable_dir_fails_open(tmp_path, capsys):
    if os.geteuid() == 0:  # pragma: no cover - CI runs unprivileged
        pytest.skip("root ignores directory permissions")
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        assert page_dedup.page_due("k", ledger_path=locked / "pages.json") is True
        assert "paging anyway" in capsys.readouterr().err
    finally:
        os.chmod(locked, 0o700)


def test_flock_failure_fails_open_without_stamping(ledger, monkeypatch, capsys):
    """fcntl PRESENT but flock() raising is the real failure branch: we cannot
    serialise, so we page AND we refuse to write a stamp we could not guard."""
    fcntl = pytest.importorskip("fcntl", reason="flock only exists on POSIX")

    def boom(*_a, **_kw):
        raise OSError(9, "bad file descriptor")

    monkeypatch.setattr(fcntl, "flock", boom)
    assert page_dedup.page_due("k", ledger_path=ledger) is True
    assert "k" not in page_dedup.read_ledger(ledger)
    assert "lock failed, paging anyway" in capsys.readouterr().err


def test_flock_absent_platform_degrades_not_fails_open(ledger, monkeypatch, capsys):
    """fcntl ABSENT is a platform capability, not a failure. The normal
    cooldown decision still applies — paging on every call would make the
    Windows dev workstation unusable while protecting nothing."""
    monkeypatch.setattr(page_dedup, "_HAVE_FLOCK", False)
    monkeypatch.setattr(page_dedup, "_FLOCK_NOTE_EMITTED", False)
    assert page_dedup.page_due("k", ledger_path=ledger) is True
    assert page_dedup.page_due("k", ledger_path=ledger) is False
    assert "degraded to in-process only" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# D1 — in-process contention
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trial", range(15))
def test_sixteen_threads_one_key_page_once(tmp_path, trial):
    """The round-1 reproduction: without the per-path lock this failed 15/15,
    because every thread read "no stamp" before any thread wrote one."""
    ledger = tmp_path / f"pages-{trial}.json"
    n = 16
    barrier = threading.Barrier(n)
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        got = page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600.0)
        with results_lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == n
    assert sum(1 for r in results if r) == 1


def test_no_tmp_residue_after_thread_storm(tmp_path):
    ledger = tmp_path / "pages.json"
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        page_dedup.page_due("k", ledger_path=ledger, cooldown_s=3600.0)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert list(tmp_path.glob("*.tmp")) == []
    assert not (tmp_path / "pages.json.tmp").exists()
    assert "k" in json.loads(ledger.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# D2 — cross-process contention
# ---------------------------------------------------------------------------

_CHILD = """
import sys, time
sys.path.insert(0, sys.argv[3])
from lib import page_dedup
start = float(sys.argv[2])
while time.time() < start:
    pass
print(1 if page_dedup.page_due("k", ledger_path=sys.argv[1], cooldown_s=3600.0) else 0)
"""


def test_eight_processes_one_key_page_once(tmp_path):
    pytest.importorskip(
        "fcntl",
        reason="cross-process flock is Linux-only; CI is ubuntu-latest, "
               "production is Linux",
    )
    ledger = tmp_path / "pages.json"
    start = time.time() + 1.5
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(ledger), str(start), str(MAINT_DIR)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(8)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        outs.append(int(out.strip()))
    assert sum(outs) == 1, outs
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Atomicity + bounded growth
# ---------------------------------------------------------------------------

def test_write_ledger_uses_unique_temp(ledger, monkeypatch):
    real = tempfile.mkstemp
    calls: list[dict] = []

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(page_dedup.tempfile, "mkstemp", spy)
    page_dedup.write_ledger(ledger, {"k": 1.0})
    assert len(calls) == 1
    assert Path(calls[0]["dir"]) == ledger.parent


def test_no_fixed_tmp_path_in_source():
    """The fixed predictable temp name is the round-1 defect: two processes
    writing the same temp name interleave and os.replace publishes the mix.

    The needle is assembled at runtime so this assertion does not itself
    become a hit for the repo-level grep gate it mirrors."""
    needle = ".json" + ".tmp"
    for rel in ("scripts/maint/lib/page_dedup.py", "scripts/maint/lib/recovery.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert needle not in src
        assert "with_suffix(" not in src


def test_expired_keys_pruned_on_write(ledger):
    cooldown = page_dedup.DEFAULT_COOLDOWN_S
    now = time.time()
    page_dedup.write_ledger(ledger, {
        "ancient": now - 3 * cooldown,
        "recent": now - 10,
    })
    # An unrelated key's page_due() is what triggers the prune.
    assert page_dedup.page_due("fresh", ledger_path=ledger,
                               cooldown_s=cooldown, now=now) is True
    after = page_dedup.read_ledger(ledger)
    assert "ancient" not in after
    assert "recent" in after
    assert "fresh" in after


def test_prune_is_skipped_for_zero_cooldown(ledger):
    now = time.time()
    page_dedup.write_ledger(ledger, {"ancient": now - 10_000_000})
    page_dedup.page_due("fresh", ledger_path=ledger, cooldown_s=0.0, now=now)
    assert "ancient" in page_dedup.read_ledger(ledger)


# ---------------------------------------------------------------------------
# D4 — audit-log field hygiene
# ---------------------------------------------------------------------------

def _has_control(s: str) -> bool:
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in s)


def test_sanitize_strips_crlf_tab_and_controls():
    raw = "a\rb\nc\td\x00e\x1b[31mf‮g"
    out = page_dedup.sanitize_log_field(raw)
    assert "\r" not in out and "\n" not in out and "\t" not in out
    assert not _has_control(out)
    assert out.endswith(" [sanitized]")
    # CR/LF/TAB become ONE space each — 1:1, so offsets stay honest.
    assert out.startswith("a b c d")


def test_sanitize_truncates():
    out = page_dedup.sanitize_log_field("x" * 5000)
    assert len(out) <= 200 + 13
    assert "…" in out
    assert out.endswith(" [sanitized]")


def test_sanitize_clean_input_is_identity():
    clean = "Yellowjackets S03E01"
    assert page_dedup.sanitize_log_field(clean) == clean
    assert " [sanitized]" not in page_dedup.sanitize_log_field(clean)


def test_sanitize_none_and_non_str():
    assert page_dedup.sanitize_log_field(None) == ""
    assert page_dedup.sanitize_log_field(12345) == "12345"


def test_sanitize_postcondition_over_hostile_corpus():
    hostile = [
        "ok\n2026-09-17T00:00:00Z\tpage-suppressed\tsonarr\tx\ty\t0\t0\tforged",
        "﻿bom-led",
        "zero​width",
        "bidi‮override",
        "nul\x00byte",
        "\x7fdel",
        "esc\x1b]0;title\x07",
    ]
    for raw in hostile:
        out = page_dedup.sanitize_log_field(raw)
        assert not _has_control(out), repr(out)
        assert len(out) <= 200 + 13
