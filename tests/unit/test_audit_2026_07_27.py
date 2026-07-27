"""Regression tests for the 2026-07-27 server-audit fixes.

Covers the highest-risk NEW pure logic:
  - classify_qbit_stall  (qflix-collect): exhaustive qBit `state` handling — the
    previous whitelist silently dropped forcedDL/error/missingFiles.
  - classify_torrent     (qflix-torrent-janitor): reap/keep criteria + the
    non-*arr-category and *arr-tracked safety guards.
  - reconcile_healthy    (lib.recovery): stale-failure record reconciliation.

Import isolation: loading these repo modules registers a merged `lib` namespace
package (scripts/maint/lib + scripts/mcp/lib) and mutates sys.path. Left in
place, that leaked into OTHER test files sharing the interpreter and broke
test_newsletter_digest_canary when both ran in one pytest session. `_isolated`
snapshots + restores sys.path and the `lib.*` sys.modules entries around every
load so this file is hermetic.
"""
import contextlib
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MAINT = REPO / "scripts" / "maint"
MCP = REPO / "scripts" / "mcp"


@contextlib.contextmanager
def _isolated():
    mods_before = set(sys.modules)
    path_before = list(sys.path)
    for p in (str(MAINT), str(MCP)):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        yield
    finally:
        for m in list(sys.modules):
            if m not in mods_before and (m == "lib" or m.startswith("lib.")):
                del sys.modules[m]
        sys.path[:] = path_before


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, MAINT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with _isolated():
    collect = _load("qflix_collect_mod", "qflix-collect.py")
    janitor = _load("qflix_torrent_janitor_mod", "qflix-torrent-janitor.py")


# ---------------------------------------------------------------------------
# classify_qbit_stall — #6.1
# ---------------------------------------------------------------------------
def test_forceddl_incomplete_is_a_stall():
    # The Happy Face S01E08 case: forcedDL, 65%, 0 seeds — silently dropped
    # before this audit.
    assert collect.classify_qbit_stall("forcedDL", 0.65, 0) == ("forcedDL", True)


def test_error_and_missingfiles_act_regardless_of_progress():
    assert collect.classify_qbit_stall("error", 1.0, 0) == ("error", True)
    assert collect.classify_qbit_stall("missingFiles", 0.4, 0) == ("missingFiles", True)


def test_named_download_stalls_act():
    for s in ("stalledDL", "pausedDL", "stoppedDL"):
        assert collect.classify_qbit_stall(s, 0.2, 0) == (s, True)


def test_downloading_dead_slow_vs_moving():
    assert collect.classify_qbit_stall("downloading", 0.5, 0) == ("dead-slow", True)
    assert collect.classify_qbit_stall("downloading", 0.5, 10_000_000) is None


def test_complete_and_idle_states_skip():
    for s in ("stalledUP", "uploading", "forcedUP", "queuedUP", "pausedUP"):
        assert collect.classify_qbit_stall(s, 1.0, 0) is None
    for s in ("queuedDL", "metaDL", "checkingDL", "checkingResumeData",
              "allocating", "moving"):
        assert collect.classify_qbit_stall(s, 0.3, 0) is None


def test_unknown_state_is_flagged_not_dropped():
    rule, act = collect.classify_qbit_stall("someNewLibtorrentState", 0.5, 0)
    assert act is False and rule == collect._QBIT_UNKNOWN_RULE


def test_every_documented_qbit_state_resolves_definitely():
    known = [
        "error", "missingFiles", "uploading", "pausedUP", "stoppedUP",
        "queuedUP", "stalledUP", "checkingUP", "forcedUP", "allocating",
        "downloading", "metaDL", "pausedDL", "stoppedDL", "queuedDL",
        "stalledDL", "checkingDL", "checkingResumeData", "forcedDL", "moving",
    ]
    for s in known:
        v = collect.classify_qbit_stall(s, 0.5, 0)
        if v is not None:
            assert v[0] != collect._QBIT_UNKNOWN_RULE, "state %r fell through" % s


# ---------------------------------------------------------------------------
# classify_torrent — #6.3
# ---------------------------------------------------------------------------
NOW = 1_800_000_000


def _t(**kw):
    base = dict(hash="a" * 40, name="x", category="radarr", progress=1.0,
                state="stalledUP", ratio=2.0, added_on=NOW - 86400, size=10 ** 9)
    base.update(kw)
    return base


def test_ratio_met_untracked_complete_is_reaped():
    # Both 2026-07-27 leftovers (ratio 1.85 + 1.18) reap on the first run.
    assert janitor.classify_torrent(_t(ratio=1.85), set(), NOW, 1.0, 30)[0] == "reap"
    assert janitor.classify_torrent(_t(ratio=1.18), set(), NOW, 1.0, 30)[0] == "reap"


def test_incomplete_kept():
    assert janitor.classify_torrent(_t(progress=0.9), set(), NOW, 1.0, 30)[0] == "keep"


def test_active_download_state_kept():
    assert janitor.classify_torrent(
        _t(state="downloading", progress=1.0), set(), NOW, 1.0, 30)[0] == "keep"


def test_non_arr_category_protected():
    action, reason = janitor.classify_torrent(
        _t(category="my-personal"), set(), NOW, 1.0, 30)
    assert action == "keep" and "non-arr" in reason


def test_arr_tracked_kept_even_at_high_ratio():
    h = "b" * 40
    assert janitor.classify_torrent(_t(hash=h, ratio=5.0), {h}, NOW, 1.0, 30)[0] == "keep"


def test_low_ratio_recent_kept_but_aged_out_reaped():
    # nothing forever: below ratio + recent -> keep; below ratio + old -> reap.
    assert janitor.classify_torrent(
        _t(ratio=0.2, added_on=NOW - 86400), set(), NOW, 1.0, 30)[0] == "keep"
    assert janitor.classify_torrent(
        _t(ratio=0.2, added_on=NOW - 40 * 86400), set(), NOW, 1.0, 30)[0] == "reap"


# ---------------------------------------------------------------------------
# reconcile_healthy — #7
# ---------------------------------------------------------------------------
def test_reconcile_healthy_clears_stale_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    with _isolated():
        try:
            rec = _load("recovery_mod_test", "lib/recovery.py")
            state_mod = _load("state_mod_test", "lib/state.py")
        except Exception as exc:  # pragma: no cover - dep-gated
            pytest.skip("recovery deps unavailable in test venv: %s" % exc)

        sp = tmp_path / "state.json"
        state_mod.write(sp, {"apps": {"qbittorrent": {
            "event": "failed", "final_health": "down", "attempts": 3}}})

        assert rec.reconcile_healthy("qbittorrent") is True
        data = state_mod.read(sp)
        assert data["apps"]["qbittorrent"]["event"] == "reconciled_healthy"
        assert data["apps"]["qbittorrent"]["final_health"] == "ok"
        # idempotent: a now-clean record is a no-op
        assert rec.reconcile_healthy("qbittorrent") is False
        # unknown app: no-op
        assert rec.reconcile_healthy("does-not-exist") is False
