"""Detector integrity — a detector must never invent an observation.

WHY THIS FILE EXISTS
--------------------
Three detectors and one deploy path were each lying in a different direction,
and every one of them was GREEN while doing it:

  B1  scripts/mcp/logs.py had "the routed path does not exist" and "the app is
      silent" rendering byte-identically downstream (_tail_file returns [] for a
      missing path; _file_is_dormant returns False on OSError). A one-character
      typo in a route therefore produced a confident verdict about a running
      app: bazarr2 reported "590h>26h dark" while its real log was 2 minutes
      old. FABRICATED DARKNESS is worse than no detector, because it reads like
      evidence.

  B2  qflix-collect.py emitted a known-correct, permanently-true, already-
      documented condition (nginx dark by design, measured 2026-08-24) in the
      `logs-dark=` fragment of EVERY hourly snapshot, forever. Permanent
      expected output is wallpaper, and wallpaper is what a real dark app would
      have hidden behind.

  B4  lib/cli.py mapped every non-zero canary rc to exit 2, so "the canary found
      drift" and "the canary could not run" (rc 127, bash missing) recorded the
      identical Result=exit-code. REA then paged a by-design drift red as a
      critical service outage.

WHAT THIS FILE PINS, AND THE MUTATION EVIDENCE
Each section names the mutation that must turn it RED. A fix whose reversion
leaves the suite green is not covered, it is merely present.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "mcp"))
sys.path.insert(0, str(REPO / "scripts" / "maint"))

import logs  # noqa: E402

_collect_src = (REPO / "scripts" / "maint" / "qflix-collect.py").read_text(encoding="utf-8")


def _load_collect():
    """qflix-collect.py has a hyphen in its name, so it cannot be imported.
    Load it by path, the same way tests/unit/test_qflix_collect_stale_state.py
    does."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "qflix_collect_di", REPO / "scripts" / "maint" / "qflix-collect.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collect = _load_collect()


def _now():
    return datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


def _ledger(app, *, last_lines_h_ago, quiet_cycles=5, max_gap=0):
    stamp = (_now() - timedelta(hours=last_lines_h_ago)).isoformat()
    return {"apps": {app: {
        "first_seen_at": stamp,
        "last_lines_at": stamp,
        "last_line_count": 1,
        "max_quiet_gap_h": max_gap,
        "quiet_cycles": quiet_cycles,
    }}}


# ===========================================================================
# AC-1 — B1: the route is fixed, and line 38 is untouched
# ===========================================================================

def test_bazarr2_route_points_at_the_real_file():
    """MUTATION: revert to `.apps/bazarr2/logs/bazarr2.log` → RED.

    The two bazarr instances genuinely differ (bazarr-1 is a container; see the
    bazarr-two-instances-differ note). bazarr2 keeps its db under
    ~/.apps/bazarr2/data/db/ and its log under ~/.apps/bazarr2/data/log/, and
    the file is named bazarr.log — not bazarr2.log, which never existed."""
    p = logs._FILE_LOGS["bazarr2"].replace("\\", "/")
    assert p.endswith(".apps/bazarr2/data/log/bazarr.log"), p
    assert "logs/bazarr2.log" not in p


def test_bazarr_one_route_is_byte_identical_to_its_verified_value():
    """bazarr-1's route was VERIFIED correct and is explicitly out of scope.
    A diff that touches it is an automatic fail — this is the assertion that
    makes 'don't touch line 38' mechanical rather than aspirational."""
    p = logs._FILE_LOGS["bazarr"].replace("\\", "/")
    assert p.endswith(".apps/bazarr/log/bazarr.log"), p


# ===========================================================================
# AC-2 / AC-3 / AC-4 — B1: a misroute can never read as dark
# ===========================================================================

def test_missing_route_returns_route_missing_error_not_zero_lines(tmp_path):
    """MUTATION: delete the os.path.exists guard in collect_for → RED.

    THE defect. Without this the entry is {'lines': []} with no error, which is
    indistinguishable from a healthy quiet app."""
    ghost = tmp_path / "definitely" / "not" / "there.log"
    with patch.dict(logs._FILE_LOGS, {"ghostapp": str(ghost)}, clear=False):
        entry = logs.collect_for("ghostapp", since="24h", tail=10)
    assert entry["lines"] == []
    assert entry["error"].startswith("route-missing:")
    assert str(ghost) in entry["error"]
    assert entry["source"] == str(ghost)


def test_route_missing_grades_as_source_error_and_reds_the_heartbeat(tmp_path):
    """End-to-end across the module boundary: logs.py's contract token has to
    survive into the collector's grading, because riding the EXISTING
    source_error rail is the entire reason no new monitor was added."""
    ghost = tmp_path / "gone.log"
    with patch.dict(logs._FILE_LOGS, {"ghostapp": str(ghost)}, clear=False):
        entry = logs.collect_for("ghostapp", since="24h", tail=10)

    payload = {"ghostapp": entry}
    _, report = collect.classify_log_coverage(
        _ledger("ghostapp", last_lines_h_ago=600), payload, _now())

    assert any(x.startswith("ghostapp:route-missing:") for x in report["source_error"])
    assert not any(x.startswith("ghostapp:") for x in report["dark"])
    assert not any(x.startswith("ghostapp:") for x in report["dark_expected"])
    # cov_broken is computed in main() from exactly these two keys.
    assert bool(report["roster_drop"] or report["source_error"]) is True


def test_expected_absent_slug_keeps_todays_dark_behaviour(tmp_path):
    """AC-3. A DECLARED absence opts back into the dark path: no error, zero
    lines. This is the only way out, and it costs a written reason."""
    ghost = tmp_path / "declared-gone.log"
    with patch.dict(logs._FILE_LOGS, {"declaredapp": str(ghost)}, clear=False), \
         patch.dict(logs.EXPECTED_ABSENT, {"declaredapp": "test reason"}, clear=False):
        entry = logs.collect_for("declaredapp", since="24h", tail=10)
    assert "error" not in entry
    assert entry["lines"] == []

    payload = {"declaredapp": entry}
    _, report = collect.classify_log_coverage(
        _ledger("declaredapp", last_lines_h_ago=600), payload, _now())
    assert report["source_error"] == []
    assert any(x.startswith("declaredapp:") for x in report["dark"])


def test_every_expected_absent_entry_carries_a_nonempty_reason():
    """AC-3's mandatory-reason law, applied to whatever the table grows into.
    An exemption with no named cause is a hiding place."""
    for slug, reason in logs.EXPECTED_ABSENT.items():
        assert isinstance(reason, str) and reason.strip(), slug
        assert len(reason.strip()) >= 20, f"{slug}: reason is too thin to be a reason"


def test_file_routes_is_a_copy_not_the_table():
    routes = logs.file_routes()
    routes["sonarr"] = "/tmp/hijacked"
    assert logs._FILE_LOGS["sonarr"] != "/tmp/hijacked"
    assert set(routes) >= set(logs._FILE_LOGS)


def test_verify_routes_grades_three_ways_with_injected_exists():
    """`exists` is injected precisely so this test needs no filesystem: the
    real routes are absolute paths under the seedbox $HOME and can never exist
    on the workstation, so a filesystem-touching test could only be vacuous."""
    good = logs._FILE_LOGS["sonarr"]
    with patch.dict(logs.EXPECTED_ABSENT, {"kometa": "declared for this test"}, clear=False):
        rows = logs.verify_routes(exists=lambda p: p == good)
    by_app = {r["app"]: r["status"] for r in rows}
    assert by_app["sonarr"] == "ok"
    assert by_app["kometa"] == "expected-absent"
    assert by_app["bazarr2"] == "missing"
    assert set(by_app) == set(logs.file_routes())


def test_verify_routes_cli_exit_codes_and_stderr_shape():
    """AC-4. Exit 0 when everything resolves, exit 1 listing
    `route-missing=<app>:<path>` otherwise, and --emit-json prints the list."""
    argv = ["logs.py", "--verify-routes"]

    all_ok = [{"app": "a", "path": "/p/a", "status": "ok"},
              {"app": "b", "path": "/p/b", "status": "expected-absent"}]
    with patch.object(sys, "argv", argv), \
         patch.object(logs, "verify_routes", return_value=all_ok):
        assert logs.main() == 0

    with_miss = all_ok + [{"app": "c", "path": "/p/c", "status": "missing"}]
    err = io.StringIO()
    with patch.object(sys, "argv", argv), \
         patch.object(logs, "verify_routes", return_value=with_miss), \
         redirect_stderr(err), redirect_stdout(io.StringIO()):
        assert logs.main() == 1
    assert "route-missing=c:/p/c" in err.getvalue()

    out = io.StringIO()
    with patch.object(sys, "argv", argv + ["--emit-json"]), \
         patch.object(logs, "verify_routes", return_value=all_ok), \
         redirect_stdout(out), redirect_stderr(io.StringIO()):
        assert logs.main() == 0
    assert json.loads(out.getvalue()) == all_ok


def test_verify_routes_is_reachable_as_a_real_subprocess():
    """The CLI mode has to exist as a MODE, not just as a branch: `--cron` and
    `--self-test` were in a required mutually-exclusive group, so adding a flag
    outside it is the kind of change argparse silently rejects at runtime."""
    cp = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "mcp" / "logs.py"),
         "--verify-routes", "--emit-json"],
        capture_output=True, text=True)
    assert cp.returncode in (0, 1), cp.stderr
    rows = json.loads(cp.stdout)
    assert {r["app"] for r in rows} == set(logs.file_routes())


def test_verify_routes_rejects_the_modes_it_is_exclusive_with():
    cp = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "mcp" / "logs.py"),
         "--verify-routes", "--self-test"],
        capture_output=True, text=True)
    assert cp.returncode == 2          # argparse usage error
    assert "mutually exclusive" in cp.stderr


def test_no_mode_at_all_is_still_an_error():
    """The mutex group lost required=True; something must still demand a mode,
    or `logs.py --app sonarr` would silently do nothing and exit 0."""
    cp = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "mcp" / "logs.py"), "--app", "sonarr"],
        capture_output=True, text=True)
    assert cp.returncode == 2
    assert "is required" in cp.stderr


# ===========================================================================
# AC-5..AC-8 — B2: expected-dark leaves the hourly summary, losslessly
# ===========================================================================

def _dark_payload(*apps):
    return {a: {"source": "x", "lines": []} for a in apps}


def test_nginx_is_declared_expected_dark_with_evidence():
    assert "nginx" in collect.EXPECTED_DARK
    reason = collect.EXPECTED_DARK["nginx"]
    assert reason.strip()
    # The reason must cite the MEASUREMENT, not merely assert the conclusion.
    assert "2026-08-24" in reason and "error.log.1" in reason


def test_expected_dark_leaves_the_kuma_fragment_but_undeclared_dark_does_not():
    """AC-5, both directions in one test so neither can pass alone.
    MUTATION: delete the EXPECTED_DARK branch in classify_log_coverage → RED."""
    ledger = {"apps": {}}
    for app in ("nginx", "someapp"):
        ledger["apps"].update(_ledger(app, last_lines_h_ago=600)["apps"])

    _, report = collect.classify_log_coverage(
        ledger, _dark_payload("nginx", "someapp"), _now())

    assert any(x.startswith("nginx:") for x in report["dark_expected"])
    assert not any(x.startswith("nginx:") for x in report["dark"])
    assert any(x.startswith("someapp:") for x in report["dark"])

    frag = collect.format_log_coverage(report)
    assert "nginx" not in frag
    assert "logs-dark=someapp:" in frag


def test_empty_dark_means_no_fragment_at_all():
    """AC-5's quiet half: with nginx the ONLY dark source, the hourly message
    goes back to saying nothing about coverage — which is the point."""
    _, report = collect.classify_log_coverage(
        _ledger("nginx", last_lines_h_ago=600), _dark_payload("nginx"), _now())
    assert report["dark"] == []
    assert report["dark_expected"]
    assert collect.format_log_coverage(report) == ""


def test_dark_verdicts_are_lossless_and_disjoint():
    """AC-6. Every source that trips the threshold lands in exactly one bucket —
    never both, never neither. Split-a-list refactors lose members silently."""
    apps = ["nginx", "alpha", "beta", "gamma"]
    ledger = {"apps": {}}
    for a in apps:
        ledger["apps"].update(_ledger(a, last_lines_h_ago=600)["apps"])

    _, report = collect.classify_log_coverage(ledger, _dark_payload(*apps), _now())
    named = [x.split(":")[0] for x in report["dark"] + report["dark_expected"]]
    assert sorted(named) == sorted(apps)
    assert len(named) == len(set(named))
    # same fragment shape in both buckets
    for frag in report["dark"] + report["dark_expected"]:
        assert "h>" in frag and frag.endswith("h")


def test_dark_expected_never_reds_the_heartbeat():
    """AC-6's second half. cov_broken is roster_drop|source_error and must stay
    that way: promoting dark to a red pins the PUBLIC status page."""
    _, report = collect.classify_log_coverage(
        _ledger("nginx", last_lines_h_ago=600), _dark_payload("nginx"), _now())
    assert bool(report["roster_drop"] or report["source_error"]) is False
    assert "dark_expected" not in _collect_src.split("cov_broken = ")[1].split("\n")[0]


def test_expected_dark_spoke_is_populated_and_reaches_every_required_surface():
    """AC-7. The DECISION is: the spoke fragment DOES enter the Kuma msg.
    Justified in the source next to format_log_coverage; asserted here, because
    a choice nothing tests is not a choice."""
    _, report = collect.classify_log_coverage(
        {"apps": {}},
        {"nginx": {"source": "x", "lines": [{"message": "upstream timed out"}]}},
        _now())
    assert report["expected_dark_spoke"] == ["nginx"]

    frag = collect.format_log_coverage(report)
    assert "logs-expected-dark-spoke=nginx" in frag        # -> Kuma msg
    # main() writes `warn("log coverage: " + cov_msg)` whenever cov_msg is
    # non-empty, and serialises the whole report into last-collect.json.
    assert 'warn("log coverage: " + cov_msg)' in _collect_src
    assert '"log_coverage": coverage' in _collect_src


def test_spoke_fragment_never_displaces_a_paging_fragment():
    """The cost of the include decision, bounded: the spoke rides LAST, so the
    130-char cap can only ever truncate it, never truncate a page."""
    report = {
        "roster_drop": ["dropped-app"],
        "source_error": ["broken-app:route-missing:/x"],
        "dark": ["d1:99h>26h"],
        "dark_expected": ["nginx:600h>26h"],
        "expected_dark_spoke": ["nginx"],
    }
    frag = collect.format_log_coverage(report)
    assert frag.index("logs-roster-drop=") < frag.index("logs-source-error=")
    assert frag.index("logs-source-error=") < frag.index("logs-dark=")
    assert frag.index("logs-dark=") < frag.index("logs-expected-dark-spoke=")
    assert len(frag) <= 130


def test_classify_is_pure_and_deterministic():
    """AC-8. Two identical calls, identical output, and no filesystem in the
    module-level text of the function itself."""
    ledger = _ledger("nginx", last_lines_h_ago=600)
    payload = _dark_payload("nginx", "someapp")
    a = collect.classify_log_coverage(json.loads(json.dumps(ledger)), payload, _now())
    b = collect.classify_log_coverage(json.loads(json.dumps(ledger)), payload, _now())
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    # Structural, via AST rather than substring: the function is wall-to-wall
    # prose comments that mention `subprocess` and `Path` by name, so a text
    # scan would be red for the wrong reason.
    import ast
    tree = ast.parse(_collect_src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "classify_log_coverage")
    banned = {"open", "print", "input"}
    banned_mods = {"os", "io", "json", "requests", "subprocess", "urllib",
                   "Path", "time", "datetime"}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                assert f.id not in banned, f"I/O in classify_log_coverage: {f.id}"
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                assert f.value.id not in banned_mods, \
                    f"I/O in classify_log_coverage: {f.value.id}.{f.attr}"


def test_no_init_py_was_added_to_either_merged_namespace_package():
    """C5/AC-8. Either __init__.py shadows the other half of the merged
    namespace and kills collect.py on the box."""
    assert not (REPO / "scripts" / "maint" / "lib" / "__init__.py").exists()
    assert not (REPO / "scripts" / "mcp" / "lib" / "__init__.py").exists()


# ===========================================================================
# AC-13..AC-15 — B4: finding vs harness-error
# ===========================================================================

def _push_args(name="deploy-drift"):
    return SimpleNamespace(name=name)


def _run_canary_push(rc, stdout="", stderr="", tmp_path=None):
    """Drive _cmd_canary_push with a canned subprocess result, with the two
    suppression gates open and no token configured (so it returns before the
    HTTP call unless the test patches _tokens_path)."""
    from lib import cli as cli_mod
    manifest = MagicMock()
    manifest.canary.return_value = SimpleNamespace(script="scripts/canaries/x.sh")
    result = SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)
    err = io.StringIO()
    with patch.object(cli_mod.subprocess, "run", return_value=result), \
         patch("lib.suppression.push_suppressed", return_value=None), \
         patch("lib.suppression.in_maintenance_window", return_value=False), \
         patch.object(cli_mod, "_tokens_path", side_effect=FileNotFoundError), \
         redirect_stderr(err):
        code = cli_mod._cmd_canary_push(_push_args(), manifest)
    return code, err.getvalue()


def test_finding_rc_maps_to_20_and_anything_else_maps_to_2():
    """AC-13, and the exact mutation that matters: collapsing both branches back
    to `exit_code = 2` must turn this RED."""
    code, _ = _run_canary_push(20, stderr="STAGE=deploy-drift msg=1-of-242")
    assert code == 20
    for rc in (1, 2, 3, 127, 255):
        code, _ = _run_canary_push(rc, stderr="boom")
        assert code == 2, rc
    code, _ = _run_canary_push(0, stdout="PASS: deploy-drift")
    assert code == 0


def test_exactly_one_verdict_line_per_non_pass_run():
    """AC-13. Stable prefix, all five fields, one line — REA parses it."""
    _, err = _run_canary_push(20, stderr="STAGE=deploy-drift msg=1-of-242-differ")
    lines = [l for l in err.splitlines() if l.startswith("QFLIX_CANARY_VERDICT")]
    assert len(lines) == 1
    line = lines[0]
    assert " name=deploy-drift " in line
    assert " unit=manitoba-maint-canary-deploy-drift.service " in line
    assert " verdict=finding " in line
    assert " rc=20 " in line
    assert " msg=STAGE=deploy-drift" in line
    # the human line is RETAINED alongside it
    assert any(l.startswith("canary 'deploy-drift' FAILED (rc=20)") for l in err.splitlines())


def test_harness_error_says_so_and_a_pass_emits_no_verdict_line():
    _, err = _run_canary_push(127, stderr="bash not found")
    assert " verdict=harness-error " in err
    assert " rc=127 " in err
    _, err = _run_canary_push(0, stdout="PASS")
    assert "QFLIX_CANARY_VERDICT" not in err


def test_multiline_canary_output_still_yields_exactly_one_verdict_line():
    _, err = _run_canary_push(20, stderr="STAGE=deploy-drift\nfiles= a b c\nmore")
    lines = [l for l in err.splitlines() if l.startswith("QFLIX_CANARY_VERDICT")]
    assert len(lines) == 1
    assert "files= a b c" in lines[0].replace("  ", " ")


def _push_and_capture_kuma(rc, stdout="", stderr="", tmp_path=None):
    from lib import cli as cli_mod
    manifest = MagicMock()
    manifest.canary.return_value = SimpleNamespace(script="scripts/canaries/x.sh")
    result = SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)
    tokens = tmp_path / "kuma-push-tokens.json"
    tokens.write_text(json.dumps({"canary-deploy-drift": "TOKEN123"}), encoding="utf-8")
    getter = MagicMock()
    getter.return_value = SimpleNamespace(status_code=200)
    with patch.object(cli_mod.subprocess, "run", return_value=result), \
         patch("lib.suppression.push_suppressed", return_value=None), \
         patch("lib.suppression.in_maintenance_window", return_value=False), \
         patch.object(cli_mod, "_tokens_path", return_value=tokens), \
         patch.object(cli_mod.requests, "get", getter), \
         redirect_stderr(io.StringIO()):
        code = cli_mod._cmd_canary_push(_push_args(), manifest)
    return code, getter


def test_kuma_push_is_byte_identical_for_both_non_pass_verdicts(tmp_path):
    """AC-14. A red stays red. The two verdicts differ ONLY in the exit code and
    the journal line — never in what Kuma is told, or the page changes shape."""
    code_f, get_f = _push_and_capture_kuma(20, stderr="same detail", tmp_path=tmp_path)
    code_h, get_h = _push_and_capture_kuma(127, stderr="same detail", tmp_path=tmp_path)
    assert (code_f, code_h) == (20, 2)
    assert get_f.call_args == get_h.call_args
    url, kwargs = get_f.call_args[0][0], get_f.call_args[1]
    assert url.endswith("/api/push/TOKEN123")
    assert kwargs["params"] == {"status": "down", "msg": "same detail"}
    assert kwargs["timeout"] == 5


def test_msg_derivation_is_unchanged(tmp_path):
    """stderr, else stdout, else 'FAIL', 200-char slice — all four arms."""
    _, g = _push_and_capture_kuma(20, stderr="E", stdout="O", tmp_path=tmp_path)
    assert g.call_args[1]["params"]["msg"] == "E"
    _, g = _push_and_capture_kuma(20, stderr="", stdout="O", tmp_path=tmp_path)
    assert g.call_args[1]["params"]["msg"] == "O"
    _, g = _push_and_capture_kuma(20, stderr="", stdout="", tmp_path=tmp_path)
    assert g.call_args[1]["params"]["msg"] == "FAIL"
    _, g = _push_and_capture_kuma(20, stderr="x" * 500, tmp_path=tmp_path)
    assert g.call_args[1]["params"]["msg"] == "x" * 200
    _, g = _push_and_capture_kuma(0, stdout="PASS: all good", tmp_path=tmp_path)
    assert g.call_args[1]["params"]["params" if False else "status"] == "up"


def test_suppression_and_maint_window_paths_are_untouched(tmp_path):
    """AC-14's tail: both early returns still push `up` and return 0, and they
    must not have grown a verdict line (there is no run to have a verdict about)."""
    from lib import cli as cli_mod
    manifest = MagicMock()
    manifest.canary.return_value = SimpleNamespace(script="scripts/canaries/x.sh")
    tokens = tmp_path / "t.json"
    tokens.write_text(json.dumps({"canary-deploy-drift": "TOK"}), encoding="utf-8")
    for patch_name, other in (("push_suppressed", "in_maintenance_window"),
                              ("in_maintenance_window", "push_suppressed")):
        getter = MagicMock(return_value=SimpleNamespace(status_code=200))
        rv = "muted because X" if patch_name == "push_suppressed" else True
        err = io.StringIO()
        with patch(f"lib.suppression.{patch_name}", return_value=rv), \
             patch(f"lib.suppression.{other}",
                   return_value=(False if other == "in_maintenance_window" else None)), \
             patch.object(cli_mod, "_tokens_path", return_value=tokens), \
             patch.object(cli_mod.requests, "get", getter), \
             patch.object(cli_mod.subprocess, "run",
                          side_effect=AssertionError("script must not run")), \
             redirect_stderr(err):
            assert cli_mod._cmd_canary_push(_push_args(), manifest) == 0
        assert getter.call_args[1]["params"]["status"] == "up"
        assert "QFLIX_CANARY_VERDICT" not in err.getvalue()


# ===========================================================================
# AC-15 — no consumer is blinded by the new exit code
# ===========================================================================

DRIFT_UNIT = REPO / "scripts" / "maint" / "systemd" / "manitoba-maint-canary-deploy-drift.service"


def test_the_unit_did_not_gain_success_exit_status():
    """DESIGN (A) of spec 4.4 was chosen: the unit is LEFT ALONE.

    SuccessExitStatus=20 would make systemd record Result=success for a finding
    and silently blind three consumers that grade on Result — cli._probe_canary,
    canaries/timer-liveness.sh and health._probe_systemd_oneshot. Turning a
    by-design red into a green is the exact defect class this cluster exists to
    kill, so the disambiguation is carried by ExecMainStatus=20 and the
    QFLIX_CANARY_VERDICT line instead, and nothing downstream has to change.

    This assertion is the guard on that decision: adding SuccessExitStatus here
    without doing the consumer sweep turns the suite RED."""
    text = DRIFT_UNIT.read_text(encoding="utf-8")
    assert "SuccessExitStatus" not in text


def test_probe_canary_still_grades_a_finding_as_not_ok():
    """cli._probe_canary reads Result/ActiveState. With the unit unchanged a
    finding still yields Result=exit-code + ActiveState=failed → not ok."""
    from lib import cli as cli_mod
    canary = SimpleNamespace(name="deploy-drift", schedule="hourly",
                             kuma_monitor="QFlix Canary - deploy drift")
    show = ("LoadState=loaded\nResult=exit-code\nActiveState=failed\n"
            "ExecMainStatus=20\nExecMainExitTimestamp=@1789000000\n")
    with patch.object(cli_mod.subprocess, "run",
                      return_value=SimpleNamespace(returncode=0, stdout=show, stderr="")):
        entry = cli_mod._probe_canary(canary)
    assert entry["ok"] is False
    assert "exit-code" in entry["reason"]


def test_health_probe_systemd_oneshot_still_grades_a_finding_as_not_ok():
    from lib import health as health_mod
    app = SimpleNamespace(
        health=SimpleNamespace(kind="systemd_oneshot",
                               raw={"unit": "manitoba-maint-canary-deploy-drift.service"}),
        raw={}, defaults={})
    show = "Result=exit-code\nActiveState=failed\n"
    with patch.object(health_mod.subprocess, "run",
                      return_value=SimpleNamespace(returncode=0, stdout=show, stderr="")):
        res = health_mod._probe_systemd_oneshot(app, 5.0)
    assert res.ok is False


def test_timer_liveness_awk_still_grades_a_finding_as_failed():
    """The awk block keys on `res != "success"`. Unchanged unit ⇒ exit-code ⇒
    FAILED. Pinned textually because the script only runs over SSH."""
    text = (REPO / "scripts" / "canaries" / "timer-liveness.sh").read_text(encoding="utf-8")
    assert 'res != "success"' in text
