"""scripts/configure/311-native-seerr-install.sh (QFLX-36, A12 seerr, spec 5.9, I-13).

Subprocess tests against fakes that MODEL the box (shape of the bazarr/unpackerr
suites): a fake /proc (the container's `npm start` + `node dist/index.js` in a
docker scope, the native node in the qflix-seerr.service cgroup), fake appctl /
systemctl / ss / ps / fuser / curl / hostpolicy that mutate that tree the way the
real tools would, so "the container exited", "the unit is active" and "the gate
ran report-only" are STATE the installer must observe.

Seerr-specific fakes:
  * a Node tarball whose bin/node runs fake_seerr.py: it serves /api/v1/status
    (package.json version of its CWD) and /api/v1/auth/me (the copy's API key) on
    the first SEERR_LISTEN address, and records argv / env / cwd for asserts;
  * a flat Seerr artifact tarball (dist/index.js, .next, node_modules,
    package.json, committag.json);
  * the entitlement gate: a real execute.conf drop-in file, members.yaml, and a
    fake `systemctl start manitoba-maint-entitlement.service` that appends the
    gate's own log line, `execute=True` while the drop-in exists and
    `execute=False` once it is parked (as qflix-entitlement.py does);
  * the movie / anime canary scripts (request smoke) and the panel vhost (curl).
A REAL sqlite db.sqlite3 is VACUUM'd INTO the proof copy and sanitized for real.
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "configure" / "311-native-seerr-install.sh"
GOLDEN_UNIT = REPO / "scripts" / "maint" / "systemd" / "qflix-seerr.service"
PRELOAD = REPO / "scripts" / "data" / "seerr-listen.cjs"
WORKFLOW = REPO / ".github" / "workflows" / "seerr-artifact.yml"
UNIT = "qflix-seerr.service"
GATE_UNIT = "manitoba-maint-entitlement.service"
APIKEY = "k" * 32
COMMIT = "e2f24cb46079746936516c723b09820360f95113"
LISTEN_ROWS = ["10.9.8.7", "172.17.0.1", "127.0.0.1"]   # three addresses, F-17
DROPIN = ("# Armed: override ExecStart to add --execute.\n[Service]\nExecStart=\n"
          "ExecStart=/usr/bin/python3 %h/scripts/maint/qflix-entitlement.py --execute\n")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _posix(p) -> str:
    return Path(p).as_posix()


def _masked(p: Path) -> bool:
    f = _posix(p)
    return subprocess.run(["bash", "-c", f'[ -L "{f}" ] || {{ [ -f "{f}" ] && [ ! -s "{f}" ]; }}'],
                          capture_output=True).returncode == 0


def _uid() -> str:
    return subprocess.run(["bash", "-c", "id -u"], capture_output=True, text=True).stdout.strip()


def _versions_env() -> dict:
    out = {}
    for line in (REPO / "versions.env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# fake_seerr.py: what `node --disable-wasm-trap-handler --require <preload> dist/index.js` does.
FAKE_SEERR = r'''
import json, os, signal, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
argv = sys.argv[1:]
procdir = os.environ["QFLIX_PROC"]
ok = "--disable-wasm-trap-handler" in argv and "--require" in argv \
    and os.path.isfile(argv[argv.index("--require") + 1]) and argv[-1].endswith("dist/index.js")
with open(os.path.join(procdir, "proof-env.json"), "w") as fh:
    json.dump({"argv": argv, "cwd": os.getcwd().replace("\\", "/"),
               "env": {k: os.environ.get(k) for k in ("CONFIG_DIRECTORY", "NODE_ENV", "TZ", "PORT",
                                                      "SEERR_LISTEN", "COMMIT_TAG", "NODE_OPTIONS",
                                                      "UV_THREADPOOL_SIZE")}}, fh)
if not ok:
    sys.exit(3)
host, port = os.environ["SEERR_LISTEN"].split()[0].rsplit(":", 1)
version = os.environ.get("FAKE_REPORT_VERSION") or json.load(open("package.json"))["version"]
key = json.load(open(os.path.join(os.environ["CONFIG_DIRECTORY"], "settings.json")))["main"]["apiKey"]
marker = os.path.join(procdir, "proof.alive")
open(marker, "w").close()
def bye(*a):
    try: os.remove(marker)
    except OSError: pass
    os._exit(0)
signal.signal(signal.SIGTERM, bye)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        if self.path == "/api/v1/status":
            return self._send(200, {"version": version, "commitTag": os.environ.get("COMMIT_TAG")})
        if self.path == "/api/v1/auth/me":
            if self.headers.get("X-Api-Key") == key and not os.environ.get("FAKE_AUTH_FAIL"):
                return self._send(200, {"id": 1})
            return self._send(403, {"message": "no"})
        self._send(404, {})
srv = HTTPServer((host, int(port)), H)
srv.timeout = 1
deadline = time.time() + 60          # watchdog: never outlive a test run
while time.time() < deadline:
    srv.handle_request()
bye()
'''

SETTINGS = {
    "clientId": "c",
    "main": {"apiKey": APIKEY, "applicationUrl": "https://seerr-slot.example.invalid",
             "localLogin": False},
    "applicationUrl": "/seerr",
    "radarr": [{"name": "Radarr", "hostname": "172.17.0.1", "port": 17027, "baseUrl": "/radarr",
                "activeDirectory": "/home/u/media/Movies"}],
    "sonarr": [{"name": "Sonarr", "hostname": "172.17.0.1", "port": 17026, "baseUrl": "/sonarr",
                "activeDirectory": "/home/u/media/TV Shows"}],
    "plex": {"ip": "172.17.0.1", "port": 17025, "libraries": [{"id": "1", "enabled": True}]},
    "tautulli": {"urlBase": "/tautulli"},
    "notifications": {"agents": {"discord": {"enabled": True}, "webpush": {"enabled": True},
                                 "email": {"enabled": False}}},
}


def _tgz(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, (body, mode) in files.items():
            data = body.encode() if isinstance(body, str) else body
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class Box:
    """A fake slot. Paths are POSIX strings for bash."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.apps = self.home / ".apps"
        self.appdir = self.apps / "seerr"
        self.unitdir = self.home / ".config" / "systemd" / "user"
        self.envdir = self.home / ".config" / "qflix"
        self.state = self.home / ".opt" / "maint"
        self.swap = self.state / "swap"
        self.gatelog = self.state / "entitlement"
        self.secrets = self.home / "secrets"
        self.canaries = tmp / "canaries"
        self.proc = tmp / "proc"
        self.stub = tmp / "stub"
        self.calls = tmp / "calls.log"
        self.manifest = self.state / "apps.yaml"
        self.dropin = self.unitdir / f"{GATE_UNIT}.d" / "execute.conf"
        for d in (self.appdir / "db", self.appdir / "logs", self.unitdir, self.envdir, self.swap,
                  self.gatelog, self.secrets, self.canaries, self.proc, self.stub,
                  self.dropin.parent):
            d.mkdir(parents=True, exist_ok=True)
        self.settings = self.appdir / "settings.json"
        self.settings.write_text(json.dumps(SETTINGS, indent=1), newline="\n")
        self._db()
        self.dropin.write_text(DROPIN, newline="\n")
        (self.secrets / "members.yaml").write_text("armed: true\ndefaults:\n  grace_days: 7\n",
                                                   newline="\n")
        self.uid = _uid()
        self.api_version = "3.5.0"
        self.auth_ok = True
        self.port = self._free_port()
        (self.secrets / "seerr.port").write_text(str(self.port))
        self.ss_file = tmp / "ss.txt"
        self.ss_file.write_text("".join(
            f"LISTEN 0 65535 {a}:{self.port} 0.0.0.0:*\n" for a in LISTEN_ROWS) +
            "LISTEN 0 4096 127.0.0.1:9999 0.0.0.0:*\n", newline="\n")
        self.set_manifest(swap_state="pending-swap")
        self.container_up()
        self._payloads()
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
        con = sqlite3.connect(self.appdir / "db" / "db.sqlite3")
        con.execute("CREATE TABLE user_settings (id INTEGER PRIMARY KEY, "
                    "watchlistSyncMovies BOOLEAN, watchlistSyncTv BOOLEAN)")
        con.executemany("INSERT INTO user_settings VALUES (?,?,?)", [(1, 1, 1), (2, 0, 1)])
        con.execute("CREATE TABLE media_request (id INTEGER PRIMARY KEY)")
        con.executemany("INSERT INTO media_request VALUES (?)", [(1,), (2,), (3,)])
        con.commit()
        con.close()

    def start_status_server(self):
        """Stands in for the live listener (container or native) on self.port."""
        box = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Length", str(len(b)))
                self.end_headers(); self.wfile.write(b)

            def do_GET(self):
                if self.path == "/api/v1/status":
                    return self._send(200, {"version": box.api_version})
                if self.path == "/api/v1/auth/me" and box.auth_ok \
                        and self.headers.get("X-Api-Key") == APIKEY:
                    return self._send(200, {"id": 1})
                self._send(403, {})

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
        self._proc(9000, "0::/system.slice/docker-abc.scope", "npm start")
        self._proc(9001, "0::/system.slice/docker-abc.scope", "node dist/index.js")

    def container_running(self) -> bool:
        return (self.proc / "9001").exists()

    def native_running(self) -> bool:
        return (self.proc / "9002").exists()

    def set_manifest(self, *, cls="systemd", swap_state=None, dormant=True):
        lines = ["apps:", "  seerr:", f"    class: {cls}", "    ucc_slug: seerr"]
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
        p = self.swap / "seerr" / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def gate_lines(self) -> list[str]:
        out = []
        for f in sorted(self.gatelog.glob("*.log")):
            out += re.findall(r"execute=(?:True|False)", f.read_text())
        return out

    def proof_env(self) -> dict:
        return json.loads((self.proc / "proof-env.json").read_text())

    # --- fakes ---------------------------------------------------------------
    def _payloads(self, *, node_ver="22.23.2"):
        realpy = _posix(sys.executable)
        node = (f'#!/usr/bin/env bash\nif [ "$1" = --version ]; then echo "v${{FAKE_NODE_VERSION:-{node_ver}}}"; exit 0; fi\n'
                f'echo "node $*" >> "{_posix(self.calls)}"\n'
                f'exec "{realpy}" "$(dirname "$0")/fake_seerr.py" "$@"\n')
        root = "node-v22.23.2-linux-x64"
        self.node_payload = self.tmp / "node.tgz"
        self.node_payload.write_bytes(_tgz({
            f"{root}/bin/node": (node, 0o755),
            f"{root}/bin/fake_seerr.py": (FAKE_SEERR, 0o644),
            f"{root}/LICENSE": ("MIT\n", 0o644),
        }))
        self.node_sha = hashlib.sha256(self.node_payload.read_bytes()).hexdigest()
        self.set_artifact()

    def set_artifact(self, *, version="3.5.0", commit=COMMIT, with_dist=True):
        files = {
            "./package.json": (json.dumps({"name": "seerr", "version": version,
                                           "engines": {"node": "^22.19.0"}}), 0o644),
            "./committag.json": (json.dumps({"commitTag": commit}), 0o644),
            "./.next/BUILD_ID": ("b1\n", 0o644),
            "./node_modules/express/index.js": ("module.exports = 1;\n", 0o644),
        }
        if with_dist:
            files["./dist/index.js"] = ("// seerr\n", 0o644)
        self.payload = self.tmp / "seerr.tgz"
        self.payload.write_bytes(_tgz(files))
        self.sha = hashlib.sha256(self.payload.read_bytes()).hexdigest()

    def _w(self, name: str, body: str, where=None):
        p = (where or self.stub) / name
        p.write_text("#!/usr/bin/env bash\n" + body, newline="\n")
        p.chmod(0o755)

    def _stubs(self):
        P, C = _posix(self.proc), _posix(self.calls)
        U, DR, GL = _posix(self.unitdir), _posix(self.dropin), _posix(self.gatelog)
        self._w("appctl", f'''echo "appctl $*" >> "{C}"
mk() {{ mkdir -p "{P}/$1"
  printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/$1/status"
  echo "0::/system.slice/docker-def.scope" > "{P}/$1/cgroup"
  printf "$2" > "{P}/$1/cmdline"; }}
case "$1" in
  version) echo '{{"data": {{"version": "'"${{FAKE_UCC_VERSION:-3.5.0}}"'"}}, "result": true}}' ;;
  stop) [ "${{FAKE_CONTAINER_STICKS:-0}}" = 1 ] || rm -rf "{P}/9000" "{P}/9001" ;;
  start) mk 9000 'npm\\0start\\0'; mk 9001 'node\\0dist/index.js\\0' ;;
  is-native) v="${{FAKE_ISNATIVE:-ucc}}"; echo "$v"; [ "$v" = native ] ;;
esac
''')
        self._w("systemctl", f'''echo "systemctl $*" >> "{C}"
[ "$1" = --user ] && shift
case "$1" in
  is-active)
    if [ "$2" = "{GATE_UNIT}" ]; then [ "${{FAKE_GATE_ACTIVE:-0}}" = 1 ] && {{ echo active; exit 0; }}; echo inactive; exit 3; fi
    [ -d "{P}/9002" ] && echo active && exit 0; echo inactive; exit 3 ;;
  show)
    case "$*" in
      *NextElapseUSecRealtime*) t=$(( $(date +%s) + ${{FAKE_GATE_NEXT_S:-3600}} ))
        # systemd 257 prints this property human-readable even with --timestamp=unix
        if [ "${{FAKE_GATE_NEXT_HUMAN:-0}}" = 1 ]; then date -u -d "@$t" '+%a %Y-%m-%d %H:%M:%S UTC'
        else echo "@$t"; fi ;;
      *ExecStart*) if [ -f "{DR}" ] && grep -q -- --execute "{DR}"; then
                     echo "{{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 qflix-entitlement.py --execute ; }}"
                   else echo "{{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 qflix-entitlement.py ; }}"; fi ;;
    esac ;;
  start)
    if [ "$2" = "{GATE_UNIT}" ]; then
      ex=False; {{ [ -f "{DR}" ] || [ "${{FAKE_GATE_LOG_STUCK:-0}}" = 1 ]; }} && ex=True
      [ "${{FAKE_GATE_NO_LOG:-0}}" = 1 ] || echo "$(date -u +%FT%TZ) [qflix-entitlement] roster=members.yaml armed=True window=False execute=$ex" >> "{GL}/entitlement-$(date -u +%F).log"
    fi ;;
  enable) if [ "$2" = --now ]; then
            {{ [ -L "{U}/$3" ] || [ ! -s "{U}/$3" ]; }} && {{ echo "unit $3 is masked or missing" >&2; exit 1; }}
            echo "ENV: $(tr '\\n' ' ' < "{_posix(self.envdir)}/seerr.env")" >> "{C}"
            [ "${{FAKE_NATIVE_FAILS:-0}}" = 1 ] && exit 0
            mkdir -p "{P}/9002"
            printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9002/status"
            echo "0::/user.slice/user-1.slice/app.slice/$3" > "{P}/9002/cgroup"
            printf '/h/.apps/seerr/bin/node/bin/node\\0--require\\0x.cjs\\0/h/.apps/seerr/bin/current/dist/index.js\\0' > "{P}/9002/cmdline"
          fi ;;
  stop) [ "$2" = "{UNIT}" ] && rm -rf "{P}/9002" ;;
  mask) [ -s "{U}/$2" ] && [ ! -L "{U}/$2" ] && {{ echo "Failed to mask unit: File {U}/$2 already exists." >&2; exit 1; }}
        ln -sf /dev/null "{U}/$2" ;;
  unmask) {{ [ -L "{U}/$2" ] || [ ! -s "{U}/$2" ]; }} && rm -f "{U}/$2" ;;
esac
exit 0
''')
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
        self._w("ps", f'''n=${{FAKE_TASKS:-1000}}; [ -f "{P}/proof.alive" ] && n=$((n + ${{FAKE_PROOF_TASKS:-30}}))
for i in $(seq 1 "$n"); do echo x; done
''')
        self._w("fuser", 'exit "${FAKE_DB_BUSY_RC:-1}"\n')
        self._w("curl", f'''url=""; out=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift ;; -*) ;; *) url="$1" ;; esac; shift; done
echo "curl $url" >> "{C}"
case "$url" in
  *nodejs.org*) cp "{_posix(self.node_payload)}" "$out" ;;
  */releases/download/*) cp "{_posix(self.payload)}" "$out" ;;
  */api/v1/status) [ "${{FAKE_VHOST_RC:-0}}" = 0 ] || exit 22
                   echo '{{"version":"'"${{FAKE_VHOST_VERSION:-3.5.0}}"'"}}' ;;
  *) exit 22 ;;
esac
''')
        self._w("hostpolicy", f'''echo "hostpolicy $*" >> "{C}"
case "$1" in
  preflight) [ -n "${{FAKE_PROFILE-ultra}}" ] || exit 2; echo "${{FAKE_PROFILE-ultra}}" ;;
  in-window) exit "${{FAKE_INWINDOW_RC:-1}}" ;;
  task-ceiling) echo "${{FAKE_CEILING:-2000}}" ;;
esac
''')
        for c in ("movie", "anime"):
            self._w(f"{c}.sh", f'echo "smoke {c}" >> "{C}"\nexit "${{FAKE_SMOKE_RC:-0}}"\n',
                    where=self.canaries)

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
                 QFLIX_PYTHON=_posix(sys.executable),
                 QFLIX_APPCTL=_posix(self.stub / "appctl"),
                 QFLIX_SYSTEMCTL=_posix(self.stub / "systemctl"),
                 QFLIX_SS=_posix(self.stub / "ss"), QFLIX_PS=_posix(self.stub / "ps"),
                 QFLIX_FUSER=_posix(self.stub / "fuser"), QFLIX_CURL=_posix(self.stub / "curl"),
                 QFLIX_HOSTPOLICY=_posix(self.stub / "hostpolicy"),
                 QFLIX_CANARIES_DIR=_posix(self.canaries), QFLIX_GATE_LOG_DIR=_posix(self.gatelog),
                 QFLIX_SEERR_SHA256=self.sha, QFLIX_NODE_SHA256=self.node_sha,
                 QFLIX_SEERR_PORT=str(self.port), FAKE_SS_FILE=_posix(self.ss_file),
                 QFLIX_POLL_S="0.2", QFLIX_SETTLE_S="0.3", QFLIX_STOP_TIMEOUT_S="3",
                 QFLIX_PROOF_TIMEOUT_S="20", QFLIX_STATUS_TIMEOUT_S="5",
                 QFLIX_GATE_GAP_S="5", QFLIX_GATE_RUN_TIMEOUT_S="3")
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

    def swapped(self, env=None):
        self.proved()
        r = self.run("--swap", "--execute", env=env)
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


@pytest.mark.parametrize("path", [INSTALLER, GOLDEN_UNIT, PRELOAD, WORKFLOW])
def test_lf_only_and_ascii(path):
    data = path.read_bytes()
    assert b"\r" not in data
    data.decode("ascii")


def test_pins_match_versions_env():
    text = INSTALLER.read_text(encoding="utf-8")
    v = _versions_env()
    assert f'VERSION="{v["SEERR_VERSION"]}"' in text
    assert f'COMMIT="{v["SEERR_COMMIT"]}"' in text
    assert f'NODE_VERSION="{v["SEERR_NODE_VERSION"]}"' in text
    assert f'NODE_SHA256="{v["SEERR_NODE_SHA256"]}"' in text
    assert re.search(r'^ARTIFACT_SHA256="[0-9a-f]{64}"$', text, re.M), "artifact sha256 not pinned"
    assert "releases/download/seerr-v${VERSION}/seerr-${VERSION}-linux-x64.tar.gz" in text
    assert "nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz" in text


def test_workflow_builds_the_pinned_commit_in_bullseye_and_publishes_once():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    text = WORKFLOW.read_text(encoding="utf-8")
    for k in ("SEERR_VERSION", "SEERR_COMMIT", "SEERR_NODE_VERSION", "SEERR_NODE_SHA256"):
        assert k in text
    assert "debian:bullseye" in text
    assert "docker run -i " in text                                  # script arrives on stdin
    assert "codeload.github.com/seerr-team/seerr/tar.gz/${SEERR_COMMIT}" in text
    assert "pnpm install --frozen-lockfile" in text and "pnpm build" in text
    assert "sha256sum -c -" in text                                  # node runtime verified
    assert "--require /w/scripts/data/seerr-listen.cjs" in text      # smoke boots the preload
    assert 'tag="seerr-v${VER}"' in text and "never replaced" in text
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["publish"]["permissions"] == {"contents": "write"}
    # every `uses:` is pinned to a full commit sha
    for uses in re.findall(r"uses:\s*(\S+)", text):
        assert re.search(r"@[0-9a-f]{40}$", uses), uses


def test_240_stages_and_deploys_the_installer_and_preload():
    text = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    assert "    scripts/configure/311-native-seerr-install.sh \\\n" in text
    assert "    scripts/data/seerr-listen.cjs \\\n" in text
    assert ('cp -f   "$STG"/scripts/configure/311-native-seerr-install.sh '
            '~/scripts/configure/311-native-seerr-install.sh\n'
            "chmod +x ~/scripts/configure/311-native-seerr-install.sh") in text
    assert 'cp -f   "$STG"/scripts/data/seerr-listen.cjs ~/scripts/data/seerr-listen.cjs' in text


def test_installer_never_calls_the_panel_tool_the_gateway_or_the_rail():
    code = [l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith("#")]
    assert not any("app-seerr" in l for l in code)
    assert not any("172.17.0.1" in l for l in code)
    assert not any("NODE_OPTIONS" in l for l in code)
    # I-13: the gate is paused by its drop-in, never by members.yaml's rail.
    assert not any(re.search(r"\brail\b", l) for l in code)
    assert not any(re.search(r"(sed|>|write_text).*members\.yaml", l) for l in code)


def test_manifest_flip_for_seerr():
    a = yaml.safe_load((REPO / "manifest" / "apps.yaml").read_text(encoding="utf-8"))["apps"]["seerr"]
    assert a["class"] == "systemd" and a["unit"] == UNIT and a["ucc_dormant"] is True
    assert a["swap_state"] == "pending-swap" and a["ucc_slug"] == "seerr"
    up = a["upgrade"]
    assert up["kind"] == "tarball_swap" and "{version}" in up["target_dir"]
    assert any("311-native-seerr-install.sh --post-upgrade {version} --execute" in s
               for s in up["post_steps"])


def test_golden_unit_is_what_the_installer_renders(box):
    box.installed()
    staged = box.appdir / "native" / UNIT
    assert staged.read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    unit = GOLDEN_UNIT.read_text(encoding="utf-8")
    assert ("ExecStart=%h/.apps/seerr/bin/node/bin/node --disable-wasm-trap-handler --require "
            "%h/.apps/seerr/native/seerr-listen.cjs %h/.apps/seerr/bin/current/dist/index.js\n") in unit
    assert "WorkingDirectory=%h/.apps/seerr/bin/current\n" in unit
    assert "EnvironmentFile=%h/.config/qflix/seerr.env" in unit
    assert "TasksMax" not in unit and "NODE_OPTIONS" not in unit


# --- inert by default -----------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["--install"], ["--prove"], ["--swap"], ["--finish"],
                                  ["--rollback"], ["--post-upgrade", "3.5.1"]])
def test_without_execute_nothing_is_touched(box, args):
    before = sorted(p.as_posix() for p in box.tmp.rglob("*"))
    r = box.run(*args)
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    after = sorted(p.as_posix() for p in box.tmp.rglob("*") if p.name != "host.id")
    assert after == before
    calls = box.calls_text()
    assert "appctl" not in calls and "systemctl" not in calls and "curl" not in calls


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
    assert box.container_running() and box.dropin.read_text() == DROPIN


# --- step 1: pin + install --------------------------------------------------------

def test_install_lays_out_node_artifact_env_and_stages_unit_without_enabling(box):
    box.installed()
    assert (box.appdir / "bin" / "node-v22.23.2" / "bin" / "node").exists()
    assert (box.appdir / "bin" / "node" / "bin" / "node").exists()
    cur = box.appdir / "bin" / "current"
    assert (box.appdir / "bin" / "3.5.0" / "dist" / "index.js").exists()
    assert (cur / "dist" / "index.js").exists() and (cur / ".next" / "BUILD_ID").exists()
    assert (box.appdir / "native" / "seerr-listen.cjs").read_bytes() == PRELOAD.read_bytes()
    env = (box.envdir / "seerr.env").read_text().splitlines()
    for line in ("UV_THREADPOOL_SIZE=4", "MALLOC_ARENA_MAX=2", "NODE_ENV=production",
                 "TZ=Europe/Amsterdam", f"COMMIT_TAG={COMMIT}",
                 f"CONFIG_DIRECTORY={_posix(box.appdir)}", f"PORT={box.port}"):
        assert line in env, line
    assert not any(l.startswith(("SEERR_LISTEN", "NODE_OPTIONS", "HOST=")) for l in env)
    assert (box.appdir / "native" / UNIT).exists()
    assert not (box.unitdir / UNIT).exists()
    assert "enable" not in box.calls_text()
    assert box.container_running()
    assert box.dropin.read_text() == DROPIN                           # gate untouched


def test_install_refuses_version_mismatch(box):
    r = box.run("--install", "--execute", env={"FAKE_UCC_VERSION": "3.4.0"})
    assert r.returncode != 0
    assert not (box.appdir / "bin" / "3.5.0").exists()


def test_install_refuses_artifact_sha_mismatch(box):
    r = box.run("--install", "--execute", env={"QFLIX_SEERR_SHA256": "0" * 64})
    assert r.returncode != 0 and "sha256" in r.stderr
    assert not (box.appdir / "bin" / "3.5.0").exists()


def test_install_refuses_an_unpinned_artifact(box):
    r = box.run("--install", "--execute", env={"QFLIX_SEERR_SHA256": "TBD"})
    assert r.returncode != 0 and "not pinned" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_install_refuses_node_sha_mismatch(box):
    r = box.run("--install", "--execute", env={"QFLIX_NODE_SHA256": "0" * 64})
    assert r.returncode != 0 and "Node" in r.stderr
    assert not (box.appdir / "bin" / "node-v22.23.2").exists()


def test_install_refuses_a_node_that_reports_another_version(box):
    r = box.run("--install", "--execute", env={"FAKE_NODE_VERSION": "22.1.0"})
    assert r.returncode != 0 and "v22.1.0" in r.stderr
    assert not (box.appdir / "bin" / "3.5.0").exists()


@pytest.mark.parametrize("kw,msg", [({"commit": "0" * 40}, "commitTag"),
                                    ({"version": "3.4.0"}, "package.json"),
                                    ({"with_dist": False}, "not a Seerr build")])
def test_install_refuses_an_artifact_that_is_not_the_pinned_build(box, kw, msg):
    box.set_artifact(**kw)
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and msg in r.stderr
    assert not (box.appdir / "bin" / "3.5.0").exists()


def test_install_refuses_an_artifact_that_escapes_its_directory(box):
    box.payload.write_bytes(_tgz({"../evil": ("x", 0o644)}))
    r = box.run("--install", "--execute",
                env={"QFLIX_SEERR_SHA256": hashlib.sha256(box.payload.read_bytes()).hexdigest()})
    assert r.returncode != 0 and "unsafe" in r.stderr
    assert not (box.apps / "evil").exists()


# --- step 2: proof -------------------------------------------------------------------

def _live_hash(box) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (box.settings, box.appdir / "db" / "db.sqlite3")}


def test_prove_boots_a_sanitized_copy_checks_login_measures_tasks_and_cleans_up(box):
    live_before = _live_hash(box)
    r = box.proved()
    assert "delta=30" in r.stdout and "version 3.5.0" in r.stdout and "login ok" in r.stdout
    assert not (box.apps / ".prove" / "seerr").exists()
    proof = json.loads((box.swap / "seerr" / "proof.json").read_text())
    assert proof["ok"] is True and proof["login"] is True and proof["delta"] == 30
    assert box.container_running()
    assert _live_hash(box) == live_before                               # live data untouched
    pe = box.proof_env()
    assert pe["env"]["CONFIG_DIRECTORY"].endswith("/.apps/.prove/seerr")
    assert pe["env"]["SEERR_LISTEN"].startswith("127.0.0.1:")
    assert pe["env"]["NODE_ENV"] == "production" and pe["env"]["NODE_OPTIONS"] is None
    # bin/current (Git Bash copies it) or the release it links to (getcwd resolves)
    assert pe["cwd"].endswith(("/seerr/bin/current", "/seerr/bin/3.5.0"))
    assert pe["argv"][0] == "--disable-wasm-trap-handler" and pe["argv"][1] == "--require"
    assert box.dropin.read_text() == DROPIN                             # gate untouched
    assert "/api/v1/status" not in box.calls_text()                    # never the live vhost


def test_prove_copy_is_inert(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"QFLIX_KEEP_PROOF": "1"})
    assert r.returncode == 0, r.stderr
    pv = box.apps / ".prove" / "seerr"
    s = json.loads((pv / "settings.json").read_text())
    assert s["radarr"] == [] and s["sonarr"] == []
    assert not any(a["enabled"] for a in s["notifications"]["agents"].values())
    assert not any(lib["enabled"] for lib in s["plex"]["libraries"])
    con = sqlite3.connect(pv / "db" / "db.sqlite3")
    assert con.execute("SELECT count(*) FROM user_settings WHERE watchlistSyncMovies!=0 "
                       "OR watchlistSyncTv!=0").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM media_request").fetchone()[0] == 3   # data copied
    con.close()
    live = sqlite3.connect(box.appdir / "db" / "db.sqlite3")
    assert live.execute("SELECT count(*) FROM user_settings WHERE watchlistSyncTv=1").fetchone()[0] == 2
    live.close()
    assert json.loads(box.settings.read_text())["radarr"]                # live settings intact


def test_prove_refuses_at_seventy_percent_of_ceiling(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_TASKS": "1371"})   # 1371 + 30 >= 1400
    assert r.returncode != 0 and "70%" in r.stderr
    assert not (box.swap / "seerr" / "proof.json").exists()
    assert not (box.apps / ".prove" / "seerr").exists()


def test_prove_refuses_a_version_the_status_endpoint_does_not_report(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_REPORT_VERSION": "3.4.0"})
    assert r.returncode != 0 and "3.4.0" in r.stderr
    assert not (box.swap / "seerr" / "proof.json").exists()


def test_prove_refuses_when_login_fails(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_AUTH_FAIL": "1"})
    assert r.returncode != 0 and "login" in r.stderr
    assert not (box.swap / "seerr" / "proof.json").exists()


def test_prove_refuses_when_sanitize_cannot_prove_inert(box):
    box.installed()
    (box.appdir / "db" / "db.sqlite3").write_bytes(b"not a database")
    r = box.run("--prove", "--execute")
    assert r.returncode != 0
    assert not (box.swap / "seerr" / "proof.json").exists()


# --- steps 3-6: swap ------------------------------------------------------------------

def test_swap_refuses_without_a_proof(box):
    box.installed()
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "prove" in r.stderr
    assert box.container_running() and not box.suppressed() and box.dropin.read_text() == DROPIN


def test_swap_refuses_unless_the_pending_swap_flip_is_deployed(box):
    box.proved()
    box.set_manifest(cls="ucc", dormant=False)
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert box.container_running() and box.dropin.read_text() == DROPIN


def test_swap_full_sequence(box):
    r = box.swapped()
    calls = box.calls_text()
    # I-13 order: gate confirmed report-only BEFORE suppression and the stop.
    assert calls.index(f"systemctl --user start {GATE_UNIT}") < calls.index("appctl stop seerr")
    assert calls.index("appctl stop seerr") < calls.index(f"systemctl --user enable --now {UNIT}")
    assert box.gate_lines() == ["execute=False"]
    assert not box.dropin.exists()                                      # paused...
    assert (box.swap / "seerr" / "gate" / "execute.conf").read_text() == DROPIN   # ...parked intact
    g = json.loads((box.swap / "seerr" / "gate.json").read_text())
    assert g["dropin"] == "present" and g["roster_armed"] == "true"
    assert "gate armed state BEFORE: drop-in=present roster_armed=true exec_has_execute=yes" in r.stdout
    assert not box.container_running() and box.native_running()
    assert (box.unitdir / UNIT).read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    rec = (box.swap / "seerr" / "listen-set.before").read_text().split()
    assert sorted(rec) == sorted(f"{a}:{box.port}" for a in LISTEN_ROWS)
    env = (box.envdir / "seerr.env").read_text().splitlines()
    listen = next(l for l in env if l.startswith("SEERR_LISTEN=")).split("=", 1)[1].split()
    assert sorted(listen) == sorted(rec)
    st = box.swapstate()
    assert st["ucc_version"] == "3.5.0"
    assert st["swap_date"] and st["soak_until"] and st["rollback_window"] == "open"
    assert set(box.suppressed()) == {"seerr", "canary-movie", "canary-anime",
                                     "canary-seerr-arr-parity", "canary-entitlement-service",
                                     "canary-thread-ceiling"}
    assert "curl https://seerr-slot.example.invalid/api/v1/status" in calls   # vhost live test
    assert calls.index("smoke movie") > calls.index(f"enable --now {UNIT}")
    assert "smoke anime" in calls
    snaps = list((box.swap / "seerr").glob("snapshot-*.tgz"))
    assert len(snaps) == 1
    with tarfile.open(snaps[0]) as tf:
        names = tf.getnames()
        assert "seerr/db/db.sqlite3" in names and "seerr/settings.json" in names
        assert not any(n.startswith(("seerr/bin", "seerr/native", "seerr/logs")) for n in names)
    assert "elapsed=" in r.stdout and "PAUSED" in r.stdout


@pytest.mark.parametrize("rows", [
    "",                                                         # nothing listening
    "LISTEN 0 65535 0.0.0.0:{p} 0.0.0.0:*\n",                  # wildcard: never an option
    "LISTEN 0 65535 *:{p} 0.0.0.0:*\n",
])
def test_swap_refuses_an_empty_or_wildcard_listen_set_before_touching_the_gate(box, rows):
    box.proved()
    box.ss_file.write_text(rows.format(p=box.port), newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "listen" in r.stderr
    assert box.container_running() and not box.suppressed()
    assert box.dropin.read_text() == DROPIN and box.gate_lines() == []


def test_swap_refuses_container_paths_in_settings(box):
    box.proved()
    s = json.loads(box.settings.read_text())
    s["main"]["cachePath"] = "/app/config/cache"
    box.settings.write_text(json.dumps(s), newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "container path" in r.stderr and "main.cachePath" in r.stderr
    assert box.container_running() and box.dropin.read_text() == DROPIN


def test_swap_ignores_arr_root_folders_and_url_bases(box):
    """activeDirectory is the ARR's path; /radarr etc. are URL bases, not paths."""
    box.swapped()
    assert box.native_running()


def test_swap_refuses_a_port_secret_that_disagrees(box):
    box.proved()
    (box.secrets / "seerr.port").write_text("1")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "seerr.port" in r.stderr
    assert box.container_running() and box.dropin.read_text() == DROPIN


def test_swap_aborts_when_the_gate_will_not_go_report_only(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_GATE_LOG_STUCK": "1"})
    assert r.returncode != 0 and "execute=False" in r.stderr
    assert box.dropin.read_text() == DROPIN                             # reinstalled
    assert not (box.swap / "seerr" / "gate" / "execute.conf").exists()
    assert "appctl stop" not in box.calls_text()
    assert box.container_running() and not box.suppressed()


def test_swap_aborts_when_the_gate_logs_nothing(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_GATE_NO_LOG": "1"})
    assert r.returncode != 0
    assert box.dropin.read_text() == DROPIN and "appctl stop" not in box.calls_text()


def test_swap_with_no_drop_in_records_absent_and_never_creates_one(box):
    box.dropin.unlink()
    box.swapped()
    g = json.loads((box.swap / "seerr" / "gate.json").read_text())
    assert g["dropin"] == "absent"
    assert box.gate_lines() == ["execute=False"]
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--finish", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not box.dropin.exists()                                      # never arm what was not


@pytest.mark.parametrize("human", ["0", "1"])
def test_swap_waits_out_an_imminent_gate_run(box, human):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_GATE_NEXT_S": "2", "FAKE_GATE_NEXT_HUMAN": human})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "waiting it out" in r.stdout


def test_swap_aborts_when_container_never_exits_and_restores_everything(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_CONTAINER_STICKS": "1"})
    assert r.returncode != 0 and "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert not box.native_running() and box.container_running()
    assert not box.suppressed()
    assert box.dropin.read_text() == DROPIN                             # gate re-armed


def test_swap_aborts_while_something_still_holds_the_database(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_DB_BUSY_RC": "0"})
    assert r.returncode != 0 and "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert not box.suppressed() and box.dropin.read_text() == DROPIN


def test_swap_without_fuser_cannot_prove_the_db_idle(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"QFLIX_FUSER": "/nonexistent/fuser"})
    assert r.returncode != 0
    assert "enable --now" not in box.calls_text()


@pytest.mark.parametrize("env,msg", [
    ({"FAKE_NATIVE_FAILS": "1"}, "--rollback"),
    ({"FAKE_NATIVE_LISTEN_DIFF": "1"}, "listen set differs"),
    ({"FAKE_VHOST_RC": "22"}, "vhost does not answer"),
    ({"FAKE_VHOST_VERSION": "3.4.0"}, "vhost reports '3.4.0'"),
    ({"FAKE_SMOKE_RC": "2"}, "request smoke failed"),
])
def test_swap_post_start_failures_keep_suppression_and_the_gate_paused(box, env, msg):
    box.proved()
    r = box.run("--swap", "--execute", env=env)
    assert r.returncode != 0 and msg in r.stderr, r.stderr
    assert "seerr" in box.suppressed()
    assert not box.dropin.exists()
    assert (box.swap / "seerr" / "gate" / "execute.conf").read_text() == DROPIN


def test_swap_wrong_reported_version_fails_parity(box):
    box.proved()
    box.api_version = "3.4.0"
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "3.4.0" in r.stderr
    assert "seerr" in box.suppressed()


def test_swap_login_failure_fails_parity(box):
    box.proved()
    box.auth_ok = False
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "login" in r.stderr


def test_swap_is_resumable_when_already_swapped(box):
    box.swapped()
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stderr
    assert "already" in r.stdout
    assert box.calls_text().count("appctl stop seerr") == 1


# --- step 9: finish -----------------------------------------------------------------

def test_finish_refuses_while_manifest_still_pending_swap(box):
    box.swapped()
    r = box.run("--finish", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert "seerr" in box.suppressed() and not box.dropin.exists()


def test_finish_rearms_the_gate_exactly_then_lifts_app_and_canaries(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--finish", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.dropin.read_text() == DROPIN                             # byte-identical
    assert not (box.swap / "seerr" / "gate" / "execute.conf").exists()
    assert "gate armed state AFTER: drop-in=present roster_armed=true exec_has_execute=yes" in r.stdout
    calls = box.calls_text()
    assert calls.rindex("daemon-reload") < calls.rindex("show manitoba-maint-entitlement.service")
    assert box.suppressed() == {}


def test_finish_refuses_a_different_drop_in_that_appeared_meanwhile(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    box.dropin.write_text("[Service]\nExecStart=\nExecStart=/bin/true --execute\n", newline="\n")
    r = box.run("--finish", "--execute")
    assert r.returncode != 0 and "DIFFERENT" in r.stderr
    assert "seerr" in box.suppressed()


def test_finish_refuses_when_the_roster_arm_changed(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    (box.secrets / "members.yaml").write_text("armed: false\n", newline="\n")
    r = box.run("--finish", "--execute")
    assert r.returncode != 0 and "roster armed changed" in r.stderr
    assert "seerr" in box.suppressed()


# --- rollback (0-5) -----------------------------------------------------------------------

def test_rollback_masks_before_stopping_restores_container_and_gate(box):
    box.swapped()
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls_text()
    assert calls.index(f"systemctl --user mask {UNIT}") < calls.index(f"systemctl --user stop {UNIT}")
    assert _masked(box.unitdir / UNIT)
    assert not box.native_running() and box.container_running()
    assert calls.rindex("appctl start seerr") > calls.index(f"systemctl --user stop {UNIT}")
    assert box.dropin.read_text() == DROPIN
    assert box.suppressed() == {}
    assert "elapsed=" in r.stdout


def test_rollback_pauses_before_ucc_start_until_manifest_reverted(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--rollback", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 10 and "revert" in r.stderr
    assert not box.native_running() and not box.container_running()
    assert "appctl start" not in box.calls_text()
    assert "seerr" in box.suppressed() and not box.dropin.exists()   # gate stays paused
    box.set_manifest(swap_state="pending-swap")
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running() and box.dropin.read_text() == DROPIN


def test_drill_rollback_then_reswap_unmasks(box):
    box.swapped()
    assert box.run("--rollback", "--execute").returncode == 0
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"systemctl --user unmask {UNIT}" in box.calls_text()
    assert not _masked(box.unitdir / UNIT)
    assert box.native_running() and not box.container_running()
    assert box.gate_lines() == ["execute=False", "execute=False"]


def test_rollback_with_nothing_swapped_is_harmless(box):
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running() and box.dropin.read_text() == DROPIN


# --- --post-upgrade (tarball_swap post step) ---------------------------------------------------

def _extract_build(box, version, *, engines="^22.19.0", tag="abc123"):
    d = box.appdir / "bin" / version                           # what lifecycle's tar -x leaves
    shutil.copytree(box.appdir / "bin" / "3.5.0", d)
    (d / "package.json").write_text(json.dumps({"version": version, "engines": {"node": engines}}))
    (d / "committag.json").write_text(json.dumps({"commitTag": tag}))
    return d


@pytest.mark.skipif(os.name == "nt", reason="Git Bash copies `current` instead of linking")
def test_post_upgrade_flips_current_and_rewrites_commit_tag(box):
    box.swapped()
    _extract_build(box, "3.5.1")
    r = box.run("--post-upgrade", "3.5.1", "--execute", env={"FAKE_INWINDOW_RC": "0"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.readlink(box.appdir / "bin" / "current") == "3.5.1"
    env = (box.envdir / "seerr.env").read_text().splitlines()
    assert "COMMIT_TAG=abc123" in env
    assert any(l.startswith("SEERR_LISTEN=") for l in env)     # the recorded set survives
    assert f"CONFIG_DIRECTORY={_posix(box.appdir)}" in env


def test_post_upgrade_refuses_a_directory_that_is_not_a_build(box):
    box.installed()
    (box.appdir / "bin" / "9.9.9").mkdir()
    r = box.run("--post-upgrade", "9.9.9", "--execute")
    assert r.returncode != 0 and "not an extracted Seerr build" in r.stderr


def test_post_upgrade_refuses_a_build_for_another_node_major(box):
    box.installed()
    _extract_build(box, "4.0.0", engines="^24.0.0")
    r = box.run("--post-upgrade", "4.0.0", "--execute")
    assert r.returncode != 0 and "Node upgrade" in r.stderr
