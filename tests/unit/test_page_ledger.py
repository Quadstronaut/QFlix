"""tests/unit/test_page_ledger.py — the ONE cross-run page-dedup mechanism.

The mechanism was born private inside lib/recovery.py on 2026-09-02 (33
identical Plex pages in ten hours). On 2026-09-17 a SECOND caller appeared
(arr-housekeeping's hourly unstick sweep: 12 of 13 Discord messages in a 24h
window, one ongoing fault). Two copies of a cooldown drift apart and the copy
that drifts is the one nobody is looking at — so the code moved here and
recovery.py delegates.

These tests pin the four properties every caller depends on: the window
boundary, fail-open on every error path, bounded state, and atomic writes.
`tests/unit/test_recovery*.py` is the fifth property (byte-identical
behaviour for the original caller) and passes UNMODIFIED.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from lib import page_ledger

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def ledger(tmp_path) -> Path:
    return tmp_path / "pages.json"


# ---------------------------------------------------------------------------
# AC-5 — the window boundary
# ---------------------------------------------------------------------------

def test_default_cooldown_is_one_day():
    assert page_ledger.DEFAULT_COOLDOWN_S == 86400


def test_first_page_is_due_and_stamps(ledger):
    assert page_ledger.page_due(ledger, "k", now=1000.0) is True
    assert json.loads(ledger.read_text())["k"] == 1000.0


def test_inside_the_window_is_muted(ledger):
    page_ledger.page_due(ledger, "k", 86400, now=1000.0)
    assert page_ledger.page_due(ledger, "k", 86400, now=1000.0 + 86399) is False


def test_one_second_before_the_boundary_does_not_repage(ledger):
    page_ledger.page_due(ledger, "k", 86400, now=1000.0)
    assert page_ledger.page_due(ledger, "k", 86400, now=1000.0 + 86400 - 1) is False


def test_one_second_after_the_boundary_repages(ledger):
    """Silence-forever is the opposite failure to the storm. A fault still
    broken tomorrow is re-surfaced."""
    page_ledger.page_due(ledger, "k", 86400, now=1000.0)
    assert page_ledger.page_due(ledger, "k", 86400, now=1000.0 + 86400 + 1) is True


def test_a_future_stamp_is_not_trusted_to_mute(ledger):
    """Clock skew / restored backup / NTP step. Inherited byte-for-byte from
    recovery.py's `0 < (now - last)` guard."""
    ledger.write_text(json.dumps({"k": 9_000_000.0}))
    assert page_ledger.page_due(ledger, "k", 86400, now=1000.0) is True


def test_cooldown_zero_never_mutes(ledger):
    page_ledger.page_due(ledger, "k", 0.0, now=1000.0)
    assert page_ledger.page_due(ledger, "k", 0.0, now=1000.5) is True


# ---------------------------------------------------------------------------
# clear_page — per-OUTAGE, not per-wall-clock-day
# ---------------------------------------------------------------------------

def test_clear_page_makes_the_next_occurrence_page(ledger):
    page_ledger.page_due(ledger, "k", now=1000.0)
    page_ledger.clear_page(ledger, "k")
    assert page_ledger.page_due(ledger, "k", now=1001.0) is True


def test_clear_page_on_an_absent_key_is_a_no_op(ledger):
    page_ledger.clear_page(ledger, "never-seen")  # must not raise
    assert not ledger.exists()


# ---------------------------------------------------------------------------
# partition_due — one read-modify-write for a whole batch
# ---------------------------------------------------------------------------

def test_partition_splits_and_preserves_order(ledger):
    page_ledger.page_due(ledger, "b", now=1000.0)
    due, muted = page_ledger.partition_due(ledger, ["a", "b", "c"], now=1001.0)
    assert due == ["a", "c"]
    assert muted == ["b"]


def test_partition_collapses_duplicates(ledger):
    due, muted = page_ledger.partition_due(ledger, ["a", "a", "a"], now=1000.0)
    assert due == ["a"]
    assert muted == []


def test_partition_stamps_every_due_key_in_one_write(ledger, monkeypatch):
    writes = []
    real = page_ledger.write_ledger
    monkeypatch.setattr(page_ledger, "write_ledger",
                        lambda p, d: (writes.append(dict(d)), real(p, d))[1])
    page_ledger.partition_due(ledger, ["a", "b", "c"], now=1000.0)
    assert len(writes) == 1, "a 40-action sweep must not do 40 read/replace cycles"
    assert set(json.loads(ledger.read_text())) == {"a", "b", "c"}


def test_partition_with_no_due_keys_does_not_rewrite(ledger, monkeypatch):
    page_ledger.partition_due(ledger, ["a"], now=1000.0)
    monkeypatch.setattr(page_ledger, "write_ledger",
                        lambda *a, **k: pytest.fail("rewrote for nothing"))
    due, muted = page_ledger.partition_due(ledger, ["a"], now=1001.0)
    assert (due, muted) == ([], ["a"])


# ---------------------------------------------------------------------------
# AC-12 — FAIL OPEN on every error path
# ---------------------------------------------------------------------------

def test_fail_open_ledger_absent(ledger):
    due, muted = page_ledger.partition_due(ledger, ["a", "b"], now=1000.0)
    assert (due, muted) == (["a", "b"], [])


def test_fail_open_malformed_json(ledger):
    ledger.write_text("{not json", encoding="utf-8")
    due, muted = page_ledger.partition_due(ledger, ["a"], now=1000.0)
    assert due == ["a"]
    assert page_ledger.page_due(ledger, "a", now=1000.0) is True


def test_fail_open_non_numeric_stamp(ledger):
    ledger.write_text(json.dumps({"a": "yesterday", "b": None}))
    due, muted = page_ledger.partition_due(ledger, ["a", "b"], now=1000.0)
    assert (due, muted) == (["a", "b"], [])


def test_fail_open_json_is_a_list_not_a_dict(ledger):
    ledger.write_text(json.dumps(["a"]))
    assert page_ledger.page_due(ledger, "a", now=1000.0) is True


def test_fail_open_state_dir_unwritable(ledger, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise PermissionError("read-only filesystem")
    monkeypatch.setattr(page_ledger, "write_ledger", boom)
    due, muted = page_ledger.partition_due(ledger, ["a", "b"], now=1000.0)
    assert (due, muted) == (["a", "b"], []), "an unwritable ledger must never mute"
    assert page_ledger.page_due(ledger, "a", now=1000.0) is True
    assert "paging" in capsys.readouterr().err


def test_prune_and_clear_never_raise(ledger, monkeypatch):
    def boom(*_a, **_k):
        raise PermissionError("read-only filesystem")
    ledger.write_text(json.dumps({"a": 1.0}))
    monkeypatch.setattr(page_ledger, "write_ledger", boom)
    assert page_ledger.prune(ledger, 86400, now=1_000_000.0) == 0
    page_ledger.clear_page(ledger, "a")  # must not raise


# ---------------------------------------------------------------------------
# AC-13 — bounded state
# ---------------------------------------------------------------------------

def test_prune_drops_expired_keeps_live(ledger):
    now = 10_000_000.0
    seeded = {f"expired-{i}": now - 200_000 for i in range(10_000)}
    seeded.update({f"live-{i}": now - 10 for i in range(3)})
    ledger.write_text(json.dumps(seeded))

    removed = page_ledger.prune(ledger, 86400, now=now)

    kept = json.loads(ledger.read_text())
    assert removed == 10_000
    assert len(kept) == 3
    assert set(kept) == {"live-0", "live-1", "live-2"}


def test_prune_on_a_missing_file_is_zero(ledger):
    assert page_ledger.prune(ledger, 86400, now=1000.0) == 0


# ---------------------------------------------------------------------------
# AC-14 — atomic write
# ---------------------------------------------------------------------------

def test_write_goes_through_tmp_plus_os_replace(ledger, monkeypatch):
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(page_ledger.os, "replace",
                        lambda a, b: (replaced.append((str(a), str(b))),
                                      real_replace(a, b))[1])
    page_ledger.page_due(ledger, "k", now=1000.0)
    assert len(replaced) == 1
    src, dst = replaced[0]
    assert src.endswith(".tmp") and dst == str(ledger)
    assert not Path(src).exists(), "tmp file must not survive the replace"


def test_a_reader_never_sees_a_partial_file(ledger):
    """The concurrency shape that matters here: the hourly unstick timer and
    the maint daemon are separate PROCESSES sharing a state dir. os.replace is
    atomic on both POSIX and NTFS, so a concurrent reader sees the old file or
    the new one — never half of either, and never an exception out of
    partition_due."""
    page_ledger.page_due(ledger, "old", now=1000.0)
    observed = []

    real_dump = page_ledger.json.dump

    def dump_and_peek(obj, fh, **kw):
        real_dump(obj, fh, **kw)
        fh.flush()
        # Mid-write: the destination still holds the previous complete file.
        observed.append(page_ledger.read_ledger(ledger))
        observed.append(page_ledger.partition_due(ledger, ["old"], now=1001.0))

    page_ledger.json.dump = dump_and_peek
    try:
        page_ledger.page_due(ledger, "new", now=1001.0)
    finally:
        page_ledger.json.dump = real_dump

    assert observed, "the peek never ran — test is not exercising the write"
    assert observed[0] == {"old": 1000.0}, "reader saw a partial/absent file"


# ---------------------------------------------------------------------------
# ledger_path
# ---------------------------------------------------------------------------

def test_ledger_path_honours_the_state_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    assert page_ledger.ledger_path("pages") == tmp_path / "pages.json"
    assert page_ledger.ledger_path("pages.json") == tmp_path / "pages.json"


# ---------------------------------------------------------------------------
# AC-1 — ONE mechanism, not two
# ---------------------------------------------------------------------------

def _tracked(*globs) -> list[Path]:
    out = subprocess.run(["git", "ls-files", *globs], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True)
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line.strip()]


def test_recovery_delegates_and_keeps_no_second_implementation():
    src = (REPO_ROOT / "scripts" / "maint" / "lib" / "recovery.py").read_text(encoding="utf-8")
    assert "page_ledger.page_due" in src
    assert "page_ledger.clear_page" in src
    # The private cooldown body is GONE, not merely unused.
    assert "_ESCALATION_PAGE_COOLDOWN_S:" in src, "the env-driven knob stays public"
    assert "os.replace" not in src, "recovery.py must not write ledgers itself"


def test_no_second_page_dedup_implementation_in_tracked_python():
    """A second stamp-and-cooldown implementation is how the two copies drift.
    `os.replace` on a *-pages.json is the fingerprint; only page_ledger.py may
    carry it."""
    offenders = []
    for path in _tracked("scripts/**/*.py"):
        if path.name == "page_ledger.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "pages.json" in text and "os.replace" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"second page-dedup implementation in {offenders}"
