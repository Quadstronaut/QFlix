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


# --- C5: SAB stuck-handling parity (2026-07-19 spec) ------------------------
#
# unstick.py's core DELETE flow (_resolve_queue_item / _execute_delete / run)
# is untouched — it was already protocol-agnostic (matches on the *arr's
# downloadId string regardless of what client produced it). What's new here
# is: (1) _id_kind, a pure shape classifier that tells the rest of the module
# which client an id belongs to; (2) SAB-aware auto-detect and orphan-cleanup
# paths that dispatch on that classification instead of assuming qBit.
#
# SabClient itself is never hit over the network in these tests — its module
# docstring is explicit that it RAISES on transport error rather than
# swallowing it (unlike QbitClient), so every SAB-touching test here either
# (a) lets a real SabClient fail closed via a secrets dir with no sabnzbd.*
# files (mirrors how the existing qbit-login-failed tests above rely on a
# real QbitClient with no qbittorrent.* secrets), or (b) monkeypatches the
# `unstick.SabClient` module attribute with a small stand-in, per the
# existing test file's convention of patching module-level names rather than
# reaching into urllib.


class _FakeSabClient:
    """Stand-in for lib.sab_client.SabClient. Tests monkeypatch
    `unstick.SabClient` to a zero-arg callable returning one of these so
    _auto_detect_slug_sab / _try_sab_orphan_cleanup can be driven without a
    real SAB box. `host`/`apikey` mirror the real client's truthy check for
    "secrets configured"."""

    def __init__(self, slots=None, delete_ok=True,
                 list_raises=False, delete_raises=False):
        self.host = "http://127.0.0.1:9999/api"
        self.apikey = "KEY"
        self._slots = slots if slots is not None else []
        self._delete_ok = delete_ok
        self._list_raises = list_raises
        self._delete_raises = delete_raises
        self.deleted_ids = []

    def list_slots(self):
        if self._list_raises:
            raise RuntimeError("sab unreachable")
        return self._slots

    def delete_slot(self, nzo_id, del_files=True):
        if self._delete_raises:
            raise RuntimeError("sab unreachable")
        self.deleted_ids.append(nzo_id)
        return self._delete_ok


# -- _id_kind: pure shape classification -------------------------------------

def test_id_kind_sab_prefix():
    assert unstick._id_kind("SABnzbd_nzo_AbCdEf12") == "sab"


def test_id_kind_qbit_hex_hash():
    assert unstick._id_kind("a1b2c3d4" * 5) == "qbit"          # 40 hex chars
    assert unstick._id_kind("F" * 40) == "qbit"                 # uppercase hex ok


def test_id_kind_unknown_for_garbage_none_and_empty():
    assert unstick._id_kind("not-a-real-id") == "unknown"
    assert unstick._id_kind("") == "unknown"
    assert unstick._id_kind(None) == "unknown"


def test_id_kind_unknown_for_40_chars_non_hex():
    # Right length, wrong alphabet — must not be misclassified as qbit.
    assert unstick._id_kind("g" * 40) == "unknown"


def test_id_kind_unknown_for_wrong_length_hex():
    assert unstick._id_kind("abc123") == "unknown"


# -- sab slug autodetect (mocked SabClient) ----------------------------------

def test_auto_detect_slug_sab_shaped_id_uses_slot_cat(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(slots=[{"nzo_id": "SABnzbd_nzo_ABC", "cat": "sonarr"}])
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    assert unstick._auto_detect_slug("SABnzbd_nzo_ABC") == "sonarr"


def test_auto_detect_slug_sab_unknown_cat_returns_none(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(slots=[{"nzo_id": "SABnzbd_nzo_ABC", "cat": "not-an-arr"}])
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    assert unstick._auto_detect_slug("SABnzbd_nzo_ABC") is None


def test_auto_detect_slug_sab_no_matching_slot_returns_none(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(slots=[{"nzo_id": "SABnzbd_nzo_OTHER", "cat": "sonarr"}])
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    assert unstick._auto_detect_slug("SABnzbd_nzo_ABC") is None


def test_auto_detect_slug_sab_transport_error_returns_none(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(list_raises=True)
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    assert unstick._auto_detect_slug("SABnzbd_nzo_ABC") is None


def test_auto_detect_slug_unknown_shape_probes_qbit_then_sab(tmp_path, monkeypatch):
    """No qBit secrets in this tmp env -> qBit lookup fails closed -> the
    unknown-shape dispatch falls through to SAB, per C5's probe order."""
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(slots=[{"nzo_id": "weird-id-123", "cat": "radarr"}])
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    assert unstick._auto_detect_slug("weird-id-123") == "radarr"


def test_auto_detect_slug_qbit_path_unchanged(tmp_path, monkeypatch):
    """The 40-char-hex path still goes straight to qBit, never touching SAB
    (a raising fake would blow up the test if it were reached)."""
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(list_raises=True)
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    # No qbittorrent.* secrets -> QbitClient().login() is False -> None.
    assert unstick._auto_detect_slug("a" * 40) is None


# -- sab orphan-cleanup statuses (C5 status vocabulary) ----------------------

def test_sab_orphan_cleanup_removed(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(slots=[{"nzo_id": "SABnzbd_nzo_ABC", "filename": "Show.S01E01"}])
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    out = unstick._try_sab_orphan_cleanup("SABnzbd_nzo_ABC", dry_run=False)
    assert out["status"] == "sab-orphan-removed"
    assert out["sab_title"] == "Show.S01E01"
    assert fake.deleted_ids == ["SABnzbd_nzo_ABC"]


def test_sab_orphan_cleanup_already_fully_removed_no_slot(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(slots=[])
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    out = unstick._try_sab_orphan_cleanup("SABnzbd_nzo_ABC", dry_run=False)
    assert out["status"] == "already-fully-removed"


def test_sab_orphan_cleanup_already_fully_removed_no_id():
    out = unstick._try_sab_orphan_cleanup(None, dry_run=False)
    assert out["status"] == "already-fully-removed"


def test_sab_orphan_cleanup_unreachable_no_secrets(tmp_path, monkeypatch):
    """Real SabClient (not the fake), no sabnzbd.* secrets in this tmp env —
    mirrors how the existing qbit-login-failed tests rely on a real
    QbitClient failing closed for the same reason."""
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    out = unstick._try_sab_orphan_cleanup("SABnzbd_nzo_ABC", dry_run=False)
    assert out["status"] == "sab-unreachable"


def test_sab_orphan_cleanup_unreachable_on_transport_error(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(list_raises=True)
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    out = unstick._try_sab_orphan_cleanup("SABnzbd_nzo_ABC", dry_run=False)
    assert out["status"] == "sab-unreachable"


def test_sab_orphan_cleanup_delete_failed(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(
        slots=[{"nzo_id": "SABnzbd_nzo_ABC", "filename": "X"}], delete_ok=False)
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    out = unstick._try_sab_orphan_cleanup("SABnzbd_nzo_ABC", dry_run=False)
    assert out["status"] == "sab-delete-failed"


def test_sab_orphan_cleanup_dry_run_does_not_delete(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(slots=[{"nzo_id": "SABnzbd_nzo_ABC", "filename": "X"}])
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    out = unstick._try_sab_orphan_cleanup("SABnzbd_nzo_ABC", dry_run=True)
    assert out["status"] == "dry-run-sab-orphan"
    assert fake.deleted_ids == []  # dry-run must never call delete_slot


# -- _try_client_orphan_cleanup: id-shape dispatcher -------------------------

def test_client_orphan_dispatch_routes_sab_shaped_id(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(slots=[{"nzo_id": "SABnzbd_nzo_ABC", "filename": "X"}])
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    out = unstick._try_client_orphan_cleanup("SABnzbd_nzo_ABC", dry_run=False)
    assert out["status"] == "sab-orphan-removed"


def test_client_orphan_dispatch_routes_qbit_shaped_id(tmp_path, monkeypatch):
    """A 40-char hex id must go straight to qBit's fallback and never touch
    SAB — the raising fake would blow up the test if it were reached."""
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(list_raises=True)
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    out = unstick._try_client_orphan_cleanup("a" * 40, dry_run=False)
    assert out["status"] == "qbit-login-failed"  # no qbit secrets in this env


def test_client_orphan_dispatch_sab_shaped_id_never_touches_qbit(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(slots=[{"nzo_id": "SABnzbd_nzo_ABC", "filename": "X"}])
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    with patch("unstick.QbitClient") as MockQbit:
        MockQbit.return_value.login.return_value = False
        out = unstick._try_client_orphan_cleanup("SABnzbd_nzo_ABC", dry_run=False)
    # This id is sab-shaped, so the dispatcher must route straight to SAB —
    # confirm qBit wasn't even consulted (unlike the true "unknown" case
    # tested below, which does probe qBit first).
    MockQbit.return_value.login.assert_not_called()
    assert out["status"] == "sab-orphan-removed"


def test_client_orphan_dispatch_unknown_shape_falls_through_to_sab_when_qbit_says_absent(
        tmp_path, monkeypatch):
    """qBit successfully logs in but has no matching hash — an ordinary "not
    found" (already-fully-removed), as opposed to qBit being unreachable.
    Per C5's probe order, the unknown-shape dispatch then continues on to
    SAB rather than stopping at qBit's answer."""
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake_sab = _FakeSabClient(slots=[{"nzo_id": "weird-id-123", "filename": "X"}])
    monkeypatch.setattr(unstick, "SabClient", lambda: fake_sab)
    with patch("unstick.QbitClient") as MockQbit:
        MockQbit.return_value.login.return_value = True
        MockQbit.return_value.list_torrents.return_value = []
        out = unstick._try_client_orphan_cleanup("weird-id-123", dry_run=False)
    assert out["status"] == "sab-orphan-removed"


def test_client_orphan_dispatch_no_id_short_circuits_without_asking_sab(tmp_path, monkeypatch):
    """A falsy id can't be found in either client. Confirm SAB is never
    consulted for the unanswerable question (the raising fake would blow up
    the test if it were reached)."""
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(list_raises=True)
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    out = unstick._try_client_orphan_cleanup(None, dry_run=False)
    assert out["status"] == "no-hash-for-qbit-lookup"


# -- effective-status accounting (daily cap) ---------------------------------

def test_sab_orphan_removed_is_an_effective_status():
    assert "sab-orphan-removed" in unstick._EFFECTIVE_STATUSES


def test_count_today_counts_sab_orphan_removed(tmp_path, monkeypatch):
    import datetime as dt_
    _, _, events = _setup(tmp_path)
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    today = events / f"{dt_.date.today().isoformat()}.jsonl"
    today.write_text("\n".join([
        '{"result":"sab-orphan-removed"}',
        '{"result":"sab-unreachable"}',
        '{"result":"sab-delete-failed"}',
        '{"result":"already-fully-removed"}',
        '{"result":"sab-orphan-removed"}',
    ]) + "\n")
    assert unstick._count_today() == 2


def test_run_end_to_end_counts_sab_orphan_removed_toward_cap(tmp_path, monkeypatch):
    """Full run() path: *arr says already-removed, SAB still has the slot ->
    sab-orphan-removed -> consumes a daily-cap slot exactly like
    qbit-orphan-removed does."""
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    fake = _FakeSabClient(slots=[{"nzo_id": "SABnzbd_nzo_ABC", "filename": "X"}])
    monkeypatch.setattr(unstick, "SabClient", lambda: fake)
    with patch("lib.arr_client.urllib.request.urlopen") as mock_open:
        mock_open.return_value = _resp({"records": []})
        res = unstick.run(slug="sonarr", hash_="SABnzbd_nzo_ABC", reason="t",
                          dry_run=False, state_file=state)
    assert res["status"] == "sab-orphan-removed"
    assert unstick._count_today() == 1


# -- resolve-by-nzo_id (documents the already-proven protocol-agnostic path) -

@patch("lib.arr_client.urllib.request.urlopen")
def test_resolve_queue_item_matches_sab_shaped_hash_unchanged(mock_open, tmp_path, monkeypatch):
    """C5/C9: _resolve_queue_item needed ZERO changes for SAB parity — it
    already matches purely on the *arr's downloadId string, whatever shape
    that string is. This test documents that proven behavior for an
    nzo_id-shaped hash, the same way test_resolve_queue_item_by_hash_found
    documents it for a qBit hash above."""
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    mock_open.return_value = _resp({"records": [
        {"id": 7, "downloadId": "SABnzbd_nzo_XYZ789", "title": "Some Episode"},
    ]})
    from lib.arr_client import ArrClient
    c = ArrClient("sonarr", "v3", secrets_dir=secrets)
    out = unstick._resolve_queue_item(c, hash_="SABnzbd_nzo_XYZ789", queue_id=None)
    assert out["status"] == "found"
    assert out["queue_id"] == 7
    assert out["title"] == "Some Episode"
