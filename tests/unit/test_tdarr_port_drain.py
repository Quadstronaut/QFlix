"""tdarr-port-drain.sh contract tests.

The drain is the fix for the EADDRINUSE restart flap: `systemctl restart`
returns when systemd reaps the main PID, but Tdarr_Server's listener can
outlive it, so the replacement binds instantly and dies. It runs as
ExecStartPre because that is the one place all three restart paths (config
deploy, 5-minute heartbeat, Restart=on-failure) pass through.

These are text contracts, in the same style as the other shell pins in this
suite: the behavioural paths were exercised against the live box, but the
SHAPE has to stay pinned here or it rots silently.
"""
from __future__ import annotations

import os
import re

_REPO = os.path.join(os.path.dirname(__file__), "..", "..")
_DRAIN = os.path.join(_REPO, "scripts", "ops", "tdarr-port-drain.sh")


def _src() -> str:
    with open(_DRAIN, encoding="utf-8") as fh:
        return fh.read()


def _code() -> str:
    """Executable lines only.

    The header comment deliberately QUOTES the broken idiom it replaced, so a
    naive substring check over the whole file matches the documentation of the
    bug and calls it the bug. Comments are the wrong place to look for code.
    """
    return "\n".join(
        line for line in _src().splitlines()
        if not line.lstrip().startswith("#")
    )


class TestPortOverridePrecedence:
    """The regression that cost a production process.

    The original was:

        PORT=$(grep ... "$CONF")
        : "${PORT:=${TDARR_PORT:-42018}}"

    `:=` only fires when PORT is unset or empty, so a successful config read
    made TDARR_PORT dead — silently. The first attempt to exercise the drain
    against a scratch port therefore drained the REAL one and SIGKILLed the
    live Tdarr_Server (2026-08-20). It came back 10s later on
    Restart=on-failure, which at least proved that path worked.

    The rule this encodes: a destructive script MUST be pointable somewhere
    safe, or it only ever gets tested in production.
    """

    def test_override_is_read_before_the_config_file(self):
        src = _code()
        i_override = src.index('PORT="${TDARR_PORT:-}"')
        i_config = src.index('grep -oP \'"serverPort"')
        assert i_override < i_config, (
            "TDARR_PORT must be consulted BEFORE the config read, or a "
            "successful config parse silently disables the override"
        )

    def test_config_read_is_guarded_by_an_empty_port(self):
        # The config read only happens when the override did not supply one.
        assert re.search(
            r'PORT="\$\{TDARR_PORT:-\}"\s*\nif \[ -z "\$PORT" \]; then',
            _code(),
        ), "config read must sit inside `if [ -z \"$PORT\" ]`"

    def test_no_colon_equals_chain_on_tdarr_port(self):
        # The exact broken idiom must not come back. Checked against CODE, not
        # the header comment, which quotes the broken line on purpose.
        code = _code()
        assert ':=${TDARR_PORT' not in code
        assert ':= ${TDARR_PORT' not in code

    def test_default_still_applies_when_neither_source_yields_a_port(self):
        assert ': "${PORT:=42018}"' in _code()


class TestDrainSafetyContract:
    def test_refuses_rather_than_killing_a_process_it_does_not_own(self):
        src = _src()
        # Only our own Tdarr/node processes are killable. `ss -ltnp` shows the
        # pid for OUR sockets only, so anything else is another tenant.
        assert "Tdarr*|node)" in src
        assert "not ours to kill" in src
        # And the give-up path must be a hard failure, so the unit goes
        # visibly FAILED instead of crash-looping on a buried log line.
        assert "REFUSING START" in src
        assert src.rstrip().endswith("exit 1")

    def test_polite_wait_before_any_escalation(self):
        # No SIGKILL until at least halfway through the timeout — a socket in
        # lingering close clears on its own, and Tdarr ignores SIGTERM for a
        # beat while it flushes its DB.
        assert 'WAITED" -ge $((TIMEOUT / 2))' in _src()

    def test_fast_path_exits_zero_when_the_port_is_already_free(self):
        # The common case must be silent and instant, or every start pays for
        # a race that usually is not there.
        assert "port_busy || exit 0" in _src()


class TestUnitWiring:
    def test_installer_wires_the_drain_as_execstartpre_without_dash(self):
        p = os.path.join(_REPO, "scripts", "configure", "50-tdarr-install.sh")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        assert "ExecStartPre=%h/scripts/ops/tdarr-port-drain.sh" in src
        # A `-` prefix would make systemd ignore a failed drain, which is
        # exactly the case the drain exists to stop.
        assert "ExecStartPre=-" not in src

    def test_unit_kills_the_whole_cgroup_and_caps_the_crash_loop(self):
        p = os.path.join(_REPO, "scripts", "configure", "50-tdarr-install.sh")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        # A lingering child holding the port would recreate the race.
        assert "KillMode=control-group" in src
        # A crash-loop on a SHARED slot is antisocial.
        assert "StartLimitBurst=5" in src
        # Restores the loopback pin that lived only on the box, so an
        # installer re-run cannot silently un-pin the listener.
        assert 'Environment="HOST=127.0.0.1" "serverIP=127.0.0.1"' in src

    def test_ffmpeg_threadcap_marker_agrees_across_every_surface(self):
        """A check that cannot tell present from absent is worse than none.

        The first smoke-test assertion grepped `ffmpeg-threadcap` against the
        shim's prose, which reads "ffmpeg thread-cap shim". It reported MISSING
        against a healthy, working shim on 2026-08-20 — a false alarm that, left
        alone, trains you to ignore the one check standing between a Tdarr
        upgrade and the box's task ceiling.

        So the marker is an unbroken token on its own line, and all three
        surfaces must grep the SAME token. Same law as the worker1.js
        QFLIX-WORKER2-EXIT-NULLGUARD marker.
        """
        MARKER = "QFLIX-FFMPEG-THREADCAP"
        shim = os.path.join(_REPO, "scripts", "ops", "ffmpeg-threadcap-shim.sh")
        with open(shim, encoding="utf-8") as fh:
            head = fh.read().splitlines()[:3]
        # Must live in the first 3 lines, because that is what `head -3` reads.
        assert any(MARKER in ln for ln in head), (
            f"{MARKER} must appear within the first 3 lines of the shim"
        )
        for rel in (("scripts", "smoke-test.sh"),
                    ("scripts", "configure", "50-tdarr-install.sh")):
            p = os.path.join(_REPO, *rel)
            with open(p, encoding="utf-8") as fh:
                src = fh.read()
            assert MARKER in src, f"{p} must grep {MARKER}"
            # The hyphenated prose form must never be used as the check token.
            assert "grep -q ffmpeg-threadcap" not in src, (
                f"{p} greps the prose form, which does not match the file"
            )

    def test_heartbeat_does_not_stack_a_restart_on_one_in_flight(self):
        p = os.path.join(_REPO, "scripts", "ops", "heartbeat-tdarr-server.sh")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        assert "activating" in src and "deactivating" in src
