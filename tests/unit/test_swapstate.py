"""lib/swapstate.py - swap state + recorded listen set (QFLX-20, spec 5.8)."""
from __future__ import annotations

import json
import threading

import pytest

from lib import swapstate

SS = """\
LISTEN 0 4096 127.0.0.1:42050 0.0.0.0:*
LISTEN 0 4096 172.17.0.1:42050 0.0.0.0:*
LISTEN 0 4096 127.0.0.1:42051 0.0.0.0:*
LISTEN 0 128 [::]:42050 [::]:*
"""


@pytest.fixture(autouse=True)
def _swap_dir(tmp_path, monkeypatch):
    # Resolved lazily: setenv after import must take effect.
    monkeypatch.setenv("QFLIX_SWAP_DIR", str(tmp_path / "swap"))


def test_parse_listen_keeps_only_the_port_sorted_unique():
    assert swapstate.parse_listen(SS, 42050) == [
        "127.0.0.1:42050", "172.17.0.1:42050", "[::]:42050"]
    assert swapstate.parse_listen(SS, 9) == []


def test_parse_listen_accepts_rows_without_state_column():
    assert swapstate.parse_listen("0 4096 127.0.0.1:7878 0.0.0.0:*\n", 7878) == [
        "127.0.0.1:7878"]


def test_capture_writes_listen_set_and_state(tmp_path):
    got = swapstate.capture("sonarr", SS, 42050, ucc_version="4.0.20")
    assert got == ["127.0.0.1:42050", "172.17.0.1:42050", "[::]:42050"]
    base = tmp_path / "swap" / "sonarr"
    assert (base / "listen-set.before").read_text().splitlines() == got
    st = json.loads((base / "state.json").read_text())
    assert st["ucc_version"] == "4.0.20"
    assert st["port"] == 42050
    assert st["rollback_window"] == "open"
    assert st["swap_date"] is None and st["soak_until"] is None


def test_recapture_never_resets_swap_bookkeeping():
    swapstate.capture("sonarr", SS, 42050, ucc_version="4.0.20")
    swapstate.update_state("sonarr", swap_date="2026-10-20", soak_until="2026-11-03",
                           rollback_window="closed")
    swapstate.capture("sonarr", SS, 42050)
    st = swapstate.load_state("sonarr")
    assert (st["swap_date"], st["soak_until"], st["rollback_window"]) == (
        "2026-10-20", "2026-11-03", "closed")
    assert st["ucc_version"] == "4.0.20"


def test_update_state_rejects_bad_input():
    with pytest.raises(swapstate.SwapStateError):
        swapstate.update_state("sonarr", rollback_window="maybe")
    with pytest.raises(swapstate.SwapStateError):
        swapstate.update_state("sonarr", port="1")
    with pytest.raises(swapstate.SwapStateError):
        swapstate.capture("../evil", SS, 1)


def test_diff_listen_reports_added_removed_and_honours_exceptions(tmp_path):
    swapstate.capture("sonarr", SS, 42050)
    same = swapstate.diff_listen("sonarr", SS)
    assert same == {"added": [], "removed": []}
    now = SS.replace("[::]:42050", "0.0.0.0:42050").replace(
        "172.17.0.1:42050", "127.0.0.2:42050")
    d = swapstate.diff_listen("sonarr", now)
    assert d["added"] == ["0.0.0.0:42050", "127.0.0.2:42050"]
    assert d["removed"] == ["172.17.0.1:42050", "[::]:42050"]
    p = tmp_path / "swap" / "sonarr" / "state.json"
    st = json.loads(p.read_text())
    st["exceptions"] = ["0.0.0.0:42050", "[::]:42050"]
    p.write_text(json.dumps(st))
    d = swapstate.diff_listen("sonarr", now)
    assert d == {"added": ["127.0.0.2:42050"], "removed": ["172.17.0.1:42050"]}


def test_add_exceptions_records_union_and_is_honoured_by_diff():
    swapstate.capture("tautulli", SS, 42050)
    swapstate.add_exceptions("tautulli", ["172.17.0.1:42050"])
    st = swapstate.add_exceptions("tautulli", ["[::]:42050", "172.17.0.1:42050"])
    assert st["exceptions"] == ["172.17.0.1:42050", "[::]:42050"]
    assert st["port"] == 42050 and st["rollback_window"] == "open"   # untouched
    only_loopback = "LISTEN 0 4096 127.0.0.1:42050 0.0.0.0:*\n"
    assert swapstate.diff_listen("tautulli", only_loopback) == {"added": [], "removed": []}
    # a NEW unexpected listener is still reported
    d = swapstate.diff_listen("tautulli", only_loopback + "LISTEN 0 1 0.0.0.0:42050 0.0.0.0:*\n")
    assert d["added"] == ["0.0.0.0:42050"]


@pytest.mark.parametrize("bad", ["", "nope", "1.2.3.4", "1.2.3.4:x", "a b:1"])
def test_add_exceptions_rejects_malformed_addresses(bad):
    swapstate.capture("tautulli", SS, 42050)
    with pytest.raises(swapstate.SwapStateError):
        swapstate.add_exceptions("tautulli", [bad])


def test_add_exception_cli(tmp_path, capsys):
    swapstate.capture("tautulli", SS, 42050)
    assert swapstate.main(["add-exception", "tautulli", "172.17.0.1:42050"]) == 0
    assert json.loads(capsys.readouterr().out) == ["172.17.0.1:42050"]


def test_diff_without_baseline_raises_not_clean():
    with pytest.raises(swapstate.SwapStateError):
        swapstate.diff_listen("never-captured", SS)


def test_swapped_slugs_needs_a_swap_date():
    swapstate.capture("a", SS, 42050)
    swapstate.capture("b", SS, 42050)
    swapstate.update_state("b", swap_date="2026-10-20")
    assert swapstate.swapped_slugs() == ["b"]


@pytest.mark.skipif(swapstate.fcntl is None, reason="flock is POSIX-only (fail-open elsewhere)")
def test_concurrent_updates_do_not_lose_fields():
    """flock around the whole read-modify-write: two writers, both keys land."""
    swapstate.capture("a", SS, 42050)
    errs = []

    def w(**kw):
        try:
            for _ in range(20):
                swapstate.update_state("a", **kw)
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    t1 = threading.Thread(target=w, kwargs={"swap_date": "2026-10-20"})
    t2 = threading.Thread(target=w, kwargs={"soak_until": "2026-11-03"})
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errs
    st = swapstate.load_state("a")
    assert st["swap_date"] == "2026-10-20" and st["soak_until"] == "2026-11-03"


def test_cli_capture_and_diff_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path / "sec"))
    (tmp_path / "sec").mkdir()
    (tmp_path / "sec" / "sonarr.port").write_text("42050\n")
    man = tmp_path / "apps.yaml"
    man.write_text("apps:\n  sonarr:\n    class: ucc\n    health: {kind: http_api, port_secret: sonarr.port}\n"
                   "  unpackerr:\n    class: ucc\n    health: {kind: process_pattern}\n")
    ssf = tmp_path / "ss.txt"
    ssf.write_text(SS)
    assert swapstate.main(["capture", "sonarr", "--manifest", str(man), "--ss-file", str(ssf)]) == 0
    assert swapstate.main(["diff", "sonarr", "--ss-file", str(ssf)]) == 0
    ssf.write_text("")
    assert swapstate.main(["diff", "sonarr", "--ss-file", str(ssf)]) == 1
    # app with no port secret is skipped loudly but not an error
    assert swapstate.main(["capture", "unpackerr", "--manifest", str(man), "--ss-file", str(ssf)]) == 0
    assert swapstate.recorded_listen("unpackerr") is None
    capsys.readouterr()
    assert swapstate.main(["ucc-slugs", "--manifest", str(man)]) == 0
    assert capsys.readouterr().out.split() == ["sonarr", "unpackerr"]


# --- QFLX-21: soak gate + rollback-window close --------------------------------

import datetime as _dt

_NOW = _dt.datetime(2026, 11, 1, 12, 0, tzinfo=_dt.timezone.utc)


def test_soak_gate_open_when_never_swapped():
    assert swapstate.soak_gate("sonarr", now=_NOW)["refused"] is False


def test_soak_gate_refuses_inside_the_window():
    swapstate.update_state("sonarr", swap_date="2026-10-25",
                           soak_until="2026-11-08T00:00:00Z")
    g = swapstate.soak_gate("sonarr", now=_NOW)
    assert g["refused"] is True and "2026-11-08" in g["reason"]


def test_soak_gate_allows_after_the_window_and_accepts_date_only():
    swapstate.update_state("sonarr", swap_date="2026-10-01", soak_until="2026-10-15")
    assert swapstate.soak_gate("sonarr", now=_NOW)["refused"] is False


def test_soak_gate_fails_closed_on_unparseable_soak_until():
    swapstate.update_state("sonarr", swap_date="2026-10-25", soak_until="soonish")
    assert swapstate.soak_gate("sonarr", now=_NOW)["refused"] is True


def test_soak_gate_fails_closed_when_swapped_without_soak_until():
    swapstate.update_state("sonarr", swap_date="2026-10-25")
    assert swapstate.soak_gate("sonarr", now=_NOW)["refused"] is True


def test_close_rollback_window_is_idempotent_and_keeps_other_fields():
    swapstate.update_state("sonarr", swap_date="2026-10-01", soak_until="2026-10-15")
    assert swapstate.close_rollback_window("sonarr") is True    # changed
    assert swapstate.close_rollback_window("sonarr") is False   # already closed
    st = swapstate.load_state("sonarr")
    assert st["rollback_window"] == "closed" and st["swap_date"] == "2026-10-01"


def test_close_rollback_window_noop_for_unswapped_slug(tmp_path):
    assert swapstate.close_rollback_window("sonarr") is False
    assert not (tmp_path / "swap" / "sonarr").exists()


def test_cli_soak_check_exit_codes(capsys):
    swapstate.update_state("sonarr", swap_date="2026-10-25", soak_until="2999-01-01")
    assert swapstate.main(["soak-check", "sonarr"]) == 1
    assert swapstate.main(["soak-check", "nope"]) == 0
    assert swapstate.main(["close-window", "sonarr"]) == 0
    assert swapstate.load_state("sonarr")["rollback_window"] == "closed"


def test_merge_refuses_corrupt_state_instead_of_erasing_swap_record(tmp_path):
    swapstate.capture("sonarr", SS, 42050)
    swapstate.update_state("sonarr", swap_date="2026-10-20", soak_until="2026-11-03")
    sj = tmp_path / "swap" / "sonarr" / "state.json"
    sj.write_text("{not json")
    with pytest.raises(swapstate.SwapStateError):
        swapstate.update_state("sonarr", rollback_window="closed")
    assert sj.read_text() == "{not json"      # left for the operator, not overwritten
