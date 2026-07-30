"""A created monitor must never be left without a usable push token.

WHY (2026-07-30): `dash-asset-integrity` was deployed, scheduled, and ran on its
timer with `Result=success ExecMainStatus=0` -- while pushing NOTHING. Its Kuma
monitor held exactly one heartbeat ever: status=0, "No heartbeat in the time
window". Timer green, systemd green, zero coverage.

Chain, each link individually reasonable:
  1. bootstrap creates the monitor; `_add_push_monitor` RETURNS its push token
  2. the app/canary/janitor loops DISCARDED that return value
  3. the token was re-read later from `api.get_monitors()`, which races -- the
     file itself documents "get_monitors() sometimes returns the PUSH monitor
     without the pushToken field even seconds after creation", which is why
     `pusher` and `fleet` keep a create-time fallback and nothing else did
  4. the key landed in `missing`, which printed `[warn]` and returned 0
  5. the installer deployed a token file with that key absent
  6. `manitoba-maint canary push <name>` SILENTLY EXITS 0 with no token

So the guard against dead canaries was itself a dead canary.

These are STRUCTURAL pins, and the docstring says so rather than implying
coverage that is not there: `main()` is a single ~300-line function that talks
to a live socket API, so the create-then-reconcile path cannot be exercised
without standing up Kuma. What is pinned is the property whose removal is
invisible -- that the create-time token is captured and used as a fallback, and
that a missing token fails the run instead of warning.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "scripts" / "maint" / "bootstrap-kuma-monitors.py"
INSTALLER = REPO / "scripts" / "configure" / "240-maintenance-install.sh"

SRC = BOOTSTRAP.read_text(encoding="utf-8")
MAIN = SRC[SRC.index("def main("):]


def test_creation_captures_the_returned_token():
    """_add_push_monitor returns the token; the loops must keep it.

    Mutation that this catches: reverting any `tok = _add_push_monitor(...)`
    back to a bare `_add_push_monitor(...)` call.
    """
    calls = re.findall(r"^\s*(\w+\s*=\s*)?_add_push_monitor\(", MAIN, re.M)
    assert calls, "no _add_push_monitor calls found — did the function get renamed?"
    discarded = [c for c in calls if not c]
    assert not discarded, (
        f"{len(discarded)} _add_push_monitor call(s) discard the returned push "
        "token; a create-then-read race then leaves that monitor tokenless"
    )


def test_created_tokens_is_populated_at_creation():
    assert "created_tokens: dict[str, str] = {}" in MAIN, \
        "the create-time token map is gone"
    assert re.search(r"created_tokens\[\w+\] = tok", MAIN), \
        "nothing is ever stored into created_tokens"


def test_every_token_lookup_falls_back_to_the_create_time_token():
    """apps, canaries and standalone self-pushers all need the fallback.

    Before the fix only `pusher` and `fleet` had one, which is why a canary --
    and only a canary -- shipped tokenless.
    """
    fallbacks = re.findall(r"elif created_tokens\.get\(", MAIN)
    assert len(fallbacks) >= 3, (
        f"expected a created_tokens fallback for apps, canaries AND standalone "
        f"self-pushers; found {len(fallbacks)}"
    )


def test_a_missing_token_fails_the_run():
    """It printed [warn] and returned 0. A warning nobody reads is not a guard."""
    assert "token_failure = True" in MAIN, "missing tokens no longer set a failure flag"
    assert re.search(r"if token_failure:\s*\n\s*return 1", MAIN), \
        "main() no longer returns non-zero when a monitor lacks a push token"
    warn_only = re.search(r"if missing:\s*\n\s*print\(f?\"\\n\[warn\]", MAIN)
    assert not warn_only, "missing tokens reverted to a non-fatal [warn]"


def test_failure_flag_is_set_before_it_is_checked():
    """Guards against the flag being introduced but never reached."""
    assert MAIN.index("token_failure = True") < MAIN.index("if token_failure:"), \
        "token_failure is checked before anything can set it"


def test_installer_asserts_the_token_on_the_box_not_just_the_timer():
    """A scheduled timer is not evidence the canary reports anything.

    The install gate has to check the file the CONSUMER reads, on the box --
    the repo's copy being correct is what made this bug invisible.
    """
    src = INSTALLER.read_text(encoding="utf-8")
    assert "canary-token-${canary}" in src, \
        "installer no longer gates on canary push tokens"
    assert "secrets/kuma-push-tokens.json" in src
    token_gate = src[src.index("canary-token-${canary}") - 1200:]
    assert "fail" in token_gate, "the token gate has no failure branch"


def test_token_gate_covers_the_same_canaries_as_the_timer_gate():
    """Both gates iterate the same loop, so they cannot drift apart."""
    src = INSTALLER.read_text(encoding="utf-8")
    loop = re.search(r"for canary in ([a-z0-9 \-]+); do", src)
    assert loop, "canary gate loop not found"
    names = loop.group(1).split()
    assert "dash-asset-integrity" in names
    body = src[loop.end():src.index("done", loop.end())]
    assert "canary-timer-${canary}" in body
    assert "canary-token-${canary}" in body, \
        "the token gate must live in the same loop as the timer gate"
