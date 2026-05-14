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
    state.write_text(json.dumps({"monitors": {"Sonarr": {"status": "up"}}}))
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
    # event line written
    log_files = list(events.glob("*.jsonl"))
    assert len(log_files) == 1
    line = json.loads(log_files[0].read_text().strip())
    assert line["queue_id"] == 42 and line["slug"] == "sonarr"


def test_refuse_when_arr_red(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    state.write_text(json.dumps({"monitors": {"Sonarr": {"status": "down"}}}))
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
    today.write_text("\n".join('{"action":"unstick"}' for _ in range(10)) + "\n")
    res = unstick.run(slug="sonarr", queue_id=42, reason="t", dry_run=False,
                      max_actions_per_day=10, state_file=state)
    assert res["status"] == "refused-cap-hit"


@patch("lib.arr_client.urllib.request.urlopen")
def test_idempotent_on_already_removed(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    # Queue lookup returns empty, DELETE never happens
    mock_open.return_value = _resp({"records": []})
    res = unstick.run(slug="sonarr", queue_id=42, reason="t", dry_run=False,
                      state_file=state)
    assert res["status"] == "already-removed"


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
    state.write_text(json.dumps({"monitors": {"Sonarr": {"status": "down"}}}))
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
