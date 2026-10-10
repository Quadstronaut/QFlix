"""scripts/configure/302-native-bazarr-install.sh (QFLX-27, A3 bazarr, spec 5.9).

Subprocess tests against fakes that MODEL the box (see the pilot's
test_native_unpackerr_install.py): a fake /proc (container pid in a docker
cgroup, native pid in the qflix-bazarr.service cgroup), fake appctl / systemctl /
ss / ps / fuser / curl / python3.11 / hostpolicy that mutate that tree the way
the real tools would, so "the container exited" and "the unit is active" are
STATE the installer has to observe, not an exit status it can trust.

New against the pilot: a REAL sqlite bazarr.db (VACUUM INTO + sanitize run for
real), a fake bazarr.py that serves /bazarr/api/system/status from
BAZARR_VERSION on the first BAZARR_LISTEN address (proof boot), and an HTTP
status server inside the test process standing in for the live listener.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "configure" / "302-native-bazarr-install.sh"
GOLDEN_UNIT = REPO / "scripts" / "maint" / "systemd" / "qflix-bazarr.service"
UNIT = "qflix-bazarr.service"
APIKEY = "k" * 32
LISTEN_ROWS = ["10.9.8.7", "172.17.0.1", "127.0.0.1"]   # three addresses, F-17

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _posix(p) -> str:
    return Path(p).as_posix()


def _masked(p: Path) -> bool:
    f = _posix(p)
    return subprocess.run(["bash", "-c", f'[ -L "{f}" ] || {{ [ -f "{f}" ] && [ ! -s "{f}" ]; }}'],
                          capture_output=True).returncode == 0


def _uid() -> str:
    return subprocess.run(["bash", "-c", "id -u"], capture_output=True, text=True).stdout.strip()


# The two shapes of upstream's server.py that matter (1.6.2, verbatim excerpt).
SERVER_PY = '''# coding=utf-8
from waitress.server import create_server
app = create_app()


class Server:
    def __init__(self):
        self.address = str(settings.general.ip)
        self.port = int(args.port) if args.port else int(settings.general.port)

    def configure_server(self):
        try:
            self.server = create_server(app,
                                        host=self.address,
                                        port=self.port,
                                        threads=100)
            self.connected = True
        except OSError as error:
            pass
'''

# The fake bazarr: serves the status endpoint on the first BAZARR_LISTEN address
# with the version from BAZARR_VERSION (KeyError if the env hook is missing).
FAKE_BAZARR = r'''
import atexit, json, os, signal, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
args = sys.argv[1:]
cfgdir = args[args.index("--config") + 1]
listen = os.environ["BAZARR_LISTEN"].split()[0]
host, port = listen.rsplit(":", 1)
version = os.environ.get("FAKE_REPORT_VERSION") or os.environ["BAZARR_VERSION"]
key = ""
sec = None
for line in open(os.path.join(cfgdir, "config", "config.yaml"), encoding="utf-8"):
    if line.startswith("auth:"): sec = "auth"; continue
    if line and line[0] not in " \t": sec = None
    if sec == "auth" and line.strip().startswith("apikey:"): key = line.split(":", 1)[1].strip()
marker = os.path.join(os.environ["QFLIX_PROC"], "proof.alive")
open(marker, "w").close()
# Real bazarr.py is a SUPERVISOR: it spawns bazarr/main.py and does not take it
# down on SIGTERM (box 2026-10-10: the first real --prove orphaned main.py).
# Model that: a child carrying the same --config that only dies on its own TERM.
import subprocess
CHILD = chr(10).join([
    "import os, signal, sys, time",
    "m = os.path.join(os.environ['QFLIX_PROC'], 'proof-child.alive')",
    "open(m, 'w').close()",
    "def bye(*a):",
    "    try: os.remove(m)",
    "    except OSError: pass",
    "    os._exit(0)",
    "signal.signal(signal.SIGTERM, bye)",
    "time.sleep(60); bye()",
])
def bye(*a):
    try: os.remove(marker)
    except OSError: pass
    os._exit(0)
signal.signal(signal.SIGTERM, bye)
atexit.register(bye)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path != "/bazarr/api/system/status" or self.headers.get("X-API-KEY") != key:
            self.send_response(401); self.end_headers(); return
        b = json.dumps({"data": {"bazarr_version": version}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)
srv = HTTPServer((host, int(port)), H)
subprocess.Popen([sys.executable, "-c", CHILD, "--no-update", "--config", cfgdir])
srv.timeout = 1
import time
deadline = time.time() + 60          # watchdog: never outlive a test run (Git Bash cannot kill us)
while time.time() < deadline:
    srv.handle_request()
bye()
'''

CONFIG_YAML = """---
analytics:
  enabled: true
auth:
  apikey: {key}
  type: form
backup:
  day: 0
  folder: /config/backup
  frequency: Weekly
general:
  auto_update: false
  base_url: /bazarr
  enabled_providers:
  - yifysubtitles
  - podnapisi
  ip: '*'
  path_mappings: []
  path_mappings_movie: []
  port: 6767
  use_plex: true
  use_radarr: true
  use_sonarr: true
plex:
  server_url: https://169-1-2-3.abc.plex.direct:17025
sonarr:
  base_url: /sonarr
  ip: example.invalid
  port: 443
"""


class Box:
    """A fake slot. Paths are POSIX strings for bash."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.apps = self.home / ".apps"
        self.appdir = self.apps / "bazarr"
        self.unitdir = self.home / ".config" / "systemd" / "user"
        self.envdir = self.home / ".config" / "qflix"
        self.state = self.home / ".opt" / "maint"
        self.swap = self.state / "swap"
        self.secrets = self.home / "secrets"
        self.proc = tmp / "proc"
        self.stub = tmp / "stub"
        self.calls = tmp / "calls.log"
        self.manifest = self.state / "apps.yaml"
        for d in (self.appdir / "config", self.appdir / "db", self.appdir / "backup", self.unitdir,
                  self.envdir, self.swap, self.secrets, self.proc, self.stub):
            d.mkdir(parents=True, exist_ok=True)
        self.cfg = self.appdir / "config" / "config.yaml"
        self.cfg.write_text(CONFIG_YAML.format(key=APIKEY), newline="\n")
        self._db()
        self.uid = _uid()
        self.api_version = "1.6.2"
        self.port = self._free_port()
        (self.secrets / "bazarr.port").write_text(str(self.port))
        self.ss_file = tmp / "ss.txt"
        self.ss_file.write_text("".join(
            f"LISTEN 0 65535 {a}:{self.port} 0.0.0.0:*\n" for a in LISTEN_ROWS) +
            "LISTEN 0 4096 127.0.0.1:9999 0.0.0.0:*\n", newline="\n")
        self.set_manifest(swap_state="pending-swap")
        self.container_up()
        self._zip()
        self._stubs()
        self._server = None

    # --- state ---------------------------------------------------------------
    @staticmethod
    def _free_port() -> int:
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def _db(self):
        con = sqlite3.connect(self.appdir / "db" / "bazarr.db")
        con.execute("CREATE TABLE table_settings_notifier (name TEXT, enabled INTEGER, url TEXT)")
        con.executemany("INSERT INTO table_settings_notifier VALUES (?,?,?)",
                        [("discord", 1, "x"), ("telegram", 1, "y")])
        con.execute("CREATE TABLE table_shows (id INTEGER)")
        con.execute("INSERT INTO table_shows VALUES (1)")
        con.commit()
        con.close()

    def start_status_server(self):
        """Stands in for the live listener (container or native) on self.port."""
        box = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path != "/bazarr/api/system/status" or self.headers.get("X-API-KEY") != APIKEY:
                    self.send_response(401); self.end_headers(); return
                b = json.dumps({"data": {"bazarr_version": box.api_version}}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(b)))
                self.end_headers(); self.wfile.write(b)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop_status_server(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def _proc(self, pid: int, cgroup: str, cmd: str):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(f"Name:\tx\nUid:\t{self.uid}\t{self.uid}\n", newline="\n")
        (d / "cgroup").write_text(cgroup + "\n", newline="\n")
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")

    def container_up(self):
        self._proc(9001, "0::/system.slice/docker-abc.scope",
                   "python3 /app/bazarr/bin/bazarr.py --no-update --config /config")

    def container_running(self) -> bool:
        return (self.proc / "9001").exists()

    def native_running(self) -> bool:
        return (self.proc / "9002").exists()

    def set_manifest(self, *, cls="systemd", swap_state=None, dormant=True):
        lines = ["apps:", "  bazarr:", f"    class: {cls}", "    ucc_slug: bazarr"]
        if cls == "systemd":
            lines.append(f"    unit: {UNIT}")
        if dormant:
            lines.append("    ucc_dormant: true")
        if swap_state:
            lines.append(f"    swap_state: {swap_state}")
        self.manifest.write_text("\n".join(lines) + "\n", newline="\n")

    def calls_text(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""

    def suppressed(self) -> dict:
        p = self.state / "push-suppress.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def swapstate(self) -> dict:
        p = self.swap / "bazarr" / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def backup_folder(self) -> str:
        return yaml.safe_load(self.cfg.read_text())["backup"]["folder"]

    # --- fakes ---------------------------------------------------------------
    def _zip(self, version="1.6.2"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("bazarr.py", FAKE_BAZARR)
            z.writestr("requirements.txt", "waitress\n")
            z.writestr("VERSION", f"v{version}\n")
            z.writestr("bazarr/app/server.py", SERVER_PY)
            z.writestr("frontend/build/index.html", "<html></html>")
        self.payload = self.tmp / "payload.zip"
        self.payload.write_bytes(buf.getvalue())
        self.sha = hashlib.sha256(buf.getvalue()).hexdigest()

    def _w(self, name: str, body: str):
        p = self.stub / name
        p.write_text("#!/usr/bin/env bash\n" + body, newline="\n")
        p.chmod(0o755)

    def _stubs(self):
        P, C = _posix(self.proc), _posix(self.calls)
        REALPY = _posix(sys.executable)
        self._w("appctl", f'''echo "appctl $*" >> "{C}"
case "$1" in
  version) echo '{{"data": {{"version": "'"${{FAKE_UCC_VERSION:-1.6.2}}"'"}}, "result": true}}' ;;
  stop) [ "${{FAKE_CONTAINER_STICKS:-0}}" = 1 ] || rm -rf "{P}/9001" ;;
  start) mkdir -p "{P}/9001"
         printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9001/status"
         echo "0::/system.slice/docker-abc.scope" > "{P}/9001/cgroup"
         printf 'python3\\0/app/bazarr/bin/bazarr.py\\0' > "{P}/9001/cmdline" ;;
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
            echo "ENV: $(tr '\\n' ' ' < "{_posix(self.envdir)}/bazarr.env")" >> "{C}"
            [ "${{FAKE_NATIVE_FAILS:-0}}" = 1 ] && exit 0
            mkdir -p "{P}/9002"
            printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9002/status"
            echo "0::/user.slice/user-1.slice/app.slice/$3" > "{P}/9002/cgroup"
            printf '/h/.apps/bazarr/venv/bin/python\\0/h/.apps/bazarr/bin/current/bazarr.py\\0' > "{P}/9002/cmdline"
          fi ;;
  stop) rm -rf "{P}/9002" ;;
  mask) [ -s "$U/$2" ] && [ ! -L "$U/$2" ] && {{ echo "Failed to mask unit: File $U/$2 already exists." >&2; exit 1; }}
        ln -sf /dev/null "$U/$2" ;;
  unmask) {{ [ -L "$U/$2" ] || [ ! -s "$U/$2" ]; }} && rm -f "$U/$2" ;;
esac
exit 0
''')
        # listeners belong to whoever is up: the container (sport query) or either
        self._w("ss", f'''rows="${{FAKE_SS_FILE:-/dev/null}}"
case "$*" in
  *sport*) [ -d "{P}/9001" ] && cat "$rows" ;;
  *) if [ -d "{P}/9001" ]; then cat "$rows"
     elif [ -d "{P}/9002" ]; then
       if [ "${{FAKE_NATIVE_LISTEN_DIFF:-0}}" = 1 ]; then head -n 1 "$rows"; else cat "$rows"; fi
     fi ;;
esac
exit 0
''')
        self._w("ps", f'''n=${{FAKE_TASKS:-1000}}; [ -f "{P}/proof.alive" ] && n=$((n + ${{FAKE_PROOF_TASKS:-25}}))
for i in $(seq 1 "$n"); do echo x; done
''')
        self._w("fuser", 'exit "${FAKE_DB_BUSY_RC:-1}"\n')   # 0 = somebody holds the db
        self._w("curl", 'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                        f'cp "{_posix(self.payload)}" "$out"\n')
        self._w("py311", f'''echo "py311 $*" >> "{C}"
if [ "$1" = -m ] && [ "$2" = venv ]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<'EOS'
#!/usr/bin/env bash
if [ "$1" = -m ]; then echo "venvpy $*" >> "{C}"; exit 0; fi
exec "{REALPY}" "$@"
EOS
  chmod +x "$3/bin/python"
fi
''')
        self._w("hostpolicy", f'''echo "hostpolicy $*" >> "{C}"
case "$1" in
  preflight) [ -n "${{FAKE_PROFILE-ultra}}" ] || exit 2; echo "${{FAKE_PROFILE-ultra}}" ;;
  in-window) exit "${{FAKE_INWINDOW_RC:-1}}" ;;
  task-ceiling) echo "${{FAKE_CEILING:-2000}}" ;;
esac
''')

    # --- run -----------------------------------------------------------------
    def run(self, *args, env=None, timeout=180):
        marker = self.tmp / "host.id"
        marker.write_text("test-slot\n")
        e = dict(os.environ,
                 HOME=_posix(self.home), USERPROFILE=str(self.home),
                 QFLIX_HOST_ID_FILE=_posix(marker),
                 QFLIX_APPS_DIR=_posix(self.apps), QFLIX_UNIT_DIR=_posix(self.unitdir),
                 QFLIX_ENV_DIR=_posix(self.envdir), QFLIX_SWAP_DIR=_posix(self.swap),
                 QFLIX_SECRETS_DIR=_posix(self.secrets), MANITOBA_STATE_DIR=_posix(self.state),
                 QFLIX_MANIFEST=_posix(self.manifest), QFLIX_PROC=_posix(self.proc),
                 QFLIX_PYTHON=_posix(sys.executable), QFLIX_PY311=_posix(self.stub / "py311"),
                 QFLIX_APPCTL=_posix(self.stub / "appctl"),
                 QFLIX_SYSTEMCTL=_posix(self.stub / "systemctl"),
                 QFLIX_SS=_posix(self.stub / "ss"), QFLIX_PS=_posix(self.stub / "ps"),
                 QFLIX_FUSER=_posix(self.stub / "fuser"), QFLIX_CURL=_posix(self.stub / "curl"),
                 QFLIX_HOSTPOLICY=_posix(self.stub / "hostpolicy"),
                 QFLIX_BAZARR_SHA256=self.sha, QFLIX_BAZARR_PORT=str(self.port),
                 FAKE_SS_FILE=_posix(self.ss_file),
                 QFLIX_POLL_S="0.2", QFLIX_SETTLE_S="0.3", QFLIX_STOP_TIMEOUT_S="3",
                 QFLIX_PROOF_TIMEOUT_S="20", QFLIX_STATUS_TIMEOUT_S="5")
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


@pytest.fixture()
def box(tmp_path):
    b = Box(tmp_path)
    b.start_status_server()
    yield b
    b.stop_status_server()


# --- static -------------------------------------------------------------------

def test_bash_syntax():
    r = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_lf_only():
    assert b"\r" not in INSTALLER.read_bytes()
    assert b"\r" not in GOLDEN_UNIT.read_bytes()


def test_pins_exact_version_and_sha256_matching_versions_env():
    text = INSTALLER.read_text(encoding="utf-8")
    ver = next(l.split("=", 1)[1].strip() for l in
               (REPO / "versions.env").read_text(encoding="utf-8").splitlines()
               if l.startswith("BAZARR_VERSION="))
    assert f'VERSION="{ver}"' in text
    assert re.search(r'^SHA256="[0-9a-f]{64}"$', text, re.M)
    assert f"download/v${{VERSION}}/bazarr.zip" in text


def test_240_stages_and_deploys_the_installer_and_sanitizer():
    text = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    assert "    scripts/configure/302-native-bazarr-install.sh \\\n" in text
    assert "    scripts/maint/native_sanitize.py \\\n" in text
    assert ('cp -f   "$STG"/scripts/configure/302-native-bazarr-install.sh '
            '~/scripts/configure/302-native-bazarr-install.sh\n'
            "chmod +x ~/scripts/configure/302-native-bazarr-install.sh") in text
    assert ('cp -f   "$STG"/scripts/maint/native_sanitize.py ~/scripts/maint/native_sanitize.py') in text


def test_installer_never_calls_the_panel_tool_or_the_gateway_directly():
    code = [l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith("#")]
    assert not any("app-bazarr" in l for l in code)
    assert not any("172.17.0.1" in l for l in code)


def test_manifest_flip_for_bazarr():
    a = yaml.safe_load((REPO / "manifest" / "apps.yaml").read_text(encoding="utf-8"))["apps"]["bazarr"]
    assert a["class"] == "systemd" and a["unit"] == UNIT and a["ucc_dormant"] is True
    assert "swap_state" not in a          # swapped 2026-10-10; the pending-swap hold is gone
    assert a["health"]["require_unit_active"] is True
    up = a["upgrade"]
    assert up["kind"] == "zip_swap" and "{version}" in up["target_dir"]
    assert any("302-native-bazarr-install.sh --post-upgrade {version} --execute" in s
               for s in up["post_steps"])
    assert up["version_pin"] == {"source": "versions.env", "key": "BAZARR_VERSION"}


def test_golden_unit_is_what_the_installer_renders(box):
    box.installed()
    staged = box.appdir / "native" / UNIT
    assert staged.read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    unit = GOLDEN_UNIT.read_text(encoding="utf-8")
    assert ("ExecStart=%h/.apps/bazarr/venv/bin/python %h/.apps/bazarr/bin/current/bazarr.py "
            "--no-update --config %h/.apps/bazarr\n") in unit
    assert "EnvironmentFile=%h/.config/qflix/bazarr.env" in unit
    assert "Environment=PATH=%h/.apps/bazarr/bin/current:" in unit
    assert "TasksMax" not in unit and "--port" not in unit


# --- inert by default -----------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["--install"], ["--prove"], ["--swap"], ["--finish"],
                                  ["--rollback"], ["--post-upgrade", "1.6.3"]])
def test_without_execute_nothing_is_touched(box, args):
    before = sorted(p.as_posix() for p in box.tmp.rglob("*"))
    r = box.run(*args)
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    after = sorted(p.as_posix() for p in box.tmp.rglob("*") if p.name != "host.id")
    assert after == before
    assert "appctl" not in box.calls_text() and "systemctl" not in box.calls_text()


def test_unknown_flag_and_bare_post_upgrade_are_usage_errors(box):
    assert box.run("--frobnicate").returncode == 64
    assert box.run("--post-upgrade").returncode == 64


def test_monday_window_refuses_execute(box):
    r = box.run("--install", "--execute", env={"FAKE_INWINDOW_RC": "0"})
    assert r.returncode != 0 and "window" in r.stderr.lower()
    assert not (box.appdir / "bin").exists()


def test_missing_host_profile_fails_closed(box):
    r = box.run("--install", "--execute", env={"FAKE_PROFILE": ""})
    assert r.returncode != 0
    assert not (box.appdir / "bin").exists()


def test_swap_refuses_on_a_generic_host(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_PROFILE": "generic"})
    assert r.returncode != 0 and "profile" in r.stderr
    assert box.container_running()


# --- step 1: pin + install --------------------------------------------------------

def test_install_lays_out_release_venv_env_and_stages_unit_without_enabling(box):
    box.installed()
    cur = box.appdir / "bin" / "current"
    assert (box.appdir / "bin" / "1.6.2" / "bazarr.py").exists()
    assert (cur / "bazarr.py").exists()
    assert (cur / "frontend" / "build" / "index.html").exists()      # prebuilt UI shipped
    assert (box.appdir / "venv" / "bin" / "python").exists()
    assert "venvpy -m pip install --quiet -r" in box.calls_text()
    env = (box.envdir / "bazarr.env").read_text().splitlines()
    assert "BAZARR_VERSION=1.6.2" in env                              # O-7 hook
    assert "MALLOC_ARENA_MAX=2" in env and "TZ=Europe/Amsterdam" in env
    assert not any(l.startswith("BAZARR_LISTEN") for l in env)        # captured at swap
    assert (box.appdir / "native" / UNIT).exists()
    assert not (box.unitdir / UNIT).exists()
    assert "enable" not in box.calls_text()
    assert box.container_running()


def test_install_patches_server_py_threads_and_bind(box):
    box.installed()
    src = (box.appdir / "bin" / "1.6.2" / "bazarr" / "app" / "server.py").read_text()
    assert "threads=100" not in src and "threads=4" in src
    assert "_qflix_bind(self.address, self.port)" in src
    assert "BAZARR_LISTEN is not set; refusing to bind" in src
    compile(src, "server.py", "exec")                                  # still valid python


def test_patch_is_idempotent_and_fails_closed_on_a_changed_upstream(box):
    box.installed()
    if os.name != "nt":   # Git Bash copies `current`, so the second atomic flip hits a real dir
        assert box.run("--install", "--execute").returncode == 0       # second run
    src = (box.appdir / "bin" / "1.6.2" / "bazarr" / "app" / "server.py").read_text()
    assert src.count("def _qflix_bind") == 1
    # upstream reshapes the call: the patch must refuse, not guess
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("bazarr.py", FAKE_BAZARR)
        zf.writestr("requirements.txt", "waitress\n")
        zf.writestr("bazarr/app/server.py", SERVER_PY.replace("threads=100", "threads=64"))
    box.payload.write_bytes(z.getvalue())
    shutil.rmtree(box.appdir / "bin")
    r = box.run("--install", "--execute",
                env={"QFLIX_BAZARR_SHA256": hashlib.sha256(z.getvalue()).hexdigest()})
    assert r.returncode != 0 and "upstream shape changed" in r.stderr


def test_install_refuses_version_mismatch(box):
    r = box.run("--install", "--execute", env={"FAKE_UCC_VERSION": "1.6.1"})
    assert r.returncode != 0
    assert not (box.appdir / "bin" / "1.6.2").exists()


def test_install_refuses_sha_mismatch(box):
    r = box.run("--install", "--execute", env={"QFLIX_BAZARR_SHA256": "0" * 64})
    assert r.returncode != 0 and "sha256" in r.stderr
    assert not (box.appdir / "bin").exists()


# --- step 2: proof -------------------------------------------------------------------

def _tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and p.parent.name in ("config", "db")}


def test_prove_boots_a_sanitized_copy_measures_tasks_and_cleans_up(box):
    live_before = _tree_hash(box.appdir)
    r = box.proved()
    assert "delta=25" in r.stdout and "version 1.6.2" in r.stdout
    assert not (box.apps / ".prove" / "bazarr").exists()
    proof = json.loads((box.swap / "bazarr" / "proof.json").read_text())
    assert proof["delta"] == 25 and proof["ceiling"] == 2000 and proof["ok"] is True
    assert box.container_running()
    assert _tree_hash(box.appdir) == live_before                       # live data untouched
    if os.name != "nt":      # Git Bash cannot SIGTERM a Windows python; the fake self-exits
        assert not (box.proc / "proof.alive").exists()                 # proof process stopped
        # ...and its child: a TERM to the supervisor alone orphans it (box 2026-10-10)
        assert not (box.proc / "proof-child.alive").exists()


def test_prove_copy_is_inert_and_off_container_paths(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"QFLIX_KEEP_PROOF": "1"})
    assert r.returncode == 0, r.stderr
    pv = box.apps / ".prove" / "bazarr"
    gen = yaml.safe_load((pv / "config" / "config.yaml").read_text())
    assert gen["general"]["enabled_providers"] == []
    assert gen["general"]["use_sonarr"] is False and gen["general"]["use_radarr"] is False
    assert gen["general"]["use_plex"] is False
    assert gen["analytics"]["enabled"] is False
    assert gen["backup"]["folder"] == (pv / "backup").as_posix()
    con = sqlite3.connect(pv / "db" / "bazarr.db")
    assert con.execute("SELECT count(*) FROM table_settings_notifier WHERE enabled!=0").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM table_shows").fetchone()[0] == 1   # data really copied
    con.close()
    live = sqlite3.connect(box.appdir / "db" / "bazarr.db")
    assert live.execute("SELECT count(*) FROM table_settings_notifier WHERE enabled!=0").fetchone()[0] == 2
    live.close()


def test_prove_refuses_at_seventy_percent_of_ceiling(box):
    box.installed()
    # 1376 + 25 = 1401 >= 0.70 * 2000
    r = box.run("--prove", "--execute", env={"FAKE_TASKS": "1376"})
    assert r.returncode != 0 and "70%" in r.stderr
    assert not (box.swap / "bazarr" / "proof.json").exists()
    assert not (box.apps / ".prove" / "bazarr").exists()


def test_prove_refuses_a_version_the_status_endpoint_does_not_report(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_REPORT_VERSION": "1.5.5"})
    assert r.returncode != 0 and "1.5.5" in r.stderr
    assert not (box.swap / "bazarr" / "proof.json").exists()


def test_prove_refuses_when_the_env_lacks_the_version_hook(box):
    box.installed()
    env = box.envdir / "bazarr.env"
    env.write_text(env.read_text().replace("BAZARR_VERSION=1.6.2\n", ""), newline="\n")
    r = box.run("--prove", "--execute")
    assert r.returncode != 0 and "BAZARR_VERSION" in r.stderr


def test_prove_refuses_when_sanitize_cannot_prove_inert(box):
    box.installed()
    (box.appdir / "db" / "bazarr.db").write_bytes(b"not a database")
    r = box.run("--prove", "--execute")
    assert r.returncode != 0
    assert not (box.swap / "bazarr" / "proof.json").exists()


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
    assert calls.index("appctl stop bazarr") < calls.index(f"systemctl --user enable --now {UNIT}")
    assert not box.container_running() and box.native_running()
    assert (box.unitdir / UNIT).read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    # all three listeners recorded and reproduced through BAZARR_LISTEN
    rec = (box.swap / "bazarr" / "listen-set.before").read_text().split()
    assert sorted(rec) == sorted(f"{a}:{box.port}" for a in LISTEN_ROWS)
    env = (box.envdir / "bazarr.env").read_text().splitlines()
    listen = next(l for l in env if l.startswith("BAZARR_LISTEN=")).split("=", 1)[1].split()
    assert sorted(listen) == sorted(rec)
    assert "BAZARR_VERSION=1.6.2" in env
    assert "BAZARR_LISTEN=" in next(l for l in calls.splitlines() if l.startswith("ENV:"))
    st = box.swapstate()
    assert st["ucc_version"] == "1.6.2"
    assert st["swap_date"] and st["soak_until"] and st["rollback_window"] == "open"
    assert set(box.suppressed()) == {"bazarr", "canary-bazarr-ingest", "canary-thread-ceiling",
                                     "bazarr2-sync"}
    assert "elapsed=" in r.stdout


def test_swap_rewrites_only_backup_folder_and_snapshots_the_original(box):
    before = box.cfg.read_text()
    box.swapped()
    after = box.cfg.read_text()
    assert box.backup_folder() == (box.appdir / "backup").as_posix()
    diff = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert diff == [("  folder: /config/backup", f"  folder: {(box.appdir / 'backup').as_posix()}")]
    assert (box.swap / "bazarr" / "backup-folder.orig").read_text() == "/config/backup"
    snaps = list((box.swap / "bazarr").glob("snapshot-*.tgz"))
    assert len(snaps) == 1
    with tarfile.open(snaps[0]) as tf:
        names = tf.getnames()
        assert "bazarr/db/bazarr.db" in names and "bazarr/config/config.yaml" in names
        assert not any(n.startswith("bazarr/bin") or n.startswith("bazarr/venv") for n in names)
        assert b"folder: /config/backup" in tf.extractfile("bazarr/config/config.yaml").read()


@pytest.mark.parametrize("rows", [
    "",                                                         # nothing listening
    "LISTEN 0 65535 0.0.0.0:{p} 0.0.0.0:*\n",                  # wildcard: never an option
    "LISTEN 0 65535 *:{p} 0.0.0.0:*\n",
])
def test_swap_refuses_an_empty_or_wildcard_listen_set(box, rows):
    box.proved()
    box.ss_file.write_text(rows.format(p=box.port), newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "listen" in r.stderr
    assert box.container_running() and not box.suppressed()
    assert box.backup_folder() == "/config/backup"


def test_swap_refuses_container_paths_other_than_backup_folder(box):
    box.proved()
    box.cfg.write_text(box.cfg.read_text().replace(
        "path_mappings_movie: []", "path_mappings_movie:\n  - path_bazarr: /downloads/movies"),
        newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "container path" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_refuses_a_port_secret_that_disagrees(box):
    box.proved()
    (box.secrets / "bazarr.port").write_text("1")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "bazarr.port" in r.stderr
    assert box.container_running()


def test_swap_aborts_when_container_never_exits_and_restores_service(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_CONTAINER_STICKS": "1"})
    assert r.returncode != 0 and "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert not box.native_running() and box.container_running()
    assert not box.suppressed()
    assert box.backup_folder() == "/config/backup"


def test_swap_aborts_while_something_still_holds_the_database(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_DB_BUSY_RC": "0"})
    assert r.returncode != 0 and "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert box.backup_folder() == "/config/backup" and not box.suppressed()


def test_swap_without_fuser_cannot_prove_the_db_idle(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"QFLIX_FUSER": "/nonexistent/fuser"})
    assert r.returncode != 0
    assert "enable --now" not in box.calls_text()


def test_swap_parity_failure_is_reported_and_stays_suppressed(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_NATIVE_FAILS": "1"})
    assert r.returncode != 0 and "--rollback" in r.stderr
    assert "bazarr" in box.suppressed()


def test_swap_listen_drift_fails_parity(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_NATIVE_LISTEN_DIFF": "1"})
    assert r.returncode != 0 and "listen set differs" in r.stderr
    assert "bazarr" in box.suppressed()


def test_swap_wrong_reported_version_fails_parity(box):
    box.proved()
    box.api_version = "1.6.1"
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "1.6.1" in r.stderr
    assert "bazarr" in box.suppressed()


def test_swap_is_resumable_when_already_swapped(box):
    box.swapped()
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stderr
    assert "already" in r.stdout
    assert box.calls_text().count("appctl stop bazarr") == 1


# --- step 9: finish -----------------------------------------------------------------

def test_finish_refuses_while_manifest_still_pending_swap(box):
    box.swapped()
    r = box.run("--finish", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert "bazarr" in box.suppressed()


def test_finish_lifts_app_and_canaries_together(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--finish", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 0, r.stderr
    assert box.suppressed() == {}


# --- rollback (0-5) -----------------------------------------------------------------------

def test_rollback_masks_before_stopping_restores_the_path_and_the_container(box):
    box.swapped()
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls_text()
    assert calls.index(f"systemctl --user mask {UNIT}") < calls.index(f"systemctl --user stop {UNIT}")
    assert _masked(box.unitdir / UNIT)
    assert not box.native_running() and box.container_running()
    assert calls.rindex("appctl start bazarr") > calls.index(f"systemctl --user stop {UNIT}")
    assert box.backup_folder() == "/config/backup"             # the container can write it again
    assert box.suppressed() == {}
    assert "elapsed=" in r.stdout


def test_rollback_keeps_an_operator_edited_backup_folder(box):
    box.swapped()
    cur = box.cfg.read_text()
    box.cfg.write_text(cur.replace(f"folder: {(box.appdir / 'backup').as_posix()}",
                                   "folder: /mnt/elsewhere"), newline="\n")
    assert box.run("--rollback", "--execute").returncode == 0
    assert box.backup_folder() == "/mnt/elsewhere"


def test_rollback_pauses_before_ucc_start_until_manifest_reverted(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--rollback", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 10 and "revert" in r.stderr
    assert not box.native_running() and not box.container_running()
    assert "appctl start" not in box.calls_text()
    assert "bazarr" in box.suppressed()
    assert box.backup_folder() == "/config/backup"             # already safe for the container
    box.set_manifest(swap_state="pending-swap")
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running()


def test_drill_rollback_then_reswap_unmasks_and_rewrites_again(box):
    box.swapped()
    assert box.run("--rollback", "--execute").returncode == 0
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"systemctl --user unmask {UNIT}" in box.calls_text()
    assert not _masked(box.unitdir / UNIT)
    assert box.native_running() and not box.container_running()
    assert box.backup_folder() == (box.appdir / "backup").as_posix()


def test_rollback_with_nothing_swapped_is_harmless(box):
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running() and box.backup_folder() == "/config/backup"


# --- --post-upgrade (zip_swap post step) -----------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="Git Bash copies `current` instead of linking, so "
                    "the atomic flip hits a real dir")
def test_post_upgrade_patches_flips_current_and_rewrites_the_version_env(box):
    box.swapped()
    new = box.appdir / "bin" / "1.6.3"                         # what lifecycle's unzip leaves
    shutil.copytree(box.appdir / "bin" / "1.6.2", new)
    (new / "bazarr" / "app" / "server.py").write_text(SERVER_PY)   # unpatched upstream
    # inside the Monday window: the upgrade sweep runs there, the gate must not block
    r = box.run("--post-upgrade", "1.6.3", "--execute", env={"FAKE_INWINDOW_RC": "0"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.readlink(box.appdir / "bin" / "current") == "1.6.3"
    assert "_qflix_bind" in (new / "bazarr" / "app" / "server.py").read_text()
    env = (box.envdir / "bazarr.env").read_text().splitlines()
    assert "BAZARR_VERSION=1.6.3" in env and "BAZARR_VERSION=1.6.2" not in env
    assert any(l.startswith("BAZARR_LISTEN=") for l in env)    # the recorded set survives
    assert "TZ=Europe/Amsterdam" in env


def test_post_upgrade_refuses_a_directory_that_is_not_a_release(box):
    box.installed()
    (box.appdir / "bin" / "9.9.9").mkdir()
    r = box.run("--post-upgrade", "9.9.9", "--execute")
    assert r.returncode != 0 and "not an extracted bazarr release" in r.stderr
    assert "BAZARR_VERSION=9.9.9" not in (box.envdir / "bazarr.env").read_text()


# --- the status/config helpers in isolation ---------------------------------------------------

def test_cfg_audit_flags_only_unexpected_container_paths(box):
    box.proved()
    cfg = box.cfg.read_text()
    box.cfg.write_text(cfg.replace("ip: example.invalid", "ip: example.invalid\n  data_dir: /data/x"),
                       newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "container path" in r.stderr
    # a URL that merely contains /config or /data is not a container path
    box.cfg.write_text(cfg.replace("ip: example.invalid", "ip: example.invalid/data"), newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
