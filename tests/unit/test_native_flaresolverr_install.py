"""scripts/configure/301-native-flaresolverr-install.sh (QFLX-26, A2, spec 5.9).

Subprocess tests against fakes that MODEL the box (same approach as the pilot,
test_native_unpackerr_install.py): a fake /proc tree (the UCC container's pid in
a docker cgroup, the native pid in the unit's cgroup) and fake appctl /
systemctl / ss / ps / ldd / curl / hostpolicy that mutate it like the real tools.
The proof runs a REAL local HTTP server that plays FlareSolverr (GET / ready,
POST /v1 request.get fetches the fixture), so the request path is exercised end
to end over loopback.

Real python runs swapstate.py and suppression.py: swap state and
push-suppress.json are the real files.
"""
from __future__ import annotations

import hashlib
import importlib.util
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
INSTALLER = REPO / "scripts" / "configure" / "301-native-flaresolverr-install.sh"
GOLDEN_UNIT = REPO / "scripts" / "maint" / "systemd" / "qflix-flaresolverr.service"
UNIT = "qflix-flaresolverr.service"
PORT = "17011"
LISTEN = f"LISTEN 0 65535 172.17.0.1:{PORT} 0.0.0.0:*"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _posix(p) -> str:
    return Path(p).as_posix()


def _masked(p: Path) -> bool:
    f = _posix(p)
    return subprocess.run(["bash", "-c", f'[ -L "{f}" ] || {{ [ -f "{f}" ] && [ ! -s "{f}" ]; }}'],
                          capture_output=True).returncode == 0


def _uid() -> str:
    return subprocess.run(["bash", "-c", "id -u"], capture_output=True,
                          text=True).stdout.strip()


# The bundled `flaresolverr` bootloader: registers the fake /proc entries for
# itself under the SHELL pid (exec keeps it), then becomes the server.
FAKE_EXE = r'''#!/usr/bin/env bash
if [ "${FAKE_NEEDS_XVFB:-0}" = 1 ] && ! command -v Xvfb >/dev/null 2>&1; then
  echo "Xvfb not found" >&2; exit 1
fi
export FAKE_ROOT_PID=$$
mkdir -p "$QFLIX_PROC/$$"
printf 'Name:\tflaresolverr\nPPid:\t1\n' > "$QFLIX_PROC/$$/status"
for i in $(seq 1 "${FAKE_FS_TASKS:-40}"); do mkdir -p "$QFLIX_PROC/$$/task/$i"; done
exec "$QFLIX_PYTHON" "$(dirname "$0")/_internal/server.py"
'''

FAKE_SERVER = r'''
import json, os, shutil, sys, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
root = os.environ["FAKE_ROOT_PID"]
proc = os.environ["QFLIX_PROC"]
child = int(os.environ.get("FAKE_FS_CHILD", "20"))

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self._send({"msg": "FlareSolverr is ready!", "version": "3.5.2"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        req = json.loads(self.rfile.read(n) or b"{}")
        cdir = os.path.join(proc, "99%s" % root)
        os.makedirs(os.path.join(cdir, "task"), exist_ok=True)
        with open(os.path.join(cdir, "status"), "w") as fh:
            fh.write("Name:\tchrome\nPPid:\t%s\n" % root)
        for i in range(child):
            os.makedirs(os.path.join(cdir, "task", str(i)), exist_ok=True)
        time.sleep(float(os.environ.get("FAKE_FS_REQ_S", "1.5")))   # Chromium is "up"
        try:
            body = urllib.request.urlopen(req["url"], timeout=10).read().decode()
            out = {"status": "ok", "solution": {"response": body, "status": 200}}
        except Exception as exc:
            out = {"status": "error", "message": str(exc)}
        shutil.rmtree(cdir, ignore_errors=True)
        self._send(out)

HTTPServer((os.environ["HOST"], int(os.environ["PORT"])), H).serve_forever()
'''


class Box:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.apps = self.home / ".apps"
        self.appdir = self.apps / "flaresolverr"
        self.unitdir = self.home / ".config" / "systemd" / "user"
        self.envdir = self.home / ".config" / "qflix"
        self.state = self.home / ".opt" / "maint"
        self.swap = self.state / "swap"
        self.secrets = self.home / "secrets"
        self.proc = tmp / "proc"
        self.stub = tmp / "stub"
        self.calls = tmp / "calls.log"
        self.manifest = self.state / "apps.yaml"
        for d in (self.appdir, self.unitdir, self.envdir, self.swap, self.proc,
                  self.stub, self.secrets):
            d.mkdir(parents=True, exist_ok=True)
        (self.secrets / "flaresolverr.port").write_text(PORT + "\n")
        (self.secrets / "net.app_host").write_text("172.17.0.1\n")
        self.uid = _uid()
        self.set_manifest(swap_state="pending-swap")
        self.container_up()
        self._tarball()
        self._stubs()

    # --- state ---------------------------------------------------------------
    def _proc(self, pid: int, cgroup: str, cmd: str):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(f"Name:\tx\nUid:\t{self.uid}\t{self.uid}\nPPid:\t1\n", newline="\n")
        (d / "cgroup").write_text(cgroup + "\n", newline="\n")
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
        (d / "task" / "1").mkdir(parents=True, exist_ok=True)

    def container_up(self):
        self._proc(9001, "0::/system.slice/docker-abc.scope", "/usr/local/bin/python -u /app/flaresolverr.py")
        (self.proc / "9001" / "environ").write_bytes(
            b"LOG_LEVEL=debug\0PROXY_URL=http://u:secret@x:1\0PATH=/usr/bin\0TZ=Europe/Amsterdam\0")

    def container_running(self) -> bool:
        return (self.proc / "9001").exists()

    def native_running(self) -> bool:
        return (self.proc / "9002").exists()

    def set_manifest(self, *, cls="systemd", swap_state=None, dormant=True):
        lines = ["apps:", "  flaresolverr:", f"    class: {cls}", "    ucc_slug: flaresolverr"]
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
            for name, data, mode in (
                ("flaresolverr/flaresolverr", FAKE_EXE.encode(), 0o755),
                ("flaresolverr/_internal/server.py", FAKE_SERVER.encode(), 0o644),
                ("flaresolverr/_internal/chrome/chrome", b"\x7fELFfake", 0o755),
            ):
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode = len(data), mode
                tf.addfile(ti, io.BytesIO(data))
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
  version) echo '{{"data": {{"version": "'"${{FAKE_UCC_VERSION:-3.5.2}}"'"}}, "result": true}}' ;;
  stop) [ "${{FAKE_CONTAINER_STICKS:-0}}" = 1 ] || rm -rf "{P}/9001" ;;
  start) mkdir -p "{P}/9001/task/1"
         printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9001/status"
         echo "0::/system.slice/docker-abc.scope" > "{P}/9001/cgroup"
         printf '/app/flaresolverr.py\\0' > "{P}/9001/cmdline" ;;
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
            printf '/h/.apps/flaresolverr/bin/current/flaresolverr\\0' > "{P}/9002/cmdline"
          fi ;;
  stop) rm -rf "{P}/9002" ;;
  mask) [ -s "$U/$2" ] && [ ! -L "$U/$2" ] && {{ echo "Failed to mask unit: File $U/$2 already exists." >&2; exit 1; }}
        ln -sf /dev/null "$U/$2" ;;
  unmask) {{ [ -L "$U/$2" ] || [ ! -s "$U/$2" ]; }} && rm -f "$U/$2" ;;
esac
exit 0
''')
        # Listening while EITHER runtime exists; FAKE_SS_EXTRA adds foreign rows.
        self._w("ss", f'''if [ -d "{P}/9001" ] || [ -d "{P}/9002" ]; then echo "{LISTEN}"; fi
[ -n "${{FAKE_SS_EXTRA:-}}" ] && echo "$FAKE_SS_EXTRA"
exit 0
''')
        self._w("ps", 'n=${FAKE_TASKS:-1000}; for i in $(seq 1 "$n"); do echo x; done\n')
        self._w("ldd", 'for l in ${FAKE_LDD_MISSING:-}; do echo "\t$l => not found"; done\n'
                       'echo "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x1)"\n'
                       '[ "${FAKE_LDD_FAILS:-0}" = 1 ] && exit 1\nexit 0\n')
        self._w("curl", 'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                        f'cp "{_posix(self.payload)}" "$out"\n')
        self._w("probe", f'''[ "${{FAKE_PROBE_FAILS:-0}}" = 1 ] && exit 22
if [ -d "{P}/9001" ] || [ -d "{P}/9002" ]; then echo '{{"msg": "FlareSolverr is ready!"}}'; else exit 7; fi
''')
        self._w("hostpolicy", f'''echo "hostpolicy $*" >> "{C}"
case "$1" in
  preflight) [ -n "${{FAKE_PROFILE-ultra}}" ] || exit 2; echo "${{FAKE_PROFILE-ultra}}" ;;
  in-window) exit "${{FAKE_INWINDOW_RC:-1}}" ;;
  task-ceiling) echo "${{FAKE_CEILING:-2000}}" ;;
esac
''')
        self._w("Xvfb", "exit 0\n")

    # --- run -----------------------------------------------------------------
    def run(self, *args, env=None, timeout=180):
        marker = self.tmp / "host.id"
        marker.write_text("test-slot\n")
        e = dict(os.environ,
                 HOME=_posix(self.home),
                 QFLIX_HOST_ID_FILE=_posix(marker),
                 QFLIX_APPS_DIR=_posix(self.apps),
                 QFLIX_UNIT_DIR=_posix(self.unitdir),
                 QFLIX_ENV_DIR=_posix(self.envdir),
                 QFLIX_SWAP_DIR=_posix(self.swap),
                 QFLIX_SECRETS_DIR=_posix(self.secrets),
                 MANITOBA_STATE_DIR=_posix(self.state),
                 QFLIX_MANIFEST=_posix(self.manifest),
                 QFLIX_PROC=_posix(self.proc),
                 QFLIX_PYTHON=_posix(sys.executable),
                 QFLIX_APPCTL=_posix(self.stub / "appctl"),
                 QFLIX_SYSTEMCTL=_posix(self.stub / "systemctl"),
                 QFLIX_SS=_posix(self.stub / "ss"),
                 QFLIX_PS=_posix(self.stub / "ps"),
                 QFLIX_LDD=_posix(self.stub / "ldd"),
                 QFLIX_XVFB=_posix(self.stub / "Xvfb"),
                 QFLIX_CURL=_posix(self.stub / "curl"),
                 QFLIX_PROBE_CURL=_posix(self.stub / "probe"),
                 QFLIX_HOSTPOLICY=_posix(self.stub / "hostpolicy"),
                 QFLIX_FLARESOLVERR_SHA256=self.sha,
                 QFLIX_POLL_S="0.2", QFLIX_SETTLE_S="0.3",
                 QFLIX_STOP_TIMEOUT_S="3", QFLIX_PROOF_TIMEOUT_S="30")
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
        p = self.swap / "flaresolverr" / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}


@pytest.fixture()
def box(tmp_path):
    return Box(tmp_path)


SUPPRESSED = {"flaresolverr", "canary-prowlarr-proxy-link-fatal",
              "canary-prowlarr-indexer-health", "canary-thread-ceiling"}


# --- static -------------------------------------------------------------------

def test_bash_syntax():
    r = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_pins_exact_version_and_sha256_matching_versions_env():
    text = INSTALLER.read_text(encoding="utf-8")
    ver = next(l.split("=", 1)[1].strip() for l in
               (REPO / "versions.env").read_text(encoding="utf-8").splitlines()
               if l.startswith("FLARESOLVERR_VERSION="))
    assert f'VERSION="{ver}"' in text
    assert 'SHA256="84f6df48849b2e1742692841805c4f284118fe4e2798b49d3a78159e89a46a91"' in text
    assert "flaresolverr_linux_x64.tar.gz" in text


def test_240_stages_and_deploys_the_installer():
    text = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    assert "    scripts/configure/301-native-flaresolverr-install.sh \\\n" in text
    assert ("~/scripts/configure/301-native-flaresolverr-install.sh\n"
            "chmod +x ~/scripts/configure/301-native-flaresolverr-install.sh") in text


def test_installer_never_calls_the_panel_tool_directly():
    code = [l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith("#")]
    assert not any("app-flaresolverr" in l for l in code)


def test_installer_never_binds_wide():
    code = "\n".join(l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
                     if not l.strip().startswith("#"))
    assert "0.0.0.0" in code            # only as the value check_bind_host REFUSES
    assert 'BIND_HOST=""' in code and 'BIND_HOST="$h"' in code
    assert "172.17.0.1" not in code     # the host comes from the net.app_host secret (C-12)


def test_golden_unit_is_what_the_installer_renders(box):
    box.installed()
    staged = box.appdir / "native" / UNIT
    assert staged.read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    unit = GOLDEN_UNIT.read_text(encoding="utf-8")
    assert "ExecStart=%h/.apps/flaresolverr/bin/current/flaresolverr\n" in unit
    assert "Environment=PATH=%h/.apps/flaresolverr/bin/current:%h/bin:" in unit
    assert "EnvironmentFile=%h/.config/qflix/flaresolverr.env" in unit
    assert "TasksMax" not in unit


def test_canary_restart_uses_the_absolute_appctl_path():
    spec = importlib.util.spec_from_file_location(
        "fs_canary", REPO / "scripts" / "maint" / "flaresolverr-canary.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    argv = mod.FS_RESTART_CMD.split()
    assert os.path.isabs(argv[0]) and argv[0].endswith("/bin/appctl")
    assert argv[1:] == ["restart", "flaresolverr"]


def test_canary_uptime_probe_matches_the_native_cmdline_too():
    text = (REPO / "scripts" / "maint" / "flaresolverr-canary.py").read_text(encoding="utf-8")
    assert r"\.apps/flaresolverr/bin/current/flaresolverr" in text
    assert r"/app/flaresolverr\.py" in text


# --- inert by default -----------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["--precheck"], ["--install"], ["--prove"],
                                  ["--swap"], ["--finish"], ["--rollback"]])
def test_without_execute_nothing_is_touched(box, args):
    before = sorted(p.as_posix() for p in box.tmp.rglob("*"))
    r = box.run(*args)
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    after = sorted(p.as_posix() for p in box.tmp.rglob("*") if p.name != "host.id")
    assert after == before
    assert "appctl" not in box.calls_text() and "systemctl" not in box.calls_text()


def test_unknown_flag_is_usage_error(box):
    assert box.run("--frobnicate").returncode == 64


def test_monday_window_refuses_execute(box):
    r = box.run("--install", "--execute", env={"FAKE_INWINDOW_RC": "0"})
    assert r.returncode != 0 and "window" in r.stderr.lower()
    assert not (box.appdir / "bin").exists()


def test_missing_host_profile_fails_closed(box):
    r = box.run("--install", "--execute", env={"FAKE_PROFILE": ""})
    assert r.returncode != 0
    assert not (box.appdir / "bin").exists()


# --- dependency probe (D-7) -------------------------------------------------------

def test_precheck_passes_and_installs_nothing(box):
    r = box.run("--precheck", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PRECHECK OK" in r.stdout
    assert not (box.appdir / "bin").exists()
    assert not list(box.apps.glob(".stage-*"))          # scratch dir removed


def test_missing_library_blocks_install_with_exit_3(box):
    r = box.run("--install", "--execute", env={"FAKE_LDD_MISSING": "libnss3.so libgbm.so.1"})
    assert r.returncode == 3
    assert "BLOCKED" in r.stderr and "libnss3.so" in r.stderr and "UCC" in r.stderr
    assert not (box.appdir / "bin").exists()
    assert not (box.envdir / "flaresolverr.env").exists()
    assert box.container_running()


def test_ldd_failure_fails_closed(box):
    r = box.run("--precheck", "--execute", env={"FAKE_LDD_FAILS": "1"})
    assert r.returncode != 0
    assert not (box.appdir / "bin").exists()


def test_missing_xvfb_is_only_a_warning_at_precheck(box):
    r = box.run("--precheck", "--execute", env={"QFLIX_XVFB": "/nonexistent/Xvfb"})
    assert r.returncode == 0 and "WARN" in r.stdout


# --- step 1: pin + install --------------------------------------------------------

def test_install_lays_out_binary_env_and_stages_unit_without_enabling(box):
    box.installed()
    cur = box.appdir / "bin" / "current"
    assert (box.appdir / "bin" / "3.5.2" / "flaresolverr").exists()
    assert (box.appdir / "bin" / "3.5.2" / "_internal" / "chrome" / "chrome").exists()
    assert (cur / "flaresolverr").exists()
    env = (box.envdir / "flaresolverr.env").read_text().splitlines()
    # bind: exactly the recorded listen set, headless, caps; never a wide host
    assert "HOST=172.17.0.1" in env and f"PORT={PORT}" in env
    assert "HEADLESS=true" in env and "MALLOC_ARENA_MAX=2" in env
    assert not any("0.0.0.0" in l for l in env)
    # carried from the container config; the proxy credential is NOT
    assert "LOG_LEVEL=debug" in env and "TZ=Europe/Amsterdam" in env
    assert not any(l.startswith("PROXY") for l in env)
    assert (box.appdir / "native" / UNIT).exists()
    assert not (box.unitdir / UNIT).exists()
    assert "enable" not in box.calls_text()
    assert box.container_running()
    assert oct((box.envdir / "flaresolverr.env").stat().st_mode & 0o777) in ("0o600", "0o666", "0o644")


def test_install_refuses_version_mismatch(box):
    r = box.run("--install", "--execute", env={"FAKE_UCC_VERSION": "3.4.6"})
    assert r.returncode != 0
    assert not (box.appdir / "bin" / "3.5.2").exists()


def test_install_refuses_sha_mismatch(box):
    r = box.run("--install", "--execute", env={"QFLIX_FLARESOLVERR_SHA256": "0" * 64})
    assert r.returncode != 0 and "sha256" in r.stderr
    assert not (box.appdir / "bin").exists()


@pytest.mark.parametrize("bad", ["0.0.0.0", "127.0.0.1", "", "localhost", "::"])
def test_install_refuses_a_wide_or_loopback_net_app_host(box, bad):
    (box.secrets / "net.app_host").write_text(bad + "\n")
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and "net.app_host" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_install_refuses_without_the_port_secret(box):
    (box.secrets / "flaresolverr.port").unlink()
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and "port" in r.stderr


# --- step 2: proof -----------------------------------------------------------------

def test_prove_returns_a_solution_measures_tasks_and_cleans_up(box):
    r = box.proved()
    assert "request.get returned the fixture" in r.stdout
    assert not (box.apps / ".prove" / "flaresolverr").exists()
    proof = json.loads((box.swap / "flaresolverr" / "proof.json").read_text())
    # 40 bootloader tasks + 20 chrome tasks sampled while the request ran
    assert proof["delta"] == 60 and proof["ceiling"] == 2000 and proof["ok"] is True
    assert box.container_running()                       # live app untouched
    assert not box.calls_text().count("appctl") or "appctl stop" not in box.calls_text()


def test_prove_binds_a_fresh_loopback_port_never_the_live_one(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"QFLIX_KEEP_PROOF": "1"})
    assert r.returncode == 0, r.stderr
    log = (box.apps / ".prove" / "flaresolverr" / "stdout.log")
    assert log.exists()
    # the proof process env, as the installer built it
    text = INSTALLER.read_text(encoding="utf-8")
    assert "HOST=127.0.0.1 PORT=\"$proofport\"" in text


def test_prove_refuses_at_seventy_percent_of_ceiling(box):
    box.installed()
    # 1340 + 60 = 1400 >= 0.70 * 2000
    r = box.run("--prove", "--execute", env={"FAKE_TASKS": "1340"})
    assert r.returncode == 1 and "70%" in r.stderr
    assert not (box.swap / "flaresolverr" / "proof.json").exists()
    assert not (box.apps / ".prove" / "flaresolverr").exists()


def test_prove_blocks_when_the_delta_is_too_big(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_FS_CHILD": "100"})   # 40 + 100 > 80
    assert r.returncode == 3 and "BLOCKED" in r.stderr and "delta" in r.stderr
    assert not (box.swap / "flaresolverr" / "proof.json").exists()


def test_prove_blocks_when_the_build_cannot_run_without_xvfb(box):
    box.installed()
    # The unit PATH has no Xvfb on this slot; a build that needs it never answers.
    r = box.run("--prove", "--execute",
                env={"FAKE_NEEDS_XVFB": "1", "QFLIX_PROOF_TIMEOUT_S": "3",
                     "PATH": os.environ["PATH"]})
    if shutil.which("Xvfb"):
        pytest.skip("a real Xvfb is on this machine's PATH")
    assert r.returncode == 3 and "BLOCKED" in r.stderr
    assert not (box.swap / "flaresolverr" / "proof.json").exists()


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


@pytest.mark.parametrize("name", ["manitoba-maint-flaresolverr-unsuppress.timer",
                                  "manitoba-maint-flaresolverr-unsuppress.service"])
def test_swap_refuses_while_the_unsuppress_watcher_units_exist(box, name):
    box.proved()
    (box.unitdir / name).write_text("[Unit]\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "unsuppress watcher" in r.stderr
    assert box.container_running() and not box.suppressed()
    assert "appctl stop" not in box.calls_text()


def test_swap_full_sequence(box):
    r = box.swapped()
    calls = box.calls_text()
    assert calls.index("appctl stop flaresolverr") < calls.index(f"systemctl --user enable --now {UNIT}")
    assert not box.container_running() and box.native_running()
    assert (box.unitdir / UNIT).read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    # the listen set is captured and is EXACTLY the bridge gateway + port
    assert (box.swap / "flaresolverr" / "listen-set.before").read_text() == f"172.17.0.1:{PORT}\n"
    st = box.swapstate()
    assert st["ucc_version"] == "3.5.2" and st["port"] == int(PORT)
    assert st["swap_date"] and st["soak_until"] and st["rollback_window"] == "open"
    # app + every dependent canary muted together, and STILL muted (pending-swap)
    assert set(box.suppressed()) == SUPPRESSED
    assert "elapsed=" in r.stdout
    # suppression happens before the container is stopped
    assert not (box.swap / "flaresolverr" / "container-env.before").read_text().count("PROXY")


def test_swap_suppresses_before_stopping_the_container(box):
    box.proved()
    # Make the stop observe the registry: a stop that finds it empty fails the test.
    wrapper = box.stub / "appctl"
    orig = wrapper.read_text()
    wrapper.write_text(orig.replace(
        '  stop)', f'  stop) [ -s "{_posix(box.state)}/push-suppress.json" ] && '
                  f'grep -q canary-prowlarr-indexer-health "{_posix(box.state)}/push-suppress.json" '
                  f'|| echo "stop-before-suppress" >> "{_posix(box.calls)}";'), newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stderr
    assert "stop-before-suppress" not in box.calls_text()


def test_swap_refuses_a_listen_set_that_is_not_the_gateway_only(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_SS_EXTRA": f"LISTEN 0 4096 0.0.0.0:{PORT} 0.0.0.0:*"})
    assert r.returncode != 0 and "listen set" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_refuses_when_the_env_file_binds_something_else(box):
    box.proved()
    env = box.envdir / "flaresolverr.env"
    env.write_text(env.read_text().replace("HOST=172.17.0.1", "HOST=0.0.0.0"), newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "does not bind exactly" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_aborts_when_container_never_exits_and_restores_service(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_CONTAINER_STICKS": "1"})
    assert r.returncode != 0 and "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert not box.native_running() and box.container_running()
    assert not box.suppressed()


def test_swap_parity_failure_is_reported_and_stays_suppressed(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_NATIVE_FAILS": "1"})
    assert r.returncode != 0 and "--rollback" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED


def test_swap_fails_parity_when_the_ready_probe_never_answers(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_PROBE_FAILS": "1"}, timeout=240)
    assert r.returncode != 0 and "FlareSolverr is ready" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED


def test_swap_is_resumable_when_already_swapped(box):
    box.swapped()
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stderr
    assert "already" in r.stdout
    assert box.calls_text().count("appctl stop flaresolverr") == 1


# --- step 9: finish -----------------------------------------------------------------

def test_finish_refuses_while_manifest_still_pending_swap(box):
    box.swapped()
    r = box.run("--finish", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED


def test_finish_lifts_app_and_canaries_together(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--finish", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 0, r.stderr
    assert box.suppressed() == {}


# --- rollback (0-5) -----------------------------------------------------------------

def test_rollback_step0_suppresses_and_masks_before_stopping(box):
    box.swapped()
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls_text()
    assert calls.index(f"systemctl --user mask {UNIT}") < calls.index(f"systemctl --user stop {UNIT}")
    assert _masked(box.unitdir / UNIT)
    assert not box.native_running() and box.container_running()
    assert calls.rindex("appctl start flaresolverr") > calls.index(f"systemctl --user stop {UNIT}")
    assert box.suppressed() == {}
    assert "elapsed=" in r.stdout


def test_rollback_pauses_until_manifest_reverted(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--rollback", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 10 and "revert" in r.stderr
    assert not box.native_running() and not box.container_running()
    assert "appctl start" not in box.calls_text()
    assert set(box.suppressed()) == SUPPRESSED            # stays muted while paused
    box.set_manifest(swap_state="pending-swap")
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running()


def test_rollback_stays_suppressed_if_the_container_is_not_ready(box):
    box.swapped()
    r = box.run("--rollback", "--execute", env={"FAKE_PROBE_FAILS": "1"}, timeout=240)
    assert r.returncode != 0 and "not ready" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED


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
