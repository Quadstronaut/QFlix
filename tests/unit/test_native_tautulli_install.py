"""scripts/configure/309-native-tautulli-install.sh (QFLX-34, A10, spec 5.9 + row 10).

Subprocess tests against fakes that MODEL the box (same approach as the pilot,
test_native_unpackerr_install.py and test_native_flaresolverr_install.py): a fake
/proc tree (the UCC container's pid in a docker cgroup, the native pid in the
unit's cgroup) and fake appctl / systemctl / ss / ps / curl / hostpolicy that
mutate it like the real tools. The proof boots a REAL local HTTP server that
plays Tautulli (reads the copy's config.ini for its bind host and api key,
answers get_tautulli_info, and records what it was booted with), so the proof
path runs end to end over loopback against a REAL sqlite fixture sanitized by
the REAL native_sanitize.py.

Real python runs swapstate.py and suppression.py: swap state and
push-suppress.json are the real files.
"""
from __future__ import annotations

import hashlib
import importlib.util  # noqa: F401  (kept for parity with sibling tests)
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "configure" / "309-native-tautulli-install.sh"
GOLDEN_UNIT = REPO / "scripts" / "maint" / "systemd" / "qflix-tautulli.service"
UNIT = "qflix-tautulli.service"
PORT = "17014"
PUBLIC = "169.150.251.170"
GATEWAY = "172.17.0.1"
VERSION = "2.18.2"

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


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# `venv/bin/python`: registers fake /proc entries for itself under the SHELL pid
# (exec keeps it), then becomes the server. `-c` is the import smoke test.
FAKE_PYTHON = r'''#!/usr/bin/env bash
if [ "$1" = -c ]; then exit "${FAKE_IMPORT_RC:-0}"; fi
export FAKE_ROOT_PID=$$
mkdir -p "$QFLIX_PROC/$$"
printf 'Name:\tpython\nPPid:\t1\n' > "$QFLIX_PROC/$$/status"
for i in $(seq 1 "${FAKE_TT_TASKS:-30}"); do mkdir -p "$QFLIX_PROC/$$/task/$i"; done
"$QFLIX_PYTHON" "$@" &
child=$!
# A real exec replaces this shell; under Git Bash it spawns a native child, so
# forward the stop signal or the "server" outlives the test and holds its pipes.
trap 'kill "$child" 2>/dev/null; exit 0' TERM INT
wait "$child"
'''

# Tautulli.py: bind http_host:--port from <datadir>/config.ini, answer
# get_tautulli_info for the config's api_key, record what it booted with.
FAKE_TAUTULLI = r'''
import json, os, sqlite3, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

args = sys.argv[1:]
datadir = args[args.index("--datadir") + 1]
port = int(args[args.index("--port") + 1])
cfg = {}
for line in open(os.path.join(datadir, "config.ini"), encoding="utf-8"):
    if " = " in line:
        k, v = line.rstrip("\r\n").split(" = ", 1)
        cfg[k.strip()] = v.strip().strip('"')
con = sqlite3.connect(os.path.join(datadir, "tautulli.db"))
boot = {
    "datadir": datadir.replace("\\", "/"), "host": cfg.get("http_host"), "port": port,
    "notifiers": con.execute("select count(*) from notifiers").fetchone()[0],
    "newsletters_active": con.execute("select count(*) from newsletters where active != 0").fetchone()[0],
    "check_github": cfg.get("check_github"), "args": args,
}
con.close()
with open(os.path.join(datadir, "boot.json"), "w") as fh:
    json.dump(boot, fh)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        root = cfg.get("http_root", "").rstrip("/")
        ok = (u.path == root + "/api/v2" and q.get("cmd") == ["get_tautulli_info"]
              and q.get("apikey") == [cfg.get("api_key")])
        body = json.dumps({"response": {"result": "success" if ok else "error",
                                        "data": {"tautulli_version": os.environ.get("FAKE_TT_VERSION", "v2.18.2")}
                                        if ok else {}}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

HTTPServer((cfg.get("http_host") or "127.0.0.1", port), H).serve_forever()
'''

# QFLIX_VENV_CMD <dest> <requirements>: the real thing is `python -m venv` + pip.
FAKE_VENV_CMD = r'''#!/usr/bin/env bash
echo "venv $1 $2" >> "$FAKE_CALLS"
[ "${FAKE_VENV_FAILS:-0}" = 1 ] && exit 1
[ -f "$2" ] || exit 2
mkdir -p "$1/bin"
cp "$FAKE_PYTHON_SRC" "$1/bin/python"
chmod 0755 "$1/bin/python"
'''

CONFIG_INI = """\
[General]
http_host = 0.0.0.0
http_port = 8181
http_root = /tautulli
api_key = livekey123
pms_ip = 172.17.0.1
pms_port = 17025
pms_ssl = 0
pms_url = http://172.17.0.1:17025
pms_url_manual = 1
cache_dir = /config/cache
log_dir = /config/logs
backup_dir = /config/backups
newsletter_dir = /config/newsletters
exports_dir = ""
check_github = 1
check_github_on_startup = 1
refresh_users_on_startup = 1
refresh_libraries_on_startup = 1
"""


def _make_db(path: Path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE notifiers (id INTEGER PRIMARY KEY, agent_id INTEGER, notifier_name TEXT);
        INSERT INTO notifiers (agent_id, notifier_name) VALUES (1, 'hook-a'), (2, 'hook-b');
        CREATE TABLE newsletters (id INTEGER PRIMARY KEY, agent_id INTEGER, active INTEGER DEFAULT 1);
        INSERT INTO newsletters (agent_id, active) VALUES (1, 1);
        CREATE TABLE session_history (id INTEGER PRIMARY KEY, note TEXT);
        INSERT INTO session_history (note) VALUES ('row-1'), ('row-2'), ('row-3');
    """)
    con.commit()
    con.close()


class Box:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.apps = self.home / ".apps"
        self.appdir = self.apps / "tautulli"
        self.unitdir = self.home / ".config" / "systemd" / "user"
        self.envdir = self.home / ".config" / "qflix"
        self.state = self.home / ".opt" / "maint"
        self.swap = self.state / "swap"
        self.secrets = self.home / "secrets"
        self.proc = tmp / "proc"
        self.stub = tmp / "stub"
        self.calls = tmp / "calls.log"
        self.manifest = self.state / "apps.yaml"
        self.frag = self.apps / "nginx" / "proxy.d" / "tautulli.conf"
        for d in (self.appdir, self.unitdir, self.envdir, self.swap, self.proc,
                  self.stub, self.secrets, self.frag.parent):
            d.mkdir(parents=True, exist_ok=True)
        (self.secrets / "tautulli.port").write_text(PORT + "\n")
        self.cfg.write_text(CONFIG_INI, newline="\n")
        _make_db(self.appdir / "tautulli.db")
        self.frag.write_text(f"location ^~ /tautulli {{\n    proxy_pass       http://127.0.0.1:{PORT};\n}}\n")
        self.uid = _uid()
        self.set_manifest(swap_state="pending-swap")
        self.container_up()
        self._tarball()
        self._stubs()

    @property
    def cfg(self) -> Path:
        return self.appdir / "config.ini"

    # --- state ---------------------------------------------------------------
    def _proc(self, pid: int, cgroup: str, cmd: str):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(f"Name:\tx\nUid:\t{self.uid}\t{self.uid}\nPPid:\t1\n", newline="\n")
        (d / "cgroup").write_text(cgroup + "\n", newline="\n")
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
        (d / "task" / "1").mkdir(parents=True, exist_ok=True)

    def container_up(self):
        self._proc(9001, "0::/system.slice/docker-abc.scope", "python3 /app/tautulli/Tautulli.py --datadir /config")
        (self.proc / "9001" / "environ").write_bytes(
            b"TZ=Europe/Amsterdam\0LANG=en_US.UTF-8\0SECRET_TOKEN=hunter2\0PATH=/usr/bin\0")

    def container_running(self) -> bool:
        return (self.proc / "9001").exists()

    def native_running(self) -> bool:
        return (self.proc / "9002").exists()

    def hold_db(self, pid=9500):
        """A process (not matching the app cmdline) that still has the db open."""
        d = self.proc / str(pid) / "fd"
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink("/config/tautulli.db", d / "3")
        except (OSError, NotImplementedError):
            pytest.skip("cannot create symlinks here")

    def set_manifest(self, *, cls="systemd", swap_state=None, dormant=True):
        lines = ["apps:", "  tautulli:", f"    class: {cls}", "    ucc_slug: tautulli"]
        if cls == "systemd":
            lines.append(f"    unit: {UNIT}")
        if dormant:
            lines.append("    ucc_dormant: true")
        if swap_state:
            lines.append(f"    swap_state: {swap_state}")
        self.manifest.write_text("\n".join(lines) + "\n", newline="\n")

    def calls_text(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""

    def live_hashes(self) -> dict:
        return {n: _sha(self.appdir / n) for n in ("config.ini", "tautulli.db")}

    # --- fakes ---------------------------------------------------------------
    def _tarball(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data, mode in (
                (f"Tautulli-{VERSION}/", None, 0o755),
                (f"Tautulli-{VERSION}/Tautulli.py", FAKE_TAUTULLI.encode(), 0o755),
                (f"Tautulli-{VERSION}/requirements.txt", b"cherrypy==18.10.0\n", 0o644),
                (f"Tautulli-{VERSION}/plexpy/__init__.py", b"", 0o644),
            ):
                ti = tarfile.TarInfo(name)
                if data is None:
                    ti.type = tarfile.DIRTYPE
                    ti.mode = mode
                    tf.addfile(ti)
                    continue
                ti.size, ti.mode = len(data), mode
                tf.addfile(ti, io.BytesIO(data))
        self.payload = self.tmp / "payload.tgz"
        self.payload.write_bytes(buf.getvalue())
        self.sha = hashlib.sha256(buf.getvalue()).hexdigest()
        self.fake_python = self.tmp / "fake-python"
        self.fake_python.write_text(FAKE_PYTHON, newline="\n")

    def _w(self, name: str, body: str):
        p = self.stub / name
        p.write_text("#!/usr/bin/env bash\n" + body, newline="\n")
        p.chmod(0o755)

    def _stubs(self):
        P, C, A = _posix(self.proc), _posix(self.calls), _posix(self.cfg)
        self._w("appctl", f'''echo "appctl $*" >> "{C}"
case "$1" in
  version) echo '{{"data": {{"version": "'"${{FAKE_UCC_VERSION:-{VERSION}}}"'"}}, "result": true}}' ;;
  stop) [ "${{FAKE_CONTAINER_STICKS:-0}}" = 1 ] || rm -rf "{P}/9001"
        [ "${{FAKE_DB_STAYS:-0}}" = 1 ] && {{ mkdir -p "{P}/9500/fd"; ln -sf /config/tautulli.db "{P}/9500/fd/3"; }} ;;
  start) # the container only works with ITS config: gateway-published host, /config paths
         h=$(awk -F' = ' '$1=="http_host"{{print $2; exit}}' "{A}" | tr -d '\\r')
         if [ "$h" != 0.0.0.0 ] || grep -E '^[a-z_]+ = "?{_posix(self.appdir)}' "{A}" >/dev/null; then
           echo "container cannot start with a native-shaped config.ini" >&2; exit 1
         fi
         mkdir -p "{P}/9001/task/1"
         printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9001/status"
         echo "0::/system.slice/docker-abc.scope" > "{P}/9001/cgroup"
         printf 'python3\\0/app/tautulli/Tautulli.py\\0' > "{P}/9001/cmdline" ;;
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
            printf '/h/.apps/tautulli/bin/current/venv/bin/python\\0/h/.apps/tautulli/bin/current/Tautulli.py\\0' > "{P}/9002/cmdline"
          fi ;;
  stop) rm -rf "{P}/9002" ;;
  mask) [ -s "$U/$2" ] && [ ! -L "$U/$2" ] && {{ echo "Failed to mask unit: File $U/$2 already exists." >&2; exit 1; }}
        ln -sf /dev/null "$U/$2" ;;
  unmask) {{ [ -L "$U/$2" ] || [ ! -s "$U/$2" ]; }} && rm -f "$U/$2" ;;
esac
exit 0
''')
        # The container publishes loopback + public + gateway; the native app listens
        # on whatever the (edited) config.ini says. FAKE_SS_EXTRA adds foreign rows.
        self._w("ss", f'''if [ -d "{P}/9001" ]; then
  echo "LISTEN 0 65535 {PUBLIC}:{PORT} 0.0.0.0:*"
  echo "LISTEN 0 65535 {GATEWAY}:{PORT} 0.0.0.0:*"
  echo "LISTEN 0 65535 127.0.0.1:{PORT} 0.0.0.0:*"
fi
if [ -d "{P}/9002" ]; then
  h=$(awk -F' = ' '$1=="http_host"{{print $2; exit}}' "{A}" | tr -d '\\r')
  [ "${{FAKE_NATIVE_WIDE:-0}}" = 1 ] && h=0.0.0.0
  echo "LISTEN 0 4096 $h:{PORT} 0.0.0.0:*"
fi
[ -n "${{FAKE_SS_EXTRA:-}}" ] && echo "$FAKE_SS_EXTRA"
exit 0
''')
        self._w("ps", 'n=${FAKE_TASKS:-1000}; for i in $(seq 1 "$n"); do echo x; done\n')
        self._w("curl", 'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                        f'cp "{_posix(self.payload)}" "$out"\n')
        # live probe: reads the -K - config from stdin, answers like get_tautulli_info
        self._w("probe", f'''cat >/dev/null
[ "${{FAKE_PROBE_FAILS:-0}}" = 1 ] && exit 22
if [ -d "{P}/9001" ] || [ -d "{P}/9002" ]; then
  echo '{{"response": {{"result": "success", "data": {{"tautulli_version": "'"${{FAKE_TT_VERSION:-v{VERSION}}}"'"}}}}}}'
else exit 7; fi
''')
        self._w("plex", '[ "${FAKE_PLEX_DOWN:-0}" = 1 ] && exit 7\necho \'<MediaContainer size="0" machineIdentifier="x"/>\'\n')
        self._w("hostpolicy", f'''echo "hostpolicy $*" >> "{C}"
case "$1" in
  preflight) [ -n "${{FAKE_PROFILE-ultra}}" ] || exit 2; echo "${{FAKE_PROFILE-ultra}}" ;;
  in-window) exit "${{FAKE_INWINDOW_RC:-1}}" ;;
  task-ceiling) echo "${{FAKE_CEILING:-2000}}" ;;
esac
''')
        self._w("venvcmd", FAKE_VENV_CMD.split("\n", 1)[1])

    # --- run -----------------------------------------------------------------
    def run(self, *args, env=None, timeout=900):
        marker = self.tmp / "host.id"
        marker.write_text("test-slot\n")
        e = dict(os.environ,
                 HOME=_posix(self.home), USERPROFILE=str(self.home),
                 QFLIX_HOST_ID_FILE=_posix(marker),
                 QFLIX_APPS_DIR=_posix(self.apps),
                 QFLIX_UNIT_DIR=_posix(self.unitdir),
                 QFLIX_ENV_DIR=_posix(self.envdir),
                 QFLIX_SWAP_DIR=_posix(self.swap),
                 QFLIX_SECRETS_DIR=_posix(self.secrets),
                 MANITOBA_STATE_DIR=_posix(self.state),
                 QFLIX_MANIFEST=_posix(self.manifest),
                 QFLIX_NGINX_FRAGMENT=_posix(self.frag),
                 QFLIX_PROC=_posix(self.proc),
                 QFLIX_PYTHON=_posix(sys.executable),
                 QFLIX_APPCTL=_posix(self.stub / "appctl"),
                 QFLIX_SYSTEMCTL=_posix(self.stub / "systemctl"),
                 QFLIX_SS=_posix(self.stub / "ss"),
                 QFLIX_PS=_posix(self.stub / "ps"),
                 QFLIX_CURL=_posix(self.stub / "curl"),
                 QFLIX_PROBE_CURL=_posix(self.stub / "probe"),
                 QFLIX_PLEX_CURL=_posix(self.stub / "plex"),
                 QFLIX_HOSTPOLICY=_posix(self.stub / "hostpolicy"),
                 QFLIX_VENV_CMD=_posix(self.stub / "venvcmd"),
                 FAKE_CALLS=_posix(self.calls),
                 FAKE_PYTHON_SRC=_posix(self.fake_python),
                 QFLIX_TAUTULLI_SHA256=self.sha,
                 QFLIX_POLL_S="0.2", QFLIX_SETTLE_S="0.3",
                 QFLIX_STOP_TIMEOUT_S="8", QFLIX_PROOF_TIMEOUT_S="120")
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
        p = self.swap / "tautulli" / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def cfg_value(self, key: str) -> str:
        for line in self.cfg.read_text().splitlines():
            if line.startswith(key + " = "):
                return line.split(" = ", 1)[1].strip()
        return ""


@pytest.fixture()
def box(tmp_path):
    return Box(tmp_path)


SUPPRESSED = {"tautulli", "canary-tautulli-plex-link"}
PMS_LINES = [l for l in CONFIG_INI.splitlines() if l.startswith("pms_")]


# --- static -------------------------------------------------------------------

def test_bash_syntax():
    r = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_pins_exact_version_and_sha256_matching_versions_env():
    text = INSTALLER.read_text(encoding="utf-8")
    ver = next(l.split("=", 1)[1].strip() for l in
               (REPO / "versions.env").read_text(encoding="utf-8").splitlines()
               if l.startswith("TAUTULLI_VERSION="))
    assert ver == VERSION and f'VERSION="{ver}"' in text
    assert 'SHA256="cde285c9954bcdd7680f5d9d268ccde2751c8c84e2146db25f0c83e5d3e8f293"' in text
    assert "archive/refs/tags/v${VERSION}.tar.gz" in text


def test_240_stages_and_deploys_the_installer_and_the_sanitizer():
    text = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    assert "    scripts/configure/309-native-tautulli-install.sh \\\n" in text
    assert "    scripts/maint/native_sanitize.py \\\n" in text
    assert ("~/scripts/configure/309-native-tautulli-install.sh\n"
            "chmod +x ~/scripts/configure/309-native-tautulli-install.sh") in text
    assert '"$STG"/scripts/maint/native_sanitize.py ~/scripts/maint/native_sanitize.py' in text


def test_installer_never_calls_the_panel_tool_directly():
    code = [l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith("#")]
    assert not any("app-tautulli" in l for l in code)


def test_installer_binds_loopback_only_and_never_wide():
    code = "\n".join(l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
                     if not l.strip().startswith("#"))
    assert 'BIND_HOST="127.0.0.1"' in code
    assert 'cfgtool set "$cfg" http_host "$BIND_HOST"' in code
    # the wildcard appears only as a value REFUSED / the container's original default
    for l in code.splitlines():
        if "0.0.0.0" in l:
            assert l.strip().startswith(("0.0.0.0:*", "[ -n \"$orig\" ] ||")) or "orig=" in l \
                or "wildcard" in l, l


def test_golden_unit_is_what_the_installer_renders(box):
    box.installed()
    staged = box.appdir / "native" / UNIT
    assert staged.read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    unit = GOLDEN_UNIT.read_text(encoding="utf-8")
    assert ("ExecStart=%h/.apps/tautulli/bin/current/venv/bin/python "
            "%h/.apps/tautulli/bin/current/Tautulli.py --datadir %h/.apps/tautulli "
            "--port ${TAUTULLI_PORT} --nolaunch --quiet --nofork\n") in unit
    assert "EnvironmentFile=%h/.config/qflix/tautulli.env" in unit
    assert "TasksMax" not in unit and "0.0.0.0" not in unit


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


# --- precheck -----------------------------------------------------------------------

def test_precheck_passes_and_installs_nothing(box):
    r = box.run("--precheck", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PRECHECK OK" in r.stdout
    assert not (box.appdir / "bin").exists()
    assert not list(box.apps.glob(".stage-*"))          # scratch dir removed
    assert "venv " not in box.calls_text()


def test_precheck_blocks_when_python_cannot_build_a_venv(box):
    r = box.run("--precheck", "--execute", env={"QFLIX_PYTHON": "/nonexistent/python3"})
    assert r.returncode == 3 and "BLOCKED" in r.stderr and "UCC" in r.stderr


# --- step 1: pin + install --------------------------------------------------------

def test_install_lays_out_release_venv_env_and_stages_unit_without_enabling(box):
    live = box.live_hashes()
    box.installed()
    cur = box.appdir / "bin" / "current"
    assert (box.appdir / "bin" / VERSION / "Tautulli.py").exists()      # tag dir flattened
    assert (box.appdir / "bin" / VERSION / "venv" / "bin" / "python").exists()
    assert (cur / "Tautulli.py").exists() and (cur / "venv" / "bin" / "python").exists()
    env = (box.envdir / "tautulli.env").read_text().splitlines()
    assert f"TAUTULLI_PORT={PORT}" in env and "MALLOC_ARENA_MAX=2" in env
    assert "TZ=Europe/Amsterdam" in env and "LANG=en_US.UTF-8" in env   # carried from the container
    assert not any("SECRET" in l or "hunter2" in l for l in env)
    assert not any("0.0.0.0" in l or l.startswith("HOST=") for l in env)
    assert (box.appdir / "native" / UNIT).exists()
    assert not (box.unitdir / UNIT).exists()
    assert "enable" not in box.calls_text()
    assert box.container_running()
    assert box.live_hashes() == live                                    # data untouched
    # the venv is built from the release's own pinned requirements
    assert "requirements.txt" in box.calls_text()


def test_install_refuses_version_mismatch_before_building_anything(box):
    r = box.run("--install", "--execute", env={"FAKE_UCC_VERSION": "2.17.0"})
    assert r.returncode != 0
    assert not (box.appdir / "bin" / VERSION).exists()
    assert "venv " not in box.calls_text()


def test_install_refuses_sha_mismatch(box):
    r = box.run("--install", "--execute", env={"QFLIX_TAUTULLI_SHA256": "0" * 64})
    assert r.returncode != 0 and "sha256" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_venv_build_failure_blocks_with_exit_3_and_installs_nothing(box):
    r = box.run("--install", "--execute", env={"FAKE_VENV_FAILS": "1"})
    assert r.returncode == 3 and "BLOCKED" in r.stderr
    assert not (box.appdir / "bin").exists() and not (box.envdir / "tautulli.env").exists()
    assert box.container_running()


def test_dependency_import_failure_blocks(box):
    r = box.run("--install", "--execute", env={"FAKE_IMPORT_RC": "1"})
    assert r.returncode == 3 and "BLOCKED" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_install_refuses_without_the_port_secret(box):
    (box.secrets / "tautulli.port").unlink()
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and "port" in r.stderr


# --- step 2: proof (sanitize fixture) --------------------------------------------------

def test_prove_boots_a_sanitized_copy_and_leaves_live_data_untouched(box):
    live = box.live_hashes()
    r = box.proved()
    assert "v2.18.2 answered on a sanitized copy" in r.stdout
    assert box.live_hashes() == live                       # config.ini + tautulli.db byte-identical
    assert not (box.apps / ".prove" / "tautulli").exists()  # the copy (it holds the api key) is destroyed
    proof = json.loads((box.swap / "tautulli" / "proof.json").read_text())
    assert proof["ok"] is True and proof["version"] == VERSION
    assert proof["delta"] == 30 and proof["ceiling"] == 2000      # tasks of the proof process tree
    assert proof["sanitize"]["counts"] == {"notifiers": 0, "newsletters": 0, "auto_update": 0}
    assert box.container_running()                         # live app untouched
    assert "appctl stop" not in box.calls_text()
    assert "systemctl" not in box.calls_text()


def test_proof_copy_is_inert_loopback_and_points_at_the_same_plex(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"QFLIX_KEEP_PROOF": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    data = box.apps / ".prove" / "tautulli" / "data"
    boot = json.loads((data / "boot.json").read_text())
    # what the proof process was booted with: the SANITIZED copy, loopback, a fresh port
    assert boot["notifiers"] == 0 and boot["newsletters_active"] == 0
    assert boot["check_github"] == "0"
    assert boot["host"] == "127.0.0.1" and str(boot["port"]) != PORT
    assert boot["datadir"].endswith("/.prove/tautulli/data")
    assert "--nolaunch" in boot["args"] and "--nofork" in boot["args"]
    # container paths were mapped into the copy; pms_* is byte-for-byte the live Plex target
    ini = (data / "config.ini").read_text()
    assert "cache_dir = " + _posix(data) in ini and "/config/" not in ini
    for line in PMS_LINES:
        assert line in ini.splitlines()
    assert "refresh_users_on_startup = 0" in ini and "refresh_libraries_on_startup = 0" in ini
    # the data was COPIED (rows survive), and the live db still carries its own notifiers
    con = sqlite3.connect(data / "tautulli.db")
    assert con.execute("select count(*) from session_history").fetchone()[0] == 3
    assert con.execute("select count(*) from notifiers").fetchone()[0] == 0
    con.close()
    live = sqlite3.connect(box.appdir / "tautulli.db")
    assert live.execute("select count(*) from notifiers").fetchone()[0] == 2
    live.close()
    assert box.cfg_value("http_host") == "0.0.0.0" and box.cfg_value("check_github") == "1"


def test_prove_requires_an_install(box):
    r = box.run("--prove", "--execute")
    assert r.returncode != 0 and "install" in r.stderr


def test_prove_refuses_when_the_configured_plex_is_unreachable_from_the_host(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_PLEX_DOWN": "1"})
    assert r.returncode != 0 and "Plex" in r.stderr
    assert not (box.swap / "tautulli" / "proof.json").exists()
    assert not (box.apps / ".prove" / "tautulli").exists()


def test_prove_refuses_when_the_copy_cannot_be_made(box):
    box.installed()
    (box.appdir / "tautulli.db").write_bytes(b"this is not a sqlite database" * 50)
    r = box.run("--prove", "--execute")
    assert r.returncode != 0
    assert not (box.swap / "tautulli" / "proof.json").exists()
    assert not (box.apps / ".prove" / "tautulli").exists()


def test_prove_refuses_a_build_that_reports_another_version(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_TT_VERSION": "v2.17.0"})
    assert r.returncode != 0 and "v2.17.0" in r.stderr
    assert not (box.swap / "tautulli" / "proof.json").exists()


def test_prove_refuses_at_seventy_percent_of_ceiling(box):
    box.installed()
    # 1380 + 30 = 1410 >= 0.70 * 2000
    r = box.run("--prove", "--execute", env={"FAKE_TASKS": "1380"})
    assert r.returncode == 1 and "70%" in r.stderr
    assert not (box.swap / "tautulli" / "proof.json").exists()
    assert not (box.apps / ".prove" / "tautulli").exists()


def test_prove_blocks_when_the_delta_is_too_big(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_TT_TASKS": "150"})
    assert r.returncode == 3 and "BLOCKED" in r.stderr and "delta" in r.stderr
    assert not (box.swap / "tautulli" / "proof.json").exists()


def test_prove_blocks_when_the_build_never_answers(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"QFLIX_PROOF_TIMEOUT_S": "2", "FAKE_TT_VERSION": ""})
    # an empty version string is not an answer the probe accepts as a success
    assert r.returncode != 0
    assert not (box.swap / "tautulli" / "proof.json").exists()


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


@pytest.mark.parametrize("frag", [None, "location /tautulli {\n    proxy_pass http://172.17.0.1:%s;\n}\n" % PORT])
def test_swap_refuses_when_ingress_is_not_nginx_to_loopback(box, frag):
    box.proved()
    if frag is None:
        box.frag.unlink()
    else:
        box.frag.write_text(frag)
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "D-4" in r.stderr
    assert box.container_running() and not box.suppressed()
    assert "appctl stop" not in box.calls_text()


def test_swap_full_sequence(box):
    pre_db = _sha(box.appdir / "tautulli.db")
    pre_cfg = box.cfg.read_text()
    r = box.swapped()
    calls = box.calls_text()
    assert calls.index("appctl stop tautulli") < calls.index(f"systemctl --user enable --now {UNIT}")
    assert not box.container_running() and box.native_running()
    assert (box.unitdir / UNIT).read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    # listen set: the container's three listeners were captured; the native app reproduces
    # the loopback one and the other two are recorded as operator-approved exceptions
    assert (box.swap / "tautulli" / "listen-set.before").read_text().split() == sorted(
        [f"{PUBLIC}:{PORT}", f"{GATEWAY}:{PORT}", f"127.0.0.1:{PORT}"])
    st = box.swapstate()
    assert st["exceptions"] == sorted([f"{PUBLIC}:{PORT}", f"{GATEWAY}:{PORT}"])
    assert st["ucc_version"] == VERSION and st["port"] == int(PORT)
    assert st["swap_date"] and st["soak_until"] and st["rollback_window"] == "open"
    # config edits: loopback bind, container paths mapped; pms_* untouched, byte for byte
    assert box.cfg_value("http_host") == "127.0.0.1"
    appdir = _posix(box.appdir)
    assert box.cfg_value("cache_dir") == f"{appdir}/cache"
    assert box.cfg_value("newsletter_dir") == f"{appdir}/newsletters"
    assert box.cfg_value("exports_dir") == '""'
    assert "/config/" not in box.cfg.read_text()
    for line in PMS_LINES:
        assert line in box.cfg.read_text().splitlines()
    assert box.cfg_value("http_port") == "8181"                 # the unit passes --port instead
    assert _sha(box.appdir / "tautulli.db") == pre_db           # db never rewritten by the swap
    # snapshot of the pre-swap state and the recorded original host
    assert (box.swap / "tautulli" / "snapshot" / "config.ini").read_text() == pre_cfg
    assert _sha(box.swap / "tautulli" / "snapshot" / "tautulli.db") == pre_db
    assert (box.swap / "tautulli" / "orig-http-host").read_text().strip() == "0.0.0.0"
    # app + dependent canary muted together, and STILL muted (pending-swap)
    assert set(box.suppressed()) == SUPPRESSED
    assert "elapsed=" in r.stdout


def test_swap_suppresses_before_stopping_the_container(box):
    box.proved()
    wrapper = box.stub / "appctl"
    orig = wrapper.read_text()
    wrapper.write_text(orig.replace(
        '  stop)', f'  stop) grep -q canary-tautulli-plex-link "{_posix(box.state)}/push-suppress.json" 2>/dev/null '
                  f'|| echo "stop-before-suppress" >> "{_posix(box.calls)}";'), newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stderr
    assert "stop-before-suppress" not in box.calls_text()


def test_swap_refuses_a_wildcard_container_listener(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_SS_EXTRA": f"LISTEN 0 4096 0.0.0.0:{PORT} 0.0.0.0:*"})
    assert r.returncode != 0 and "wildcard" in r.stderr
    assert box.container_running() and not box.suppressed()
    assert box.cfg_value("http_host") == "0.0.0.0"


def test_swap_refuses_a_config_with_unmappable_container_paths(box):
    box.proved()
    box.cfg.write_text(box.cfg.read_text() + "some_dir = /downloads/x\n", newline="\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "container paths" in r.stderr and "some_dir" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_aborts_when_container_never_exits_and_restores_service(box):
    box.proved()
    before = box.cfg.read_text()
    r = box.run("--swap", "--execute", env={"FAKE_CONTAINER_STICKS": "1"})
    assert r.returncode != 0 and "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert not box.native_running() and box.container_running()
    assert not box.suppressed()
    assert box.cfg.read_text() == before                     # no config edit happened


def test_swap_aborts_while_something_still_holds_the_db(box):
    box.proved()
    before = box.cfg.read_text()
    box.hold_db()      # skips where symlinks are unavailable
    r = box.run("--swap", "--execute", env={"FAKE_DB_STAYS": "1"})
    assert r.returncode != 0 and "db unused: no" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert box.cfg.read_text() == before and not box.suppressed()


def test_swap_parity_failure_is_reported_and_stays_suppressed(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_NATIVE_FAILS": "1"})
    assert r.returncode != 0 and "--rollback" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED


def test_swap_fails_parity_when_the_native_listens_wider_than_recorded(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_NATIVE_WIDE": "1"})
    assert r.returncode != 0 and "listen set differs" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED


def test_swap_fails_parity_when_the_api_never_reports_the_pinned_version(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_PROBE_FAILS": "1"}, timeout=900)
    assert r.returncode != 0 and "get_tautulli_info" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED


def test_swap_is_resumable_when_already_swapped(box):
    box.swapped()
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stderr
    assert "already" in r.stdout
    assert box.calls_text().count("appctl stop tautulli") == 1


# --- step 9: finish -----------------------------------------------------------------

def test_finish_refuses_while_manifest_still_pending_swap(box):
    box.swapped()
    r = box.run("--finish", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED


def test_finish_lifts_app_and_canary_together(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--finish", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 0, r.stderr
    assert box.suppressed() == {}


# --- rollback (0-5) -----------------------------------------------------------------

def test_rollback_step0_suppresses_and_masks_before_stopping_then_restores_the_config(box):
    pre_cfg = box.cfg.read_text()
    box.swapped()
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls_text()
    assert calls.index(f"systemctl --user mask {UNIT}") < calls.index(f"systemctl --user stop {UNIT}")
    assert _masked(box.unitdir / UNIT)
    assert not box.native_running() and box.container_running()
    assert calls.rindex("appctl start tautulli") > calls.index(f"systemctl --user stop {UNIT}")
    # the container-shaped config is back (the fake container refuses to start otherwise)
    assert box.cfg_value("http_host") == "0.0.0.0"
    assert box.cfg.read_text() == pre_cfg
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
    assert box.container_running() and box.cfg_value("http_host") == "0.0.0.0"


def test_rollback_stays_suppressed_if_the_container_is_not_ready(box):
    box.swapped()
    r = box.run("--rollback", "--execute", env={"FAKE_PROBE_FAILS": "1"}, timeout=900)
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
    assert box.cfg_value("http_host") == "127.0.0.1"
    assert (box.swap / "tautulli" / "orig-http-host").read_text().strip() == "0.0.0.0"


def test_rollback_with_nothing_swapped_is_harmless_and_leaves_the_config_alone(box):
    before = box.live_hashes()
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running()
    assert box.live_hashes() == before
