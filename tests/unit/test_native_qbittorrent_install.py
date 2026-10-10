"""scripts/configure/310-native-qbittorrent-install.sh (QFLX-35, A11 ADOPT, spec 5.9).

Subprocess tests against fakes that MODEL the box (same approach as the pilot,
test_native_unpackerr_install.py): a fake /proc tree (the panel engine's pid in
the panel unit's cgroup .../qbittorrent.service, the tracked engine's pid in
.../qflix-qbittorrent.service) and fake appctl / systemctl / ss / ps / curl /
hostpolicy that mutate it like the real tools. The WebUI API is a REAL local
HTTP server (login, torrents/info, stop/start) so the pause/resume of the
exact active set is exercised end to end; the proof boots a fake
qbittorrent-nox that serves /api/v2/app/version on its scratch port.

Real python runs swapstate.py and suppression.py: swap state and
push-suppress.json are the real files.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "configure" / "310-native-qbittorrent-install.sh"
GOLDEN_UNIT = REPO / "scripts" / "maint" / "systemd" / "qflix-qbittorrent.service"
MANIFEST = REPO / "manifest" / "apps.yaml"
UNIT = "qflix-qbittorrent.service"
PANEL = "qbittorrent.service"
BTPORT = "64120"
PIN_SHA = "0c6be8354f7d0ef4971e1a1abba2bf667b1aa5e6a8670d831b03910fafe8df92"

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


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# The static qbittorrent-nox: `--version`, or a WebUI on --webui-port that
# answers /api/v2/app/version. Registers its own fake /proc entry (exec keeps
# the pid) so the proof can count its tasks.
FAKE_NOX = r'''#!/usr/bin/env bash
prof=""; port=""
for a in "$@"; do
  case "$a" in
    --version) echo "qBittorrent v${FAKE_QBIT_VERSION:-5.0.3}"; exit 0 ;;
    --profile=*) prof="${a#*=}" ;;
    --webui-port=*) port="${a#*=}" ;;
  esac
done
echo "$HOME|$prof|$port|$TMPDIR" > "$FAKE_PROOF_LOG"
mkdir -p "$QFLIX_PROC/$$"
for i in $(seq 1 "${FAKE_QBIT_TASKS:-12}"); do mkdir -p "$QFLIX_PROC/$$/task/$i"; done
exec "$QFLIX_PYTHON" - "$port" <<'PY'
import os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = (("v" + os.environ.get("FAKE_QBIT_VERSION", "5.0.3"))
             if self.path == "/api/v2/app/version" else "ok").encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
'''


class FakeQbit:
    """The live engine's WebUI API, as the installer's qbit_api sees it."""

    def __init__(self, user="quser", password="qpass"):
        self.user, self.password = user, password
        self.torrents = {"aaa1": "uploading", "bbb2": "stoppedUP",
                         "ccc3": "downloading", "ddd4": "pausedDL", "eee5": "queuedDL"}
        self.calls: list[tuple[str, str]] = []
        self.legacy = False          # 4.x API: stop/start 404, pause/resume work
        self.refuse_login = False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, headers=None):
                b = body.encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _authed(self):
                return "SID=sid123" in (self.headers.get("Cookie") or "")

            def do_GET(self):
                outer.calls.append(("GET", self.path))
                if not self._authed():
                    return self._send(403, "Forbidden")
                if self.path == "/api/v2/app/version":
                    return self._send(200, "v5.0.3")
                if self.path == "/api/v2/torrents/info":
                    return self._send(200, json.dumps(
                        [{"hash": h, "state": s} for h, s in outer.torrents.items()]))
                return self._send(404, "")

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                form = urllib.parse.parse_qs(self.rfile.read(n).decode())
                outer.calls.append(("POST", self.path))
                if self.path == "/api/v2/auth/login":
                    if (outer.refuse_login or form.get("username") != [outer.user]
                            or form.get("password") != [outer.password]):
                        return self._send(200, "Fails.")
                    return self._send(200, "Ok.", {"Set-Cookie": "SID=sid123; HttpOnly; path=/"})
                if not self._authed():
                    return self._send(403, "Forbidden")
                verb = self.path.rsplit("/", 1)[-1]
                if outer.legacy and verb in ("stop", "start"):
                    return self._send(404, "")
                if not outer.legacy and verb in ("pause", "resume"):
                    return self._send(404, "")
                hashes = (form.get("hashes") or [""])[0].split("|")
                for h in hashes:
                    if h in outer.torrents:
                        if verb in ("stop", "pause"):
                            outer.torrents[h] = "stoppedDL" if "DL" in outer.torrents[h] or \
                                outer.torrents[h] == "downloading" else "stoppedUP"
                        else:
                            outer.torrents[h] = "uploading"
                outer.calls.append((verb, ",".join(sorted(hashes))))
                return self._send(200, "")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def verbs(self, *names):
        return [c for c in self.calls if c[0] in names]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Box:
    def __init__(self, tmp: Path, api: FakeQbit):
        self.tmp, self.api = tmp, api
        self.port = str(api.port)
        self.home = tmp / "home"
        self.apps = self.home / ".apps"
        self.appdir = self.apps / "qbittorrent"
        self.unitdir = self.home / ".config" / "systemd" / "user"
        self.envdir = self.home / ".config" / "qflix"
        self.qconf = self.home / ".config" / "qBittorrent" / "qBittorrent.conf"
        self.qdata = self.home / ".local" / "share" / "qBittorrent"
        self.state = self.home / ".opt" / "maint"
        self.swap = self.state / "swap"
        self.secrets = self.home / "secrets"
        self.proc = tmp / "proc"
        self.stub = tmp / "stub"
        self.calls = tmp / "calls.log"
        self.manifest = self.state / "apps.yaml"
        self.prooflog = tmp / "proof-args.log"
        for d in (self.unitdir, self.envdir, self.qconf.parent, self.qdata, self.swap,
                  self.proc, self.stub, self.secrets, self.home / "bin"):
            d.mkdir(parents=True, exist_ok=True)
        (self.secrets / "qbittorrent.port").write_text(self.port + "\n")
        (self.secrets / "qbittorrent.user").write_text(api.user + "\n")
        (self.secrets / "qbittorrent.password").write_text(api.password + "\n")
        self.panel_unit_text = ("[Unit]\nDescription=qBittorrent 5.0.3\n\n[Service]\n"
                                "ExecStart=%h/bin/qbittorrent-nox\n\n[Install]\n"
                                "WantedBy=default.target\n")
        (self.unitdir / PANEL).write_text(self.panel_unit_text, newline="\n")
        self.write_conf()
        self.uid = _uid()
        self.set_manifest(swap_state="pending-swap")
        self.payload = tmp / "x86_64-qbittorrent-nox"
        self.payload.write_text(FAKE_NOX, newline="\n")
        self.sha = hashlib.sha256(self.payload.read_bytes()).hexdigest()
        # The panel runs the SAME build: its ~/bin binary hashes to the pin.
        shutil.copy(self.payload, self.home / "bin" / "qbittorrent-nox")
        self.panel_up()
        self._stubs()

    # --- state ---------------------------------------------------------------
    def write_conf(self, **over):
        kv = {"Session\\Port": BTPORT,
              "Session\\DefaultSavePath": "/home/u/downloads/qbittorrent",
              "WebUI\\Address": "*", "WebUI\\Port": self.port,
              "WebUI\\LocalHostAuth": "true", "WebUI\\AuthSubnetWhitelistEnabled": "false"}
        kv.update(over)
        bt = [f"{k}={v}" for k, v in kv.items() if k.startswith("Session") and v is not None]
        pr = [f"{k}={v}" for k, v in kv.items() if not k.startswith("Session") and v is not None]
        self.qconf.write_text("[BitTorrent]\n" + "\n".join(bt) + "\n\n[Preferences]\n"
                              + "\n".join(pr) + "\n", newline="\n")

    def _proc(self, pid: int, cgroup: str, cmd: str):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(f"Name:\tx\nUid:\t{self.uid}\t{self.uid}\nPPid:\t1\n", newline="\n")
        (d / "cgroup").write_text(cgroup + "\n", newline="\n")
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
        (d / "task" / "1").mkdir(parents=True, exist_ok=True)

    def panel_up(self):
        self._proc(9001, f"0::/user.slice/user-1.slice/app.slice/{PANEL}",
                   "/home/u/bin/qbittorrent-nox")

    def panel_running(self) -> bool:
        return (self.proc / "9001").exists()

    def native_running(self) -> bool:
        return (self.proc / "9002").exists()

    def set_manifest(self, *, cls="systemd", swap_state=None, dormant=True):
        lines = ["apps:", "  qbittorrent:", f"    class: {cls}", "    ucc_slug: qbittorrent"]
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
    def _w(self, name: str, body: str):
        p = self.stub / name
        p.write_text("#!/usr/bin/env bash\n" + body, newline="\n")
        p.chmod(0o755)

    def _stubs(self):
        P, C, U = _posix(self.proc), _posix(self.calls), _posix(self.unitdir)
        mk = ('mk() {{ mkdir -p "{P}/$1/task/1"; '
              'printf "Name:\\tx\\nUid:\\t%s\\t%s\\n" "$(id -u)" "$(id -u)" > "{P}/$1/status"; '
              'echo "0::/user.slice/user-1.slice/app.slice/$2" > "{P}/$1/cgroup"; '
              'printf "%s\\0" "$3" > "{P}/$1/cmdline"; }}\n').format(P=P)
        self._w("appctl", f'''echo "appctl $*" >> "{C}"
case "$1" in
  version) echo '{{"data": {{"version": "'"${{FAKE_UCC_VERSION:-5.0.3}}"'"}}, "result": true}}' ;;
  is-native) v="${{FAKE_ISNATIVE:-ucc}}"; echo "$v"; [ "$v" = native ] ;;
esac
''')
        self._w("systemctl", mk + f'''echo "systemctl $*" >> "{C}"
[ "$1" = --user ] && shift
native() {{ mk 9002 {UNIT} /h/.apps/qbittorrent/bin/current/qbittorrent-nox; }}
panel()  {{ mk 9001 {PANEL} /home/u/bin/qbittorrent-nox; }}
case "$1" in
  is-active) case "$2" in
               {UNIT}) [ -d "{P}/9002" ] && echo active && exit 0 ;;
               {PANEL}) [ -d "{P}/9001" ] && echo active && exit 0 ;;
             esac; echo inactive; exit 3 ;;
  enable) if [ "$2" = --now ]; then
            case "$3" in
              {PANEL}) [ -f "{U}/{PANEL}" ] || {{ echo "Unit {PANEL} not found" >&2; exit 1; }}; panel ;;
              {UNIT}) {{ [ -L "{U}/$3" ] || [ ! -s "{U}/$3" ]; }} && {{ echo "unit $3 is masked or missing" >&2; exit 1; }}
                      [ "${{FAKE_NATIVE_FAILS:-0}}" = 1 ] && exit 0
                      native ;;
            esac
          fi ;;
  disable) if [ "$2" = --now ] && [ "$3" = {PANEL} ]; then
             [ "${{FAKE_PANEL_STICKS:-0}}" = 1 ] || rm -rf "{P}/9001"
           fi ;;
  restart) case "$2" in
             {UNIT}) rm -rf "{P}/9002"; native; touch "{P}/.restarted" ;;
             {PANEL}) panel; touch "{P}/.restarted" ;;
           esac ;;
  stop) [ "$2" = {UNIT} ] && rm -rf "{P}/9002" ;;
  mask) [ -s "{U}/$2" ] && [ ! -L "{U}/$2" ] && {{ echo "Failed to mask unit: File {U}/$2 already exists." >&2; exit 1; }}
        ln -sf /dev/null "{U}/$2" ;;
  unmask) {{ [ -L "{U}/$2" ] || [ ! -s "{U}/$2" ]; }} && rm -f "{U}/$2" ;;
esac
exit 0
''')
        # Both listeners exist while EITHER engine runs (one profile, one set).
        self._w("ss", f'''if [ -d "{P}/9001" ] || [ -d "{P}/9002" ]; then
  echo "LISTEN 0 50 *:{self.port} *:*"
  echo "LISTEN 0 30 192.0.2.10%bond0:{BTPORT} 0.0.0.0:*"
fi
[ -n "${{FAKE_SS_EXTRA:-}}" ] && echo "$FAKE_SS_EXTRA"
exit 0
''')
        self._w("ps", 'n=${FAKE_TASKS:-1000}; for i in $(seq 1 "$n"); do echo x; done\n')
        self._w("curl", 'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                        f'cp "{_posix(self.payload)}" "$out"\n')
        # Live WebUI probe: 200 while an engine runs, unless it lost the boot bind
        # race (FAKE_BIND_RACE: silent until the first restart).
        self._w("probe", f'''[ "${{FAKE_PROBE_FAILS:-0}}" = 1 ] && {{ printf 000; exit 7; }}
if [ "${{FAKE_BIND_RACE:-0}}" = 1 ] && [ ! -e "{P}/.restarted" ]; then printf 000; exit 7; fi
if [ -d "{P}/9001" ] || [ -d "{P}/9002" ]; then printf 200; else printf 000; exit 7; fi
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
                 HOME=_posix(self.home),
                 QFLIX_HOST_ID_FILE=_posix(marker),
                 QFLIX_APPS_DIR=_posix(self.apps),
                 QFLIX_UNIT_DIR=_posix(self.unitdir),
                 QFLIX_ENV_DIR=_posix(self.envdir),
                 QFLIX_SWAP_DIR=_posix(self.swap),
                 QFLIX_SECRETS_DIR=_posix(self.secrets),
                 QFLIX_QBIT_CONF=_posix(self.qconf),
                 QFLIX_QBIT_DATA=_posix(self.qdata),
                 QFLIX_QBIT_PANEL_BIN=_posix(self.home / "bin" / "qbittorrent-nox"),
                 MANITOBA_STATE_DIR=_posix(self.state),
                 QFLIX_MANIFEST=_posix(self.manifest),
                 QFLIX_PROC=_posix(self.proc),
                 QFLIX_PYTHON=_posix(sys.executable),
                 QFLIX_APPCTL=_posix(self.stub / "appctl"),
                 QFLIX_SYSTEMCTL=_posix(self.stub / "systemctl"),
                 QFLIX_SS=_posix(self.stub / "ss"),
                 QFLIX_PS=_posix(self.stub / "ps"),
                 QFLIX_CURL=_posix(self.stub / "curl"),
                 QFLIX_PROBE_CURL=_posix(self.stub / "probe"),
                 QFLIX_HOSTPOLICY=_posix(self.stub / "hostpolicy"),
                 QFLIX_QBITTORRENT_SHA256=self.sha,
                 FAKE_PROOF_LOG=_posix(self.prooflog),
                 QFLIX_BIND_BACKOFF_S="1 1 1",
                 QFLIX_POLL_S="0.2", QFLIX_SETTLE_S="0.2",
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
        p = self.swap / "qbittorrent" / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}


@pytest.fixture()
def box(tmp_path):
    api = FakeQbit()
    try:
        yield Box(tmp_path, api)
    finally:
        api.close()


SUPPRESSED = {"qbittorrent", "canary-qbit-stall", "canary-thread-ceiling"}
ACTIVE = "aaa1,ccc3,eee5"           # not stopped/paused at swap time


# --- static -------------------------------------------------------------------

def test_bash_syntax():
    r = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _versions_env(key):
    return next(l.split("=", 1)[1].strip() for l in
                (REPO / "versions.env").read_text(encoding="utf-8").splitlines()
                if l.startswith(key + "="))


def test_pins_exact_running_version_and_sha256():
    text = INSTALLER.read_text(encoding="utf-8")
    ver = _versions_env("QBITTORRENT_VERSION")
    assert ver == "5.0.3"                       # `app-qbittorrent version`, box 2026-10-10
    assert f'VERSION="{ver}"' in text
    # sha256 of userdocs release-5.0.3_v1.2.19 x86_64-qbittorrent-nox == the
    # panel's ~/bin/qbittorrent-nox on the box (both hashed 2026-10-10).
    assert f'SHA256="{PIN_SHA}"' in text
    assert 'LIBTORRENT="1.2.19"' in text
    assert "userdocs/qbittorrent-nox-static/releases/download/release-${VERSION}_v${LIBTORRENT}/x86_64-qbittorrent-nox" in text


def test_manifest_upgrade_template_matches_the_installer_pin():
    a = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["apps"]["qbittorrent"]
    up = a["upgrade"]
    assert up["kind"] == "tarball_swap" and up["binary_name"] == "qbittorrent-nox"
    assert up["url_template"].format(version="5.0.3").endswith(
        "release-5.0.3_v1.2.19/x86_64-qbittorrent-nox")
    assert up["version_pin"] == {"source": "versions.env", "key": "QBITTORRENT_VERSION"}
    assert up["target_dir"] == "~/.apps/qbittorrent/bin/{version}"


def test_manifest_flip_is_pending_swap_with_panel_cgroup_marker():
    a = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["apps"]["qbittorrent"]
    assert a["class"] == "systemd" and a["unit"] == UNIT
    assert a["swap_state"] == "pending-swap" and a["ucc_dormant"] is True
    assert a["ucc_cgroup_markers"] == ["/qbittorrent.service"]
    assert a["kuma_monitor"] == "qBittorrent"                 # never renamed
    assert a["recovery_backoff_s"] == [30, 90, 180]           # bind-race backoff kept
    assert a["health"]["port_secret"] == "qbittorrent.port"


def test_240_stages_and_deploys_the_installer():
    text = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    assert "    scripts/configure/310-native-qbittorrent-install.sh \\\n" in text
    assert ("~/scripts/configure/310-native-qbittorrent-install.sh\n"
            "chmod +x ~/scripts/configure/310-native-qbittorrent-install.sh") in text


def test_installer_never_calls_the_panel_tool_or_appctl_lifecycle():
    code = [l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith("#")]
    assert not any("app-qbittorrent" in l for l in code)
    assert not any('"$APPCTL" start' in l or '"$APPCTL" stop' in l for l in code)


def test_installer_never_removes_the_panel_unit():
    code = "\n".join(l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
                     if not l.strip().startswith("#"))
    assert 'rm -f "$UNIT_DIR/$PANEL_UNIT"' not in code
    assert "mask \"$PANEL_UNIT\"" not in code


def test_golden_unit_is_what_the_installer_renders(box):
    box.installed()
    staged = box.appdir / "native" / UNIT
    assert staged.read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    unit = GOLDEN_UNIT.read_text(encoding="utf-8")
    assert "ExecStart=%h/.apps/qbittorrent/bin/current/qbittorrent-nox\n" in unit
    assert "Environment=PATH=%h/.apps/qbittorrent/bin/current:%h/bin:" in unit
    assert "EnvironmentFile=%h/.config/qflix/qbittorrent.env" in unit
    assert "TimeoutStopSec=120" in unit and "TasksMax" not in unit
    assert "--profile" not in unit          # the live default profile, in place


# --- inert by default -----------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["--install"], ["--prove"], ["--swap"],
                                  ["--finish"], ["--rollback"]])
def test_without_execute_nothing_is_touched(box, args):
    before = sorted(p.as_posix() for p in box.tmp.rglob("*"))
    r = box.run(*args)
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    after = sorted(p.as_posix() for p in box.tmp.rglob("*") if p.name != "host.id")
    assert after == before
    assert "appctl" not in box.calls_text() and "systemctl" not in box.calls_text()
    assert box.api.calls == []


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


def test_swap_refuses_on_a_generic_host(box):
    r = box.run("--swap", "--execute", env={"FAKE_PROFILE": "generic"})
    assert r.returncode != 0 and "generic" in r.stderr
    assert box.panel_running()


# --- step 1: pin + install --------------------------------------------------------

def test_install_lays_out_binary_env_and_stages_unit_without_enabling(box):
    box.installed()
    assert (box.appdir / "bin" / "5.0.3" / "qbittorrent-nox").exists()
    assert (box.appdir / "bin" / "current" / "qbittorrent-nox").exists()
    env = (box.envdir / "qbittorrent.env").read_text().splitlines()
    assert f"TMPDIR={_posix(box.qdata)}" in env          # absolute: no %h in an env file
    assert "QT_BEARER_POLL_TIMEOUT=-1" in env and "MALLOC_ARENA_MAX=2" in env
    assert (box.appdir / "native" / UNIT).exists()
    assert not (box.unitdir / UNIT).exists()
    calls = box.calls_text()
    assert "enable" not in calls and "disable" not in calls
    assert box.panel_running()
    assert (box.unitdir / PANEL).read_text() == box.panel_unit_text


def test_install_refuses_version_mismatch(box):
    r = box.run("--install", "--execute", env={"FAKE_UCC_VERSION": "5.1.4"})
    assert r.returncode != 0
    assert not (box.appdir / "bin" / "5.0.3").exists()


def test_install_refuses_sha_mismatch(box):
    # The pin no longer equals the running panel binary either: refused first.
    r = box.run("--install", "--execute", env={"QFLIX_QBITTORRENT_SHA256": "0" * 64})
    assert r.returncode != 0 and "sha256" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_install_refuses_when_the_panel_runs_a_different_build(box):
    (box.home / "bin" / "qbittorrent-nox").write_text("#!/bin/sh\n# another build\n")
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and "pinned userdocs build" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_install_refuses_without_the_live_config(box):
    box.qconf.unlink()
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and "qBittorrent.conf" in r.stderr


# --- step 2: proof -----------------------------------------------------------------

def test_prove_boots_a_scratch_engine_measures_tasks_and_cleans_up(box):
    r = box.proved()
    assert "scratch engine answered v5.0.3" in r.stdout
    assert not (box.apps / ".prove" / "qbittorrent").exists()
    proof = json.loads((box.swap / "qbittorrent" / "proof.json").read_text())
    assert proof["delta"] == 12 and proof["ceiling"] == 2000 and proof["ok"] is True
    # never two engines on one profile: scratch HOME, profile and TMPDIR, fresh port
    home, prof, port, tmpdir = box.prooflog.read_text().strip().split("|")
    prove_root = _posix(box.apps / ".prove" / "qbittorrent")
    assert home.startswith(prove_root) and prof.startswith(prove_root) and tmpdir.startswith(prove_root)
    assert port != box.port
    assert box.panel_running() and box.api.calls == []    # live engine untouched
    assert "systemctl" not in box.calls_text()


def test_prove_scratch_profile_is_loopback_only_and_announces_nothing(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"QFLIX_KEEP_PROOF": "1"})
    assert r.returncode == 0, r.stderr
    conf = (box.apps / ".prove" / "qbittorrent" / "profile" / "qBittorrent" / "config"
            / "qBittorrent.conf").read_text()
    for line in ("WebUI\\Address=127.0.0.1", "Session\\InterfaceAddress=127.0.0.1",
                 "Session\\DHTEnabled=false", "Session\\PeXEnabled=false",
                 "Session\\LSDEnabled=false", "PortForwardingEnabled=false"):
        assert line in conf


def test_prove_refuses_a_wrong_version_binary(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_QBIT_VERSION": "5.1.4"})
    assert r.returncode != 0 and "--version" in r.stderr
    assert not (box.swap / "qbittorrent" / "proof.json").exists()


def test_prove_refuses_at_seventy_percent_of_ceiling(box):
    box.installed()
    # 1390 + 12 = 1402 >= 0.70 * 2000
    r = box.run("--prove", "--execute", env={"FAKE_TASKS": "1390"})
    assert r.returncode == 1 and "70%" in r.stderr
    assert not (box.swap / "qbittorrent" / "proof.json").exists()
    assert not (box.apps / ".prove" / "qbittorrent").exists()


# --- steps 3-6: swap ------------------------------------------------------------------

def test_swap_refuses_without_a_proof(box):
    box.installed()
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "prove" in r.stderr
    assert box.panel_running() and not box.suppressed()


def test_swap_refuses_unless_the_pending_swap_flip_is_deployed(box):
    box.proved()
    box.set_manifest(cls="ucc", dormant=False)
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert box.panel_running()


def test_swap_full_sequence(box):
    r = box.swapped()
    calls = box.calls_text()
    # panel stopped + disabled before the tracked unit starts; never uninstalled
    assert calls.index(f"systemctl --user disable --now {PANEL}") < \
        calls.index(f"systemctl --user enable --now {UNIT}")
    assert (box.unitdir / PANEL).read_text() == box.panel_unit_text
    backup = box.swap / "qbittorrent" / "panel-unit" / PANEL
    assert backup.read_text() == box.panel_unit_text
    assert not box.panel_running() and box.native_running()
    assert (box.unitdir / UNIT).read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    # listen sets: WebUI (swapstate, audited by runtime parity) + BitTorrent rows
    assert (box.swap / "qbittorrent" / "listen-set.before").read_text() == f"*:{box.port}\n"
    assert (box.swap / "qbittorrent" / "bt-listen.before").read_text() == \
        f"192.0.2.10%bond0:{BTPORT}\n"
    assert "bypass_local_auth=false" in (box.swap / "qbittorrent" / "local-auth").read_text()
    st = box.swapstate()
    assert st["ucc_version"] == "5.0.3" and st["port"] == int(box.port)
    assert st["swap_date"] and st["soak_until"] and st["rollback_window"] == "open"
    assert set(box.suppressed()) == SUPPRESSED            # still muted (pending-swap)
    # only the ACTIVE set was paused, and exactly it resumed
    assert box.api.verbs("stop") == [("stop", ACTIVE)]
    assert box.api.verbs("start") == [("start", ACTIVE)]
    assert box.api.torrents["bbb2"] == "stoppedUP" and box.api.torrents["ddd4"] == "pausedDL"
    assert not (box.swap / "qbittorrent" / "quiesced.hashes").exists()
    assert "appctl stop" not in calls and "appctl start" not in calls
    assert "elapsed=" in r.stdout


def test_swap_suppresses_and_pauses_before_stopping_the_panel(box):
    box.proved()
    wrapper = box.stub / "systemctl"
    orig = wrapper.read_text()
    sup = _posix(box.state / "push-suppress.json")
    q = _posix(box.swap / "qbittorrent" / "quiesced.hashes")
    wrapper.write_text(orig.replace(
        '  disable)', f'  disable) {{ grep -q canary-qbit-stall "{sup}" && [ -s "{q}" ]; }} '
                     f'|| echo "stop-before-suppress-or-pause" >> "{_posix(box.calls)}";'), newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stderr
    assert "stop-before-suppress-or-pause" not in box.calls_text()


def test_swap_refuses_when_localhost_bypasses_auth(box):
    box.proved()
    box.write_conf(**{"WebUI\\LocalHostAuth": "false"})
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "bypass_local_auth" in r.stderr
    assert box.panel_running() and not box.suppressed()


def test_swap_refuses_a_subnet_auth_whitelist(box):
    box.proved()
    box.write_conf(**{"WebUI\\AuthSubnetWhitelistEnabled": "true"})
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "AuthSubnetWhitelistEnabled" in r.stderr
    assert box.panel_running() and not box.suppressed()


def test_swap_refuses_container_paths_in_the_config(box):
    box.proved()
    box.write_conf(**{"Session\\DefaultSavePath": "/downloads/qbittorrent"})
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "container path" in r.stderr
    assert box.panel_running() and not box.suppressed()


def test_swap_refuses_when_the_port_secret_drifts_from_the_config(box):
    box.proved()
    box.write_conf(**{"WebUI\\Port": "17999"})
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "WebUI" in r.stderr
    assert box.panel_running() and not box.suppressed()


def test_swap_refuses_before_muting_when_the_api_login_fails(box):
    box.proved()
    box.api.refuse_login = True
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "API" in r.stderr
    assert box.panel_running() and not box.suppressed()
    assert "disable" not in box.calls_text()


def test_swap_aborts_when_the_panel_never_exits_and_restores_service(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_PANEL_STICKS": "1"})
    assert r.returncode != 0 and "did not exit" in r.stderr
    calls = box.calls_text()
    assert f"enable --now {UNIT}" not in calls
    assert f"systemctl --user enable --now {PANEL}" in calls
    assert not box.native_running() and box.panel_running()
    assert not box.suppressed()
    assert box.api.verbs("start") == [("start", ACTIVE)]          # torrents resumed


def test_swap_waits_out_the_webui_boot_bind_race_with_a_restart(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_BIND_RACE": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"systemctl --user restart {UNIT}" in box.calls_text()
    assert "bind race" in r.stdout


def test_swap_parity_failure_keeps_suppression_and_the_pause_record(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_PROBE_FAILS": "1"})
    assert r.returncode != 0 and "--rollback" in r.stderr and "WebUI" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED
    assert (box.swap / "qbittorrent" / "quiesced.hashes").exists()
    # every backoff step but the last restarted the tracked unit
    assert box.calls_text().count(f"systemctl --user restart {UNIT}") == 2


def test_swap_refuses_a_second_engine_on_the_profile(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_PANEL_STICKS": "1", "QFLIX_STOP_TIMEOUT_S": "1"})
    assert r.returncode != 0
    assert not box.native_running()                       # never enabled beside the panel


def test_swap_is_resumable_when_already_swapped(box):
    box.swapped()
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stderr
    assert "already" in r.stdout
    assert box.calls_text().count(f"disable --now {PANEL}") == 1


def test_swap_uses_the_4x_pause_resume_verbs_on_404(box):
    box.api.legacy = True
    box.swapped()
    assert box.api.verbs("pause") == [("pause", ACTIVE)]
    assert box.api.verbs("resume") == [("resume", ACTIVE)]


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

def test_rollback_masks_before_stopping_and_re_enables_the_panel(box):
    box.swapped()
    stops_before = len(box.api.verbs("stop"))
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls_text()
    assert calls.index(f"systemctl --user mask {UNIT}") < calls.index(f"systemctl --user stop {UNIT}")
    assert calls.rindex(f"systemctl --user enable --now {PANEL}") > calls.index(f"systemctl --user stop {UNIT}")
    assert _masked(box.unitdir / UNIT)
    assert not box.native_running() and box.panel_running()
    assert len(box.api.verbs("stop")) == stops_before + 1       # quiesced before the stop
    assert box.suppressed() == {}
    assert not (box.swap / "qbittorrent" / "quiesced.hashes").exists()
    assert "elapsed=" in r.stdout


def test_rollback_pauses_until_manifest_reverted(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--rollback", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 10 and "revert" in r.stderr
    assert not box.native_running() and not box.panel_running()
    assert box.calls_text().count(f"enable --now {PANEL}") == 0
    assert set(box.suppressed()) == SUPPRESSED
    box.set_manifest(swap_state="pending-swap")
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.panel_running() and box.suppressed() == {}


def test_rollback_restores_a_missing_panel_unit_from_the_backup(box):
    box.swapped()
    (box.unitdir / PANEL).unlink()                 # e.g. the panel's repair removed it
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.unitdir / PANEL).read_text() == box.panel_unit_text
    assert "restored" in r.stdout and box.panel_running()


def test_rollback_stays_suppressed_if_the_panel_webui_never_answers(box):
    box.swapped()
    r = box.run("--rollback", "--execute", env={"FAKE_PROBE_FAILS": "1"})
    assert r.returncode != 0 and "still suppressed" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED


def test_drill_rollback_then_reswap_unmasks(box):
    box.swapped()
    assert box.run("--rollback", "--execute").returncode == 0
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"systemctl --user unmask {UNIT}" in box.calls_text()
    assert not _masked(box.unitdir / UNIT)
    assert box.native_running() and not box.panel_running()


def test_rollback_with_nothing_swapped_is_harmless(box):
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.panel_running() and box.suppressed() == {}
    assert box.api.verbs("stop") == []


# --- runtime parity: two engines on one profile (ticket "Tests") --------------------

def test_runtime_parity_alarms_on_two_qbittorrent_nox_pids(tmp_path):
    """With the REAL manifest entry (minus pending-swap), a panel engine woken
    beside the tracked one is a runtime-parity violation (I-6)."""
    from lib import runtime_parity as rp
    from lib.manifest import load as load_manifest

    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    data["apps"]["qbittorrent"].pop("swap_state", None)
    mp = tmp_path / "apps.yaml"
    mp.write_text(yaml.safe_dump(data), encoding="utf-8")
    app = load_manifest(mp).app("qbittorrent")
    assert rp.applies(app)

    uid = 1120
    nox = "/home/u/.apps/qbittorrent/bin/current/qbittorrent-nox"
    root = tmp_path / "proc"

    def proc(pid, cgroup, cmd):
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "status").write_text(f"Name:\tqbittorrent-nox\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (d / "cgroup").write_text(cgroup + "\n")
        (d / "cmdline").write_text(cmd + "\0")
        (d / "stat").write_text(f"{pid} (qbittorrent-nox) S 1 1 1 0 -1 0\n")

    base = "0::/user.slice/user-1120.slice/app.slice/"
    proc(500, base + UNIT, nox)

    class Host(rp.Host):
        def uid(self):
            return uid

        def run(self, cmd):
            if cmd[0] == "systemctl":
                return 0, f"MainPID=500\nExecStart={{ path={nox} ; argv[]={nox} ; }}\n"
            if cmd[0] == "pgrep":
                return 0, "500\n"
            if cmd[0] == "ss":
                return 0, ('LISTEN 0 50 *:17041 *:* users:(("qbittorrent-nox",pid=%s,fd=23))\n'
                           % self.owner)
            raise AssertionError(cmd)

    h = Host(root)
    h.owner = 500
    assert rp.check(app, h, port=17041) == []            # one engine: parity holds

    proc(600, base + PANEL, "/home/u/bin/qbittorrent-nox")   # the panel unit woke
    h.owner = 600
    v = rp.check(app, h, port=17041)
    assert any("dormant container woken" in x and "600" in x for x in v), v
    assert any("port 17041 owned by pid 600" in x for x in v), v
