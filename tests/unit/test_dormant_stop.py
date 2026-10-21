"""QFLX-55: lib/dormant_stop.py puts a woken dormant UCC container back to sleep.

The worker is driven synchronously (_worker) with subprocess, the parity
re-check, notify and sleep patched: no thread, no box, no Discord.
"""
from __future__ import annotations

import itertools
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lib import dormant_stop as ds


def _app(name="sonarr"):
    return SimpleNamespace(name=name, raw={"ucc_slug": name, "unit": f"qflix-{name}.service"})


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MANITOBA_APPCTL", "/fake/appctl")
    ds._in_flight.clear()
    yield


def _work(app, woken_seq, rc=0):
    seq = iter(woken_seq)
    cp = subprocess.CompletedProcess([], rc, stdout="", stderr="")
    with patch("lib.dormant_stop.subprocess.run", return_value=cp) as run, \
         patch("lib.dormant_stop._still_woken", side_effect=lambda a: next(seq)), \
         patch("lib.dormant_stop.time.sleep"), \
         patch("lib.dormant_stop.time.monotonic", side_effect=itertools.count(0, 10)), \
         patch("lib.dormant_stop.notify_mod.notify") as note:
        ds._worker(app)
    return run, note


def test_is_woken_only_for_the_woken_predicate():
    assert ds.is_woken(["dormant container woken: sonarr pid(s) 7"])
    assert not ds.is_woken(["2 process trees match ExecStart /x"])
    assert not ds.is_woken([])


def test_stops_via_appctl_stop_dormant_and_notes_once(tmp_path):
    run, note = _work(_app(), [True, False])
    assert run.call_args.args[0] == ["/fake/appctl", "stop-dormant", "sonarr"]
    note.assert_called_once()
    assert note.call_args.kwargs["level"] == "info"
    assert "qflix-sonarr.service" in note.call_args.args[0]
    _, note2 = _work(_app(), [False])          # wakes again later the same day
    note2.assert_not_called()                  # one note per cooldown, from the FILE ledger
    assert "sonarr" in json.loads((tmp_path / "dormant-stops.json").read_text())


def test_still_running_after_stop_pages_an_error_naming_the_safe_command():
    _, note = _work(_app(), [True] * 100, rc=1)
    note.assert_called_once()
    msg = note.call_args.args[0]
    assert note.call_args.kwargs["level"] == "error"
    assert "app-sonarr stop" in msg and "NOT `appctl stop`" in msg


def test_corrupt_ledger_fails_open(tmp_path):
    (tmp_path / "dormant-stops.json").write_text("{not json")
    _, note = _work(_app(), [False])
    note.assert_called_once()


def test_one_stop_in_flight_per_app():
    with patch("lib.dormant_stop.threading.Thread"):
        assert ds.trigger_async(_app()) == "started"
        assert ds.trigger_async(_app()) == "already_running"
        assert ds.trigger_async(_app("unpackerr")) == "started"


def test_worker_releases_the_in_flight_slot_even_on_crash():
    ds._in_flight.add("sonarr")
    with patch("lib.dormant_stop.subprocess.run", side_effect=RuntimeError("boom")), \
         patch("lib.dormant_stop._still_woken", side_effect=RuntimeError("boom2")), \
         patch("lib.dormant_stop.notify_mod.notify"):
        ds._worker(_app())
    assert "sonarr" not in ds._in_flight
