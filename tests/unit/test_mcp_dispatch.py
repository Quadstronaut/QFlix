# tests/unit/test_mcp_dispatch.py
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

def _load():
    path = REPO / "scripts" / "mcp" / "dispatch.py"
    spec = importlib.util.spec_from_file_location("dispatch", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dispatch"] = mod
    spec.loader.exec_module(mod)
    return mod

def test_envelope_has_the_documented_keys():
    d = _load()
    env = d.envelope(verb="status", target=None, ok=True,
                     verdict="all good", lines=["a", "b"], elapsed_s=1.5)
    assert set(env) == {"ok", "verb", "target", "verdict", "lines", "elapsed_s"}
    assert env["ok"] is True
    assert env["verdict"] == "all good"

def test_envelope_caps_lines_so_a_phone_never_pulls_an_unbounded_log():
    d = _load()
    env = d.envelope(verb="logs", target="sonarr", ok=True, verdict="ok",
                     lines=[str(i) for i in range(500)], elapsed_s=0.1)
    assert len(env["lines"]) == d.MAX_LINES

def test_no_verb_returns_member_viewing_activity():
    """The privacy constraint is structural: the capability must be ABSENT
    from the wire protocol, not merely unused by the UI. plex.py supports a
    sessions snapshot; no verb may reach it."""
    d = _load()
    # Non-vacuity guard: an empty VERBS would make the loop below assert
    # nothing and pass forever. `help` is registered from Task 1 precisely so
    # this test has something to check from the first commit.
    assert len(d.VERBS) >= 1, "VERBS is empty - the loop below would prove nothing"
    banned = ("session", "watch", "history", "viewer", "member", "who")
    for name in d.VERBS:
        low = name.lower()
        assert not any(b in low for b in banned), (
            "verb %r looks like it exposes member activity; see the privacy "
            "constraint in the 2026-08-03 spec" % name)

def test_parse_command_splits_verb_from_args():
    d = _load()
    assert d.parse_command("app.restart sonarr") == ("app.restart", ["sonarr"])

def test_empty_command_is_help_not_a_crash():
    d = _load()
    assert d.parse_command(None) == ("help", [])
    assert d.parse_command("   ") == ("help", [])

def test_unknown_verb_lists_every_known_verb_so_the_app_cannot_silently_noop():
    d = _load()
    env = d.dispatch(["definitely.not.a.verb"])
    assert env["ok"] is False
    assert "unknown verb" in env["verdict"].lower()
    for name in d.VERBS:
        assert any(name in line for line in env["lines"])

def test_wrong_arity_says_what_was_expected():
    """Registers a throwaway probe verb rather than naming a real one.

    At Task 2 the only registered verb is `help` (arity 0); every verb that
    takes an argument arrives in Task 4 or later. Naming one of those here
    would make this test pass for the wrong reason now (it would hit the
    unknown-verb branch) and couple it to another task's registration order
    forever."""
    d = _load()
    d.VERBS["probe.needs_one"] = d.VerbSpec(
        handler=lambda argv: d.envelope(verb="probe.needs_one", target=argv[0],
                                        ok=True, verdict="ok", lines=[],
                                        elapsed_s=0.0),
        arity=1, help="arity probe")
    env = d.dispatch(["probe.needs_one"])      # missing the one required arg
    assert env["ok"] is False
    assert "expects" in env["verdict"].lower()
    assert "1" in env["verdict"]               # says how many it wanted

def test_help_verb_is_registered_and_lists_verbs():
    d = _load()
    env = d.dispatch(["help"])
    assert env["ok"] is True
    assert len(env["lines"]) >= 1

def test_app_list_returns_only_lifecycle_classes():
    d = _load()
    env = d.dispatch(["app.list"])
    assert env["ok"] is True
    # 18 ucc + 6 systemd = 24. Counted off App.class_ via the manifest loader,
    # NOT off the `# --- group ---` comment headers in apps.yaml, which have
    # drifted from the real class fields (postgres sits under the systemd
    # comment but is class: ucc). cron (10) and library (1) have no lifecycle.
    assert len(env["lines"]) == 24
    for line in env["lines"]:
        assert line.split()[1] in ("ucc", "systemd")

def test_app_list_names_the_class_so_the_phone_never_guesses_how_to_start_a_thing():
    d = _load()
    env = d.dispatch(["app.list"])
    joined = "\n".join(env["lines"])
    assert "sonarr ucc" in joined
    assert "listmonk systemd" in joined

def test_status_never_returns_the_per_member_top5_section(monkeypatch):
    """The name-substring guard cannot catch this: the verb is called `status`
    and the member data hides inside its payload. Assert on what goes OUT."""
    d = _load()
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = '{"quota": {}, "kuma": {}, "streams": {}, "downloads": {}}'
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    env = d.dispatch(["status"])
    assert "--sections" in captured["cmd"]
    sections = captured["cmd"][captured["cmd"].index("--sections") + 1]
    assert "top5" not in sections, "status must never request the per-member section"
    assert "top5" not in json.dumps(env)

def test_ucc_app_routes_to_the_approved_ultra_command(monkeypatch):
    d = _load()
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    env = d.dispatch(["app.restart", "sonarr"])
    assert env["target"] == "sonarr"
    assert "ucc" in env["verdict"] or "app-sonarr" in "\n".join(env["lines"] + [env["verdict"]])

def test_systemd_app_routes_to_systemctl(monkeypatch):
    d = _load()
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    env = d.dispatch(["app.restart", "listmonk"])
    assert env["target"] == "listmonk"
    assert "systemd" in env["verdict"] or "systemctl" in "\n".join(env["lines"] + [env["verdict"]])

def test_a_cron_app_is_refused_rather_than_silently_doing_nothing():
    d = _load()
    env = d.dispatch(["app.restart", "kometa"])
    assert env["ok"] is False
    assert "no lifecycle" in env["verdict"].lower()

def test_unknown_slug_is_refused():
    d = _load()
    env = d.dispatch(["app.restart", "not-an-app"])
    assert env["ok"] is False
    assert "unknown app" in env["verdict"].lower()

def test_start_during_the_ucc_gate_explains_itself(monkeypatch):
    d = _load()
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    monkeypatch.setattr(d, "_ucc_gate_up", lambda: True)
    env = d.dispatch(["app.start", "sonarr"])
    assert env["ok"] is False
    assert "gate" in env["verdict"].lower()

def test_restart_is_not_blocked_by_the_gate(monkeypatch):
    """Only `start` is gated by Ultra.cc. Blocking restart too would remove the
    operator's main remote remediation for no reason."""
    d = _load()
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    monkeypatch.setattr(d, "_ucc_gate_up", lambda: True)
    env = d.dispatch(["app.restart", "sonarr"])
    assert "gate" not in env["verdict"].lower()

def test_stop_routes_like_the_other_lifecycle_verbs(monkeypatch):
    d = _load()
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    env = d.dispatch(["app.stop", "sonarr"])
    assert env["target"] == "sonarr"
    assert "stop" in env["verdict"]


def test_the_gate_blocks_only_ucc_start_not_systemd_start(monkeypatch):
    """The Ultra.cc gate is an Ultra.cc constraint. systemd units are ours and
    keep working through it — gating them would invent an outage."""
    d = _load()
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    monkeypatch.setattr(d, "_ucc_gate_up", lambda: True)
    env = d.dispatch(["app.start", "listmonk"])      # systemd class
    assert "gate" not in env["verdict"].lower()


def test_every_lifecycle_verb_carries_its_own_action(monkeypatch):
    """Guards the lambda-in-loop registration: a late-binding bug would make
    all three verbs perform whichever action was registered last."""
    d = _load()
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    for action in ("start", "stop", "restart"):
        env = d.dispatch(["app." + action, "sonarr"])
        assert action in env["verdict"], (action, env["verdict"])


ARRS = ("sonarr", "sonarr2", "radarr", "radarr2")

def test_search_wanted_refuses_a_non_arr():
    d = _load()
    env = d.dispatch(["arr.search_wanted", "plex"])
    assert env["ok"] is False
    assert "not an *arr" in env["verdict"] or "not an arr" in env["verdict"].lower()

def test_search_wanted_accepts_each_arr(monkeypatch):
    d = _load()
    monkeypatch.setattr(d, "_run_mcp",
                        lambda *a, **k: (True, {"per_arr": {}}, ""))
    for slug in ARRS:
        env = d.dispatch(["arr.search_wanted", slug])
        assert env["ok"] is True, slug
        assert env["target"] == slug

def test_logs_honours_an_explicit_tail_within_the_ceiling(monkeypatch):
    d = _load()
    # Mix in one dict-shaped record — logs.py's real --emit-json shape is
    # {ts, level, message, source_file}, not a bare string — so
    # _format_log_line's dict branch (the one real logs.py output actually
    # takes) gets exercised here, not just the list[str] shape every other
    # mock in this file substitutes.
    mock_lines = [str(i) for i in range(299)] + [
        {"ts": "2026-08-03T00:00:00", "level": "INFO", "message": "RSS Sync Completed.",
         "source_file": "/home/x/.apps/sonarr/logs/sonarr.txt"}
    ]
    monkeypatch.setattr(d, "_run_mcp",
                        lambda *a, **k: (True, {"lines": mock_lines}, ""))
    env = d.dispatch(["logs", "sonarr", "--tail", "50"])
    assert len(env["lines"]) == 50
    assert env["lines"][-1] == "2026-08-03T00:00:00 INFO RSS Sync Completed."

def test_logs_tail_cannot_exceed_the_ceiling(monkeypatch):
    d = _load()
    monkeypatch.setattr(d, "_run_mcp",
                        lambda *a, **k: (True, {"lines": [str(i) for i in range(1000)]}, ""))
    env = d.dispatch(["logs", "sonarr", "--tail", "99999"])
    assert len(env["lines"]) == d.MAX_LINES_CEILING

def test_unstick_requires_both_slug_and_queue_id():
    d = _load()
    env = d.dispatch(["unstick", "sonarr"])
    assert env["ok"] is False
    assert "expects" in env["verdict"].lower()


def test_logs_refuses_apps_whose_logs_carry_member_identity():
    """listmonk holds subscriber email addresses, tautulli per-member watch
    records, seerr per-member requests, plex usernames. logs.py applies no
    redaction, so the refusal must happen HERE, before it is ever invoked."""
    d = _load()
    for slug in ("listmonk", "tautulli", "seerr", "plex"):
        env = d.dispatch(["logs", slug])
        assert env["ok"] is False, slug
        assert "not exposed" in env["verdict"], slug


def test_logs_refusal_happens_before_the_subprocess_runs(monkeypatch):
    """A post-hoc filter would still have read the data."""
    d = _load()
    called = []
    monkeypatch.setattr(d, "_run_mcp",
                        lambda *a, **k: called.append(a) or (True, {"lines": []}, ""))
    d.dispatch(["logs", "listmonk"])
    assert called == [], "logs.py must not be invoked for a refused app"


def test_an_allowlisted_app_still_reaches_logs_py(monkeypatch):
    """Guards against over-correcting the allowlist into uselessness."""
    d = _load()
    captured = {}
    def fake(script, args, timeout_s=45.0):
        captured["script"], captured["args"] = script, args
        return (True, {"lines": ["2026-08-03 INFO x"]}, "")
    monkeypatch.setattr(d, "_run_mcp", fake)
    env = d.dispatch(["logs", "sonarr"])
    assert env["ok"] is True
    assert captured["script"] == "logs.py"
    assert "sonarr" in captured["args"]


# --- starr: all four *arrs in one round trip ---------------------------

def test_starr_returns_all_four_arrs_in_one_call(monkeypatch):
    d = _load()
    monkeypatch.setattr(d, "_peek_one",
                        lambda slug: {"slug": slug, "kind": "series",
                                      "titles": [], "ok": True, "error": ""})
    monkeypatch.setattr(d, "_usage_one",
                        lambda slug: {"slug": slug, "bytes": 0, "human": "0.0 B",
                                      "title_count": 0, "ok": True, "error": ""})
    env = d.dispatch(["starr"])
    assert env["ok"] is True
    assert sorted(env["arrs"]) == ["radarr", "radarr2", "sonarr", "sonarr2"]


def test_one_dead_arr_does_not_kill_the_page(monkeypatch):
    d = _load()
    def peek(slug):
        if slug == "radarr2":
            return {"slug": slug, "kind": "movie", "titles": [],
                    "ok": False, "error": "refused"}
        return {"slug": slug, "kind": "series", "titles": [], "ok": True, "error": ""}
    monkeypatch.setattr(d, "_peek_one", peek)
    monkeypatch.setattr(d, "_usage_one",
                        lambda slug: {"slug": slug, "bytes": 0, "human": "0.0 B",
                                      "title_count": 0, "ok": True, "error": ""})
    env = d.dispatch(["starr"])
    assert env["ok"] is True                       # page still renders
    assert env["arrs"]["radarr2"]["peek"]["ok"] is False
    assert "radarr2" in env["verdict"]


def test_starr_calls_each_script_exactly_once_per_slug(monkeypatch):
    """The whole reason `starr` is one verb: eight per-instance SSH round
    trips would defeat the point on a flaky mobile link. Assert the call
    count directly at the _run_mcp seam (not the higher _peek_one/_usage_one
    seam the other two tests use) so a future refactor that fans a slug out
    to two peek calls, or re-fetches on retry, goes red here."""
    d = _load()
    calls = []
    def fake_run_mcp(script, args, timeout_s=45.0):
        slug = args[args.index("--slug") + 1]
        calls.append((script, slug))
        if script == "arr_library_peek.py":
            return (True, {"slug": slug, "kind": "series", "titles": [],
                           "ok": True, "error": ""}, "")
        return (True, {"slug": slug, "bytes": 0, "human": "0.0 B",
                       "title_count": 0, "ok": True, "error": ""}, "")
    monkeypatch.setattr(d, "_run_mcp", fake_run_mcp)
    d.dispatch(["starr"])
    assert len(calls) == len(ARRS) * 2, calls          # exactly 8, not 4 or 16
    assert len(set(calls)) == len(calls), "duplicate (script, slug) call: %r" % calls


# --- quota: disk headroom for the Dashboard tile ------------------------

def test_quota_reports_used_total_and_percent(monkeypatch):
    d = _load()
    monkeypatch.setattr(d, "_quota_raw",
                        lambda: {"used_gb": 2190.0, "total_gb": 2794.0})
    env = d.dispatch(["quota"])
    assert env["ok"] is True
    assert env["used_gb"] == 2190.0
    assert env["percent"] == 78.4


def test_quota_zero_total_does_not_divide_by_zero(monkeypatch):
    d = _load()
    monkeypatch.setattr(d, "_quota_raw",
                        lambda: {"used_gb": 0.0, "total_gb": 0.0})
    env = d.dispatch(["quota"])
    assert env["ok"] is False
    assert env["percent"] == 0.0


def test_quota_degrades_when_the_binary_is_absent(monkeypatch):
    """`quota` may not exist on a dev machine (it never does on this
    Windows workstation) or may be missing on the box itself. This test
    must not, and does not, depend on the real binary — subprocess.run
    itself is replaced with something that raises FileNotFoundError, the
    exact exception a missing executable produces."""
    d = _load()
    import subprocess
    def fake_run(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'quota'")
    monkeypatch.setattr(subprocess, "run", fake_run)
    env = d.dispatch(["quota"])
    assert env["ok"] is False
    assert env["percent"] == 0.0
    assert "used_gb" in env and "total_gb" in env      # not just dispatch()'s generic catch


def test_app_list_reads_the_manifest_from_manitoba_manifest_env_not_a_hardcoded_path(
    monkeypatch, tmp_path
):
    """Guards the production defect found in box smoke-testing: _load_manifest
    used to hardcode HERE.parent.parent / "manifest" / "apps.yaml", which
    resolves to <repo>/manifest/apps.yaml on the workstation (HERE = .../scripts/
    /mcp) but to ~/manifest/apps.yaml on the box (HERE = ~/scripts/mcp) — a path
    that does not exist there, so app.list came back ok=False with a
    ManifestError. The fix routes through lib.cli._manifest_path(), whose first
    step honours $MANITOBA_MANIFEST. Point that env var at a throwaway
    single-app manifest (nothing like the real 24-app one) and confirm app.list
    reflects THAT file. If _load_manifest ever reverts to a hardcoded
    repo-relative path, this env var is ignored, the real repo manifest (24
    lifecycle apps) is read instead, and the assertion below goes red."""
    d = _load()
    throwaway = tmp_path / "throwaway-apps.yaml"
    throwaway.write_text(
        "apps:\n"
        "  probe-app:\n"
        "    class: systemd\n"
        "    kuma_monitor: null\n"
        "    health:\n"
        "      kind: systemd_only\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MANITOBA_MANIFEST", str(throwaway))
    env = d.dispatch(["app.list"])
    assert env["ok"] is True
    assert env["lines"] == ["probe-app systemd"]


def test_quota_raw_parses_real_quota_w_output(monkeypatch):
    """Exercises the actual `quota -w` line-parsing (the seam every other
    quota test here mocks past), against the exact sample line shape from
    the spec: `/dev/sdaa1 2292465076  2929721344  2929721344 ...`."""
    d = _load()
    import subprocess
    sample = (
        "Disk quotas for user example (uid 1234):\n"
        "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
        "     /dev/sdaa1 2292465076  2929721344  2929721344          185567       0       0\n"
    )
    class FakeProc:
        stdout = sample
        stderr = ""
        returncode = 0
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    raw = d._quota_raw()
    assert raw["used_gb"] == round(2292465076 / 1024 / 1024, 1)
    assert raw["total_gb"] == round(2929721344 / 1024 / 1024, 1)
