"""Pure-logic tests for the LIVE half (scripts/maint/qflix-audit-live.py).

The live audit cannot be exercised offline — that is the definition of classes
L-01..L-06 and the whole reason they are registered as residual. What CAN be
tested offline is its DECISION LOGIC, and that is where the interesting bugs are:
"is this snapshot stale?", "is this token missing?", "is this mode too open?".

No network, no SSH, no secrets, no live box (AC-14).
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "maint" / "qflix-audit-live.py"


@pytest.fixture(scope="module")
def live():
    spec = importlib.util.spec_from_file_location("qflix_audit_live", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_diff_unit_sets_names_both_directions(live):
    d = live.diff_unit_sets({"a.timer", "b.service"}, {"b.service", "c.timer"})
    assert d["deployed_only"] == ["c.timer"]
    assert d["repo_only"] == ["a.timer"]
    assert d["both"] == ["b.service"]


def test_secret_mode_classification(live):
    assert live.classify_secret_mode(0o600) == "ok"
    assert live.classify_secret_mode(0o400) == "ok"
    assert live.classify_secret_mode(0o644) == "too-permissive"
    assert live.classify_secret_mode(0o660) == "too-permissive"


def test_missing_push_tokens_treats_empty_string_as_missing(live):
    """The born-mute class in one assertion: an empty token is exactly how the
    reaper's forever-red and 32 mute monitors happened."""
    expected = ["QFlix Reaper", "QFlix Audit Live", "Canary Quota"]
    tokens = {"QFlix Reaper": "abc", "QFlix Audit Live": "   ", "Canary Quota": None}
    assert live.missing_push_tokens(expected, tokens) == [
        "Canary Quota", "QFlix Audit Live"]


def test_parse_quota_pct(live):
    assert live.parse_quota_pct(
        "/dev/sda1  1000000  900000  100000  90% /home") == 90
    assert live.parse_quota_pct("nonsense") is None


def test_snapshot_staleness_uses_record_time_not_file_mtime(live):
    """C-04 applied to the auditor's own artefact. A rewritten-in-place file
    looks fresh to stat(2) while carrying week-old content."""
    now = _dt.datetime(2026, 7, 29, 12, 0, tzinfo=_dt.timezone.utc)
    fresh = {"recorded_at": "2026-07-29T11:30:00Z"}
    stale = {"recorded_at": "2026-07-27T11:30:00Z"}
    assert live.snapshot_is_stale(fresh, now, max_age_min=120) is False
    assert live.snapshot_is_stale(stale, now, max_age_min=120) is True


def test_missing_or_unparsable_record_time_counts_as_stale(live):
    now = _dt.datetime(2026, 7, 29, 12, 0, tzinfo=_dt.timezone.utc)
    assert live.snapshot_is_stale({}, now, 120) is True
    assert live.snapshot_is_stale({"recorded_at": "yesterday"}, now, 120) is True
    assert live.snapshot_is_stale(None, now, 120) is True


def test_collect_degrades_to_unavailable_not_to_clean(live, monkeypatch, tmp_path):
    """"I could not look" must never be reported as "I looked and it is fine" —
    the exact class the whole regime exists to remove."""
    monkeypatch.setattr(live, "REPO_UNITS", tmp_path / "nope")
    monkeypatch.setattr(live, "DEPLOYED_UNITS", tmp_path / "also-nope")
    monkeypatch.setattr(live, "SECRETS_DIR", tmp_path / "no-secrets")
    monkeypatch.setattr(live, "PUSH_TOKENS", tmp_path / "no-tokens.json")
    monkeypatch.setattr(live, "KUMA_DB", tmp_path / "no-kuma.db")
    result = live.collect(["QFlix Reaper"])
    for cid in ("L-01", "L-02", "L-03", "L-04", "L-05"):
        assert result["coverage"][cid] == "unavailable"
    assert result["findings"] == []


def test_monitor_name_matches_the_self_push_registration(live):
    from lib.kuma import STANDALONE_SELF_PUSH_MONITORS
    assert live.MONITOR_NAME in STANDALONE_SELF_PUSH_MONITORS
