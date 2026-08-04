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
