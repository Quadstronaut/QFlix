"""Rolling observation ledger for hardlink-integrity (fix 2026-08-06).

BACKGROUND. This box deliberately runs qBit's completed pool near-empty (the
torrent janitor purges *arr-untracked leftovers daily; qBit's own ratio
cleanup removes seeds). MIN_SAMPLE=5 CONCURRENT torrents was consequently
almost never reachable from a single snapshot -- measured 2026-08 at 2-3
total -- so the canary's own vacuity clock (see test_hardlink_vacuity_clock.py)
was about to start firing weekly forever. Lowering MIN_SAMPLE is explicitly
NOT the fix: a tiny denominator is precisely the failure mode that retired
BOTH prior designs of this canary (see the script's module header).

The fix accumulates a verdict per TORRENT (hardlinked/detached; orphans stay
excluded exactly as before) keyed by qBit's stable info-hash into a rolling
ledger at ~/.opt/maint/hardlink-integrity/observations.json, and evaluates
MIN_SAMPLE against that ACCUMULATED distinct-torrent count instead of the
concurrent snapshot.

TEST STRATEGY. Two layers, matching the precedent set by
test_hardlink_vacuity_clock.py (whose own docstring notes it drives threshold
logic through the empty-pool exit specifically so it needs no seedbox
library layout -- LIB_ROOTS in the script are hardcoded absolute
/home/quadstronaut/... paths that do not exist on a dev workstation):

  1. Ledger primitives (_load_observations / _save_observations /
     _prune_observations / _record_observation) are extracted and exec'd in
     isolation and tested directly -- this is where "no double-count on
     re-observation" and "verdict overwrite is deliberate" are proven, since
     those are exactly what _record_observation does and neither needs a
     qBit payload nor a library walk.
  2. The full embedded script is run as a subprocess (same technique as the
     vacuity-clock tests) with a PRE-SEEDED ledger file and an EMPTY current
     torrent list -- this exercises the real MIN_SAMPLE-against-accumulator
     assertion, the vacuity-clock interaction, and TTL pruning end to end,
     without needing the library walk (an empty current pool skips it, as it
     always did for the empty-pool case).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts" / "canaries" / "hardlink-integrity.sh"

DAY = 86400


def _embedded_python() -> str:
    """Lift the python heredoc out of the shell wrapper. Pinned deliberately:
    if the delimiter or heredoc style changes, this raises instead of
    silently testing an empty string and passing."""
    src = CANARY.read_text(encoding="utf-8")
    start = src.index('python3 <<"PYEND"') + len('python3 <<"PYEND"')
    end = src.index("\nPYEND", start)
    body = src[start:end]
    assert "_record_observation" in body, "extracted the wrong block"
    return body


def _ledger_functions_source(full_source: str) -> str:
    """Slice out ONLY the ledger primitives (constants + the four functions),
    skipping everything before them (which includes an unconditional
    `open("/tmp/qfh-completed.json")` this slice must not trigger) and
    everything after (the top-level classification/assertion logic, which
    needs a real qBit payload + library layout). Prepends the stdlib imports
    those functions use."""
    start_marker = 'OBS_STATE_DIR = os.path.expanduser("~/.opt/maint/hardlink-integrity")'
    end_marker = "observations, pruned_n = _prune_observations("
    start = full_source.index(start_marker)
    end = full_source.index(end_marker, start)
    assert start > 0 and end > start, "could not locate the ledger primitives slice"
    return "import json, os, sys, time\n" + full_source[start:end]


@pytest.fixture(scope="module")
def code():
    return _embedded_python()


@pytest.fixture()
def ledger_ns(code, tmp_path, monkeypatch):
    """Exec just the ledger primitives into a fresh namespace, with HOME
    pointed at tmp_path so OBS_STATE_DIR/OBS_STATE_PATH land under it exactly
    as the real script's os.path.expanduser("~/...") would on the box."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser on Windows
    ns = {}
    exec(compile(_ledger_functions_source(code), "ledger_primitives", "exec"), ns)
    return ns


def _obs_state_file(tmp_path) -> Path:
    return tmp_path / ".opt" / "maint" / "hardlink-integrity" / "observations.json"


def _seed_observations(tmp_path, entries):
    """entries: list of (hash, verdict, first_seen_days_ago, last_seen_days_ago)."""
    now = time.time()
    obs = {}
    for h, verdict, first_days_ago, last_days_ago in entries:
        obs[h] = {
            "verdict": verdict,
            "first_seen": now - first_days_ago * DAY,
            "last_seen": now - last_days_ago * DAY,
        }
    p = _obs_state_file(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"observations": obs}), encoding="utf-8")


def _run(code, tmp_path, torrents, *, min_sample="5", max_detached="2",
         max_detached_pct="5", ttl_days="14", max_vacuous_days="7"):
    """Run the embedded canary python with a fake HOME and torrent payload,
    exactly as test_hardlink_vacuity_clock.py does."""
    payload = tmp_path / "qfh-completed.json"
    payload.write_text(json.dumps(torrents), encoding="utf-8")

    body = code.replace('"/tmp/qfh-completed.json"', json.dumps(str(payload)))

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env["MIN_SAMPLE"] = min_sample
    env["MAX_DETACHED"] = max_detached
    env["MAX_DETACHED_PCT"] = max_detached_pct
    env["OBSERVATION_TTL_DAYS"] = ttl_days
    env["MAX_VACUOUS_DAYS"] = max_vacuous_days
    script = tmp_path / "canary_body.py"
    script.write_text(body, encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], env=env,
                          capture_output=True, text=True)


# =============================================================================
# Layer 1: ledger primitives, tested directly (no subprocess, no qBit payload)
# =============================================================================

class TestRecordObservation:
    """_record_observation is the merge step the per-torrent classification
    loop calls once per resolved torrent. Testing it directly is what proves
    'no double-count on re-observation' and 'verdict overwrite is deliberate'
    without needing the hardcoded /home/quadstronaut library-walk fixture."""

    def test_new_hash_creates_one_entry(self, ledger_ns):
        obs = ledger_ns["_record_observation"]({}, "h1", "hardlinked", 1000)
        assert len(obs) == 1
        assert obs["h1"] == {"verdict": "hardlinked", "first_seen": 1000, "last_seen": 1000}

    def test_reobserving_same_hash_does_not_double_count(self, ledger_ns):
        """THE POINT: two observations of the SAME torrent (e.g. two canary
        runs seeing it still completed) must accumulate to ONE ledger entry,
        not two -- accumulation counts distinct torrents, not distinct polls."""
        rec = ledger_ns["_record_observation"]
        obs = rec({}, "h1", "hardlinked", 1000)
        obs = rec(obs, "h1", "hardlinked", 2000)
        assert len(obs) == 1, "re-observing the same hash grew the ledger"
        assert obs["h1"]["last_seen"] == 2000, "last_seen must advance"
        assert obs["h1"]["first_seen"] == 1000, "first_seen must not move"

    def test_reobservation_overwrites_verdict_explicitly(self, ledger_ns):
        """A genuine operator fix flips detached -> hardlinked on
        re-observation; the ledger must show the CURRENT truth, not freeze on
        the first bad reading forever."""
        rec = ledger_ns["_record_observation"]
        obs = rec({}, "h1", "detached", 1000)
        obs = rec(obs, "h1", "hardlinked", 2000)
        assert obs["h1"]["verdict"] == "hardlinked"
        assert obs["h1"]["first_seen"] == 1000, "identity timeline survives the flip"

    def test_reobservation_can_also_regress_hardlinked_to_detached(self, ledger_ns):
        """Symmetric case: the overwrite is not a one-way ratchet toward good
        news -- it reflects whatever THIS run actually saw."""
        rec = ledger_ns["_record_observation"]
        obs = rec({}, "h1", "hardlinked", 1000)
        obs = rec(obs, "h1", "detached", 2000)
        assert obs["h1"]["verdict"] == "detached"

    def test_distinct_hashes_accumulate_separately(self, ledger_ns):
        rec = ledger_ns["_record_observation"]
        obs = rec({}, "h1", "hardlinked", 1000)
        obs = rec(obs, "h2", "detached", 1000)
        assert len(obs) == 2


class TestPruneObservations:
    def test_stale_entry_is_pruned(self, ledger_ns):
        now = 100 * DAY
        obs = {
            "fresh": {"verdict": "hardlinked", "first_seen": 0, "last_seen": now - 1 * DAY},
            "stale": {"verdict": "hardlinked", "first_seen": 0, "last_seen": now - 20 * DAY},
        }
        kept, pruned_n = ledger_ns["_prune_observations"](obs, now, 14)
        assert list(kept.keys()) == ["fresh"]
        assert pruned_n == 1

    def test_entry_exactly_at_ttl_boundary_survives(self, ledger_ns):
        """cutoff is now - ttl_days*86400; last_seen == cutoff must be KEPT
        (>=), not pruned -- an off-by-one here would silently shrink the
        window every run."""
        now = 100 * DAY
        obs = {"h1": {"verdict": "hardlinked", "first_seen": 0, "last_seen": now - 14 * DAY}}
        kept, pruned_n = ledger_ns["_prune_observations"](obs, now, 14)
        assert pruned_n == 0
        assert "h1" in kept

    def test_empty_ledger_prunes_to_empty(self, ledger_ns):
        kept, pruned_n = ledger_ns["_prune_observations"]({}, 100 * DAY, 14)
        assert kept == {}
        assert pruned_n == 0


class TestLoadObservations:
    def test_missing_file_returns_empty(self, ledger_ns):
        assert ledger_ns["_load_observations"]() == {}

    def test_round_trip_through_save_and_load(self, ledger_ns):
        obs = {"h1": {"verdict": "detached", "first_seen": 1, "last_seen": 2}}
        ledger_ns["_save_observations"](obs)
        assert ledger_ns["_load_observations"]() == obs

    def test_corrupt_json_degrades_to_empty_not_a_crash(self, ledger_ns, capsys):
        path = Path(ledger_ns["OBS_STATE_PATH"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json at all", encoding="utf-8")
        assert ledger_ns["_load_observations"]() == {}
        assert "unreadable" in capsys.readouterr().err

    def test_wrong_shape_degrades_to_empty(self, ledger_ns):
        """{"observations": [...]}  (a list, not a dict) must not crash the
        loader -- defend the shape, not just the JSON syntax."""
        path = Path(ledger_ns["OBS_STATE_PATH"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"observations": ["not", "a", "dict"]}), encoding="utf-8")
        assert ledger_ns["_load_observations"]() == {}

    def test_orphan_verdict_is_rejected_on_load(self, ledger_ns):
        """Orphans must NEVER be recorded by the classification loop, but this
        proves it defensively too: even if an 'orphan' entry somehow ends up
        in the state file (a future bug, a hand edit, an old format), the
        loader's verdict whitelist strips it rather than letting it silently
        count toward the sample."""
        path = Path(ledger_ns["OBS_STATE_PATH"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"observations": {
            "good": {"verdict": "hardlinked", "first_seen": 1, "last_seen": 2},
            "bad": {"verdict": "orphan", "first_seen": 1, "last_seen": 2},
        }}), encoding="utf-8")
        loaded = ledger_ns["_load_observations"]()
        assert list(loaded.keys()) == ["good"]

    def test_one_poisoned_record_does_not_take_down_the_others(self, ledger_ns):
        path = Path(ledger_ns["OBS_STATE_PATH"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"observations": {
            "good": {"verdict": "hardlinked", "first_seen": 1, "last_seen": 2},
            "poisoned": {"verdict": "hardlinked", "first_seen": "not-a-number", "last_seen": 2},
        }}), encoding="utf-8")
        loaded = ledger_ns["_load_observations"]()
        assert list(loaded.keys()) == ["good"]


# =============================================================================
# Layer 2: full script, subprocess, pre-seeded ledger + empty current pool
# =============================================================================

class TestAccumulatedAssertion:
    """Drives the real MIN_SAMPLE-against-ACCUMULATED-ledger assertion. The
    current run's torrent list is empty (as it usually is on this box) --
    this proves the fix directly: the OLD code would have called this
    'imported=0 < min=5' and gone straight to inconclusive every time,
    regardless of what already happened this week."""

    def test_passes_once_accumulated_sample_is_clean(self, code, tmp_path):
        _seed_observations(tmp_path, [
            ("h1", "hardlinked", 6, 1), ("h2", "hardlinked", 5, 1),
            ("h3", "hardlinked", 4, 1), ("h4", "hardlinked", 3, 1),
            ("h5", "hardlinked", 2, 1),
        ])
        r = _run(code, tmp_path, [])
        assert r.returncode == 0, r.stderr
        assert "PASS" in r.stdout
        assert "observed=5/5" in r.stdout
        assert "hardlinked=5 detached=0" in r.stdout
        assert "pruned=0" in r.stdout

    def test_fails_once_accumulated_sample_exceeds_both_thresholds(self, code, tmp_path):
        """3 detached / 5 total: n=3 >= MAX_DETACHED(2) AND pct=60% >=
        MAX_DETACHED_PCT(5) -- both exceeded, must fail. None of these 5
        torrents ever coexisted; only the ACCUMULATOR reaches 5."""
        _seed_observations(tmp_path, [
            ("h1", "detached", 6, 1), ("h2", "detached", 5, 1),
            ("h3", "detached", 4, 1), ("h4", "hardlinked", 3, 1),
            ("h5", "hardlinked", 2, 1),
        ])
        r = _run(code, tmp_path, [])
        assert r.returncode == 1
        assert "STAGE=hardlink-regression" in r.stderr
        assert "detached=3/5" in r.stderr

    def test_stays_inconclusive_below_min_sample_even_with_bad_ratio(self, code, tmp_path):
        """2 detached / 3 total is a HORRIBLE ratio (67%) but 3 < MIN_SAMPLE
        (5) -- must still pass as inconclusive. This is the tiny-denominator
        guard that retired the two earlier designs; the rewrite must not
        reintroduce it at the accumulator level."""
        _seed_observations(tmp_path, [
            ("h1", "detached", 3, 1), ("h2", "detached", 2, 1),
            ("h3", "hardlinked", 1, 1),
        ])
        r = _run(code, tmp_path, [])
        assert r.returncode == 0, r.stderr
        assert "inconclusive" in r.stdout
        assert "observed=3/5" in r.stdout

    def test_min_sample_is_tunable_against_the_accumulator(self, code, tmp_path):
        """Same 3-entry ledger as above, but MIN_SAMPLE lowered to 3 via env
        -- the assertion must now actually run against the accumulator."""
        _seed_observations(tmp_path, [
            ("h1", "detached", 3, 1), ("h2", "detached", 2, 1),
            ("h3", "hardlinked", 1, 1),
        ])
        r = _run(code, tmp_path, [], min_sample="3")
        assert r.returncode == 1, r.stdout
        assert "STAGE=hardlink-regression" in r.stderr
        assert "detached=2/3" in r.stderr

    def test_boundary_n_below_floor_still_passes(self, code, tmp_path):
        """1 detached / 5 total: pct=20% >= 5%, but n=1 < MAX_DETACHED(2) --
        only ONE threshold exceeded, so it must still pass (both must be
        exceeded, unchanged from the pre-ledger design)."""
        _seed_observations(tmp_path, [
            ("h1", "detached", 6, 1), ("h2", "hardlinked", 5, 1),
            ("h3", "hardlinked", 4, 1), ("h4", "hardlinked", 3, 1),
            ("h5", "hardlinked", 2, 1),
        ])
        r = _run(code, tmp_path, [])
        assert r.returncode == 0, r.stderr
        assert "PASS" in r.stdout


class TestTTLPruning:
    def test_stale_entries_pruned_and_reported(self, code, tmp_path):
        _seed_observations(tmp_path, [
            ("fresh1", "hardlinked", 6, 1), ("fresh2", "hardlinked", 5, 1),
            ("stale1", "hardlinked", 40, 30), ("stale2", "detached", 40, 20),
        ])
        r = _run(code, tmp_path, [], ttl_days="14")
        assert r.returncode == 0, r.stderr
        assert "pruned=2" in r.stdout
        assert "observed=2/5" in r.stdout

    def test_pruning_persists_to_the_state_file(self, code, tmp_path):
        """A pruned entry must actually be dropped from disk, not just from
        this run's in-memory count -- otherwise it keeps getting 're-pruned'
        (harmless) but also keeps inflating len(observations) if the pruning
        boolean were ever miswired."""
        _seed_observations(tmp_path, [
            ("fresh", "hardlinked", 6, 1),
            ("stale", "hardlinked", 40, 30),
        ])
        r = _run(code, tmp_path, [], ttl_days="14")
        assert r.returncode == 0, r.stderr
        on_disk = json.loads(_obs_state_file(tmp_path).read_text(encoding="utf-8"))
        assert list(on_disk["observations"].keys()) == ["fresh"]

    def test_ttl_is_tunable(self, code, tmp_path):
        """The same 'stale' entry (30d old) survives under a longer TTL."""
        _seed_observations(tmp_path, [("h1", "hardlinked", 30, 30)])
        r = _run(code, tmp_path, [], ttl_days="60")
        assert r.returncode == 0, r.stderr
        assert "pruned=0" in r.stdout
        on_disk = json.loads(_obs_state_file(tmp_path).read_text(encoding="utf-8"))
        assert "h1" in on_disk["observations"]


class TestRobustness:
    def test_corrupt_ledger_degrades_to_empty_not_a_crash(self, code, tmp_path):
        p = _obs_state_file(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json at all", encoding="utf-8")
        r = _run(code, tmp_path, [])
        assert r.returncode == 0, "a corrupt ledger crashed the canary: " + r.stderr
        assert "unreadable" in r.stderr
        assert "observed=0/5" in r.stdout

    def test_missing_ledger_is_a_clean_empty_start(self, code, tmp_path):
        r = _run(code, tmp_path, [])
        assert r.returncode == 0, r.stderr
        assert "observed=0/5" in r.stdout

    def test_repeated_empty_runs_do_not_grow_the_ledger(self, code, tmp_path):
        """Two back-to-back runs with nothing new to observe must leave the
        accumulated count unchanged -- a bug that re-recorded stale data on
        every pass would silently inflate the sample over time."""
        _seed_observations(tmp_path, [
            ("h1", "hardlinked", 1, 1), ("h2", "hardlinked", 1, 1),
            ("h3", "hardlinked", 1, 1),
        ])
        _run(code, tmp_path, [])
        r2 = _run(code, tmp_path, [])
        assert r2.returncode == 0, r2.stderr
        assert "observed=3/5" in r2.stdout


class TestVacuityClockInteraction:
    """The vacuity clock (test_hardlink_vacuity_clock.py) must key off the
    ACCUMULATOR now, not the single-run pool. Confirms it still arms/clears
    correctly with the ledger in the loop."""

    def test_vacuity_clock_arms_when_accumulator_is_short(self, code, tmp_path):
        r = _run(code, tmp_path, [])  # empty ledger, empty pool
        assert r.returncode == 0
        vacuity = json.loads(
            (tmp_path / ".opt" / "maint" / "hardlink-integrity" / "vacuity.json")
            .read_text(encoding="utf-8"))
        assert vacuity["reason"] == "empty-pool"

    def test_vacuity_clock_clears_once_accumulator_actually_asserts(self, code, tmp_path):
        """Arm the clock first (as if the ledger had been short for days),
        then seed enough accumulated observations to cross MIN_SAMPLE. The
        real assertion running must clear the clock even though this run's
        own live pool is still empty."""
        vac = tmp_path / ".opt" / "maint" / "hardlink-integrity" / "vacuity.json"
        vac.parent.mkdir(parents=True, exist_ok=True)
        vac.write_text(json.dumps({"since": int(time.time()) - 3 * DAY,
                                   "reason": "empty-pool"}), encoding="utf-8")
        _seed_observations(tmp_path, [
            ("h1", "hardlinked", 6, 1), ("h2", "hardlinked", 5, 1),
            ("h3", "hardlinked", 4, 1), ("h4", "hardlinked", 3, 1),
            ("h5", "hardlinked", 2, 1),
        ])
        r = _run(code, tmp_path, [])
        assert r.returncode == 0, r.stderr
        assert "PASS" in r.stdout
        assert not vac.exists(), "a real assertion ran but did not clear the vacuity clock"
