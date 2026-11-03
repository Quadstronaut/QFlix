"""tdarr-port-drain.sh contract tests (QFLX-97: it drains the unix socket).

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


class TestSocketOverridePrecedence:
    """The regression that cost a production process, carried into QFLX-97.

    The TCP-era original was:

        PORT=$(grep ... "$CONF")
        : "${PORT:=${TDARR_PORT:-42018}}"

    `:=` only fires when PORT is unset or empty, so a successful config read
    made TDARR_PORT dead, silently. The first attempt to exercise the drain
    against a scratch port therefore drained the REAL one and SIGKILLed the
    live Tdarr_Server (2026-08-20).

    Since QFLX-97 the drain watches Tdarr's unix socket (the relay owns the TCP
    port for good, so draining that would block every start). Same rule: a
    destructive script MUST be pointable somewhere safe, override first.
    """

    def test_override_is_read_before_the_default(self):
        src = _code()
        assert src.index('SOCK="${TDARR_SOCK:-}"') < src.index("/qflix-tdarr/tdarr.sock")

    def test_default_is_guarded_by_an_empty_override(self):
        assert re.search(
            r'SOCK="\$\{TDARR_SOCK:-\}"\s*\nif \[ -z "\$SOCK" \]; then',
            _code(),
        ), "default must sit inside `if [ -z \"$SOCK\" ]`"

    def test_no_colon_equals_chain_on_the_override(self):
        code = _code()
        assert ':=${TDARR_' not in code
        assert ':= ${TDARR_' not in code

    def test_default_is_the_socket_50a_patches_in(self):
        assert 'SOCK="/run/user/$(id -u)/qflix-tdarr/tdarr.sock"' in _code()

    def test_never_watches_the_tcp_port_the_relay_owns(self):
        code = _code()
        assert "ss -ltn" not in code and "sport = :" not in code
        assert 'ss -lxH src "$SOCK"' in code


class TestDrainSafetyContract:
    def test_refuses_rather_than_killing_a_process_it_does_not_own(self):
        src = _src()
        # Only our own Tdarr/node processes are killable, by comm.
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

    def test_fast_path_exits_zero_when_the_socket_is_already_free(self):
        # The common case must be silent and instant, or every start pays for
        # a race that usually is not there.
        assert "sock_busy || exit 0" in _src()


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
        # Inert since QFLX-97 (50a pins the listener), kept so the unit text
        # stays what the box has run since 2026-08-20.
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
