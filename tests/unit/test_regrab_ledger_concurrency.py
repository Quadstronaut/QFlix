"""Regression tests for the three defects the Cluster A council routed back on.

All three were reproduced by the council's own adversarial artifact before the
fix landed; each test below fails against the pre-fix module.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "maint"))


def _mod(state_dir: Path, monkeypatch):
    """Import the ledger with MANITOBA_STATE_DIR already pointing at tmp.

    Uses monkeypatch, never a raw os.environ write: a raw write outlives the
    test and re-points every OTHER module that captured its state path at
    import time (lib/recovery.py does), which shows up as an unrelated
    suppressed-escalation failure hundreds of tests later.
    """
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(state_dir))
    import lib.regrab_ledger as rl  # type: ignore
    return importlib.reload(rl)


# --- Defect 1: import-time path capture leaked tests into the real home dir --
def test_ledger_path_follows_env_set_after_import(tmp_path, monkeypatch):
    """The autouse conftest fixture sets MANITOBA_STATE_DIR AFTER import.

    The pre-fix module bound LEDGER_PATH at import, ignored the fixture, and
    wrote fixture rows into the developer's real ~/.opt/maint.
    """
    rl = _mod(tmp_path / "first", monkeypatch)
    later = tmp_path / "second"
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(later))

    assert rl.ledger_path().parent == later, "path must resolve at call time"
    assert rl.LEDGER_PATH.parent == later, "module attribute must follow too"

    rl.write({"sonarr|1|2": {"adds": [1.0], "parked": False,
                             "notified": False, "title": "t", "last": 1.0}})
    assert (later / "arr-regrab-ledger.json").exists()
    assert not (tmp_path / "first" / "arr-regrab-ledger.json").exists()


def test_no_write_escapes_to_the_real_state_dir(tmp_path, monkeypatch):
    """Belt and braces: the resolved path must sit under the injected dir.

    Asserted against the real ~/.opt/maint specifically, not against the home
    dir: on Windows pytest's own tmp_path lives under the home dir, so a
    home-dir assertion is vacuously wrong rather than protective.
    """
    rl = _mod(tmp_path, monkeypatch)
    assert str(rl.ledger_path()).startswith(str(tmp_path))
    assert rl.ledger_path().parent != Path.home() / ".opt" / "maint"


# --- Defect 2: fixed temp filename let two writers publish a torn file ------
def test_write_uses_a_unique_temp_not_a_fixed_name(tmp_path, monkeypatch):
    rl = _mod(tmp_path, monkeypatch)
    seen: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(str(src))
        return real_replace(src, dst)

    rl_os = sys.modules[rl.__name__].os
    orig = rl_os.replace
    rl_os.replace = spy
    try:
        for i in range(5):
            rl.write({f"sonarr|{i}|{i}": {"adds": [float(i)], "parked": False,
                                          "notified": False, "title": "t",
                                          "last": float(i)}})
    finally:
        rl_os.replace = orig

    assert len(seen) == 5
    assert len(set(seen)) == 5, f"temp names must be unique, got {seen}"
    assert not any(s.endswith(".json.tmp") for s in seen), \
        "fixed '<name>.json.tmp' is the defect being fixed"


def test_failed_write_leaves_no_stray_temp(tmp_path, monkeypatch):
    rl = _mod(tmp_path, monkeypatch)
    rl.write({})                      # create the dir
    monkeypatch.setattr(sys.modules[rl.__name__].json, "dump",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    assert rl.write({"sonarr|1|2": {}}) is False
    strays = list(rl.ledger_path().parent.glob("*.tmp"))
    assert strays == [], f"stray temp files left behind: {strays}"


# --- Defect 3: unlocked read-modify-write lost adds and re-paged parks ------
def test_run_lock_is_exclusive_within_a_process(tmp_path, monkeypatch):
    rl = _mod(tmp_path, monkeypatch)
    with rl.run_lock() as first:
        if not first:
            pytest.skip("lock unavailable in this environment")
        # Same process, same file: a second acquisition must be refused on
        # platforms with fcntl. Where fcntl is absent the documented
        # fail-open behaviour returns True instead.
        with rl.run_lock() as second:
            if sys.platform.startswith("win"):
                assert second is True, "documented fail-open without fcntl"
            else:
                assert second is False, "second holder must be refused"


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="fcntl absent; run_lock documents fail-open there")
def test_run_lock_excludes_a_second_process(tmp_path, monkeypatch):
    rl = _mod(tmp_path, monkeypatch)
    rl.write({})
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import os,sys,time;"
         f"os.environ['MANITOBA_STATE_DIR']=r'{tmp_path}';"
         f"sys.path.insert(0,r'{REPO / 'scripts' / 'maint'}');"
         "import lib.regrab_ledger as rl;"
         "ctx=rl.run_lock();held=ctx.__enter__();"
         "print('HELD' if held else 'NOPE',flush=True);time.sleep(6)"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "HELD"
        with rl.run_lock() as mine:
            assert mine is False, "cross-process contention must be refused"
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_lock_failure_fails_open_rather_than_blocking_repair(tmp_path, monkeypatch):
    """A broken lock must cost protection, never the stuck-download repair."""
    rl = _mod(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "open",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    with rl.run_lock() as held:
        assert held is True, "lock failure must fail OPEN"


def test_concurrent_writes_do_not_publish_a_torn_file(tmp_path, monkeypatch):
    """Whatever wins, the published file is always valid JSON."""
    rl = _mod(tmp_path, monkeypatch)
    errors: list[BaseException] = []

    def worker(n: int):
        try:
            for i in range(12):
                rl.write({f"sonarr|{n}|{i}": {"adds": [float(i)], "parked": False,
                                              "notified": False, "title": "t",
                                              "last": float(i)}})
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"writes raised: {errors}"
    json.loads(rl.ledger_path().read_text(encoding="utf-8"))
