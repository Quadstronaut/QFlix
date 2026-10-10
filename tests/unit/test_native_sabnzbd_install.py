"""scripts/configure/308-native-sabnzbd-install.sh (QFLX-33, A9, spec 5.9).

Subprocess tests against fakes that MODEL the box (the approach of the pilot and
of test_native_flaresolverr_install.py): a fake /proc tree (the UCC container's
pid in a docker cgroup, the native pid in the unit's cgroup) and fake appctl /
systemctl / systemd-run / ss / ps / ldd / fuser / curl / hostpolicy that mutate
it like the real tools. A fake SAB API keeps the queue's paused state in a file
so pause / resume / re-poll are exercised; the proof boots a REAL local HTTP
server that plays SABnzbd (/sabnzbd/ 200, api?mode=version) over loopback, and
the helper fixture really runs under the (fake) systemd-run with the unit PATH.

Real python runs swapstate.py, suppression.py and native_sanitize.py: swap
state, push-suppress.json and the sanitized proof ini are the real files.
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
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "configure" / "308-native-sabnzbd-install.sh"
GOLDEN_UNIT = REPO / "scripts" / "maint" / "systemd" / "qflix-sabnzbd.service"
UNIT = "qflix-sabnzbd.service"
PORT = "17007"
KEY = "0123456789abcdef0123456789abcdef"
PUBLIC = f"203.0.113.9:{PORT}"
SUPPRESSED = {"sabnzbd", "canary-sab-stall", "canary-thread-ceiling"}

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _posix(p) -> str:
    return Path(p).as_posix()


def _masked(p: Path) -> bool:
    f = _posix(p)
    return subprocess.run(["bash", "-c", f'[ -L "{f}" ] || {{ [ -f "{f}" ] && [ ! -s "{f}" ]; }}'],
                          capture_output=True).returncode == 0


def _uid() -> str:
    return subprocess.run(["bash", "-c", "id -u"], capture_output=True, text=True).stdout.strip()


# The fake SABnzbd.py: a loopback HTTP server that answers like SAB.
FAKE_SAB = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
args = sys.argv[1:]
host, _, port = args[args.index("--server") + 1].rpartition(":")
assert "--config-file" in args

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/sabnzbd/api", "/api"):
            q = parse_qs(u.query)
            body = json.dumps({"version": "5.1.3"} if q.get("mode") == ["version"] else {}).encode()
        elif u.path.startswith("/sabnzbd"):
            body = b"<html>login</html>"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

HTTPServer((host, int(port)), H).serve_forever()
'''

LIVE_INI = """__version__ = 19
[misc]
check_new_rel = 1
host = ::
port = 8080
username = admin
password = hunter2
api_key = {key}
download_dir = {dl}/incomplete
complete_dir = {dl}/complete
script_dir = ""
admin_dir = admin
log_dir = logs
pause_on_post_processing = 0
[servers]
[[news.example]]
enable = 1
[categories]
[[sonarr]]
dir = sonarr
"""


class Box:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.apps = self.home / ".apps"
        self.appdir = self.apps / "sabnzbd"
        self.unitdir = self.home / ".config" / "systemd" / "user"
        self.envdir = self.home / ".config" / "qflix"
        self.state = self.home / ".opt" / "maint"
        self.swap = self.state / "swap"
        self.secrets = self.home / "secrets"
        self.proc = tmp / "proc"
        self.stub = tmp / "stub"
        self.calls = tmp / "calls.log"
        self.qstate = tmp / "queue-paused"
        self.manifest = self.state / "apps.yaml"
        self.downloads = self.home / "downloads" / "sabnzbd"
        for d in (self.appdir / "admin", self.unitdir, self.envdir, self.swap, self.proc,
                  self.stub, self.secrets, self.downloads / "incomplete", self.downloads / "complete"):
            d.mkdir(parents=True, exist_ok=True)
        (self.secrets / "sabnzbd.port").write_text(PORT + "\n")
        (self.secrets / "sabnzbd.key").write_text(KEY + "\n")
        (self.secrets / "net.app_host").write_text("172.17.0.1\n")
        self.write_ini()
        (self.appdir / "admin" / "history1.db").write_bytes(b"SQLite format 3\0")
        self.qstate.write_text("false")
        self.uid = _uid()
        self.set_manifest(swap_state="pending-swap")
        self.container_up()
        self._payloads()
        self._stubs()

    # --- state ---------------------------------------------------------------
    def write_ini(self, **repl):
        text = LIVE_INI.format(key=KEY, dl=_posix(self.downloads))
        for k, v in repl.items():
            text = "\n".join(f"{k} = {v}" if l.split(" = ")[0] == k else l
                             for l in text.splitlines()) + "\n"
        (self.appdir / "sabnzbd.ini").write_text(text, newline="\n")

    def ini(self) -> str:
        return (self.appdir / "sabnzbd.ini").read_text()

    def _proc(self, pid: int, cgroup: str, cmd: str):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(f"Name:\tx\nUid:\t{self.uid}\t{self.uid}\nPPid:\t1\n", newline="\n")
        (d / "cgroup").write_text(cgroup + "\n", newline="\n")
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
        (d / "task" / "1").mkdir(parents=True, exist_ok=True)

    def container_up(self):
        self._proc(9001, "0::/system.slice/docker-abc.scope",
                   "python3 /app/sabnzbd/SABnzbd.py --config-file /config --server ::")
        (self.proc / "9001" / "environ").write_bytes(
            b"HOME=/config\0PYTHONIOENCODING=utf-8\0TZ=Etc/UTC\0SECRET=nope nope\0")

    def container_running(self) -> bool:
        return (self.proc / "9001").exists()

    def native_running(self) -> bool:
        return (self.proc / "9002").exists()

    def set_manifest(self, *, cls="systemd", swap_state=None, dormant=True):
        lines = ["apps:", "  sabnzbd:", f"    class: {cls}", "    ucc_slug: sabnzbd"]
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
    @staticmethod
    def _tar(members, mode="w:gz") -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode=mode) as tf:
            for name, data, perm in members:
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode = len(data), perm
                tf.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    def _payloads(self):
        def sh(body):
            return ("#!/usr/bin/env bash\n" + body).encode()
        par2 = sh('c="$1"; shift; while [ "${1#-}" != "$1" ]; do shift; done\n'
                  'case "$c" in create) cp "$2" "$1.data" ;;\n'
                  '  repair) [ "${FAKE_PAR2_BROKEN:-0}" = 1 ] || cp "$1.data" "$2" ;; esac\n')
        unrar = sh('# x -inul -o+ ARC DEST/\ncp "$4" "$5/orig.bin"\n')
        rar = sh('# a -inul -ep ARC FILE\ncp "$5" "$4"\n')
        sevenz = sh('c="$1"; shift\ncase "$c" in a) cp "$3" "$2" ;;\n'
                    '  x) out=""; for a in "$@"; do case "$a" in -o*) out="${a#-o}";; esac; done\n'
                    '     mkdir -p "$out"; cp "${@: -1}" "$out/orig.bin" ;; esac\n')
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w") as zf:
            zi = zipfile.ZipInfo("par2")
            zi.external_attr = 0o755 << 16
            zf.writestr(zi, par2)
        self.payloads = {
            "SABnzbd-5.1.3-src.tar.gz": self._tar([
                ("SABnzbd-5.1.3/SABnzbd.py", FAKE_SAB.encode(), 0o644),
                ("SABnzbd-5.1.3/requirements.txt", b"sabctools==9.6.3\ncherrypy==18.10.0\n", 0o644),
                ("SABnzbd-5.1.3/sabnzbd/version.py", b'__version__ = "5.1.3"\n', 0o644)]),
            "par2cmdline-turbo-1.5.0-linux-amd64.zip": z.getvalue(),
            "7z2601-linux-x64.tar.xz": self._tar([("7zz", b"dyn", 0o755), ("7zzs", sevenz, 0o755)],
                                                 mode="w:xz"),
            "rarlinux-x64-723.tar.gz": self._tar([("rar/unrar", unrar, 0o755), ("rar/rar", rar, 0o755)]),
        }
        self.pdir = self.tmp / "payloads"
        self.pdir.mkdir()
        self.sha = {}
        for name, data in self.payloads.items():
            (self.pdir / name).write_bytes(data)
            self.sha[name] = hashlib.sha256(data).hexdigest()

    def _w(self, name: str, body: str):
        p = self.stub / name
        p.write_text("#!/usr/bin/env bash\n" + body, newline="\n")
        p.chmod(0o755)

    def _stubs(self):
        P, C, Q = _posix(self.proc), _posix(self.calls), _posix(self.qstate)
        INI = _posix(self.appdir / "sabnzbd.ini")
        self._w("appctl", f'''echo "appctl $*" >> "{C}"
case "$1" in
  version) echo '{{"data": {{"version": "'"${{FAKE_UCC_VERSION:-5.1.3}}"'"}}, "result": true}}' ;;
  stop) [ "${{FAKE_CONTAINER_STICKS:-0}}" = 1 ] || rm -rf "{P}/9001" ;;
  start) [ "${{FAKE_START_FAILS:-0}}" = 1 ] && exit 1
         mkdir -p "{P}/9001/task/1"
         printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9001/status"
         echo "0::/system.slice/docker-abc.scope" > "{P}/9001/cgroup"
         printf 'python3\\0/app/sabnzbd/SABnzbd.py\\0' > "{P}/9001/cmdline" ;;
  is-native) v="${{FAKE_ISNATIVE:-ucc}}"; echo "$v"; [ "$v" = native ] ;;
esac
''')
        # enable --now: the native SAB starts and, like the real one, writes the
        # --server host/port it was given back into the ini.
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
            printf 'python\\0-OO\\0/h/.apps/sabnzbd/bin/5.1.3/SABnzbd.py\\0' > "{P}/9002/cmdline"
            sed -i 's/^host = .*/host = 172.17.0.1/; s/^port = .*/port = {PORT}/' "{INI}"
          fi ;;
  stop) rm -rf "{P}/9002" ;;
  mask) [ -s "$U/$2" ] && [ ! -L "$U/$2" ] && {{ echo "Failed to mask unit: File $U/$2 already exists." >&2; exit 1; }}
        ln -sf /dev/null "$U/$2" ;;
  unmask) {{ [ -L "$U/$2" ] || [ ! -s "$U/$2" ]; }} && rm -f "$U/$2" ;;
esac
exit 0
''')
        # Container: the three docker-proxy listeners. Native: SAB on the bridge
        # gateway + the loopback forwarder (FAKE_NO_FWD drops the forwarder).
        self._w("ss", f'''if [ -d "{P}/9001" ]; then
  echo "LISTEN 0 65535 {PUBLIC} 0.0.0.0:*"
  echo "LISTEN 0 65535 172.17.0.1:{PORT} 0.0.0.0:*"
  echo "LISTEN 0 65535 127.0.0.1:{PORT} 0.0.0.0:*"
fi
if [ -d "{P}/9002" ]; then
  echo "LISTEN 0 1024 172.17.0.1:{PORT} 0.0.0.0:*"
  [ "${{FAKE_NO_FWD:-0}}" = 1 ] || echo "LISTEN 0 100 127.0.0.1:{PORT} 0.0.0.0:*"
fi
[ -n "${{FAKE_SS_EXTRA:-}}" ] && echo "$FAKE_SS_EXTRA"
exit 0
''')
        self._w("ps", 'n=${FAKE_TASKS:-1000}; for i in $(seq 1 "$n"); do echo x; done\n')
        self._w("ldd", 'for l in ${FAKE_LDD_MISSING:-}; do echo "\t$l => not found"; done\n'
                       '[ "${FAKE_LDD_FAILS:-0}" = 1 ] && { echo "ldd: boom" >&2; exit 1; }\n'
                       'case "$1" in */unrar) echo "\tlibc.so.6 => /lib/libc.so.6 (0x1)"; exit 0 ;; esac\n'
                       'echo "\tnot a dynamic executable"; exit 1\n')
        self._w("fuser", f'[ "${{FAKE_DB_HELD:-0}}" = 1 ] && {{ echo "$1: 9001" >&2; exit 0; }}\nexit 1\n')
        self._w("curl", f'''u=""; out=""
while [ $# -gt 0 ]; do [ "$1" = -o ] && {{ out="$2"; shift; }}; u="$1"; shift; done
echo "download ${{u##*/}}" >> "{C}"
cp "{_posix(self.pdir)}/${{u##*/}}" "$out"
''')
        # Live SAB: API + page, answered while either runtime exists.
        self._w("sabapi", f'''w=0; u=""
for a in "$@"; do [ "$a" = -w ] && w=1; u="$a"; done
up=0; {{ [ -d "{P}/9001" ] || [ -d "{P}/9002" ]; }} && up=1
if [ "$w" = 1 ]; then
  if [ "$up" = 1 ] && [ "${{FAKE_HTTP_FAILS:-0}}" != 1 ]; then printf 200; else printf 000; fi; exit 0
fi
[ "$up" = 1 ] || exit 7
case "$u" in *apikey={KEY}*) ;; *) echo '{{"error":"API Key Incorrect"}}'; exit 0 ;; esac
mode="$(printf '%s' "$u" | sed -n 's/.*mode=\\([a-z]*\\).*/\\1/p')"
echo "api $mode" >> "{C}"
case "$mode" in
  version) echo '{{"version": "'"${{FAKE_API_VERSION:-5.1.3}}"'"}}' ;;
  queue) echo '{{"queue": {{"paused": '"$(cat "{Q}")"', "slots": []}}}}' ;;
  pause) [ "${{FAKE_PAUSE_LIES:-0}}" = 1 ] || echo true > "{Q}"; echo '{{"status": true}}' ;;
  resume) [ "${{FAKE_RESUME_LIES:-0}}" = 1 ] || echo false > "{Q}"; echo '{{"status": true}}' ;;
  history) echo '{{"history": {{"slots": [{{"status": "'"${{FAKE_PP_STATUS:-Completed}}"'"}}]}}}}' ;;
esac
''')
        self._w("hostpolicy", f'''echo "hostpolicy $*" >> "{C}"
case "$1" in
  preflight) [ -n "${{FAKE_PROFILE-ultra}}" ] || exit 2; echo "${{FAKE_PROFILE-ultra}}" ;;
  in-window) exit "${{FAKE_INWINDOW_RC:-1}}" ;;
  task-ceiling) echo "${{FAKE_CEILING:-2000}}" ;;
esac
''')
        # systemd-run --user --pipe --wait --quiet --working-directory=D -p Environment=PATH=P CMD...
        self._w("systemd-run", f'''echo "systemd-run $*" >> "{C}"
wd=""; ep=""
while [ $# -gt 0 ]; do
  case "$1" in
    --user|--pipe|--wait|--quiet) shift ;;
    --working-directory=*) wd="${{1#*=}}"; shift ;;
    -p) case "$2" in Environment=PATH=*) ep="${{2#Environment=PATH=}}" ;; esac; shift 2 ;;
    *) break ;;
  esac
done
cd "$wd" || exit 1
# Windows test hosts only: C:/x -> /c/x so the ':'-separated PATH survives.
ep="$(printf '%s' "$ep" | sed -E 's#(^|:)([A-Za-z]):/#\\1/\\L\\2/#g')"
B="$(command -v bash)"
PATH="$ep:$(dirname "$B")" "$@"
''')
        # The venv base python: `-m venv DIR` writes a shim that plays pip and the
        # sabctools import, registers the fake /proc tree for SABnzbd.py, and
        # otherwise runs the real interpreter.
        real = _posix(sys.executable)
        self._w("sabpy", f'''echo "sabpy $*" >> "{C}"
[ "$1" = -m ] && [ "$2" = venv ] || exit 2
mkdir -p "$3/bin"
cat > "$3/bin/python" <<'SHIM'
#!/usr/bin/env bash
if [ "$1" = -m ] && [ "$2" = pip ]; then
  echo "pip ${{*:3}}" >> "{C}"
  [ "$3" = freeze ] && echo "sabctools==9.6.3"
  exit "${{FAKE_PIP_RC:-0}}"
fi
if [ "$1" = -c ]; then case "$2" in *sabctools*) echo "${{FAKE_SABCTOOLS:-9.6.3}}"; exit 0 ;; esac; fi
for a in "$@"; do case "$a" in *SABnzbd.py)
  mkdir -p "$QFLIX_PROC/$$"
  printf 'Name:\\tsab\\nPPid:\\t1\\n' > "$QFLIX_PROC/$$/status"
  for i in $(seq 1 "${{FAKE_SAB_TASKS:-23}}"); do mkdir -p "$QFLIX_PROC/$$/task/$i"; done ;;
esac; done
exec "{real}" "$@"
SHIM
chmod 755 "$3/bin/python"
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
                 MANITOBA_STATE_DIR=_posix(self.state),
                 QFLIX_MANIFEST=_posix(self.manifest),
                 QFLIX_PROC=_posix(self.proc),
                 QFLIX_PYTHON=_posix(sys.executable),
                 QFLIX_SAB_PYTHON=_posix(self.stub / "sabpy"),
                 QFLIX_APPCTL=_posix(self.stub / "appctl"),
                 QFLIX_SYSTEMCTL=_posix(self.stub / "systemctl"),
                 QFLIX_SYSTEMD_RUN=_posix(self.stub / "systemd-run"),
                 QFLIX_SS=_posix(self.stub / "ss"),
                 QFLIX_PS=_posix(self.stub / "ps"),
                 QFLIX_LDD=_posix(self.stub / "ldd"),
                 QFLIX_FUSER=_posix(self.stub / "fuser"),
                 QFLIX_CURL=_posix(self.stub / "curl"),
                 QFLIX_API_CURL=_posix(self.stub / "sabapi"),
                 QFLIX_HOSTPOLICY=_posix(self.stub / "hostpolicy"),
                 QFLIX_SABNZBD_SHA256=self.sha["SABnzbd-5.1.3-src.tar.gz"],
                 QFLIX_PAR2_SHA256=self.sha["par2cmdline-turbo-1.5.0-linux-amd64.zip"],
                 QFLIX_7ZIP_SHA256=self.sha["7z2601-linux-x64.tar.xz"],
                 QFLIX_UNRAR_SHA256=self.sha["rarlinux-x64-723.tar.gz"],
                 QFLIX_POLL_S="0.1", QFLIX_SETTLE_S="0.2",
                 QFLIX_STOP_TIMEOUT_S="3", QFLIX_PROOF_TIMEOUT_S="30",
                 QFLIX_PAUSE_TIMEOUT_S="2", QFLIX_PP_TIMEOUT_S="2")
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
        r = self.run("--swap", "--execute", "--approve-exception")
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def suppressed(self) -> dict:
        p = self.state / "push-suppress.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def swapstate(self) -> dict:
        p = self.swap / "sabnzbd" / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def paused(self) -> str:
        return self.qstate.read_text().strip()


@pytest.fixture()
def box(tmp_path):
    return Box(tmp_path)


# --- static -------------------------------------------------------------------

def test_bash_syntax():
    r = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_pins_exact_version_and_sha256s_matching_versions_env():
    text = INSTALLER.read_text(encoding="utf-8")
    ver = next(l.split("=", 1)[1].strip() for l in
               (REPO / "versions.env").read_text(encoding="utf-8").splitlines()
               if l.startswith("SABNZBD_VERSION="))
    assert ver == "5.1.3"                      # == `app-sabnzbd version` on the box, 2026-10-10
    assert f'VERSION="{ver}"' in text
    # upstream-published digests (GitHub release asset sha256) + rarlab's tarball
    for sha in ("12a01e30ce166297a375ffc3a761f98bf7d93260e040391497f643f8a3525fed",
                "5a9f64386813456693c2ea1fb7649436fe7544bbdf97fd73b3483dfcc8aca464",
                "8ea0fc8a135e7b848e80a4116fe22dff56c8c4518dde1f43cce67f4e340b437a",
                "759b4b6aa0d9f77131882162951193f3a0e54bf60e1d8dc4255aa308accab588"):
        assert sha in text
    assert "SABnzbd-${VERSION}-src.tar.gz" in text


def test_240_stages_and_deploys_the_installer_forwarder_and_sanitizer():
    text = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    for f in ("scripts/configure/308-native-sabnzbd-install.sh", "scripts/lib/qflix-tcpfwd.py",
              "scripts/maint/native_sanitize.py"):
        assert f"    {f} \\\n" in text, f
    assert ("~/scripts/configure/308-native-sabnzbd-install.sh\n"
            "chmod +x ~/scripts/configure/308-native-sabnzbd-install.sh") in text
    assert '"$STG"/scripts/lib/qflix-tcpfwd.py ~/scripts/lib/qflix-tcpfwd.py' in text
    assert '"$STG"/scripts/maint/native_sanitize.py ~/scripts/maint/native_sanitize.py' in text


def test_installer_never_calls_the_panel_tool_directly_nor_binds_wide():
    code = "\n".join(l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
                     if not l.strip().startswith("#"))
    assert "app-sabnzbd" not in code
    assert "172.17.0.1" not in code     # the bind comes from the net.app_host secret (C-12)
    assert 'BIND_HOST=""' in code and 'BIND_HOST="$h"' in code


def test_golden_unit_is_what_the_installer_renders(box):
    box.installed()
    staged = box.appdir / "native" / UNIT
    assert staged.read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    unit = GOLDEN_UNIT.read_text(encoding="utf-8")
    assert "ExecStart=%h/.apps/sabnzbd/bin/current/qflix-sabnzbd\n" in unit
    # G-3: par2/unrar/7zz live in bin/current, which leads the unit PATH
    assert "Environment=PATH=%h/.apps/sabnzbd/bin/current:%h/bin:/usr/local/bin:/usr/bin:/bin\n" in unit
    assert "EnvironmentFile=%h/.config/qflix/sabnzbd.env" in unit
    assert "TasksMax" not in unit


# --- inert by default -----------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["--install"], ["--prove"], ["--swap"], ["--finish"],
                                  ["--rollback"], ["--swap", "--approve-exception"]])
def test_without_execute_nothing_is_touched(box, args):
    before = sorted(p.as_posix() for p in box.tmp.rglob("*"))
    r = box.run(*args)
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    after = sorted(p.as_posix() for p in box.tmp.rglob("*") if p.name != "host.id")
    assert after == before
    assert box.calls_text() == ""


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


# --- step 1: pin + install --------------------------------------------------------

def test_install_lays_out_release_helpers_venv_env_and_stages_unit(box):
    r = box.installed()
    rel = box.appdir / "bin" / "5.1.3"
    for f in ("SABnzbd.py", "par2", "unrar", "7zz", "qflix-sabnzbd", "qflix-tcpfwd.py",
              "venv/bin/python", ".pip-freeze"):
        assert (rel / f).exists(), f
    assert (box.appdir / "bin" / "current" / "qflix-sabnzbd").exists()
    # the static 7zzs build is the one installed as 7zz
    assert (rel / "7zz").read_bytes().startswith(b"#!/usr/bin/env bash")
    assert "pip install --disable-pip-version-check --no-cache-dir --prefer-binary -q -r" in box.calls_text()
    assert "sabctools 9.6.3" in r.stdout
    env = (box.envdir / "sabnzbd.env").read_text().splitlines()
    assert "SAB_BIND=172.17.0.1" in env and f"SAB_PORT={PORT}" in env
    assert "SAB_LOOPBACK=127.0.0.1" in env and "MALLOC_ARENA_MAX=2" in env
    assert f"SAB_INI={_posix(box.appdir / 'sabnzbd.ini')}" in env
    assert "TZ=Etc/UTC" in env and "PYTHONIOENCODING=utf-8" in env
    assert not any(l.startswith(("SECRET", "HOME")) for l in env)
    assert not any("0.0.0.0" in l for l in env)
    assert (box.appdir / "native" / UNIT).exists() and not (box.unitdir / UNIT).exists()
    assert "enable" not in box.calls_text()
    assert box.container_running()
    assert "host = ::" in box.ini()              # the live ini is not touched by install


def test_wrapper_starts_the_forwarder_before_exec_and_binds_one_host(box):
    box.installed()
    w = (box.appdir / "bin" / "5.1.3" / "qflix-sabnzbd").read_text()
    fwd = w.index('qflix-tcpfwd.py" --listen "$SAB_LOOPBACK:$SAB_PORT" --target "$SAB_BIND:$SAB_PORT" &')
    ex = w.index('exec "$py" -OO "$here/SABnzbd.py" --config-file "$SAB_INI" --server "$SAB_BIND:$SAB_PORT" --browser 0')
    assert fwd < ex


def test_install_refuses_version_mismatch_before_downloading(box):
    r = box.run("--install", "--execute", env={"FAKE_UCC_VERSION": "5.1.2"})
    assert r.returncode != 0 and "parity" in r.stderr
    assert "download" not in box.calls_text()
    assert not (box.appdir / "bin").exists()


@pytest.mark.parametrize("var", ["QFLIX_SABNZBD_SHA256", "QFLIX_PAR2_SHA256", "QFLIX_7ZIP_SHA256",
                                 "QFLIX_UNRAR_SHA256"])
def test_install_refuses_any_sha_mismatch(box, var):
    r = box.run("--install", "--execute", env={var: "0" * 64})
    assert r.returncode != 0 and "sha256" in r.stderr
    assert not (box.appdir / "bin").exists()
    assert not list(box.apps.glob(".stage-*"))


def test_install_refuses_a_helper_with_missing_libraries(box):
    r = box.run("--install", "--execute", env={"FAKE_LDD_MISSING": "libstdc++.so.6"})
    assert r.returncode != 0 and "missing libraries" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_install_refuses_when_pip_fails_or_sabctools_is_not_the_pin(box):
    r = box.run("--install", "--execute", env={"FAKE_PIP_RC": "1"})
    assert r.returncode != 0 and "pip install" in r.stderr
    r = box.run("--install", "--execute", env={"FAKE_SABCTOOLS": "8.2.0"})
    assert r.returncode != 0 and "sabctools" in r.stderr
    assert not (box.appdir / "bin").exists()


@pytest.mark.parametrize("bad", ["0.0.0.0", "127.0.0.1", "", "localhost"])
def test_install_refuses_a_wide_or_loopback_net_app_host(box, bad):
    (box.secrets / "net.app_host").write_text(bad + "\n")
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and "net.app_host" in r.stderr
    assert not (box.appdir / "bin").exists()


@pytest.mark.parametrize("secret", ["sabnzbd.port", "sabnzbd.key"])
def test_install_refuses_without_the_port_or_key_secret(box, secret):
    (box.secrets / secret).unlink()
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and secret in r.stderr


def test_install_warns_on_pause_on_post_processing(box):
    box.write_ini(pause_on_post_processing="1")
    r = box.installed()
    assert "pause_on_post_processing=1" in r.stdout and "WARN" in r.stdout
    assert "pause_on_post_processing = 1" in box.ini()     # carried, never changed


# --- step 2: proof -----------------------------------------------------------------

def test_prove_boots_a_sanitized_copy_on_loopback_and_runs_the_helper_fixture(box):
    r = box.proved()
    assert "PROOF OK" in r.stdout and "API 5.1.3" in r.stdout
    proof = json.loads((box.swap / "sabnzbd" / "proof.json").read_text())
    assert proof["ok"] is True and proof["delta"] == 23 and proof["ceiling"] == 2000
    assert not (box.apps / ".prove" / "sabnzbd").exists()
    calls = box.calls_text()
    # G-3: helpers are resolved under systemd-run --user --pipe with the UNIT's PATH
    run = next(l for l in calls.splitlines() if l.startswith("systemd-run"))
    assert "--user --pipe --wait" in run
    path = run.split("Environment=PATH=", 1)[1].split()[0]
    assert path.startswith(f"{_posix(box.appdir)}/bin/current:")       # == the golden unit's PATH
    assert path.endswith("/home/bin:/usr/local/bin:/usr/bin:/bin")
    # the live app is untouched
    assert box.container_running() and "appctl stop" not in calls
    assert "host = ::" in box.ini() and "enable = 1" in box.ini()


def test_prove_copy_is_sanitized_and_points_at_no_live_dir(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"QFLIX_KEEP_PROOF": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    copy = (box.apps / ".prove" / "sabnzbd" / "sabnzbd.ini").read_text()
    assert "enable = 0" in copy and "enable = 1" not in copy
    assert _posix(box.downloads) not in copy
    counts = json.loads((box.apps / ".prove" / "sabnzbd" / "sanitize.json").read_text())["counts"]
    assert sum(counts.values()) == 0
    text = INSTALLER.read_text(encoding="utf-8")
    assert '--server "127.0.0.1:$pp"' in text


def test_prove_refuses_at_seventy_percent_of_ceiling(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_TASKS": "1380"})   # 1380 + 23 >= 1400
    assert r.returncode == 1 and "70%" in r.stderr
    assert not (box.swap / "sabnzbd" / "proof.json").exists()
    assert not (box.apps / ".prove" / "sabnzbd").exists()


def test_prove_fails_when_par2_cannot_repair(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_PAR2_BROKEN": "1"})
    assert r.returncode != 0 and "fixture" in r.stderr
    assert not (box.swap / "sabnzbd" / "proof.json").exists()


def test_prove_fails_when_a_helper_resolves_outside_bin_current(box):
    box.installed()
    rel = box.appdir / "bin" / "5.1.3"
    (box.home / "bin").mkdir(exist_ok=True)
    shutil.move(str(rel / "unrar"), str(box.home / "bin" / "unrar"))
    cur = box.appdir / "bin" / "current" / "unrar"     # MSYS ln -s may copy instead of link
    if cur.exists():
        cur.unlink()
    r = box.run("--prove", "--execute")
    assert r.returncode != 0 and "unrar" in r.stderr and "bin/current" in r.stderr
    assert not (box.swap / "sabnzbd" / "proof.json").exists()


def test_prove_refuses_an_uninstalled_box(box):
    r = box.run("--prove", "--execute")
    assert r.returncode != 0 and "--install" in r.stderr


# --- steps 3-6: swap ------------------------------------------------------------------

def test_swap_refuses_without_a_proof(box):
    box.installed()
    r = box.run("--swap", "--execute", "--approve-exception")
    assert r.returncode != 0 and "prove" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_refuses_unless_the_pending_swap_flip_is_deployed(box):
    box.proved()
    box.set_manifest(cls="ucc", dormant=False)
    r = box.run("--swap", "--execute", "--approve-exception")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert box.container_running()


def test_swap_refuses_the_public_listener_without_operator_approval(box):
    box.proved()
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and PUBLIC in r.stderr and "--approve-exception" in r.stderr
    assert box.container_running() and not box.suppressed()
    assert "appctl stop" not in box.calls_text() and "api pause" not in box.calls_text()


def test_swap_full_sequence(box):
    r = box.swapped()
    calls = box.calls_text()
    order = [calls.index("api pause"), calls.index("appctl stop sabnzbd"),
             calls.index(f"systemctl --user enable --now {UNIT}"), calls.rindex("api resume")]
    assert order == sorted(order)
    assert not box.container_running() and box.native_running()
    assert (box.unitdir / UNIT).read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    sw = box.swap / "sabnzbd"
    assert (sw / "listen-set.before").read_text().split() == sorted(
        [f"127.0.0.1:{PORT}", f"172.17.0.1:{PORT}", PUBLIC])
    st = box.swapstate()
    assert st["ucc_version"] == "5.1.3" and st["port"] == int(PORT)
    assert st["swap_date"] and st["soak_until"] and st["rollback_window"] == "open"
    assert st["exceptions"] == [PUBLIC] and "D-4" in st["exception_reasons"][PUBLIC]
    # step 3 recorded: auth bypass off, no container paths, the container's own host/port
    audit = json.loads((sw / "ini-audit.json").read_text())
    assert audit["auth"] == {"api_key_matches_secret": True, "username_set": True,
                             "password_set": True, "api_key_disabled": False}
    assert audit["violations"] == []
    assert (sw / "ini-listen.before").read_text().split() == ["host=::", "port=8080"]
    assert list(sw.glob("snapshot-*.tgz")) and (sw / "sabnzbd.ini.before").exists()
    # app + canaries muted together and STILL muted (pending-swap); queue resumed
    assert set(box.suppressed()) == SUPPRESSED
    assert box.paused() == "false"
    assert "elapsed=" in r.stdout


def test_swap_suppresses_before_pausing_or_stopping(box):
    box.proved()
    w = box.stub / "sabapi"
    w.write_text(w.read_text().replace(
        '  pause)', f'  pause) grep -q canary-sab-stall "{_posix(box.state)}/push-suppress.json" 2>/dev/null '
                    f'|| echo "pause-before-suppress" >> "{_posix(box.calls)}";'), newline="\n")
    r = box.run("--swap", "--execute", "--approve-exception")
    assert r.returncode == 0, r.stderr
    assert "pause-before-suppress" not in box.calls_text()


def test_swap_leaves_an_operator_paused_queue_paused(box):
    box.proved()
    box.qstate.write_text("true")
    r = box.run("--swap", "--execute", "--approve-exception")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "api pause" not in box.calls_text() and "api resume" not in box.calls_text()
    assert box.paused() == "true" and "left paused" in r.stdout


@pytest.mark.parametrize("repl,msg", [
    ({"api_key": "ffffffffffffffffffffffffffffffff"}, "differs from secret"),
    ({"username": '""'}, "auth bypass"),
    ({"download_dir": "/downloads/incomplete"}, "container path"),
    ({"complete_dir": "/config/complete"}, "container path"),
])
def test_swap_refuses_an_ini_that_fails_the_audit(box, repl, msg):
    box.proved()
    box.write_ini(**repl)
    r = box.run("--swap", "--execute", "--approve-exception")
    assert r.returncode != 0 and msg in r.stdout + r.stderr
    assert box.container_running() and not box.suppressed()
    assert "appctl stop" not in box.calls_text()


def test_swap_aborts_when_the_queue_never_reports_paused(box):
    box.proved()
    r = box.run("--swap", "--execute", "--approve-exception", env={"FAKE_PAUSE_LIES": "1"})
    assert r.returncode != 0 and "paused" in r.stderr and "aborted" in r.stderr
    assert box.container_running() and "appctl stop" not in box.calls_text()
    assert not box.suppressed()


def test_swap_waits_out_post_processing_then_aborts(box):
    box.proved()
    r = box.run("--swap", "--execute", "--approve-exception", env={"FAKE_PP_STATUS": "Extracting"})
    assert r.returncode != 0 and "post-processing" in r.stderr
    assert box.container_running() and "appctl stop" not in box.calls_text()
    assert box.paused() == "false" and not box.suppressed()     # resumed + unmuted


def test_swap_aborts_when_container_never_exits_and_restores_service(box):
    box.proved()
    r = box.run("--swap", "--execute", "--approve-exception", env={"FAKE_CONTAINER_STICKS": "1"})
    assert r.returncode != 0 and "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert not box.native_running() and box.container_running()
    assert box.paused() == "false" and not box.suppressed()


def test_swap_aborts_while_the_db_is_still_held(box):
    box.proved()
    r = box.run("--swap", "--execute", "--approve-exception", env={"FAKE_DB_HELD": "1"})
    assert r.returncode != 0 and "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()


def test_swap_parity_failure_keeps_suppression_and_the_queue_paused(box):
    box.proved()
    r = box.run("--swap", "--execute", "--approve-exception", env={"FAKE_NO_FWD": "1"})
    assert r.returncode != 0 and "--rollback" in r.stderr and "listen set" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED
    assert box.paused() == "true"


def test_swap_is_resumable_when_already_swapped(box):
    box.swapped()
    r = box.run("--swap", "--execute", "--approve-exception")
    assert r.returncode == 0, r.stderr
    assert "already" in r.stdout
    assert box.calls_text().count("appctl stop sabnzbd") == 1


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

def test_rollback_masks_before_stopping_and_restores_the_ini_host_port(box):
    box.swapped()
    assert "host = 172.17.0.1" in box.ini() and f"port = {PORT}" in box.ini()   # SAB wrote --server back
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls_text()
    assert calls.index(f"systemctl --user mask {UNIT}") < calls.index(f"systemctl --user stop {UNIT}")
    assert _masked(box.unitdir / UNIT)
    assert not box.native_running() and box.container_running()
    assert calls.rindex("appctl start sabnzbd") > calls.index(f"systemctl --user stop {UNIT}")
    ini = box.ini()
    assert "host = ::" in ini and "port = 8080" in ini and f"api_key = {KEY}" in ini
    assert box.suppressed() == {} and box.paused() == "false"
    assert "elapsed=" in r.stdout


def test_rollback_pauses_until_manifest_reverted(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--rollback", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 10 and "revert" in r.stderr
    assert not box.native_running() and not box.container_running()
    assert "appctl start" not in box.calls_text()
    assert set(box.suppressed()) == SUPPRESSED
    box.set_manifest(swap_state="pending-swap")
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running() and "port = 8080" in box.ini()


def test_rollback_stays_suppressed_if_the_container_is_not_ready(box):
    box.swapped()
    r = box.run("--rollback", "--execute", env={"FAKE_HTTP_FAILS": "1"}, timeout=240)
    assert r.returncode != 0 and "not ready" in r.stderr
    assert set(box.suppressed()) == SUPPRESSED


def test_drill_rollback_then_reswap_unmasks(box):
    box.swapped()
    assert box.run("--rollback", "--execute").returncode == 0
    r = box.run("--swap", "--execute", "--approve-exception")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"systemctl --user unmask {UNIT}" in box.calls_text()
    assert not _masked(box.unitdir / UNIT)
    assert box.native_running() and not box.container_running()
    # the recorded container host/port survive the second capture
    assert (box.swap / "sabnzbd" / "ini-listen.before").read_text().split() == ["host=::", "port=8080"]


def test_rollback_with_nothing_swapped_is_harmless(box):
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running() and "host = ::" in box.ini()
