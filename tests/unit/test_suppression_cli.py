"""lib/suppression.py writer CLI (QFLX-25, spec 5.9 steps 4 / 9 and rollback 0).

The swap suppresses the app's Kuma monitor AND its behavioural canaries in
push-suppress.json, then lifts them together. The registry is shared state, so
the writer holds an flock over the whole read-modify-write, writes via
mkstemp + replace, and REFUSES (exit 2) to overwrite a file it cannot parse:
merging onto {} would silently drop someone else's suppression.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUP = REPO / "scripts" / "maint" / "lib" / "suppression.py"


def _cli(state: Path, *args):
    env = dict(os.environ, MANITOBA_STATE_DIR=str(state))
    return subprocess.run([sys.executable, str(SUP), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def _reg(state: Path) -> dict:
    return json.loads((state / "push-suppress.json").read_text(encoding="utf-8"))


def test_add_creates_registry_entries_with_reason_and_since(tmp_path):
    r = _cli(tmp_path, "add", "unpackerr", "canary-thread-ceiling", "--reason", "QFLX-25 swap")
    assert r.returncode == 0, r.stderr
    reg = _reg(tmp_path)
    assert set(reg) == {"unpackerr", "canary-thread-ceiling"}
    assert reg["unpackerr"]["reason"] == "QFLX-25 swap"
    assert reg["unpackerr"]["since"].endswith("Z")


def test_add_keeps_other_entries_and_remove_lifts_only_named(tmp_path):
    (tmp_path / "push-suppress.json").write_text(
        json.dumps({"flaresolverr": {"reason": "upstream", "since": "x"}}), encoding="utf-8")
    assert _cli(tmp_path, "add", "unpackerr", "--reason", "swap").returncode == 0
    assert set(_reg(tmp_path)) == {"flaresolverr", "unpackerr"}
    assert _cli(tmp_path, "remove", "unpackerr").returncode == 0
    assert set(_reg(tmp_path)) == {"flaresolverr"}


def test_add_is_idempotent_and_keeps_first_since(tmp_path):
    _cli(tmp_path, "add", "unpackerr", "--reason", "a")
    first = _reg(tmp_path)["unpackerr"]["since"]
    assert _cli(tmp_path, "add", "unpackerr", "--reason", "b").returncode == 0
    assert _reg(tmp_path)["unpackerr"]["since"] == first


def test_remove_of_absent_entry_is_ok(tmp_path):
    assert _cli(tmp_path, "remove", "unpackerr").returncode == 0


def test_corrupt_registry_is_refused_not_overwritten(tmp_path):
    p = tmp_path / "push-suppress.json"
    p.write_text("{not json", encoding="utf-8")
    r = _cli(tmp_path, "add", "unpackerr", "--reason", "x")
    assert r.returncode == 2
    assert p.read_text(encoding="utf-8") == "{not json"


def test_has_reports_membership(tmp_path):
    _cli(tmp_path, "add", "unpackerr", "--reason", "x")
    assert _cli(tmp_path, "has", "unpackerr").returncode == 0
    assert _cli(tmp_path, "has", "canary-thread-ceiling").returncode == 1


def test_bad_usage_exits_nonzero(tmp_path):
    assert _cli(tmp_path, "frobnicate").returncode != 0
    assert _cli(tmp_path, "add").returncode != 0
