# tests/unit/test_mcp_dispatch.py
import importlib.util
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
