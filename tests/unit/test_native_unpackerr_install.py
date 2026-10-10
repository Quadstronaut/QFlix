"""scripts/configure/300-native-unpackerr-install.sh (QFLX-25, A1 pilot, spec 5.9).

Subprocess tests. Every step runs against fakes that MODEL the box rather than
just recording argv: a fake /proc tree (the UCC container's pid lives in a
docker cgroup; the native pid in the qflix-unpackerr.service cgroup), and fake
appctl / systemctl / ss / ps / rar / hostpolicy / curl that mutate that tree the
way the real tools would. So "the container exited" and "the unit is active"
are STATE the installer has to observe, never an exit status it can trust
(guard the thing, not the exit code).

Real python runs swapstate.py and suppression.py, so swap state and
push-suppress.json are the real files.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "configure" / "300-native-unpackerr-install.sh"
GOLDEN_UNIT = REPO / "scripts" / "maint" / "systemd" / "qflix-unpackerr.service"
UNIT = "qflix-unpackerr.service"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _posix(p) -> str:
    return Path(p).as_posix()


def _masked(p: Path) -> bool:
    """systemd: linked to /dev/null OR an empty file. Asked through bash: Git
    Bash may emulate the link in a way only its own runtime recognises."""
    f = _posix(p)
    return subprocess.run(["bash", "-c", f'[ -L "{f}" ] || {{ [ -f "{f}" ] && [ ! -s "{f}" ]; }}'],
                          capture_output=True).returncode == 0


def _uid() -> str:
    return subprocess.run(["bash", "-c", "id -u"], capture_output=True,
                          text=True).stdout.strip()


FAKE_UNPACKERR = r'''#!/usr/bin/env bash
# Fake unpackerr: register 13 threads under the fake /proc, then "extract" every
# fixture.rar dropped (inside a sub-folder) into the watch dir.
conf=""
while [ $# -gt 0 ]; do [ "$1" = -c ] && conf="$2"; shift; done
watch=$(sed -n 's/^path = "\(.*\)"/\1/p' "$conf")
out=$(sed -n 's/^extract_path = "\(.*\)"/\1/p' "$conf")
mkdir -p "$QFLIX_PROC/$$/task"
for i in $(seq 1 13); do mkdir -p "$QFLIX_PROC/$$/task/$i"; done
while :; do
  for d in "$watch"/*/; do
    [ -f "$d/fixture.rar" ] || continue
    n=$(basename "$d"); mkdir -p "$out/${n}_unpackerred"
    cp "$d/fixture.rar" "$out/${n}_unpackerred/qflix-fixture.txt"
  done
  sleep 0.2
done
'''


class Box:
    """A fake slot. Paths are POSIX strings for bash."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.apps = self.home / ".apps"
        self.appdir = self.apps / "unpackerr"
        self.unitdir = self.home / ".config" / "systemd" / "user"
        self.envdir = self.home / ".config" / "qflix"
        self.state = self.home / ".opt" / "maint"
        self.swap = self.state / "swap"
        self.proc = tmp / "proc"
        self.stub = tmp / "stub"
        self.calls = tmp / "calls.log"
        self.manifest = self.state / "apps.yaml"
        for d in (self.appdir, self.unitdir, self.envdir, self.swap, self.proc, self.stub):
            d.mkdir(parents=True, exist_ok=True)
        (self.appdir / "unpackerr.conf").write_text(
            '[[general]]\nlog_file = "/home/u/.apps/unpackerr/unpackerr.log"\n\n'
            '[[sonarr]]\nurl = "http://gw:1/sonarr"\napi_key = "k"\n'
            'paths = ["/home/u/downloads/qbittorrent"]\n', newline="\n")
        self.uid = _uid()
        self.set_manifest(swap_state="pending-swap")
        self.container_up()
        self._tarball()
        self._stubs()

    # --- state ---------------------------------------------------------------
    def _proc(self, pid: int, cgroup: str, cmd: str):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(f"Name:\tx\nUid:\t{self.uid}\t{self.uid}\n", newline="\n")
        (d / "cgroup").write_text(cgroup + "\n", newline="\n")
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
        (d / "task" / "1").mkdir(parents=True, exist_ok=True)

    def container_up(self):
        self._proc(9001, "0::/system.slice/docker-abc.scope", "/unpackerr")

    def container_running(self) -> bool:
        return (self.proc / "9001").exists()

    def native_running(self) -> bool:
        return (self.proc / "9002").exists()

    def set_manifest(self, *, cls="systemd", swap_state=None, dormant=True):
        lines = ["apps:", "  unpackerr:", f"    class: {cls}", "    ucc_slug: unpackerr"]
        if cls == "systemd":
            lines.append(f"    unit: {UNIT}")
        if dormant:
            lines.append("    ucc_dormant: true")
        if swap_state:
            lines.append(f"    swap_state: {swap_state}")
        self.manifest.write_text("\n".join(lines) + "\n", newline="\n")

    def calls_text(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""

    # --- fakes ---------------------------------------------------------------
    def _tarball(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = FAKE_UNPACKERR.encode()
            ti = tarfile.TarInfo("unpackerr")
            ti.size, ti.mode = len(data), 0o755
            tf.addfile(ti, io.BytesIO(data))
            readme = b"readme"
            ti = tarfile.TarInfo("README.md")
            ti.size = len(readme)
            tf.addfile(ti, io.BytesIO(readme))
        self.payload = self.tmp / "payload.tgz"
        self.payload.write_bytes(buf.getvalue())
        self.sha = hashlib.sha256(buf.getvalue()).hexdigest()

    def _w(self, name: str, body: str):
        p = self.stub / name
        p.write_text("#!/usr/bin/env bash\n" + body, newline="\n")
        p.chmod(0o755)

    def _stubs(self):
        P, C = _posix(self.proc), _posix(self.calls)
        self._w("appctl", f'''echo "appctl $*" >> "{C}"
case "$1" in
  version) echo '{{"data": {{"version": "'"${{FAKE_UCC_VERSION:-0.16.1}}"'"}}, "result": true}}' ;;
  stop) [ "${{FAKE_CONTAINER_STICKS:-0}}" = 1 ] || rm -rf "{P}/9001" ;;
  start) mkdir -p "{P}/9001/task/1"
         printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9001/status"
         echo "0::/system.slice/docker-abc.scope" > "{P}/9001/cgroup"
         printf '/unpackerr\\0' > "{P}/9001/cmdline" ;;
  is-native) v="${{FAKE_ISNATIVE:-ucc}}"; echo "$v"; [ "$v" = native ] ;;
esac
''')
        self._w("systemctl", f'''echo "systemctl $*" >> "{C}"
[ "$1" = --user ] && shift
U="{_posix(self.unitdir)}"
case "$1" in
  is-active) [ -d "{P}/9002" ] && echo active && exit 0; echo inactive; exit 3 ;;
  enable) if [ "$2" = --now ]; then
            {{ [ -L "$U/$3" ] || [ ! -s "$U/$3" ]; }} && {{ echo "unit $3 is masked or missing" >&2; exit 1; }}
            [ "${{FAKE_NATIVE_FAILS:-0}}" = 1 ] && exit 0
            mkdir -p "{P}/9002/task/1"
            printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9002/status"
            echo "0::/user.slice/user-1.slice/app.slice/$3" > "{P}/9002/cgroup"
            printf '/h/.apps/unpackerr/bin/current/unpackerr\\0-c\\0x\\0' > "{P}/9002/cmdline"
          fi ;;
  stop) rm -rf "{P}/9002" ;;
  mask) [ -s "$U/$2" ] && [ ! -L "$U/$2" ] && {{ echo "Failed to mask unit: File $U/$2 already exists." >&2; exit 1; }}
        ln -sf /dev/null "$U/$2" ;;
  unmask) {{ [ -L "$U/$2" ] || [ ! -s "$U/$2" ]; }} && rm -f "$U/$2" ;;
esac
exit 0
''')
        self._w("ss", 'cat "${FAKE_SS_FILE:-/dev/null}"\n')
        self._w("ps", 'n=${FAKE_TASKS:-1000}; for i in $(seq 1 "$n"); do echo x; done\n')
        self._w("rar", 'cp "$5" "$4"\n')   # rar a -ep -idq ARCHIVE FILE
        self._w("curl", 'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                        f'cp "{_posix(self.payload)}" "$out"\n')
        self._w("hostpolicy", f'''echo "hostpolicy $*" >> "{C}"
case "$1" in
  preflight) [ -n "${{FAKE_PROFILE-ultra}}" ] || exit 2; echo "${{FAKE_PROFILE-ultra}}" ;;
  in-window) exit "${{FAKE_INWINDOW_RC:-1}}" ;;
  task-ceiling) echo "${{FAKE_CEILING:-2000}}" ;;
esac
''')

    # --- run -----------------------------------------------------------------
    def run(self, *args, env=None, timeout=120):
        marker = self.tmp / "host.id"
        marker.write_text("test-slot\n")
        e = dict(os.environ,
                 HOME=_posix(self.home),
                 QFLIX_HOST_ID_FILE=_posix(marker),
                 QFLIX_APPS_DIR=_posix(self.apps),
                 QFLIX_UNIT_DIR=_posix(self.unitdir),
                 QFLIX_ENV_DIR=_posix(self.envdir),
                 QFLIX_SWAP_DIR=_posix(self.swap),
                 MANITOBA_STATE_DIR=_posix(self.state),
                 QFLIX_MANIFEST=_posix(self.manifest),
                 QFLIX_PROC=_posix(self.proc),
                 QFLIX_PYTHON=_posix(sys.executable),
                 QFLIX_APPCTL=_posix(self.stub / "appctl"),
                 QFLIX_SYSTEMCTL=_posix(self.stub / "systemctl"),
                 QFLIX_SS=_posix(self.stub / "ss"),
                 QFLIX_PS=_posix(self.stub / "ps"),
                 QFLIX_RAR=_posix(self.stub / "rar"),
                 QFLIX_CURL=_posix(self.stub / "curl"),
                 QFLIX_HOSTPOLICY=_posix(self.stub / "hostpolicy"),
                 QFLIX_UNPACKERR_SHA256=self.sha,
                 QFLIX_POLL_S="0.2", QFLIX_SETTLE_S="0.3",
                 QFLIX_STOP_TIMEOUT_S="3", QFLIX_PROOF_TIMEOUT_S="20")
        e.update(env or {})
        return subprocess.run(["bash", _posix(INSTALLER), *args], env=e,
                              capture_output=True, text=True, timeout=timeout)

    def installed(self):
        r = self.run("--install", "--execute")
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def proved(self):
        self.installed()
        r = self.run("--prove", "--execute")
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def swapped(self):
        self.proved()
        r = self.run("--swap", "--execute")
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def suppressed(self) -> dict:
        p = self.state / "push-suppress.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def swapstate(self) -> dict:
        p = self.swap / "unpackerr" / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}


@pytest.fixture()
def box(tmp_path):
    return Box(tmp_path)


# --- static -------------------------------------------------------------------

def test_bash_syntax():
    r = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_pins_exact_version_and_sha256_matching_versions_env():
    text = INSTALLER.read_text(encoding="utf-8")
    ver = next(l.split("=", 1)[1].strip() for l in
               (REPO / "versions.env").read_text(encoding="utf-8").splitlines()
               if l.startswith("UNPACKERR_VERSION="))
    assert f'VERSION="{ver}"' in text
    assert 'SHA256="821b84f96f99213e30a675e1fdcd4266d7b60c926bb05e795feffdfd928d6eb5"' in text


def test_240_stages_and_deploys_the_installer_with_its_lib():
    text = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    assert "    scripts/lib/native.sh \\\n" in text
    assert "    scripts/configure/300-native-unpackerr-install.sh \\\n" in text
    assert 'cp -f   "$STG"/scripts/lib/native.sh ~/scripts/lib/native.sh' in text
    assert ("~/scripts/configure/300-native-unpackerr-install.sh\n"
            "chmod +x ~/scripts/configure/300-native-unpackerr-install.sh") in text


def test_installer_never_calls_the_panel_tool_directly():
    code = [l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith("#")]
    assert not any("app-unpackerr" in l for l in code)


def test_golden_unit_is_what_the_installer_renders(box):
    box.installed()
    staged = box.appdir / "native" / UNIT
    assert staged.read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    unit = GOLDEN_UNIT.read_text(encoding="utf-8")
    assert "ExecStart=%h/.apps/unpackerr/bin/current/unpackerr -c %h/.apps/unpackerr/unpackerr.conf" in unit
    assert "Environment=PATH=%h/.apps/unpackerr/bin/current:" in unit
    assert "TasksMax" not in unit


# --- inert by default -----------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["--install"], ["--prove"], ["--swap"],
                                  ["--finish"], ["--rollback"]])
def test_without_execute_nothing_is_touched(box, args):
    before = sorted(p.as_posix() for p in box.tmp.rglob("*"))
    r = box.run(*args)
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    after = sorted(p.as_posix() for p in box.tmp.rglob("*")
                   if p.name != "host.id")
    assert after == [p for p in before]
    assert "appctl" not in box.calls_text() and "systemctl" not in box.calls_text()


def test_unknown_flag_is_usage_error(box):
    assert box.run("--frobnicate").returncode == 64


def test_monday_window_refuses_execute(box):
    r = box.run("--install", "--execute", env={"FAKE_INWINDOW_RC": "0"})
    assert r.returncode != 0
    assert "window" in r.stderr.lower()
    assert not (box.appdir / "bin").exists()


def test_missing_host_profile_fails_closed(box):
    r = box.run("--install", "--execute", env={"FAKE_PROFILE": ""})
    assert r.returncode != 0
    assert not (box.appdir / "bin").exists()


# --- step 1: pin + install --------------------------------------------------------

def test_install_lays_out_binary_env_and_stages_unit_without_enabling(box):
    box.installed()
    cur = box.appdir / "bin" / "current"
    assert (box.appdir / "bin" / "0.16.1" / "unpackerr").exists()
    if os.name != "nt":   # Git Bash on Windows copies instead of linking
        assert os.readlink(cur) == "0.16.1"
    assert (cur / "unpackerr").exists()
    env = (box.envdir / "unpackerr.env").read_text().splitlines()
    assert "GOMAXPROCS=4" in env and "MALLOC_ARENA_MAX=2" in env
    assert "TZ=Europe/Amsterdam" in env
    assert (box.appdir / "native" / UNIT).exists()
    # NOT in the unit dir and never enabled: WantedBy=default.target would start
    # it beside the live container on the next user-manager start (I-6).
    assert not (box.unitdir / UNIT).exists()
    assert "enable" not in box.calls_text()
    assert box.container_running()


def test_install_refuses_version_mismatch(box):
    r = box.run("--install", "--execute", env={"FAKE_UCC_VERSION": "0.16.0"})
    assert r.returncode != 0
    assert not (box.appdir / "bin" / "0.16.1").exists()


def test_install_refuses_sha_mismatch(box):
    r = box.run("--install", "--execute", env={"QFLIX_UNPACKERR_SHA256": "0" * 64})
    assert r.returncode != 0
    assert "sha256" in r.stderr
    assert not (box.appdir / "bin").exists()


@pytest.mark.skipif(os.name == "nt", reason="Git Bash copies `current` instead of "
                    "linking, so the second atomic link swap hits a real dir")
def test_install_is_idempotent(box):
    box.installed()
    box.installed()
    assert (box.appdir / "bin" / "current" / "unpackerr").exists()


# --- step 2: inert proof -----------------------------------------------------------

def test_prove_extracts_fixture_measures_tasks_and_cleans_up(box):
    r = box.proved()
    assert "delta=13" in r.stdout
    assert not (box.apps / ".prove" / "unpackerr").exists()
    proof = json.loads((box.swap / "unpackerr" / "proof.json").read_text())
    assert proof["delta"] == 13 and proof["ceiling"] == 2000 and proof["ok"] is True
    assert box.container_running()                      # live app untouched


def test_prove_refuses_at_seventy_percent_of_ceiling(box):
    box.installed()
    # 1388 + 13 = 1401 >= 0.70 * 2000
    r = box.run("--prove", "--execute", env={"FAKE_TASKS": "1388"})
    assert r.returncode != 0
    assert "70%" in r.stderr
    assert not (box.swap / "unpackerr" / "proof.json").exists()
    assert not (box.apps / ".prove" / "unpackerr").exists()


def test_prove_config_is_inert(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"QFLIX_KEEP_PROOF": "1"})
    assert r.returncode == 0, r.stderr
    conf = (box.apps / ".prove" / "unpackerr" / "unpackerr.conf").read_text()
    for section in ("[[sonarr]]", "[[radarr]]", "[[lidarr]]", "[[readarr]]",
                    "[[whisparr]]", "[webserver]", "[[webhook]]", "[[cmdhook]]"):
        assert section not in conf
    assert "[[folder]]" in conf


# --- steps 3-6: swap ------------------------------------------------------------------

def test_swap_refuses_without_a_proof(box):
    box.installed()
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "prove" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_refuses_unless_the_pending_swap_flip_is_deployed(box):
    box.proved()
    box.set_manifest(cls="ucc", dormant=False)
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert box.container_running()


def test_swap_full_sequence(box):
    r = box.swapped()
    calls = box.calls_text()
    # capture -> suppress -> stop container -> container gone -> enable --now
    assert calls.index("appctl stop unpackerr") < calls.index(f"systemctl --user enable --now {UNIT}")
    assert not box.container_running() and box.native_running()
    assert (box.unitdir / UNIT).read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    # listen set captured and EMPTY (unpackerr has no listener)
    assert (box.swap / "unpackerr" / "listen-set.before").read_text() == ""
    st = box.swapstate()
    assert st["ucc_version"] == "0.16.1"
    assert st["swap_date"] and st["soak_until"] and st["rollback_window"] == "open"
    # suppression stays ON until --finish (the manifest still says pending-swap)
    assert set(box.suppressed()) == {"unpackerr", "canary-thread-ceiling"}
    assert list((box.swap / "unpackerr").glob("snapshot-*.tgz"))
    assert "elapsed=" in r.stdout


def test_swap_refuses_a_non_empty_listen_set(box):
    box.proved()
    ss = box.tmp / "ss.txt"
    ss.write_text("LISTEN 0 4096 0.0.0.0:5656 0.0.0.0:*\n")
    r = box.run("--swap", "--execute", env={"FAKE_SS_FILE": _posix(ss)})
    assert r.returncode != 0 and "listen" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_refuses_container_paths_in_config(box):
    box.proved()
    conf = box.appdir / "unpackerr.conf"
    conf.write_text(conf.read_text() + 'paths = ["/downloads/x"]\n', newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "container path" in r.stderr
    assert box.container_running()


def test_swap_aborts_when_container_never_exits_and_restores_service(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_CONTAINER_STICKS": "1"})
    assert r.returncode != 0
    assert "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert not box.native_running()
    assert box.container_running()
    assert not box.suppressed()          # back to normal monitoring


def test_swap_parity_failure_is_reported_and_stays_suppressed(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_NATIVE_FAILS": "1"})
    assert r.returncode != 0
    assert "--rollback" in r.stderr
    assert "unpackerr" in box.suppressed()


def test_swap_is_resumable_when_already_swapped(box):
    box.swapped()
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stderr
    assert "already" in r.stdout
    assert box.calls_text().count("appctl stop unpackerr") == 1


# --- step 9: finish -----------------------------------------------------------------

def test_finish_refuses_while_manifest_still_pending_swap(box):
    box.swapped()
    r = box.run("--finish", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert "unpackerr" in box.suppressed()


def test_finish_lifts_app_and_canaries_together(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--finish", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 0, r.stderr
    assert box.suppressed() == {}


# --- rollback (0-5) + drill ---------------------------------------------------------------

def test_rollback_step0_suppresses_and_masks_before_stopping(box):
    box.swapped()
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls_text()
    assert calls.index(f"systemctl --user mask {UNIT}") < calls.index(f"systemctl --user stop {UNIT}")
    assert _masked(box.unitdir / UNIT)
    assert not box.native_running() and box.container_running()
    assert calls.rindex("appctl start unpackerr") > calls.index(f"systemctl --user stop {UNIT}")
    assert box.suppressed() == {}
    assert "elapsed=" in r.stdout


def test_rollback_pauses_before_ucc_start_until_manifest_reverted(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--rollback", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 10
    assert "revert" in r.stderr
    assert not box.native_running() and not box.container_running()
    assert "appctl start" not in box.calls_text()
    assert "unpackerr" in box.suppressed()               # stays muted while paused
    # operator reverts the deployed manifest, re-runs: resumes at step 3
    box.set_manifest(swap_state="pending-swap")
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running()


def test_drill_rollback_then_reswap_unmasks(box):
    box.swapped()
    assert box.run("--rollback", "--execute").returncode == 0
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"systemctl --user unmask {UNIT}" in box.calls_text()
    assert not _masked(box.unitdir / UNIT)
    assert box.native_running() and not box.container_running()


def test_rollback_with_nothing_swapped_is_harmless(box):
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running()
