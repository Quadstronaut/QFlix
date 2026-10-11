"""QFLX-40: 45-plex-invites mirrors blue's share SET onto green, gate disarmed.

Loaded by path (filename starts with digits). Fakes only; no plexapi, no net.
Fixtures use synthetic example.org addresses, never real members.
"""
import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "migrate" / "45-plex-invites.py"


@pytest.fixture(scope="module")
def m():
    spec = importlib.util.spec_from_file_location("plex_invites_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OLD, NEW = "OLDID", "NEWID"
LIBS = ["Movies", "TV", "Anime", "QFlix - Welcome"]


def sec(*names):
    return lambda: [NS(title=n) for n in names]


def share(mid, names, all_libs=False):
    return NS(machineIdentifier=mid, allLibraries=all_libs, sections=sec(*names))


class Acct:
    def __init__(self, users, pending=(), invite_raises=False, invite_creates=True):
        self._users = users
        self.pending = set(pending)
        self.calls = []
        self.invite_raises = invite_raises
        self.invite_creates = invite_creates

    def users(self):
        return self._users

    def resources(self):
        mk = lambda cid: NS(clientIdentifier=cid, owned=True, provides="server",
                            connect=lambda: NS(library=NS(sections=sec(*LIBS))))
        return [mk(OLD), mk(NEW)]

    def pendingInvites(self, includeSent=True, includeReceived=True):
        return [NS(email=e, username=None) for e in self.pending]

    def inviteFriend(self, user, server, sections, allowSync=False):
        assert server == NEW
        # QFLX-49: every member may download; plexapi defaults this to False.
        assert allowSync is True, "invites must carry allowSync=True"
        self.calls.append(("invite", user, tuple(sections)))
        if self.invite_creates:
            self.pending.add(user)
        if self.invite_raises:
            raise RuntimeError("400 Bad Request")

    def updateFriend(self, user, server, sections):
        self.calls.append(("update", user, tuple(sections)))


def user(i, *names, all_libs=False, extra=()):
    return NS(id=i, email="u%d@example.org" % i, username=None,
              servers=[share(OLD, names, all_libs), *extra])


# ---- set mirroring ---------------------------------------------------------

def test_plan_mirrors_exact_share_set_by_name(m):
    a = Acct([user(1, "Movies", "TV"), user(2, "QFlix - Welcome"), user(3, "Movies", "Anime", "TV")])
    rows = m.build_plan(a, OLD, LIBS)
    assert [r["titles"] for r in rows] == [["Movies", "TV"], ["QFlix - Welcome"], ["Anime", "Movies", "TV"]]
    assert all(r["kind"] == "mirror" for r in rows)


def test_all_libraries_expands_to_blue_full_set(m):
    rows = m.build_plan(Acct([user(1, all_libs=True)]), OLD, LIBS)
    assert rows[0]["titles"] == sorted(LIBS)


def test_non_friends_excluded(m):
    stranger = NS(id=9, email="s@example.org", username=None, servers=[share("OTHER", ["Movies"])])
    rows = m.build_plan(Acct([user(1, "QFlix - Welcome"), stranger]), OLD, LIBS)
    assert len(rows) == 1


def test_zero_sections_and_missing_green_library_are_skipped_not_guessed(m):
    rows = m.build_plan(Acct([user(1), user(2, "Movies", "Ghost")]), OLD, LIBS)
    assert [r["kind"] for r in rows] == ["anomalous", "anomalous"]


def test_execute_invites_with_identical_sets_and_counts_match(m, capsys):
    a = Acct([user(1, "Movies", "TV"), user(2, "QFlix - Welcome"), user(3, "Anime")])
    rows = m.build_plan(a, OLD, LIBS)
    assert m.execute_plan(a, rows, NEW) == 0
    assert len(a.calls) == len(rows) == 3
    assert a.calls[0] == ("invite", "u1@example.org", ("Movies", "TV"))
    out = capsys.readouterr().out
    assert "example.org" not in out and "u1@" not in out


def test_rerun_skips_existing_equal_share(m):
    a = Acct([user(1, "Movies", extra=[share(NEW, ["Movies"])])])
    assert m.execute_plan(a, m.build_plan(a, OLD, LIBS), NEW) == 1  # skipped -> partial
    assert a.calls == []


def test_existing_different_share_is_updated(m):
    a = Acct([user(1, "Movies", "TV", extra=[share(NEW, ["Movies"])])])
    assert m.execute_plan(a, m.build_plan(a, OLD, LIBS), NEW) == 0
    assert a.calls == [("update", "u1@example.org", ("Movies", "TV"))]


# ---- 400-but-created -------------------------------------------------------

def test_400_with_invite_created_is_verified_not_failed(m, capsys):
    a = Acct([user(1, "Movies")], invite_raises=True, invite_creates=True)
    assert m.execute_plan(a, m.build_plan(a, OLD, LIBS), NEW) == 0
    assert "verified pending" in capsys.readouterr().out


def test_400_with_no_invite_is_a_failure(m):
    a = Acct([user(1, "Movies")], invite_raises=True, invite_creates=False)
    assert m.execute_plan(a, m.build_plan(a, OLD, LIBS), NEW) == 1


def test_already_pending_invite_is_not_resent(m):
    a = Acct([user(1, "Movies")], pending=["u1@example.org"])
    m.execute_plan(a, m.build_plan(a, OLD, LIBS), NEW)
    assert a.calls == []


# ---- gate assertion --------------------------------------------------------

GOOD = "ROSTER_ARMED=false\nPROBE_END\n"


@pytest.mark.parametrize("out,ok", [
    (GOOD, True),
    ("ROSTER_ARMED=False\nPROBE_END\n", True),
    ("ROSTER_ARMED=true\nPROBE_END\n", False),
    ("ROSTER_ARMED=false\nDROPIN=execute.conf\nPROBE_END\n", False),
    ("ROSTER_ARMED=MISSING\nPROBE_END\n", False),
    ("ROSTER_ARMED=false\n", False),
    ("", False),
])
def test_parse_gate_probe(m, out, ok):
    assert m.parse_gate_probe(out)[0] is ok


def test_gate_unreachable_or_no_host_fails_closed(m):
    def boom(h):
        raise RuntimeError("ssh down")
    assert m.assert_gate_disarmed("h", runner=boom)[0] is False
    assert m.assert_gate_disarmed(None)[0] is False


def test_probe_command_is_read_only(m):
    for bad in ("rm ", "systemctl", "tee", "sed -i", "> "):
        assert bad not in m.GATE_PROBE_CMD


# ---- CLI end to end with fake plexapi + fake ssh ---------------------------

def _cli_env(tmp_path, probe_out):
    fake = tmp_path / "fakepkgs" / "plexapi"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("")
    (fake / "myplex.py").write_text(textwrap.dedent('''
        import os
        from types import SimpleNamespace as NS
        LOG = os.environ["FAKE_LOG"]
        def _s(names): return lambda: [NS(title=n) for n in names]
        class MyPlexAccount:
            def __init__(self, token=None): pass
            def users(self):
                return [NS(id=1, email="a@example.org", username=None,
                           servers=[NS(machineIdentifier="OLDID", allLibraries=False, sections=_s(["Movies"]))])]
            def resources(self):
                mk = lambda c: NS(clientIdentifier=c, owned=True, provides="server",
                                  connect=lambda: NS(library=NS(sections=_s(["Movies"]))))
                return [mk("OLDID"), mk("NEWID")]
            def pendingInvites(self, **k): return []
            def inviteFriend(self, **k): open(LOG, "a").write("invite\\n")
            def updateFriend(self, **k): open(LOG, "a").write("update\\n")
    '''))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text("#!/bin/sh\nprintf '%s' \"$FAKE_PROBE\"\n")
    ssh.chmod(0o755)
    sec_dir = tmp_path / "secrets"
    sec_dir.mkdir()
    (sec_dir / "plex.token").write_text("tok")
    return dict(os.environ, PYTHONPATH=str(tmp_path / "fakepkgs"),
                PATH=str(bindir) + os.pathsep + os.environ["PATH"],
                MANITOBA_SECRETS_DIR=str(sec_dir), FAKE_PROBE=probe_out,
                FAKE_LOG=str(tmp_path / "log"))


def _run(env, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--old-machine-id", "OLDID", *args],
                          capture_output=True, text=True, env=env)


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake ssh is a sh script")


@posix_only
def test_cli_dry_run_writes_nothing_and_masks(tmp_path):
    r = _run(_cli_env(tmp_path, GOOD), "green.invalid")
    assert r.returncode == 0, r.stderr
    assert "a***@e***.org" in r.stdout and "a@example.org" not in r.stdout
    assert not (tmp_path / "log").exists()


@posix_only
def test_cli_execute_refuses_when_gate_armed(tmp_path):
    r = _run(_cli_env(tmp_path, "ROSTER_ARMED=true\nPROBE_END\n"), "green.invalid", "--execute")
    assert r.returncode == 2 and "REFUSING" in r.stderr
    assert not (tmp_path / "log").exists()


@posix_only
def test_cli_execute_refuses_without_host(tmp_path):
    r = _run(_cli_env(tmp_path, GOOD), "--execute")
    assert r.returncode == 2 and not (tmp_path / "log").exists()


@posix_only
def test_cli_execute_invites_when_disarmed(tmp_path):
    r = _run(_cli_env(tmp_path, GOOD), "green.invalid", "--execute")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "log").read_text().strip() == "invite"
