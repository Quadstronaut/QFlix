"""Tests for qflix-collect.py update_stale_state — ghost pruning.

Regression (2026-07-19): acted-on unstick candidates whose torrents were
long gone from qBit lingered in stale-state.json forever — nothing ever
removed a hash that stopped appearing in snapshots (the delta!=0 prune
needs 3 samples, which a gone torrent never produces). The heartbeat app
surfaced them as 5 phantom "stuck" downloads vs 0 real.

Extended 2026-07-19 for the SAB stuck-parity spec (C3/C4): SAB slots share
the same samples/hashes/ghost-prune machinery as qBit torrents (disjoint id
namespaces, "kind" field distinguishes them), plus the C4 escalation
circuit-breaker (`escalate_sab_if_pinned`) that fires SAB's restart_repair
when the ordinary unstick loop can't reach a wedged job.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

# Load qflix-collect.py as a module (it's a script, not a package).
ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "qflix_collect",
    ROOT / "scripts" / "maint" / "qflix-collect.py",
)
qc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qc)


GHOST = "45e79f57" + "0" * 32   # acted-on, torrent long gone from qBit
LIVE = "c4962b76" + "0" * 32    # healthy, downloading with progress


def _snapshot(torrents: list[dict], *, qbit_error: str | None = None,
              sab_slots: list[dict] | None = None,
              sab_queue_paused: bool = False,
              sab_error: str | None = None,
              sab_present: bool = False) -> dict:
    """Builds one snapshot dict. The `sab` key is OMITTED entirely unless the
    caller opts in (sab_slots / sab_error / sab_present=True) — that omission
    is what simulates a legacy pre-SAB snapshot for the ghost-prune tests
    below, and matches every pre-existing call site in this file exactly."""
    q: dict = {"torrents": torrents, "totals": {}}
    if qbit_error:
        q["error"] = qbit_error
    snap: dict = {"qbit": q}
    if sab_present or sab_slots is not None or sab_error is not None:
        sab: dict = {"slots": sab_slots or [], "queue": {"paused": sab_queue_paused},
                     "totals": {}}
        if sab_error:
            sab["error"] = sab_error
        snap["sab"] = sab
    return snap


def _torrent(h: str, downloaded: int, state: str = "downloading") -> dict:
    return {"hash": h, "downloaded_bytes": downloaded, "state": state,
            "progress": 0.5, "dl_speed_bytes_s": 500_000}


def _sab_slot(id_: str, downloaded: int, state: str = "Downloading") -> dict:
    return {"id": id_, "name": "Some.Release", "cat": "sonarr", "state": state,
            "size_bytes": 2_000_000, "downloaded_bytes": downloaded,
            "progress": 0.35, "dl_speed_bytes_s": 0}


def _seed(data_root: Path, snapshots: list[dict], stale_hashes: dict) -> None:
    snap_dir = data_root / "snapshots" / "2026-07-19"
    snap_dir.mkdir(parents=True)
    for i, snap in enumerate(snapshots):
        (snap_dir / f"{i:02d}.json").write_text(json.dumps(snap), encoding="utf-8")
    (data_root / "stale-state.json").write_text(
        json.dumps({"hashes": stale_hashes, "updated_at": "2026-07-19T00:00:00Z"}),
        encoding="utf-8")


def _stale_entry(acted: bool = True) -> dict:
    return {
        "first_zero_movement_at": "2026-07-15T16:00:00Z",
        "consecutive_zero_hours": 7,
        "last_progress": 0.62,
        "rule_matched": "stalledDL",
        "candidate_for_unstick": True,
        "acted_on_at": "2026-07-15T19:00:39Z" if acted else None,
    }


def _run(monkeypatch, tmp_path: Path) -> tuple[list[str], dict]:
    monkeypatch.setattr(qc, "DATA_ROOT", tmp_path)
    candidates = qc.update_stale_state()
    state = json.loads((tmp_path / "stale-state.json").read_text(encoding="utf-8"))
    return candidates, state["hashes"]


def test_ghost_hash_pruned(monkeypatch, tmp_path):
    """Hash absent from the latest snapshot's torrent list is dropped."""
    snaps = [_snapshot([_torrent(LIVE, d)]) for d in (100, 200, 300)]
    _seed(tmp_path, snaps, {GHOST: _stale_entry()})
    _, hashes = _run(monkeypatch, tmp_path)
    assert GHOST not in hashes


def test_qbit_error_snapshot_keeps_state(monkeypatch, tmp_path):
    """A failed qBit collect (error key, empty torrent list) must NOT
    mass-prune legitimate tracked state."""
    snaps = [_snapshot([_torrent(LIVE, d)]) for d in (100, 200)]
    snaps.append(_snapshot([], qbit_error="login_failed"))
    _seed(tmp_path, snaps, {GHOST: _stale_entry()})
    _, hashes = _run(monkeypatch, tmp_path)
    assert GHOST in hashes


def test_live_stalled_candidate_survives_prune(monkeypatch, tmp_path):
    """A genuinely stalled torrent (still in qBit, zero movement across
    3 snapshots) is tracked and survives the ghost prune."""
    snaps = [_snapshot([_torrent(LIVE, 100, state="stalledDL")]) for _ in range(3)]
    _seed(tmp_path, snaps, {})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert LIVE in hashes
    assert candidates == [LIVE]


def test_fewer_than_three_snapshots_no_prune(monkeypatch, tmp_path):
    """<3 snapshots = early return; state is rewritten untouched."""
    _seed(tmp_path, [_snapshot([])], {GHOST: _stale_entry()})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert candidates == []
    assert GHOST in hashes


# ---------------------------------------------------------------------------
# C3: SAB sample tracking, kind field, SAB rule dispatch
# ---------------------------------------------------------------------------

def test_sab_zero_movement_tracked_as_candidate(monkeypatch, tmp_path):
    """A SAB slot stuck in a downloadish state with zero byte movement
    across 3 snapshots becomes a sab-zero-movement candidate, kind='sab'."""
    sid = "SABnzbd_nzo_stuck1"
    snaps = [_snapshot([], sab_slots=[_sab_slot(sid, 500, state="Downloading")])
             for _ in range(3)]
    _seed(tmp_path, snaps, {})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert candidates == [sid]
    assert hashes[sid]["kind"] == "sab"
    assert hashes[sid]["rule_matched"] == "sab-zero-movement"
    assert hashes[sid]["candidate_for_unstick"] is True


def test_sab_paused_pinned_when_queue_not_paused(monkeypatch, tmp_path):
    """The object.py wedge: a Paused slot while the QUEUE itself is running
    is the force-paused-job bug, not an operator pause."""
    sid = "SABnzbd_nzo_wedged1"
    snaps = [_snapshot([], sab_slots=[_sab_slot(sid, 700, state="Paused")],
                       sab_queue_paused=False) for _ in range(3)]
    _seed(tmp_path, snaps, {})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert sid in candidates
    assert hashes[sid]["rule_matched"] == "sab-paused-pinned"
    assert hashes[sid]["kind"] == "sab"


def test_sab_paused_not_flagged_when_queue_paused(monkeypatch, tmp_path):
    """An operator-paused QUEUE is normal; a Paused slot under it must not
    be flagged as the wedge."""
    sid = "SABnzbd_nzo_opadmin1"
    snaps = [_snapshot([], sab_slots=[_sab_slot(sid, 700, state="Paused")],
                       sab_queue_paused=True) for _ in range(3)]
    _seed(tmp_path, snaps, {})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert candidates == []
    assert sid not in hashes


def test_sab_pp_hung_tracked_not_candidate(monkeypatch, tmp_path):
    """A hung post-processing step (Verifying/par2) is tracked with rule
    sab-pp-hung but candidate_for_unstick=False -- unstick's DELETE can't
    fix a hung unrar, so it must never be dispatched to the per-hour
    unstick loop (it feeds the stuck list + C4 escalation instead)."""
    sid = "SABnzbd_nzo_hungpar2a"
    snaps = [_snapshot([], sab_slots=[_sab_slot(sid, 900, state="Verifying")])
             for _ in range(3)]
    _seed(tmp_path, snaps, {})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert sid not in candidates
    assert hashes[sid]["rule_matched"] == "sab-pp-hung"
    assert hashes[sid]["candidate_for_unstick"] is False


def test_sab_healthy_state_not_tracked(monkeypatch, tmp_path):
    """A state outside all three SAB rule buckets (e.g. a completed-but-
    not-yet-moved-to-history 'Completed' slot) is not tracked at all."""
    sid = "SABnzbd_nzo_healthy1"
    snaps = [_snapshot([], sab_slots=[_sab_slot(sid, 900, state="Completed")])
             for _ in range(3)]
    _seed(tmp_path, snaps, {})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert candidates == []
    assert sid not in hashes


def test_legacy_entry_gets_qbit_kind_default(monkeypatch, tmp_path):
    """A stale-state.json entry written before SAB support (no 'kind' key)
    is backfilled to kind='qbit' the moment it's loaded."""
    legacy = _stale_entry(acted=False)
    assert "kind" not in legacy
    # Single snapshot -> early return path; only the load-time backfill runs.
    _seed(tmp_path, [_snapshot([])], {GHOST: legacy})
    _, hashes = _run(monkeypatch, tmp_path)
    assert hashes[GHOST]["kind"] == "qbit"


# ---------------------------------------------------------------------------
# C3: ghost-prune union matrix
# ---------------------------------------------------------------------------

def test_live_sab_id_survives_ghost_prune(monkeypatch, tmp_path):
    """A sab-kind entry whose id is still present in the latest snapshot's
    sab.slots survives the ghost prune."""
    sid = "SABnzbd_nzo_alive1"
    snaps = [_snapshot([], sab_slots=[_sab_slot(sid, 100, state="Downloading")])
             for _ in range(3)]
    stale = _stale_entry(acted=True)
    stale["kind"] = "sab"
    _seed(tmp_path, snaps, {sid: stale})
    _, hashes = _run(monkeypatch, tmp_path)
    assert sid in hashes


def test_gone_sab_id_pruned(monkeypatch, tmp_path):
    """A sab-kind entry absent from the latest snapshot's sab.slots (with
    the sab section present and healthy) is pruned, same as a gone qBit
    hash."""
    sid_gone = "SABnzbd_nzo_gone1"
    sid_alive = "SABnzbd_nzo_alive2"
    snaps = [_snapshot([], sab_slots=[_sab_slot(sid_alive, 100)]) for _ in range(3)]
    stale = _stale_entry(acted=True)
    stale["kind"] = "sab"
    _seed(tmp_path, snaps, {sid_gone: stale})
    _, hashes = _run(monkeypatch, tmp_path)
    assert sid_gone not in hashes


def test_qbit_error_protects_sab_ghost_too(monkeypatch, tmp_path):
    """qbit-error alone doesn't mass-prune sab: an errored qBit section
    skips the WHOLE prune this cycle -- a gone-looking SAB entry must
    survive too, not just qBit-kind ones."""
    sid_gone = "SABnzbd_nzo_gone3"
    snaps = [_snapshot([], sab_slots=[]) for _ in range(2)]
    snaps.append(_snapshot([], qbit_error="login_failed", sab_slots=[]))
    stale = _stale_entry(acted=True)
    stale["kind"] = "sab"
    _seed(tmp_path, snaps, {sid_gone: stale})
    _, hashes = _run(monkeypatch, tmp_path)
    assert sid_gone in hashes


def test_sab_error_protects_qbit_ghost_too(monkeypatch, tmp_path):
    """...and vice versa: an errored SAB section also skips the whole
    prune, so a gone-looking qBit ghost must survive too."""
    snaps = [_snapshot([_torrent(LIVE, d)]) for d in (100, 200)]
    snaps.append(_snapshot([_torrent(LIVE, 300)], sab_error="api_unreachable"))
    _seed(tmp_path, snaps, {GHOST: _stale_entry(acted=True)})
    _, hashes = _run(monkeypatch, tmp_path)
    assert GHOST in hashes


def test_sab_section_missing_protects_sab_but_not_qbit(monkeypatch, tmp_path):
    """A snapshot with NO 'sab' key at all (legacy snapshot / a collect run
    with --include lacking sab) is not an error -- it's just no evidence.
    qBit-kind ghosts still prune normally; sab-kind entries are left alone
    since there's no SAB live-set to judge them against this cycle."""
    sab_ghost = "SABnzbd_nzo_gone4"
    snaps = [_snapshot([_torrent(LIVE, d)]) for d in (100, 200, 300)]  # no sab key
    stale = {
        GHOST: _stale_entry(acted=True),
        sab_ghost: dict(_stale_entry(acted=True), kind="sab"),
    }
    _seed(tmp_path, snaps, stale)
    _, hashes = _run(monkeypatch, tmp_path)
    assert GHOST not in hashes      # qbit-kind ghost still pruned normally
    assert sab_ghost in hashes      # sab-kind protected: no sab data this cycle


# ---------------------------------------------------------------------------
# C4: escalation circuit-breaker
# ---------------------------------------------------------------------------

def _urlopen_resp(body: dict) -> MagicMock:
    m = MagicMock()
    m.read.return_value = json.dumps(body).encode("utf-8")
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


def _setup_sab_secrets(tmp_path: Path) -> Path:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "sabnzbd.port").write_text("8090", encoding="utf-8")
    (secrets / "sabnzbd.key").write_text("APIKEY", encoding="utf-8")
    return secrets


def _escalation_env(monkeypatch, tmp_path: Path) -> None:
    """Common wiring for escalation tests: isolated DATA_ROOT, no real
    Discord push, no real 60s verify sleep, and SAB creds pointed at a
    throwaway secrets dir."""
    monkeypatch.setattr(qc, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(qc, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(qc, "SAB_REPAIR_VERIFY_DELAY_S", 0)
    secrets = _setup_sab_secrets(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))


def test_escalation_strike_a_unstick_no_op_fires_restart_repair(monkeypatch, tmp_path):
    """Strike (a): unstick fired >=1h ago, the SAB slot is STILL there and
    STILL rule-matching -- the *arr DELETE no-oped against a wedged queue
    object (GH #802/#1104/#3106: mode=resume/delete return {"status":true}
    while doing nothing). This is the documented trigger for the
    restart_repair hammer, and the event line matches the C4 schema."""
    _escalation_env(monkeypatch, tmp_path)

    sid = "SABnzbd_nzo_wedged9"
    old_acted = (qc.utc_now() - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    stale = {
        "kind": "sab", "rule_matched": "sab-paused-pinned",
        "candidate_for_unstick": True, "acted_on_at": old_acted,
        "consecutive_zero_hours": 5, "last_progress": 0.3,
        "first_zero_movement_at": old_acted,
    }
    snap = _snapshot([], sab_slots=[_sab_slot(sid, 700, state="Paused")],
                      sab_queue_paused=False)
    _seed(tmp_path, [snap], {sid: stale})

    mock_open = MagicMock(side_effect=[
        _urlopen_resp({"status": True}),               # restart_repair
        _urlopen_resp({"queue": {"paused": False}}),    # verify re-poll
    ])
    monkeypatch.setattr(qc.urllib.request, "urlopen", mock_open)

    result = qc.escalate_sab_if_pinned()

    assert result["fired"] is True
    assert result["trigger"] == "strike-a-unstick-no-op"
    assert result["ids"] == [sid]
    assert mock_open.call_count == 2   # restart_repair + verify re-poll

    latch = tmp_path / "sab-repair-latch.epoch"
    assert latch.exists()

    events_file = list((tmp_path / "events").glob("*.jsonl"))[0]
    line = json.loads(events_file.read_text(encoding="utf-8").strip())
    assert line["action"] == "sab-restart-repair"
    assert line["trigger"] == "strike-a-unstick-no-op"
    assert line["ids"] == [sid]


def test_escalation_strike_a_requires_one_hour_elapsed(monkeypatch, tmp_path):
    """A too-recent acted_on_at (unstick just dispatched this cycle) must
    NOT strike -- give the ordinary unstick loop its full hour before
    escalating."""
    _escalation_env(monkeypatch, tmp_path)
    sid = "SABnzbd_nzo_freshact1"
    recent_acted = (qc.utc_now() - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    stale = {
        "kind": "sab", "rule_matched": "sab-paused-pinned",
        "candidate_for_unstick": True, "acted_on_at": recent_acted,
        "consecutive_zero_hours": 3, "last_progress": 0.3,
        "first_zero_movement_at": recent_acted,
    }
    snap = _snapshot([], sab_slots=[_sab_slot(sid, 700, state="Paused")],
                      sab_queue_paused=False)
    _seed(tmp_path, [snap], {sid: stale})
    result = qc.escalate_sab_if_pinned()
    assert result["fired"] is False


def test_escalation_strike_b_pp_hung_past_threshold_fires(monkeypatch, tmp_path):
    """Strike (b): a sab-pp-hung entry with consecutive_zero_hours >=
    PP_HUNG_ESCALATE_HOURS (default 4) fires restart_repair -- unstick was
    never even dispatched for these (candidate_for_unstick False), so
    strike (a) can't apply; this is their only path out."""
    _escalation_env(monkeypatch, tmp_path)
    sid = "SABnzbd_nzo_hungpar9"
    stale = {
        "kind": "sab", "rule_matched": "sab-pp-hung",
        "candidate_for_unstick": False, "acted_on_at": None,
        "consecutive_zero_hours": qc.PP_HUNG_ESCALATE_HOURS,
        "pp_state": "Verifying",
        "pp_same_state_hours": qc.PP_HUNG_ESCALATE_HOURS,
        "last_progress": 0.9, "first_zero_movement_at": qc.iso(),
    }
    snap = _snapshot([], sab_slots=[_sab_slot(sid, 900, state="Verifying")])
    _seed(tmp_path, [snap], {sid: stale})

    mock_open = MagicMock(side_effect=[
        _urlopen_resp({"status": True}),
        _urlopen_resp({"queue": {"paused": False}}),
    ])
    monkeypatch.setattr(qc.urllib.request, "urlopen", mock_open)

    result = qc.escalate_sab_if_pinned()
    assert result["fired"] is True
    assert result["trigger"] == "strike-b-pp-hung"
    assert sid in result["ids"]


def test_escalation_strike_b_below_threshold_does_not_fire(monkeypatch, tmp_path):
    """Below the escalation threshold, a sab-pp-hung entry is left alone --
    no network call should even be attempted."""
    _escalation_env(monkeypatch, tmp_path)
    sid = "SABnzbd_nzo_hungpar8"
    stale = {
        "kind": "sab", "rule_matched": "sab-pp-hung",
        "candidate_for_unstick": False, "acted_on_at": None,
        "consecutive_zero_hours": qc.PP_HUNG_ESCALATE_HOURS + 5,
        "pp_state": "Verifying",
        "pp_same_state_hours": qc.PP_HUNG_ESCALATE_HOURS - 1,
        "last_progress": 0.9, "first_zero_movement_at": qc.iso(),
    }
    snap = _snapshot([], sab_slots=[_sab_slot(sid, 900, state="Verifying")])
    _seed(tmp_path, [snap], {sid: stale})
    result = qc.escalate_sab_if_pinned()
    assert result["fired"] is False
    assert not (tmp_path / "sab-repair-latch.epoch").exists()


def test_escalation_latch_cooldown_prevents_second_fire(monkeypatch, tmp_path):
    """Max one fire per SAB_REPAIR_COOLDOWN_H: a second strike against the
    SAME unresolved slot, evaluated again before the cooldown window
    elapses, must not re-fire restart_repair."""
    _escalation_env(monkeypatch, tmp_path)
    sid = "SABnzbd_nzo_hungpar7"
    stale = {
        "kind": "sab", "rule_matched": "sab-pp-hung",
        "candidate_for_unstick": False, "acted_on_at": None,
        "consecutive_zero_hours": qc.PP_HUNG_ESCALATE_HOURS,
        "pp_state": "Verifying",
        "pp_same_state_hours": qc.PP_HUNG_ESCALATE_HOURS,
        "last_progress": 0.9, "first_zero_movement_at": qc.iso(),
    }
    snap = _snapshot([], sab_slots=[_sab_slot(sid, 900, state="Verifying")])
    _seed(tmp_path, [snap], {sid: stale})

    mock_open = MagicMock(side_effect=[
        _urlopen_resp({"status": True}),
        _urlopen_resp({"queue": {"paused": False}}),
    ])
    monkeypatch.setattr(qc.urllib.request, "urlopen", mock_open)

    first = qc.escalate_sab_if_pinned()
    assert first["fired"] is True

    second = qc.escalate_sab_if_pinned()
    assert second["fired"] is False
    assert second.get("skipped") == "cooldown"
    assert mock_open.call_count == 2   # no additional calls beyond the first fire


def test_escalation_no_strikes_no_op(monkeypatch, tmp_path):
    """No sab-kind entries at all -> nothing fires, nothing written."""
    _escalation_env(monkeypatch, tmp_path)
    snap = _snapshot([_torrent(LIVE, 100)])
    _seed(tmp_path, [snap], {})
    result = qc.escalate_sab_if_pinned()
    assert result == {"fired": False, "trigger": None, "ids": []}
    assert not (tmp_path / "sab-repair-latch.epoch").exists()
    assert not (tmp_path / "events").exists()


def test_escalation_never_raises_on_corrupt_state(monkeypatch, tmp_path):
    """A corrupt stale-state.json must degrade to a safe no-op, never an
    exception into main()'s flow."""
    monkeypatch.setattr(qc, "DATA_ROOT", tmp_path)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "stale-state.json").write_text("not json{{{", encoding="utf-8")
    result = qc.escalate_sab_if_pinned()
    assert result["fired"] is False


# -- sab-orphan-removed parity with unstick.py's _EFFECTIVE_STATUSES --------
# (C5, SAB stuck-parity spec 2026-07-19): unstick.py counts "sab-orphan-
# removed" as an effective/terminal action; qflix-collect.py keeps its own
# mirror tuples per the compartmentalization law and must carry it too.

def test_sab_orphan_removed_is_an_effective_result():
    assert "sab-orphan-removed" in qc._EFFECTIVE_RESULTS


def test_sab_orphan_removed_is_a_terminal_status():
    assert "sab-orphan-removed" in qc._TERMINAL_STATUSES


def test_count_todays_actions_counts_sab_orphan_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(qc, "DATA_ROOT", tmp_path)
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True)
    today = qc.utc_now().strftime("%Y-%m-%d")
    (events_dir / (today + ".jsonl")).write_text("\n".join([
        json.dumps({"result": "sab-orphan-removed"}),
        json.dumps({"result": "sab-unreachable"}),
        json.dumps({"result": "sab-delete-failed"}),
        json.dumps({"result": "already-fully-removed"}),
        json.dumps({"result": "sab-orphan-removed"}),
    ]) + "\n", encoding="utf-8")
    assert qc.count_todays_actions() == 2


def test_act_on_candidates_stamps_acted_on_for_sab_orphan_removed(monkeypatch, tmp_path):
    """A 'sab-orphan-removed' result from unstick.py must stamp acted_on_at
    (terminal status), exactly like 'qbit-orphan-removed' does — otherwise
    the SAB-side twin of a cleared candidate would never leave stale-state.json."""
    monkeypatch.setattr(qc, "DATA_ROOT", tmp_path)
    h = "SABnzbd_nzo_ABC"
    state_file = tmp_path / "stale-state.json"
    state_file.write_text(json.dumps({
        "hashes": {h: {"acted_on_at": None, "kind": "sab"}},
        "updated_at": qc.iso(),
    }), encoding="utf-8")

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = json.dumps({"status": "sab-orphan-removed"})
    monkeypatch.setattr(qc, "_run_mcp", lambda *a, **k: fake_result)

    acted = qc.act_on_candidates([h])
    assert acted == [h]
    loaded = json.loads(state_file.read_text(encoding="utf-8"))
    assert loaded["hashes"][h]["acted_on_at"] is not None


def test_collect_snapshot_requests_sab_section(monkeypatch, tmp_path):
    """Regression (2026-07-20 review crit): the hourly collector must ask
    collect.py for the sab section or the whole SAB parity pipeline is dead
    in production regardless of collect.py's own default."""
    seen = {}

    class _R:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def _fake_run_mcp(script, args, timeout):
        seen["script"], seen["args"] = script, args
        return _R()

    monkeypatch.setattr(qc, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(qc, "_run_mcp", _fake_run_mcp)
    qc.collect_snapshot()
    include = seen["args"][seen["args"].index("--include") + 1]
    assert "sab" in include.split(",")


# ---------------------------------------------------------------------------
# Council 2026-07-20 follow-ups: D1 (latch not burned on no-op), D2 (breaker
# doesn't interrupt healthy long PP), D7 (malformed env knob degrades).
# ---------------------------------------------------------------------------

def test_escalation_no_secrets_does_not_burn_latch(monkeypatch, tmp_path):
    """Defect 1: when restart_repair is NEVER ISSUED (no-secrets), the 24h
    cooldown latch must NOT be stamped and no fire recorded -- else a single
    transient secrets glitch burns the breaker's whole daily budget on a
    call that never left the box."""
    monkeypatch.setattr(qc, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(qc, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(qc, "SAB_REPAIR_VERIFY_DELAY_S", 0)
    # Point secrets at an EMPTY dir so _sab_api raises FileNotFoundError ->
    # _sab_restart_repair returns "error:no-secrets:...".
    empty = tmp_path / "nosecrets"
    empty.mkdir()
    monkeypatch.setenv("MANITOBA_SECRETS", str(empty))

    sid = "SABnzbd_nzo_wedged1"
    old_acted = (qc.utc_now() - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    stale = {"kind": "sab", "rule_matched": "sab-paused-pinned",
             "candidate_for_unstick": True, "acted_on_at": old_acted,
             "consecutive_zero_hours": 5, "last_progress": 0.3,
             "first_zero_movement_at": old_acted}
    snap = _snapshot([], sab_slots=[_sab_slot(sid, 700, state="Paused")],
                     sab_queue_paused=False)
    _seed(tmp_path, [snap], {sid: stale})

    result = qc.escalate_sab_if_pinned()
    assert result["fired"] is False
    assert str(result.get("outcome", "")).startswith("error:no-secrets")
    assert not (tmp_path / "sab-repair-latch.epoch").exists()   # latch NOT burned
    assert not (tmp_path / "events").exists() or \
        not list((tmp_path / "events").glob("*.jsonl"))          # no fire event


def test_pp_hung_healthy_state_transition_does_not_escalate(monkeypatch, tmp_path):
    """Defect 2: a huge release legitimately moving Verifying -> Repairing ->
    Extracting shows zero downloaded-delta for hours (consecutive_zero_hours
    climbs) but its PP STATE keeps advancing -- update_stale_state must reset
    pp_same_state_hours on each transition so the breaker never interrupts
    healthy long post-processing."""
    monkeypatch.setattr(qc, "DATA_ROOT", tmp_path)
    sid = "SABnzbd_nzo_bigremux"
    # 3 snapshots, zero downloaded-delta, but state advances each hour.
    snaps = [
        _snapshot([], sab_slots=[_sab_slot(sid, 900, state=st)])
        for st in ("Verifying", "Repairing", "Extracting")
    ]
    _seed(tmp_path, snaps, {})
    qc.update_stale_state()
    entry = json.loads((tmp_path / "stale-state.json").read_text())["hashes"][sid]
    assert entry["rule_matched"] == "sab-pp-hung"
    # state changed on the latest sample -> stability clock reset to 0
    assert entry.get("pp_same_state_hours") == 0
    assert entry.get("pp_state") == "Extracting"


def test_pp_hung_same_state_accrues_stability_hours(monkeypatch, tmp_path):
    """Complement: a job WEDGED in one PP state across the window accrues
    pp_same_state_hours so the breaker can eventually fire on it."""
    monkeypatch.setattr(qc, "DATA_ROOT", tmp_path)
    sid = "SABnzbd_nzo_wedgepp"
    snaps = [_snapshot([], sab_slots=[_sab_slot(sid, 900, state="Repairing")])
             for _ in range(3)]
    # pre-seed an entry already sitting in Repairing so the update increments.
    pre = {"kind": "sab", "rule_matched": "sab-pp-hung",
           "candidate_for_unstick": False, "acted_on_at": None,
           "consecutive_zero_hours": qc.ZERO_MOVEMENT_HOURS,
           "pp_state": "Repairing", "pp_same_state_hours": 2,
           "last_progress": 0.9, "first_zero_movement_at": qc.iso()}
    _seed(tmp_path, snaps, {sid: pre})
    qc.update_stale_state()
    entry = json.loads((tmp_path / "stale-state.json").read_text())["hashes"][sid]
    assert entry["pp_state"] == "Repairing"
    assert entry["pp_same_state_hours"] == 3   # incremented, not reset


def test_env_int_malformed_degrades_to_default(monkeypatch):
    """Defect 7: a malformed env knob must fall back to the default, not
    raise at import and kill the whole collect cycle."""
    monkeypatch.setenv("PP_HUNG_ESCALATE_HOURS", "not-a-number")
    assert qc._env_int("PP_HUNG_ESCALATE_HOURS", 4) == 4
    monkeypatch.setenv("PP_HUNG_ESCALATE_HOURS", "")
    assert qc._env_int("PP_HUNG_ESCALATE_HOURS", 4) == 4
    monkeypatch.delenv("PP_HUNG_ESCALATE_HOURS", raising=False)
    assert qc._env_int("PP_HUNG_ESCALATE_HOURS", 4) == 4
    monkeypatch.setenv("PP_HUNG_ESCALATE_HOURS", "6")
    assert qc._env_int("PP_HUNG_ESCALATE_HOURS", 4) == 6


# --- 2026-08-07: never-started SAB slots are queued, not stuck --------------


def test_sab_slot_that_never_started_is_NOT_a_candidate(monkeypatch, tmp_path):
    """THE REGRESSION, end to end. A slot with zero downloaded bytes across all
    three snapshots is waiting its turn behind the head of the queue, not
    stalled — SAB transfers one nzb at a time while labelling every queued slot
    "Downloading". Before this, such slots became unstick candidates, and
    act_on_candidates DELETES AND BLOCKLISTS them: 10 legitimate Vanderpump
    releases went that way at 20:00Z on 2026-08-07.

    This is the integration half. The rule can be perfectly correct and still
    never fire if the collector does not pass has_started — so assert the
    behaviour, not the predicate."""
    sid = "SABnzbd_nzo_neverstarted"
    snaps = [_snapshot([], sab_slots=[_sab_slot(sid, 0, state="Downloading")])
             for _ in range(3)]
    _seed(tmp_path, snaps, {})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert candidates == [], "a never-started slot must not be an unstick candidate"
    assert sid not in hashes, "and must not accrue zero-movement hours"


def test_sab_slot_that_started_then_stalled_IS_still_a_candidate(monkeypatch, tmp_path):
    """The exemption must not blind the detector. Bytes received, then flat."""
    sid = "SABnzbd_nzo_realstall"
    snaps = [_snapshot([], sab_slots=[_sab_slot(sid, 750, state="Downloading")])
             for _ in range(3)]
    _seed(tmp_path, snaps, {})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert candidates == [sid]
    assert hashes[sid]["rule_matched"] == "sab-zero-movement"


def test_sab_slot_that_started_mid_window_counts_as_started(monkeypatch, tmp_path):
    """has_started reads ANY sample, not the oldest. A slot at 0 bytes in the
    first snapshot that has bytes by the third genuinely started — and its
    byte-delta is non-zero anyway, so it is popped as progressing. Pinned so a
    future 'use sm[0]' refactor cannot quietly reintroduce the false negative."""
    sid = "SABnzbd_nzo_latestart"
    snaps = [_snapshot([], sab_slots=[_sab_slot(sid, n, state="Downloading")])
             for n in (0, 400, 900)]
    _seed(tmp_path, snaps, {})
    candidates, hashes = _run(monkeypatch, tmp_path)
    assert candidates == [], "a slot making progress is not stuck"
    assert sid not in hashes
