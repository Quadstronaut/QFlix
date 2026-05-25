"""Tests for scripts/mcp/unstick.py."""
from __future__ import annotations
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "mcp"))

import unstick  # noqa: E402


def _resp(body, status=200):
    m = MagicMock()
    m.status = status
    m.read.return_value = (body if isinstance(body, str) else json.dumps(body)).encode()
    m.__enter__.return_value = m
    return m


def _setup(tmp_path: Path):
    s = tmp_path / "secrets"; s.mkdir()
    (s / "sonarr.key").write_text("KEY")
    (s / "sonarr.port").write_text("17026")
    (s / "sonarr.urlbase").write_text("sonarr")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"apps": {"sonarr": {"final_health": "ok", "kuma_status": "up"}}}))
    events = tmp_path / "events"; events.mkdir()
    return s, state, events


@patch("lib.arr_client.urllib.request.urlopen")
def test_unstick_happy_path(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    # Reload module so it picks up the new env var
    import importlib; importlib.reload(unstick)
    # 1st call = GET queue, 2nd = DELETE
    mock_open.side_effect = [
        _resp({"records": [{"id": 42, "downloadId": "abc", "title": "X"}]}),
        _resp("", status=200),
    ]
    res = unstick.run(slug="sonarr", queue_id=42, reason="t", dry_run=False,
                      state_file=state)
    assert res["status"] == "deleted+blocklisted"
    # Two event lines: the durable in-flight marker (written before the
    # DELETE) then the terminal outcome.
    log_files = list(events.glob("*.jsonl"))
    assert len(log_files) == 1
    lines = [json.loads(l) for l in log_files[0].read_text().splitlines() if l.strip()]
    assert lines[0]["result"] == "delete-in-flight"
    assert lines[-1]["result"] == "deleted+blocklisted"
    assert lines[-1]["queue_id"] == 42 and lines[-1]["slug"] == "sonarr"


@patch("lib.arr_client.urllib.request.urlopen")
def test_inflight_marker_durable_when_delete_interrupted(mock_open, tmp_path, monkeypatch):
    """If the process is killed mid-DELETE (the 2026-05 SSH-timeout case), the
    terminal record never runs — but the in-flight marker must already be on
    disk so the action is never silently lost."""
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    mock_open.side_effect = [
        _resp({"records": [{"id": 42, "downloadId": "abc", "title": "X"}]}),
    ]
    # Simulate a kill during the destructive call.
    with patch.object(unstick, "_execute_delete", side_effect=KeyboardInterrupt):
        try:
            unstick.run(slug="sonarr", queue_id=42, reason="t", dry_run=False,
                        state_file=state)
        except KeyboardInterrupt:
            pass
    today = list(events.glob("*.jsonl"))[0]
    lines = [json.loads(l) for l in today.read_text().splitlines() if l.strip()]
    assert any(l["result"] == "delete-in-flight" and l["queue_id"] == 42
               for l in lines)


@patch("lib.arr_client.urllib.request.urlopen")
def test_dry_run_writes_no_inflight_marker(mock_open, tmp_path, monkeypatch):
    """Dry-run must not write the in-flight marker (no destructive call to
    protect, and the marker would falsely imply a DELETE was committed)."""
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    mock_open.side_effect = [
        _resp({"records": [{"id": 42, "downloadId": "abc", "title": "X"}]}),
    ]
    res = unstick.run(slug="sonarr", queue_id=42, reason="t", dry_run=True,
                      state_file=state)
    assert res["status"] == "dry-run"
    today = list(events.glob("*.jsonl"))[0]
    lines = [json.loads(l) for l in today.read_text().splitlines() if l.strip()]
    assert not any(l["result"] == "delete-in-flight" for l in lines)


def test_refuse_when_arr_red(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    state.write_text(json.dumps({"apps": {"sonarr": {"final_health": "down", "kuma_status": "n/a"}}}))
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    res = unstick.run(slug="sonarr", queue_id=42, reason="t", dry_run=False,
                      state_file=state)
    assert res["status"] == "refused-arr-red"


@patch("lib.arr_client.urllib.request.urlopen")
def test_refuse_when_daily_cap_hit(mock_open, tmp_path, monkeypatch):
    import datetime as dt
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    today = events / f"{dt.date.today().isoformat()}.jsonl"
    # Only entries with an effective result (deleted+blocklisted /
    # qbit-orphan-removed) count toward the cap — refusals do not, otherwise
    # the cap self-traps.
    today.write_text("\n".join(
        '{"action":"unstick","result":"deleted+blocklisted"}' for _ in range(10)
    ) + "\n")
    res = unstick.run(slug="sonarr", queue_id=42, reason="t", dry_run=False,
                      max_actions_per_day=10, state_file=state)
    assert res["status"] == "refused-cap-hit"


@patch("lib.arr_client.urllib.request.urlopen")
def test_idempotent_on_already_removed(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    # Queue lookup returns empty → already-removed → falls through to qBit
    # orphan cleanup. With no hash to look up, that short-circuits to
    # no-hash-for-qbit-lookup. Either way no DELETE is issued — that's
    # what "idempotent" means here.
    mock_open.return_value = _resp({"records": []})
    res = unstick.run(slug="sonarr", queue_id=42, reason="t", dry_run=False,
                      state_file=state)
    assert res["status"] == "no-hash-for-qbit-lookup"


def test_preflight_passes_when_clean(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    refusal = unstick._preflight("sonarr", state_file=state, max_actions_per_day=10)
    assert refusal is None


def test_preflight_returns_unknown_slug():
    refusal = unstick._preflight("garbage", state_file=None, max_actions_per_day=10)
    assert refusal["status"] == "refused-unknown-slug"


def test_preflight_returns_red(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    state.write_text(json.dumps({"apps": {"sonarr": {"final_health": "down", "kuma_status": "n/a"}}}))
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    refusal = unstick._preflight("sonarr", state_file=state, max_actions_per_day=10)
    assert refusal["status"] == "refused-arr-red"


@patch("lib.arr_client.urllib.request.urlopen")
def test_resolve_queue_item_by_hash_found(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    mock_open.return_value = _resp({"records": [
        {"id": 99, "downloadId": "ABC", "title": "Some Show"},
    ]})
    from lib.arr_client import ArrClient
    c = ArrClient("sonarr", "v3", secrets_dir=secrets)
    out = unstick._resolve_queue_item(c, hash_="abc", queue_id=None)
    assert out["status"] == "found"
    assert out["queue_id"] == 99 and out["title"] == "Some Show"


@patch("lib.arr_client.urllib.request.urlopen")
def test_resolve_queue_item_already_removed(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    mock_open.return_value = _resp({"records": []})
    from lib.arr_client import ArrClient
    c = ArrClient("sonarr", "v3", secrets_dir=secrets)
    out = unstick._resolve_queue_item(c, hash_="missing", queue_id=None)
    assert out["status"] == "already-removed"


@patch("lib.arr_client.urllib.request.urlopen")
def test_execute_delete_success(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    mock_open.return_value = _resp("", status=200)
    from lib.arr_client import ArrClient
    c = ArrClient("sonarr", "v3", secrets_dir=secrets)
    out = unstick._execute_delete(c, queue_id=99, dry_run=False)
    assert out["status"] == "deleted+blocklisted"


def test_execute_delete_dry_run(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    from lib.arr_client import ArrClient
    c = ArrClient("sonarr", "v3", secrets_dir=secrets)
    out = unstick._execute_delete(c, queue_id=99, dry_run=True)
    assert out["status"] == "dry-run"


def test_refused_arr_red_writes_event(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    state.write_text(json.dumps({"apps": {"sonarr": {"final_health": "down", "kuma_status": "n/a"}}}))
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    unstick.run(slug="sonarr", queue_id=42, reason="t", state_file=state)
    log_files = list(events.glob("*.jsonl"))
    assert len(log_files) == 1
    line = json.loads(log_files[0].read_text().strip())
    assert line["result"] == "refused-arr-red"
    assert line["slug"] == "sonarr"


def test_refused_cap_hit_writes_event(tmp_path, monkeypatch):
    import datetime as dt_
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    today = events / f"{dt_.date.today().isoformat()}.jsonl"
    today.write_text("\n".join(
        '{"action":"unstick","result":"deleted+blocklisted"}' for _ in range(10)
    ) + "\n")
    unstick.run(slug="sonarr", queue_id=42, reason="t",
                max_actions_per_day=10, state_file=state)
    line = json.loads(today.read_text().splitlines()[-1])
    assert line["result"] == "refused-cap-hit"


def test_count_today_ignores_refusals(tmp_path, monkeypatch):
    """Refusals are recorded for audit but don't gate the next attempt.

    Confirmed empirically: ~/scripts/mcp/events/2026-05-15.jsonl grew to
    32 lines under a cap of 10 because each refused-cap-hit retry was
    appended, then re-counted, then refused again — self-trapping the
    counter for the rest of the day.
    """
    import datetime as dt_
    _, _, events = _setup(tmp_path)
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    today = events / f"{dt_.date.today().isoformat()}.jsonl"
    today.write_text("\n".join([
        '{"result":"refused-cap-hit"}',
        '{"result":"refused-arr-red"}',
        '{"result":"refused-unknown-slug"}',
        '{"result":"already-removed"}',
        '{"result":"already-fully-removed"}',
        '{"result":"qbit-login-failed"}',
        '{"result":"qbit-delete-failed"}',
        '{"result":"delete-failed"}',
    ]) + "\n")
    assert unstick._count_today() == 0


def test_count_today_counts_effective_only(tmp_path, monkeypatch):
    import datetime as dt_
    _, _, events = _setup(tmp_path)
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    today = events / f"{dt_.date.today().isoformat()}.jsonl"
    today.write_text("\n".join([
        '{"result":"deleted+blocklisted"}',
        '{"result":"refused-cap-hit"}',
        '{"result":"qbit-orphan-removed"}',
        '{"result":"refused-cap-hit"}',
        '{"result":"deleted+blocklisted"}',
    ]) + "\n")
    assert unstick._count_today() == 3


def test_count_today_skips_malformed_lines(tmp_path, monkeypatch):
    import datetime as dt_
    _, _, events = _setup(tmp_path)
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    today = events / f"{dt_.date.today().isoformat()}.jsonl"
    today.write_text(
        "not even json\n"
        "\n"
        '{"result":"deleted+blocklisted"}\n'
        '{"result":"refused-cap-hit"}\n'
    )
    assert unstick._count_today() == 1


@patch("lib.arr_client.urllib.request.urlopen")
def test_already_removed_writes_event(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    mock_open.return_value = _resp({"records": []})
    # *arr says already-removed → qBit fallback runs; with no real qBit in
    # the test env, login fails. The event must still be recorded.
    unstick.run(slug="sonarr", hash_="dead", reason="t", state_file=state)
    log_files = list(events.glob("*.jsonl"))
    line = json.loads(log_files[0].read_text().strip())
    assert line["result"] == "qbit-login-failed"


@patch("lib.arr_client.urllib.request.urlopen")
def test_diagnose_returns_phase_timings(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    # The diagnose path makes 3 GET calls: legacy-shape (paged), default,
    # and one inside _resolve_queue_item. Mock returns the same payload
    # for all three; the test only cares about structure and timing keys.
    mock_open.side_effect = [
        _resp({"records": [{"id": 9, "downloadId": "ABC", "title": "T"}]}),
        _resp({"records": [{"id": 9, "downloadId": "ABC", "title": "T"}]}),
        _resp({"records": [{"id": 9, "downloadId": "ABC", "title": "T"}]}),
    ]
    out = unstick.diagnose(slug="sonarr", hash_="abc", state_file=state)
    assert out["status"] == "diagnose"
    assert out["slug"] == "sonarr"
    assert "state_read_ms" in out["phases"]
    assert "queue_lookup_paged_ms" in out["phases"]
    assert "queue_lookup_default_ms" in out["phases"]
    assert isinstance(out["phases"]["state_read_ms"], (int, float))
    # No event written (diagnose is pure-read)
    assert not list(events.glob("*.jsonl"))


@patch("lib.arr_client.urllib.request.urlopen")
def test_resolve_queue_item_follows_pagination(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    # First page lacks the target; second page contains it.
    mock_open.side_effect = [
        _resp({"records": [{"id": 1, "downloadId": "OTHER", "title": "X"}],
                "page": 1, "pageSize": 1, "totalRecords": 2}),
        _resp({"records": [{"id": 2, "downloadId": "TARGET", "title": "Y"}],
                "page": 2, "pageSize": 1, "totalRecords": 2}),
    ]
    from lib.arr_client import ArrClient
    c = ArrClient("sonarr", "v3", secrets_dir=secrets)
    out = unstick._resolve_queue_item(c, hash_="target", queue_id=None)
    assert out["status"] == "found"
    assert out["queue_id"] == 2
