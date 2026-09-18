"""tests/unit/test_page_ledger.py — lib/page_ledger.py, the shared cross-run
page-dedup mechanism (Stage-0 Cluster C: alert hygiene, 2026-09-17).

AC-5 (window boundary), AC-12 (fail-open), AC-13 (bounded state / prune),
AC-14 (atomic write) live here — they're properties of the module itself,
independent of either caller (recovery.py's escalation page or
arr-housekeeping.py's --unstick sweep).
"""
from __future__ import annotations

import json
import os

import pytest

from lib import page_ledger


# ---------------------------------------------------------------------------
# ledger_path
# ---------------------------------------------------------------------------

def test_ledger_path_uses_manitoba_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    assert page_ledger.ledger_path("foo") == tmp_path / "foo.json"


def test_ledger_path_does_not_double_the_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    assert page_ledger.ledger_path("foo.json") == tmp_path / "foo.json"


# ---------------------------------------------------------------------------
# page_due
# ---------------------------------------------------------------------------

def test_page_due_first_call_true_and_stamps(tmp_path):
    p = tmp_path / "l.json"
    assert page_ledger.page_due(p, "k", 100.0, now=1000.0) is True
    assert json.loads(p.read_text()) == {"k": 1000.0}


def test_page_due_within_cooldown_is_false_and_does_not_restamp(tmp_path):
    p = tmp_path / "l.json"
    assert page_ledger.page_due(p, "k", 100.0, now=1000.0) is True
    assert page_ledger.page_due(p, "k", 100.0, now=1050.0) is False
    # Stamp unchanged — a muted page must not push the window forward.
    assert json.loads(p.read_text()) == {"k": 1000.0}


def test_page_due_is_per_key(tmp_path):
    p = tmp_path / "l.json"
    assert page_ledger.page_due(p, "a", 100.0, now=1000.0) is True
    assert page_ledger.page_due(p, "b", 100.0, now=1000.0) is True
    assert page_ledger.page_due(p, "a", 100.0, now=1010.0) is False


# ---------------------------------------------------------------------------
# AC-5: window boundary
# ---------------------------------------------------------------------------

def test_window_boundary_repages_just_after_cooldown(tmp_path):
    p = tmp_path / "l.json"
    t0 = 1_000_000.0
    cooldown = 86400.0
    assert page_ledger.page_due(p, "k", cooldown, now=t0) is True
    assert page_ledger.page_due(p, "k", cooldown, now=t0 + cooldown + 1) is True


def test_window_boundary_does_not_repage_just_before_cooldown(tmp_path):
    p = tmp_path / "l.json"
    t0 = 1_000_000.0
    cooldown = 86400.0
    assert page_ledger.page_due(p, "k", cooldown, now=t0) is True
    assert page_ledger.page_due(p, "k", cooldown, now=t0 + cooldown - 1) is False


def test_default_cooldown_is_86400s():
    assert page_ledger.DEFAULT_COOLDOWN_S == 86400.0


# ---------------------------------------------------------------------------
# partition_due
# ---------------------------------------------------------------------------

def test_partition_due_order_preserving_dedup(tmp_path):
    p = tmp_path / "l.json"
    due, muted = page_ledger.partition_due(p, ["a", "b", "a", "c", "b"], 100.0, now=1000.0)
    assert due == ["a", "b", "c"]
    assert muted == []


def test_partition_due_splits_due_and_muted(tmp_path):
    p = tmp_path / "l.json"
    # "a" already paged, "b" is new.
    page_ledger.page_due(p, "a", 100.0, now=1000.0)
    due, muted = page_ledger.partition_due(p, ["a", "b"], 100.0, now=1010.0)
    assert due == ["b"]
    assert muted == ["a"]


def test_partition_due_stamps_every_due_key_in_one_rmw(tmp_path):
    p = tmp_path / "l.json"
    due, muted = page_ledger.partition_due(p, ["a", "b", "c"], 100.0, now=1000.0)
    assert due == ["a", "b", "c"]
    ledger = json.loads(p.read_text())
    assert ledger == {"a": 1000.0, "b": 1000.0, "c": 1000.0}


def test_partition_due_empty_keys_is_a_noop(tmp_path):
    p = tmp_path / "l.json"
    due, muted = page_ledger.partition_due(p, [], 100.0, now=1000.0)
    assert (due, muted) == ([], [])
    assert not p.exists()


# ---------------------------------------------------------------------------
# clear_page
# ---------------------------------------------------------------------------

def test_clear_page_makes_the_next_occurrence_page_immediately(tmp_path):
    p = tmp_path / "l.json"
    page_ledger.page_due(p, "k", 100.0, now=1000.0)
    assert page_ledger.page_due(p, "k", 100.0, now=1010.0) is False
    page_ledger.clear_page(p, "k")
    assert page_ledger.page_due(p, "k", 100.0, now=1011.0) is True


def test_clear_page_on_missing_file_never_raises(tmp_path):
    page_ledger.clear_page(tmp_path / "does-not-exist.json", "k")  # must not raise


def test_clear_page_on_absent_key_never_raises(tmp_path):
    p = tmp_path / "l.json"
    page_ledger.page_due(p, "other", 100.0, now=1000.0)
    page_ledger.clear_page(p, "k")  # not present — must not raise or touch the file


# ---------------------------------------------------------------------------
# AC-13: prune / bounded state
# ---------------------------------------------------------------------------

def test_prune_removes_expired_keeps_live(tmp_path):
    p = tmp_path / "l.json"
    now = 1_000_000.0
    cooldown = 86400.0
    ledger = {f"expired-{i}": now - cooldown - 1 for i in range(10_000)}
    ledger.update({"live-1": now - 10, "live-2": now - 20, "live-3": now})
    p.write_text(json.dumps(ledger))

    removed = page_ledger.prune(p, cooldown, now=now)

    assert removed == 10_000
    remaining = json.loads(p.read_text())
    assert set(remaining.keys()) == {"live-1", "live-2", "live-3"}


def test_prune_on_missing_file_returns_zero(tmp_path):
    assert page_ledger.prune(tmp_path / "nope.json", 100.0, now=1000.0) == 0


def test_prune_removes_non_numeric_garbage_stamps(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"good": 999.0, "bad": "not-a-number", "also-bad": None}))
    removed = page_ledger.prune(p, 100.0, now=1000.0)
    assert removed == 2
    assert json.loads(p.read_text()) == {"good": 999.0}


# ---------------------------------------------------------------------------
# AC-12: fail-open
# ---------------------------------------------------------------------------

def test_page_due_missing_file_is_due(tmp_path):
    p = tmp_path / "nope.json"
    assert page_ledger.page_due(p, "k") is True


def test_page_due_malformed_json_fails_open(tmp_path):
    p = tmp_path / "l.json"
    p.write_text("{not json at all")
    assert page_ledger.page_due(p, "k") is True


def test_page_due_non_numeric_stamp_fails_open(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"k": "yesterday"}))
    assert page_ledger.page_due(p, "k") is True


def test_page_due_unwritable_dir_fails_open(tmp_path, monkeypatch):
    p = tmp_path / "l.json"

    def _boom(*a, **kw):
        raise OSError("state dir unwritable")
    monkeypatch.setattr(page_ledger, "_write", _boom)
    assert page_ledger.page_due(p, "k") is True


def test_partition_due_missing_file_all_due(tmp_path):
    p = tmp_path / "nope.json"
    due, muted = page_ledger.partition_due(p, ["a", "b"])
    assert due == ["a", "b"]
    assert muted == []


def test_partition_due_malformed_json_all_due(tmp_path):
    p = tmp_path / "l.json"
    p.write_text("{not json")
    due, muted = page_ledger.partition_due(p, ["a", "b"])
    assert due == ["a", "b"]
    assert muted == []


def test_partition_due_non_numeric_stamp_all_due(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"a": "not-a-number"}))
    due, muted = page_ledger.partition_due(p, ["a", "b"])
    assert due == ["a", "b"]
    assert muted == []


def test_partition_due_unwritable_dir_all_due(tmp_path, monkeypatch):
    p = tmp_path / "l.json"

    def _boom(*a, **kw):
        raise OSError("state dir unwritable")
    monkeypatch.setattr(page_ledger, "_write", _boom)
    due, muted = page_ledger.partition_due(p, ["a", "b"])
    assert due == ["a", "b"]
    assert muted == []


def test_prune_never_raises_on_unwritable_dir(tmp_path, monkeypatch):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"a": 1.0}))

    def _boom(*a, **kw):
        raise OSError("state dir unwritable")
    monkeypatch.setattr(page_ledger, "_write", _boom)
    # Must not raise; a failed prune write is non-fatal.
    page_ledger.prune(p, 0.0, now=1000.0)


# ---------------------------------------------------------------------------
# AC-14: atomic write
# ---------------------------------------------------------------------------

def test_write_uses_tmp_file_then_os_replace(tmp_path, monkeypatch):
    p = tmp_path / "l.json"
    calls = []
    real_replace = os.replace

    def _spy_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)
    monkeypatch.setattr(page_ledger.os, "replace", _spy_replace)

    page_ledger.page_due(p, "k", 100.0, now=1000.0)

    assert len(calls) == 1
    src, dst = calls[0]
    assert str(dst) == str(p)
    assert str(src) != str(p)
    # No leftover tmp file after a successful write.
    assert not (tmp_path / "l.json.tmp").exists()


def test_no_partial_file_observable_mid_write(tmp_path):
    """A reader between the tmp-file write and os.replace must see either the
    OLD complete file or the NEW complete file, never a half-written one —
    that is exactly what write-to-tmp + os.replace guarantees. Simulated by
    asserting the ledger is always valid JSON immediately after every write
    in a tight sequence."""
    p = tmp_path / "l.json"
    for i in range(50):
        page_ledger.page_due(p, f"k{i}", 100.0, now=1000.0 + i)
        # A "concurrent read" right after each write must never raise.
        data = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(data, dict)


def test_concurrent_read_during_write_never_raises_out_of_partition_due(tmp_path, monkeypatch):
    """Simulate a reader racing the writer: corrupt the file transiently by
    truncating it mid-flow via a monkeypatched _read that raises once, then
    recovers. partition_due must fail open on that one call, never raise."""
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"a": 1000.0}))

    calls = {"n": 0}
    real_read = page_ledger._read

    def _flaky_read(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated concurrent read race")
        return real_read(path)
    monkeypatch.setattr(page_ledger, "_read", _flaky_read)

    due, muted = page_ledger.partition_due(p, ["a", "b"], 100.0, now=1000.0)
    assert due == ["a", "b"]
    assert muted == []
